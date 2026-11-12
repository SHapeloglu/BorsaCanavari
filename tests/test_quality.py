"""Veri kalite kontrolleri (PROJE.md Bolum 4, 13). Agsiz calisir."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from bist_tahmin.data.quality import check_bars, drop_incomplete_today_bar

NOW = pd.Timestamp("2030-01-01 20:00")  # tum test verisinden sonra; gelecek/yarim bar tetiklemez


def make_bars(n: int = 30, start: str = "2024-01-02") -> pd.DataFrame:
    idx = pd.DatetimeIndex(pd.bdate_range(start, periods=n), name="date")
    close = 100.0 + np.arange(n) * 0.5
    return pd.DataFrame(
        {"open": close, "high": close + 1, "low": close - 1, "close": close,
         "volume": np.full(n, 1000.0), "close_adj": close},
        index=idx,
    )


def check(bars, **kw):
    kw.setdefault("now", NOW)
    return check_bars(bars, "TEST", **kw)


@pytest.mark.fast
def test_clean_data_has_no_issues():
    rep = check(make_bars())
    assert rep.issues == []
    assert rep.usable


@pytest.mark.fast
def test_duplicate_dates_error():
    b = make_bars()
    b = pd.concat([b, b.iloc[[5]]]).sort_index(kind="stable")
    rep = check(b)
    assert "duplicate_dates" in rep.codes
    assert rep.get("duplicate_dates").dates == (b.index[5],)
    assert not rep.usable


@pytest.mark.parametrize("col", ["close", "close_adj"])
@pytest.mark.parametrize("bad", [0.0, -1.0, np.nan])
@pytest.mark.fast
def test_bad_price_error(col, bad):
    b = make_bars()
    b.iloc[7, b.columns.get_loc(col)] = bad
    rep = check(b)
    code = "bad_close" if col == "close" else "bad_close_adj"
    assert code in rep.codes
    assert rep.get(code).dates == (b.index[7],)
    assert rep.get(code).severity == "error"
    assert not rep.usable


@pytest.mark.fast
def test_not_monotonic_error():
    b = make_bars()
    b = b.iloc[[0, 1, 3, 2, 4] + list(range(5, len(b)))]
    rep = check(b)
    assert "not_monotonic" in rep.codes
    assert not rep.usable


@pytest.mark.fast
def test_jump_flagged_but_not_removed():
    b = make_bars()
    b.iloc[10, b.columns.get_loc("close")] *= 2  # +%100 ve ertesi gun geri doner
    before = b.copy()
    rep = check(b)
    jump = rep.get("jump_close")
    assert jump is not None and jump.severity == "flag"
    assert b.index[10] in jump.dates and b.index[11] in jump.dates  # sicrama ve geri donus
    assert rep.get("jump_close_adj") is None  # close_adj ayri degerlendirilir
    assert rep.usable  # yalnizca isaret
    pd.testing.assert_frame_equal(b, before)  # silinmedi/degismedi
    assert len(b) == len(before)


@pytest.mark.fast
def test_jump_adj_flagged_separately():
    b = make_bars()
    b.iloc[10, b.columns.get_loc("close_adj")] *= 0.5
    rep = check(b)
    assert "jump_close_adj" in rep.codes
    assert "jump_close" not in rep.codes


@pytest.mark.fast
def test_jump_threshold_is_parameter():
    b = make_bars()
    b.iloc[10, b.columns.get_loc("close")] *= 1.2
    assert "jump_close" not in check(b).codes
    assert "jump_close" in check(b, jump_threshold=0.1).codes


@pytest.mark.fast
def test_zero_volume_flag():
    b = make_bars()
    b.iloc[3, b.columns.get_loc("volume")] = 0.0
    rep = check(b)
    issue = rep.get("zero_volume")
    assert issue.severity == "flag" and issue.dates == (b.index[3],)
    assert rep.usable


@pytest.mark.fast
def test_missing_trading_days_flag_and_error():
    full = make_bars(40)
    td = full.index
    # tek eksik gun: flag, kullanilabilir
    b = full.drop(full.index[10])
    rep = check(b, trading_days=td)
    assert rep.get("missing_trading_days").dates == (full.index[10],)
    assert rep.usable
    # cok eksik gun (son 40 gunde %5'ten fazla): error
    b2 = full.drop(full.index[[10, 11, 12]])
    rep2 = check(b2, trading_days=td)
    assert "excessive_missing_days" in rep2.codes
    assert not rep2.usable
    # esik parametre: gevsetilince error yok
    assert check(b2, trading_days=td, max_missing_ratio=0.5).usable


@pytest.mark.fast
def test_missing_days_only_after_first_bar():
    """Gec baslayan hisse: ilk bardan onceki XU100 gunleri eksik sayilmaz."""
    full = make_bars(40)
    b = full.iloc[10:]
    rep = check(b, trading_days=full.index)
    assert rep.issues == []


@pytest.mark.fast
def test_missing_tail_days_detected():
    full = make_bars(40)
    rep = check(full.iloc[:-3], trading_days=full.index)
    assert rep.get("missing_trading_days").dates == tuple(full.index[-3:])


@pytest.mark.fast
def test_bars_on_non_trading_days_flag():
    full = make_bars(40)
    rep = check(full, trading_days=full.index.delete(5))
    assert rep.get("bars_on_non_trading_days").dates == (full.index[5],)


# --- yarim bar (bugun) ---

def _with_today(today: str = "2030-01-02") -> tuple[pd.DataFrame, pd.Timestamp]:
    b = make_bars(30, start="2029-11-20")
    t = pd.Timestamp(today)
    assert t > b.index[-1]
    row = b.iloc[[-1]].copy()
    row.index = pd.DatetimeIndex([t], name="date")
    return pd.concat([b, row]), t


@pytest.mark.fast
def test_half_bar_dropped_before_cutoff():
    b, t = _with_today()
    out = drop_incomplete_today_bar(b, now=pd.Timestamp("2030-01-02 17:00"))
    assert t not in out.index and len(out) == len(b) - 1
    assert t in b.index  # girdi degismedi


@pytest.mark.fast
def test_half_bar_kept_after_cutoff():
    b, t = _with_today()
    out = drop_incomplete_today_bar(b, now=pd.Timestamp("2030-01-02 19:00"))
    assert t in out.index and len(out) == len(b)


@pytest.mark.fast
def test_half_bar_cutoff_boundary_exact_is_accepted():
    b, t = _with_today()
    just_before = drop_incomplete_today_bar(b, now=pd.Timestamp("2030-01-02 18:29:59"))
    exact = drop_incomplete_today_bar(b, now=pd.Timestamp("2030-01-02 18:30:00"))
    assert t not in just_before.index
    assert t in exact.index


@pytest.mark.fast
def test_half_bar_custom_cutoff():
    b, t = _with_today()
    out = drop_incomplete_today_bar(b, now=pd.Timestamp("2030-01-02 18:45"), close_cutoff_time=dt.time(19, 0))
    assert t not in out.index


@pytest.mark.fast
def test_half_bar_timezone_aware_now_converted_to_istanbul():
    b, t = _with_today()
    # 15:00 UTC = 18:00 Istanbul (cutoff oncesi); 15:30 UTC = 18:30 Istanbul (kabul)
    assert t not in drop_incomplete_today_bar(b, now=pd.Timestamp("2030-01-02 15:00", tz="UTC")).index
    assert t in drop_incomplete_today_bar(b, now=pd.Timestamp("2030-01-02 15:30", tz="UTC")).index


@pytest.mark.fast
def test_half_bar_previous_days_untouched_and_no_bar_today_noop():
    b = make_bars(30, start="2029-11-20")
    out = drop_incomplete_today_bar(b, now=pd.Timestamp("2030-01-02 10:00"))
    pd.testing.assert_frame_equal(out, b)
    assert out is not b


@pytest.mark.fast
def test_check_bars_reports_half_bar_as_flag_and_ignores_it():
    b, t = _with_today()
    b.loc[t, "volume"] = 0.0  # yarim barin hacmi sifir olabilir; rapora girmemeli
    b.loc[t, "close"] = b["close"].iloc[-2] * 2  # sicrama da yarim bardan gelirse sayilmaz
    rep = check_bars(b, "TEST", now=pd.Timestamp("2030-01-02 10:00"))
    issue = rep.get("incomplete_today_bar")
    assert issue.severity == "flag" and issue.dates == (t,)
    assert "zero_volume" not in rep.codes and "jump_close" not in rep.codes
    assert rep.usable


@pytest.mark.fast
def test_check_bars_accepts_today_bar_after_cutoff():
    b, t = _with_today()
    rep = check_bars(b, "TEST", now=pd.Timestamp("2030-01-02 18:30"))
    assert "incomplete_today_bar" not in rep.codes
    assert rep.usable


@pytest.mark.fast
def test_incomplete_today_not_counted_as_missing_day():
    b, t = _with_today()
    td = b.index  # XU100 de bugun bari getirmis
    rep = check_bars(b.iloc[:-1], "TEST", trading_days=td, now=pd.Timestamp("2030-01-02 10:00"))
    assert rep.issues == []
    # cutoff sonrasi bugunun bari beklenir
    rep2 = check_bars(b.iloc[:-1], "TEST", trading_days=td, now=pd.Timestamp("2030-01-02 19:00"))
    assert rep2.get("missing_trading_days").dates == (t,)


# --- gelecek tarih ---

@pytest.mark.fast
def test_future_dates_error():
    b = make_bars(30, start="2024-01-02")
    rep = check_bars(b, "TEST", now=pd.Timestamp("2024-01-10 20:00"))
    f = rep.get("future_dates")
    assert f.severity == "error"
    assert f.dates[0] == pd.Timestamp("2024-01-11")
    assert not rep.usable


# --- yapisal ---

@pytest.mark.fast
def test_missing_columns_and_empty_and_bad_index():
    assert "missing_columns" in check(make_bars().drop(columns=["volume"])).codes
    assert "empty" in check(make_bars().iloc[:0]).codes
    tz = make_bars()
    tz.index = tz.index.tz_localize("Europe/Istanbul")
    assert "bad_index" in check(tz).codes
    ts = make_bars()
    ts.index = ts.index + pd.Timedelta(hours=9)
    assert "bad_index" in check(ts).codes


# --- mutate etmeme ---

@pytest.mark.fast
def test_input_not_mutated_even_with_all_problems():
    b, t = _with_today()
    b = pd.concat([b, b.iloc[[3]]])  # tekrar + sira bozuk
    b.iloc[5, b.columns.get_loc("close")] = -1.0
    b.iloc[8, b.columns.get_loc("close_adj")] *= 3
    b.iloc[9, b.columns.get_loc("volume")] = 0.0
    snapshot = b.copy(deep=True)
    check_bars(b, "TEST", trading_days=b.index.unique().sort_values(), now=pd.Timestamp("2030-01-02 10:00"))
    drop_incomplete_today_bar(b, now=pd.Timestamp("2030-01-02 10:00"))
    pd.testing.assert_frame_equal(b, snapshot)
    assert b.index.equals(snapshot.index)
