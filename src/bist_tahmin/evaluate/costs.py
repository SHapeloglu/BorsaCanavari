"""Maliyet duyarlilik izgarasi (PROJE.md Bolum 8, 11).

Ayni secim ve benchmark ile yalnizca `cost_bps` degistirilir: secim maliyetten bagimsizdir, bu
yuzden kohort secimi izgara basina degil TOPLAM bir kez yapilir. Komisyon/spread
oranlari dogrulanmadi (PROJE.md Bolum 17, madde 1): tablonun 'tahmini' etiketini rapor koyar,
bu modul etiket eklemez.
"""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

from .portfolio import (
    _apply_cost,
    _base_cohorts,
    _prepare,
    _summarize,
    _validate_min_universe,
    _validate_top_frac,
    validate_cost_bps,
)

DEFAULT_GRID_BPS = (0.0, 10.0, 20.0, 40.0)  # config costs.sensitivity_grid_bps ile ayni

SENSITIVITY_COLS = (
    "cost_bps", "n_cohorts", "mean_gross", "mean_net", "mean_benchmark",
    "mean_excess_net", "hit_rate",
)


def cost_sensitivity(
    df: pd.DataFrame,
    grid_bps: Iterable[float] = DEFAULT_GRID_BPS,
    *,
    top_frac: float = 0.2,
    min_universe: int = 5,
) -> pd.DataFrame:
    """Her `grid_bps` degeri icin bir ozet satiri (izgara sirasi korunur).

    Kolonlar: cost_bps (bir yonlu), n_cohorts, mean_gross, mean_net, mean_benchmark,
    mean_excess_net, hit_rate (excess_net > 0 olan kohort orani). Her satir
    simulate_cohort_portfolio(df, cost_bps=...) ozetiyle aynidir. Maliyet arttikca mean_net
    ve mean_excess_net tam olarak 2 * artis / 1e4 kadar azalir; hit_rate artmaz.
    """
    top_frac = _validate_top_frac(top_frac)
    min_universe = _validate_min_universe(min_universe)
    try:
        grid = [validate_cost_bps(x) for x in grid_bps]
    except TypeError as exc:
        raise ValueError(f"grid_bps iterable olmali: {exc}") from exc
    if not grid:
        raise ValueError("grid_bps bos olamaz")
    if len(set(grid)) != len(grid):
        raise ValueError("grid_bps tekrar eden deger iceremez")
    work = _prepare(df)
    base, counters = _base_cohorts(work, top_frac, min_universe)
    rows = []
    for bps in grid:
        s = _summarize(_apply_cost(base, bps), bps, counters)
        rows.append({
            "cost_bps": bps,
            "n_cohorts": s["n_cohorts"],
            "mean_gross": s["mean_gross"],
            "mean_net": s["mean_net"],
            "mean_benchmark": s["mean_benchmark"],
            "mean_excess_net": s["mean_excess_net"],
            "hit_rate": s["hit_rate_vs_benchmark"],
        })
    return pd.DataFrame(rows, columns=list(SENSITIVITY_COLS))


__all__ = ["DEFAULT_GRID_BPS", "SENSITIVITY_COLS", "cost_sensitivity"]
