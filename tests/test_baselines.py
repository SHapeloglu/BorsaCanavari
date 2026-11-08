"""Baseline kurallari (PROJE.md Bolum 10) ve sizinti korumasi (Bolum 5, 13)."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from bist_tahmin.baselines.rules import (
    BASELINES,
    baseline_inputs,
    baseline_model_version,
    baseline_prob,
    historical_base_rate,
)
from bist_tahmin.calendar import TradingCalendar

T = pd.Timestamp


def series(values, end="2025-03-31"):
    idx = pd.bdate_range(end=end, periods=len(values))
    return pd.Series(values, index=idx, dtype=float)


def test_model_version_is_fixed():
    assert baseline_model_version("ma20") == "baseline_ma20_v1"
    assert [baseline_model_version(n) for n in BASELINES] == [f"baseline_{n}_v1" for n in BASELINES]
    with pytest.raises(ValueError):
        baseline_model_version("nope")
    with pytest.raises(ValueError):
        baseline_prob("nope", "X", series([1, 2]), "2025-03-31")


def test_always_up_is_clip_upper_bound():
    s = series([10, 9])
    assert baseline_prob("always_up", "AAA", s, s.index[-1]) == 0.99
    assert baseline_prob("always_up", "AAA", s, s.index[-1], clip=(0.05, 0.9)) == 0.9


def test_yesterday_up_down_and_exact_zero():
    for vals, expected in (([10, 11], 0.6), ([10, 9], 0.4), ([10, 10], 0.4)):
        s = series(vals)
        assert baseline_prob("yesterday", "AAA", s, s.index[-1]) == expected


def test_yesterday_insufficient_or_stale_is_none():
    s = series([10])
    assert baseline_prob("yesterday", "AAA", s, s.index[-1]) is None
    s = series([10, 11, 12])
    assert baseline_prob("yesterday", "AAA", s, s.index[-1] + pd.Timedelta(days=3)) is None  # bayat veri
    assert baseline_prob("yesterday", "AAA", s.iloc[0:0], "2025-03-31") is None


def test_ma20_boundaries():
    flat = [10.0] * 19
    up = series(flat + [10.5])
    down = series(flat + [9.5])
    equal = series(flat + [10.0])
    assert baseline_prob("ma20", "A", up, up.index[-1]) == 0.6
    assert baseline_prob("ma20", "A", down, down.index[-1]) == 0.4
    assert baseline_prob("ma20", "A", equal, equal.index[-1]) == 0.4  # esit -> asagi kural
    short = series(flat)
    assert baseline_prob("ma20", "A", short, short.index[-1]) is None  # 19 bar
    # MA bugunu icerir: son 20 = 19 sabit + son; ortalama < son ise 0.6 (ilk bar sayilmaz)
    long_ = series([100.0] + flat + [10.2])
    assert baseline_prob("ma20", "A", long_, long_.index[-1]) == 0.6


def test_base_rate_uses_param_only():
    s = series([1, 2])
    assert baseline_prob("base_rate", "A", s, s.index[-1]) is None
    assert baseline_prob("base_rate", "A", s, s.index[-1], base_rate=float("nan")) is None
    assert baseline_prob("base_rate", "A", s, s.index[-1], base_rate=0.55) == 0.55
    assert baseline_prob("base_rate", "A", s, s.index[-1], base_rate=1.0) == 0.99
    assert baseline_prob("base_rate", "A", s, s.index[-1], base_rate=0.0) == 0.01
    with pytest.raises(ValueError):
        baseline_prob("base_rate", "A", s, s.index[-1], base_rate=1.5)


def test_invalid_clip_rejected():
    s = series([1, 2])
    with pytest.raises(ValueError):
        baseline_prob("always_up", "A", s, s.index[-1], clip=(0.9, 0.1))


def test_random_deterministic_in_range_and_varies():
    s = series([1, 2])
    d = T("2025-03-31")
    a = baseline_prob("random", "AAA", s, d, seed=7)
    assert a == baseline_prob("random", "AAA", s, d, seed=7)
    assert 0.4 <= a <= 0.6
    assert a != baseline_prob("random", "BBB", s, d, seed=7)
    assert a != baseline_prob("random", "AAA", s, T("2025-04-01"), seed=7)
    assert a != baseline_prob("random", "AAA", s, d, seed=8)
    vals = [baseline_prob("random", f"T{i}", s, d) for i in range(200)]
    assert all(0.4 <= v <= 0.6 for v in vals)
    assert max(vals) - min(vals) > 0.1  # gercekten yayiliyor


def test_random_ignores_global_random_state():
    s = series([1, 2])
    np.random.seed(1)
    a = baseline_prob("random", "AAA", s, "2025-03-31")
    np.random.seed(999)
    assert a == baseline_prob("random", "AAA", s, "2025-03-31")


@pytest.mark.parametrize("name", BASELINES)
def test_rows_after_made_at_do_not_change_result(name):
    rng = np.random.default_rng(3)
    full = series(100 + np.cumsum(rng.normal(size=60)), end="2025-06-30")
    made_at = full.index[39]
    past = full[full.index <= made_at]
    kwargs = {"base_rate": 0.55}
    assert baseline_prob(name, "AAA", full, made_at, **kwargs) == baseline_prob(name, "AAA", past, made_at, **kwargs)
    assert baseline_inputs(name, "AAA", full, made_at, **kwargs) == baseline_inputs(name, "AAA", past, made_at, **kwargs)
    # gelecegi uc degerlerle bozmak da sonucu degistirmemeli
    spoiled = full.copy()
    spoiled[spoiled.index > made_at] = [1e9 if i % 2 else 1e-3 for i in range(int((spoiled.index > made_at).sum()))]
    assert baseline_prob(name, "AAA", spoiled, made_at, **kwargs) == baseline_prob(name, "AAA", past, made_at, **kwargs)


def test_baseline_inputs_are_json_and_reflect_decision():
    s = series([10.0] * 19 + [10.5])
    d = s.index[-1]
    for name in BASELINES:
        json.dumps(baseline_inputs(name, "A", s, d, base_rate=0.5), allow_nan=False)
    inp = baseline_inputs("ma20", "A", s, d)
    assert inp["close"] == 10.5 and inp["ma20"] == pytest.approx((19 * 10 + 10.5) / 20)
    y = baseline_inputs("yesterday", "A", s, d)
    assert y["ret_1d"] == pytest.approx(0.05) and y["prev_close"] == 10.0
    assert baseline_inputs("ma20", "A", s.iloc[:5], s.index[4])["status"] == "insufficient_data"
    assert baseline_inputs("base_rate", "A", s, d)["status"] == "insufficient_data"
    assert baseline_inputs("base_rate", "A", s, d, base_rate=0.5)["base_rate"] == 0.5
    assert baseline_inputs("random", "A", s, d, seed=3) == {"rule": "random", "seed": 3, "ticker": "A", "date": str(d.date())}


# --- historical_base_rate ---

@pytest.fixture
def cal():
    return TradingCalendar(pd.bdate_range("2025-01-01", "2025-12-31"))


def hist(cal, ups_pattern, start_close=100.0):
    """Her gun icin kapanis: ups_pattern(i) True ise +1% degilse -1% (gunluk)."""
    closes, c = [], start_close
    for i in range(len(cal)):
        c *= 1.01 if ups_pattern(i) else 0.99
        closes.append(c)
    return pd.DataFrame({"close_adj": closes}, index=cal.days)


def test_base_rate_known_synthetic(cal):
    up = hist(cal, lambda i: True)
    down = hist(cal, lambda i: False)
    made_at = cal.days[200]
    assert historical_base_rate({"U": up}, made_at, cal, 5) == 1.0
    assert historical_base_rate({"D": down}, made_at, cal, 5) == 0.0
    assert historical_base_rate({"U": up, "D": down}, made_at, cal, 5) == 0.5
    # 3 yukari, 1 asagi hisse -> 0.75
    assert historical_base_rate({"U1": up, "U2": up, "U3": up, "D": down}, made_at, cal, 5) == 0.75


def test_base_rate_excludes_unknown_labels_and_window(cal):
    H = 5
    made_at = cal.days[100]
    # etiketi made_at'te bilinen son ornek s = days[100-H]. Onun ucundan sonraki gunlerde fiyat dusuk -> o ornekler
    # (etiketi bilinmeyen) dahil edilseydi oran degisirdi. Son bilinen ornek (s=days[95]) disinda hepsi 'yukari'.
    closes = np.cumprod(np.full(len(cal), 1.01))
    closes[96:] = closes[95] * 0.5  # s=95'in etiketi (95->100) asagi; s>=96'nin etiketi bilinmiyor
    df = pd.DataFrame({"close_adj": closes}, index=cal.days)
    # s=91..95 icin etiket: 91->96 asagi (0.5*c95 < c91? c95*0.5 < c91 evet), ... hepsi asagi; pencere = son 5 ornek
    r5 = historical_base_rate({"X": df}, made_at, cal, H, window_days=5, min_samples=1)
    assert r5 == 0.0
    # pencere 100: ilk (yukselen) ornekler de girer, 0 < oran < 1
    r100 = historical_base_rate({"X": df}, made_at, cal, H, window_days=100, min_samples=1)
    assert 0.0 < r100 < 1.0
    # ornek sayisi: made_at=days[100], H=5 -> uygun s sayisi 96, hepsi pencere icinde (i=0..95)
    n_ok = sum(
        1 for s in cal.days if cal.add_trading_days(s, H) is not None and cal.add_trading_days(s, H) <= made_at
    )
    assert n_ok == 96
    r = historical_base_rate({"X": df}, made_at, cal, H, window_days=1000, min_samples=n_ok)
    assert r is not None
    assert historical_base_rate({"X": df}, made_at, cal, H, window_days=1000, min_samples=n_ok + 1) is None


def test_base_rate_no_leak_from_future_rows(cal):
    made_at = cal.days[150]
    rng = np.random.default_rng(5)
    base = pd.DataFrame({"close_adj": 100 * np.cumprod(1 + rng.normal(0, 0.02, len(cal)))}, index=cal.days)
    truncated = base[base.index <= made_at]
    expected = historical_base_rate({"X": truncated}, made_at, cal, 5)
    assert expected is not None
    assert historical_base_rate({"X": base}, made_at, cal, 5) == expected
    spoiled = base.copy()
    spoiled.loc[spoiled.index > made_at, "close_adj"] = 1e9
    assert historical_base_rate({"X": spoiled}, made_at, cal, 5) == expected
    # takvim de kesilse sonuc ayni (takvimin gelecegi sonucu etkilemez)
    assert historical_base_rate({"X": base}, made_at, TradingCalendar(cal.days[cal.days <= made_at]), 5) == expected
    # made_at'in hemen sonrasindaki barlari bozmak etiketi bilinmeyen ornekleri kullanmadigimizi kanitlar:
    spoiled2 = base.copy()
    spoiled2.loc[spoiled2.index == cal.days[151], "close_adj"] = 1e-6
    assert historical_base_rate({"X": spoiled2}, made_at, cal, 5) == expected


def test_base_rate_min_samples_and_edges(cal):
    up = hist(cal, lambda i: True)
    assert historical_base_rate({"U": up}, cal.days[20], cal, 5, min_samples=30) is None  # 16 ornek
    assert historical_base_rate({"U": up}, cal.days[3], cal, 5) is None  # H'den kisa takvim
    assert historical_base_rate({}, cal.days[200], cal, 5) is None
    # NaN kapanislar ornek sayilmaz
    df = up.copy()
    df.iloc[100:110, 0] = np.nan
    full = historical_base_rate({"U": up}, cal.days[200], cal, 5, window_days=1000, min_samples=1)
    assert full == 1.0
    assert historical_base_rate({"U": df}, cal.days[200], cal, 5, window_days=1000, min_samples=1) == 1.0
    # esik: label_threshold=0.2 ile 5 gunluk ~%5 getiri yukari sayilmaz
    assert historical_base_rate({"U": up}, cal.days[200], cal, 5, label_threshold=0.2) == 0.0


def test_base_rate_ignores_samples_whose_label_is_unknown_at_made_at(cal):
    H = 5
    made_at = cal.days[100]
    closes = np.cumprod(np.full(len(cal), 1.01))
    closes[101:] = 1.0  # made_at sonrasi cokus: bilinmeyen etiketler (s=96..) asagi olurdu
    df = pd.DataFrame({"close_adj": closes}, index=cal.days)
    # bilinen etiketler (s<=95, bitis<=100) hep yukari; s=96..100'un bitisi 101+ -> kullanilirsa oran < 1 olurdu
    assert historical_base_rate({"X": df}, made_at, cal, H, window_days=250, min_samples=1) == 1.0
    assert historical_base_rate({"X": df}, made_at, cal, H, window_days=3, min_samples=1) == 1.0
