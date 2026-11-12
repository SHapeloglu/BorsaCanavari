"""Faz 3.5 laboratuvar kosucusu ve degerlendirmesi (backtest/lab.py) testleri.

Komsu modullere (splits/guard/dataset) bagli degildir: sozlesmeye uyan sentetik veri, kucuk Fold ve guard
taklidi burada uretilir. DB gerektirmez.
"""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from bist_tahmin.backtest import lab
from bist_tahmin.backtest.lab import (
    ModelSpec,
    dataset_baseline_probs,
    evaluate,
    format_result,
    resolve_features,
    run_walk_forward,
)
from bist_tahmin.baselines import rules
from bist_tahmin.features.build import (
    CALENDAR_FEATURES,
    FEATURE_NAMES,
    MARKET_FEATURES,
    STOCK_FEATURES,
    Z_FEATURES,
)

H = 5
CFG = {
    "horizon_days": H,
    "costs": {"commission_bps": None, "spread_bps": None, "sensitivity_grid_bps": [0, 10, 20, 40]},
    "terfi": {
        "min_resolved_dates": 60,
        "min_improvement_brier": 0.005,
        "bootstrap": {"kind": "block_by_date", "resamples": 600, "block_days": 10, "ci": 0.95},
    },
}


# ---------------------------------------------------------------- sentetik veri / taklitler

@dataclass
class Fold:
    fold_id: int
    train_end: pd.Timestamp
    test_dates: pd.DatetimeIndex


class HoldoutLocked(Exception):
    pass


class FakeGuard:
    """guard sozlesmesi: kilitliyken holdout'a degen satirlari dusurur; check() HoldoutLocked firlatir."""

    def __init__(self, start, locked=True):
        self.start = pd.Timestamp(start)
        self.locked = locked

    def filter_frame(self, df, date_col="date", end_col="end_date"):
        if not self.locked:
            return df.copy()
        keep = df[date_col] < self.start
        if end_col is not None:
            keep &= df[end_col] < self.start  # NaT -> False
        return df.loc[keep].copy()

    def check(self, *dates):
        if not self.locked:
            return
        for d in dates:
            for t in pd.DatetimeIndex([d] if np.isscalar(d) or isinstance(d, pd.Timestamp) else d):
                if t >= self.start:
                    raise HoldoutLocked(str(t))


