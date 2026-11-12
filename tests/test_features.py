"""Ozellik uretimi testleri: saf pandas, ag/DB yok (PROJE.md Bolum 9, 13)."""

from __future__ import annotations

import json
import math
import statistics

import numpy as np
import pandas as pd
import pytest

from bist_tahmin.calendar import TradingCalendar
from bist_tahmin.features import build as fb
from bist_tahmin.features.build import (
    FEATURE_NAMES,
    FEATURE_VERSION,
    STOCK_FEATURES,
    build_features,
    make_feature_row,
)


# --- sentetik veri -------------------------------------------------------------------------------------------

def make_frame(close_adj, dates, *, close=None, volume=None) -> pd.DataFrame:
    close_adj = np.asarray(close_adj, dtype=float)
    close = close_adj if close is None else np.asarray(close, dtype=float)
    volume = np.full(len(close_adj), 1000.0) if volume is None else np.asarray(volume, dtype=float)
    df = pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "close_adj": close_adj,
         "open_adj": close_adj, "volume": volume},
        index=pd.DatetimeIndex(dates, name="date"),
    )
    return df


def random_walk(rng: np.random.Generator, n: int, start: float = 100.0, vol: float = 0.015) -> np.ndarray:
    return start * np.exp(np.cumsum(rng.normal(0.0003, vol, n)))


