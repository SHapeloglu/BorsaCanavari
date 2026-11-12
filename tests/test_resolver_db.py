"""resolve_due uctan uca (gercek Postgres, bist_app yetkileri). Oturum boyu paylasilan DB oldugundan her test
kendi ticker'i ve kendi yil araligini kullanir; takvim DB'den degil testten verilir (trading_calendar ortak)."""

import uuid

import pandas as pd
import psycopg
import pytest
from conftest import APP_PASSWORD
from psycopg import errors

from bist_tahmin.calendar import TradingCalendar
from bist_tahmin.db import APP_ROLE, run_init, with_overrides
from bist_tahmin.resolve import resolve_due
from bist_tahmin.resolve.resolver import count_overdue

H = 5


def _setup(conn, year, *, n_bars=12, close=None, raw=None, ticker=None):
    """year yilinin Ocak ayinda 12 is gunu: takvim, XU100 ve ticker fiyatlari (XU100 sabit 1000, +%2 son gun).

    close/raw: ticker close_adj / close listeleri (12 eleman). n_bars: yalniz ilk n_bars gun yazilir (takvim 12 gun)."""
    ticker = ticker or f"D{uuid.uuid4().hex[:7].upper()}"
    days = pd.bdate_range(f"{year}-01-05", periods=12)
    cal = TradingCalendar(days)
    close = close or [100.0 + i for i in range(12)]
    raw = raw or close
    for i, d in enumerate(days):
        if i >= n_bars:
            break
        o = raw[i] - 0.5
        conn.execute(
            "INSERT INTO prices (ticker,date,open,high,low,close,close_adj,volume,source,fetched_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,1,'test',now()) ON CONFLICT DO NOTHING",
            (ticker, d.date(), o, raw[i] + 1, o - 1, raw[i], close[i]))
        x = 1000.0 * (1.02 if i >= 6 else 1.0)    # made_at=days[1], end=days[6]
        conn.execute(
            "INSERT INTO prices (ticker,date,open,high,low,close,close_adj,volume,source,fetched_at)"
            " VALUES ('XU100',%s,%s,%s,%s,%s,%s,1,'test',now()) ON CONFLICT DO NOTHING",
            (d.date(), x, x, x, x, x))
    return ticker, days, cal


def _predict(conn, new_prediction, insert_prediction, ticker, made_at, version="baseline_base_rate"):
    return insert_prediction(conn, new_prediction(ticker=ticker, made_at=str(made_at.date()),
                                                  model_version=version, horizon_days=H))


def _row(conn, pid):
    return conn.execute(
        "SELECT resolved_at, realized_ret, realized_rel_ret, tradable_ret, label_up, label_rel_up"
        " FROM predictions WHERE id = %s", (pid,)).fetchone()


@pytest.mark.fast
def test_resolve_end_to_end_and_second_run_is_noop(app_conn, new_prediction, insert_prediction):
    t, days, cal = _setup(app_conn, 2001)
    made, end, nxt = days[1], days[6], days[2]
    p1 = _predict(app_conn, new_prediction, insert_prediction, t, made, "model_a")
    p2 = _predict(app_conn, new_prediction, insert_prediction, t, made, "model_b")

    res = resolve_due(app_conn, cal)
    assert res.resolved == 2 and not res.dry_run

    # close_adj: made=101, end=106; XU100 +%2; open_adj[next]=102-0.5=101.5 (ham=adj)
    for pid in (p1, p2):
        r = _row(app_conn, pid)
        assert r[0] == end.date()
        assert r[1] == pytest.approx(106 / 101 - 1)
        assert r[2] == pytest.approx(106 / 101 - 1 - 0.02)
        assert r[3] == pytest.approx(106 / 101.5 - 1)
        assert r[4] == 1 and r[5] == 1

    again = resolve_due(app_conn, cal)
    assert again.resolved == 0
    assert _row(app_conn, p1)[1] == pytest.approx(106 / 101 - 1)       # degismedi


