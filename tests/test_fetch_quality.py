"""fetch_all + kalite kapisi + yarim bar (PROJE.md Bolum 4, 5, 12). Ag yok; gercek DB."""

from __future__ import annotations

import datetime as dt
import uuid

import pandas as pd
import pytest

from bist_tahmin.data.fetch import fetch_all

UTC = dt.timezone.utc
# 2026-11-06 Cuma. 07:00 UTC = 10:00 Istanbul (kapanistan once); 16:30 UTC = 19:30 Istanbul (sonra).
BEFORE_CLOSE = dt.datetime(2026, 11, 6, 7, 0, tzinfo=UTC)
AFTER_CLOSE = dt.datetime(2026, 11, 6, 16, 30, tzinfo=UTC)


def _bars(days: list[str], volume: float = 100.0) -> pd.DataFrame:
    idx = pd.DatetimeIndex(pd.to_datetime(days), name="date")
    return pd.DataFrame(
        {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": volume, "close_adj": 1.5}, index=idx)


WEEK = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"]


class Source:
    name = "fake"

    def __init__(self, per_ticker: dict[str, pd.DataFrame], index_days=WEEK):
        self.per_ticker = per_ticker
        self.index_days = index_days
        self.order: list[str] = []

    def history(self, ticker, start=None, end=None):
        self.order.append(ticker)
        return self.per_ticker[ticker]

    def index_history(self, symbol, start=None, end=None):
        self.order.append(symbol)
        return _bars(self.index_days)

    def index_components(self, symbol):
        return []

    def corporate_actions(self, ticker):
        return pd.DataFrame({"ex_date": pd.to_datetime([]), "kind": [], "value": []})


def _tk() -> str:
    return f"Q{uuid.uuid4().hex[:7].upper()}"


def _dates(conn, ticker):
    return [str(r[0]) for r in conn.execute("SELECT date FROM prices WHERE ticker=%s ORDER BY date", (ticker,))]


def test_xu100_is_fetched_before_tickers(app_conn):
    t = _tk()
    src = Source({t: _bars(WEEK)})
    fetch_all(src, app_conn, [t], "2026-11-01", now=AFTER_CLOSE, pause=0)
    assert src.order[0] == "XU100" and src.order[1:] == [t]


def test_ticker_with_excessive_missing_days_is_excluded_not_written(app_conn):
    good, bad = _tk(), _tk()
    src = Source({good: _bars(WEEK), bad: _bars(WEEK[:2])})  # kotu: bes beklenen gunden uc tanesi eksik
    res = fetch_all(src, app_conn, [good, bad], "2026-11-01", now=AFTER_CLOSE, pause=0)
    assert good in res.written and bad not in res.written
    assert "excessive_missing_days" in res.excluded[bad]
    assert _dates(app_conn, bad) == []
    assert len(_dates(app_conn, good)) == 5
    assert res.ok  # dislanma hata degil; is surer


def test_flags_are_recorded_but_data_is_written_unmodified(app_conn):
    t = _tk()
    df = _bars(WEEK)
    df.loc[pd.Timestamp("2026-11-04"), "volume"] = 0.0  # sifir hacim: flag
    res = fetch_all(Source({t: df}), app_conn, [t], "2026-11-01", now=AFTER_CLOSE, pause=0)
    assert t in res.written and t not in res.excluded
    assert any(f.startswith("zero_volume") for f in res.flags[t])
    assert len(_dates(app_conn, t)) == 5  # hicbir bar silinmedi
    vol = app_conn.execute("SELECT volume FROM prices WHERE ticker=%s AND date='2026-11-04'", (t,)).fetchone()[0]
    assert vol == 0.0  # duzeltilmedi
    assert any("BAYRAK" in line for line in res.summary_lines())


def test_incomplete_today_bar_is_not_written_before_cutoff(app_conn):
    # Oturum boyu paylasilan DB'de baska testlerin takvim satirlariyla karismasin diye ayri bir hafta kullanilir.
    week = ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06"]  # 6 Mart Cuma
    before_close = dt.datetime(2026, 3, 6, 7, 0, tzinfo=UTC)                        # 10:00 Istanbul
    t = _tk()
    res = fetch_all(Source({t: _bars(week)}, index_days=week), app_conn, [t], "2026-03-01",
                    now=before_close, pause=0)
    assert _dates(app_conn, t)[-1] == "2026-03-05"           # 6 Mart (bugun, yarim) yazilmadi
    cal = [str(r[0]) for r in app_conn.execute(
        "SELECT date FROM trading_calendar WHERE date BETWEEN '2026-03-02' AND '2026-03-06' ORDER BY date")]
    assert cal == ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05"]  # takvime de girmedi
    assert res.ok and t not in res.excluded                  # yarim bar "eksik gun" sayilmaz


def test_today_bar_is_kept_after_cutoff(app_conn):
    t = _tk()
    fetch_all(Source({t: _bars(WEEK)}), app_conn, [t], "2026-11-01", now=AFTER_CLOSE, pause=0)
    assert _dates(app_conn, t)[-1] == "2026-11-06"


def test_drop_incomplete_today_can_be_disabled(app_conn):
    t = _tk()
    fetch_all(Source({t: _bars(WEEK)}), app_conn, [t], "2026-11-01", now=BEFORE_CLOSE, pause=0,
              drop_incomplete_today=False, use_quality=False)
    assert _dates(app_conn, t)[-1] == "2026-11-06"


def test_dry_run_with_quality_writes_nothing(app_conn):
    t = _tk()
    res = fetch_all(Source({t: _bars(WEEK)}), app_conn, [t], "2026-11-01", now=AFTER_CLOSE, pause=0, dry_run=True)
    assert res.written[t] == 5
    assert _dates(app_conn, t) == []
