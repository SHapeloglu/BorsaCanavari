"""Rapor portfoy uyarlayicisi: P'nin kohort portfoyu + Rp'nin rapor kancasi."""

import numpy as np
import pandas as pd

from bist_tahmin.config import load_config
from bist_tahmin.evaluate.report_portfolio import cohort_portfolio_section

CFG = load_config("config/default.yaml")


def _df(scores_fn, n_days=12, n_names=10, seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for d in pd.bdate_range("2026-01-05", periods=n_days):
        rets = rng.normal(0.002, 0.03, n_names)
        for i in range(n_names):
            s = scores_fn(i, rets[i])
            rows.append({"made_at": d, "ticker": f"T{i}", "score": s, "prob_up": 0.5, "tradable_ret": rets[i]})
    return pd.DataFrame(rows)


def test_always_up_equals_benchmark():
    out = cohort_portfolio_section(_df(lambda i, r: 0.99), CFG)
    s = out["ozet (maliyetsiz)"]
    assert abs(s["mean_excess_gross"]) < 1e-9 and s["selected_frac_mean"] == 1.0


def test_perfect_foresight_beats_benchmark_and_costs_reduce_net():
    out = cohort_portfolio_section(_df(lambda i, r: r), CFG)                      # skor = gercek getiri
    assert out["ozet (maliyetsiz)"]["mean_excess_gross"] > 0
    nets = [row["mean_net"] for row in out["maliyet duyarliligi"]]
    assert nets == sorted(nets, reverse=True) and len(nets) == len(CFG["costs"]["sensitivity_grid_bps"])
    assert out["dusus (alt sinir; gunluk MTM yok)"]["max_dusus_model"] >= 0


def test_empty_and_no_resolved_returns_none_or_message():
    assert cohort_portfolio_section(pd.DataFrame(), CFG) is None
    df = _df(lambda i, r: 0.5)
    df["tradable_ret"] = np.nan
    assert "hesaplanamadi" in cohort_portfolio_section(df, CFG)


def test_missing_score_falls_back_to_prob_up():
    df = _df(lambda i, r: np.nan)
    df["prob_up"] = [0.9 if i % 2 else 0.1 for i in range(len(df))]
    assert cohort_portfolio_section(df, CFG)["ozet (maliyetsiz)"]["n_cohorts"] > 0
