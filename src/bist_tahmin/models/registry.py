"""Model kayit defteri: `models` tablosu uzerinde champion/challenger yasam dongusu (PROJE.md Bolum 6, 11).

Roller ve gecisler (baska gecis ValueError):
    (kayit)     -> challenger | retired   (register_model; champion ile kayit YASAK)
    challenger  -> champion      (YALNIZ promote; mevcut champion AYNI islemde retired olur)
    challenger  -> retired       (retire)
    champion    -> retired       (retire; champion'siz kalinabilir, ornegin devre kesici sonrasi)
    retired     -> (terminal; geri donus yok)

Garantiler:
- Champion atamasi yalnizca `promote` ile. register_model champion'i reddeder (ValueError).
- En fazla BIR champion: DB'de kismi unique index (007_champion_guard.sql `models_one_champion`) ve
  tetikleyici `models_role_guard` (champion yazimi yalnizca oturum ayari app.allow_promote='on' iken).
  Uygulama tarafinda: promote tek islemde, `pg_advisory_xact_lock` + `SELECT ... FOR UPDATE` ile calisir;
  eszamanli terfilerden biri digerini emekliye ayirir, iki champion olusamaz.
- SINIR: app.allow_promote oturum ayari bist_app tarafindan da yazilabilir. DB korumasi denetimsiz/kazara
  champion yazimini engeller; ham SQL ile bilerek bypass'a karsi tam sinir degildir. promote_check'in
  zorunlulugu uygulama kodu seviyesindedir (promote.py); tam DB siniri icin promote'in SECURITY DEFINER
  fonksiyona tasinmasi gerekir (bu surumde yapilmadi).
- `register_model` mevcut bir version'u asla ezmez (ValueError). bist_app yalnizca `role` sutununu UPDATE
  edebilir (003_grants); burada baska alan degistirilmez.
- Her islem kendi `conn.transaction()` blogundadir (cagiran islemdeyse savepoint olur).
"""

from __future__ import annotations

import datetime as dt
import json
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import pandas as pd
from psycopg import Connection

ROLES = ("champion", "challenger", "retired")
ACTIVE_ROLES = ("champion", "challenger")
_REGISTER_ROLES = ("challenger", "retired")
# 007_champion_guard.sql: champion yazimi bu oturum ayariyla izinlidir; yalniz promote() acar.
_PROMOTE_GUC = "app.allow_promote"
_LOCK_KEY = zlib.crc32(b"bist_tahmin_models_lifecycle")
_COLS = "version, role, trained_at, train_end, params_json"


@dataclass(frozen=True)
class ModelRecord:
    version: str
    role: str
    trained_at: dt.datetime
    train_end: dt.date
    params: dict


def _record(row) -> ModelRecord:
    version, role, trained_at, train_end, params = row
    return ModelRecord(version=version, role=role, trained_at=trained_at, train_end=train_end,
                       params=dict(params) if params is not None else {})


def _check_version(version) -> str:
    if not isinstance(version, str) or not version.strip():
        raise ValueError("version bos olmayan metin olmali")
    return version


def _as_date(d) -> dt.date:
    if isinstance(d, pd.Timestamp):
        return d.date()
    if isinstance(d, dt.datetime):
        return d.date()
    if isinstance(d, dt.date):
        return d
    raise ValueError(f"train_end tarih olmali: {d!r}")


def _lock(conn: Connection) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))


def _select_for_update(conn: Connection, version: str) -> ModelRecord | None:
    row = conn.execute(f"SELECT {_COLS} FROM models WHERE version = %s FOR UPDATE", (version,)).fetchone()
    return _record(row) if row else None


