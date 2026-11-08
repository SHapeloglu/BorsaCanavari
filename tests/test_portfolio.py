"""Kohort portfoy simulasyonu testleri (PROJE.md Bolum 13). Elle hesaplanmis kucuk ornekler."""

import numpy as np
import pandas as pd
import pytest

from bist_tahmin.evaluate.portfolio import (
    PortfolioResult,
    equity_curve,
    max_drawdown,
    relative_drawdown,
    select_top,
    simulate_cohort_portfolio,
)


def _day(date, scores, rets, prefix="T"):
    return pd.DataFrame({
        "made_at": pd.Timestamp(date),
        "ticker": [f"{prefix}{i}" for i in range(len(scores))],
        "score": scores,
        "tradable_ret": rets,
    })


def _result(net_rets, bench_rets=None):
    idx = pd.date_range("2024-01-01", periods=len(net_rets), freq="B", name="made_at")
    bench = bench_rets if bench_rets is not None else [0.0] * len(net_rets)
    return PortfolioResult(
        cohorts=pd.DataFrame({"gross_ret": net_rets, "net_ret": net_rets, "benchmark_ret": bench}, index=idx),
        summary={},
    )


# ------------------------------------------------------------ secim kurali

def test_select_top_ceil():
    d = pd.DataFrame({"score": [0.1, 0.9, 0.5, 0.7, 0.3, 0.2, 0.8, 0.4, 0.6, 0.0]})
    # n=10, 0.25 -> ceil(2.5) = 3: en yuksek uc skor 0.9, 0.8, 0.7
    assert set(select_top(d, 0.25)) == {1, 6, 3}
    # n=10, 0.2 -> tam 2
    assert set(select_top(d, 0.2)) == {1, 6}
    # cok kucuk oran en az 1 secer
    assert set(select_top(d, 0.01)) == {1}
    # 1.0 hepsini secer
    assert len(select_top(d, 1.0)) == 10


def test_select_top_float_artifact_does_not_round_up():
    # 0.1 * 30 = 3.0000000000000004; ceil naif olarak 4 verirdi
    d = pd.DataFrame({"score": np.arange(30, dtype=float)})
    assert len(select_top(d, 0.1)) == 3
    assert len(select_top(d, 0.2)) == 6


def test_select_top_includes_all_ties_at_threshold():
    d = pd.DataFrame({"score": [0.6, 0.6, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4]})
    # k=1 ama esik 0.6'da iki esit skor: ikisi de secilir
    assert set(select_top(d, 0.1)) == {0, 1}
    # k=2: tam iki
    assert set(select_top(d, 0.2)) == {0, 1}
    # k=3: esik 0.4 oldugu icin 0.4'lerin hepsi dahil -> 10 hisse
    assert len(select_top(d, 0.3)) == 10


def test_select_top_ignores_nan_scores_and_validates():
    d = pd.DataFrame({"score": [np.nan, 3.0, 1.0, 2.0, np.nan]})
    assert set(select_top(d, 0.34)) == {1, 3}  # n=3 gecerli, ceil(1.02)=2
    with pytest.raises(ValueError):
        select_top(d, 0.0)
    with pytest.raises(ValueError):
        select_top(d, 1.5)
    with pytest.raises(ValueError):
        select_top(pd.DataFrame({"x": [1.0]}), 0.2)


# ------------------------------------------------------------ simulasyon