def make_frame(n_days=400, n_tickers=20, beta=0.0, base=0.2, seed=0):
    """Sozlesmeye uyan sentetik dataset cercevesi. beta>0: ret_5_z etiketle iliskili."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2021-01-04", periods=n_days)
    tickers = [f"T{i:02d}" for i in range(n_tickers)]
    n = n_days * n_tickers
    d_col = np.repeat(dates.to_numpy(), n_tickers)
    t_col = np.tile(tickers, n_days)
    frame = pd.DataFrame({"date": d_col, "ticker": t_col})
    for name in FEATURE_NAMES:
        frame[name] = rng.standard_normal(n)
    frame["weekday"] = pd.DatetimeIndex(frame["date"]).day_of_week.astype(float)
    frame["post_holiday"] = 0.0
    sig = frame["ret_5_z"].to_numpy()
    p = 1.0 / (1.0 + np.exp(-(base + beta * sig)))
    up = (rng.random(n) < p).astype(float)
    fwd = (up - 0.5) * 0.04 + rng.standard_normal(n) * 0.02
    frame["fwd_ret"] = fwd
    frame["fwd_rel_ret"] = fwd - pd.Series(fwd).groupby(frame["date"].to_numpy()).transform("mean").to_numpy()
    frame["tradable_ret"] = fwd - 0.001
    frame["label_up"] = up
    frame["label_rel_up"] = (frame["fwd_rel_ret"] > 0).astype(float)
    pos = np.repeat(np.arange(n_days), n_tickers)
    end = np.full(n, np.datetime64("NaT", "ns"), dtype="datetime64[ns]")
    ok = pos + H < n_days
    end[ok] = dates.to_numpy()[pos[ok] + H]
    frame["end_date"] = end
    for c in ("fwd_ret", "fwd_rel_ret", "tradable_ret", "label_up", "label_rel_up"):
        frame.loc[~ok, c] = np.nan
    return frame, dates


def make_folds(dates, first=150, step=20, last=None):
    last = len(dates) - H if last is None else last
    folds = []
    for k, s in enumerate(range(first, last - 1, step)):
        folds.append(Fold(k, dates[s - 1], dates[s:min(s + step, last)]))
    return folds


@pytest.fixture(scope="module")
def signal_data():
    frame, dates = make_frame(beta=1.2, seed=1)
    return frame, dates, make_folds(dates)


@pytest.fixture(scope="module")
def noise_data():
    frame, dates = make_frame(beta=0.0, base=0.2, seed=2)
    return frame, dates, make_folds(dates)


# ---------------------------------------------------------------- ModelSpec / ozellik cozumleme

@pytest.mark.fast
def test_feature_groups_resolve_from_build():
    assert resolve_features("all") == FEATURE_NAMES
    assert resolve_features("stock_z") == Z_FEATURES
    assert resolve_features("stock_raw") == STOCK_FEATURES
    assert resolve_features("market") == MARKET_FEATURES
    # 'weekday' (0-4 sayisal) dogrusal modelde yanlis belirtilmis: varsayilan gruplarda yok, yalniz ikili post_holiday
    assert resolve_features("calendar") == ("post_holiday",)
    assert resolve_features("all_z") == Z_FEATURES + MARKET_FEATURES + ("post_holiday",)
    assert "weekday" not in resolve_features("all_z") and "weekday" in CALENDAR_FEATURES
    assert resolve_features("market+calendar") == MARKET_FEATURES + ("post_holiday",)
    assert resolve_features(["weekday", "ret_1"]) == ("weekday", "ret_1")   # adiyla bilincli secim mumkun
    assert resolve_features(["ret_1", "ma20_z"]) == ("ret_1", "ma20_z")


@pytest.mark.parametrize("bad", ["fwd_ret", "fwd_rel_ret", "tradable_ret", "label_up", "label_rel_up", "end_date"])
@pytest.mark.fast
def test_label_derived_features_rejected(bad):
    with pytest.raises(ValueError, match="etiket"):
        ModelSpec("x", features=("ret_1", bad))
    with pytest.raises(ValueError, match="etiket"):
        resolve_features(bad)  # grup adi olarak da reddedilir


@pytest.mark.fast
def test_unknown_feature_or_group_rejected():
    with pytest.raises(ValueError, match="bilinmeyen"):
        ModelSpec("x", features=("ret_1", "yok_boyle_ozellik"))
    with pytest.raises(ValueError, match="bilinmeyen"):
        ModelSpec("x", features="yok_boyle_grup")
    with pytest.raises(ValueError):
        ModelSpec("x", features=())
    with pytest.raises(ValueError):
        ModelSpec("x", features=("ret_1", "ret_1"))


@pytest.mark.fast
def test_spec_validation_and_params_json():
    with pytest.raises(ValueError):
        ModelSpec("x", label="label_zzz")
    with pytest.raises(ValueError):
        ModelSpec("x", penalty="elasticnet")
    with pytest.raises(ValueError):
        ModelSpec("x", window="rolling")  # rolling_days zorunlu
    with pytest.raises(ValueError):
        ModelSpec("x", rolling_days=30)  # expanding ile kullanilamaz
    with pytest.raises(ValueError):
        ModelSpec("x", C=0.0)
    s = ModelSpec("m", features=["ret_1", "ma20_z"], C=0.5, penalty="l1", window="rolling", rolling_days=60)
    p = json.loads(json.dumps(s.params(), allow_nan=False))
    assert p["features"] == ["ret_1", "ma20_z"] and p["window"] == "rolling" and p["rolling_days"] == 60
    assert p["resolved_features"] == ["ret_1", "ma20_z"]
    assert ModelSpec("g").params()["features"] == "all_z"


# ---------------------------------------------------------------- (1) sinyal / gurultu

def test_signal_beats_base_rate_and_noise_does_not(signal_data, noise_data):
    spec = ModelSpec("lr", features="all_z", C=0.1)
    out = {}
    for tag, (frame, dates, folds) in (("signal", signal_data), ("noise", noise_data)):
        preds = run_walk_forward(frame, spec, folds)
        base = dataset_baseline_probs(frame, horizon_days=H, label="label_up")
        out[tag] = evaluate(preds, base, label="label_up", cfg=CFG, seed=0)
    sig, noi = out["signal"]["main"], out["noise"]["main"]
    assert sig["diff"] < -0.01
    assert sig["excludes_zero"] is True and sig["model_better"] is True
    assert noi["model_better"] is False
    assert not (noi["excludes_zero"] and noi["diff"] < 0)
    assert out["signal"]["ic"]["model"]["mean_ic"] > 0.05
    assert out["signal"]["sufficient"] is True
    # her metrikle ornek ve farkli gun sayisi
    m = out["signal"]["models"]["model"]
    assert m["n"] > 0 and m["n_dates"] > 0


def test_label_rel_up_path_runs(signal_data):
    frame, dates, folds = signal_data
    preds = run_walk_forward(frame, ModelSpec("rel", label="label_rel_up"), folds)
    assert (preds["label"] == preds["label_rel_up"]).all()
    base = dataset_baseline_probs(frame, horizon_days=H, label="label_rel_up")
    res = evaluate(preds, base, label="label_rel_up", cfg=CFG)
    assert res["return_col"] == "fwd_rel_ret"
    with pytest.raises(ValueError, match="uyusmuyor"):
        evaluate(preds, base, label="label_up", cfg=CFG)  # yanlis label argumani


@pytest.mark.fast
def test_output_columns_and_clip(signal_data):
    frame, dates, folds = signal_data
    preds = run_walk_forward(frame, ModelSpec("lr", C=1000.0), folds)
    assert list(preds.columns) == list(lab.PRED_COLUMNS)
    assert preds["prob_up"].between(0.01, 0.99).all()
    assert preds["label"].isin([0.0, 1.0]).all()
    assert not preds.duplicated(["date", "ticker"]).any()
    # test satirlari yalniz fold.test_dates icinde ve etiketi bilinen
    alld = pd.DatetimeIndex(np.concatenate([f.test_dates.to_numpy() for f in folds]))
    assert preds["date"].isin(alld).all()


@pytest.mark.fast
def test_l1_penalty_runs_without_deprecation_warning(signal_data):
    frame, dates, folds = signal_data
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        p1 = run_walk_forward(frame, ModelSpec("l1", penalty="l1", C=0.05), folds[:3])
        p2 = run_walk_forward(frame, ModelSpec("l2", penalty="l2", C=0.05), folds[:3])
    assert not np.allclose(p1["prob_up"], p2["prob_up"])


# ---------------------------------------------------------------- (2) purge

@pytest.mark.fast
def test_training_rows_never_cross_first_test_day(signal_data):
    frame, dates, folds = signal_data
    preds = run_walk_forward(frame, ModelSpec("lr"), folds)
    infos = preds.attrs["run_info"]["folds"]
    assert len(infos) == len(folds)
    for info in infos:
        assert pd.Timestamp(info["train_max_end_date"]) <= pd.Timestamp(info["first_test"])
        assert pd.Timestamp(info["train_max_date"]) <= pd.Timestamp(info["train_end"])
        assert info["n_purged"] > 0  # train_end = ilk testten 1 gun once: son ufuk gunleri purge edilir


@pytest.mark.fast
def test_rolling_window_respects_purge_and_size(signal_data):
    frame, dates, folds = signal_data
    spec = ModelSpec("roll", window="rolling", rolling_days=30)
    preds = run_walk_forward(frame, spec, folds)
    for info in preds.attrs["run_info"]["folds"]:
        assert info["n_train"] <= 30 * 20
        assert pd.Timestamp(info["train_max_end_date"]) <= pd.Timestamp(info["first_test"])
    expanding = run_walk_forward(frame, ModelSpec("exp"), folds)
    last_roll = preds.attrs["run_info"]["folds"][-1]["n_train"]
    last_exp = expanding.attrs["run_info"]["folds"][-1]["n_train"]
    assert last_roll < last_exp


@pytest.mark.fast
def test_violating_fold_raises_assertion(signal_data):
    frame, dates, folds = signal_data
    # egitim sonu ilk test gunune esit: cakisma
    bad = Fold(0, dates[200], dates[200:210])
    with pytest.raises(AssertionError):
        run_walk_forward(frame, ModelSpec("lr"), [bad])
    # egitim sonu test gunlerinden sonra
    bad2 = Fold(0, dates[220], dates[200:210])
    with pytest.raises(AssertionError):
        run_walk_forward(frame, ModelSpec("lr"), [bad2])


@pytest.mark.fast
def test_corrupt_end_date_before_date_raises(signal_data):
    frame, dates, folds = signal_data
    f2 = frame.copy()
    i = f2.index[(f2["date"] == dates[100]) & (f2["ticker"] == "T00")][0]
    f2.loc[i, "end_date"] = dates[90]
    with pytest.raises(AssertionError):
        run_walk_forward(f2, ModelSpec("lr"), folds[:2])


@pytest.mark.fast
def test_malformed_folds_raise_value_error(signal_data):
    frame, dates, folds = signal_data
    with pytest.raises(ValueError):
        run_walk_forward(frame, ModelSpec("lr"), [Fold(0, dates[100], dates[110:110])])
    with pytest.raises(ValueError):
        run_walk_forward(frame, ModelSpec("lr"), [folds[0], folds[0]])  # tekrar eden fold_id
    with pytest.raises(ValueError, match="eksik sutun"):
        run_walk_forward(frame.drop(columns=["ret_5_z"]), ModelSpec("lr"), folds)


# ---------------------------------------------------------------- (3) guard

@pytest.mark.fast
def test_locked_guard_keeps_holdout_out_of_train_and_test(signal_data):
    frame, dates, _ = signal_data
    hold = dates[300]
    guard = FakeGuard(hold, locked=True)
    folds = [Fold(0, dates[249], dates[250:270]), Fold(1, dates[289], dates[290:300])]
    preds = run_walk_forward(frame, ModelSpec("lr"), folds, guard=guard)
    assert (preds["date"] < hold).all() and (preds["end_date"] < hold).all()
    for info in preds.attrs["run_info"]["folds"]:
        assert pd.Timestamp(info["train_max_end_date"]) < hold
    assert preds.attrs["run_info"]["guard_used"] is True
    # son test gunu (299) icin etiket (end=304) holdout'a deger: kilitliyken o satirlar test'ten de duser
    assert preds.loc[preds["fold_id"] == 1, "date"].max() < dates[295]


@pytest.mark.fast
def test_locked_guard_rejects_fold_inside_holdout(signal_data):
    frame, dates, _ = signal_data
    guard = FakeGuard(dates[300], locked=True)
    with pytest.raises(HoldoutLocked):
        run_walk_forward(frame, ModelSpec("lr"), [Fold(0, dates[295], dates[296:310])], guard=guard)
    # kilit acikken ayni fold calisir
    open_guard = FakeGuard(dates[300], locked=False)
    preds = run_walk_forward(frame, ModelSpec("lr"), [Fold(0, dates[295], dates[296:310])], guard=open_guard)
    assert preds["date"].max() >= dates[300]


# ---------------------------------------------------------------- (4) scaler yalniz egitime fit

@pytest.mark.fast
def test_outside_train_rows_do_not_change_predictions(signal_data):
    frame, dates, _ = signal_data
    fold = Fold(0, dates[205], dates[206:216])
    spec = ModelSpec("lr", features="all_z")
    base = run_walk_forward(frame, spec, [fold])

    f2 = frame.copy()
    feats = list(spec.feature_list)
    future = (f2["date"] >= dates[216])
    purged = (f2["date"] >= dates[202]) & (f2["date"] <= dates[205])  # etiketi test gunune tasan satirlar
    outside = future | purged
    f2.loc[outside, feats] = f2.loc[outside, feats] * 1e3 + 7.0
    f2.loc[outside, "label_up"] = 1.0 - f2.loc[outside, "label_up"]
    changed = run_walk_forward(f2, spec, [fold])
    pd.testing.assert_frame_equal(base.reset_index(drop=True), changed.reset_index(drop=True), check_exact=True)

    # duyarlilik kontrolu: egitim satirlari degisirse tahmin degisir (test bos bir kontrol degil)
    f3 = frame.copy()
    train = f3["date"] < dates[150]
    f3.loc[train, feats] = f3.loc[train, feats] * 3.0 + 1.0
    f3.loc[train, "label_up"] = 1.0 - f3.loc[train, "label_up"]
    assert not np.allclose(run_walk_forward(f3, spec, [fold])["prob_up"], base["prob_up"])


# ---------------------------------------------------------------- (5) tek sinifli fold

@pytest.mark.fast
def test_single_class_train_fold_is_skipped_and_counted(signal_data):
    frame, dates, _ = signal_data
    f2 = frame.copy()
    f2.loc[f2["date"] < dates[100], "label_up"] = 1.0
    folds = [Fold(0, dates[89], dates[90:95]), Fold(1, dates[199], dates[200:220])]
    preds = run_walk_forward(f2, ModelSpec("lr"), folds)
    info = preds.attrs["run_info"]
    assert info["n_folds"] == 2 and info["n_folds_run"] == 1 and info["n_folds_skipped"] == 1
    assert info["skipped_folds"][0]["fold_id"] == 0
    assert info["skipped_folds"][0]["reason"] == "single_class_train"
    assert set(preds["fold_id"]) == {1}


@pytest.mark.fast
def test_all_folds_skipped_returns_empty_with_columns(signal_data):
    frame, dates, _ = signal_data
    f2 = frame.copy()
    f2["label_up"] = f2["label_up"].where(f2["label_up"].isna(), 1.0)
    preds = run_walk_forward(f2, ModelSpec("lr"), [Fold(0, dates[199], dates[200:210])])
    assert preds.empty and list(preds.columns) == list(lab.PRED_COLUMNS)
    base = dataset_baseline_probs(f2, horizon_days=H, label="label_up")
    res = evaluate(preds, base, label="label_up", cfg=CFG)
    assert res["main"] is None and res["n_rows"] == 0
    json.dumps(res, allow_nan=False)
    assert "yatırım tavsiyesi değildir" in format_result(res)


# ---------------------------------------------------------------- (6) determinizm

@pytest.mark.fast
def test_deterministic(signal_data):
    frame, dates, folds = signal_data
    for pen in ("l2", "l1"):
        spec = ModelSpec("lr", penalty=pen, C=0.1)
        a = run_walk_forward(frame, spec, folds[:4])
        b = run_walk_forward(frame.sample(frac=1.0, random_state=5), spec, folds[:4])  # girdi sirasindan bagimsiz
        pd.testing.assert_frame_equal(a, b)
    base = dataset_baseline_probs(frame, horizon_days=H, label="label_up", seed=3)
    base2 = dataset_baseline_probs(frame, horizon_days=H, label="label_up", seed=3)
    pd.testing.assert_frame_equal(base, base2)


# ---------------------------------------------------------------- (7) baseline tutarliligi

def _price_world(n_days=290, n_tickers=6, seed=11):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2022-01-03", periods=n_days)
    closes = {}
    for i in range(n_tickers):
        closes[f"S{i}"] = pd.Series(100 * np.exp(np.cumsum(rng.standard_normal(n_days) * 0.015)), index=dates)
    rows = []
    for tk, c in closes.items():
        ret1 = c.pct_change()
        ma20 = c / c.rolling(20).mean() - 1.0
        fut = c.shift(-H)
        up = pd.Series(np.where(fut.notna(), (fut / c - 1.0 > 0.0).astype(float), np.nan), index=dates)
        end = pd.Series(dates, index=dates).shift(-H)
        rows.append(pd.DataFrame({
            "date": dates, "ticker": tk, "ret_1": ret1.to_numpy(), "ma20": ma20.to_numpy(),
            "end_date": end.to_numpy(), "label_up": up.to_numpy(), "label_rel_up": up.to_numpy(),
        }))
    return pd.concat(rows, ignore_index=True), closes, dates


@pytest.mark.parametrize("window_days", [250, 60])
def test_dataset_baselines_match_rules_exactly(window_days):
    frame, closes, dates = _price_world()
    seed, min_samples = 3, 30
    got = dataset_baseline_probs(frame, horizon_days=H, label="label_up", window_days=window_days,
                                 min_samples=min_samples, seed=seed)
    assert len(got) == len(frame)
    cal = SimpleNamespace(days=dates)
    histories = {tk: pd.DataFrame({"close_adj": c}) for tk, c in closes.items()}
    rate_by_day = {
        d: rules.historical_base_rate(histories, d, cal, H, window_days=window_days, min_samples=min_samples)
        for d in dates
    }
    n_none = {n: 0 for n in rules.BASELINES}
    n_val = {n: 0 for n in rules.BASELINES}
    for (_, r) in got.iterrows():
        tk, d = r["ticker"], r["date"]
        c = closes[tk].loc[:d]
        for name in rules.BASELINES:
            exp = rules.baseline_prob(name, tk, c, d, seed=seed, base_rate=rate_by_day[d])
            if exp is None:
                assert np.isnan(r[name]), (name, tk, d)
                n_none[name] += 1
            else:
                assert r[name] == pytest.approx(exp, abs=1e-12, rel=0), (name, tk, d, r[name], exp)
                n_val[name] += 1
    # ucuz dogrulama: testin kendisi anlamli (hem None hem deger gorulur; always_up/random hic None degil)
    assert n_none["always_up"] == 0 and n_none["random"] == 0
    assert n_none["yesterday"] > 0 and n_none["ma20"] > 0 and n_none["base_rate"] > 0
    assert n_val["yesterday"] > 0 and n_val["ma20"] > 0 and n_val["base_rate"] > 0


@pytest.mark.fast
def test_base_rate_uses_only_resolved_labels():
    """Gelecekteki etiketleri bozmak, o tarihe kadarki base_rate'i degistirmemeli."""
    frame, closes, dates = _price_world()
    a = dataset_baseline_probs(frame, horizon_days=H, label="label_up")
    f2 = frame.copy()
    cut = dates[200]
    f2.loc[f2["date"] >= cut, "label_up"] = 1.0 - f2.loc[f2["date"] >= cut, "label_up"]
    b = dataset_baseline_probs(f2, horizon_days=H, label="label_up")
    # cut gunune kadar (dahil) hicbir etiketin end_date'i cut'tan once sonuclanmaz -> cut-1 gunu degismez
    early = a["date"] <= dates[200 + 0]
    assert np.allclose(a.loc[early, "base_rate"], b.loc[early, "base_rate"], equal_nan=True)
    late = a["date"] >= dates[200 + H + 5]
    assert not np.allclose(a.loc[late, "base_rate"], b.loc[late, "base_rate"], equal_nan=True)


