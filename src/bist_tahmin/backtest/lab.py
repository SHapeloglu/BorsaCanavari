"""Faz 3.5 backtest laboratuvari: model kosucusu ve degerlendirme (PROJE.md Bolum 5, 10, 11, 15).

Bu modul DB'ye ve komsu backtest modullerine (splits/guard/dataset/registry) IMPORT ile bagli degildir;
yalnizca sozlesmelere (duck-typing) dayanir:
- `frame`: date, ticker, features.build.FEATURE_NAMES, end_date, fwd_ret, fwd_rel_ret, tradable_ret,
  label_up, label_rel_up sutunlari.
- `fold`: fold_id, train_end (Timestamp), test_dates (DatetimeIndex).
- `guard`: filter_frame(df, date_col=, end_col=) ve check(*dates) metotlari; kilitliyken sinama
  kumesine degen satirlari dusurur / HoldoutLocked firlatir. guard=None HICBIR kilit uygulamaz;
  gercek deneylerde cagiran taraf her zaman bir guard vermelidir.

Kurallar (hepsi testlidir):
- Egitim satirlari: date <= train_end VE end_date <= ilk test gunu (purge). Ihlalde AssertionError.
  NaT end_date'li satir (etiketi bilinmeyen) egitime girmez.
- Rolling pencere: purge'dan SONRA kalan egitim satirlarinin son `rolling_days` FARKLI gunu
  (islem gunu sayisi, takvim gunu degil). Purge rolling'de de gecerlidir.
- StandardScaler + LogisticRegression tek Pipeline; her fold'da yalnizca o fold'un egitimine fit edilir.
- Egitimde NaN etiket/ozellik satirlari dusurulur; tek sinifli (ya da cok kucuk) egitim kumesi olan
  fold ATLANIR ve sayilir (run_info). Hicbir sey uydurulmaz.
- Test satirlari: fold.test_dates icinde, etiketi bilinen, ozellikleri sonlu satirlar.
- prob_up [0.01, 0.99]'a kirpilir.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from sklearn import __version__ as _SKLEARN_VERSION
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from ..baselines.rules import BASELINES, baseline_prob
from ..evaluate.bootstrap import bonferroni_alpha, paired_diff_ci
from ..evaluate.costs import DEFAULT_GRID_BPS, cost_sensitivity
from ..evaluate.metrics import (
    accuracy,
    brier,
    calibration_table,
    daily_ic,
    ic_summary,
    log_loss,
)
from ..features.build import (
    CALENDAR_FEATURES,
    FEATURE_NAMES,
    MARKET_FEATURES,
    STOCK_FEATURES,
    Z_FEATURES,
)

DISCLAIMER = "Bu çıktı yatırım tavsiyesi değildir; yalnızca model ölçüm amaçlıdır."
PROB_CLIP = (0.01, 0.99)
LABELS = ("label_up", "label_rel_up")
RETURN_COL = {"label_up": "fwd_ret", "label_rel_up": "fwd_rel_ret"}
MIN_TRAIN_ROWS = 30  # bunun altindaki egitim kumesi icin fold atlanir (uydurma yok)
TOP_FRAC = 0.2       # PROJE.md Bolum 11: kohort portfoyu en ust %20
TAIL_DRAWS_MIN = 10  # her uc kuyrukta en az bu kadar bootstrap replikati (kantil gurultusu icin)
MAX_RESAMPLES = 200_000
CALIBRATION_BINS = 10

# Etiket/gelecek turevi sutunlar ozellik olamaz (sizinti): ad onekleri ve tam adlar
_FORBIDDEN_EXACT = frozenset({"tradable_ret", "end_date"})
_FORBIDDEN_PREFIXES = ("fwd_", "label_")

FEATURE_GROUPS: dict[str, tuple[str, ...]] = {
    "all": FEATURE_NAMES,
    # 'all_z': hisseler arasi z-skorlu hisse ozellikleri + piyasa + takvim (ham hisse ozellikleri YOK)
    # 'weekday' (0-4 sayisal) dogrusal modelde yanlis belirtilmis ozelliktir (gun etkisi monoton degil): varsayilan
    # gruplarda YOK; ikili 'post_holiday' var. Isteyen adiyla ["weekday", ...] ekleyebilir.
    "all_z": Z_FEATURES + MARKET_FEATURES + ("post_holiday",),
    "stock_z": Z_FEATURES,
    "stock_raw": STOCK_FEATURES,
    "market": MARKET_FEATURES,
    "calendar": ("post_holiday",),
}


# ---------------------------------------------------------------- spec

def _is_forbidden_feature(name: str) -> bool:
    return name in _FORBIDDEN_EXACT or name.startswith(_FORBIDDEN_PREFIXES)


def resolve_features(features: tuple[str, ...] | list[str] | str) -> tuple[str, ...]:
    """Ozellik ad listesi ya da grup ('a+b' ile birlestirilebilir) -> ad tuple'i.

    Etiket-turevi ad (fwd_*, label_*, tradable_ret, end_date) ya da bilinmeyen ad ValueError.
    Siralama korunur, tekrar eden ad ValueError.
    """
    if isinstance(features, str):
        names: list[str] = []
        for part in features.split("+"):
            part = part.strip()
            if _is_forbidden_feature(part):
                raise ValueError(f"etiket/gelecek turevi sutun ozellik olamaz: {part!r}")
            if part not in FEATURE_GROUPS:
                raise ValueError(f"bilinmeyen ozellik grubu: {part!r}; gecerliler: {sorted(FEATURE_GROUPS)}")
            names.extend(FEATURE_GROUPS[part])
    else:
        names = [str(n) for n in features]
        for n in names:
            if _is_forbidden_feature(n):
                raise ValueError(f"etiket/gelecek turevi sutun ozellik olamaz: {n!r}")
        unknown = [n for n in names if n not in FEATURE_NAMES]
        if unknown:
            raise ValueError(f"bilinmeyen ozellik(ler): {unknown}")
    if not names:
        raise ValueError("ozellik listesi bos olamaz")
    if len(set(names)) != len(names):
        raise ValueError("ozellik listesinde tekrar eden ad var")
    return tuple(names)


@dataclass(frozen=True)
class ModelSpec:
    name: str
    label: str = "label_up"                          # 'label_up' | 'label_rel_up'
    features: tuple[str, ...] | str = "all_z"        # ad listesi ya da grup adi
    C: float = 1.0
    penalty: str = "l2"                              # 'l1' | 'l2'
    window: str = "expanding"                        # 'expanding' | 'rolling'
    rolling_days: int | None = None                  # rolling icin zorunlu: son N FARKLI egitim gunu

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("name bos olmayan metin olmali")
        if self.label not in LABELS:
            raise ValueError(f"label {LABELS} icinden olmali: {self.label!r}")
        if not isinstance(self.features, str):
            object.__setattr__(self, "features", tuple(self.features))
        resolve_features(self.features)  # erken dogrulama (ValueError)
        if not (isinstance(self.C, (int, float)) and math.isfinite(self.C) and self.C > 0):
            raise ValueError(f"C sonlu ve > 0 olmali: {self.C!r}")
        if self.penalty not in ("l1", "l2"):
            raise ValueError(f"penalty 'l1' ya da 'l2' olmali: {self.penalty!r}")
        if self.window not in ("expanding", "rolling"):
            raise ValueError(f"window 'expanding' ya da 'rolling' olmali: {self.window!r}")
        if self.window == "rolling":
            if (self.rolling_days is None or isinstance(self.rolling_days, bool)
                    or int(self.rolling_days) != self.rolling_days or self.rolling_days < 1):
                raise ValueError("rolling pencere icin rolling_days pozitif tam sayi olmali")
        elif self.rolling_days is not None:
            raise ValueError("rolling_days yalnizca window='rolling' ile kullanilir")

    @property
    def feature_list(self) -> tuple[str, ...]:
        return resolve_features(self.features)

    def params(self) -> dict:
        """JSON'lanabilir parametreler (experiments.params_json)."""
        return {
            "name": self.name,
            "model": "logistic_regression",
            "scaler": "standard_fit_on_train_only",
            "label": self.label,
            "features": self.features if isinstance(self.features, str) else list(self.features),
            "resolved_features": list(self.feature_list),
            "C": float(self.C),
            "penalty": self.penalty,
            "window": self.window,
            "rolling_days": None if self.rolling_days is None else int(self.rolling_days),
            "prob_clip": list(PROB_CLIP),
        }