def test_hand_computed_single_day():
    df = _day("2024-03-01", [5, 4, 3, 2, 1], [0.10, 0.02, 0.0, -0.02, -0.04])
    r = simulate_cohort_portfolio(df, top_frac=0.2, cost_bps=20.0)
    c = r.cohorts.iloc[0]
    assert c["n_universe"] == 5 and c["n_selected"] == 1
    assert c["gross_ret"] == pytest.approx(0.10)
    assert c["benchmark_ret"] == pytest.approx(0.012)
    assert c["cost"] == pytest.approx(0.004)            # 2 * 20 / 1e4
    assert c["net_ret"] == pytest.approx(0.096)
    assert c["excess_gross"] == pytest.approx(0.088)
    assert c["excess_net"] == pytest.approx(0.084)
    s = r.summary
    assert s["n_cohorts"] == 1 and s["cost_bps"] == 20.0
    assert s["mean_gross"] == pytest.approx(0.10)
    assert s["mean_net"] == pytest.approx(0.096)
    assert s["mean_benchmark"] == pytest.approx(0.012)
    assert s["mean_excess_net"] == pytest.approx(0.084)
    assert s["hit_rate_vs_benchmark"] == 1.0
    assert s["selected_frac_mean"] == pytest.approx(0.2)
    assert s["skipped_days"] == 0 and s["dropped_nan_rows"] == 0
    assert list(r.cohorts.columns) == [
        "n_universe", "n_selected", "gross_ret", "benchmark_ret",
        "cost", "net_ret", "excess_gross", "excess_net",
    ]
    assert r.cohorts.index.name == "made_at"


def test_multi_day_means_and_hit_rate():
    d1 = _day("2024-03-01", [5, 4, 3, 2, 1], [0.10, 0.0, 0.0, 0.0, 0.0])    # gross .10, bench .02
    d2 = _day("2024-03-04", [5, 4, 3, 2, 1], [-0.05, 0.0, 0.0, 0.0, 0.0])   # gross -.05, bench -.01
    r = simulate_cohort_portfolio(pd.concat([d1, d2]), cost_bps=0.0)
    assert r.summary["n_cohorts"] == 2
    assert r.summary["mean_gross"] == pytest.approx(0.025)
    assert r.summary["mean_benchmark"] == pytest.approx(0.005)
    assert r.summary["mean_excess_net"] == pytest.approx(0.02)
    assert r.summary["hit_rate_vs_benchmark"] == 0.5  # yalniz 1. gun benchmark'i gecti


def test_always_up_equals_benchmark():
    rng = np.random.default_rng(3)
    frames = [
        _day(d, np.full(12, 0.99), rng.normal(0.003, 0.03, 12))
        for d in pd.bdate_range("2024-01-01", periods=15)
    ]
    df = pd.concat(frames)
    r = simulate_cohort_portfolio(df, top_frac=0.2, cost_bps=0.0)
    assert (r.cohorts["n_selected"] == r.cohorts["n_universe"]).all()
    np.testing.assert_allclose(r.cohorts["gross_ret"], r.cohorts["benchmark_ret"], atol=1e-15)
    np.testing.assert_allclose(r.cohorts["excess_gross"], 0.0, atol=1e-15)
    np.testing.assert_allclose(r.cohorts["excess_net"], 0.0, atol=1e-15)
    assert r.summary["selected_frac_mean"] == 1.0
    assert r.summary["mean_excess_net"] == pytest.approx(0.0, abs=1e-15)
    # maliyetle: tum evreni alan strateji benchmark'in cost kadar altinda (muhafazakar)
    r10 = simulate_cohort_portfolio(df, top_frac=0.2, cost_bps=10.0)
    np.testing.assert_allclose(r10.cohorts["excess_net"], -0.002, atol=1e-15)


def test_two_valued_baseline_scores_select_whole_tied_group():
    # yedi 0.6, uc 0.4; top_frac 0.2 -> k=2 ama esik 0.6, yedisi de secilir
    rets = [0.04, 0.02, 0.0, 0.01, -0.01, 0.03, 0.05, -0.05, 0.0, 0.02]
    df = _day("2024-03-01", [0.6] * 7 + [0.4] * 3, rets)
    r = simulate_cohort_portfolio(df, top_frac=0.2)
    c = r.cohorts.iloc[0]
    assert c["n_selected"] == 7
    assert c["gross_ret"] == pytest.approx(np.mean(rets[:7]))


def test_cost_is_two_times_one_way_bps():
    df = _day("2024-03-01", [3, 2, 1, 0, -1], [0.05, 0.0, 0.0, 0.0, 0.0])
    for bps in (0.0, 5.0, 10.0, 25.0, 100.0):
        r = simulate_cohort_portfolio(df, cost_bps=bps)
        assert r.cohorts["cost"].iloc[0] == pytest.approx(2.0 * bps / 1e4)
        assert r.cohorts["net_ret"].iloc[0] == pytest.approx(0.05 - 2.0 * bps / 1e4)


