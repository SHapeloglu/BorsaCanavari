"""Deney orkestrasyonu: kayit, deneme sayaci, holdout kilidi, dry-run (Faz 3.5 kabul kriterleri)."""

import copy
import uuid

import pandas as pd
import psycopg
import pytest
from conftest import APP_PASSWORD
from test_lab import make_frame

from bist_tahmin.backtest.dataset import Dataset
from bist_tahmin.backtest.lab import ModelSpec
from bist_tahmin.backtest.registry import count_trials, list_experiments
from bist_tahmin.backtest.runner import run_experiment
from bist_tahmin.config import load_config
from bist_tahmin.db import APP_ROLE, run_init, with_overrides
from bist_tahmin.features.build import FEATURE_NAMES

CFG = load_config("config/default.yaml")


@pytest.fixture(scope="module")
def ds():
    frame, dates = make_frame(n_days=700, n_tickers=10, beta=0.9, seed=3)
    return Dataset(frame, 5, 0.0, list(FEATURE_NAMES), {"feature_version": "f1", "n_rows": len(frame)}), dates


@pytest.fixture()
def conn(admin_conninfo):
    name = f"bist_bt_{uuid.uuid4().hex[:8]}"
    run_init(admin_conninfo, db_name=name, app_password=APP_PASSWORD)
    info = with_overrides(admin_conninfo, dbname=name, user=APP_ROLE, password=APP_PASSWORD)
    with psycopg.connect(info, autocommit=True) as c:
        yield c


def _cfg(dates, holdout_idx=600):
    c = copy.deepcopy(CFG)
    c["holdout"]["holdout_start"] = str(dates[holdout_idx].date())
    c["training"].update(min_train_days=200, retrain_every_days=40, purge_days=5)
    return c


SPEC = ModelSpec(name="lr_all_z", features="all_z", C=0.5)


def test_run_records_experiment_and_keeps_holdout_locked(conn, ds):
    d, dates = ds
    cfg = _cfg(dates)
    run = run_experiment(conn, cfg, d, SPEC, family="t_lock", description="kilitli deneme")
    assert run.experiment_id and run.n_trials == 1 and run.n_folds > 0
    assert run.preds["date"].max() < dates[600]                                 # test gunleri holdout'a girmez
    assert run.preds["end_date"].max() < dates[600]                             # etiket bitisi de girmez
    rec = list_experiments(conn, "t_lock")[0]
    assert rec["params_json"]["holdout"]["locked"] is True and rec["result_json"]["experiment"]["n_trials"] == 1
    main = run.result["main"]
    assert main["model_better"] is True and main["excludes_zero"] is True    # beta=0.9: gercek sinyal baz orani gecer
    assert main["n_trials"] == 1 and run.result["n_dates"] > 0


def test_trial_counter_grows_and_ci_widens(conn, ds):
    d, dates = ds
    cfg = _cfg(dates)
    first = run_experiment(conn, cfg, d, SPEC, family="t_trials", description="1")
    second = run_experiment(conn, cfg, d, ModelSpec(name="b", C=0.1), family="t_trials", description="2")
    assert (first.n_trials, second.n_trials) == (1, 2) and count_trials(conn, "t_trials") == 2
    a, b = first.result["main"], second.result["main"]
    assert (a["n_trials"], b["n_trials"]) == (1, 2)
    assert b["alpha_adjusted"] < a["alpha_adjusted"] and b["ci_level"] > a["ci_level"]      # Bonferroni: araligin duzeyi artar
    assert (b["hi"] - b["lo"]) > 0 and (a["hi"] - a["lo"]) > 0


def test_use_holdout_is_explicit_and_recorded(conn, ds):
    d, dates = ds
    cfg = _cfg(dates)
    run = run_experiment(conn, cfg, d, SPEC, family="t_open", description="acik", use_holdout=True)
    assert run.preds["date"].max() >= dates[600]                                # kilit acik: holdout gunleri test edilir
    rec = list_experiments(conn, "t_open")[0]
    assert rec["params_json"]["holdout"]["locked"] is False and "holdout" in rec["params_json"]["holdout"]["note"].lower()


def test_dry_run_writes_nothing(conn, ds):
    d, dates = ds
    run = run_experiment(conn, _cfg(dates), d, SPEC, family="t_dry", description="kuru", dry_run=True)
    assert run.experiment_id is None and run.n_trials == 1 and count_trials(conn, "t_dry") == 0


def test_failed_run_still_counts_as_a_trial(conn, ds):
    d, dates = ds
    bad = Dataset(d.frame.drop(columns=["end_date"]), 5, 0.0, d.feature_names, d.meta)   # zorunlu sutun yok => hata
    with pytest.raises(Exception):
        run_experiment(conn, _cfg(dates), bad, SPEC, family="t_fail", description="hatali")
    assert count_trials(conn, "t_fail") == 1                                    # gizlenmez
    assert "error" in list_experiments(conn, "t_fail")[0]["result_json"]


def test_too_short_data_raises(conn, ds):
    d, dates = ds
    tiny = Dataset(d.frame[d.frame["date"] < dates[100]].reset_index(drop=True), 5, 0.0, d.feature_names, d.meta)
    with pytest.raises(ValueError, match="fold uretilemedi"):
        run_experiment(conn, _cfg(dates), tiny, SPEC, family="t_short", description="kisa", dry_run=True)


def test_calibrated_spec_runs_and_is_recorded(conn, ds):
    d, dates = ds
    spec = ModelSpec(name="lr_cal", features="all_z", C=0.5, calibration="isotonic")
    run = run_experiment(conn, _cfg(dates), d, spec, family="t_cal", description="kalibre")
    rec = list_experiments(conn, "t_cal")[0]
    assert rec["params_json"]["calibration"] == "isotonic"                       # on kayitta kalibrasyon ayari var
    ri = run.result["run_info"]
    assert ri["n_folds_calibrated"] > 0 and ri["spec"]["calibration"] == "isotonic"
    assert run.preds["date"].max() < dates[600]                                  # holdout yine kilitli
    assert run.preds["prob_up"].between(0.01, 0.99).all()