def _make_pipeline(spec: ModelSpec, seed: int) -> Pipeline:
    """StandardScaler + LogisticRegression. sklearn >= 1.8 'penalty' parametresini kullanmaz (l1_ratio)."""
    major, minor = (int(x) for x in _SKLEARN_VERSION.split(".")[:2])
    kwargs: dict[str, Any] = {"C": float(spec.C), "max_iter": 2000, "random_state": int(seed)}
    solver = "liblinear" if spec.penalty == "l1" else "lbfgs"
    if (major, minor) >= (1, 8):
        kwargs["l1_ratio"] = 1.0 if spec.penalty == "l1" else 0.0
    else:
        kwargs["penalty"] = spec.penalty
    kwargs["solver"] = solver
    return Pipeline([("scale", StandardScaler()), ("clf", LogisticRegression(**kwargs))])


# ---------------------------------------------------------------- walk-forward

PRED_COLUMNS = (
    "date", "ticker", "fold_id", "prob_up", "label", "label_up", "label_rel_up",
    "fwd_ret", "fwd_rel_ret", "tradable_ret", "end_date",
)
_REQUIRED_FRAME_COLS = (
    "date", "ticker", "end_date", "fwd_ret", "fwd_rel_ret", "tradable_ret", "label_up", "label_rel_up",
)


def _check_frame(frame: pd.DataFrame, feats: tuple[str, ...]) -> None:
    if not isinstance(frame, pd.DataFrame):
        raise ValueError("frame bir pandas DataFrame olmali")
    missing = [c for c in (*_REQUIRED_FRAME_COLS, *feats) if c not in frame.columns]
    if missing:
        raise ValueError(f"frame'de eksik sutun(lar): {missing}")


