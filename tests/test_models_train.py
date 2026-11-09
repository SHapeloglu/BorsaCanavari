"""models.train: purge'lu egitim satirlari, sizintisizlik kaniti, kayit ve yeniden egitim zamani."""

from __future__ import annotations

import copy
import json
import uuid

import numpy as np
import pandas as pd
import psycopg
import pytest
from conftest import APP_PASSWORD

from bist_tahmin.backtest import lab
from bist_tahmin.backtest.dataset import Dataset
from bist_tahmin.calendar import TradingCalendar
from bist_tahmin.config import load_config
from bist_tahmin.db import APP_ROLE, run_init, with_overrides
from bist_tahmin.features.build import FEATURE_VERSION
from bist_tahmin.models import logreg, registry as reg, train

CFG = load_config("config/default.yaml")
H = 5
FEATS = ["ret_1_z", "ret_5_z", "mkt_ret_1"]
TICKERS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
N_DAYS = 260
DAYS = pd.bdate_range("2024-01-01", periods=N_DAYS)
SPEC = lab.ModelSpec(name="t1", features=FEATS, C=1.0)


def make_frame(seed=0, n_days=N_DAYS) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    days = DAYS[:n_days]
    rows = []
    for i, d in enumerate(days):
        mkt = rng.normal()
        for t in TICKERS:
            z1, z5 = rng.normal(), rng.normal()
            p = 1 / (1 + np.exp(-(1.5 * z1 + 0.3 * mkt)))
            y = float(rng.random() < p)
            end = days[i + H] if i + H < n_days else pd.NaT
            rows.append({
                "date": d, "ticker": t, "ret_1_z": z1, "ret_5_z": z5, "mkt_ret_1": mkt, "end_date": end,
                "label_up": y if pd.notna(end) else np.nan,
                "label_rel_up": float(rng.random() < 0.5) if pd.notna(end) else np.nan,
                "fwd_ret": 0.0, "fwd_rel_ret": 0.0, "tradable_ret": 0.0,
            })
    return pd.DataFrame(rows)


def _params_eq(a: dict, b: dict) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


AS_OF = DAYS[200]
TRAIN_END = DAYS[195]


def test_training_rows_boundaries():
    f = make_frame()
    rows = train.training_rows(f, SPEC, AS_OF, purge_days=5, horizon_days=H)
    assert rows["date"].max() == TRAIN_END
    assert (rows["end_date"] <= AS_OF).all()
    assert len(rows) == 196 * len(TICKERS)
    assert rows[SPEC.label].notna().all()


def test_training_rows_drops_nan_label_and_features():
    f = make_frame()
    f.loc[f.index[10], "ret_1_z"] = np.nan
    f.loc[f.index[11], "ret_5_z"] = np.inf
    f.loc[f.index[12], "label_up"] = np.nan
    rows = train.training_rows(f, SPEC, AS_OF, purge_days=5, horizon_days=H)
    assert len(rows) == 196 * len(TICKERS) - 3


def test_purge_shorter_than_horizon_rejected():
    f = make_frame()
    with pytest.raises(ValueError, match="sizinti"):
        train.training_rows(f, SPEC, AS_OF, purge_days=3, horizon_days=H)


def test_label_known_only_rows_even_with_larger_purge():
    f = make_frame()
    rows = train.training_rows(f, SPEC, AS_OF, purge_days=8, horizon_days=H)
    assert rows["date"].max() == DAYS[192]
    assert (rows["end_date"] <= AS_OF).all()


def test_missing_column_rejected():
    f = make_frame().drop(columns=["ret_5_z"])
    with pytest.raises(ValueError, match="eksik"):
        train.training_rows(f, SPEC, AS_OF, purge_days=5, horizon_days=H)