def register_model(
    conn: Connection,
    version: str,
    params: dict,
    train_end: dt.date,
    *,
    role: str = "challenger",
    trained_at: dt.datetime | None = None,
) -> ModelRecord:
    """Yeni model satiri ekler (yalniz INSERT). Ayni version varsa ValueError: sessizce ezme/atlama yok.

    role: 'challenger' (varsayilan) ya da 'retired'. 'champion' ValueError: champion yalniz promote() ile atanir.
    params JSON'a (NaN/sonsuz olmadan) cevrilebilir olmalidir.
    """
    _check_version(version)
    if role == "champion":
        raise ValueError("champion register_model ile atanamaz; promote() kullanin")
    if role not in _REGISTER_ROLES:
        raise ValueError(f"kayit rolu {_REGISTER_ROLES} icinden olmali: {role!r}")
    if not isinstance(params, Mapping):
        raise ValueError("params bir sozluk olmali")
    try:
        params_json = json.dumps(dict(params), allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"params JSON'a cevrilemiyor: {exc}") from exc
    end = _as_date(train_end)
    if trained_at is None:
        trained_at = dt.datetime.now(dt.timezone.utc)
    elif trained_at.tzinfo is None:
        raise ValueError("trained_at tz-aware olmali")

    with conn.transaction():
        _lock(conn)
        row = conn.execute(
            "INSERT INTO models (version, trained_at, train_end, params_json, role)"
            " VALUES (%s, %s, %s, %s::jsonb, %s) ON CONFLICT (version) DO NOTHING"
            f" RETURNING {_COLS}",
            (version, trained_at, end, params_json, role),
        ).fetchone()
        if row is None:
            raise ValueError(f"model zaten kayitli: {version}")
    return _record(row)


def list_models(conn: Connection, roles: Sequence[str] | None = None) -> list[ModelRecord]:
    """trained_at artan sirada (esitlikte version). roles verilirse yalniz o roller."""
    sql = f"SELECT {_COLS} FROM models"
    args: tuple = ()
    if roles is not None:
        if isinstance(roles, str):
            raise ValueError("roles bir liste olmali (tek metin degil)")
        bad = [r for r in roles if r not in ROLES]
        if bad:
            raise ValueError(f"bilinmeyen rol(ler): {bad}; gecerliler: {ROLES}")
        if not roles:
            return []
        sql += " WHERE role = ANY(%s)"
        args = (list(roles),)
    sql += " ORDER BY trained_at, version"
    return [_record(r) for r in conn.execute(sql, args).fetchall()]


def load_active_models(conn: Connection) -> list[ModelRecord]:
    """Canli tahmin yazan modeller: champion ve challenger'lar (trained_at artan)."""
    return list_models(conn, ACTIVE_ROLES)


def get_model(conn: Connection, version: str) -> ModelRecord | None:
    row = conn.execute(f"SELECT {_COLS} FROM models WHERE version = %s", (_check_version(version),)).fetchone()
    return _record(row) if row else None


def retire(conn: Connection, version: str) -> None:
    """challenger ya da champion'i emekliye ayirir. Yok ya da zaten retired ise ValueError."""
    _check_version(version)
    with conn.transaction():
        _lock(conn)
        rec = _select_for_update(conn, version)
        if rec is None:
            raise ValueError(f"model yok: {version}")
        if rec.role == "retired":
            raise ValueError(f"model zaten retired (terminal): {version}")
        conn.execute("UPDATE models SET role = 'retired' WHERE version = %s", (version,))


def promote(conn: Connection, challenger_version: str) -> ModelRecord:
    """TEK islemde: challenger -> champion, mevcut champion (varsa) -> retired.

    Kontrol (karar kurali) burada YAPILMAZ; cagiran `promote.promote_check` onayini almis olmalidir.
    Model yoksa, challenger degilse (champion zaten / retired terminal) ValueError.
    """
    _check_version(challenger_version)
    with conn.transaction():
        _lock(conn)
        rec = _select_for_update(conn, challenger_version)
        if rec is None:
            raise ValueError(f"model yok: {challenger_version}")
        if rec.role != "challenger":
            raise ValueError(f"yalniz challenger terfi edebilir; {challenger_version} rolu: {rec.role!r}")
        old = conn.execute("SELECT version FROM models WHERE role = 'champion' FOR UPDATE").fetchall()
        for (v,) in old:
            conn.execute("UPDATE models SET role = 'retired' WHERE version = %s", (v,))
        # Champion yazimi yalniz bu islem icinde izinlidir (SET LOCAL: islem sonunda kendiliginden doner;
        # savepoint/dis islem icinde de hemen 'off' yapilir ki acik kalmasin).
        # Hata olursa islem zaten geri alinir (ayar da doner); bu yuzden try/finally yok.
        conn.execute(f"SET LOCAL {_PROMOTE_GUC} = 'on'")
        conn.execute("UPDATE models SET role = 'champion' WHERE version = %s", (challenger_version,))
        conn.execute(f"SET LOCAL {_PROMOTE_GUC} = 'off'")
        row = conn.execute(f"SELECT {_COLS} FROM models WHERE version = %s", (challenger_version,)).fetchone()
    return _record(row)