def _fold_dates(fold) -> tuple[pd.Timestamp, pd.DatetimeIndex]:
    train_end = pd.Timestamp(fold.train_end)
    test_dates = pd.DatetimeIndex(fold.test_dates)
    if pd.isna(train_end):
        raise ValueError(f"fold {fold.fold_id}: train_end eksik")
    if len(test_dates) == 0:
        raise ValueError(f"fold {fold.fold_id}: test_dates bos")
    if test_dates.isna().any() or test_dates.has_duplicates or not test_dates.is_monotonic_increasing:
        raise ValueError(f"fold {fold.fold_id}: test_dates sirali, tekrarsiz ve eksiksiz olmali")
    return train_end, test_dates


def run_walk_forward(
    frame: pd.DataFrame,
    spec: ModelSpec,
    folds,
    *,
    guard=None,
    seed: int = 0,
    min_train_rows: int = MIN_TRAIN_ROWS,
) -> pd.DataFrame:
    """Purge'lu walk-forward: her fold'da yalniz o fold'un izinli egitim satirlariyla model fit edilir.

    Dondurulen tablo test tahminleridir (PRED_COLUMNS). Fold ayrintilari, atlanan fold'lar ve sayaclar
    `result.attrs["run_info"]` icindedir (JSON'lanabilir).
    """
    feats = spec.feature_list
    _check_frame(frame, feats)
    folds = list(folds)
    ids = [f.fold_id for f in folds]
    if len(set(ids)) != len(ids):
        raise ValueError("fold_id tekrar ediyor")

    data = frame
    if guard is not None:
        data = guard.filter_frame(frame, date_col="date", end_col="end_date")
    data = data.copy()
    data["date"] = pd.to_datetime(data["date"])
    data["end_date"] = pd.to_datetime(data["end_date"])
    data = data.sort_values(["date", "ticker"], kind="stable").reset_index(drop=True)

    date_s = data["date"]
    end_s = data["end_date"]
    x_all = data[list(feats)].to_numpy(dtype=float)
    y_all = pd.to_numeric(data[spec.label], errors="coerce").to_numpy(dtype=float)
    ok_xy = np.isfinite(x_all).all(axis=1) & np.isfinite(y_all)

    parts: list[pd.DataFrame] = []
    fold_infos: list[dict] = []
    skipped: list[dict] = []

    for fold in folds:
        fid = int(fold.fold_id)
        train_end, test_dates = _fold_dates(fold)
        first_test = test_dates[0]
        if not train_end < first_test:
            raise AssertionError(
                f"fold {fid}: train_end ({train_end.date()}) ilk test gunune ({first_test.date()}) "
                "esit ya da sonra: egitim/test cakisiyor"
            )
        if guard is not None:
            guard.check(train_end, first_test, test_dates[-1])

        pre = (date_s <= train_end).to_numpy()
        purge_ok = (end_s <= first_test).to_numpy()  # NaT -> False
        n_purged = int((pre & ~purge_ok).sum())
        train_mask = pre & purge_ok
        if spec.window == "rolling" and train_mask.any():
            uniq = date_s[train_mask].drop_duplicates().sort_values()
            cutoff = uniq.iloc[-int(spec.rolling_days):].iloc[0]
            train_mask &= (date_s >= cutoff).to_numpy()
        train_mask &= ok_xy

        # --- purge/sizinti dogrulamasi (L1'deki kontrolden bagimsiz, ikinci savunma hatti)
        if train_mask.any():
            t_dates = date_s[train_mask]
            t_ends = end_s[train_mask]
            if not (t_dates <= train_end).all():
                raise AssertionError(f"fold {fid}: egitim satiri train_end sonrasinda")
            if not (t_ends <= first_test).all():
                raise AssertionError(f"fold {fid}: egitim satirinin etiketi ilk test gunune tasiyor (purge ihlali)")
            if not (t_ends >= t_dates).all():
                raise AssertionError(f"fold {fid}: end_date < date olan bozuk egitim satiri")

        test_rows = date_s.isin(test_dates).to_numpy()
        n_test_unusable = int((test_rows & ~ok_xy).sum())
        test_mask = test_rows & ok_xy

        info = {
            "fold_id": fid,
            "train_end": str(train_end.date()),
            "first_test": str(first_test.date()),
            "last_test": str(test_dates[-1].date()),
            "n_train": int(train_mask.sum()),
            "n_test": int(test_mask.sum()),
            "n_purged": n_purged,
            "n_test_unusable": n_test_unusable,
            "train_max_date": str(date_s[train_mask].max().date()) if train_mask.any() else None,
            "train_max_end_date": str(end_s[train_mask].max().date()) if train_mask.any() else None,
        }
        fold_infos.append(info)

        if int(train_mask.sum()) < max(int(min_train_rows), 2):
            skipped.append({"fold_id": fid, "reason": "insufficient_train_rows", "n_train": int(train_mask.sum())})
            continue
        y_tr = y_all[train_mask]
        if np.unique(y_tr).size < 2:
            skipped.append({"fold_id": fid, "reason": "single_class_train", "n_train": int(train_mask.sum())})
            continue
        if not test_mask.any():
            skipped.append({"fold_id": fid, "reason": "no_test_rows", "n_train": int(train_mask.sum())})
            continue

        model = _make_pipeline(spec, seed)
        model.fit(x_all[train_mask], y_tr.astype(int))
        prob = model.predict_proba(x_all[test_mask])[:, 1]
        prob = np.clip(prob, PROB_CLIP[0], PROB_CLIP[1])

        part = data.loc[test_mask, ["date", "ticker"]].copy()
        part["fold_id"] = fid
        part["prob_up"] = prob
        part["label"] = y_all[test_mask]
        for col in ("label_up", "label_rel_up", "fwd_ret", "fwd_rel_ret", "tradable_ret", "end_date"):
            part[col] = data.loc[test_mask, col].to_numpy()
        parts.append(part)

    if parts:
        out = pd.concat(parts, ignore_index=True)[list(PRED_COLUMNS)]
    else:
        out = pd.DataFrame({c: pd.Series(dtype=float) for c in PRED_COLUMNS})
    out.attrs["run_info"] = {
        "spec": spec.params(),
        "seed": int(seed),
        "guard_used": guard is not None,
        "n_folds": len(folds),
        "n_folds_run": len(parts),
        "n_folds_skipped": len(skipped),
        "skipped_folds": skipped,
        "folds": fold_infos,
    }
    return out