def test_changing_anything_after_as_of_does_not_change_model():
    f = make_frame()
    base, te, n = train.train_model(f, SPEC, AS_OF, horizon_days=H, purge_days=5)
    assert te == TRAIN_END and n == 196 * len(TICKERS)

    g = f.copy()
    after = g["date"] > AS_OF                       # as_of sonrasi gunler: ozellik, etiket, fiyat turevleri
    pending = (g["date"] > TRAIN_END) & ~after      # etiketi as_of'ta henuz bilinmeyen son 5 gun
    rng = np.random.default_rng(99)
    for mask in (after, pending):
        for c in ("ret_1_z", "ret_5_z", "mkt_ret_1"):
            g.loc[mask, c] = rng.normal(size=int(mask.sum())) * 50
        g.loc[mask, "label_up"] = 1.0 - g.loc[mask, "label_up"]
        g.loc[mask, "label_rel_up"] = 1.0
        g.loc[mask, "fwd_ret"] = 123.0
        g.loc[mask, "tradable_ret"] = -9.0
    changed, te2, n2 = train.train_model(g, SPEC, AS_OF, horizon_days=H, purge_days=5)
    assert (te2, n2) == (te, n) and _params_eq(base, changed)

    # duyarlilik kontrolu: bilinen bir egitim satirinin etiketi degisirse model DEGISIR
    h = f.copy()
    known = h.index[h["date"] <= TRAIN_END][:200]
    h.loc[known, "label_up"] = 1.0 - h.loc[known, "label_up"]
    other, _, _ = train.train_model(h, SPEC, AS_OF, horizon_days=H, purge_days=5)
    assert not _params_eq(base, other)


def test_train_model_learns_signal_and_params_are_json():
    params, _, _ = train.train_model(make_frame(), SPEC, AS_OF, horizon_days=H, purge_days=5, seed=4)
    assert params["features"] == FEATS and params["label"] == "label_up" and params["C"] == 1.0
    assert params["coef"][0] > 0.5                                  # ret_1_z gercek sinyal
    ex = params["extra"]
    assert ex["spec"]["name"] == "t1" and ex["as_of"] == str(AS_OF.date()) and ex["seed"] == 4
    assert ex["train_end"] == str(TRAIN_END.date()) and ex["purge_days"] == 5
    json.dumps(params, allow_nan=False)


def test_too_few_rows_and_single_class_raise():
    f = make_frame()
    with pytest.raises(ValueError, match="yetersiz"):
        train.train_model(f, SPEC, DAYS[50], horizon_days=H, purge_days=5)
    with pytest.raises(ValueError, match="gun yok"):
        train.train_model(f, SPEC, DAYS[2], horizon_days=H, purge_days=5)
    g = f.copy()
    g["label_up"] = g["label_up"].where(g["label_up"].isna(), 1.0)
    with pytest.raises(ValueError, match="tek sinif"):
        train.train_model(g, SPEC, AS_OF, horizon_days=H, purge_days=5)


def test_rolling_window_limits_training_days():
    f = make_frame()
    spec = lab.ModelSpec(name="r", features=FEATS, window="rolling", rolling_days=100)
    rows = train.training_rows(f, spec, AS_OF, purge_days=5, horizon_days=H)
    assert rows["date"].nunique() == 100 and rows["date"].max() == TRAIN_END


def test_rel_label_spec_uses_rel_label():
    f = make_frame()
    spec = lab.ModelSpec(name="rel", label="label_rel_up", features=FEATS)
    params, _, _ = train.train_model(f, spec, AS_OF, horizon_days=H, purge_days=5)
    assert params["label"] == "label_rel_up"


# ---------------------------------------------------------------- DB

@pytest.fixture()
def db(admin_conninfo):
    name = f"bist_train_{uuid.uuid4().hex[:8]}"
    run_init(admin_conninfo, db_name=name, app_password=APP_PASSWORD)
    info = with_overrides(admin_conninfo, dbname=name, user=APP_ROLE, password=APP_PASSWORD)
    with psycopg.connect(info, autocommit=True) as conn:
        yield conn


def make_ds(frame=None, **meta_over) -> Dataset:
    f = make_frame() if frame is None else frame
    meta = {"feature_version": FEATURE_VERSION, "date_min": "2024-01-01", "date_max": str(DAYS[-1].date()),
            "n_rows": len(f), "built_at": "2026-11-09T00:00:00+00:00"}
    meta.update(meta_over)
    return Dataset(frame=f, horizon_days=H, label_threshold=0.0, feature_names=FEATS, meta=meta)


