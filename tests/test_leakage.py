"""Sizinti dedektoru ve ozellik uretiminin sizintisizligi (PROJE.md Bolum 5, 13)."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from test_features import at, make_frame, make_world

from bist_tahmin.calendar import TradingCalendar
from bist_tahmin.features import build as fb
from bist_tahmin.features.build import FeatureRow, FeatureSet, assert_no_lookahead, build_features, make_feature_row


# --- kasten sizintili ozellik fonksiyonlari --------------------------------------------------------------------

def _leaky_forward_return(histories, xu100, made_at, calendar):
    """Gelecege bakar: made_at'ten 5 bar sonraki getiri (negatif kaydirma). Tam veride deger, kesikte NaN->-9."""
    base = build_features(histories, xu100, made_at, calendar)
    rows = {}
    for t, row in base.rows.items():
        fwd = histories[t]["close_adj"].shift(-5) / histories[t]["close_adj"] - 1.0
        v = fwd.get(pd.Timestamp(made_at), np.nan)
        rows[t] = make_feature_row(t, {**row.features, "fwd_ret_5": -9.0 if pd.isna(v) else float(v)})
    return FeatureSet(made_at=base.made_at, rows=rows, excluded=base.excluded)


def _leaky_full_sample_zscore(histories, xu100, made_at, calendar):
    """Gelecege bakar: bugunku getiriyi TUM orneklemin (made_at sonrasi dahil) ortalama/std'siyle normalize eder."""
    base = build_features(histories, xu100, made_at, calendar)
    rows = {}
    for t, row in base.rows.items():
        c = histories[t]["close_adj"]
        r = c.pct_change()
        z = (row.features["ret_1"] - r.mean()) / r.std()
        rows[t] = make_feature_row(t, {**row.features, "ret_1_fullz": float(z)})
    return FeatureSet(made_at=base.made_at, rows=rows, excluded=base.excluded)


def _leaky_last_row(histories, xu100, made_at, calendar):
    """Gelecege bakar: made_at filtresini unutur, serinin SON satirini kullanir; sadece ayirt edici sonuc dondurur."""
    base = build_features(histories, xu100, made_at, calendar)
    rows = {t: make_feature_row(t, {**row.features, "last_px": float(histories[t]["close_adj"].iloc[-1])})
            for t, row in base.rows.items()}
    return FeatureSet(made_at=base.made_at, rows=rows, excluded=base.excluded)


def _leaky_excludes_by_future(histories, xu100, made_at, calendar):
    """Gelecege bakar ve etkisi yalnizca excluded'ta gorulur: gelecekte dusen hisseyi eler."""
    base = build_features(histories, xu100, made_at, calendar)
    excluded = dict(base.excluded)
    rows = dict(base.rows)
    for t in list(rows):
        later = histories[t].loc[histories[t].index > made_at, "close_adj"]
        if len(later) and later.iloc[-1] < histories[t].loc[made_at, "close_adj"]:
            excluded[t] = "future_drop"
            del rows[t]
    return FeatureSet(made_at=base.made_at, rows=rows, excluded=excluded)


LEAKY = [_leaky_forward_return, _leaky_full_sample_zscore, _leaky_last_row, _leaky_excludes_by_future]


@pytest.mark.parametrize("fn", LEAKY, ids=lambda f: f.__name__)
def test_detector_catches_deliberately_leaky_feature_functions(fn):
    hist, xu, cal = make_world(n=160, seed=21)
    m = at(cal, 30)
    with pytest.raises(AssertionError, match="SIZINTI"):
        assert_no_lookahead(fn, hist, xu, m, cal)


def test_leaky_function_passes_build_but_is_caught():
    """build_features temiz; sizintili sarmalayici aynı girdiyle yakalanir (dedektor build'in kendisini de sinar)."""
    hist, xu, cal = make_world(n=160, seed=22)
    m = at(cal, 30)
    assert_no_lookahead(build_features, hist, xu, m, cal)  # temiz: hata yok
    with pytest.raises(AssertionError):
        assert_no_lookahead(_leaky_forward_return, hist, xu, m, cal)


def test_build_features_passes_detector_at_many_made_at_dates():
    hist, xu, cal = make_world(n=200, seed=23)
    for off in (1, 5, 20, 60, 100):
        assert_no_lookahead(build_features, hist, xu, at(cal, off), cal)


def test_detector_with_gaps_and_exclusions_still_clean():
    hist, xu, cal = make_world(("A", "B", "C", "D"), n=150, seed=24)
    m = at(cal, 10)
    hist["B"] = hist["B"].drop(index=hist["B"].index[-14])       # made_at oncesi bosluk
    hist["C"] = hist["C"].iloc[:-3]                              # made_at sonrasi satirlar eksik (gelecek kismi)
    hist["D"].loc[m, "close_adj"] = np.nan
    assert_no_lookahead(build_features, hist, xu, m, cal)


def test_detector_vacuous_input_raises_unless_allowed():
    hist, xu, cal = make_world(n=100, seed=25)
    m = cal.days[-1]  # gelecek satir yok
    with pytest.raises(ValueError, match="made_at sonrasi satir yok"):
        assert_no_lookahead(build_features, hist, xu, m, cal)
    assert_no_lookahead(build_features, hist, xu, m, cal, require_future_rows=False)


def test_detector_reports_difference_details_and_does_not_mutate_inputs():
    hist, xu, cal = make_world(("A", "B", "C"), n=140, seed=26)
    snap = {t: df.copy() for t, df in hist.items()}
    m = at(cal, 20)
    with pytest.raises(AssertionError) as ei:
        assert_no_lookahead(_leaky_forward_return, hist, xu, m, cal)
    assert "fwd_ret_5" in str(ei.value)
    for t in hist:
        pd.testing.assert_frame_equal(hist[t], snap[t])


def test_detector_catches_hash_only_difference():
    def fn(h, x, m, c):
        base = build_features(h, x, m, c)
        rows = {t: FeatureRow(t, r.features, r.features_json, r.feature_hash + ("x" if len(x) > 165 else ""))
                for t, r in base.rows.items()}
        return FeatureSet(base.made_at, rows, base.excluded)

    hist, xu, cal = make_world(n=170, seed=27)
    with pytest.raises(AssertionError, match="feature_hash"):
        assert_no_lookahead(fn, hist, xu, at(cal, 10), cal)


def test_future_calendar_rows_do_not_change_features():
    hist, xu, cal = make_world(n=150, seed=28)
    m = at(cal, 20)
    a = build_features(hist, xu, m, cal)
    b = build_features(hist, xu, m, TradingCalendar(cal.days[cal.days <= m]))
    assert {t: r.feature_hash for t, r in a.rows.items()} == {t: r.feature_hash for t, r in b.rows.items()}


# --- kaynak taramasi -------------------------------------------------------------------------------------------

def test_build_source_has_no_centered_windows_or_negative_shifts():
    src = Path(fb.__file__).read_text(encoding="utf-8")
    assert not re.search(r"center\s*=\s*True", src)
    assert not re.search(r"shift\(\s*-", src)
    assert not re.search(r"periods\s*=\s*-", src)  # shift/diff/pct_change(periods=-k) de gelecege bakar
    assert not re.search(r"\.(?:diff|pct_change)\(\s*-", src)