# ---------------------------------------------------------------- dataset baseline'lari

def _check_clip(clip) -> tuple[float, float]:
    lo, hi = clip
    if not (0.0 <= lo < hi <= 1.0):
        raise ValueError(f"clip 0 <= alt < ust <= 1 olmali: {clip}")
    return float(lo), float(hi)


def _pooled_base_rate_by_date(
    frame: pd.DataFrame, label: str, window_days: int, min_samples: int
) -> pd.Series:
    """Her tarih d icin: etiketi d'de ZATEN BILINEN (end_date <= d) orneklerin son `window_days` FARKLI
    gundeki havuzlanmis yukselis orani (rules.historical_base_rate ile ayni tanim); ornek < min_samples ise NaN.

    Gun, ancak o gunun etiketli satirlarinin en gec end_date'i <= d ise uygundur (muhafazakar).
    """
    dates_idx = pd.DatetimeIndex(frame["date"].drop_duplicates()).sort_values()
    dates_all = dates_idx.to_numpy(dtype="datetime64[ns]")
    use = frame.loc[frame["end_date"].notna() & frame[label].notna(), ["date", "end_date", label]]
    out = pd.Series(np.nan, index=pd.DatetimeIndex(dates_idx, name="date"), dtype=float)
    if use.empty:
        return out
    g = use.groupby("date", sort=True)
    day_end = g["end_date"].max().to_numpy(dtype="datetime64[ns]")
    day_up = g[label].sum().to_numpy(dtype=float)
    day_n = g[label].count().to_numpy(dtype=float)
    cum_up = np.concatenate([[0.0], np.cumsum(day_up)])
    cum_n = np.concatenate([[0.0], np.cumsum(day_n)])
    vals = np.full(len(dates_all), np.nan)
    for i, d in enumerate(dates_all):
        idx = np.flatnonzero(day_end <= d)
        if idx.size == 0:
            continue
        idx = idx[-window_days:]
        if idx.size and np.all(np.diff(idx) == 1):  # ardisik: kumulatif toplam
            n = cum_n[idx[-1] + 1] - cum_n[idx[0]]
            up = cum_up[idx[-1] + 1] - cum_up[idx[0]]
        else:
            n = day_n[idx].sum()
            up = day_up[idx].sum()
        if n >= min_samples:
            vals[i] = up / n
    out[:] = vals
    return out