@pytest.mark.fast
def test_dataset_baseline_input_validation():
    frame, _, _ = _price_world(n_days=60, n_tickers=2)
    with pytest.raises(ValueError):
        dataset_baseline_probs(frame, horizon_days=H, label="label_zzz")
    with pytest.raises(ValueError):
        dataset_baseline_probs(frame.drop(columns=["ma20"]), horizon_days=H, label="label_up")
    with pytest.raises(ValueError):
        dataset_baseline_probs(frame, horizon_days=0, label="label_up")


# ---------------------------------------------------------------- (8) evaluate: coklu deneme, json

def test_more_trials_widen_ci_and_raise_resamples(signal_data):
    frame, dates, folds = signal_data
    preds = run_walk_forward(frame, ModelSpec("lr"), folds)
    base = dataset_baseline_probs(frame, horizon_days=H, label="label_up")
    res = {n: evaluate(preds, base, label="label_up", cfg=CFG, n_trials=n, seed=7) for n in (1, 5, 50)}
    width = {n: r["main"]["hi"] - r["main"]["lo"] for n, r in res.items()}
    assert width[1] < width[5] < width[50]
    assert res[1]["main"]["diff"] == res[50]["main"]["diff"]
    assert res[1]["ci_level"] == pytest.approx(0.95)
    assert res[50]["ci_level"] == pytest.approx(1 - 0.05 / 50)
    # kuyruk niceligi gurultulenmesin: resamples artirilir ve belirtilir
    assert res[1]["main"]["resamples_used"] == 600 and not res[1]["main"]["resamples_raised"]
    assert res[50]["main"]["resamples_raised"] is True
    assert res[50]["main"]["resamples_used"] >= 20 / (0.05 / 50)
    assert res[50]["main"]["tail_draws"] >= 10
    assert any("resamples" in w for w in res[50]["warnings"])
    for r in res.values():
        s = json.dumps(r, allow_nan=False)  # NaN yok, tamamen serilestirilebilir
        assert json.loads(s)["label"] == "label_up"
        assert lab.result_json(r)


