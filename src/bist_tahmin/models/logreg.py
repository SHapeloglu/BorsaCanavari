"""Lojistik regresyon: JSON-only parametreler ve saf numpy tahmin (PROJE.md Bolum 3, 6).

Egitim `backtest.lab._make_pipeline` ile AYNI ayarlarla (StandardScaler + LogisticRegression; ayni solver,
max_iter, l1_ratio/penalty mantigi) yapilir ve katsayilar cikarilir. Tahmin sklearn'e baglanmadan numpy ile
hesaplanir; boylece canli tahmin yolu pickle'a ya da sklearn surumune bagimli olmaz ve `models.params_json`
JSONB'sinde (NaN/sonsuz yok) saklanabilir.

Hicbir sey uydurulmaz: eksik/NaN/sonsuz ozellik tahmin sirasinda ValueError verir.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd

from ..backtest import lab

KIND = "logreg_v1"
PROB_CLIP: tuple[float, float] = tuple(lab.PROB_CLIP)  # type: ignore[assignment]
MIN_FIT_ROWS = 2
# Etiket/gelecek turevi sutunlar ozellik olamaz (lab ile ayni kural; lab'in ozel yardimcisina baglanmamak icin kopya)
_FORBIDDEN_EXACT = frozenset({"tradable_ret", "end_date"})
_FORBIDDEN_PREFIXES = ("fwd_", "label_")


# ---------------------------------------------------------------- egitim

def _clean_names(feature_names: Sequence[str]) -> list[str]:
    if isinstance(feature_names, str):
        raise ValueError("feature_names ad listesi olmali (tek metin degil)")
    names = [str(n) for n in feature_names]
    if not names:
        raise ValueError("ozellik listesi bos olamaz")
    if len(set(names)) != len(names):
        raise ValueError("ozellik listesinde tekrar eden ad var")
    for n in names:
        if n in _FORBIDDEN_EXACT or n.startswith(_FORBIDDEN_PREFIXES):
            raise ValueError(f"etiket/gelecek turevi sutun ozellik olamaz: {n!r}")
    return names


def _json_safe(obj, what: str):
    """allow_nan=False ile serilestirilebilirligi dogrular ve JSON-esdeger bir kopya dondurur."""
    try:
        return json.loads(json.dumps(obj, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what} JSON'a (NaN/sonsuz olmadan) cevrilemiyor: {exc}") from exc


def fit_logreg(
    X: np.ndarray,
    y: np.ndarray,
    feature_names: Sequence[str],
    *,
    C: float,
    penalty: str,
    label: str,
    seed: int = 0,
    extra: dict | None = None,
) -> dict:
    """StandardScaler + LogisticRegression fit eder, JSON-only `params_json` sozlugu dondurur.

    X: (n, k) sonlu matris; y: 0/1 etiket (iki sinif da bulunmali). Sozluk anahtarlari: kind, features, mean,
    scale, coef, intercept, C, penalty, label, n_train, prob_clip, extra.
    """
    names = _clean_names(feature_names)
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    if X.ndim != 2 or X.shape[1] != len(names):
        raise ValueError(f"X (n, {len(names)}) bicimli olmali: {X.shape}")
    if y.ndim != 1 or y.shape[0] != X.shape[0]:
        raise ValueError(f"y uzunlugu X ile uyusmuyor: {y.shape} vs {X.shape}")
    if X.shape[0] < MIN_FIT_ROWS:
        raise ValueError(f"en az {MIN_FIT_ROWS} satir gerekli: {X.shape[0]}")
    if not np.isfinite(X).all():
        raise ValueError("X NaN/sonsuz iceremez")
    if not np.isin(y, (0.0, 1.0)).all():
        raise ValueError("y yalniz 0/1 icermeli (NaN dahil degil)")
    if np.unique(y).size < 2:
        raise ValueError("tek sinifli etiket: model egitilemez")
    extra_json = _json_safe(extra if extra is not None else {}, "extra")
    if not isinstance(extra_json, dict):
        raise ValueError("extra bir sozluk olmali")

    # _make_pipeline yalniz C ve penalty'yi kullanir; spec'in ozellik listesi burada onemsizdir (varsayilan grup),
    # asil ozellik adlari yukarida ayri dogrulandi.
    spec = lab.ModelSpec(name=KIND, label=label, C=float(C), penalty=penalty)
    pipe = lab._make_pipeline(spec, int(seed))
    pipe.fit(X, y.astype(int))
    scaler, clf = pipe.named_steps["scale"], pipe.named_steps["clf"]

    params = {
        "kind": KIND,
        "features": names,
        "mean": [float(v) for v in scaler.mean_],
        "scale": [float(v) for v in scaler.scale_],
        "coef": [float(v) for v in clf.coef_[0]],
        "intercept": float(clf.intercept_[0]),
        "C": float(C),
        "penalty": str(penalty),
        "label": str(label),
        "n_train": int(X.shape[0]),
        "prob_clip": [float(PROB_CLIP[0]), float(PROB_CLIP[1])],
        "extra": extra_json,
    }
    _parse(params)  # uretilen sozlugun kendisi de gecerli olmali (NaN/sonsuz katsayi yakalanir)
    json.dumps(params, allow_nan=False)
    return params


# ---------------------------------------------------------------- tahmin

def _parse(params: Mapping) -> tuple[list[str], np.ndarray, np.ndarray, np.ndarray, float, tuple[float, float]]:
    if not isinstance(params, Mapping):
        raise ValueError("params bir sozluk olmali")
    if params.get("kind") != KIND:
        raise ValueError(f"desteklenmeyen model turu: {params.get('kind')!r} (beklenen {KIND!r})")
    for key in ("features", "mean", "scale", "coef", "intercept", "prob_clip"):
        if key not in params:
            raise ValueError(f"params'ta eksik anahtar: {key}")
    names = [str(n) for n in params["features"]]
    k = len(names)
    if k == 0 or len(set(names)) != k:
        raise ValueError("params.features bos ya da tekrarli")
    try:
        mean = np.asarray(params["mean"], dtype=float)
        scale = np.asarray(params["scale"], dtype=float)
        coef = np.asarray(params["coef"], dtype=float)
        intercept = float(params["intercept"])
        lo, hi = (float(v) for v in params["prob_clip"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"params sayisal degil: {exc}") from exc
    if not (mean.shape == scale.shape == coef.shape == (k,)):
        raise ValueError("params.mean/scale/coef uzunlugu features ile uyusmuyor")
    if not (np.isfinite(mean).all() and np.isfinite(scale).all() and np.isfinite(coef).all()
            and math.isfinite(intercept)):
        raise ValueError("params NaN/sonsuz deger iceriyor")
    if (scale <= 0).any():
        raise ValueError("params.scale pozitif olmali")
    if not (0.0 <= lo < hi <= 1.0):
        raise ValueError(f"params.prob_clip gecersiz: {[lo, hi]}")
    return names, mean, scale, coef, intercept, (lo, hi)


def _sigmoid(z: np.ndarray) -> np.ndarray:
    out = np.empty_like(z)
    pos = z >= 0
    out[pos] = 1.0 / (1.0 + np.exp(-z[pos]))
    ez = np.exp(z[~pos])
    out[~pos] = ez / (1.0 + ez)
    return out


def _raw_prob(params: Mapping, X: np.ndarray) -> np.ndarray:
    """Kirpmasiz olasilik (sklearn predict_proba ile karsilastirma icin)."""
    _, mean, scale, coef, intercept, _ = _parse(params)
    z = ((X - mean) / scale) @ coef + intercept
    return _sigmoid(z)


def _clip(params: Mapping, p: np.ndarray) -> np.ndarray:
    _, _, _, _, _, (lo, hi) = _parse(params)
    return np.clip(p, lo, hi)


def predict_prob(params: dict, features: Mapping[str, float]) -> float:
    """Tek satir: `features` {ad: deger}. Eksik/NaN/sonsuz/sayisal olmayan ozellik => ValueError."""
    names, *_ = _parse(params)
    if not isinstance(features, Mapping):
        raise ValueError("features bir sozluk olmali")
    missing = [n for n in names if n not in features]
    if missing:
        raise ValueError(f"eksik ozellik(ler): {missing}")
    vals = []
    for n in names:
        v = features[n]
        if isinstance(v, (bool, np.bool_)) or v is None:
            raise ValueError(f"ozellik sayisal degil: {n}={v!r}")
        try:
            f = float(v)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"ozellik sayisal degil: {n}={v!r}") from exc
        if not math.isfinite(f):
            raise ValueError(f"ozellik sonlu degil: {n}={f}")
        vals.append(f)
    p = _raw_prob(params, np.asarray(vals, dtype=float)[None, :])
    return float(_clip(params, p)[0])


def predict_frame(params: dict, df: pd.DataFrame) -> np.ndarray:
    """DataFrame satirlari icin olasilik dizisi (kirpilmis). Eksik sutun ya da NaN/sonsuz => ValueError."""
    names, *_ = _parse(params)
    missing = [n for n in names if n not in df.columns]
    if missing:
        raise ValueError(f"eksik ozellik sutun(lar)i: {missing}")
    try:
        X = df[names].to_numpy(dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"ozellik sutunlari sayisal degil: {exc}") from exc
    if not np.isfinite(X).all():
        bad = [n for n, ok in zip(names, np.isfinite(X).all(axis=0), strict=True) if not ok]
        raise ValueError(f"NaN/sonsuz ozellik: {bad}")
    return _clip(params, _raw_prob(params, X))
