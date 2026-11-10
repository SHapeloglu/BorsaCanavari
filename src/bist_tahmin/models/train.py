"""Challenger egitimi (PROJE.md Bolum 5, 12, 15 Faz 4).

Sizinti kurali (ASLI KORUMA): `as_of` gununde etiketi henuz bilinmeyen hicbir satir egitime girmez.
- Egitim satiri: date <= train_end VE end_date <= as_of; train_end = as_of'tan `purge_days` islem gunu once
  (islem gunleri veri setinin gunlerinden sayilir). purge_days >= horizon_days zorunludur.
- end_date, ufuk bitisidir: end_date <= as_of ise etiket as_of kapanisinda bilinir. Daha yeni fiyat/etiket
  degisse bile model degismez (testle kanitli).
- NaN/sonsuz ozellik ya da NaN etiket satirlari dusurulur; tek sinifli ya da az (< MIN_TRAIN_ROWS) satirda
  ValueError verilir (uydurma yok).

Sinama kumesi (holdout) notu: backtest laboratuvari holdout_start sonrasini kilitler, cunku orada ARAYAN
deneyler yapilir (deneme sisirmesi riski). Canli model ise NIHAI modeldir ve hakemi canli defterdir
(ileriye donuk, degistirilemez); bu yuzden egitim holdout_start sonrasi gunleri de kullanabilir. Bu bilgi
sizintisi degildir (as_of'ta bilinen veridir). Egitimde holdout donemi kullanildiysa
`params.extra.train_includes_holdout` True yazilir: terfi kosulu 3 (sinama kumesi kaniti) o durumda
holdout'u disarida birakan ayni spec'in laboratuvar sonucuyla (cagiranin verdigi `holdout_result`) verilmelidir.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import sklearn

from ..backtest import lab
from ..backtest.registry import record_experiment, update_result
from ..features.build import FEATURE_VERSION
from . import logreg
from .registry import ModelRecord, get_model, list_models, register_model

MIN_TRAIN_ROWS = 500
EXPERIMENT_FAMILY = "model-train"  # asil deney ailelerinden ayri: Bonferroni deneme sayisini sismesin


def _as_day(d, name: str) -> pd.Timestamp:
    ts = pd.Timestamp(d)
    if ts.tzinfo is not None:
        raise ValueError(f"{name} tz-naive olmali: {d!r}")
    return ts.normalize()


def _check_days(purge_days, horizon_days) -> tuple[int, int]:
    for n, v in (("purge_days", purge_days), ("horizon_days", horizon_days)):
        if isinstance(v, bool) or int(v) != v or v < 1:
            raise ValueError(f"{n} pozitif tam sayi olmali: {v!r}")
    if purge_days < horizon_days:
        raise ValueError(f"purge_days ({purge_days}) horizon_days ({horizon_days}) degerinden kucuk olamaz (sizinti)")
    return int(purge_days), int(horizon_days)


def _train_end(dates: pd.DatetimeIndex, as_of: pd.Timestamp, purge_days: int) -> pd.Timestamp | None:
    """as_of'a kadar olan (dahil) veri seti gunlerinden, as_of'tan purge_days gun onceki gun; yoksa None."""
    le = dates[dates <= as_of]
    pos = len(le) - 1 - purge_days
    return le[pos] if pos >= 0 else None


def _prepare(frame: pd.DataFrame, spec: lab.ModelSpec) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise ValueError("frame bir pandas DataFrame olmali")
    need = ["date", "end_date", spec.label, *spec.feature_list]
    missing = [c for c in need if c not in frame.columns]
    if missing:
        raise ValueError(f"frame'de eksik sutun(lar): {missing}")
    return frame


def _select(frame: pd.DataFrame, spec: lab.ModelSpec, as_of: pd.Timestamp, purge_days: int
            ) -> tuple[pd.DataFrame, pd.Timestamp | None]:
    f = _prepare(frame, spec)
    dates = pd.to_datetime(f["date"])
    ends = pd.to_datetime(f["end_date"])
    train_end = _train_end(pd.DatetimeIndex(dates.drop_duplicates().sort_values()), as_of, purge_days)
    if train_end is None:
        return f.iloc[0:0].copy(), None
    mask = ((dates <= train_end) & (ends <= as_of)).to_numpy()  # NaT -> False
    if spec.window == "rolling" and mask.any():
        uniq = dates[mask].drop_duplicates().sort_values()
        cutoff = uniq.iloc[-int(spec.rolling_days):].iloc[0]
        mask = mask & (dates >= cutoff).to_numpy()
    feats = list(spec.feature_list)
    x = f[feats].to_numpy(dtype=float)
    y = pd.to_numeric(f[spec.label], errors="coerce").to_numpy(dtype=float)
    # not: 'mask &= ...' pandas'in salt-okunur (copy-on-write) dizisinde ValueError verir; yeni dizi uretilir
    mask = mask & np.isfinite(x).all(axis=1) & np.isfinite(y)
    rows = f.loc[mask].copy()
    rows["date"] = pd.to_datetime(rows["date"])
    rows["end_date"] = pd.to_datetime(rows["end_date"])
    rows = rows.sort_values(["date", "ticker"] if "ticker" in rows.columns else ["date"], kind="stable")
    return rows.reset_index(drop=True), train_end


def training_rows(
    frame: pd.DataFrame, spec: lab.ModelSpec, as_of: pd.Timestamp, *, purge_days: int, horizon_days: int
) -> pd.DataFrame:
    """as_of anindaki egitim satirlari: date <= train_end VE end_date <= as_of; NaN etiket/ozellik dusurulur."""
    purge_days, horizon_days = _check_days(purge_days, horizon_days)
    rows, _ = _select(frame, spec, _as_day(as_of, "as_of"), purge_days)
    return rows


def train_model(
    frame: pd.DataFrame,
    spec: lab.ModelSpec,
    as_of: pd.Timestamp,
    *,
    horizon_days: int,
    purge_days: int,
    seed: int = 0,
) -> tuple[dict, pd.Timestamp, int]:
    """(params_json, train_end, n_rows). Tek sinifli ya da az (< MIN_TRAIN_ROWS) satirda ValueError."""
    purge_days, horizon_days = _check_days(purge_days, horizon_days)
    as_of = _as_day(as_of, "as_of")
    rows, train_end = _select(frame, spec, as_of, purge_days)
    if train_end is None:
        raise ValueError(f"as_of ({as_of.date()}) oncesinde purge_days ({purge_days}) kadar gun yok: egitilemez")
    n = len(rows)
    if n < MIN_TRAIN_ROWS:
        raise ValueError(f"egitim satiri yetersiz: {n} < {MIN_TRAIN_ROWS} (as_of={as_of.date()})")
    # ikinci savunma hatti: etiketi as_of'ta bilinmeyen satir asla bulunmamali
    if not (rows["end_date"] <= as_of).all() or not (rows["date"] <= train_end).all():
        raise AssertionError("egitim satiri as_of/train_end sinirini asiyor (sizinti)")
    y = rows[spec.label].to_numpy(dtype=float)
    if np.unique(y).size < 2:
        raise ValueError("egitim etiketi tek sinifli: model egitilemez")
    extra = {
        "spec": spec.params(),
        "as_of": as_of.date().isoformat(),
        "train_end": train_end.date().isoformat(),
        "purge_days": purge_days,
        "horizon_days": horizon_days,
        "n_rows": int(n),
        "n_train_days": int(rows["date"].nunique()),
        "train_date_min": rows["date"].min().date().isoformat(),
        "train_date_max": rows["date"].max().date().isoformat(),
        "train_base_rate": float(y.mean()),
        "seed": int(seed),
        "sklearn_version": sklearn.__version__,
    }
    params = logreg.fit_logreg(
        rows[list(spec.feature_list)].to_numpy(dtype=float), y, spec.feature_list,
        C=spec.C, penalty=spec.penalty, label=spec.label, seed=seed, extra=extra,
    )
    return params, train_end, int(n)


def train_and_register(
    conn,
    cfg: dict,
    ds,
    spec: lab.ModelSpec,
    as_of: pd.Timestamp,
    *,
    version_prefix: str = "logreg",
    role: str = "challenger",
    seed: int = 0,
) -> ModelRecord:
    """Egitir ve `models`e kaydeder. version = f"{prefix}_{spec.name}@{as_of:%Y-%m-%d}".

    Ayni version varsa egitimden ONCE ValueError (ezme yok). Veri seti as_of gununu kapsamiyorsa ValueError
    (eski veri seti ile sessizce egitilmez). Kayit + `experiments` satiri (aile 'model-train') tek islemdedir.
    """
    as_of = _as_day(as_of, "as_of")
    horizon = int(cfg["horizon_days"])
    if int(ds.horizon_days) != horizon:
        raise ValueError(f"veri seti ufku ({ds.horizon_days}) yapilandirmayla ({horizon}) uyusmuyor")
    purge_days = int(cfg["training"]["purge_days"])
    ds_fv = (ds.meta or {}).get("feature_version")
    if ds_fv is not None and ds_fv != FEATURE_VERSION:
        raise ValueError(f"veri seti ozellik surumu {ds_fv!r} kodun surumu {FEATURE_VERSION!r} ile uyusmuyor")
    last = pd.to_datetime(ds.frame["date"]).max() if len(ds.frame) else pd.NaT
    if pd.isna(last) or last < as_of:
        raise ValueError(f"veri seti as_of ({as_of.date()}) gununu kapsamiyor (son gun: {last}); yeniden olusturun")

    version = f"{version_prefix}_{spec.name}@{as_of:%Y-%m-%d}"
    if get_model(conn, version) is not None:
        raise ValueError(f"model zaten kayitli: {version}")

    params, train_end, n_rows = train_model(
        ds.frame, spec, as_of, horizon_days=horizon, purge_days=purge_days, seed=seed)
    extra = params["extra"]
    holdout_start = (cfg.get("holdout") or {}).get("holdout_start")
    extra["feature_version"] = FEATURE_VERSION
    extra["dataset"] = {
        "feature_version": ds_fv,
        "date_min": (ds.meta or {}).get("date_min"),
        "date_max": (ds.meta or {}).get("date_max"),
        "n_rows": (ds.meta or {}).get("n_rows"),
        "built_at": (ds.meta or {}).get("built_at"),
    }
    extra["version_prefix"] = version_prefix
    if holdout_start is not None:
        extra["holdout_start"] = str(holdout_start)
        extra["train_includes_holdout"] = bool(pd.Timestamp(extra["train_date_max"]) >= pd.Timestamp(holdout_start))

    now = dt.datetime.now(dt.timezone.utc)
    with conn.transaction():
        rec = register_model(conn, version, params, train_end.date(), role=role, trained_at=now)
        exp_id = record_experiment(
            conn, EXPERIMENT_FAMILY, f"model egitimi: {version}",
            {"version": version, "spec": spec.params(), "as_of": as_of.date().isoformat(), "role": role, "seed": seed},
            created_at=now,
        )
        update_result(conn, exp_id, {"version": version, "n_rows": n_rows, "train_end": train_end.date().isoformat()})
    return rec


def due_for_retrain(conn, cfg: dict, as_of: pd.Timestamp, calendar) -> bool:
    """Yeniden egitim zamani geldi mi?

    Referans: kayitli TUM modellerin (retired dahil) en yeni `train_end`i; retired dahil edilir ki reddedilen
    bir challenger ertesi gun yeniden egitimi tetiklemesin (egitim ritmi sabit kalir). Hic model yoksa True.
    (train_end, egitim as_of'undan purge_days once oldugu icin fiilen ritim retrain_every_days - purge_days
    gundur; sozlesme train_end uzerinden tanimlidir.) (train_end, as_of] araligindaki islem gunu sayisi
    >= training.retrain_every_days ise True. Takvim araligi disindaysa hafta ici gunleriyle yaklasik sayilir
    (tatil bilinmez; hafta ici sayisi islem gunu sayisindan fazla olabilir, yani erken tetiklenebilir).
    """
    every = int(cfg["training"]["retrain_every_days"])
    as_of = _as_day(as_of, "as_of")
    models = list_models(conn)
    if not models:
        return True
    last_end = pd.Timestamp(max(m.train_end for m in models))
    if as_of <= last_end:
        return False
    n = calendar.trading_days_between(last_end, as_of)
    if n is None:
        n = int(np.busday_count((last_end + pd.Timedelta(days=1)).date(), (as_of + pd.Timedelta(days=1)).date()))
    return n >= every
