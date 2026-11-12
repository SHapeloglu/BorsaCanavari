"""models.logreg: JSON-only parametreler, saf numpy tahmin, lab pipeline'i ile esdegerlik."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from bist_tahmin.backtest import lab
from bist_tahmin.models import logreg

NAMES = ["f1", "f2", "f3", "f4"]


def _data(n=800, seed=0):
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n, 4)) * np.array([1.0, 5.0, 0.2, 100.0]) + np.array([0.0, 3.0, -1.0, 50.0])
    z = 0.6 * (X[:, 0]) - 0.3 * (X[:, 1] - 3.0) / 5.0 + 0.2 * (X[:, 3] - 50.0) / 100.0
    y = (rng.random(n) < 1 / (1 + np.exp(-z))).astype(float)
    return X, y


@pytest.mark.parametrize("penalty,C", [("l2", 1.0), ("l2", 0.05), ("l1", 0.5)])
@pytest.mark.fast
def test_matches_lab_pipeline_within_1e9(penalty, C):
    X, y = _data()
    params = logreg.fit_logreg(X, y, NAMES, C=C, penalty=penalty, label="label_up", seed=3)
    spec = lab.ModelSpec(name="t", C=C, penalty=penalty)
    ref = lab._make_pipeline(spec, 3).fit(X, y.astype(int)).predict_proba(X)[:, 1]
    raw = logreg._raw_prob(params, X)
    assert np.max(np.abs(raw - ref)) < 1e-9
    df = pd.DataFrame(X, columns=NAMES)
    clipped = logreg.predict_frame(params, df)
    assert np.max(np.abs(clipped - np.clip(ref, 0.01, 0.99))) < 1e-9
    # tek satir yolu da ayni
    rows = [logreg.predict_prob(params, dict(zip(NAMES, X[i], strict=True))) for i in range(25)]
    assert np.max(np.abs(np.array(rows) - clipped[:25])) < 1e-12


@pytest.mark.fast
def test_params_keys_and_json_roundtrip():
    X, y = _data()
    params = logreg.fit_logreg(X, y, NAMES, C=1.0, penalty="l2", label="label_rel_up", extra={"a": [1, 2], "b": "x"})
    assert {"kind", "features", "mean", "scale", "coef", "intercept", "C", "penalty", "label", "n_train",
            "prob_clip", "extra"} <= set(params)
    assert params["kind"] == logreg.KIND == "logreg_v1"
    assert params["n_train"] == len(y) and params["prob_clip"] == [0.01, 0.99]
    assert params["label"] == "label_rel_up" and params["extra"] == {"a": [1, 2], "b": "x"}
    text = json.dumps(params, allow_nan=False)
    back = json.loads(text)
    df = pd.DataFrame(X, columns=NAMES)
    assert np.array_equal(logreg.predict_frame(params, df), logreg.predict_frame(back, df))


@pytest.mark.fast
def test_prob_is_clipped():
    X, y = _data()
    params = logreg.fit_logreg(X, y, NAMES, C=1.0, penalty="l2", label="label_up")
    far = {n: 1e6 for n in NAMES}
    assert logreg.predict_prob(params, far) in (0.01, 0.99)
    assert 0.01 <= logreg.predict_prob(params, {n: -1e6 for n in NAMES}) <= 0.99


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), None, "x", True])
@pytest.mark.fast
def test_missing_or_bad_feature_raises(bad):
    X, y = _data()
    params = logreg.fit_logreg(X, y, NAMES, C=1.0, penalty="l2", label="label_up")
    feats = dict(zip(NAMES, X[0], strict=True))
    feats["f2"] = bad
    with pytest.raises(ValueError):
        logreg.predict_prob(params, feats)
    feats.pop("f2")
    with pytest.raises(ValueError, match="eksik"):
        logreg.predict_prob(params, feats)


@pytest.mark.fast
def test_predict_frame_rejects_nan_and_missing_columns():
    X, y = _data()
    params = logreg.fit_logreg(X, y, NAMES, C=1.0, penalty="l2", label="label_up")
    df = pd.DataFrame(X, columns=NAMES)
    bad = df.copy()
    bad.iloc[3, 1] = np.nan
    with pytest.raises(ValueError, match="NaN"):
        logreg.predict_frame(params, bad)
    with pytest.raises(ValueError, match="eksik"):
        logreg.predict_frame(params, df.drop(columns=["f4"]))
    # fazladan sutun sorun degil
    assert len(logreg.predict_frame(params, df.assign(other=1.0))) == len(df)


@pytest.mark.fast
def test_fit_validation():
    X, y = _data(200)
    kw = dict(C=1.0, penalty="l2", label="label_up")
    with pytest.raises(ValueError, match="tek sinif"):
        logreg.fit_logreg(X, np.ones(len(y)), NAMES, **kw)
    Xn = X.copy()
    Xn[0, 0] = np.nan
    with pytest.raises(ValueError, match="NaN"):
        logreg.fit_logreg(Xn, y, NAMES, **kw)
    with pytest.raises(ValueError):
        logreg.fit_logreg(X, y, NAMES[:3], **kw)
    with pytest.raises(ValueError, match="tekrar"):
        logreg.fit_logreg(X, y, ["a", "a", "b", "c"], **kw)
    with pytest.raises(ValueError, match="turevi"):
        logreg.fit_logreg(X, y, ["f1", "f2", "f3", "fwd_ret"], **kw)
    with pytest.raises(ValueError, match="JSON"):
        logreg.fit_logreg(X, y, NAMES, extra={"x": float("nan")}, **kw)
    with pytest.raises(ValueError):
        logreg.fit_logreg(X, y, NAMES, C=-1.0, penalty="l2", label="label_up")
    with pytest.raises(ValueError):
        logreg.fit_logreg(X, y, NAMES, C=1.0, penalty="l2", label="nope")


@pytest.mark.fast
def test_corrupt_params_rejected():
    X, y = _data(200)
    params = logreg.fit_logreg(X, y, NAMES, C=1.0, penalty="l2", label="label_up")
    row = dict(zip(NAMES, X[0], strict=True))
    for mutate in (lambda p: p.update(kind="other"), lambda p: p.update(coef=p["coef"][:-1]),
                   lambda p: p.update(scale=[0.0] * 4), lambda p: p.pop("intercept")):
        p = json.loads(json.dumps(params))
        mutate(p)
        with pytest.raises(ValueError):
            logreg.predict_prob(p, row)


@pytest.mark.fast
def test_constant_column_is_safe():
    X, y = _data(300)
    X[:, 2] = 7.0  # sabit sutun: StandardScaler scale_=1
    params = logreg.fit_logreg(X, y, NAMES, C=1.0, penalty="l2", label="label_up")
    spec = lab.ModelSpec(name="t", C=1.0, penalty="l2")
    ref = lab._make_pipeline(spec, 0).fit(X, y.astype(int)).predict_proba(X)[:, 1]
    assert np.max(np.abs(logreg._raw_prob(params, X) - ref)) < 1e-9
