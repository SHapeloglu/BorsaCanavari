"""Is takibi: `runs` tablosu + advisory lock (PROJE.md Bolum 6, 12).

- Ayni isin (job) iki kopyasi ayni anda calismaz: `pg_try_advisory_lock` (oturum duzeyi). Kilit alinamazsa
  `JobLocked` firlatilir ve (record=True ise) `runs`a 'skipped' satiri yazilir; cagiran bunu raporlar.
- Her calistirma `runs`a yazilir (running -> ok | skipped | failed). `run.status` normal cikista cagiran tarafindan
  'skipped' yapilabilir (ornegin bayat veri). `checkpoint` adim sonuclarini hemen kaydeder: is sonradan patlasa bile
  `runs.detail` o ana kadarkini icerir.
- `record=False` (dry-run) hicbir sey yazmaz ama kilidi yine alir.
- Baglanti autocommit olmali (CLI boyle acar); aksi halde runs guncellemeleri cagiranin islemine baglanir.
"""

from __future__ import annotations

import datetime as dt
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import psycopg
from psycopg.types.json import Jsonb

OK, SKIPPED, FAILED = "ok", "skipped", "failed"


class JobLocked(RuntimeError):
    """Ayni is zaten calisiyor."""


@dataclass
class JobRun:
    job: str
    run_date: dt.date
    run_id: int | None = None
    detail: dict = field(default_factory=dict)
    status: str = OK  # normal cikista yazilacak durum


def _lock_key(job: str) -> int:
    return zlib.crc32(f"bist_tahmin_job:{job}".encode())


def checkpoint(conn: psycopg.Connection, run: JobRun) -> None:
    """Eldeki `run.detail`i hemen kaydeder (durum 'running' kalir)."""
    if run.run_id is None:
        return
    conn.execute("UPDATE runs SET detail = %s WHERE id = %s", (Jsonb(_jsonable(run.detail)), run.run_id))


@contextmanager
def tracked_job(
    conn: psycopg.Connection, job: str, run_date: dt.date, *, record: bool = True
) -> Iterator[JobRun]:
    key = _lock_key(job)
    if not conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]:
        if record:  # kilit baskasinda: iz birak (silent skip olmasin)
            conn.execute(
                "INSERT INTO runs (job, run_date, started_at, finished_at, status, detail) "
                "VALUES (%s, %s, now(), now(), 'skipped', %s)",
                (job, run_date, Jsonb({"reason": "locked"})),
            )
        raise JobLocked(f"{job} zaten calisiyor")
    run = JobRun(job=job, run_date=run_date)
    try:
        if record:
            run.run_id = conn.execute(
                "INSERT INTO runs (job, run_date, started_at, status) VALUES (%s, %s, now(), 'running') RETURNING id",
                (job, run_date),
            ).fetchone()[0]
        try:
            yield run
        except BaseException as exc:
            _finish(conn, run, FAILED, {**run.detail, "error": f"{type(exc).__name__}: {exc}"[:500]})
            raise
        else:
            _finish(conn, run, run.status, run.detail)
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (key,))


def _jsonable(obj):
    """Sozluk/liste/Timestamp/tarih iceren ayrintilari JSON'a uygun hale getirir (anahtarlar str)."""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in (sorted(obj) if isinstance(obj, set) else obj)]
    if isinstance(obj, (dt.date, dt.datetime)):
        return obj.isoformat()
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return obj


def _finish(conn: psycopg.Connection, run: JobRun, status: str, detail: dict) -> None:
    if run.run_id is None:
        return
    conn.execute(
        "UPDATE runs SET finished_at = now(), status = %s, detail = %s WHERE id = %s",
        (status, Jsonb(_jsonable(detail)), run.run_id),
    )
