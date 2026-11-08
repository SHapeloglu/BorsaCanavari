"""Metrik testleri (PROJE.md Bolum 13): Brier, log loss, kalibrasyon, IC, Newey-West."""

import math

import numpy as np
import pandas as pd
import pytest

from bist_tahmin.evaluate.metrics import (
    accuracy,
    base_rate_brier,
    brier,
    calibration_table,
    daily_ic,
    default_nw_lags,
    effective_sample_size,
    ic_summary,
    log_loss,
    newey_west_variance_of_mean,
)


# --- Brier / log loss / accuracy ---

def test_brier_constant_half_is_quarter():
    labels = np.array([1, 0, 1, 1, 0, 0, 1])
    assert brier(np.full(len(labels), 0.5), labels) == pytest.approx(0.25)


def test_brier_hand_computed():
    # (0.9-1)^2=0.01, (0.2-0)^2=0.04, (0.6-1)^2=0.16, (0.7-0)^2=0.49 -> 0.7/4
    assert brier([0.9, 0.2, 0.6, 0.7], [1, 0, 1, 0]) == pytest.approx(0.175)


def test_brier_perfect_and_worst():
    assert brier([1.0, 0.0], [1, 0]) == 0.0
    assert brier([0.0, 1.0], [1, 0]) == 1.0


def test_log_loss_hand_computed():
    expected = -(math.log(0.8) + math.log(0.7)) / 2
    assert log_loss([0.8, 0.3], [1, 0]) == pytest.approx(expected)


def test_log_loss_clips_extremes_and_is_finite():
    # prob 1.0 ve etiket 0: kirpilmazsa sonsuz olurdu
    val = log_loss([1.0, 0.0], [0, 1])
    assert math.isfinite(val)
    assert val == pytest.approx(-math.log(0.01))


def test_log_loss_custom_clip_and_invalid_clip():
    assert log_loss([1.0], [0], clip=(0.1, 0.9)) == pytest.approx(-math.log(0.1))
    for bad in [(0.0, 0.9), (0.1, 1.0), (0.6, 0.4)]:
        with pytest.raises(ValueError):
            log_loss([0.5], [1], clip=bad)


def test_accuracy_threshold():
    assert accuracy([0.9, 0.4, 0.6, 0.2], [1, 0, 0, 0]) == pytest.approx(0.75)
    assert accuracy([0.9, 0.4], [1, 0], threshold=0.95) == pytest.approx(0.5)


@pytest.mark.parametrize("fn", [brier, log_loss, accuracy])
@pytest.mark.parametrize(
    "prob,label",
    [
        ([1.2, 0.5], [1, 0]),       # olasilik > 1
        ([-0.1, 0.5], [1, 0]),      # olasilik < 0
        ([0.5, 0.5], [1, 2]),       # etiket 0/1 disi
        ([0.5, 0.5], [1, 0, 1]),    # uzunluk uyumsuz
        ([], []),                   # bos
        ([float("nan"), 0.5], [1, 0]),
        ([0.5, 0.5], [1, float("nan")]),
    ],
)
def test_metric_input_validation(fn, prob, label):
    with pytest.raises(ValueError):
        fn(prob, label)


def test_base_rate_brier():
    assert base_rate_brier([1, 1, 0, 0]) == pytest.approx(0.25)
    assert base_rate_brier([1, 1, 1, 0]) == pytest.approx(0.1875)
    assert base_rate_brier([1, 1, 1]) == 0.0
    with pytest.raises(ValueError):
        base_rate_brier([])
    with pytest.raises(ValueError):
        base_rate_brier([0, 3])


def test_base_rate_brier_matches_constant_forecast():
    y = np.array([1, 0, 1, 1, 0, 1, 1, 0, 1, 1])
    assert base_rate_brier(y) == pytest.approx(brier(np.full(len(y), y.mean()), y))


# --- Kalibrasyon ---

