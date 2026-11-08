"""Faz 2 entegrasyonu: gunluk akis (ozellik + baseline + defter + cozucu + is takibi). Her modul KENDI izole
veritabaninda calisir (trading_calendar oturum boyu ortak; burada son gun kontrolu onemli)."""

from __future__ import annotations

import datetime as dt
import uuid

import numpy as np
import pandas as pd
import psycopg
import pytest
from conftest import APP_PASSWORD

from bist_tahmin.baselines.rules import BASELINES
from bist_tahmin.calendar import expected_last_bar_date
from bist_tahmin.config import load_config
from bist_tahmin.data import store
from bist_tahmin.data.universe import Membership
from bist_tahmin.db import APP_ROLE, run_init, with_overrides
from bist_tahmin.jobs import JobLocked, tracked_job
from bist_tahmin.pipeline import run_pipeline
from bist_tahmin.predict.daily import LateWrite, run_daily
from bist_tahmin.resolve.resolver import resolve_due

UTC = dt.timezone.utc
CFG = load_config("config/default.yaml")
TICKERS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
LAST = pd.Timestamp("2026-10-30")           # Cuma
NDAYS = 330


# Defterin "ileriye donuk" kurali: yeni satirlar made_at'i izleyen seans acilisindan once yazilmali. Testler
# 30 Ekim 2026 (Cuma) 19:30 Istanbul'da yazilmis gibi calisir.
CREATED = dt.datetime(2026, 10, 30, 16, 30, tzinfo=UTC)


def _run(conn, cfg, **kw):
    kw.setdefault("created_at", CREATED)
    return run_daily(conn, cfg, **kw)


