"""004: open_adj = open * close_adj / close (bedelsiz gununde ham acilis ile duzeltmeli kapanis karismaz)."""

import pytest


def _insert(conn, ticker, date, o, c, c_adj):
    conn.execute(
        "INSERT INTO prices (ticker, date, open, high, low, close, close_adj, volume, source, fetched_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, 1, 'test', now())",
        (ticker, date, o, max(o, c), min(o, c), c, c_adj),
    )


@pytest.mark.fast
def test_open_adj_scales_open_to_adjusted_series(app_conn):
    # BIMAS 2026-05-14 benzeri %100 bedelsiz: onceki gun ham 813 -> duzeltmeli 406.5, ex-gun 414/414
    _insert(app_conn, "OPENADJ1", "2026-05-13", 810.0, 813.0, 406.5)
    _insert(app_conn, "OPENADJ1", "2026-05-14", 410.0, 414.0, 414.0)
    rows = dict(app_conn.execute(
        "SELECT date::text, open_adj FROM prices WHERE ticker='OPENADJ1'").fetchall())
    assert rows["2026-05-13"] == pytest.approx(405.0)   # 810 * 406.5 / 813
    assert rows["2026-05-14"] == pytest.approx(410.0)   # faktor 1
    # duzeltmeli seride ex-gun sicramasi yok: onceki kapanis(adj) 406.5 -> ex-gun duzeltmeli acilis 410
    assert abs(rows["2026-05-14"] / 406.5 - 1) < 0.02


@pytest.mark.fast
def test_open_adj_is_null_when_close_is_zero_or_missing(app_conn):
    _insert(app_conn, "OPENADJ2", "2026-05-13", 10.0, 10.0, 10.0)
    app_conn.execute(
        "INSERT INTO prices (ticker, date, open, close, close_adj, source, fetched_at)"
        " VALUES ('OPENADJ2', '2026-05-14', 10, NULL, NULL, 'test', now())")
    got = app_conn.execute("SELECT open_adj FROM prices WHERE ticker='OPENADJ2' AND date='2026-05-14'").fetchone()[0]
    assert got is None


@pytest.mark.fast
def test_app_cannot_write_generated_column(app_conn):
    from psycopg import errors
    with pytest.raises((errors.GeneratedAlways, errors.InsufficientPrivilege)):
        app_conn.execute("UPDATE prices SET open_adj = 1 WHERE ticker='OPENADJ1'")
