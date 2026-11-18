"""Salt-okunur inceleme bulgularinin duzeltmeleri (ayri dosya; her test bagimsiz)."""

from __future__ import annotations

import datetime as dt
import uuid
import zlib

import numpy as np
import pandas as pd
import psycopg
import pytest

from bist_tahmin import cli
from bist_tahmin.backtest import lab
from bist_tahmin.data.fetch import CALENDAR_SYMBOL, fetch_all
from bist_tahmin.db import APP_ROLE, with_overrides
from bist_tahmin.models import train
from bist_tahmin.predict.ledger import PredictionRow, write_predictions

UTC = dt.timezone.utc
AFTER_CLOSE = dt.datetime(2026, 11, 6, 16, 30, tzinfo=UTC)
WEEK = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"]


def _bars(days):
    idx = pd.DatetimeIndex(pd.to_datetime(days), name="date")
    return pd.DataFrame({"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100.0,
                         "close_adj": 1.5}, index=idx)


class CalendarDown:
    """XU100 indirilemiyor; hisse verisi saglam."""
    name = "fake"

    def __init__(self):
        self.ticker_calls: list[str] = []

    def history(self, ticker, start=None, end=None):
        self.ticker_calls.append(ticker)
        return _bars(WEEK)

    def index_history(self, symbol, start=None, end=None):
        raise RuntimeError("XU100 kaynagi yok")

    def index_components(self, symbol):
        return []

    def corporate_actions(self, ticker):
        return pd.DataFrame({"ex_date": pd.to_datetime([]), "kind": [], "value": []})


# --- madde 2: takvim yoksa hisse yazimi yapilmaz -----------------------------------------------------------------

@pytest.mark.fast
def test_calendar_failure_writes_no_tickers(app_conn):
    t = f"R{uuid.uuid4().hex[:7].upper()}"
    src = CalendarDown()
    res = fetch_all(src, app_conn, [t], "2026-11-01", now=AFTER_CLOSE, pause=0, retries=1)
    assert CALENDAR_SYMBOL in res.errors and not res.ok
    assert src.ticker_calls == []  # hisseler hic indirilmedi
    assert app_conn.execute("SELECT count(*) FROM prices WHERE ticker = %s", (t,)).fetchone()[0] == 0


# --- madde 5: ayni anahtar, farkli icerik -> celiski sayilir, mevcut satir korunur ---------------------------------

def _row(ticker, prob=0.55, fhash="h1"):
    return PredictionRow(ticker=ticker, made_at=dt.date(2026, 11, 2), horizon_days=5, prob_up=prob, score=0.1,
                         model_version="baseline_ma20_v1", model_role="baseline", feature_hash=fhash,
                         features_json='{"rule":"ma20"}')


@pytest.mark.fast
def test_ledger_counts_conflicting_duplicates(app_conn):
    t = f"R{uuid.uuid4().hex[:7].upper()}"
    assert write_predictions(app_conn, [_row(t)]).conflicts == 0
    # ayni icerik: celiski degil
    same = write_predictions(app_conn, [_row(t)])
    assert (same.inserted, same.skipped_existing, same.conflicts) == (0, 1, 0)
    # farkli olasilik: celiski; mevcut satir DEGISMEZ
    diff = write_predictions(app_conn, [_row(t, prob=0.9)])
    assert (diff.inserted, diff.skipped_existing, diff.conflicts) == (0, 1, 1)
    assert app_conn.execute("SELECT prob_up FROM predictions WHERE ticker = %s", (t,)).fetchone()[0] == 0.55
    # farkli ozellik hash'i: celiski
    hdiff = write_predictions(app_conn, [_row(t, fhash="h2")])
    assert hdiff.conflicts == 1


@pytest.mark.fast
def test_ledger_dry_run_reports_no_conflicts(app_conn):
    t = f"R{uuid.uuid4().hex[:7].upper()}"
    write_predictions(app_conn, [_row(t)])
    res = write_predictions(app_conn, [_row(t, prob=0.9)], dry_run=True)
    assert res.conflicts == 0 and res.skipped_existing == 1


# --- madde 3: breaker reset gunluk akisin kilidini bekler --------------------------------------------------------

def _lock_key(job: str) -> int:
    return zlib.crc32(f"bist_tahmin_job:{job}".encode())