def _frame(days: pd.DatetimeIndex, seed: int, level: float = 100.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = level * np.exp(np.cumsum(rng.normal(0.0003, 0.015, len(days))))
    open_ = close * (1 + rng.normal(0, 0.003, len(days)))
    return pd.DataFrame({
        "open": open_, "high": np.maximum(open_, close) * 1.004, "low": np.minimum(open_, close) * 0.996,
        "close": close, "close_adj": close, "volume": rng.lognormal(12, 0.3, len(days)),
    }, index=pd.DatetimeIndex(days, name="date"))


def _load(conn, days: pd.DatetimeIndex, tickers=TICKERS, seed0=0):
    now = dt.datetime(2026, 11, 1, tzinfo=UTC)
    for i, t in enumerate(tickers):
        store.upsert_prices(conn, t, _frame(days, seed0 + i), "test", now)
    store.upsert_prices(conn, "XU100", _frame(days, seed0 + 99), "test", now)
    store.upsert_trading_calendar(conn, days)


def _extend(conn, days: pd.DatetimeIndex, seed0: int = 1000):
    """Mevcut serilerin SON kapanisindan devam eden gelecek barlari ekler (fiyat sicramasi yok)."""
    now = dt.datetime(2026, 11, 1, tzinfo=UTC)
    for i, t in enumerate([*TICKERS, "XU100"]):
        last = conn.execute("SELECT close FROM prices WHERE ticker = %s ORDER BY date DESC LIMIT 1", (t,)).fetchone()[0]
        store.upsert_prices(conn, t, _frame(days, seed0 + i, level=last), "test", now)
    store.upsert_trading_calendar(conn, days)


@pytest.fixture()
def db(admin_conninfo):
    """Taze, bos veritabani + bist_app baglantisi; evren = TICKERS (uyelik gecmisi uzakta)."""
    name = f"bist_daily_{uuid.uuid4().hex[:8]}"
    run_init(admin_conninfo, db_name=name, app_password=APP_PASSWORD)
    info = with_overrides(admin_conninfo, dbname=name, user=APP_ROLE, password=APP_PASSWORD)
    with psycopg.connect(info, autocommit=True) as conn:
        days = pd.bdate_range(end=LAST, periods=NDAYS)
        _load(conn, days)
        store.apply_membership_changes(conn, [Membership(t, dt.date(2020, 1, 1), None) for t in TICKERS], [])
        yield conn, info


def _count(conn, where="true", params=()):
    return conn.execute(f"SELECT count(*) FROM predictions WHERE {where}", params).fetchone()[0]


def test_run_daily_writes_all_baselines_for_all_tickers(db):
    conn, _ = db
    res = _run(conn, CFG)
    assert res.made_at == LAST.date() and res.evaluated == sorted(TICKERS) and not res.excluded
    assert res.base_rate is not None
    assert res.inserted == len(BASELINES) * len(TICKERS) and res.skipped_existing == 0
    per_model = dict(conn.execute("SELECT model_version, count(*) FROM predictions GROUP BY 1").fetchall())
    assert set(per_model.values()) == {len(TICKERS)} and len(per_model) == len(BASELINES)
    roles = {r[0] for r in conn.execute("SELECT DISTINCT model_role FROM predictions")}
    assert roles == {"baseline"}


def test_run_daily_is_idempotent(db):
    conn, _ = db
    first = _run(conn, CFG)
    second = _run(conn, CFG)
    assert second.inserted == 0 and second.skipped_existing == first.inserted
    assert _count(conn) == first.inserted


def test_dry_run_writes_nothing_but_reports_counts(db):
    conn, _ = db
    res = _run(conn, CFG, dry_run=True)
    assert res.dry_run and res.inserted == len(BASELINES) * len(TICKERS)
    assert _count(conn) == 0


def test_short_history_ticker_is_excluded_and_logged(db):
    conn, _ = db
    short_days = pd.bdate_range(end=LAST, periods=20)
    store.upsert_prices(conn, "NEWCO", _frame(short_days, 7), "test", dt.datetime(2026, 11, 1, tzinfo=UTC))
    store.apply_membership_changes(conn, [Membership("NEWCO", dt.date(2020, 1, 1), None)], [])
    res = _run(conn, CFG)
    assert res.excluded["NEWCO"] == "insufficient_history" and "NEWCO" not in res.evaluated
    assert _count(conn, "ticker = 'NEWCO'") == 0
    assert res.universe == len(TICKERS) + 1


def test_ticker_without_prices_is_excluded(db):
    conn, _ = db
    store.apply_membership_changes(conn, [Membership("GHOST", dt.date(2020, 1, 1), None)], [])
    res = _run(conn, CFG)
    assert res.excluded["GHOST"] == "no_prices"


def test_future_bars_do_not_change_predictions(db):
    """SIZINTI: made_at sonrasi barlar (ve takvim gunleri) eklenince ayni made_at icin tahminler aynidir."""
    conn, _ = db
    before = _run(conn, CFG, dry_run=True)
    future = pd.bdate_range(start=LAST + pd.Timedelta(days=3), periods=8)
    _extend(conn, future)
    after = _run(conn, CFG, made_at=LAST, allow_backfill=True, dry_run=True)
    assert before.rows == after.rows and before.base_rate == after.base_rate


def test_backfill_is_refused_without_flag(db):
    conn, _ = db
    past = LAST - pd.Timedelta(days=7)
    with pytest.raises(ValueError, match="ileriye donuk"):
        _run(conn, CFG, made_at=past)
    res = _run(conn, CFG, made_at=past, allow_backfill=True, dry_run=True)   # yalniz gelistirme bayragiyla
    assert res.made_at == past.date()


def test_non_trading_day_is_refused(db):
    conn, _ = db
    with pytest.raises(ValueError, match="islem gunu degil"):
        _run(conn, CFG, made_at=LAST + pd.Timedelta(days=1), allow_backfill=True)   # Cumartesi


def test_resolver_closes_the_loop_after_horizon(db):
    conn, _ = db
    wrote = _run(conn, CFG)
    assert resolve_due(conn, store.read_calendar(conn)).resolved == 0          # vade gelmedi
    future = pd.bdate_range(start=LAST + pd.Timedelta(days=3), periods=6)
    _extend(conn, future)
    res = resolve_due(conn, store.read_calendar(conn), label_threshold=CFG["label_threshold"])
    assert res.resolved == wrote.inserted
    assert _count(conn, "resolved_at IS NOT NULL") == wrote.inserted
    row = conn.execute("SELECT resolved_at, realized_ret, tradable_ret, label_up, label_rel_up FROM predictions "
                       "WHERE resolved_at IS NOT NULL LIMIT 1").fetchone()
    horizon_end = store.read_calendar(conn).horizon_end(LAST, CFG["horizon_days"])
    assert pd.Timestamp(row[0]) == horizon_end and all(v is not None for v in row)
    assert resolve_due(conn, store.read_calendar(conn)).resolved == 0          # tek seferlik


# --- is takibi ---------------------------------------------------------------------------------------------

def test_tracked_job_records_ok_and_failed_runs(db):
    conn, _ = db
    with tracked_job(conn, "t_ok", dt.date(2026, 11, 2)) as run:
        run.detail["n"] = 3
    with pytest.raises(RuntimeError), tracked_job(conn, "t_bad", dt.date(2026, 11, 2)):
        raise RuntimeError("boom")
    rows = dict((r[0], (r[1], r[2])) for r in conn.execute(
        "SELECT job, status, detail FROM runs WHERE job IN ('t_ok','t_bad')"))
    assert rows["t_ok"][0] == "ok" and rows["t_ok"][1]["n"] == 3
    assert rows["t_bad"][0] == "failed" and "boom" in rows["t_bad"][1]["error"]


def test_tracked_job_is_exclusive_across_connections(db):
    conn, info = db
    with psycopg.connect(info, autocommit=True) as other:
        with tracked_job(conn, "t_lock", dt.date(2026, 11, 2)):
            with pytest.raises(JobLocked):
                with tracked_job(other, "t_lock", dt.date(2026, 11, 2)):
                    pass
        with tracked_job(other, "t_lock", dt.date(2026, 11, 2)):    # kilit birakildiktan sonra alinabilir
            pass


def test_tracked_job_dry_run_records_nothing(db):
    conn, _ = db
    with tracked_job(conn, "t_dry", dt.date(2026, 11, 2), record=False):
        pass
    assert conn.execute("SELECT count(*) FROM runs WHERE job = 't_dry'").fetchone()[0] == 0


# --- bayat veri takvimi ------------------------------------------------------------------------------------

@pytest.mark.parametrize("now,expected", [
    (dt.datetime(2026, 11, 6, 19, 0, tzinfo=dt.timezone(dt.timedelta(hours=3))), dt.date(2026, 11, 6)),   # Cuma 19:00
    (dt.datetime(2026, 11, 6, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=3))), dt.date(2026, 11, 5)),   # kapanistan once
    (dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=3))), dt.date(2026, 11, 6)),   # Pazar
    (dt.datetime(2026, 10, 30, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=3))), dt.date(2026, 10, 28)),  # 29 Ekim tatil -> Carsamba'ya
])
def test_expected_last_bar_date(now, expected):
    assert expected_last_bar_date(now) == expected


