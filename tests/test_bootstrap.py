"""Blok bootstrap testleri (PROJE.md Bolum 13)."""

import numpy as np
import pandas as pd
import pytest

from bist_tahmin.evaluate.bootstrap import (
    BOOT_DAY_COL,
    block_bootstrap_ci,
    bonferroni_alpha,
    iid_bootstrap_ci,
    paired_diff_ci,
)
from bist_tahmin.evaluate.metrics import daily_ic


def _panel(n_days=120, n_names=30, shock_sd=1.0, noise_sd=0.5, seed=0):
    """Gun ici korelasyonlu panel: getiri = ortak gun soku + hisse gurultusu."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-02", periods=n_days)
    shock = rng.normal(0, shock_sd, n_days)
    rows = []
    for di, d in enumerate(dates):
        noise = rng.normal(0, noise_sd, n_names)
        for i in range(n_names):
            rows.append({"date": d, "ticker": f"T{i}", "ret": shock[di] + noise[i]})
    return pd.DataFrame(rows)


def _mean_ret(d):
    return float(d["ret"].mean())


def test_bonferroni_alpha():
    assert bonferroni_alpha(0.05, 1) == 0.05
    assert bonferroni_alpha(0.05, 10) == pytest.approx(0.005)
    for a, n in [(0.0, 3), (1.0, 3), (0.05, 0), (0.05, 2.5)]:
        with pytest.raises(ValueError):
            bonferroni_alpha(a, n)


def test_block_bootstrap_never_splits_days():
    # Her gunun satir sayisi farkli: tutarlilik kontrolu daha guclu olur.
    rng = np.random.default_rng(1)
    dates = pd.bdate_range("2024-01-01", periods=25)
    counts = rng.integers(3, 9, size=len(dates))
    df = pd.DataFrame({
        "date": np.repeat(dates, counts),
        "ret": rng.normal(size=int(counts.sum())),
    })
    orig = df.groupby("date").size()
    seen = []

    def spy(sample):
        seen.append(sample.copy())
        return float(sample["ret"].mean())

    block_bootstrap_ci(df, spy, date_col="date", block_days=4, resamples=30, seed=3)
    assert len(seen) == 31  # nokta + 30 ornek
    for s in seen[1:]:
        assert s[BOOT_DAY_COL].nunique() == len(dates)       # tam gun sayisi kadar gun ornegi
        for _, g in s.groupby(BOOT_DAY_COL):
            assert g["date"].nunique() == 1                   # bir ornek-gun tek tarihten
            assert len(g) == orig[g["date"].iloc[0]]          # ve o tarihin TUM satirlari
        # her tarihin satir sayisi orijinalin tam kati
        per_date = s.groupby("date").size()
        for d, c in per_date.items():
            assert c % orig[d] == 0
        assert len(s) == sum(orig[d] * (c // orig[d]) for d, c in per_date.items())


def test_block_bootstrap_rows_of_a_day_are_identical_copies():
    df = _panel(n_days=20, n_names=6)
    captured = []

    def spy(sample):
        captured.append(sample)
        return 0.0

    block_bootstrap_ci(df, spy, date_col="date", block_days=5, resamples=5, seed=2)
    ref = {d: g["ret"].to_numpy() for d, g in df.groupby("date")}
    for s in captured[1:]:
        for _, g in s.groupby(BOOT_DAY_COL):
            np.testing.assert_array_equal(g["ret"].to_numpy(), ref[g["date"].iloc[0]])


def test_blocks_are_consecutive_and_circular():
    # 1 satir/gun, deger = gun indeksi: bloklar ardisik (mod n) gunlerden olusmali.
    n = 23
    df = pd.DataFrame({"date": pd.bdate_range("2024-01-01", periods=n), "v": np.arange(n, dtype=float)})
    seqs = []

    def spy(sample):
        seqs.append(sample["v"].to_numpy().astype(int))
        return 0.0

    block_bootstrap_ci(df, spy, date_col="date", block_days=5, resamples=40, seed=9)
    for seq in seqs[1:]:
        assert len(seq) == n
        for b in range(0, n, 5):
            blk = seq[b:b + 5]
            assert np.all(np.diff(blk) % n == 1)
    wrapped = any(np.any(np.diff(s[:5]) < 0) for s in seqs[1:])
    assert wrapped  # dairesel sarma en az bir ornekte gorulmeli


def test_unsorted_input_and_index_do_not_matter():
    df = _panel(n_days=40, n_names=4)
    shuffled = df.sample(frac=1.0, random_state=0)
    a = block_bootstrap_ci(df, _mean_ret, date_col="date", resamples=200, seed=4)
    b = block_bootstrap_ci(shuffled, _mean_ret, date_col="date", resamples=200, seed=4)
    assert a[:3] == pytest.approx(b[:3])


def test_reproducible_with_seed_and_differs_across_seeds():
    df = _panel(n_days=60, n_names=5)
    r1 = block_bootstrap_ci(df, _mean_ret, date_col="date", resamples=300, seed=7)
    r2 = block_bootstrap_ci(df, _mean_ret, date_col="date", resamples=300, seed=7)
    r3 = block_bootstrap_ci(df, _mean_ret, date_col="date", resamples=300, seed=8)
    assert r1[:3] == r2[:3]
    np.testing.assert_array_equal(r1[3]["replicates"], r2[3]["replicates"])
    assert r1[1:3] != r3[1:3]
    i1 = iid_bootstrap_ci(df, _mean_ret, resamples=300, seed=7)
    i2 = iid_bootstrap_ci(df, _mean_ret, resamples=300, seed=7)
    assert i1[:3] == i2[:3]


def test_point_estimate_is_statistic_on_original_data():
    df = _panel(n_days=30, n_names=4)
    point, lo, hi, det = block_bootstrap_ci(df, _mean_ret, date_col="date", resamples=200)
    assert point == pytest.approx(df["ret"].mean())
    assert lo <= point <= hi
    assert det["n_dates"] == 30 and det["n_rows"] == 120 and det["resamples"] == 200
    assert det["method"] == "circular_block_by_date"


def test_block_bootstrap_ci_much_wider_than_iid_with_within_day_correlation():
    df = _panel(n_days=120, n_names=30, shock_sd=1.0, noise_sd=0.5, seed=42)
    _, blo, bhi, _ = block_bootstrap_ci(df, _mean_ret, date_col="date", block_days=10,
                                        resamples=500, seed=0)
    _, ilo, ihi, _ = iid_bootstrap_ci(df, _mean_ret, resamples=500, seed=0)
    # Kuramsal oran: sqrt(30 / 2.3) ~ 5; esik cok gevsek tutuldu.
    assert (bhi - blo) > 2.0 * (ihi - ilo)


def test_block_bootstrap_matches_iid_without_dependence():
    # Gun soku yok, saf gurultu: iki aralik yakin olmali (blok yontemi gereksiz genisletmez).
    df = _panel(n_days=150, n_names=20, shock_sd=0.0, noise_sd=1.0, seed=5)
    _, blo, bhi, _ = block_bootstrap_ci(df, _mean_ret, date_col="date", resamples=500, seed=0)
    _, ilo, ihi, _ = iid_bootstrap_ci(df, _mean_ret, resamples=500, seed=0)
    assert 0.6 < (bhi - blo) / (ihi - ilo) < 1.6


def test_block_days_larger_than_dates_is_clamped():
    df = _panel(n_days=6, n_names=3)
    _, _, _, det = block_bootstrap_ci(df, _mean_ret, date_col="date", block_days=50, resamples=20)
    assert det["block_days_used"] == 6


def test_bootstrap_with_daily_ic_statistic_uses_boot_day_groups():
    rng = np.random.default_rng(0)
    dates = pd.bdate_range("2024-01-01", periods=60)
    rows = []
    for d in dates:
        s = rng.normal(size=12)
        r = 0.5 * s + rng.normal(size=12)
        rows += [{"date": d, "score": s[i], "ret": r[i]} for i in range(12)]
    df = pd.DataFrame(rows)

    def mean_ic(sample):
        col = BOOT_DAY_COL if BOOT_DAY_COL in sample.columns else "date"
        return float(daily_ic(sample, col, "score", "ret").mean())

    point, lo, hi, _ = block_bootstrap_ci(df, mean_ic, date_col="date", block_days=5,
                                          resamples=100, seed=1)
    assert 0.0 < lo < point < hi


def test_block_bootstrap_validation():
    df = _panel(n_days=10, n_names=2)
    with pytest.raises(ValueError):
        block_bootstrap_ci(df, _mean_ret, date_col="nope")
    with pytest.raises(ValueError):
        block_bootstrap_ci(df.iloc[0:0], _mean_ret, date_col="date")
    with pytest.raises(ValueError):
        block_bootstrap_ci(df, _mean_ret, date_col="date", block_days=0)
    with pytest.raises(ValueError):
        block_bootstrap_ci(df, _mean_ret, date_col="date", ci=1.0)
    with pytest.raises(ValueError):
        block_bootstrap_ci(df, _mean_ret, date_col="date", resamples=0)
    with pytest.raises(ValueError):
        block_bootstrap_ci(df.assign(**{BOOT_DAY_COL: 1}), _mean_ret, date_col="date")
    nan_dates = df.copy()
    nan_dates.loc[0, "date"] = pd.NaT
    with pytest.raises(ValueError):
        block_bootstrap_ci(nan_dates, _mean_ret, date_col="date")


def test_nan_replicates_are_dropped_and_counted():
    df = _panel(n_days=20, n_names=3)
    calls = {"n": 0}

    def flaky(sample):
        calls["n"] += 1
        return float("nan") if calls["n"] % 4 == 0 else float(sample["ret"].mean())

    _, lo, hi, det = block_bootstrap_ci(df, flaky, date_col="date", resamples=40, seed=0)
    assert det["n_nan"] > 0 and det["n_valid"] + det["n_nan"] == 40
    assert np.isfinite(lo) and np.isfinite(hi)


# --- Eslestirilmis fark ---

def _loss_panel(delta, n_days=150, n_names=20, seed=0):
    """loss_b = taban + ortak gun soku + gurultu; loss_a = loss_b + delta + kucuk gurultu."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-02", periods=n_days)
    day_shock = rng.normal(0, 0.05, n_days)       # a ve b icin ortak
    diff_shock = rng.normal(0, 0.005, n_days)     # farkta gun duzeyi dalgalanma
    rows = []
    for di, d in enumerate(dates):
        for i in range(n_names):
            b = 0.25 + day_shock[di] + rng.normal(0, 0.05)
            a = b + delta + diff_shock[di] + rng.normal(0, 0.02)
            rows.append({"date": d, "ticker": f"T{i}", "loss_a": a, "loss_b": b})
    return pd.DataFrame(rows)


