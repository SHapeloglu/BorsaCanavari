"""Kalici sistem durumu: `system_state(key, value JSONB)` (migration 006).

Degerler JSON'a guvenli sekilde cevrilir (`to_jsonable`): tarih/zaman -> ISO metin, numpy -> python, NaN/sonsuz -> None
(JSONB NaN kabul etmez; `json.dumps` varsayilani NaN yazip Postgres'te patlardi).
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

import numpy as np
import psycopg
from psycopg.types.json import Jsonb


def to_jsonable(obj: Any) -> Any:
    """Sozluk/liste/tarih/numpy/NaN iceren nesneyi JSONB'ye yazilabilir sade python nesnesine cevirir."""
    if obj is None or isinstance(obj, (str, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        try:
            items = sorted(obj)
        except TypeError:
            items = list(obj)
        return [to_jsonable(v) for v in items]
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [to_jsonable(v) for v in obj.tolist()]
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        x = float(obj)
        return x if math.isfinite(x) else None
    if isinstance(obj, int):
        return obj
    if isinstance(obj, (dt.date, dt.datetime)):
        return obj.isoformat()
    if hasattr(obj, "isoformat"):  # pandas Timestamp / NaT
        try:
            return obj.isoformat()
        except (ValueError, TypeError):
            return None
    return str(obj)


def get_state(conn: psycopg.Connection, key: str) -> Any | None:
    """Anahtarin JSON degerini dondurur; kayit yoksa None."""
    row = conn.execute("SELECT value FROM system_state WHERE key = %s", (key,)).fetchone()
    return None if row is None else row[0]


def set_state(conn: psycopg.Connection, key: str, value: Any) -> None:
    """Anahtari yazar (UPSERT); updated_at DB saatiyle guncellenir."""
    conn.execute(
        "INSERT INTO system_state (key, value, updated_at) VALUES (%s, %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (key, Jsonb(to_jsonable(value))),
    )
