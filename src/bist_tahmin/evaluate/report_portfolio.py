"""Rapor icin kohort portfoy bolumu: `build_report(portfolio_fn=...)` kancasina baglanan uyarlayici.

Girdi: bir modelin COZULMUS satirlari (predictions sutunlari). Cikti: raporun genel sozluk basimina uygun dict.
Maliyetler `costs` konfigundaki izgara ile gosterilir; oranlar dogrulanmadiysa rapor "tahmini" etiketini ayrica basar.
Benchmark maliyetsiz, esit agirlikli evrendir (bkz. portfolio.simulate_cohort_portfolio).
"""

from __future__ import annotations

import pandas as pd

from .costs import cost_sensitivity
from .portfolio import equity_curve, max_drawdown, relative_drawdown, simulate_cohort_portfolio

TOP_FRAC = 0.2


def cohort_portfolio_section(df: pd.DataFrame, cfg: dict) -> dict | None:
    if df is None or df.empty:
        return None
    d = df.copy()
    d["score"] = d["score"].where(d["score"].notna(), d["prob_up"])        # skoru olmayan satirda olasilik
    d = d[["made_at", "ticker", "score", "tradable_ret"]].dropna(subset=["tradable_ret"])
    if d.empty:
        return {"hesaplanamadi": "tradable_ret'li cozulmus satir yok"}
    horizon = int(cfg["horizon_days"])
    grid = list(cfg["costs"]["sensitivity_grid_bps"])
    try:
        res = simulate_cohort_portfolio(d, top_frac=TOP_FRAC, cost_bps=0.0)
        if res.cohorts.empty:
            return {"hesaplanamadi": f"kohort yok (atlanan gun: {res.summary.get('skipped_days')})",
                    "ozet": {k: v for k, v in res.summary.items()}}
        table = cost_sensitivity(d, grid, top_frac=TOP_FRAC)
        net = equity_curve(res, horizon, kind="net")
        bench = equity_curve(res, horizon, kind="benchmark")
    except ValueError as exc:
        return {"hesaplanamadi": str(exc)}
    return {
        "secim": f"her gun skora gore ilk %{int(TOP_FRAC * 100)} (esit skorlar dahil), esit agirlik, yalniz uzun",
        "ozet (maliyetsiz)": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in res.summary.items()},
        "maliyet duyarliligi": [{k: (round(v, 6) if isinstance(v, float) else v) for k, v in row.items()}
                                for row in table.to_dict("records")],
        "dusus (alt sinir; gunluk MTM yok)": {
            "max_dusus_model": round(max_drawdown(net), 6),
            "max_dusus_benchmark": round(max_drawdown(bench), 6),
            "goreli_dusus (model/benchmark)": round(relative_drawdown(net, bench), 6),
        },
    }
