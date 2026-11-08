"""Tahmin defteri yazicisi (PROJE.md Bolum 6, 12).

Kurallar:
- Yalnizca INSERT ... ON CONFLICT (ticker, made_at, horizon_days, model_version) DO NOTHING.
  Mevcut satirin tahmin alanlari ASLA degistirilmez (UPDATE yazilmaz; tetikleyici de engeller).
  Ayni anahtarla farkli prob_up gelirse mevcut kayit korunur ve satir "skipped_existing" sayilir.
- Dogrulama yazmadan once, tum satirlar icin yapilir; hata varsa hicbir sey yazilmaz (ValueError).
- Tek islem (conn.transaction()). dry_run hicbir sey yazmaz ama olacak sayilari hesaplar.
- Ayni anahtar ayni cagrida birden fazla gelirse ilki yazilir, digerleri skipped_existing sayilir.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from dataclasses import dataclass

import numpy as np

MODEL_ROLES = ("champion", "challenger", "baseline")

_INSERT_SQL = (
    "INSERT INTO predictions (ticker, made_at, horizon_days, prob_up, score, model_version,"
    " model_role, feature_hash, features_json, created_at)"
    " VALUES (%(ticker)s, %(made_at)s, %(horizon_days)s, %(prob_up)s, %(score)s, %(model_version)s,"
    " %(model_role)s, %(feature_hash)s, %(features_json)s::jsonb, %(created_at)s)"
    " ON CONFLICT (ticker, made_at, horizon_days, model_version) DO NOTHING"
)
_EXISTS_SQL = (
    "SELECT 1 FROM predictions WHERE ticker = %s AND made_at = %s AND horizon_days = %s"
    " AND model_version = %s"
)


@dataclass(frozen=True)
class PredictionRow:
    ticker: str
    made_at: dt.date
    horizon_days: int
    prob_up: float
    score: float | None
    model_version: str
    model_role: str
    feature_hash: str
    features_json: str

    @property
    def key(self) -> tuple[str, dt.date, int, str]:
        return (self.ticker, self.made_at, self.horizon_days, self.model_version)


@dataclass
class WriteResult:
    inserted: int
    skipped_existing: int
    dry_run: bool


def _is_number(x) -> bool:
    return isinstance(x, (int, float, np.integer, np.floating)) and not isinstance(x, (bool, np.bool_))


def _reject_constant(name: str):
    raise ValueError(f"features_json NaN/Infinity iceremez: {name}")


def _validate(row: PredictionRow, i: int) -> None:
    where = f"satir {i} ({row.ticker!r})"
    if not isinstance(row.ticker, str) or not row.ticker.strip():
        raise ValueError(f"{where}: ticker bos olamaz")
    if not isinstance(row.made_at, dt.date) or isinstance(row.made_at, dt.datetime):
        raise ValueError(f"{where}: made_at datetime.date olmali (datetime/Timestamp degil): {row.made_at!r}")
    if not _is_number(row.horizon_days) or int(row.horizon_days) != row.horizon_days or row.horizon_days <= 0:
        raise ValueError(f"{where}: horizon_days pozitif tam sayi olmali: {row.horizon_days!r}")
    if not _is_number(row.prob_up) or not math.isfinite(row.prob_up) or not 0.0 <= row.prob_up <= 1.0:
        raise ValueError(f"{where}: prob_up 0..1 araliginda sonlu sayi olmali: {row.prob_up!r}")
    if row.score is not None and (not _is_number(row.score) or not math.isfinite(row.score)):
        raise ValueError(f"{where}: score sonlu sayi ya da None olmali: {row.score!r}")
    if not isinstance(row.model_version, str) or not row.model_version.strip():
        raise ValueError(f"{where}: model_version bos olamaz")
    if row.model_role not in MODEL_ROLES:
        raise ValueError(f"{where}: bilinmeyen model_role {row.model_role!r}; gecerliler: {MODEL_ROLES}")
    if not isinstance(row.feature_hash, str) or not row.feature_hash.strip():
        raise ValueError(f"{where}: feature_hash bos olamaz")
    if not isinstance(row.features_json, str):
        raise ValueError(f"{where}: features_json str olmali")
    try:
        json.loads(row.features_json, parse_constant=_reject_constant)
    except ValueError as exc:  # JSONDecodeError de ValueError'dur
        raise ValueError(f"{where}: features_json gecerli JSON degil: {exc}") from exc


def _params(row: PredictionRow, created_at: dt.datetime) -> dict:
    return {
        "ticker": row.ticker,
        "made_at": row.made_at,
        "horizon_days": int(row.horizon_days),
        "prob_up": float(row.prob_up),
        "score": None if row.score is None else float(row.score),
        "model_version": row.model_version,
        "model_role": row.model_role,
        "feature_hash": row.feature_hash,
        "features_json": row.features_json,
        "created_at": created_at,
    }


def write_predictions(conn, rows, *, dry_run: bool = False, created_at: dt.datetime | None = None) -> WriteResult:
    rows = list(rows)
    for i, row in enumerate(rows):
        _validate(row, i)
    if created_at is None:
        created_at = dt.datetime.now(dt.timezone.utc)
    elif created_at.tzinfo is None:
        raise ValueError("created_at tz-aware olmali")

    with conn.transaction():
        if dry_run:
            seen: set = set()
            would_insert = 0
            for row in rows:
                if row.key in seen:
                    continue
                seen.add(row.key)
                if conn.execute(_EXISTS_SQL, row.key).fetchone() is None:
                    would_insert += 1
            return WriteResult(inserted=would_insert, skipped_existing=len(rows) - would_insert, dry_run=True)
        inserted = 0
        for row in rows:
            cur = conn.execute(_INSERT_SQL, _params(row, created_at))
            inserted += cur.rowcount
    return WriteResult(inserted=inserted, skipped_existing=len(rows) - inserted, dry_run=False)


def _canonical(obj):
    if isinstance(obj, dict):
        for k in obj:
            if not isinstance(k, str):
                raise ValueError(f"anahtarlar str olmali: {k!r}")
        return {k: _canonical(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_canonical(v) for v in obj]
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        x = float(obj)
        if not math.isfinite(x):
            raise ValueError(f"NaN/sonsuz deger hash'lenemez: {obj!r}")
        return float(f"{x:.10g}")
    if obj is None or isinstance(obj, str):
        return obj
    raise ValueError(f"json'lanamayan tip: {type(obj).__name__}")


def baseline_feature_hash(inputs: dict) -> tuple[str, str]:
    """(features_json, feature_hash): kanonik JSON (sort_keys, ayirici (",",":")), floatlar .10g, sha256.

    Not: int ile float farkli temsil edilir (5 ve 5.0 ayni hash'i vermez); cagiran tip tutarliligini korur.
    """
    features_json = json.dumps(_canonical(inputs), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return features_json, hashlib.sha256(features_json.encode("utf-8")).hexdigest()