def test_benchmark_is_cost_free():
    rng = np.random.default_rng(1)
    df = pd.concat([_day(d, rng.normal(size=8), rng.normal(0, 0.02, 8)) for d in pd.bdate_range("2024-01-01", periods=6)])
    r0 = simulate_cohort_portfolio(df, cost_bps=0.0)
    r1 = simulate_cohort_portfolio(df, cost_bps=50.0)
    pd.testing.assert_series_equal(r0.cohorts["benchmark_ret"], r1.cohorts["benchmark_ret"])
    pd.testing.assert_series_equal(r0.cohorts["gross_ret"], r1.cohorts["gross_ret"])
    assert r1.summary["mean_benchmark"] == r0.summary["mean_benchmark"]
    np.testing.assert_allclose(r1.cohorts["excess_net"], r0.cohorts["excess_net"] - 0.01, atol=1e-15)


def test_nan_rows_dropped_and_counted():
    # 6 satir: T1 skoru NaN (evrenden cikar), T4 getirisi NaN (evrenden cikar) -> evren 4 hisse
    df = _day("2024-03-01", [6.0, np.nan, 4.0, 3.0, 2.0, 1.0], [0.08, 0.50, 0.0, 0.0, np.nan, -0.04])
    r = simulate_cohort_portfolio(df, top_frac=0.25, min_universe=4)
    assert r.summary["dropped_nan_rows"] == 2
    assert r.summary["n_input_rows"] == 6
    c = r.cohorts.iloc[0]
    assert c["n_universe"] == 4
    assert c["gross_ret"] == pytest.approx(0.08)       # k=ceil(1.0)=1 -> T0
    # benchmark yalniz temiz evrenden: mean(0.08, 0, 0, -0.04) = 0.01; 0.50'lik satir DAHIL DEGIL
    assert c["benchmark_ret"] == pytest.approx(0.01)


def test_small_universe_days_skipped_and_counted():
    ok = _day("2024-03-01", [5, 4, 3, 2, 1], [0.1, 0, 0, 0, 0])
    small = _day("2024-03-04", [3, 2, 1], [0.1, 0.0, 0.0])               # 3 < 5
    all_nan = _day("2024-03-05", [np.nan] * 5, [0.1] * 5)                # tamami dusurulur
    after_nan = _day("2024-03-06", [5, 4, 3, 2, np.nan], [0.1, 0, 0, 0, 0])  # 4 kalir < 5
    r = simulate_cohort_portfolio(pd.concat([ok, small, all_nan, after_nan]))
    assert list(r.cohorts.index) == [pd.Timestamp("2024-03-01")]
    assert r.summary["n_cohorts"] == 1
    assert r.summary["skipped_days"] == 3
    assert r.summary["n_days_input"] == 4
    assert r.summary["dropped_nan_rows"] == 6
    # min_universe dusurulunce kucuk gun girer
    r2 = simulate_cohort_portfolio(pd.concat([ok, small]), min_universe=3)
    assert r2.summary["n_cohorts"] == 2 and r2.summary["skipped_days"] == 0


def test_empty_and_all_skipped_input():
    empty = pd.DataFrame({"made_at": [], "ticker": [], "score": [], "tradable_ret": []})
    r = simulate_cohort_portfolio(empty)
    assert len(r.cohorts) == 0 and r.summary["n_cohorts"] == 0
    assert np.isnan(r.summary["mean_net"]) and np.isnan(r.summary["hit_rate_vs_benchmark"])
    assert list(r.cohorts.columns)[0] == "n_universe"
    small = _day("2024-03-04", [3, 2, 1], [0.1, 0.0, 0.0])
    r = simulate_cohort_portfolio(small)
    assert r.summary["n_cohorts"] == 0 and r.summary["skipped_days"] == 1
    assert len(equity_curve(r, 5)) == 0
    assert np.isnan(max_drawdown(equity_curve(r, 5)))