def dataset_baseline_probs(
    frame: pd.DataFrame,
    *,
    horizon_days: int,
    label: str,
    clip: tuple[float, float] = PROB_CLIP,
    window_days: int = 250,
    min_samples: int = 30,
    seed: int = 0,
) -> pd.DataFrame:
    """baselines/rules.py'nin 5 baseline'ini DATASET KOLONLARINDAN yeniden uretir (fiyat serisi gerekmez).

    - always_up: clip ust siniri (0.99)
    - yesterday: ret_1 > 0 ise 0.6, degilse 0.4 (ret_1 NaN ise NaN)
    - ma20: ma20 (fiyat/MA20 - 1) > 0 ise 0.6, degilse 0.4 (NaN ise NaN)
    - random: rules.baseline_prob('random') ile AYNI deterministik tohum turetimi
    - base_rate: o gun etiketi bilinen (end_date <= date) orneklerin son `window_days` gunluk havuzlanmis
      yukselis orani (`label` sutunu icin); ornek < min_samples ise NaN
    `horizon_days` yalnizca dogrulama icindir: etiket bitisi `end_date` sutunundan gelir, ufuk burada
    yeniden hesaplanmaz. Cikti satirlari frame ile ayni sirada: date, ticker + baseline kolonlari.
    """
    if int(horizon_days) != horizon_days or horizon_days < 1:
        raise ValueError("horizon_days pozitif tam sayi olmali")
    if label not in LABELS:
        raise ValueError(f"label {LABELS} icinden olmali: {label!r}")
    if int(window_days) != window_days or window_days < 1 or int(min_samples) != min_samples or min_samples < 1:
        raise ValueError("window_days ve min_samples pozitif tam sayi olmali")
    lo, hi = _check_clip(clip)
    need = ("date", "ticker", "ret_1", "ma20", "end_date", label)
    missing = [c for c in need if c not in frame.columns]
    if missing:
        raise ValueError(f"frame'de eksik sutun(lar): {missing}")

    f = frame.loc[:, list(need)].reset_index(drop=True).copy()
    f["date"] = pd.to_datetime(f["date"])
    f["end_date"] = pd.to_datetime(f["end_date"])
    n = len(f)
    out = pd.DataFrame({"date": f["date"], "ticker": f["ticker"]})

    out["always_up"] = float(np.clip(1.0, lo, hi))

    for name, col in (("yesterday", "ret_1"), ("ma20", "ma20")):
        v = pd.to_numeric(f[col], errors="coerce").to_numpy(dtype=float)
        p = np.where(v > 0.0, 0.6, 0.4)
        out[name] = np.where(np.isfinite(v), np.clip(p, lo, hi), np.nan)

    rnd = np.empty(n, dtype=float)
    for i, (tk, d) in enumerate(zip(f["ticker"].tolist(), f["date"].tolist())):
        rnd[i] = baseline_prob("random", tk, None, d, seed=seed, clip=(lo, hi))
    out["random"] = rnd

    rate = _pooled_base_rate_by_date(f, label, int(window_days), int(min_samples))
    br = f["date"].map(rate).to_numpy(dtype=float)
    out["base_rate"] = np.where(np.isfinite(br), np.clip(br, lo, hi), np.nan)
    return out[["date", "ticker", *BASELINES]]


# ---------------------------------------------------------------- degerlendirme

def _num(x):
    """JSON icin: numpy -> python, NaN/sonsuz -> None."""
    if x is None:
        return None
    if isinstance(x, (np.floating, float)):
        v = float(x)
        return v if math.isfinite(v) else None
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.bool_,)):
        return bool(x)
    return x


def _jsonable(obj):
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if obj is pd.NaT:
        return None
    if isinstance(obj, (pd.Timestamp, np.datetime64)):
        return None if pd.isna(obj) else str(pd.Timestamp(obj).date())
    if isinstance(obj, pd.Series):
        return _jsonable(obj.to_dict())
    if isinstance(obj, np.ndarray):
        return _jsonable(obj.tolist())
    return _num(obj)


def _prob_metrics(p: np.ndarray, y: np.ndarray, dates: pd.Series) -> dict:
    if len(p) == 0:
        return {"n": 0, "n_dates": 0, "brier": None, "log_loss": None, "accuracy": None}
    return {
        "n": int(len(p)),
        "n_dates": int(dates.nunique()),
        "brier": brier(p, y),
        "log_loss": log_loss(p, y, clip=PROB_CLIP),
        "accuracy": accuracy(p, y),
        "mean_prob": float(np.mean(p)),
        "obs_rate": float(np.mean(y)),
    }


def _ic_block(df: pd.DataFrame, score_col: str, ret_col: str, horizon_days: int) -> dict:
    sub = df[["date", score_col, ret_col]].dropna()
    if sub.empty:
        return {"mean_ic": None, "t_stat_nw": None, "n_days": 0, "n_rows": 0, "nw_lags": None}
    ic = daily_ic(sub, "date", score_col, ret_col)
    s = ic_summary(ic, horizon_days=horizon_days)
    return {
        "mean_ic": s["mean_ic"], "t_stat_nw": s["t_stat_nw"], "t_stat_iid": s["t_stat_iid"],
        "nw_lags": s["nw_lags"], "n_days": s["n_days"], "n_days_total": s["n_days_total"],
        "n_rows": int(len(sub)),
    }


def _resample_plan(cfg_resamples: int, alpha_adj: float) -> dict:
    needed = math.ceil(2 * TAIL_DRAWS_MIN / alpha_adj)
    used = min(max(int(cfg_resamples), needed), MAX_RESAMPLES)
    return {
        "resamples_cfg": int(cfg_resamples),
        "resamples_needed": int(needed),
        "resamples_used": int(used),
        "resamples_raised": bool(used > int(cfg_resamples)),
        "resamples_capped": bool(needed > MAX_RESAMPLES),
    }