@pytest.mark.fast
def test_breaker_reset_refused_while_daily_job_runs(bist_db, monkeypatch):
    admin, name = bist_db
    info = with_overrides(admin, dbname=name, user=APP_ROLE, password="test-app-password")
    holder = psycopg.connect(info, autocommit=True)
    try:
        # Gunluk akis kilidini baska bir oturum tutuyor (= calisan pipeline)
        assert holder.execute("SELECT pg_try_advisory_lock(%s)", (_lock_key("daily"),)).fetchone()[0] is True
        # CLI her cagrida kendi baglantisini acar ve kapatir (with conn:); dogrulama ayri bir baglantiyla yapilir.
        monkeypatch.setattr(cli, "_app_conn_or_none", lambda: psycopg.connect(info, autocommit=True))
        with psycopg.connect(info, autocommit=True) as chk:
            before = chk.execute("SELECT value FROM system_state WHERE key = 'circuit_breaker'").fetchone()
        code = cli.main(["breaker", "reset", "--reason", "test: kilit"])
        assert code == 4
        with psycopg.connect(info, autocommit=True) as chk:
            after = chk.execute("SELECT value FROM system_state WHERE key = 'circuit_breaker'").fetchone()
            row = chk.execute("SELECT status, detail FROM runs WHERE job = 'breaker-reset' ORDER BY id DESC LIMIT 1"
                              ).fetchone()
        assert before == after  # reset calismadi: durum degismedi
        assert row[0] == "skipped" and row[1].get("reason") == "locked"
    finally:
        holder.execute("SELECT pg_advisory_unlock(%s)", (_lock_key("daily"),))
        holder.close()


# --- madde 8: --use-holdout onaysiz calismaz ---------------------------------------------------------------------

@pytest.mark.fast
def test_use_holdout_requires_confirmation(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_app_conn_or_none", lambda: object())
    code = cli.main(["backtest", "run", "--name", "x", "--use-holdout", "--dataset", "yok.pkl"])
    assert code == 2
    assert "--confirm-holdout" in capsys.readouterr().err


# --- madde 11: NaN yuzunden dusen satirlar sayilir ---------------------------------------------------------------

@pytest.mark.fast
def test_select_reports_dropped_nonfinite_rows():
    days = pd.bdate_range("2026-01-01", periods=40)
    rng = np.random.default_rng(0)
    rows = []
    for i, d in enumerate(days):
        for t in ("A", "B"):
            f1 = np.nan if (i == 3 and t == "A") else float(rng.normal())
            rows.append({"date": d, "end_date": d + pd.Timedelta(days=7), "ticker": t,
                         "label_up": float(i % 2), "f1": f1})
    frame = pd.DataFrame(rows).rename(columns={"f1": lab.FEATURE_NAMES[0]})
    spec = lab.ModelSpec(name="t", label="label_up", features=(lab.FEATURE_NAMES[0],))
    _, train_end, dropped = train._select(frame, spec, days[-1], 5, None)
    assert train_end is not None and dropped == 1


# --- madde 18: ozellik surumu eksik veri seti reddedilir (fail-closed) -------------------------------------------

def test_missing_feature_version_is_rejected_before_any_db_access():
    """meta'da feature_version yoksa egitim ve kayit baslamadan ValueError (conn=None: DB'ye hic dokunulmaz)."""
    from types import SimpleNamespace

    ds = SimpleNamespace(horizon_days=5, meta={}, frame=pd.DataFrame({"date": [pd.Timestamp("2026-11-06")]}))
    cfg = {"horizon_days": 5, "training": {"purge_days": 5}}
    with pytest.raises(ValueError, match="ozellik surumu"):
        train.train_and_register(None, cfg, ds, lab.ModelSpec(name="t", features=(lab.FEATURE_NAMES[0],)), pd.Timestamp("2026-11-06"))


# --- madde 12: drift yalnizca istenen ufuktaki satirlari okur -----------------------------------------------------

@pytest.mark.fast
def test_drift_load_resolved_filters_by_horizon(app_conn, new_prediction, insert_prediction):
    from bist_tahmin.monitor import drift

    version = f"hx-{uuid.uuid4().hex[:8]}@1"
    for h in (5, 10):
        row = new_prediction(model_version=version, model_role="champion", horizon_days=h)
        t = row["ticker"]
        insert_prediction(app_conn, row)
        # cozum tarihi vadeden sonra olmali (veritabani kurali: resolved_at >= made_at + horizon)
        app_conn.execute("UPDATE predictions SET resolved_at='2026-11-20', realized_ret=0.01, realized_rel_ret=0.0,"
                         " tradable_ret=0.02, label_up=1, label_rel_up=0 WHERE ticker=%s", (t,))
    assert len(drift.load_resolved(app_conn, [version])) == 2
    only5 = drift.load_resolved(app_conn, [version], horizon_days=5)
    assert len(only5) == 1