@pytest.mark.fast
def test_not_due_is_not_resolved_until_end_bar_exists(app_conn, new_prediction, insert_prediction):
    t2, days, cal = _setup(app_conn, 2002, n_bars=6)      # barlar days[5]'e kadar; END=days[6] yok
    cal = TradingCalendar(days[:8])                       # takvim END'i yalniz 1 islem gunu gecti: henuz 'stale' degil
    pid = _predict(app_conn, new_prediction, insert_prediction, t2, days[1])
    res = resolve_due(app_conn, cal)
    assert _row(app_conn, pid)[0] is None
    assert res.skipped.get("not_due", 0) >= 1

    # yalniz hisse bari gelirse (XU100 yok) hala cozulmez; ikisi de gelince cozulur
    for i in (6, 7):
        d = days[i]
        app_conn.execute(
            "INSERT INTO prices (ticker,date,open,high,low,close,close_adj,volume,source,fetched_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,1,'test',now())", (t2, d.date(), 100.0 + i - 0.5, 200, 1, 100.0 + i,
                                                              100.0 + i))
    assert resolve_due(app_conn, cal).resolved == 0 and _row(app_conn, pid)[0] is None
    for i in (6, 7):
        app_conn.execute(
            "INSERT INTO prices (ticker,date,open,high,low,close,close_adj,volume,source,fetched_at)"
            " VALUES ('XU100',%s,1020,1020,1020,1020,1020,1,'test',now()) ON CONFLICT DO NOTHING",
            (days[i].date(),))
    assert resolve_due(app_conn, cal).resolved == 1
    assert _row(app_conn, pid)[0] == days[6].date()


@pytest.mark.fast
def test_as_of_blocks_resolution_before_end_date(app_conn, new_prediction, insert_prediction):
    t, days, cal = _setup(app_conn, 2003)
    pid = _predict(app_conn, new_prediction, insert_prediction, t, days[1])
    res = resolve_due(app_conn, cal, as_of=days[5].date())     # end = days[6] > as_of
    assert _row(app_conn, pid)[0] is None and res.skipped.get("not_due", 0) >= 1
    resolve_due(app_conn, cal, as_of=days[6].date())
    assert _row(app_conn, pid)[0] == days[6].date()


@pytest.mark.fast
def test_resolution_is_write_once_by_trigger(app_conn, new_prediction, insert_prediction):
    t, days, cal = _setup(app_conn, 2004)
    pid = _predict(app_conn, new_prediction, insert_prediction, t, days[1])
    resolve_due(app_conn, cal)
    before = _row(app_conn, pid)
    assert before[0] is not None
    with pytest.raises(errors.RestrictViolation):
        app_conn.execute("UPDATE predictions SET resolved_at=%s, realized_ret=0.5, label_up=1 WHERE id=%s",
                         (days[7].date(), pid))
    assert _row(app_conn, pid) == before


@pytest.mark.fast
def test_dry_run_writes_nothing_but_counts(app_conn, new_prediction, insert_prediction):
    t, days, cal = _setup(app_conn, 2005)
    pid = _predict(app_conn, new_prediction, insert_prediction, t, days[1])
    dry = resolve_due(app_conn, cal, dry_run=True)
    assert dry.dry_run and dry.resolved == 1
    assert _row(app_conn, pid) == (None,) * 6
    real = resolve_due(app_conn, cal)
    assert real.resolved == 1 and _row(app_conn, pid)[0] == days[6].date()


@pytest.mark.fast
def test_adjustment_mismatch_is_not_written(app_conn, new_prediction, insert_prediction):
    # %100 bedelsiz days[4]'te. Ham: 100,101,102,103,52,53,54,...; made_at (days[1]) close_adj'i ESKI olcekte
    # (101) kalmis, sonrasi yeni olcekte (26.. gibi) -> sahte -%49 gorunur. Yazilmamali.
    raw = [100, 101, 102, 103, 52, 53, 54, 55, 56, 57, 58, 59]
    stale_adj = [100, 101, 102, 103, 52, 53, 54, 55, 56, 57, 58, 59]    # gecmis duzeltilmemis
    t, days, cal = _setup(app_conn, 2006, close=[float(x) for x in stale_adj], raw=[float(x) for x in raw])
    app_conn.execute("INSERT INTO corporate_actions (ticker, ex_date, kind, value, fetched_at)"
                     " VALUES (%s,%s,'split',2.0,now())", (t, days[4].date()))
    pid = _predict(app_conn, new_prediction, insert_prediction, t, days[1])
    res = resolve_due(app_conn, cal)
    assert res.skipped.get("adjustment_mismatch", 0) == 1
    assert any("adjustment_mismatch" in d and t in d for d in res.details)
    assert _row(app_conn, pid) == (None,) * 6
    assert resolve_due(app_conn, cal).resolved == 0