def make_world(tickers=("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III", "JJJ"), n=150, seed=0):
    """(histories, xu100, calendar); takvim = XU100 gunleri = hafta ici gunler."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-01", periods=n)
    xu = make_frame(random_walk(rng, n, 9000.0, 0.01), dates, volume=np.full(n, 5e6))
    hist = {t: make_frame(random_walk(rng, n), dates, volume=rng.uniform(500, 1500, n)) for t in tickers}
    return hist, xu, TradingCalendar(dates)


def at(cal: TradingCalendar, offset_from_end: int = 0) -> pd.Timestamp:
    return cal.days[-1 - offset_from_end]


# --- el ile hesaplanmis degerler -----------------------------------------------------------------------------

def linear_world(n=100):
    dates = pd.bdate_range("2024-01-01", periods=n)
    a = 100.0 + np.arange(n)            # A: dogrusal artis
    b = 50.0 + 0.5 * np.arange(n)       # B: daha yavas dogrusal artis
    xu = 1000.0 + 10.0 * np.arange(n)
    hist = {"A": make_frame(a, dates), "B": make_frame(b, dates)}
    return hist, make_frame(xu, dates), TradingCalendar(dates), a, b, xu


@pytest.mark.fast
def test_hand_computed_values_linear_series():
    hist, xu, cal, a, b, xs = linear_world()
    m = cal.days[-1]
    fs = build_features(hist, xu, m, cal)
    assert fs.excluded == {} and set(fs.rows) == {"A", "B"}
    f = fs.rows["A"].features
    p = a[-1]  # 199
    assert f["ret_1"] == pytest.approx(p / a[-2] - 1, rel=1e-9)
    assert f["ret_5"] == pytest.approx(p / a[-6] - 1, rel=1e-9)
    assert f["ret_10"] == pytest.approx(p / a[-11] - 1, rel=1e-9)
    assert f["ret_20"] == pytest.approx(p / a[-21] - 1, rel=1e-9)
    # dogrusal seride MA(k) = p - (k-1)/2  ->  ma_k = p/(p-(k-1)/2) - 1
    assert f["ma5"] == pytest.approx(p / (p - 2.0) - 1, rel=1e-9)
    assert f["ma20"] == pytest.approx(p / (p - 9.5) - 1, rel=1e-9)
    assert f["ma50"] == pytest.approx(p / (p - 24.5) - 1, rel=1e-9)
    for k, name in ((10, "vol_10"), (20, "vol_20")):
        rets = [a[i] / a[i - 1] - 1 for i in range(len(a) - k, len(a))]
        assert f[name] == pytest.approx(statistics.stdev(rets), rel=1e-9)  # ddof=1
    assert f["rsi_14"] == 100.0  # hic dusus yok
    assert f["volume_ratio"] == pytest.approx(1.0)
    # goreli guc ve piyasa
    assert f["mkt_ret_1"] == pytest.approx(xs[-1] / xs[-2] - 1, rel=1e-9)
    assert f["mkt_ret_5"] == pytest.approx(xs[-1] / xs[-6] - 1, rel=1e-9)
    assert f["mkt_ret_20"] == pytest.approx(xs[-1] / xs[-21] - 1, rel=1e-9)
    assert f["mkt_ma20"] == pytest.approx(xs[-1] / (xs[-1] - 95.0) - 1, rel=1e-9)
    assert f["mkt_vol_20"] == pytest.approx(
        statistics.stdev([xs[i] / xs[i - 1] - 1 for i in range(len(xs) - 20, len(xs))]), rel=1e-9)
    assert f["rs_5"] == pytest.approx((p / a[-6] - 1) - (xs[-1] / xs[-6] - 1), rel=1e-9)
    assert f["rs_20"] == pytest.approx((p / a[-21] - 1) - (xs[-1] / xs[-21] - 1), rel=1e-9)
    # iki hisse de MA20 ustunde -> genislik 1
    assert f["breadth"] == 1.0
    assert f["weekday"] == float(m.weekday())
    assert f["post_holiday"] == 0.0


@pytest.mark.fast
def test_rsi_wilder_textbook_values():
    # StockCharts "RSI" ornek verisi (Wilder yumusatmasi): ilk RSI 70.53, ikincisi 66.32.
    closes = [44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245, 45.8433, 46.0826,
              45.8931, 46.0328, 45.6140, 46.2820, 46.2820, 46.0028, 46.0328, 46.4116, 46.2222, 45.6439]
    assert fb._wilder_rsi(np.array(closes[:15])) == pytest.approx(70.53, abs=0.01)
    assert fb._wilder_rsi(np.array(closes[:16])) == pytest.approx(66.32, abs=0.01)


@pytest.mark.fast
def test_rsi_matches_independent_reference_through_build():
    hist, xu, cal = make_world(("AAA", "BBB"), n=120, seed=3)
    fs = build_features(hist, xu, cal.days[-1], cal)
    c = hist["AAA"]["close_adj"].tolist()
    d = [c[i] - c[i - 1] for i in range(1, len(c))]
    g, l_ = [max(x, 0) for x in d], [max(-x, 0) for x in d]
    ag, al = sum(g[:14]) / 14, sum(l_[:14]) / 14
    for i in range(14, len(d)):
        ag, al = (ag * 13 + g[i]) / 14, (al * 13 + l_[i]) / 14
    assert fs.rows["AAA"].features["rsi_14"] == pytest.approx(100 - 100 / (1 + ag / al), rel=1e-8)


@pytest.mark.fast
def test_rsi_flat_series_is_undefined_and_excluded():
    assert math.isnan(fb._wilder_rsi(np.full(30, 10.0)))
    hist, xu, cal = make_world(("AAA", "BBB"), n=100)
    hist["BBB"] = make_frame(np.full(100, 50.0), hist["BBB"].index)
    fs = build_features(hist, xu, cal.days[-1], cal)
    assert fs.excluded == {"BBB": "nan_feature:rsi_14"}
    assert "AAA" in fs.rows


@pytest.mark.fast
def test_breadth_counts_stocks_above_ma20():
    n = 100
    dates = pd.bdate_range("2024-01-01", periods=n)
    up = 100.0 + np.arange(n)
    down = 300.0 - np.arange(n)
    hist = {"U1": make_frame(up, dates), "U2": make_frame(up * 2, dates),
            "U3": make_frame(up * 3, dates), "D1": make_frame(down, dates)}
    xu = make_frame(1000.0 + np.arange(n), dates)
    fs = build_features(hist, xu, dates[-1], TradingCalendar(dates))
    assert fs.rows["U1"].features["breadth"] == pytest.approx(0.75)
    assert len({r.features["breadth"] for r in fs.rows.values()}) == 1  # herkes icin ayni


# --- z-skor --------------------------------------------------------------------------------------------------

@pytest.mark.fast
def test_zscores_mean_zero_std_one_and_cover_all_stock_features():
    hist, xu, cal = make_world(n=150, seed=1)  # 10 hisse: |z| <= 9/sqrt(10) < 3, kirpma devreye girmez
    fs = build_features(hist, xu, cal.days[-1], cal)
    assert len(fs.rows) == 10
    for name in STOCK_FEATURES:
        z = np.array([r.features[f"{name}_z"] for r in fs.rows.values()])
        assert z.mean() == pytest.approx(0.0, abs=1e-8)
        assert z.std(ddof=0) == pytest.approx(1.0, abs=1e-8)
        raw = np.array([r.features[name] for r in fs.rows.values()])
        # z ham degerin tekduze artan donusumu: siralama korunur
        assert list(np.argsort(raw)) == list(np.argsort(z))


@pytest.mark.fast
def test_zscore_depends_only_on_that_days_cross_section():
    hist, xu, cal = make_world(n=400, seed=2)
    base = build_features(hist, xu, cal.days[-1], cal)
    # Tum hisse ve XU100 icin cok eski (hicbir pencerenin/RSI hafizasinin ulasmadigi) bir gunu boz.
    hist2 = {t: df.copy() for t, df in hist.items()}
    xu2 = xu.copy()
    old = hist2["AAA"].index[-300]
    for df in [*hist2.values(), xu2]:
        df.loc[old, ["close", "close_adj"]] = df.loc[old, "close_adj"] * 3.0
    again = build_features(hist2, xu2, cal.days[-1], cal)
    assert {t: r.feature_hash for t, r in base.rows.items()} == {t: r.feature_hash for t, r in again.rows.items()}
    # z, o gunun ham degerlerinin kesitinden yeniden uretilir
    for name in STOCK_FEATURES:
        x = np.array([base.rows[t].features[name] for t in sorted(base.rows)])
        z = (x - x.mean()) / x.std(ddof=0)
        got = np.array([base.rows[t].features[f"{name}_z"] for t in sorted(base.rows)])
        assert got == pytest.approx(np.clip(z, -3, 3), abs=1e-8)


@pytest.mark.fast
def test_zscore_clip_and_constant_cross_section():
    z = fb._z_scores({"a": 0.0, **{f"x{i}": 1.0 for i in range(30)}})  # tek aykiri -> |z| > 3 -> kirpilir
    assert z["a"] == -3.0 and max(z.values()) <= 3.0
    assert fb._z_scores({"a": 0.1, "b": 0.1, "c": 0.1}) == {"a": 0.0, "b": 0.0, "c": 0.0}
    assert fb._z_scores({"a": 0.123456789}) == {"a": 0.0}  # tek hisse: std 0
    # kayan nokta gurultusu "sabit" sayilir
    z = fb._z_scores({"a": 0.1, "b": 0.1 + 1e-17, "c": 0.1 - 1e-17})
    assert all(v == 0.0 for v in z.values())


# --- sizinti / hash ------------------------------------------------------------------------------------------

@pytest.mark.fast
def test_rows_after_made_at_do_not_change_features_or_hash():
    hist, xu, cal = make_world(n=160, seed=4)
    m = cal.days[-31]
    full = build_features(hist, xu, m, cal)
    cut_h = {t: df.loc[df.index <= m] for t, df in hist.items()}
    cut = build_features(cut_h, xu.loc[xu.index <= m], m, TradingCalendar(cal.days[cal.days <= m]))
    assert full.rows and {t: (r.features, r.feature_hash) for t, r in full.rows.items()} == \
        {t: (r.features, r.feature_hash) for t, r in cut.rows.items()}
    assert full.excluded == cut.excluded
    # gelecek satirlari bozmak da sonucu degistirmez
    wild_h = {t: df.copy() for t, df in hist.items()}
    for df in wild_h.values():
        df.loc[df.index > m, ["close", "close_adj", "volume"]] = [1e-3, 1e-3, 9e9]
    wild = build_features(wild_h, xu, m, cal)
    assert {t: r.feature_hash for t, r in wild.rows.items()} == {t: r.feature_hash for t, r in full.rows.items()}


@pytest.mark.fast
def test_hash_repeatable_and_independent_of_ticker_and_row_order():
    hist, xu, cal = make_world(n=120, seed=5)
    m = cal.days[-1]
    a = build_features(hist, xu, m, cal)
    b = build_features(hist, xu, m, cal)
    rev = dict(reversed(list(hist.items())))
    shuffled = {t: df.sample(frac=1.0, random_state=7) for t, df in rev.items()}
    c = build_features(shuffled, xu.sample(frac=1.0, random_state=1), m, cal)
    ha = {t: r.feature_hash for t, r in a.rows.items()}
    assert ha == {t: r.feature_hash for t, r in b.rows.items()} == {t: r.feature_hash for t, r in c.rows.items()}
    assert list(a.rows) == sorted(a.rows)  # deterministik sira
    assert len(set(ha.values())) == len(ha)  # farkli hisse, farkli hash


@pytest.mark.fast
def test_inputs_are_not_mutated():
    hist, xu, cal = make_world(n=100, seed=6)
    snap = {t: df.copy() for t, df in hist.items()}
    xs = xu.copy()
    build_features(hist, xu, cal.days[-5], cal)
    for t in hist:
        pd.testing.assert_frame_equal(hist[t], snap[t])
    pd.testing.assert_frame_equal(xu, xs)


@pytest.mark.fast
def test_canonical_json_and_hash_format():
    row = make_feature_row("X", {"b": 1 / 3, "a": -0.0, "c": 1e-15, "d": 123456.78901234567})
    assert row.features == {"b": 0.3333333333, "a": 0.0, "c": 0.0, "d": 123456.789}
    assert row.features_json == '{"_v":"f1","a":0.0,"b":0.3333333333,"c":0.0,"d":123456.789}'
    import hashlib
    assert row.feature_hash == hashlib.sha256(row.features_json.encode()).hexdigest()
    assert FEATURE_VERSION == "f1"
    with pytest.raises(ValueError):
        make_feature_row("X", {"a": float("nan")})
    with pytest.raises(ValueError):
        make_feature_row("X", {"a": float("inf")})


@pytest.mark.fast
def test_row_json_matches_features_and_has_all_names():
    hist, xu, cal = make_world(n=120, seed=7)
    fs = build_features(hist, xu, cal.days[-1], cal)
    row = next(iter(fs.rows.values()))
    assert set(row.features) == set(FEATURE_NAMES) and len(FEATURE_NAMES) == 34
    assert "pre_holiday" not in row.features
    parsed = json.loads(row.features_json)
    assert parsed.pop("_v") == "f1" and parsed == row.features
    assert all(isinstance(v, float) and math.isfinite(v) for v in row.features.values())
    assert fs.made_at == cal.days[-1]


# --- bedelsiz ------------------------------------------------------------------------------------------------

@pytest.mark.fast
def test_bonus_issue_gives_no_fake_jump():
    n = 100
    dates = pd.bdate_range("2024-01-01", periods=n)
    rng = np.random.default_rng(11)
    true = random_walk(rng, n)  # ekonomik olarak surekli seri = close_adj
    k = n - 1                   # bolunme made_at gunu (son bar)
    raw_close = true.copy()
    raw_close[:k] *= 2.0        # %100 bedelsiz: bolunme oncesi ham fiyat 2x
    raw_vol = np.full(n, 1000.0)
    raw_vol[k:] = 2000.0        # ayni faaliyet, bolunme sonrasi 2x adet
    other = make_frame(random_walk(rng, n), dates)
    hist = {"SPL": make_frame(true, dates, close=raw_close, volume=raw_vol), "OTH": other}
    xu = make_frame(random_walk(rng, n, 9000.0), dates)
    fs = build_features(hist, xu, dates[-1], TradingCalendar(dates))
    f = fs.rows["SPL"].features
    assert f["ret_1"] == pytest.approx(true[-1] / true[-2] - 1, rel=1e-9)
    assert f["ret_1"] > -0.2  # ham close'a gore -%50 olurdu
    assert f["ret_5"] == pytest.approx(true[-1] / true[-6] - 1, rel=1e-9)
    # hacim duzeltmesi: ham oran 2000/1050 ~ 1.9 olurdu; duzeltmeli 1.0
    assert f["volume_ratio"] == pytest.approx(1.0)
    # ham close ile hesap sahte sicrama verirdi (test, karsi-olgunun gercekten fark yarattigini gosterir)
    assert raw_close[-1] / raw_close[-2] - 1 == pytest.approx(true[-1] / (2 * true[-2]) - 1)


# --- haric tutma ---------------------------------------------------------------------------------------------

@pytest.mark.fast
def test_exclusions_with_reasons():
    hist, xu, cal = make_world(("OK1", "OK2", "SHORT", "NOBAR", "NANCLOSE", "NEGCLOSE", "ZEROVOL", "GAP"),
                               n=120, seed=8)
    idx = hist["OK1"].index
    m = idx[-1]
    hist["SHORT"] = hist["SHORT"].iloc[-40:]                       # 40 bar < 60
    hist["NOBAR"] = hist["NOBAR"].iloc[:-1]                        # made_at satiri yok
    hist["NANCLOSE"].loc[m, "close_adj"] = np.nan
    hist["NEGCLOSE"].loc[m, "close_adj"] = -1.0
    hist["ZEROVOL"].loc[idx[-20:], "volume"] = 0.0                 # 20 gun sifir hacim -> ortalama 0
    hist["GAP"] = hist["GAP"].drop(index=idx[-4])                  # son pencerede eksik bar (doldurulmaz)
    fs = build_features(hist, xu, m, cal)
    assert fs.excluded == {
        "SHORT": "insufficient_history",
        "NOBAR": "no_bar_on_made_at",
        "NANCLOSE": "invalid_close_adj",
        "NEGCLOSE": "invalid_close_adj",
        "ZEROVOL": "nan_feature:volume_ratio",
        "GAP": "nan_feature:ma5",
    }
    assert set(fs.rows) == {"OK1", "OK2"}
    assert list(fs.excluded) == sorted(fs.excluded)


@pytest.mark.fast
def test_excluded_stock_does_not_affect_others_z():
    hist, xu, cal = make_world(("A", "B", "C", "D", "BAD"), n=120, seed=9)
    m = cal.days[-1]
    hist["BAD"].loc[m, "close_adj"] = np.nan
    with_bad = build_features(hist, xu, m, cal)
    without = build_features({t: df for t, df in hist.items() if t != "BAD"}, xu, m, cal)
    assert {t: r.feature_hash for t, r in with_bad.rows.items()} == {t: r.feature_hash for t, r in without.rows.items()}


@pytest.mark.fast
def test_min_history_days_parameter():
    hist, xu, cal = make_world(("A", "B"), n=100)
    hist["B"] = hist["B"].iloc[-30:]
    assert build_features(hist, xu, cal.days[-1], cal, min_history_days=60).excluded == {"B": "insufficient_history"}
    fs = build_features(hist, xu, cal.days[-1], cal, min_history_days=30)
    assert fs.excluded == {"B": "nan_feature:ma50"}  # 30 bar MA50'ye yetmez; uydurulmaz


@pytest.mark.fast
def test_made_at_not_a_trading_day_raises():
    n = 100
    dates = pd.bdate_range("2024-01-01", periods=n).delete(60)  # bir hafta ici tatil
    hist = {"A": make_frame(100 + np.arange(n - 1.0), dates)}
    xu = make_frame(1000 + np.arange(n - 1.0), dates)
    cal = TradingCalendar(dates)
    holiday = pd.bdate_range("2024-01-01", periods=n)[60]
    with pytest.raises(ValueError):
        build_features(hist, xu, holiday, cal)
    with pytest.raises(ValueError):
        build_features(hist, xu, pd.Timestamp("2024-02-10"), cal)  # cumartesi
    with pytest.raises(ValueError):
        build_features(hist, xu, dates[-1] + pd.Timedelta(days=1), cal)  # takvim disi
    with pytest.raises(ValueError):
        build_features(hist, xu, pd.Timestamp(dates[-1], tz="Europe/Istanbul"), cal)


@pytest.mark.fast
def test_input_contract_violations_raise():
    hist, xu, cal = make_world(("A",), n=100)
    m = cal.days[-1]
    dup = pd.concat([hist["A"], hist["A"].iloc[[-1]]])
    with pytest.raises(ValueError):
        build_features({"A": dup}, xu, m, cal)
    tz = hist["A"].copy()
    tz.index = tz.index.tz_localize("Europe/Istanbul")
    with pytest.raises(ValueError):
        build_features({"A": tz}, xu, m, cal)
    with pytest.raises(ValueError):
        build_features({"A": hist["A"].drop(columns="volume")}, xu, m, cal)
    with pytest.raises(ValueError):  # XU100'de made_at yok
        build_features(hist, xu.iloc[:-1], m, cal)


# --- takvim --------------------------------------------------------------------------------------------------

@pytest.mark.fast
def test_calendar_features_weekday_and_post_holiday():
    n = 130
    full = pd.bdate_range("2024-01-01", periods=n)
    thu_holiday = full[full.get_loc(pd.Timestamp("2024-03-14"))]
    dates = full.drop(thu_holiday)       # Persembe 14 Mart tatil
    hist = {t: make_frame(random_walk(np.random.default_rng(i), n - 1), dates) for i, t in enumerate("ABC")}
    xu = make_frame(random_walk(np.random.default_rng(99), n - 1, 9000.0), dates)
    cal = TradingCalendar(dates)

    def feats(day):
        fs = build_features(hist, xu, pd.Timestamp(day), cal, min_history_days=40)
        assert fs.rows, fs.excluded
        return next(iter(fs.rows.values())).features

    assert feats("2024-03-15")["post_holiday"] == 1.0 and feats("2024-03-15")["weekday"] == 4.0  # tatil sonrasi Cuma
    assert feats("2024-03-13")["post_holiday"] == 0.0                                            # tatil ONCESI gun
    assert feats("2024-03-18")["post_holiday"] == 0.0 and feats("2024-03-18")["weekday"] == 0.0  # hafta sonu degil tatil
    assert feats("2024-03-12")["weekday"] == 1.0
    # Cuma tatili -> Pazartesi tatil sonrasi (arada hafta ici gun var)
    fri = pd.Timestamp("2024-04-12")
    dates2 = full.drop(fri)
    cal2 = TradingCalendar(dates2)
    h2 = {t: make_frame(random_walk(np.random.default_rng(i), len(dates2)), dates2) for i, t in enumerate("ABC")}
    x2 = make_frame(random_walk(np.random.default_rng(99), len(dates2), 9000.0), dates2)
    fs = build_features(h2, x2, pd.Timestamp("2024-04-15"), cal2, min_history_days=40)
    assert next(iter(fs.rows.values())).features["post_holiday"] == 1.0
