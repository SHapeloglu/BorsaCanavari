"""PROJE.md Bolum 13: cozucu saf cekirdek (DB yok). Elle hesaplanmis ornekler."""

import pandas as pd
import pytest

from bist_tahmin.calendar import TradingCalendar
from bist_tahmin.resolve.resolver import (
    MAX_DAILY_MOVE,
    MAX_DETAILS_PER_REASON,
    MISMATCH_TOL,
    STALE_TRADING_DAYS,
    ResolveResult,
    compute_outcome,
    explain_outcome,
)

# 2026-03-02 Pzt .. ; 2026-03-04 ve 05 tatil (barsiz) -> 5 ISLEM gunu sonrasi takvim gunu 5'ten fazla
DAYS = pd.to_datetime([
    "2026-03-02", "2026-03-03", "2026-03-06", "2026-03-09", "2026-03-10", "2026-03-11", "2026-03-12",
    "2026-03-13", "2026-03-16",
])
CAL = TradingCalendar(DAYS)
MADE = pd.Timestamp("2026-03-03")      # islem gunu #2
END = pd.Timestamp("2026-03-12")       # 06,09,10,11,12 -> 5. islem gunu
NEXT = pd.Timestamp("2026-03-06")


def bars(close, open_=None, raw=None, index=DAYS):
    """close: close_adj. raw verilmezse close_adj = close (bolunmesiz). open_adj = open*close_adj/close."""
    close = pd.Series(close, index=index, dtype=float)
    raw = close.copy() if raw is None else pd.Series(raw, index=index, dtype=float)
    op = raw.copy() if open_ is None else pd.Series(open_, index=index, dtype=float)
    return pd.DataFrame({"open": op, "close": raw, "close_adj": close, "open_adj": op * close / raw})


def flat(v=100.0, n=len(DAYS)):
    return [v] * n


@pytest.mark.fast
def test_horizon_counts_trading_days_not_calendar_days():
    assert CAL.horizon_end(MADE, 5) == END           # 5 islem gunu = 9 takvim gunu (tatil atlandi)
    assert (END - MADE).days == 9


@pytest.mark.fast
def test_known_example_all_fields():
    close = flat(100.0)
    close[DAYS.get_loc(MADE)] = 100.0
    close[DAYS.get_loc(END)] = 110.0
    opn = flat(100.0)
    opn[DAYS.get_loc(NEXT)] = 104.0                   # ertesi gun acilisinda giris
    xu = flat(1000.0)
    xu[DAYS.get_loc(END)] = 1030.0                    # XU100 +%3
    out = compute_outcome(MADE, END, bars(close, opn), bars(xu), CAL)
    assert out.resolved_at == END.date()
    assert out.realized_ret == pytest.approx(0.10)
    assert out.realized_rel_ret == pytest.approx(0.10 - 0.03)
    assert out.tradable_ret == pytest.approx(110.0 / 104.0 - 1.0)
    assert out.label_up == 1 and out.label_rel_up == 1


@pytest.mark.fast
def test_tradable_uses_next_day_open_not_close_entry():
    close = flat(100.0)
    close[DAYS.get_loc(END)] = 110.0
    opn_gap = flat(100.0)
    opn_gap[DAYS.get_loc(NEXT)] = 108.0               # tahmin sonrasi gap
    out = compute_outcome(MADE, END, bars(close, opn_gap), bars(flat(1000.0)), CAL)
    assert out.realized_ret == pytest.approx(0.10)    # kapanis girisi: +%10
    assert out.tradable_ret == pytest.approx(110 / 108 - 1)   # acilis girisi: ~+%1,85 (FARKLI)
    assert abs(out.realized_ret - out.tradable_ret) > 0.05


@pytest.mark.fast
def test_next_day_is_next_trading_day_skipping_holiday():
    # made_at 03-03 -> next = 03-06 (04, 05 tatil). Yalnizca 03-06 acilisi kullanilmali.
    close = flat(100.0)
    close[DAYS.get_loc(END)] = 114.0                  # tek gunluk sicrama MAX_DAILY_MOVE altinda kalmali
    opn = flat(100.0)
    opn[DAYS.get_loc(pd.Timestamp("2026-03-06"))] = 150.0
    out = compute_outcome(MADE, END, bars(close, opn), bars(flat(1000.0)), CAL)
    assert out.tradable_ret == pytest.approx(114 / 150 - 1)


@pytest.mark.fast
def test_not_due_when_end_bar_missing():
    t = bars(flat(100.0)).loc[:"2026-03-11"]          # END (03-12) bari henuz yok
    out, reason, _ = explain_outcome(MADE, END, t, bars(flat(1000.0)), CAL)
    assert out is None and reason == "not_due"
    assert compute_outcome(MADE, END, t, bars(flat(1000.0)), CAL) is None