@pytest.mark.fast
def test_consistent_split_resolves_with_real_return(app_conn, new_prediction, insert_prediction):
    # Ayni bedelsiz ama close_adj surekli (onceki gunler yariya indirilmis): getiri ~ gercek, sahte dusus yok
    raw = [100, 101, 102, 103, 52, 53, 54, 55, 56, 57, 58, 59]
    adj = [50, 50.5, 51, 51.5, 52, 53, 54, 55, 56, 57, 58, 59]
    t, days, cal = _setup(app_conn, 2007, close=[float(x) for x in adj], raw=[float(x) for x in raw])
    app_conn.execute("INSERT INTO corporate_actions (ticker, ex_date, kind, value, fetched_at)"
                     " VALUES (%s,%s,'split',2.0,now())", (t, days[4].date()))
    pid = _predict(app_conn, new_prediction, insert_prediction, t, days[1])
    assert resolve_due(app_conn, cal).resolved == 1
    r = _row(app_conn, pid)
    assert r[1] == pytest.approx(54 / 50.5 - 1)          # +%6,9; sahte -%47 degil
    assert r[3] == pytest.approx(54 / (adj[2] * (raw[2] - 0.5) / raw[2]) - 1, abs=1e-9)
    assert r[4] == 1


@pytest.mark.fast
def test_horizon_unknown_when_calendar_too_short(app_conn, new_prediction, insert_prediction):
    t, days, cal = _setup(app_conn, 2008)
    pid = _predict(app_conn, new_prediction, insert_prediction, t, days[-2])   # +5 takvim disi
    res = resolve_due(app_conn, cal)
    assert res.skipped.get("horizon_unknown", 0) >= 1
    assert _row(app_conn, pid)[0] is None


@pytest.mark.fast
def test_unexplained_jump_without_corporate_action_is_not_written(app_conn, new_prediction, insert_prediction):
    # %100 bedelsiz days[4]'te, kaynak uygulamamis (close = close_adj), corporate_actions'ta KAYIT YOK:
    # mismatch korumasi gecer ama sahte -%49 yazilmamali. Satir sonraki calistirmada yine degerlendirilir.
    raw = [100.0, 101, 102, 103, 52, 53, 54, 55, 56, 57, 58, 59]
    t, days, cal = _setup(app_conn, 2009, close=raw, raw=raw)
    pid = _predict(app_conn, new_prediction, insert_prediction, t, days[1])
    res = resolve_due(app_conn, cal)
    assert res.skipped.get("unexplained_jump", 0) == 1 and res.resolved == 0
    assert any(d.startswith("unexplained_jump") and t in d and str(days[4].date()) in d for d in res.details)
    assert _row(app_conn, pid) == (None,) * 6
    again = resolve_due(app_conn, cal)
    assert again.skipped.get("unexplained_jump", 0) == 1       # tekrar degerlendirilir, hala yazilmaz

    # veri duzelince (close_adj surekli, kayit eklendi) ayni satir cozulur
    app_conn.execute("INSERT INTO corporate_actions (ticker, ex_date, kind, value, fetched_at)"
                     " VALUES (%s,%s,'split',2.0,now())", (t, days[4].date()))
    app_conn.execute("UPDATE prices SET close_adj = close / 2 WHERE ticker = %s AND date < %s", (t, days[4].date()))
    assert resolve_due(app_conn, cal).resolved == 1
    assert _row(app_conn, pid)[1] == pytest.approx(54 / 50.5 - 1)