@pytest.mark.fast
def test_resample_cap_is_reported():
    plan = lab._resample_plan(2000, 1e-9)
    assert plan["resamples_used"] == lab.MAX_RESAMPLES and plan["resamples_capped"] is True


@pytest.mark.fast
def test_evaluate_rejects_bad_inputs(signal_data):
    frame, dates, folds = signal_data
    preds = run_walk_forward(frame, ModelSpec("lr"), folds[:2])
    base = dataset_baseline_probs(frame, horizon_days=H, label="label_up")
    with pytest.raises(ValueError):
        evaluate(preds, base, label="label_up", cfg=CFG, n_trials=0)
    with pytest.raises(ValueError):
        evaluate(preds, base, label="label_up", cfg=CFG, alpha=1.5)
    with pytest.raises(ValueError):
        evaluate(preds, base.drop(columns=["base_rate"]), label="label_up", cfg=CFG)
    with pytest.raises(ValueError, match="tekrar"):
        evaluate(pd.concat([preds, preds]), base, label="label_up", cfg=CFG)


@pytest.mark.fast
def test_insufficient_dates_warning_and_text(signal_data):
    frame, dates, folds = signal_data
    preds = run_walk_forward(frame, ModelSpec("lr"), folds[:1])  # 20 gun < 60
    base = dataset_baseline_probs(frame, horizon_days=H, label="label_up")
    res = evaluate(preds, base, label="label_up", cfg=CFG)
    assert res["sufficient"] is False and res["n_dates"] == 20
    assert any("YETERSİZ" in w for w in res["warnings"])
    txt = format_result(res)
    assert "YETERSİZ" in txt and "yatırım tavsiyesi değildir" in txt