def test_calibration_table_basic_and_empty_bins():
    prob = [0.05, 0.05, 0.95, 0.95, 0.95, 1.0]
    label = [0, 1, 1, 1, 0, 1]
    t = calibration_table(prob, label, bins=10)
    assert len(t) == 10
    assert t["n"].sum() == 6
    assert list(t.columns) == ["bin_lo", "bin_hi", "n", "mean_pred", "obs_rate"]
    first, last = t.iloc[0], t.iloc[9]
    assert first["n"] == 2 and first["mean_pred"] == pytest.approx(0.05)
    assert first["obs_rate"] == pytest.approx(0.5)
    # 1.0 son kovaya girer
    assert last["n"] == 4 and last["obs_rate"] == pytest.approx(0.75)
    # bos kovalar gorunur
    empty = t.iloc[1:9]
    assert (empty["n"] == 0).all()
    assert empty["mean_pred"].isna().all() and empty["obs_rate"].isna().all()
    assert t["bin_lo"].iloc[0] == 0.0 and t["bin_hi"].iloc[-1] == 1.0


def test_calibration_table_edges_left_closed():
    t = calibration_table([0.5], [1], bins=2)
    assert list(t["n"]) == [0, 1]
    t = calibration_table([0.0], [0], bins=2)
    assert list(t["n"]) == [1, 0]


def test_calibration_table_validation():
    with pytest.raises(ValueError):
        calibration_table([0.5], [1], bins=0)
    with pytest.raises(ValueError):
        calibration_table([1.5], [1])


# --- IC ---