# --- uctan uca akis (sahte kaynak) -------------------------------------------------------------------------

class _Source:
    name = "fake"

    def __init__(self, days):
        self.frames = {t: _frame(days, i) for i, t in enumerate(TICKERS)}
        self.frames["XU100"] = _frame(days, 99)

    def history(self, ticker, start=None, end=None):
        return self.frames[ticker]

    def index_history(self, symbol, start=None, end=None):
        return self.frames[symbol]

    def index_components(self, symbol):
        return list(TICKERS)

    def corporate_actions(self, ticker):
        return pd.DataFrame({"ex_date": pd.to_datetime([]), "kind": [], "value": []})


def test_pipeline_end_to_end_and_idempotent(db):
    conn, _ = db
    src = _Source(pd.bdate_range(end=LAST, periods=NDAYS))
    now = dt.datetime(2026, 10, 30, 16, 30, tzinfo=UTC)               # 19:30 Istanbul
    res = run_pipeline(conn, CFG, src, now=now, pause=0, universe_min_size=1)
    assert res.exit_code == 0, res.lines
    assert _count(conn) == len(BASELINES) * len(TICKERS)
    again = run_pipeline(conn, CFG, src, now=now, pause=0, universe_min_size=1)
    assert again.exit_code == 0 and _count(conn) == len(BASELINES) * len(TICKERS)
    statuses = [r[0] for r in conn.execute("SELECT status FROM runs WHERE job = 'daily' ORDER BY id")]
    assert statuses == ["ok", "ok"]