def test_result_contents_and_text(signal_data):
    frame, dates, folds = signal_data
    preds = run_walk_forward(frame, ModelSpec("lr_all_z"), folds)
    base = dataset_baseline_probs(frame, horizon_days=H, label="label_up")
    res = evaluate(preds, base, label="label_up", cfg=CFG)
    assert set(res["models"]) == {"model", *rules.BASELINES}
    for m in res["models"].values():
        assert m["n"] > 0 and m["n_dates"] > 0 and m["brier"] is not None
    # model ve base_rate ayni satirlarda: model Brier == models['model']
    assert res["models"]["base_rate"]["model_brier_same_rows"] == pytest.approx(res["models"]["model"]["brier"])
    assert len(res["calibration"]) == lab.CALIBRATION_BINS
    assert res["portfolio"]["grid"] and [r["cost_bps"] for r in res["portfolio"]["grid"]] == [0, 10, 20, 40]
    assert "DOĞRULANMADI" in res["portfolio"]["cost_label"]
    assert res["portfolio"]["grid"][0]["mean_net"] > res["portfolio"]["grid"][-1]["mean_net"]
    assert res["run_info"]["spec"]["name"] == "lr_all_z"
    txt = format_result(res)
    for needle in ("Ana ölçüt", "Günlük Spearman IC", "Kalibrasyon", "Kohort portföyü", "lr_all_z",
                   "yatırım tavsiyesi değildir", "alpha_düzeltilmiş"):
        assert needle in txt
    assert "nan" not in txt.lower().replace("financial", "")