def _panel(n_days=30, n_names=10, kind="perfect", seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    dates = pd.bdate_range("2024-01-01", periods=n_days)
    for d in dates:
        score = rng.normal(size=n_names)
        if kind == "perfect":
            ret = score * 2.0 + 1.0          # monoton artan donusum
        elif kind == "inverse":
            ret = -score ** 3
        else:
            ret = rng.normal(size=n_names)
        for i in range(n_names):
            rows.append({"date": d, "ticker": f"T{i}", "score": score[i], "ret": ret[i]})
    return pd.DataFrame(rows)


def test_daily_ic_perfect_inverse_and_unrelated():
    perfect = daily_ic(_panel(kind="perfect"), "date", "score", "ret")
    assert np.allclose(perfect, 1.0)
    inv = daily_ic(_panel(kind="inverse"), "date", "score", "ret")
    assert np.allclose(inv, -1.0)
    unrelated = daily_ic(_panel(n_days=400, n_names=20, kind="noise", seed=3), "date", "score", "ret")
    assert abs(unrelated.mean()) < 0.03


def test_daily_ic_known_value_and_ties():
    # sira: score 1,2,3,4,5; ret sirasi 2,1,3,5,4 -> Spearman = 1 - 6*sum(d^2)/(n(n^2-1)) = 1 - 6*4/120 = 0.8
    df = pd.DataFrame({
        "date": ["2024-01-02"] * 5,
        "score": [1, 2, 3, 4, 5],
        "ret": [0.2, 0.1, 0.3, 0.5, 0.4],
    })
    assert daily_ic(df, "date", "score", "ret").iloc[0] == pytest.approx(0.8)
    # baglar: ortalama sira kullanilir (sabit skor -> tanimsiz -> NaN)
    df2 = df.assign(score=1.0)
    assert math.isnan(daily_ic(df2, "date", "score", "ret").iloc[0])


def test_daily_ic_min_names_gives_nan_and_keeps_day():
    df = _panel(n_days=3, n_names=10)
    df = df[~((df["date"] == df["date"].unique()[1]) & (df["ticker"].isin([f"T{i}" for i in range(6)])))]
    ic = daily_ic(df, "date", "score", "ret", min_names=5)  # 2. gun 4 hisse
    assert len(ic) == 3
    assert math.isnan(ic.iloc[1])
    assert ic.iloc[0] == pytest.approx(1.0) and ic.iloc[2] == pytest.approx(1.0)
    assert ic.index.is_monotonic_increasing


def test_daily_ic_drops_nan_rows_and_validates():
    df = _panel(n_days=1, n_names=6)
    df.loc[0, "ret"] = np.nan  # 5 hisse kalir -> yeterli
    assert daily_ic(df, "date", "score", "ret", min_names=5).iloc[0] == pytest.approx(1.0)
    assert math.isnan(daily_ic(df, "date", "score", "ret", min_names=6).iloc[0])
    with pytest.raises(ValueError):
        daily_ic(df, "date", "nope", "ret")
    with pytest.raises(ValueError):
        daily_ic(df, "date", "score", "ret", min_names=1)


def test_daily_ic_matches_numpy_pearson_of_ranks():
    df = _panel(n_days=1, n_names=25, kind="noise", seed=7)
    got = daily_ic(df, "date", "score", "ret").iloc[0]
    rs = df["score"].rank().to_numpy()
    rr = df["ret"].rank().to_numpy()
    assert got == pytest.approx(np.corrcoef(rs, rr)[0, 1])


# --- Newey-West ---

def _ar1(n, phi, seed, mean=0.05, sd=0.1):
    rng = np.random.default_rng(seed)
    x = np.empty(n)
    x[0] = rng.normal()
    for i in range(1, n):
        x[i] = phi * x[i - 1] + rng.normal()
    return mean + sd * x


def test_default_nw_lags_is_horizon_minus_one():
    assert default_nw_lags(5) == 4
    assert default_nw_lags(1) == 0
    assert ic_summary(_ar1(100, 0.0, 1))["nw_lags"] == 4
    assert ic_summary(_ar1(100, 0.0, 1), nw_lags=2)["nw_lags"] == 2
    assert ic_summary(_ar1(100, 0.0, 1), horizon_days=10)["nw_lags"] == 9


def test_newey_west_zero_lags_equals_population_variance():
    x = np.array([0.1, 0.3, -0.2, 0.05, 0.4, 0.0])
    assert newey_west_variance_of_mean(x, 0) == pytest.approx(x.var(ddof=0) / len(x))


def test_newey_west_hand_computed_bartlett():
    x = np.array([1.0, 2.0, 4.0, 3.0])
    d = x - x.mean()  # [-1.5,-0.5,1.5,0.5]
    g0 = (d @ d) / 4                     # 5/4
    g1 = (d[1:] @ d[:-1]) / 4            # (0.75 - 0.75 + 0.75)/4
    expected = (g0 + 2 * 0.5 * g1) / 4   # Bartlett agirligi 1 - 1/2
    assert newey_west_variance_of_mean(x, 1) == pytest.approx(expected)


def test_nw_abs_t_smaller_than_iid_for_autocorrelated_series():
    s = _ar1(300, 0.8, seed=11)
    r = ic_summary(s, nw_lags=4)
    assert abs(r["t_stat_nw"]) < abs(r["t_stat_iid"])
    assert r["mean_ic"] == pytest.approx(s.mean())
    assert r["n_days"] == 300


def test_nw_close_to_iid_for_white_noise():
    s = _ar1(2000, 0.0, seed=5)
    r = ic_summary(s, nw_lags=4)
    assert r["t_stat_nw"] == pytest.approx(r["t_stat_iid"], rel=0.15)


def test_ic_summary_handles_nan_and_degenerate():
    s = pd.Series([0.1, np.nan, 0.2, 0.15, np.nan, 0.05])
    r = ic_summary(s, nw_lags=1)
    assert r["n_days"] == 4 and r["n_days_total"] == 6
    assert r["mean_ic"] == pytest.approx(0.125)
    assert ic_summary(pd.Series([], dtype=float))["n_days"] == 0
    assert math.isnan(ic_summary(pd.Series([], dtype=float))["mean_ic"])
    one = ic_summary(pd.Series([0.3]))
    assert one["mean_ic"] == 0.3 and math.isnan(one["t_stat_nw"])
    const = ic_summary(pd.Series([0.2] * 10))
    assert math.isnan(const["t_stat_nw"])  # varyans sifir -> tanimsiz
    with pytest.raises(ValueError):
        ic_summary(pd.Series([0.1, 0.2]), nw_lags=-1)


def test_ic_summary_lags_capped_by_sample_size():
    assert ic_summary(pd.Series([0.1, 0.3, 0.2]), nw_lags=10)["nw_lags"] == 2


# --- Etkin orneklem ---

def test_effective_sample_size():
    assert effective_sample_size(30, 0.0) == pytest.approx(30.0)
    assert effective_sample_size(30, 1.0) == pytest.approx(1.0)
    assert effective_sample_size(30, 0.42) == pytest.approx(30 / (1 + 29 * 0.42))
    assert 2.0 < effective_sample_size(30, 0.42) < 3.0  # PROJE.md Bolum 0: 2-3 bagimsiz gozlem
    assert effective_sample_size(1, 0.9) == pytest.approx(1.0)
    with pytest.raises(ValueError):
        effective_sample_size(0, 0.1)
    with pytest.raises(ValueError):
        effective_sample_size(10, -0.2)   # 1 + 9*(-0.2) < 0
    with pytest.raises(ValueError):
        effective_sample_size(10, 1.5)
