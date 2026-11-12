"""ModelSpec.calibration ('none'|'isotonic'|'platt') testleri: sizintisizlik, regresyon esitligi, Brier etkisi. DB gerektirmez."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest
from test_lab import FakeGuard, Fold, make_folds, make_frame

from bist_tahmin.backtest import lab
from bist_tahmin.backtest.lab import ModelSpec, fit_fold_model, run_walk_forward
from bist_tahmin.evaluate.metrics import brier

H = 5
FEATS = "all_z"


def _train_arrays(frame, spec, end_day):
    """frame'den (date <= end_day, etiketi kapanmis) egitim dizileri; tarihe gore sirali."""
    f = frame.sort_values(["date", "ticker"], kind="stable")
    m = (f["date"] <= end_day) & f["end_date"].notna() & (f["end_date"] <= end_day) & f[spec.label].notna()
    f = f[m]
    return (f[list(spec.feature_list)].to_numpy(float), f[spec.label].to_numpy(float),
            f["date"].to_numpy(), f["end_date"].to_numpy(), f)


def regime_frame(n_days=420, n_tickers=20, beta_early=3.0, beta_late=0.2, switch=0.45, seed=7):
    """Sinyal gucu zamanla degisen veri: erken donem guclu, gec donem zayif => ham model sonradan asiri iddiali."""
    frame, dates = make_frame(n_days=n_days, n_tickers=n_tickers, beta=0.0, seed=seed)
    rng = np.random.default_rng(seed + 100)
    pos = np.repeat(np.arange(n_days), n_tickers)
    beta = np.where(pos < int(switch * n_days), beta_early, beta_late)
    p = 1.0 / (1.0 + np.exp(-(0.1 + beta * frame["ret_5_z"].to_numpy())))
    up = (rng.random(len(frame)) < p).astype(float)
    ok = frame["label_up"].notna().to_numpy()
    frame.loc[ok, "label_up"] = up[ok]
    return frame, dates


def _brier_of(preds):
    return brier(preds["prob_up"].to_numpy(float), preds["label_up"].to_numpy(float))


# ---------------------------------------------------------------- spec

@pytest.mark.fast
def test_spec_validation_and_params():
    assert ModelSpec("m").calibration == "none" and ModelSpec("m").params()["calibration"] == "none"
    assert ModelSpec("m", calibration="isotonic").params()["calibration"] == "isotonic"
    json.dumps(ModelSpec("m", calibration="platt").params(), allow_nan=False)
    with pytest.raises(ValueError, match="calibration"):
        ModelSpec("m", calibration="beta")


# ---------------------------------------------------------------- 'none' regresyon esitligi

@pytest.mark.fast
def test_none_is_identical_to_plain_pipeline_and_default():
    frame, dates = make_frame(n_days=330, n_tickers=12, beta=0.8, seed=11)
    folds = make_folds(dates, first=150, step=30)
    base = run_walk_forward(frame, ModelSpec("m", features=FEATS, C=0.5), folds)
    none = run_walk_forward(frame, ModelSpec("m", features=FEATS, C=0.5, calibration="none"), folds)
    pd.testing.assert_frame_equal(base, none)
    # bagimsiz referans: ilk fold icin dogrudan Pipeline fit/predict (eski kodun yaptigi is)
    spec = ModelSpec("m", features=FEATS, C=0.5)
    f0 = folds[0]
    x, y, _, _, tr = _train_arrays(frame, spec, f0.test_dates[0])
    pre = (tr["date"] <= f0.train_end).to_numpy()
    x, y = x[pre], y[pre]
    pipe = lab._make_pipeline(spec, 0).fit(x, y.astype(int))
    te = frame[frame["date"].isin(f0.test_dates) & frame["label_up"].notna()].sort_values(["date", "ticker"])
    ref = np.clip(pipe.predict_proba(te[list(spec.feature_list)].to_numpy(float))[:, 1], 0.01, 0.99)
    got = none[none["fold_id"] == 0].sort_values(["date", "ticker"])["prob_up"].to_numpy()
    np.testing.assert_allclose(got, ref, rtol=0, atol=1e-12)
    assert "n_folds_calibrated" not in none.attrs["run_info"]


# ---------------------------------------------------------------- kalibrator yalniz egitimin son kismina fit