def test_paired_diff_excludes_zero_for_truly_better_model():
    df = _loss_panel(delta=-0.01, seed=1)
    point, lo, hi, det = paired_diff_ci(df, "loss_a", "loss_b", date_col="date",
                                        resamples=1000, seed=0)
    assert point == pytest.approx((df["loss_a"] - df["loss_b"]).mean())
    assert point < 0 and hi < 0 and lo < hi
    assert det["excludes_zero"] is True


def test_paired_diff_does_not_exclude_zero_for_equal_models():
    for seed in range(5):  # sabit tohumlar: deterministik
        df = _loss_panel(delta=0.0, seed=100 + seed)
        _, lo, hi, det = paired_diff_ci(df, "loss_a", "loss_b", date_col="date",
                                        resamples=1000, seed=0)
        assert lo < 0 < hi, f"seed {seed}: ({lo}, {hi})"
        assert det["excludes_zero"] is False


def test_paired_diff_matches_generic_block_bootstrap():
    df = _loss_panel(delta=-0.004, n_days=60, n_names=5, seed=3)
    p1, lo1, hi1, _ = paired_diff_ci(df, "loss_a", "loss_b", date_col="date",
                                     block_days=7, resamples=200, seed=5)
    p2, lo2, hi2, _ = block_bootstrap_ci(
        df, lambda d: float((d["loss_a"] - d["loss_b"]).mean()), date_col="date",
        block_days=7, resamples=200, seed=5)
    assert (p1, lo1, hi1) == pytest.approx((p2, lo2, hi2))