def test_train_and_register_roundtrip(db):
    rec = train.train_and_register(db, CFG, make_ds(), SPEC, AS_OF, seed=2)
    assert rec.version == f"logreg_t1@{AS_OF:%Y-%m-%d}" and rec.role == "challenger"
    assert rec.train_end == TRAIN_END.date()
    assert reg.get_model(db, rec.version) == rec
    ex = rec.params["extra"]
    assert ex["feature_version"] == FEATURE_VERSION and ex["dataset"]["date_max"] == str(DAYS[-1].date())
    assert ex["spec"]["C"] == 1.0 and ex["holdout_start"] == "2026-05-01" and ex["train_includes_holdout"] is False
    # DB'den okunan params ile tahmin, yerel egitimle ayni
    local, _, _ = train.train_model(make_frame(), SPEC, AS_OF, horizon_days=H, purge_days=5, seed=2)
    df = make_frame().iloc[:50]
    assert np.max(np.abs(logreg.predict_frame(rec.params, df) - logreg.predict_frame(local, df))) < 1e-12
    n = db.execute("SELECT count(*) FROM experiments WHERE family = %s AND result_json IS NOT NULL",
                   (train.EXPERIMENT_FAMILY,)).fetchone()[0]
    assert n == 1


def test_train_and_register_duplicate_and_prefix_role(db):
    ds = make_ds()
    train.train_and_register(db, CFG, ds, SPEC, AS_OF)
    with pytest.raises(ValueError, match="zaten kayitli"):
        train.train_and_register(db, CFG, ds, SPEC, AS_OF)
    rec = train.train_and_register(db, CFG, ds, SPEC, AS_OF, version_prefix="lr2", role="champion")
    assert rec.version.startswith("lr2_t1@") and rec.role == "champion"
    # basarisiz kayit deney satiri birakmaz
    assert db.execute("SELECT count(*) FROM experiments").fetchone()[0] == 2


def test_train_and_register_rejects_stale_or_mismatched_dataset(db):
    f = make_frame()
    with pytest.raises(ValueError, match="kapsamiyor"):
        train.train_and_register(db, CFG, make_ds(f[f["date"] <= DAYS[150]]), SPEC, AS_OF)
    with pytest.raises(ValueError, match="surumu"):
        train.train_and_register(db, CFG, make_ds(feature_version="f0"), SPEC, AS_OF)
    cfg = copy.deepcopy(CFG)
    cfg["horizon_days"] = 10
    with pytest.raises(ValueError, match="ufku"):
        train.train_and_register(db, cfg, make_ds(), SPEC, AS_OF)
    assert reg.list_models(db) == []


def test_train_includes_holdout_flag(db):
    cfg = copy.deepcopy(CFG)
    cfg["holdout"]["holdout_start"] = str(DAYS[100].date())
    rec = train.train_and_register(db, cfg, make_ds(), SPEC, AS_OF)
    assert rec.params["extra"]["train_includes_holdout"] is True


def test_due_for_retrain(db):
    cal = TradingCalendar(pd.bdate_range("2023-06-01", periods=700))
    asof = pd.Timestamp("2025-01-15")
    assert train.due_for_retrain(db, CFG, asof, cal) is True          # hic model yok
    end = cal.days[cal.days.get_loc(pd.Timestamp("2025-01-15")) - 20]  # 20 islem gunu once
    reg.register_model(db, "m1", {"k": 1}, end.date())
    assert train.due_for_retrain(db, CFG, asof, cal) is True          # tam 20 gun
    assert train.due_for_retrain(db, CFG, cal.days[cal.days.get_loc(asof) - 1], cal) is False   # 19 gun
    assert train.due_for_retrain(db, CFG, end, cal) is False
    # retired olsa da ritim sayilir; daha yeni bir model ritmi sifirlar
    reg.retire(db, "m1")
    assert train.due_for_retrain(db, CFG, asof, cal) is True
    newer = cal.days[cal.days.get_loc(asof) - 3]
    reg.register_model(db, "m2", {"k": 1}, newer.date())
    assert train.due_for_retrain(db, CFG, asof, cal) is False


def test_due_for_retrain_outside_calendar_falls_back_to_weekdays(db):
    cal = TradingCalendar(pd.bdate_range("2025-01-01", periods=30))
    reg.register_model(db, "old", {"k": 1}, pd.Timestamp("2020-01-02").date())
    assert train.due_for_retrain(db, CFG, pd.Timestamp("2025-01-10"), cal) is True
