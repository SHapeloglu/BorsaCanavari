"""Deney orkestrasyonu (Faz 3.5): dataset -> purge'lu walk-forward -> baseline'larla degerlendirme -> `experiments`.

Kurallar (PROJE.md Bolum 5, 11, 15):
- Deney, calistirilmadan ONCE `experiments`a kaydedilir (kayit = deneme sayilir; iyi cikmayan deneme silinemez).
  `n_trials` = ayni ailedeki kayit sayisi (bu deney dahil); Bonferroni duzeltmesi `lab.evaluate`te uygulanir.
- Sinama kumesi (`holdout.holdout_start`) varsayilan KILITLI: test gunleri ve etiketi holdout'a uzanan satirlar disarida.
  `use_holdout=True` yalnizca terfi asamasi icindir; kullanim deney kaydina ve ciktiya yazilir.
- Calisma hata verirse kayit kalir ve sonuc olarak hata yazilir (deneme sayisi gizlenmez).
- `dry_run=True` veritabanina HICBIR sey yazmaz (deneme sayilmaz); n_trials = mevcut + 1 varsayilir.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pandas as pd

from . import lab
from .dataset import Dataset
from .guard import HoldoutGuard
from .registry import adjusted_alpha, count_trials, record_experiment, update_result
from .splits import make_folds


@dataclass
class ExperimentRun:
    experiment_id: int | None
    family: str
    n_trials: int
    n_folds: int
    result: dict
    preds: pd.DataFrame = field(repr=False)
    access_note: str = ""


def run_experiment(
    conn,
    cfg: dict,
    ds: Dataset,
    spec: lab.ModelSpec,
    *,
    family: str,
    description: str,
    use_holdout: bool = False,
    dry_run: bool = False,
    alpha: float = 0.05,
    seed: int = 0,
) -> ExperimentRun:
    guard = HoldoutGuard.from_config(cfg, use_holdout=use_holdout)
    tr = cfg["training"]
    days = pd.DatetimeIndex(sorted(ds.frame["date"].unique()))
    folds = make_folds(
        days, horizon_days=int(cfg["horizon_days"]), purge_days=int(tr["purge_days"]),
        min_train_days=int(tr["min_train_days"]), retrain_every_days=int(tr["retrain_every_days"]),
        end=guard.clip_end(),
    )
    if not folds:
        raise ValueError("fold uretilemedi: veri min_train_days + purge icin yetersiz ya da tum test donemi holdout'ta")
    params = {
        **spec.params(), "family": family, "seed": seed, "alpha": alpha, "n_folds": len(folds),
        "holdout": {"locked": not use_holdout, "holdout_start": str(guard.holdout_start.date()),
                    "note": guard.access_note()},
        "dataset": {k: ds.meta.get(k) for k in ("feature_version", "n_rows", "date_min", "date_max", "built_at")},
        "training": dict(tr), "horizon_days": int(cfg["horizon_days"]),
    }
    exp_id: int | None = None
    if dry_run:
        n_trials = (count_trials(conn, family) if conn is not None else 0) + 1
    else:
        exp_id = record_experiment(conn, family, description, params)   # ONCE kaydet: deneme sayilir
        n_trials = count_trials(conn, family)
    try:
        preds = lab.run_walk_forward(ds.frame, spec, folds, guard=guard, seed=seed)
        base_frame = guard.filter_frame(ds.frame, "date", "end_date")
        baselines = lab.dataset_baseline_probs(base_frame, horizon_days=ds.horizon_days, label=spec.label)
        result = lab.evaluate(preds, baselines, label=spec.label, cfg=cfg, alpha=alpha, n_trials=n_trials, seed=seed)
        result["experiment"] = {"id": exp_id, "family": family, "n_trials": n_trials, "holdout": guard.access_note()}
        if exp_id is not None:
            update_result(conn, exp_id, json.loads(lab.result_json(result)))
    except BaseException as exc:
        if exp_id is not None:
            try:
                update_result(conn, exp_id, {"error": f"{type(exc).__name__}: {exc}"[:500]})
            except Exception:  # sonuc zaten yazilmis olabilir
                pass
        raise
    return ExperimentRun(exp_id, family, n_trials, len(folds), result, preds, guard.access_note())