def test_pipeline_stale_data_writes_no_predictions_and_exits_3(db):
    conn, _ = db
    src = _Source(pd.bdate_range(end=LAST, periods=NDAYS))
    late = dt.datetime(2026, 11, 4, 16, 30, tzinfo=UTC)               # Carsamba 19:30: beklenen 4 Kasim, son bar 30 Ekim
    res = run_pipeline(conn, CFG, src, now=late, pause=0, universe_min_size=1)
    assert res.exit_code == 3 and any("BAYAT" in line for line in res.lines)
    assert _count(conn) == 0


def test_pipeline_dry_run_writes_nothing(db):
    conn, _ = db
    src = _Source(pd.bdate_range(end=LAST, periods=NDAYS))
    res = run_pipeline(conn, CFG, src, dry_run=True, now=dt.datetime(2026, 10, 30, 16, 30, tzinfo=UTC), pause=0,
                       universe_min_size=1)
    assert res.exit_code in (0, 1)
    assert _count(conn) == 0 and conn.execute("SELECT count(*) FROM runs WHERE job='daily'").fetchone()[0] == 0


# --- yazim penceresi (ileriye donuk kanit) ------------------------------------------------------------------

def test_late_write_is_refused_but_rerun_of_existing_rows_is_fine(db):
    conn, _ = db
    first = _run(conn, CFG)                                              # Cuma 19:30: zamaninda
    late = dt.datetime(2026, 11, 3, 8, 0, tzinfo=UTC)                    # Sali 11:00 Istanbul: Pzt seansi bile acildi
    again = _run(conn, CFG, created_at=late)                             # hepsi zaten var => reddedilmez
    assert again.inserted == 0 and again.skipped_existing == first.inserted


def test_late_first_write_is_refused(db):
    conn, _ = db
    late = dt.datetime(2026, 11, 3, 8, 0, tzinfo=UTC)                    # Sali 11:00 Istanbul
    with pytest.raises(LateWrite, match="yazim penceresi kapandi"):
        _run(conn, CFG, created_at=late)
    assert _count(conn) == 0


def test_weekend_write_before_monday_open_is_allowed(db):
    conn, _ = db
    sat = dt.datetime(2026, 10, 31, 9, 0, tzinfo=UTC)                    # Cumartesi: Pzt 10:00'a kadar zaman var
    assert _run(conn, CFG, created_at=sat).inserted == len(BASELINES) * len(TICKERS)


def test_holiday_pushes_the_write_deadline():
    from bist_tahmin.calendar import next_session_open
    assert next_session_open(dt.date(2026, 10, 28)) == pd.Timestamp("2026-10-30 10:00", tz="Europe/Istanbul")   # 29 Ekim tatil
    assert next_session_open(dt.date(2026, 10, 30)) == pd.Timestamp("2026-11-02 10:00", tz="Europe/Istanbul")   # hafta sonu


# --- pipeline guvenceleri ----------------------------------------------------------------------------------