@pytest.mark.fast
def test_stale_ticker_is_separated_from_not_due(app_conn, new_prediction, insert_prediction):
    # ticker barlari days[5]'te durmus; takvim days[11]'e kadar gitmis (END=days[6]'dan 5 islem gunu sonra)
    t, days, cal = _setup(app_conn, 2010, n_bars=6)
    pid = _predict(app_conn, new_prediction, insert_prediction, t, days[1])
    res = resolve_due(app_conn, cal)
    assert res.skipped.get("stale_ticker", 0) == 1
    assert any(d.startswith("stale_ticker") and t in d and str(days[5].date()) in d for d in res.details)
    assert _row(app_conn, pid)[0] is None


@pytest.mark.fast
def test_not_due_details_include_sample_rows(app_conn, new_prediction, insert_prediction):
    t, days, cal = _setup(app_conn, 2012, n_bars=6)
    cal = TradingCalendar(days[:8])
    _predict(app_conn, new_prediction, insert_prediction, t, days[1])
    res = resolve_due(app_conn, cal)
    assert any(d.startswith("not_due") and t in d and str(days[1].date()) in d for d in res.details)


@pytest.fixture()
def iso(admin_conninfo):
    """Izole, bos veritabani (count_overdue tum cozulmemis satirlari sayar; ortak DB'de baska testlerin satirlari var)."""
    name = f"bist_over_{uuid.uuid4().hex[:8]}"
    run_init(admin_conninfo, db_name=name, app_password=APP_PASSWORD)
    info = with_overrides(admin_conninfo, dbname=name, user=APP_ROLE, password=APP_PASSWORD)
    with psycopg.connect(info, autocommit=True) as conn:
        yield conn


@pytest.mark.fast
def test_count_overdue_empty(iso):
    cal = TradingCalendar(pd.bdate_range("2011-01-05", periods=12))
    assert count_overdue(iso, cal) == {"overdue": 0, "oldest_made_at": None, "tickers": []}


def test_count_overdue_counts_only_old_unresolved_rows(iso, new_prediction, insert_prediction):
    t_ok, days, cal = _setup(iso, 2013)                    # 12 gun; made=days[1] cozulebilir
    resolved_id = _predict(iso, new_prediction, insert_prediction, t_ok, days[1])
    assert resolve_due(iso, cal).resolved == 1 and _row(iso, resolved_id)[0] is not None

    stuck_a, stuck_b = "STUCKA", "STUCKB"                  # fiyatsiz: hic cozulmez
    _predict(iso, new_prediction, insert_prediction, stuck_a, days[1])       # end days[6]: 5 gun gecikme
    _predict(iso, new_prediction, insert_prediction, stuck_b, days[2], "model_b")   # end days[7]: 4 gun
    _predict(iso, new_prediction, insert_prediction, "FRESH", days[5])       # end days[10]: 1 gun, henuz degil
    _predict(iso, new_prediction, insert_prediction, "NOEND", days[-2])      # vade bilinemiyor: sayilmaz

    out = count_overdue(iso, cal)
    assert out == {"overdue": 2, "oldest_made_at": days[1].date(), "tickers": [stuck_a, stuck_b]}
    assert count_overdue(iso, cal, grace_trading_days=5)["overdue"] == 1     # yalniz 5 gun gecikmis olan
    assert count_overdue(iso, cal, grace_trading_days=1)["overdue"] == 3     # FRESH da girer
    assert count_overdue(iso, cal, grace_trading_days=6)["overdue"] == 0


def test_count_overdue_counts_rows_and_caps_ticker_list(iso, new_prediction, insert_prediction):
    days = pd.bdate_range("2014-01-06", periods=12)
    cal = TradingCalendar(days)
    for i in range(25):
        _predict(iso, new_prediction, insert_prediction, f"OV{i:02d}", days[1])
    _predict(iso, new_prediction, insert_prediction, "OV00", days[1], "model_b")   # ayni ticker, ikinci model satiri
    out = count_overdue(iso, cal)
    assert out["overdue"] == 26                            # satir sayisi
    assert len(out["tickers"]) == 20 and out["tickers"] == sorted(out["tickers"])
    assert out["tickers"][0] == "OV00"