def evaluate(
    preds: pd.DataFrame,
    baselines: pd.DataFrame,
    *,
    label: str,
    cfg: dict,
    alpha: float = 0.05,
    n_trials: int = 1,
    seed: int = 0,
) -> dict:
    """Model tahminlerini baseline'larla degerlendirir; JSON'lanabilir sozluk dondurur.

    ANA olcut: model Brier kaybi - base_rate baseline kaybi, ayni (gun, hisse) satirlarinda eslestirilmis
    blok bootstrap farki. Negatif = model daha iyi. CI duzeyi = 1 - alpha / n_trials (Bonferroni);
    alpha cok kuculurse resamples, her uc kuyrukta >= TAIL_DRAWS_MIN replikat kalacak kadar artirilir
    (tavan MAX_RESAMPLES; sonucta belirtilir). cfg['terfi']['bootstrap']['ci'] KULLANILMAZ: duzey alpha'dan gelir.
    Degerlendirme kumesi: etiketi bilinen VE base_rate baseline'i tanimli satirlar (hepsi ayni satirlar).
    """
    if label not in LABELS:
        raise ValueError(f"label {LABELS} icinden olmali: {label!r}")
    ret_col = RETURN_COL[label]
    alpha_adj = bonferroni_alpha(alpha, n_trials)  # alpha/n_trials (gecersizse ValueError)
    ci_level = 1.0 - alpha_adj
    terfi = cfg.get("terfi", {}) or {}
    boot = terfi.get("bootstrap", {}) or {}
    block_days = int(boot.get("block_days", 10))
    cfg_resamples = int(boot.get("resamples", 2000))
    min_dates = int(terfi.get("min_resolved_dates", 60))
    min_impr = terfi.get("min_improvement_brier")
    horizon = int(cfg.get("horizon_days", 5))
    costs_cfg = cfg.get("costs", {}) or {}
    grid = list(costs_cfg.get("sensitivity_grid_bps") or DEFAULT_GRID_BPS)

    for col in ("date", "ticker", "prob_up", label, ret_col, "tradable_ret"):
        if col not in preds.columns:
            raise ValueError(f"preds'te eksik sutun: {col}")
    if "base_rate" not in baselines.columns or not {"date", "ticker"} <= set(baselines.columns):
        raise ValueError("baselines date, ticker ve base_rate sutunlarini icermeli")
    if "label" in preds.columns:
        both = preds["label"].notna() & preds[label].notna()
        if not np.array_equal(preds.loc[both, "label"].to_numpy(float), preds.loc[both, label].to_numpy(float)):
            raise ValueError(f"preds['label'] ile preds[{label!r}] uyusmuyor: yanlis label argumani")
    run_info = preds.attrs.get("run_info")

    p = preds.copy()
    p["date"] = pd.to_datetime(p["date"])
    if p.duplicated(["date", "ticker"]).any():
        raise ValueError("preds'te tekrar eden (date, ticker) var: tek model icin cagrilmali")
    b = baselines.copy()
    b["date"] = pd.to_datetime(b["date"])
    if b.duplicated(["date", "ticker"]).any():
        raise ValueError("baselines'ta tekrar eden (date, ticker) var")
    bcols = [c for c in BASELINES if c in b.columns]
    p = p.merge(b[["date", "ticker", *bcols]], on=["date", "ticker"], how="left")

    p["prob_up"] = pd.to_numeric(p["prob_up"], errors="coerce")
    n_in = len(p)
    known = p["prob_up"].notna() & p[label].notna()
    n_unlabeled = int((~known).sum())
    p = p.loc[known]
    has_br = p["base_rate"].notna()
    n_no_br = int((~has_br).sum())
    d = p.loc[has_br].reset_index(drop=True)

    warnings: list[str] = []
    if run_info and run_info.get("n_folds_skipped"):
        warnings.append(f"{run_info['n_folds_skipped']} fold atlandı (tek sınıflı/yetersiz eğitim ya da test satırı yok).")
    if n_no_br:
        warnings.append(f"{n_no_br} satır base_rate baseline'ı tanımsız olduğu için dışarıda bırakıldı.")
    if n_unlabeled:
        warnings.append(f"{n_unlabeled} satırın etiketi/olasılığı eksik, dışarıda bırakıldı.")

    result: dict[str, Any] = {
        "label": label, "return_col": ret_col, "alpha": float(alpha), "n_trials": int(n_trials),
        "alpha_adjusted": float(alpha_adj), "ci_level": float(ci_level), "seed": int(seed),
        "n_rows_input": int(n_in), "n_rows": int(len(d)),
        "n_rows_dropped_no_base_rate": n_no_br, "n_rows_dropped_unlabeled": n_unlabeled,
        "n_dates": int(d["date"].nunique()) if len(d) else 0,
        "date_min": d["date"].min() if len(d) else None,
        "date_max": d["date"].max() if len(d) else None,
        "min_resolved_dates": min_dates,
        "run_info": run_info,
        "warnings": warnings,
    }
    n_dates = result["n_dates"]
    result["sufficient"] = bool(n_dates >= min_dates)
    if not result["sufficient"]:
        warnings.append(
            f"YETERSİZ: farklı gün sayısı {n_dates} < terfi.min_resolved_dates ({min_dates}); "
            "sonuç gürültü sayılmalı, hiçbir terfi/başarı kararı için kullanılamaz."
        )
    if len(d) == 0:
        warnings.append("Değerlendirilecek satır yok.")
        result.update(models={}, main=None, ic={}, calibration=[], portfolio=None)
        return _jsonable(result)

    y = d[label].to_numpy(dtype=float)
    pm = d["prob_up"].to_numpy(dtype=float)

    # --- model + baseline metrikleri (ayni satir kumesi uzerinde; baseline tanimsizsa o satir o baseline'dan cikar)
    models: dict[str, dict] = {"model": _prob_metrics(pm, y, d["date"])}
    for name in bcols:
        ok = d[name].notna().to_numpy()
        m = _prob_metrics(d.loc[ok, name].to_numpy(float), y[ok], d.loc[ok, "date"])
        if ok.any():  # modelin ayni satirlardaki Brier'i (adil karsilastirma)
            m["model_brier_same_rows"] = brier(pm[ok], y[ok])
        models[name] = m
    result["models"] = models

    # --- ANA olcut: eslestirilmis blok bootstrap
    loss_m = (pm - y) ** 2
    loss_b = (d["base_rate"].to_numpy(float) - y) ** 2
    plan = _resample_plan(cfg_resamples, alpha_adj)
    main: dict[str, Any] = {
        "metric": "brier_model_minus_base_rate", "n_rows": int(len(d)), "n_dates": n_dates,
        "block_days": block_days, "ci_level": float(ci_level), "alpha_adjusted": float(alpha_adj),
        "n_trials": int(n_trials), **plan,
        "tail_draws": plan["resamples_used"] * alpha_adj / 2.0,
        "min_improvement_brier": None if min_impr is None else float(min_impr),
    }
    if plan["resamples_raised"]:
        warnings.append(
            f"Bonferroni (alpha={alpha}, n_trials={n_trials} -> alpha_düzeltilmiş={alpha_adj:.3g}) nedeniyle "
            f"resamples {plan['resamples_cfg']} -> {plan['resamples_used']} yükseltildi (uç kuyruk kantili gürültülenmesin)."
        )
    if plan["resamples_capped"]:
        warnings.append(
            f"resamples tavana ({MAX_RESAMPLES}) takıldı; uç kuyruk başına ~{main['tail_draws']:.1f} replikat: "
            "güven aralığı uçları gürültülü olabilir."
        )
    diff = float(np.mean(loss_m - loss_b))
    if n_dates < 2:
        main.update(diff=diff, lo=None, hi=None, excludes_zero=False, model_better=False, meets_min_improvement=False)
        warnings.append("2'den az farklı gün: blok bootstrap aralığı hesaplanamadı.")
    else:
        boot_df = pd.DataFrame({"date": d["date"], "loss_model": loss_m, "loss_base": loss_b})
        point, lo, hi, det = paired_diff_ci(
            boot_df, "loss_model", "loss_base", date_col="date", block_days=block_days,
            resamples=plan["resamples_used"], ci=ci_level, seed=seed,
        )
        excl = bool(det["excludes_zero"])
        main.update(
            diff=float(point), lo=lo, hi=hi, excludes_zero=excl,
            model_better=bool(excl and point < 0.0),
            block_days_used=det["block_days_used"], n_nan_replicates=det["n_nan"], se=det["se"],
            meets_min_improvement=bool(excl and min_impr is not None and point <= -float(min_impr)),
        )
    result["main"] = main

    # --- IC (skor = prob_up, getiri = label'a gore fwd_ret / fwd_rel_ret)
    ic: dict[str, dict] = {"model": _ic_block(d.rename(columns={"prob_up": "score"}), "score", ret_col, horizon)}
    for name in bcols:
        ic[name] = _ic_block(d.rename(columns={name: "score"}), "score", ret_col, horizon)
    result["ic"] = ic

    # --- kalibrasyon (model)
    cal = calibration_table(pm, y, bins=CALIBRATION_BINS)
    result["calibration"] = cal.to_dict(orient="records")

    # --- kohort portfoyu (score = prob_up, tradable_ret); maliyet izgarasi
    port_df = d[["date", "ticker", "prob_up", "tradable_ret"]].rename(columns={"date": "made_at", "prob_up": "score"})
    port: dict[str, Any] = {
        "top_frac": TOP_FRAC, "grid_bps": [float(g) for g in grid],
        "cost_label": ("DOĞRULANMADI: tahmini maliyet"
                       if costs_cfg.get("commission_bps") is None or costs_cfg.get("spread_bps") is None
                       else "yapılandırılmış maliyet"),
        "n_rows_tradable_missing": int(port_df["tradable_ret"].isna().sum()),
    }
    try:
        tab = cost_sensitivity(port_df, grid, top_frac=TOP_FRAC)
        port["grid"] = tab.to_dict(orient="records")
        port["n_cohorts"] = int(tab["n_cohorts"].iloc[0]) if len(tab) else 0
    except ValueError as exc:
        port["grid"] = []
        port["error"] = str(exc)
        warnings.append(f"Portföy simülasyonu yapılamadı: {exc}")
    result["portfolio"] = port

    return _jsonable(result)