@pytest.mark.fast
def test_xu100_end_bar_missing_returns_none():
    xu = bars(flat(1000.0)).loc[:"2026-03-11"]
    out, reason, _ = explain_outcome(MADE, END, bars(flat(100.0)), xu, CAL)
    assert out is None and reason == "not_due"


@pytest.mark.fast
def test_hole_in_data_is_missing_bar_not_not_due():
    t = bars(flat(100.0)).drop(END)                   # sonrasi bar var, END deligi
    out, reason, _ = explain_outcome(MADE, END, t, bars(flat(1000.0)), CAL)
    assert out is None and reason == "missing_bar"


@pytest.mark.fast
def test_missing_made_at_or_open_bar_returns_none():
    t = bars(flat(100.0))
    assert compute_outcome(MADE, END, t.drop(MADE), bars(flat(1000.0)), CAL) is None
    assert compute_outcome(MADE, END, t.drop(NEXT), bars(flat(1000.0)), CAL) is None
    t_nan = t.copy()
    t_nan.loc[NEXT, ["open", "open_adj"]] = float("nan")
    assert compute_outcome(MADE, END, t_nan, bars(flat(1000.0)), CAL) is None


@pytest.mark.fast
def test_label_threshold():
    close = flat(100.0)
    close[DAYS.get_loc(END)] = 101.0                  # +%1
    t, x = bars(close), bars(flat(1000.0))
    assert compute_outcome(MADE, END, t, x, CAL).label_up == 1
    assert compute_outcome(MADE, END, t, x, CAL, label_threshold=0.02).label_up == 0
    assert compute_outcome(MADE, END, t, x, CAL, label_threshold=0.0101).label_up == 0
    assert compute_outcome(MADE, END, t, x, CAL, label_threshold=0.0099).label_up == 1
    # rel etiketi esikten bagimsiz
    assert compute_outcome(MADE, END, t, x, CAL, label_threshold=0.02).label_rel_up == 1


@pytest.mark.fast
def test_down_and_underperform_labels():
    close = flat(100.0)
    close[DAYS.get_loc(END)] = 95.0
    xu = flat(1000.0)
    xu[DAYS.get_loc(END)] = 900.0                     # XU100 -%10, hisse -%5 -> rel +5
    out = compute_outcome(MADE, END, bars(close), bars(xu), CAL)
    assert out.label_up == 0 and out.label_rel_up == 1
    assert out.realized_rel_ret == pytest.approx(-0.05 + 0.10)


@pytest.mark.fast
def test_bonus_split_in_window_no_fake_drop():
    # %100 bedelsiz (carpan 2.0) 03-09'da: ham 100 -> 50, close_adj surekli 50. Gercek getiri ~0.
    raw = [100, 100, 100, 50, 50, 50, 50, 50, 50]
    adj = [50, 50, 50, 50, 50, 50, 50, 50, 50]
    t = bars(adj, raw=raw)
    out = compute_outcome(MADE, END, t, bars(flat(1000.0)), CAL, splits=[(pd.Timestamp("2026-03-09"), 2.0)])
    assert out is not None
    assert out.realized_ret == pytest.approx(0.0, abs=1e-12)     # sahte -%50 yok
    assert out.tradable_ret == pytest.approx(0.0, abs=1e-12)
    assert out.label_up == 0


@pytest.mark.fast
def test_split_missing_from_corporate_actions_is_caught():
    # Ham %50 dusus var, close_adj surekli ama bolunme kaydi yok -> iki yol uyusmaz -> atla
    raw = [100, 100, 100, 50, 50, 50, 50, 50, 50]
    t = bars([50] * 9, raw=raw)
    out, reason, _ = explain_outcome(MADE, END, t, bars(flat(1000.0)), CAL, splits=[])
    assert out is None and reason == "adjustment_mismatch"


@pytest.mark.fast
def test_stale_close_adj_snapshot_is_mismatch():
    # made_at satiri ESKI olcekte (100), sonraki barlar yeni olcekte (50): close_adj orani -%50 gibi gorunur,
    # oysa ham+bolunme ile getiri ~0. Yazilmamali.
    raw = [100, 100, 100, 50, 50, 50, 50, 50, 50]
    stale_adj = [100, 100, 100, 50, 50, 50, 50, 50, 50]
    t = bars(stale_adj, raw=raw)
    out, reason, detail = explain_outcome(MADE, END, t, bars(flat(1000.0)), CAL,
                                          splits=[(pd.Timestamp("2026-03-09"), 2.0)])
    assert out is None and reason == "adjustment_mismatch"
    assert "duzeltmeli" in detail
    assert compute_outcome(MADE, END, t, bars(flat(1000.0)), CAL,
                           splits=[(pd.Timestamp("2026-03-09"), 2.0)]) is None