def test_paired_diff_is_antisymmetric():
    df = _loss_panel(delta=-0.01, n_days=80, n_names=5, seed=2)
    p, lo, hi, _ = paired_diff_ci(df, "loss_a", "loss_b", date_col="date", resamples=300, seed=1)
    p2, lo2, hi2, _ = paired_diff_ci(df, "loss_b", "loss_a", date_col="date", resamples=300, seed=1)
    assert p2 == pytest.approx(-p) and lo2 == pytest.approx(-hi) and hi2 == pytest.approx(-lo)


def test_paired_diff_wider_ci_at_higher_confidence():
    df = _loss_panel(delta=-0.005, n_days=80, n_names=5, seed=4)
    _, lo95, hi95, d95 = paired_diff_ci(df, "loss_a", "loss_b", date_col="date", resamples=1000, seed=0)
    _, lo99, hi99, d99 = paired_diff_ci(df, "loss_a", "loss_b", date_col="date", resamples=1000,
                                        ci=0.99, seed=0)
    assert (hi99 - lo99) > (hi95 - lo95)
    assert d99["tail_draws"] == pytest.approx(5.0)


def test_paired_diff_with_real_brier_losses():
    # Gercek model: p_true bilen model, sabit 0.5'e karsi. Gun ici ortak yon sokuyla.
    rng = np.random.default_rng(8)
    dates = pd.bdate_range("2023-01-02", periods=200)
    rows = []
    for d in dates:
        day_p = rng.choice([0.35, 0.65])
        y = (rng.random(25) < day_p).astype(int)
        for i in range(25):
            rows.append({"date": d, "y": y[i], "p_model": day_p, "p_const": 0.5})
    df = pd.DataFrame(rows)
    df["loss_model"] = (df["p_model"] - df["y"]) ** 2
    df["loss_const"] = (df["p_const"] - df["y"]) ** 2
    point, lo, hi, det = paired_diff_ci(df, "loss_model", "loss_const", date_col="date",
                                        resamples=1000, seed=0)
    assert point < 0 and det["excludes_zero"]


def test_paired_diff_validation():
    df = _loss_panel(delta=0.0, n_days=10, n_names=2)
    with pytest.raises(ValueError):
        paired_diff_ci(df, "loss_a", "missing", date_col="date")
    bad = df.copy()
    bad.loc[0, "loss_a"] = np.nan
    with pytest.raises(ValueError):
        paired_diff_ci(bad, "loss_a", "loss_b", date_col="date")
    with pytest.raises(ValueError):
        paired_diff_ci(df, "loss_a", "loss_b", date_col="date", ci=0.0)


def test_iid_bootstrap_basic():
    df = _panel(n_days=10, n_names=5)
    point, lo, hi, det = iid_bootstrap_ci(df, _mean_ret, resamples=100, seed=0)
    assert point == pytest.approx(df["ret"].mean()) and lo <= hi
    assert det["method"] == "iid_rows"
    with pytest.raises(ValueError):
        iid_bootstrap_ci(df.iloc[0:0], _mean_ret)