# ---------------------------------------------------------------- metin ozeti

def _f(x, nd: int = 4) -> str:
    return "—" if x is None else f"{x:.{nd}f}"


def format_result(result: dict) -> str:
    """Turkce markdown ozet (Bolum 11 sirasi: ana olcut, IC, yardimci, kalibrasyon, portfoy)."""
    L: list[str] = []
    spec = (result.get("run_info") or {}).get("spec") or {}
    title = spec.get("name", "model")
    L.append(f"## Backtest sonucu: {title} ({result.get('label')})")
    L.append("")
    L.append(
        f"- Satır: {result.get('n_rows')} | farklı gün: {result.get('n_dates')} "
        f"({result.get('date_min')} → {result.get('date_max')})"
    )
    L.append(
        f"- Çoklu deneme düzeltmesi: alpha={result.get('alpha')}, n_trials={result.get('n_trials')} → "
        f"alpha_düzeltilmiş={result.get('alpha_adjusted'):.4g}, güven düzeyi={result.get('ci_level'):.4%}"
    )
    ri = result.get("run_info")
    if ri:
        L.append(f"- Fold: {ri.get('n_folds_run')}/{ri.get('n_folds')} çalıştı, {ri.get('n_folds_skipped')} atlandı")
    if not result.get("sufficient"):
        L.append("")
        L.append(f"> **YETERSİZ ÖRNEK:** farklı gün sayısı {result.get('n_dates')} < {result.get('min_resolved_dates')}. "
                 "Aşağıdaki sayılar gürültü olarak okunmalı.")
    for w in result.get("warnings") or []:
        L.append(f"- UYARI: {w}")
    L.append("")

    main = result.get("main")
    L.append("### Ana ölçüt: Brier farkı (model − geçmiş yükseliş oranı)")
    if not main:
        L.append("Hesaplanamadı (değerlendirilecek satır yok).")
    else:
        L.append(
            f"- Fark: **{_f(main.get('diff'), 5)}** (negatif = model daha iyi); "
            f"{result.get('ci_level'):.2%} blok bootstrap aralığı [{_f(main.get('lo'), 5)}, {_f(main.get('hi'), 5)}]"
        )
        L.append(
            f"- Sıfırı dışlıyor: {'evet' if main.get('excludes_zero') else 'hayır'} | "
            f"model baseline'dan iyi: **{'EVET' if main.get('model_better') else 'hayır'}**"
        )
        if main.get("min_improvement_brier") is not None:
            L.append(
                f"- Asgari iyileşme ({main['min_improvement_brier']}) sağlandı: "
                f"{'evet' if main.get('meets_min_improvement') else 'hayır'}"
            )
        L.append(
            f"- Bootstrap: blok={main.get('block_days')} gün, resamples={main.get('resamples_used')} "
            f"(yapılandırma {main.get('resamples_cfg')}), uç kuyruk başına ~{main.get('tail_draws'):.1f} replikat; "
            f"n={main.get('n_rows')} satır / {main.get('n_dates')} gün"
        )
    L.append("")

    models = result.get("models") or {}
    if models:
        L.append("### Olasılık metrikleri")
        L.append("| Kaynak | Brier | Log loss | Accuracy | n | gün |")
        L.append("|---|---|---|---|---|---|")
        for name, m in models.items():
            L.append(f"| {name} | {_f(m.get('brier'))} | {_f(m.get('log_loss'))} | {_f(m.get('accuracy'))} "
                     f"| {m.get('n')} | {m.get('n_dates')} |")
        L.append("")

    ic = result.get("ic") or {}
    if ic:
        L.append("### Günlük Spearman IC")
        L.append("| Kaynak | Ort. IC | NW t | gün |")
        L.append("|---|---|---|---|")
        for name, m in ic.items():
            L.append(f"| {name} | {_f(m.get('mean_ic'))} | {_f(m.get('t_stat_nw'), 2)} | {m.get('n_days')} |")
        L.append("")

    cal = result.get("calibration") or []
    if cal:
        L.append("### Kalibrasyon (model)")
        L.append("| Kova | n | Ort. tahmin | Gerçekleşen |")
        L.append("|---|---|---|---|")
        for r in cal:
            if r.get("n"):
                L.append(f"| [{r['bin_lo']:.1f}, {r['bin_hi']:.1f}) | {r['n']} | {_f(r.get('mean_pred'), 3)} "
                         f"| {_f(r.get('obs_rate'), 3)} |")
        L.append("")

    port = result.get("portfolio")
    if port:
        L.append(f"### Kohort portföyü (en üst %{port.get('top_frac', 0) * 100:.0f}, yalnız uzun) — {port.get('cost_label')}")
        if port.get("grid"):
            L.append("| Maliyet (bps, tek yön) | Kohort | Brüt | Net | Benchmark | Net fazla | İsabet |")
            L.append("|---|---|---|---|---|---|---|")
            for r in port["grid"]:
                L.append(f"| {_f(r.get('cost_bps'), 0)} | {r.get('n_cohorts')} | {_f(r.get('mean_gross'), 5)} "
                         f"| {_f(r.get('mean_net'), 5)} | {_f(r.get('mean_benchmark'), 5)} "
                         f"| {_f(r.get('mean_excess_net'), 5)} | {_f(r.get('hit_rate'), 3)} |")
        else:
            L.append(f"Hesaplanamadı: {port.get('error')}")
        L.append("")
    L.append(DISCLAIMER)
    return "\n".join(L)


def result_json(result: dict) -> str:
    """Sonucu katı JSON metnine cevirir (NaN yok; experiments.result_json icin)."""
    return json.dumps(result, allow_nan=False, sort_keys=True)