def test_row_order_and_dtypes_do_not_matter():
    rng = np.random.default_rng(7)
    df = pd.concat([_day(d, rng.normal(size=9), rng.normal(0, 0.02, 9)) for d in pd.bdate_range("2024-01-01", periods=8)])
    shuffled = df.sample(frac=1.0, random_state=0).reset_index(drop=True)
    a = simulate_cohort_portfolio(df, cost_bps=10.0)
    b = simulate_cohort_portfolio(shuffled, cost_bps=10.0)
    pd.testing.assert_frame_equal(a.cohorts, b.cohorts)
    # python date nesneleri de kabul edilir
    d2 = df.copy()
    d2["made_at"] = d2["made_at"].dt.date
    pd.testing.assert_frame_equal(simulate_cohort_portfolio(d2, cost_bps=10.0).cohorts, a.cohorts)


# ------------------------------------------------------------ girdi dogrulama / saflik

def test_input_validation():
    df = _day("2024-03-01", [5, 4, 3, 2, 1], [0.1, 0, 0, 0, 0])
    with pytest.raises(ValueError, match="eksik sutun"):
        simulate_cohort_portfolio(df.drop(columns=["score"]))
    with pytest.raises(ValueError, match="eksik sutun"):
        simulate_cohort_portfolio(df.drop(columns=["tradable_ret"]))
    with pytest.raises(ValueError):
        simulate_cohort_portfolio([1, 2, 3])
    for bad in (0.0, -0.1, 1.01, float("nan")):
        with pytest.raises(ValueError):
            simulate_cohort_portfolio(df, top_frac=bad)
    for bad in (-1.0, float("nan"), float("inf"), "x"):
        with pytest.raises(ValueError):
            simulate_cohort_portfolio(df, cost_bps=bad)
    for bad in (0, -1, 2.5):
        with pytest.raises(ValueError):
            simulate_cohort_portfolio(df, min_universe=bad)
    dup = pd.concat([df, df.iloc[[0]]])
    with pytest.raises(ValueError, match="birden fazla"):
        simulate_cohort_portfolio(dup)
    inf = df.copy()
    inf.loc[0, "tradable_ret"] = np.inf
    with pytest.raises(ValueError, match="sonsuz"):
        simulate_cohort_portfolio(inf)
    nat = df.copy()
    nat.loc[0, "made_at"] = pd.NaT
    with pytest.raises(ValueError, match="made_at"):
        simulate_cohort_portfolio(nat)


def test_does_not_mutate_input_and_is_deterministic():
    rng = np.random.default_rng(11)
    df = pd.concat([_day(d, rng.normal(size=7), rng.normal(0, 0.02, 7)) for d in pd.bdate_range("2024-01-01", periods=5)])
    df.loc[3, "score"] = np.nan
    df["extra"] = 1
    snap = df.copy(deep=True)
    a = simulate_cohort_portfolio(df, cost_bps=15.0)
    pd.testing.assert_frame_equal(df, snap)
    b = simulate_cohort_portfolio(df, cost_bps=15.0)
    pd.testing.assert_frame_equal(a.cohorts, b.cohorts)
    assert a.summary == pytest.approx(b.summary, nan_ok=True)


# ------------------------------------------------------------ ozkaynak / drawdown

def test_equity_curve_known_series():
    # H=5: adimlar 0.05/5, -0.10/5, 0.05/5 = +0.01, -0.02, +0.01
    eq = equity_curve(_result([0.05, -0.10, 0.05]), 5)
    np.testing.assert_allclose(eq.to_numpy(), [1.01, 0.99, 1.00])
    assert len(eq) == 3 and eq.index.name == "made_at"
    # kohortlar cift sayilmaz: sabit %5 getiri, H=5 -> 5 kohort sonunda toplam +%5
    eq5 = equity_curve(_result([0.05] * 5), 5)
    assert eq5.iloc[-1] == pytest.approx(1.05)


def test_equity_curve_kinds_and_sorting():
    res = _result([0.05, 0.0], bench_rets=[0.01, 0.02])
    res.cohorts = res.cohorts.iloc[::-1]  # ters sirali girdi
    eq = equity_curve(res, 1, kind="benchmark")
    np.testing.assert_allclose(eq.to_numpy(), [1.01, 1.03])
    assert eq.index.is_monotonic_increasing
    with pytest.raises(ValueError):
        equity_curve(res, 1, kind="nope")
    with pytest.raises(ValueError):
        equity_curve(res, 0)
    with pytest.raises(ValueError):
        equity_curve("x", 5)