@pytest.mark.parametrize("kind", ["isotonic", "platt"])
@pytest.mark.fast
def test_calibrator_fit_only_on_last_part_with_gap(kind):
    frame, dates = make_frame(n_days=300, n_tickers=15, beta=1.0, seed=5)
    spec = ModelSpec("m", features=FEATS, calibration=kind)
    x, y, d, e, f = _train_arrays(frame, spec, dates[199])
    fm = fit_fold_model(spec, 0, x, y, d, e)
    info = fm.info
    assert info["applied"] and info["n_cal_rows"] >= 300
    uniq = pd.DatetimeIndex(d).unique().sort_values()
    first_cal = pd.Timestamp(info["cal_first_date"])
    assert first_cal == uniq[int(np.floor(0.8 * len(uniq)))]
    assert info["n_cal_days"] == int((uniq >= first_cal).sum())
    # fit son gunu ile kalibrasyon basi arasi >= horizon islem gunu
    gap = int(((dates > pd.Timestamp(info["fit_last_date"])) & (dates < first_cal)).sum()) + 1
    assert gap >= H
    # egitim ayni, TEST degisince kalibrator degismez
    ref = fm.calibrator.X_thresholds_.copy() if kind == "isotonic" else fm.calibrator.lr.coef_.copy()
    rng = np.random.default_rng(0)
    p1 = fm.predict(rng.standard_normal((50, x.shape[1])))
    fm.predict(rng.standard_normal((500, x.shape[1])) * 5)
    p2 = fm.predict(np.zeros((50, x.shape[1])))
    cur = fm.calibrator.X_thresholds_ if kind == "isotonic" else fm.calibrator.lr.coef_
    np.testing.assert_array_equal(ref, cur)
    assert p1.min() >= 0.01 and p1.max() <= 0.99 and p2.min() >= 0.01 and p2.max() <= 0.99
    # kalibratorun modeli fit satirlari disinda (kalibrasyon satirlari ham model fit'ine girmedi)
    fit_only = lab._make_pipeline(spec, 0)
    m = (pd.DatetimeIndex(d) < first_cal) & (pd.DatetimeIndex(e) <= first_cal)
    fit_only.fit(x[m], y[m].astype(int))
    np.testing.assert_allclose(fm.pipeline.predict_proba(x[:40])[:, 1], fit_only.predict_proba(x[:40])[:, 1])


@pytest.mark.fast
def test_test_changes_do_not_change_other_test_predictions_or_calibrator():
    frame, dates = make_frame(n_days=330, n_tickers=12, beta=1.0, seed=21)
    folds = make_folds(dates, first=200, step=40)
    spec = ModelSpec("m", features=FEATS, calibration="isotonic")
    base = run_walk_forward(frame, spec, folds[:1])
    f2 = frame.copy()
    test_days = folds[0].test_dates
    # son test gununun ozellikleri/etiketleri/getirileri degisir; ilk fold'un diger test tahminleri ayni kalmali
    mut = f2["date"] == test_days[-1]
    f2.loc[mut, list(spec.feature_list)] = 9.0
    f2.loc[mut, ["label_up", "fwd_ret"]] = [1.0, 0.5]
    out = run_walk_forward(f2, spec, folds[:1])
    keep = base["date"] != test_days[-1]
    pd.testing.assert_series_equal(base.loc[keep, "prob_up"].reset_index(drop=True),
                                   out.loc[out["date"] != test_days[-1], "prob_up"].reset_index(drop=True))
    assert base.attrs["run_info"]["folds"][0]["calibration"] == out.attrs["run_info"]["folds"][0]["calibration"]


# ---------------------------------------------------------------- gelecek bilgi sizmaz

@pytest.mark.parametrize("kind", ["isotonic", "platt"])
@pytest.mark.fast
def test_no_future_leak_into_model_or_calibrator(kind):
    frame, dates = make_frame(n_days=340, n_tickers=12, beta=1.0, seed=33)
    folds = make_folds(dates, first=220, step=30)[:1]
    spec = ModelSpec("m", features=FEATS, calibration=kind)
    fold = folds[0]
    base = run_walk_forward(frame, spec, folds)
    info = base.attrs["run_info"]["folds"][0]["calibration"]
    assert info["applied"]
    f2 = frame.copy()
    # (a) test donemi ve sonrasinin ETIKETLERI/getirileri, (b) purge boslugundaki (date<=train_end, end_date>ilk test) etiketler,
    # (c) fit-kalibrasyon arasi bosluktaki satirlarin etiketleri bozulur: tahminler DEGISMEMELI.
    cols = ["label_up", "label_rel_up", "fwd_ret", "fwd_rel_ret", "tradable_ret"]
    after = f2["date"] >= fold.test_dates[0]
    purged = (f2["date"] <= fold.train_end) & (f2["end_date"] > fold.test_dates[0])
    first_cal = pd.Timestamp(info["cal_first_date"])
    fit_gap = (f2["date"] < first_cal) & (f2["end_date"] > first_cal)
    assert purged.sum() > 0 and fit_gap.sum() > 0
    keep_label = f2.loc[~after, "label_up"].copy()
    f2.loc[purged | fit_gap, cols] = f2.loc[purged | fit_gap, cols].rsub(1.0)          # etiketleri ters cevir
    # test satirlarinin KENDI etiketi degerlendirme icindir; tahmine etki etmemeli (yalniz prob_up karsilastirilir)
    f2.loc[after, "label_up"] = 1.0 - f2.loc[after, "label_up"]
    out = run_walk_forward(f2, spec, folds)
    key = ["date", "ticker"]
    a = base.sort_values(key)["prob_up"].to_numpy()
    b = out.sort_values(key)["prob_up"].to_numpy()
    np.testing.assert_array_equal(a, b)
    assert keep_label.notna().any()


