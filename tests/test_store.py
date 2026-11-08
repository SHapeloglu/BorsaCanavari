"""store.py: idempotent upsert'ler (gercek Postgres, bist_app yetkileriyle)."""

from __future__ import annotations

import datetime as dt
import uuid

import pandas as pd
import pytest

from bist_tahmin.data import store

T0 = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc)
T1 = dt.datetime(2026, 11, 9, 12, 0, tzinfo=dt.timezone.utc)


def _prices(n=3, start="2026-11-02", bump=0.0):
    idx = pd.date_range(start, periods=n, freq="B", name="date")
    return pd.DataFrame(
        {"open": [10.0 + i for i in range(n)], "high": [11.0 + i for i in range(n)],
         "low": [9.0 + i for i in range(n)], "close": [10.5 + i + bump for i in range(n)],
         "volume": [1000.0] * n, "close_adj": [5.25 + i for i in range(n)]},
        index=idx,
    )


def _tk():
    return f"T{uuid.uuid4().hex[:8].upper()}"


def _count(conn, table, ticker):
    return conn.execute(f"SELECT count(*) FROM {table} WHERE ticker = %s", (ticker,)).fetchone()[0]


def test_upsert_prices_idempotent(app_conn):
    tk = _tk()
    assert store.upsert_prices(app_conn, tk, _prices(), "borsapy", T0) == 3
    assert _count(app_conn, "prices", tk) == 3
    assert store.upsert_prices(app_conn, tk, _prices(), "borsapy", T1) == 0  # ayni veri: satira dokunulmaz
    assert _count(app_conn, "prices", tk) == 3
    fetched = app_conn.execute("SELECT DISTINCT fetched_at FROM prices WHERE ticker = %s", (tk,)).fetchall()
    assert fetched == [(T0,)]


def test_upsert_prices_updates_changed_rows(app_conn):
    tk = _tk()
    store.upsert_prices(app_conn, tk, _prices(), "borsapy", T0)
    assert store.upsert_prices(app_conn, tk, _prices(bump=1.0), "borsapy", T1) == 3
    assert _count(app_conn, "prices", tk) == 3
    close, fa = app_conn.execute(
        "SELECT close, fetched_at FROM prices WHERE ticker = %s AND date = '2026-11-02'", (tk,)).fetchone()
    assert close == 11.5 and fa == T1


def test_upsert_prices_nan_becomes_null_and_requires_tz(app_conn):
    tk = _tk()
    df = _prices(1)
    df["volume"] = float("nan")
    store.upsert_prices(app_conn, tk, df, "borsapy", T0)
    assert app_conn.execute("SELECT volume FROM prices WHERE ticker = %s", (tk,)).fetchone() == (None,)
    with pytest.raises(ValueError):
        store.upsert_prices(app_conn, tk, df, "borsapy", dt.datetime(2026, 11, 8))


def test_upsert_corporate_actions_idempotent(app_conn):
    tk = _tk()
    df = pd.DataFrame({"ex_date": pd.to_datetime(["2026-05-14", "2026-06-17"]),
                       "kind": ["split", "dividend"], "value": [2.0, 2.5]})
    assert store.upsert_corporate_actions(app_conn, tk, df, T0) == 2
    assert store.upsert_corporate_actions(app_conn, tk, df, T1) == 0
    assert _count(app_conn, "corporate_actions", tk) == 2
    df.loc[1, "value"] = 3.0
    assert store.upsert_corporate_actions(app_conn, tk, df, T1) == 1
    assert app_conn.execute("SELECT value FROM corporate_actions WHERE ticker = %s AND kind = 'dividend'",
                            (tk,)).fetchone() == (3.0,)


def test_upsert_corporate_actions_empty(app_conn):
    empty = pd.DataFrame(columns=["ex_date", "kind", "value"])
    assert store.upsert_corporate_actions(app_conn, _tk(), empty, T0) == 0


def test_upsert_trading_calendar_idempotent_and_keeps_half_day(app_conn):
    days = pd.to_datetime(["2031-03-03", "2031-03-04"])
    assert store.upsert_trading_calendar(app_conn, days) == 2
    assert store.upsert_trading_calendar(app_conn, days) == 0
    store.upsert_trading_calendar(app_conn, days, half_days=["2031-03-04"])
    store.upsert_trading_calendar(app_conn, days)  # bayrak temizlenmez
    rows = app_conn.execute(
        "SELECT date, is_half_day FROM trading_calendar WHERE date IN ('2031-03-03','2031-03-04') ORDER BY date"
    ).fetchall()
    assert rows == [(dt.date(2031, 3, 3), False), (dt.date(2031, 3, 4), True)]


def test_app_role_cannot_delete(app_conn):
    """Yetki modeli: upsert'ten baska yol yok (silme yetkisi yok)."""
    import psycopg

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("DELETE FROM prices WHERE ticker = 'NOPE'")