def test_pipeline_low_coverage_raises_exit_code_and_is_recorded(db):
    conn, _ = db
    days = pd.bdate_range(end=LAST, periods=NDAYS)
    src = _Source(days)
    newcos = ["NEW1", "NEW2", "NEW3", "NEW4"]                          # yeni uyeler: yalniz 20 bar gecmis
    for i, t in enumerate(newcos):
        src.frames[t] = _frame(days[-20:], 50 + i)
    store.apply_membership_changes(conn, [Membership(t, dt.date(2020, 1, 1), None) for t in newcos], [])
    src.index_components = lambda symbol: [*TICKERS, *newcos]
    res = run_pipeline(conn, CFG, src, now=dt.datetime(2026, 10, 30, 16, 30, tzinfo=UTC), pause=0, universe_min_size=1)
    assert res.exit_code == 1 and any("kapsam" in line for line in res.lines)   # 6/10 = %60 < %80
    detail = conn.execute("SELECT detail FROM runs WHERE job='daily' ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert detail["predict"]["coverage"] == 0.6 and detail["exit_code"] == 1
    assert set(detail["predict"]["excluded"]) == set(newcos)


def test_pipeline_universe_sync_applies_changes_and_refuses_tiny_source(db):
    conn, _ = db
    days = pd.bdate_range(end=LAST, periods=NDAYS)
    src = _Source(days)
    src.index_components = lambda symbol: TICKERS[:3]                  # kaynak 3 bilesen verdi (< esik)
    res = run_pipeline(conn, CFG, src, dry_run=True, now=dt.datetime(2026, 10, 30, 16, 30, tzinfo=UTC), pause=0,
                       universe_min_size=5)
    assert res.exit_code >= 1 and any("senkron uygulanmadi" in line for line in res.lines)
    assert conn.execute("SELECT count(*) FROM universe_membership WHERE valid_to IS NOT NULL").fetchone()[0] == 0


def test_stale_pipeline_marks_run_skipped_not_ok(db):
    conn, _ = db
    src = _Source(pd.bdate_range(end=LAST, periods=NDAYS))
    run_pipeline(conn, CFG, src, now=dt.datetime(2026, 11, 4, 16, 30, tzinfo=UTC), pause=0, universe_min_size=1)
    status, detail = conn.execute("SELECT status, detail FROM runs WHERE job='daily' ORDER BY id DESC LIMIT 1").fetchone()
    assert status == "skipped" and detail["exit_code"] == 3 and detail["freshness"]["stale"] is True


def test_locked_job_leaves_a_skipped_run_record(db):
    conn, info = db
    with psycopg.connect(info, autocommit=True) as other, tracked_job(conn, "t_skip", dt.date(2026, 11, 2)):
        with pytest.raises(JobLocked):
            with tracked_job(other, "t_skip", dt.date(2026, 11, 2)):
                pass
    statuses = sorted(r[0] for r in conn.execute("SELECT status FROM runs WHERE job = 't_skip'"))
    assert statuses == ["ok", "skipped"]


def test_run_detail_is_checkpointed_before_a_later_step_fails(db):
    conn, _ = db
    src = _Source(pd.bdate_range(end=LAST, periods=NDAYS))
    src.index_history = lambda symbol, start=None, end=None: (_ for _ in ()).throw(RuntimeError("kaynak coktu"))
    with pytest.raises(RuntimeError):
        run_pipeline(conn, CFG, src, now=dt.datetime(2026, 10, 30, 16, 30, tzinfo=UTC), pause=0, universe_min_size=1)
    status, detail = conn.execute("SELECT status, detail FROM runs WHERE job='daily' ORDER BY id DESC LIMIT 1").fetchone()
    assert status == "failed" and "universe" in detail and "fetch" in detail and "XU100" in detail["error"]


def test_membership_starting_after_made_at_gives_a_clear_error(db):
    conn, _ = db
    store.apply_membership_changes(conn, [], [Membership(t, dt.date(2020, 1, 1), dt.date(2020, 1, 2)) for t in TICKERS])
    store.apply_membership_changes(conn, [Membership(t, dt.date(2026, 11, 8), None) for t in TICKERS], [])
    with pytest.raises(ValueError, match="gecerli uye yok.*2020-01-01"):
        _run(conn, CFG)