@pytest.mark.fast
def test_future_features_do_not_enter_training():
    """Test gunleri ozellikleri kalibratore girmez: test ozelliklerini cok degistirmek egitim bilgisini (info) degistirmez."""
    frame, dates = make_frame(n_days=330, n_tickers=12, beta=1.0, seed=44)
    folds = make_folds(dates, first=200, step=40)[:1]
    spec = ModelSpec("m", features=FEATS, calibration="platt")
    a = run_walk_forward(frame, spec, folds).attrs["run_info"]["folds"][0]["calibration"]
    f2 = frame.copy()
    f2.loc[f2["date"] >= folds[0].test_dates[0], list(spec.feature_list)] = 123.0
    b = run_walk_forward(f2, spec, folds).attrs["run_info"]["folds"][0]["calibration"]
    assert a == b


# ---------------------------------------------------------------- guard + kucuk kalibrasyon seti

@pytest.mark.fast
def test_runs_with_guard_and_small_cal_set_falls_back_to_raw():
    frame, dates = make_frame(n_days=330, n_tickers=12, beta=1.0, seed=2)
    folds = make_folds(dates, first=200, step=40, last=295)
    spec = ModelSpec("m", features=FEATS, calibration="isotonic")
    out = run_walk_forward(frame, spec, folds, guard=FakeGuard(dates[300]))
    assert out["date"].max() < dates[300]
    ri = out.attrs["run_info"]
    assert ri["n_folds_calibrated"] == ri["n_folds_run"] and ri["n_folds_calibration_skipped"] == 0
    # az gun / satir: 12 hisse x ~30 egitim gunu -> kalibrasyon satiri << 300 -> ham model
    small_folds = [Fold(0, dates[39], dates[45:50])]
    small = run_walk_forward(frame, spec, small_folds)
    ri = small.attrs["run_info"]
    assert ri["n_folds_calibration_skipped"] == 1 and ri["calibration_skip_reasons"] == ["too_few_cal_rows"]
    raw = run_walk_forward(frame, ModelSpec("m", features=FEATS), small_folds)
    np.testing.assert_array_equal(small["prob_up"].to_numpy(), raw["prob_up"].to_numpy())  # ham model, tum egitimle


def test_skip_is_reported_in_evaluate_and_format():
    frame, dates = make_frame(n_days=330, n_tickers=12, beta=1.0, seed=2)
    spec = ModelSpec("m", features=FEATS, calibration="platt")
    folds = make_folds(dates, first=60, step=40)[:3]   # ~60 gun x 12 = 720 satir, %20 ~144 < 300
    preds = run_walk_forward(frame, spec, folds)
    base = lab.dataset_baseline_probs(frame, horizon_days=H, label="label_up")
    cfg = {"horizon_days": H, "terfi": {"bootstrap": {"resamples": 200, "block_days": 10}}}
    res = lab.evaluate(preds, base, label="label_up", cfg=cfg)
    assert any("kalibrasyon yapılmadı" in w for w in res["warnings"])
    assert "Kalibrasyon: platt" in lab.format_result(res)
    json.loads(lab.result_json(res))


# ---------------------------------------------------------------- Brier etkisi (tohumlu)

@pytest.mark.parametrize("kind", ["isotonic", "platt"])
@pytest.mark.fast
def test_calibration_reduces_brier_when_model_is_overconfident(kind):
    """Sinyal gucu zamanla zayifliyor: ham model eski guclu sinyale gore asiri iddiali; yeni donem kalibrasyonu duzeltir."""
    frame, dates = regime_frame()
    folds = make_folds(dates, first=260, step=40)
    raw = run_walk_forward(frame, ModelSpec("raw", features=FEATS, C=10.0), folds)
    cal = run_walk_forward(frame, ModelSpec("cal", features=FEATS, C=10.0, calibration=kind), folds)
    assert cal.attrs["run_info"]["n_folds_calibrated"] == cal.attrs["run_info"]["n_folds_run"] > 0
    assert len(raw) == len(cal)
    assert _brier_of(cal) < _brier_of(raw) - 0.005, (kind, _brier_of(cal), _brier_of(raw))


@pytest.mark.parametrize("kind", ["isotonic", "platt"])
@pytest.mark.fast
def test_calibration_does_not_hurt_when_already_calibrated(kind):
    """Duragan ve iyi kalibre veri: kalibrasyon Brier'i belirgin bozmaz (parcali sabit/ek parametre maliyeti kucuk)."""
    frame, dates = make_frame(n_days=420, n_tickers=20, beta=1.0, seed=9)
    folds = make_folds(dates, first=260, step=40)
    raw = run_walk_forward(frame, ModelSpec("raw", features=FEATS, C=1.0), folds)
    cal = run_walk_forward(frame, ModelSpec("cal", features=FEATS, C=1.0, calibration=kind), folds)
    assert _brier_of(cal) < _brier_of(raw) + 0.003, (kind, _brier_of(cal), _brier_of(raw))
