"""Maliyet duyarlilik izgarasi testleri (PROJE.md Bolum 11)."""

import numpy as np
import pandas as pd
import pytest

from bist_tahmin.evaluate.costs import DEFAULT_GRID_BPS, SENSITIVITY_COLS, cost_sensitivity
from bist_tahmin.evaluate.portfolio import simulate_cohort_portfolio


def _panel(n_days=20, n_names=10, seed=5):
    rng = np.random.default_rng(seed)
    frames = []
    for d in pd.bdate_range("2024-01-01", periods=n_days):
        frames.append(pd.DataFrame({
            "made_at": d,
            "ticker": [f"T{i}" for i in range(n_names)],
            "score": rng.normal(size=n_names),
            "tradable_ret": rng.normal(0.002, 0.03, n_names),
        }))
    return pd.concat(frames, ignore_index=True)


@pytest.mark.fast
def test_columns_and_grid_order():
    out = cost_sensitivity(_panel(), [40, 0, 20, 10])
    assert list(out.columns) == list(SENSITIVITY_COLS)
    assert out["cost_bps"].tolist() == [40.0, 0.0, 20.0, 10.0]  # izgara sirasi korunur
    assert DEFAULT_GRID_BPS == (0.0, 10.0, 20.0, 40.0)
    assert len(cost_sensitivity(_panel(), DEFAULT_GRID_BPS)) == 4


@pytest.mark.fast
def test_matches_simulate_for_each_cost():
    df = _panel()
    out = cost_sensitivity(df, [0, 10, 20, 40], top_frac=0.3)
    for _, row in out.iterrows():
        s = simulate_cohort_portfolio(df, top_frac=0.3, cost_bps=row["cost_bps"]).summary
        assert row["mean_net"] == pytest.approx(s["mean_net"])
        assert row["mean_excess_net"] == pytest.approx(s["mean_excess_net"])
        assert row["hit_rate"] == pytest.approx(s["hit_rate_vs_benchmark"])
        assert row["mean_gross"] == pytest.approx(s["mean_gross"])
        assert row["mean_benchmark"] == pytest.approx(s["mean_benchmark"])
        assert row["n_cohorts"] == s["n_cohorts"]


@pytest.mark.fast
def test_monotone_in_cost_and_exact_slope():
    out = cost_sensitivity(_panel(), [0, 10, 20, 40])
    assert out["mean_net"].is_monotonic_decreasing
    assert out["mean_excess_net"].is_monotonic_decreasing
    assert (out["hit_rate"].diff().dropna() <= 0).all()
    # her ek bps, gidis-donus 2 * bps / 1e4 kadar dusurur
    d = out["mean_net"].iloc[0] - out["mean_net"]
    np.testing.assert_allclose(d.to_numpy(), 2.0 * out["cost_bps"].to_numpy() / 1e4, atol=1e-15)
    # brut ve benchmark maliyetten bagimsiz
    assert out["mean_gross"].nunique() == 1 and out["mean_benchmark"].nunique() == 1


@pytest.mark.fast
def test_hand_computed_row():
    df = pd.DataFrame({
        "made_at": pd.Timestamp("2024-03-01"),
        "ticker": list("ABCDE"),
        "score": [5, 4, 3, 2, 1],
        "tradable_ret": [0.10, 0.02, 0.0, -0.02, -0.04],
    })
    out = cost_sensitivity(df, [0, 10]).set_index("cost_bps")
    assert out.loc[0.0, "mean_net"] == pytest.approx(0.10)
    assert out.loc[10.0, "mean_net"] == pytest.approx(0.098)
    assert out.loc[10.0, "mean_excess_net"] == pytest.approx(0.098 - 0.012)
    assert out.loc[10.0, "hit_rate"] == 1.0


@pytest.mark.fast
def test_hit_rate_drops_when_cost_exceeds_edge():
    # brut fazla getiri 0.001; 10 bps gidis-donus 0.002 -> kohort benchmark'i gecemez
    df = pd.DataFrame({
        "made_at": pd.Timestamp("2024-03-01"),
        "ticker": list("ABCDE"),
        "score": [5, 4, 3, 2, 1],
        "tradable_ret": [0.006, 0.0, 0.0, 0.0, 0.0],  # secilen .006, benchmark .0012 -> excess .0048
    })
    out = cost_sensitivity(df, [0, 10, 40]).set_index("cost_bps")
    assert out.loc[0.0, "hit_rate"] == 1.0
    assert out.loc[10.0, "hit_rate"] == 1.0     # .0048 - .002 > 0
    assert out.loc[40.0, "hit_rate"] == 0.0     # .0048 - .008 < 0


@pytest.mark.fast
def test_validation_and_purity():
    df = _panel()
    snap = df.copy(deep=True)
    with pytest.raises(ValueError):
        cost_sensitivity(df, [])
    with pytest.raises(ValueError):
        cost_sensitivity(df, [0, -5])
    with pytest.raises(ValueError):
        cost_sensitivity(df, [0, float("nan")])
    with pytest.raises(ValueError, match="tekrar"):
        cost_sensitivity(df, [10, 10])
    with pytest.raises(ValueError):
        cost_sensitivity(df, 5)  # iterable degil
    with pytest.raises(ValueError, match="eksik sutun"):
        cost_sensitivity(df.drop(columns=["score"]), [0])
    with pytest.raises(ValueError):
        cost_sensitivity(df, [0], top_frac=0.0)
    a = cost_sensitivity(df, [0, 10])
    b = cost_sensitivity(df, [0, 10])
    pd.testing.assert_frame_equal(a, b)
    pd.testing.assert_frame_equal(df, snap)


@pytest.mark.fast
def test_empty_cohorts_gives_nan_rows():
    df = pd.DataFrame({"made_at": pd.Timestamp("2024-03-01"), "ticker": list("AB"),
                       "score": [1.0, 2.0], "tradable_ret": [0.1, 0.0]})
    out = cost_sensitivity(df, [0, 10])
    assert (out["n_cohorts"] == 0).all() and out["mean_net"].isna().all()