def test_equity_from_simulation_matches_manual():
    d = pd.bdate_range("2024-03-01", periods=3)
    df = pd.concat([
        _day(d[0], [5, 4, 3, 2, 1], [0.10, 0, 0, 0, 0]),
        _day(d[1], [5, 4, 3, 2, 1], [-0.10, 0, 0, 0, 0]),
        _day(d[2], [5, 4, 3, 2, 1], [0.05, 0, 0, 0, 0]),
    ])
    r = simulate_cohort_portfolio(df, cost_bps=0.0)
    np.testing.assert_allclose(equity_curve(r, 5).to_numpy(), [1.02, 1.00, 1.01])
    np.testing.assert_allclose(equity_curve(r, 5, kind="benchmark").to_numpy(), [1.004, 1.000, 1.002])


def test_max_drawdown_known_series():
    # zirve 1.2 -> dip 0.9: %25; sonra 1.5 -> 1.2: %20
    assert max_drawdown([1.0, 1.2, 0.9, 1.5, 1.2]) == pytest.approx(0.25)
    assert max_drawdown([1.0, 1.1, 1.2, 1.3]) == 0.0
    assert max_drawdown(pd.Series([100.0, 120.0, 90.0])) == pytest.approx(0.25)
    # baslangic degeri: ilk eleman zaten dusuk -> varsayilan initial=1.0 kaybi sayar
    assert max_drawdown([0.9, 1.1]) == pytest.approx(0.1)
    assert max_drawdown([0.9, 1.1], initial=None) == 0.0
    assert np.isnan(max_drawdown(pd.Series([], dtype=float)))
    for bad in ([1.0, np.nan], [1.0, 0.0], [1.0, -1.0], [1.0, np.inf]):
        with pytest.raises(ValueError):
            max_drawdown(bad)


def test_relative_drawdown():
    idx = pd.date_range("2024-01-01", periods=4)
    model = pd.Series([1.0, 1.1, 1.1, 1.1], index=idx)
    bench = pd.Series([1.0, 1.0, 1.1, 1.2], index=idx)
    # oran: 1, 1.1, 1.0, 0.91667 ; zirve 1.1 ; dip 0.91667 -> 1 - 0.83333 = 0.16667
    assert relative_drawdown(model, bench) == pytest.approx(1.0 - (1.1 / 1.2) / 1.1)
    assert relative_drawdown(model, bench) == pytest.approx(1.0 / 6.0)
    # model her zaman benchmark'in uzerinde buyuyorsa goreli dusus 0
    assert relative_drawdown(bench * 1.0, bench) == 0.0
    # benchmark dusuyor, model sabit -> goreli dusus yok (mutlak degil goreli)
    assert relative_drawdown(pd.Series([1.0, 1.0, 1.0], index=idx[:3]), pd.Series([1.0, 0.8, 0.6], index=idx[:3])) == 0.0
    # model dusuyor, benchmark sabit -> %20
    assert relative_drawdown([1.0, 0.9, 0.8], [1.0, 1.0, 1.0]) == pytest.approx(0.2)
    assert np.isnan(relative_drawdown(pd.Series([], dtype=float), pd.Series([], dtype=float)))


def test_relative_drawdown_validation():
    idx = pd.date_range("2024-01-01", periods=3)
    with pytest.raises(ValueError, match="uzunluk"):
        relative_drawdown([1.0, 1.0], [1.0, 1.0, 1.0])
    with pytest.raises(ValueError, match="index"):
        relative_drawdown(pd.Series([1, 1, 1.0], index=idx), pd.Series([1, 1, 1.0], index=idx + pd.Timedelta(days=1)))
    with pytest.raises(ValueError):
        relative_drawdown([1.0, 0.0, 1.0], [1.0, 1.0, 1.0])


def test_cost_sensitivity_reexported_from_portfolio():
    from bist_tahmin.evaluate import costs, portfolio

    assert portfolio.cost_sensitivity is costs.cost_sensitivity
    with pytest.raises(AttributeError):
        portfolio.does_not_exist
