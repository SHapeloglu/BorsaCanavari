"""store.read_prices / read_calendar: Faz 2 ortak okuma arayuzu."""

import datetime as dt
import uuid

import pandas as pd
import pytest

from bist_tahmin.data import store


def _tk():
    return f"R{uuid.uuid4().hex[:7].upper()}"


def _put(conn, ticker, days, base=10.0):
    for i, d in enumerate(days):
        c = base + i
        conn.execute(
            "INSERT INTO prices (ticker,date,open,high,low,close,close_adj,volume,source,fetched_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'test',now())", (ticker, d, c, c + 1, c - 1, c, c / 2, 100 + i))


def test_read_prices_shape_until_and_since(app_conn):
    t = _tk()
    days = ["2026-02-02", "2026-02-03", "2026-02-04", "2026-02-05"]
    _put(app_conn, t, days)
    got = store.read_prices(app_conn, [t])[t]
    assert isinstance(got.index, pd.DatetimeIndex) and got.index.name == "date" and got.index.tz is None
    assert list(got.columns) == store.READ_COLUMNS and len(got) == 4
    assert got["open_adj"].iloc[0] == pytest.approx(10.0 * 5.0 / 10.0)   # open*close_adj/close
    upto = store.read_prices(app_conn, [t], until="2026-02-04")[t]
    assert upto.index.max() == pd.Timestamp("2026-02-04")                # until DAHIL, gelecek yok
    assert store.read_prices(app_conn, [t], since="2026-02-04")[t].index.min() == pd.Timestamp("2026-02-04")


def test_read_prices_missing_ticker_absent_and_empty_input(app_conn):
    t = _tk()
    assert store.read_prices(app_conn, [t]) == {}
    assert store.read_prices(app_conn, []) == {}


def test_read_calendar_returns_trading_calendar(app_conn):
    store.upsert_trading_calendar(app_conn, pd.DatetimeIndex(["2025-03-03", "2025-03-04", "2025-03-05"]))
    cal = store.read_calendar(app_conn)
    assert cal.is_trading_day("2025-03-04") and not cal.is_trading_day("2025-03-02")
    assert cal.add_trading_days(dt.date(2025, 3, 3), 1) == pd.Timestamp("2025-03-04")
