"""fetch.py: sahte DataSource ile (ag yok) + DB."""

from __future__ import annotations

import datetime as dt
import os
import uuid
from types import SimpleNamespace

import pandas as pd
import pytest

from bist_tahmin.data.fetch import fetch_all
from bist_tahmin.data.source import BorsapySource

NOW = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc)
network = pytest.mark.skipif(not os.environ.get("BIST_NETWORK_TESTS"), reason="BIST_NETWORK_TESTS ayarli degil")


def _df(n=3, start="2026-11-02"):
    idx = pd.date_range(start, periods=n, freq="B", name="date")
    return pd.DataFrame(
        {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100.0, "close_adj": 1.5}, index=idx)


class FakeSource:
    name = "fake"

    def __init__(self, fail=(), flaky=None):
        self.fail = set(fail)
        self.flaky = dict(flaky or {})  # ticker -> kalan basarisiz deneme sayisi
        self.calls: list[str] = []

    def history(self, ticker, start=None, end=None):
        self.calls.append(ticker)
        if ticker in self.fail:
            raise RuntimeError("kaynak hatasi")
        if self.flaky.get(ticker, 0) > 0:
            self.flaky[ticker] -= 1
            raise TimeoutError("gecici")
        return _df()

    def index_history(self, symbol, start=None, end=None):
        return _df(4, "2026-11-02")

    def index_components(self, symbol):
        return []

    def corporate_actions(self, ticker):
        return pd.DataFrame({"ex_date": pd.to_datetime(["2026-05-14"]), "kind": ["split"], "value": [2.0]})


@pytest.fixture()
def tickers():
    return [f"F{uuid.uuid4().hex[:7].upper()}" for _ in range(3)]


def _n(conn, tickers):
    return conn.execute("SELECT count(*) FROM prices WHERE ticker = ANY(%s)", (tickers,)).fetchone()[0]


def _run(src, conn, tk, **kw):
    kw.setdefault("pause", 0)
    # Bu dosya indirme tesisatini sinar; sahte hisse ve XU100 gunleri bilerek tutarsiz. Kalite kapisi
    # davranisi tests/test_fetch_quality.py'de.
    kw.setdefault("use_quality", False)
    return fetch_all(src, conn, tk, "2026-11-01", now=NOW, **kw)


@pytest.mark.fast
def test_fetch_writes_prices_actions_and_calendar(app_conn, tickers):
    res = _run(FakeSource(), app_conn, tickers)
    assert res.ok and not res.excluded
    assert _n(app_conn, tickers) == 9
    assert app_conn.execute("SELECT count(*) FROM corporate_actions WHERE ticker = ANY(%s)",
                            (tickers,)).fetchone()[0] == 3
    assert res.calendar_rows == 4
    assert app_conn.execute("SELECT count(*) FROM trading_calendar WHERE date BETWEEN '2026-11-02' AND '2026-11-05'"
                            ).fetchone()[0] == 4
    assert app_conn.execute("SELECT count(*) FROM prices WHERE ticker = 'XU100'").fetchone()[0] >= 4


@pytest.mark.fast
def test_fetch_idempotent(app_conn, tickers):
    _run(FakeSource(), app_conn, tickers)
    before = _n(app_conn, tickers)
    res = _run(FakeSource(), app_conn, tickers)
    assert _n(app_conn, tickers) == before == 9
    assert all(v == 0 for v in res.written.values())


@pytest.mark.fast
def test_dry_run_writes_nothing(app_conn, tickers):
    res = _run(FakeSource(), app_conn, tickers, dry_run=True)
    assert res.dry_run and res.ok
    assert _n(app_conn, tickers) == 0
    assert app_conn.execute("SELECT count(*) FROM corporate_actions WHERE ticker = ANY(%s)",
                            (tickers,)).fetchone()[0] == 0
    assert res.written == {t: 3 for t in tickers}  # "yazilacak" sayisi


@pytest.mark.fast
def test_dry_run_no_db_changes_at_all(app_conn, tickers):
    tbls = ["prices", "corporate_actions", "trading_calendar", "universe_membership"]
    before = [app_conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tbls]
    _run(FakeSource(), app_conn, tickers, dry_run=True)
    after = [app_conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tbls]
    assert before == after


@pytest.mark.fast
def test_single_ticker_failure_does_not_abort(app_conn, tickers):
    bad = tickers[1]
    res = _run(FakeSource(fail=[bad]), app_conn, tickers)
    assert bad in res.errors and not res.ok
    assert _n(app_conn, [bad]) == 0
    assert _n(app_conn, [tickers[0], tickers[2]]) == 6
    assert res.calendar_rows == 4  # XU100 yine de islendi


@pytest.mark.fast
def test_transient_error_retried(app_conn, tickers):
    src = FakeSource(flaky={tickers[0]: 2})
    res = _run(src, app_conn, tickers, retries=3)
    assert res.ok and _n(app_conn, [tickers[0]]) == 3
    assert src.calls.count(tickers[0]) == 3


@pytest.mark.fast
def test_retries_are_bounded(app_conn, tickers):
    src = FakeSource(flaky={tickers[0]: 99})
    res = _run(src, app_conn, tickers, retries=2)
    assert tickers[0] in res.errors
    assert src.calls.count(tickers[0]) == 2


@pytest.mark.fast
def test_pacing_sleeps_between_requests(app_conn, tickers):
    sleeps: list[float] = []
    _run(FakeSource(), app_conn, tickers, pause=0.25, sleep=sleeps.append)
    assert len(sleeps) >= len(tickers) and set(sleeps) == {0.25}


@pytest.mark.fast
def test_validator_rejection_not_written(app_conn, tickers):
    bad = tickers[0]

    def validator(ticker, df):
        return SimpleNamespace(usable=ticker != bad, summary="sifir fiyat")

    res = _run(FakeSource(), app_conn, tickers, validator=validator)
    assert bad in res.excluded and "sifir fiyat" in res.excluded[bad]
    assert _n(app_conn, [bad]) == 0
    assert _n(app_conn, tickers[1:]) == 6
    assert app_conn.execute("SELECT count(*) FROM corporate_actions WHERE ticker = %s", (bad,)).fetchone()[0] == 0
    assert res.ok  # dislanma hata degildir


@pytest.mark.fast
def test_empty_source_data_is_error(app_conn, tickers):
    class Empty(FakeSource):
        def history(self, ticker, start=None, end=None):
            return _df(0)

    res = _run(Empty(), app_conn, tickers[:1])
    assert tickers[0] in res.errors


@network
@pytest.mark.network
def test_network_fetch_one_ticker_dry_run(app_conn):
    res = fetch_all(BorsapySource(), app_conn, ["THYAO"], "2026-10-01", dry_run=True)
    assert res.ok and res.fetched_rows["THYAO"] > 5