@pytest.mark.fast
def test_stale_open_adj_only_is_mismatch_for_tradable():
    # realized tutarli ama next_day'in open_adj'i bayat: tradable korumasi devreye girmeli
    raw = [100.0] * 9
    t = bars(flat(100.0), raw=raw)
    t.loc[NEXT, "open_adj"] = 50.0                    # bayat/yanlis olcek
    out, reason, detail = explain_outcome(MADE, END, t, bars(flat(1000.0)), CAL)
    assert out is None and reason == "adjustment_mismatch" and "tradable" in detail


@pytest.mark.fast
def test_split_on_next_day_does_not_count_for_open_entry():
    # bolunme next_day'in kendisinde: acilis zaten yeni olcekte, tradable carpansiz hesaplanir
    raw = [100, 100, 50, 50, 50, 50, 50, 50, 50]      # 03-06 (NEXT) ex-date
    t = bars([50] * 9, raw=raw)
    out = compute_outcome(MADE, END, t, bars(flat(1000.0)), CAL, splits=[(NEXT, 2.0)])
    assert out is not None
    assert out.realized_ret == pytest.approx(0.0, abs=1e-12)
    assert out.tradable_ret == pytest.approx(0.0, abs=1e-12)


@pytest.mark.fast
def test_split_outside_window_ignored():
    t = bars(flat(100.0))
    out = compute_outcome(MADE, END, t, bars(flat(1000.0)), CAL,
                          splits=[(pd.Timestamp("2026-03-02"), 2.0), (pd.Timestamp("2026-03-13"), 3.0)])
    assert out is not None and out.realized_ret == pytest.approx(0.0)


@pytest.mark.fast
def test_small_difference_below_tolerance_is_accepted():
    close = flat(100.0)
    raw = flat(100.0)
    raw[DAYS.get_loc(END)] = 100.0 * (1 + MISMATCH_TOL / 2)   # ham/duzeltmeli farki tolerans icinde
    out = compute_outcome(MADE, END, bars(close, raw=raw), bars(flat(1000.0)), CAL)
    assert out is not None


@pytest.mark.fast
def test_invalid_prices_none():
    close = flat(100.0)
    close[DAYS.get_loc(MADE)] = 0.0
    assert compute_outcome(MADE, END, bars(close, raw=flat(100.0)), bars(flat(1000.0)), CAL) is None


@pytest.mark.fast
def test_end_not_after_made_at_none():
    assert compute_outcome(MADE, MADE, bars(flat(100.0)), bars(flat(1000.0)), CAL) is None


# --- SORUN 1: aciklanamayan sicrama (kaynak bolunmeyi uygulamamis VE corporate_actions kaydi yok) ---

@pytest.mark.fast
def test_unapplied_bonus_split_without_record_is_not_written():
    # %100 bedelsiz, ne close ne close_adj duzeltilmis, splits=() : iki yol da -%50 gosterir (fark 0), mismatch gecer.
    raw = [100, 100, 102, 51, 51, 51, 51, 51, 51]
    t = bars(raw, raw=raw)
    out, reason, detail = explain_outcome(MADE, END, t, bars(flat(1000.0)), CAL, splits=())
    assert out is None and reason == "unexplained_jump"
    assert "ticker" in detail and "2026-03-09" in detail and "-50" in detail
    assert compute_outcome(MADE, END, t, bars(flat(1000.0)), CAL) is None


@pytest.mark.fast
def test_xu100_jump_is_not_written():
    xu = flat(1000.0)
    xu[DAYS.get_loc(pd.Timestamp("2026-03-10"))] = 700.0
    out, reason, detail = explain_outcome(MADE, END, bars(flat(100.0)), bars(xu), CAL)
    assert out is None and reason == "unexplained_jump" and "XU100" in detail


@pytest.mark.fast
def test_jump_outside_made_end_window_is_ignored():
    # made_at oncesi ve end sonrasi sicramalar pencereye girmez
    close = flat(100.0)
    close[0] = 50.0                                   # 03-02 -> 03-03: +%100, made_at oncesi
    close[-1] = 200.0                                 # 03-16: end sonrasi
    out = compute_outcome(MADE, END, bars(close), bars(flat(1000.0)), CAL)
    assert out is not None and out.realized_ret == pytest.approx(0.0)


@pytest.mark.fast
def test_correctly_adjusted_split_still_resolves_with_jump_guard():
    # ham 100 -> 50 var, close_adj surekli, bolunme kaydi var: sicrama korumasi close_adj'e bakar, gecer
    raw = [100, 101, 102, 52, 53, 54, 55, 56, 57]
    adj = [50, 50.5, 51, 52, 53, 54, 55, 56, 57]
    t = bars(adj, raw=raw)
    out = compute_outcome(MADE, END, t, bars(flat(1000.0)), CAL, splits=[(pd.Timestamp("2026-03-09"), 2.0)])
    assert out is not None and out.realized_ret == pytest.approx(55 / 50.5 - 1)   # sahte -%46 degil


