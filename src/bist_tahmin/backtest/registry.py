"""Deney kaydi (PROJE.md Bolum 8, 11): deneme sayisi Bonferroni duzeltmesinin paydasidir.

Kurallar:
- Deney SONUCTAN ONCE kaydedilir; kayit = deneme sayilir. bist_app'in DELETE yetkisi yoktur, yani
  sonucu kotu cikan deneme sayidan dusurulemez.
- result_json yalniz BIR KEZ yazilabilir (UPDATE ... WHERE result_json IS NULL; yarisa karsi atomik).
- Eszamanli kayit guvenlidir: her kayit tek bir INSERT'tur, sayac COUNT(*)'tur.
- JSON'a gelmeyen degerler (numpy, Timestamp, date, set, Path) donusturulur; NaN/inf null olur
  (JSONB NaN kabul etmez).
"""

from __future__ import annotations

import datetime as dt
import json
import math
from collections.abc import Mapping
from pathlib import PurePath
from typing import Any

import numpy as np
import pandas as pd
from psycopg import Connection

from bist_tahmin.evaluate.bootstrap import bonferroni_alpha


def _jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, bool)):
        return obj
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if obj is pd.NaT:
        return None
    if isinstance(obj, (pd.Timestamp, dt.datetime, dt.date, dt.time)):
        return obj.isoformat()
    if isinstance(obj, np.datetime64):
        return pd.Timestamp(obj).isoformat()
    if isinstance(obj, (pd.Timedelta, dt.timedelta)):
        return pd.Timedelta(obj).isoformat()
    if isinstance(obj, PurePath):
        return str(obj)
    if isinstance(obj, Mapping):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            key = k if isinstance(k, str) else str(_jsonable(k))
            if key in out:
                raise ValueError(f"JSON anahtari cakisiyor: {key!r}")
            out[key] = _jsonable(v)
        return out
    if isinstance(obj, np.ndarray):
        return [_jsonable(v) for v in obj.tolist()]
    if isinstance(obj, (set, frozenset)):
        return sorted((_jsonable(v) for v in obj), key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(obj, (list, tuple, pd.Index, pd.Series)):
        return [_jsonable(v) for v in list(obj)]
    raise TypeError(f"JSON'a cevrilemeyen tip: {type(obj).__name__}")


def _dump(obj: Mapping[str, Any], what: str) -> str:
    if not isinstance(obj, Mapping):
        raise TypeError(f"{what} bir sozluk olmali")
    return json.dumps(_jsonable(obj), sort_keys=True, allow_nan=False)


def _check_family(family: str) -> str:
    if not isinstance(family, str) or not family.strip():
        raise ValueError("aile adi bos olamaz")
    return family.strip()


def record_experiment(
    conn: Connection,
    family: str,
    description: str,
    params: Mapping[str, Any],
    *,
    created_at: dt.datetime | None = None,
) -> int:
    """Denemeyi sonuc yazilmadan ONCE kaydeder ve id dondurur."""
    fam = _check_family(family)
    if not isinstance(description, str) or not description.strip():
        raise ValueError("aciklama bos olamaz")
    params_json = _dump(params, "params")  # DB'ye gitmeden once dogrula
    ts = pd.Timestamp(created_at) if created_at is not None else pd.Timestamp.now(tz="UTC")
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    with conn.transaction():
        row = conn.execute(
            "INSERT INTO experiments (created_at, family, description, params_json)"
            " VALUES (%s, %s, %s, %s::jsonb) RETURNING id",
            (ts.to_pydatetime(), fam, description, params_json),
        ).fetchone()
    return int(row[0])


def update_result(conn: Connection, experiment_id: int, result: Mapping[str, Any]) -> None:
    """result_json'i bir kez yazar; zaten doluysa (veya kayit yoksa) ValueError."""
    result_json = _dump(result, "result")
    with conn.transaction():
        row = conn.execute(
            "UPDATE experiments SET result_json = %s::jsonb WHERE id = %s AND result_json IS NULL RETURNING id",
            (result_json, int(experiment_id)),
        ).fetchone()
        if row is None:
            exists = conn.execute("SELECT 1 FROM experiments WHERE id = %s", (int(experiment_id),)).fetchone()
            if exists is None:
                raise ValueError(f"deney yok: {experiment_id}")
            raise ValueError(f"deney {experiment_id} icin sonuc zaten yazilmis (yalnizca bir kez yazilir)")


def count_trials(conn: Connection, family: str) -> int:
    """Ayni ailedeki toplam deneme (kayit) sayisi; sonucu yazilmamislar da dahildir."""
    fam = _check_family(family)
    row = conn.execute("SELECT count(*) FROM experiments WHERE family = %s", (fam,)).fetchone()
    return int(row[0])


def adjusted_alpha(conn: Connection, family: str, alpha: float = 0.05) -> float:
    """Bonferroni: alpha / max(1, deneme sayisi)."""
    return bonferroni_alpha(alpha, max(1, count_trials(conn, family)))


def list_experiments(conn: Connection, family: str | None = None) -> list[dict[str, Any]]:
    """Deneyleri id sirasiyla dondurur (family verilirse yalniz o aile)."""
    sql = "SELECT id, created_at, family, description, params_json, result_json FROM experiments"
    args: tuple[Any, ...] = ()
    if family is not None:
        sql += " WHERE family = %s"
        args = (_check_family(family),)
    sql += " ORDER BY id"
    cols = ("id", "created_at", "family", "description", "params_json", "result_json")
    return [dict(zip(cols, r, strict=True)) for r in conn.execute(sql, args).fetchall()]