@pytest.mark.fast
def test_jump_threshold_boundary():
    for mv, ok in ((MAX_DAILY_MOVE - 0.01, True), (MAX_DAILY_MOVE + 0.01, False)):
        close = flat(100.0)
        close[DAYS.get_loc(END)] = 100.0 * (1 + mv)
        out, reason, _ = explain_outcome(MADE, END, bars(close), bars(flat(1000.0)), CAL)
        assert (out is not None) is ok
        assert ok or reason == "unexplained_jump"


@pytest.mark.fast
def test_mismatch_still_reported_before_jump():
    # eski semantik: bayat close_adj anlik goruntusu 'adjustment_mismatch' olarak kalir (sicrama da var)
    raw = [100, 100, 100, 50, 50, 50, 50, 50, 50]
    t = bars(raw, raw=raw)
    _, reason, _ = explain_outcome(MADE, END, t, bars(flat(1000.0)), CAL, splits=[(pd.Timestamp("2026-03-09"), 2.0)])
    assert reason == "adjustment_mismatch"


# --- SORUN 2: takili ticker ---

@pytest.mark.fast
def test_stale_ticker_when_bars_stopped_long_before_calendar_end():
    # ticker 03-11'de durmus (END=03-12 yok); takvim 03-16'ya kadar: 13, 16 = 2 islem gunu -> hala not_due
    t = bars(flat(100.0)).loc[:"2026-03-11"]
    assert explain_outcome(MADE, END, t, bars(flat(1000.0)), CAL)[1] == "not_due"
    # takvim 3 islem gunu daha uzar (END'den sonra 13, 16, 17, 18 = 4 gun): stale_ticker
    days = DAYS.append(pd.to_datetime(["2026-03-17", "2026-03-18"]))
    cal = TradingCalendar(days)
    assert STALE_TRADING_DAYS == 3
    xu = bars(flat(1000.0, len(days)), index=days)
    out, reason, detail = explain_outcome(MADE, END, t, xu, cal)
    assert out is None and reason == "stale_ticker"
    assert "2026-03-11" in detail                      # son bar tarihi


@pytest.mark.fast
def test_stale_boundary_exactly_three_trading_days():
    days = DAYS.append(pd.to_datetime(["2026-03-17"]))  # END sonrasi: 13, 16, 17 = 3 gun -> stale
    t = bars(flat(100.0)).loc[:"2026-03-11"]
    xu = bars(flat(1000.0, len(days)), index=days)
    assert explain_outcome(MADE, END, t, xu, TradingCalendar(days))[1] == "stale_ticker"


@pytest.mark.fast
def test_stale_respects_as_of():
    days = DAYS.append(pd.to_datetime(["2026-03-17", "2026-03-18"]))
    cal = TradingCalendar(days)
    t = bars(flat(100.0)).loc[:"2026-03-11"]
    xu = bars(flat(1000.0, len(days)), index=days)
    # as_of 03-13: referans gun 03-13 -> END'den 1 islem gunu; not_due
    assert explain_outcome(MADE, END, t, xu, cal, as_of=pd.Timestamp("2026-03-13").date())[1] == "not_due"
    assert explain_outcome(MADE, END, t, xu, cal, as_of=pd.Timestamp("2026-03-18").date())[1] == "stale_ticker"


@pytest.mark.fast
def test_hole_stays_missing_bar_even_if_calendar_far_ahead():
    days = DAYS.append(pd.to_datetime(["2026-03-17", "2026-03-18", "2026-03-19"]))
    cal = TradingCalendar(days)
    t = bars(flat(100.0)).drop(END)
    t.loc[pd.Timestamp("2026-03-13")] = t.iloc[-1]    # sonraki bar var -> delik
    xu = bars(flat(1000.0, len(days)), index=days)
    assert explain_outcome(MADE, END, t, xu, cal)[1] == "missing_bar"


@pytest.mark.fast
def test_result_details_capped_per_reason():
    res = ResolveResult()
    for i in range(MAX_DETAILS_PER_REASON + 15):
        res.skip("not_due", f"ornek {i}")
    res.skip("unexplained_jump", "tek")
    assert res.skipped["not_due"] == MAX_DETAILS_PER_REASON + 15
    assert sum(d.startswith("not_due") for d in res.details) == MAX_DETAILS_PER_REASON
    assert res.details[-1] == "unexplained_jump: tek"
    res.skip("x")                                      # ayrinti yoksa sayilir, satir eklenmez
    assert res.skipped["x"] == 1 and len(res.details) == MAX_DETAILS_PER_REASON + 1
