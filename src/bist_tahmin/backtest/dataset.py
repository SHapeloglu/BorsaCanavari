"""Faz 3.5 veri seti insa edicisi: (date, ticker) basina ozellik + etiket tablosu (PROJE.md Bolum 5, 9, 15).

Tek sorumluluk: backtest laboratuvarinin (lab.py) tuketecegi `Dataset`i uretmek.

Sizinti ayrimi (en kritik kural):
- OZELLIKLER yalnizca `features.build.build_features` ile, her gun icin o gunun made_at'iyle uretilir; bu modul
  ozellik hesabi yapmaz. Ozellikler etiket kolonlarini ASLA gormez: `feature_fn`e yalnizca fiyat cercevesi,
  XU100 ve takvim verilir.
- ETIKETLER (end_date, fwd_ret, fwd_rel_ret, tradable_ret, label_*) ozellik uretiminden tamamen ayri, tum seriden
  vektorel hesaplanir. Vadesi takvim ya da veri disinda kalan satir NaN/NaT olur; hicbir sey uydurulmaz.
- Evren tarihlidir: bir gun icin yalnizca o gun `members_on(...)` icindeki hisseler (hayatta kalma yanliligi yok).
  `build_features`e de yalniz o gunun uyeleri verilir; yani z-skor kesiti ve piyasa genisligi o gunun gercek
  evreninden hesaplanir (canli `predict.daily` ile ayni).
- Preflight: olusan veri setinden esit aralikli birkac gunde `assert_no_lookahead` calistirilir; sizinti varsa
  AssertionError yutulmaz.

Etiket tanimlari resolver.py ile aynidir ve `close_adj`/`open_adj` (bolunme duzeltmeli) uzerindedir:
- fwd_ret       = close_adj[end] / close_adj[date] - 1
- fwd_rel_ret   = fwd_ret - (XU100 close_adj[end] / XU100 close_adj[date] - 1)
- tradable_ret  = close_adj[end] / open_adj[next_day] - 1           (next_day = takvimde date'ten sonraki gun)
- label_up      = fwd_ret > label_threshold ; label_rel_up = fwd_rel_ret > 0   (float 0/1, hesaplanamazsa NaN)
Resolver'in `adjustment_mismatch` / `unexplained_jump` korumalari BURADA YOKTUR (corporate_actions okunmaz);
etiketler dogrudan close_adj'den gelir. Veri hatasi (kaynak bolunmeyi uygulamamis) varsa yanlis etiket uretebilir.

Performans: build_features gun basina ~60 ms (30 hisse; pandas ek yuku, seri uzunlugundan neredeyse bagimsiz),
yani 1800 gun ~2 dk tek surecte. Gunler birbirinden bagimsiz oldugu icin `n_jobs` ile fork'lu surec havuzunda
paralel hesaplanabilir (sonuc birebir ayni; varsayilan: 400 gunden buyukse en fazla 4 surec).
"""

from __future__ import annotations

import datetime as dt
import logging
import multiprocessing
import os
import pickle
import tempfile
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..calendar import TradingCalendar
from ..data import store
from ..data.universe import members_on
from ..features.build import (
    FEATURE_NAMES,
    FEATURE_VERSION,
    FeatureFn,
    assert_no_lookahead,
    build_features,
)

log = logging.getLogger(__name__)

INDEX_SYMBOL = "XU100"
DATASET_FORMAT = 1

LABEL_COLUMNS: tuple[str, ...] = ("end_date", "fwd_ret", "fwd_rel_ret", "tradable_ret", "label_up", "label_rel_up")
_NAT = np.datetime64("NaT", "ns")
FRAME_COLUMNS: tuple[str, ...] = ("date", "ticker", *FEATURE_NAMES, *LABEL_COLUMNS)


@dataclass
class Dataset:
    frame: pd.DataFrame          # FRAME_COLUMNS; date,ticker'a gore sirali, RangeIndex
    horizon_days: int
    label_threshold: float
    feature_names: list[str]
    meta: dict[str, Any] = field(default_factory=dict)


# --- yardimcilar ---------------------------------------------------------------------------------------------

def _prepare(df: pd.DataFrame, name: str) -> pd.DataFrame:
    """Sirali, tekrarsiz, tz-naive indeksli cercevenin goruntusu (girdi degistirilmez)."""
    if not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is not None:
        raise ValueError(f"{name}: index tz-naive DatetimeIndex olmali")
    if df.index.has_duplicates:
        raise ValueError(f"{name}: tekrar eden tarih var")
    if "close_adj" not in df.columns:
        raise ValueError(f"{name}: close_adj kolonu yok")
    return df if df.index.is_monotonic_increasing else df.sort_index()


def _grid_series(df: pd.DataFrame, col: str, days: pd.DatetimeIndex) -> np.ndarray:
    """Takvim gridine hizalanmis, gecersiz (<=0, sonsuz, NaN) degerleri NaN olan kolon. Kolon yoksa hepsi NaN."""
    if col not in df.columns:
        return np.full(len(days), np.nan)
    s = df[col].astype("float64").reindex(days)
    return s.where(np.isfinite(s) & (s > 0)).to_numpy(dtype=float)


def _open_adj_series(df: pd.DataFrame, days: pd.DatetimeIndex) -> np.ndarray:
    """open_adj varsa o; yoksa open * close_adj / close (store.read_prices ile ayni tanim)."""
    if "open_adj" in df.columns:
        return _grid_series(df, "open_adj", days)
    if "open" in df.columns and "close" in df.columns:
        with np.errstate(all="ignore"):
            out = (_grid_series(df, "open", days) * _grid_series(df, "close_adj", days)
                   / _grid_series(df, "close", days))
        out[~np.isfinite(out)] = np.nan
        return out
    return np.full(len(days), np.nan)


def compute_labels(
    close_adj: np.ndarray, open_adj: np.ndarray, xu_close: np.ndarray, horizon_days: int, label_threshold: float
) -> dict[str, np.ndarray]:
    """Takvim gridi uzerinde, TUM gunler icin etiket dizileri (ozellik hesabindan bagimsiz, saf numpy).

    Tum girdiler takvim gunlerine hizali, gecersiz deger NaN. i. gunun vadesi i+H, ertesi gun i+1.
    Vade grid disindaysa sonuc NaN (uydurma yok). `end_idx`: vade konumu, yoksa -1.
    """
    n = len(close_adj)
    h = int(horizon_days)
    idx = np.arange(n)
    end_idx = np.where(idx + h < n, idx + h, -1)
    nxt_idx = np.where(idx + 1 < n, idx + 1, -1)
    has_end = end_idx >= 0

    def at(a: np.ndarray, ix: np.ndarray) -> np.ndarray:
        out = np.where(ix >= 0, a[np.where(ix >= 0, ix, 0)], np.nan)
        return out

    c0, c1 = close_adj, at(close_adj, end_idx)
    x0, x1 = xu_close, at(xu_close, end_idx)
    o1 = at(open_adj, nxt_idx)
    with np.errstate(all="ignore"):
        fwd = c1 / c0 - 1.0
        rel = fwd - (x1 / x0 - 1.0)
        trad = c1 / o1 - 1.0
    # H == 1 ve sonuncu gun gibi durumlar zaten NaN: has_end maskesi (NaN zaten ama acik olsun)
    fwd = np.where(has_end, fwd, np.nan)
    rel = np.where(has_end, rel, np.nan)
    trad = np.where(has_end, trad, np.nan)
    with np.errstate(invalid="ignore"):
        label_up = np.where(np.isfinite(fwd), (fwd > label_threshold).astype(float), np.nan)
        label_rel = np.where(np.isfinite(rel), (rel > 0.0).astype(float), np.nan)
    return {"end_idx": end_idx, "fwd_ret": fwd, "fwd_rel_ret": rel, "tradable_ret": trad,
            "label_up": label_up, "label_rel_up": label_rel}


def _pick_evenly(items: Sequence, k: int) -> list:
    if k <= 0 or not items:
        return []
    if k >= len(items):
        return list(items)
    pos = np.unique(np.round(np.linspace(0, len(items) - 1, k)).astype(int))
    return [items[i] for i in pos]


# --- cekirdek ------------------------------------------------------------------------------------------------

# Ozellik hesabi icin is-parcasi durumu (fork ile cocuk sureclere miras kalir; sozlesme: yalniz okunur).
_WORK: dict[str, Any] = {}


def _compute_day(step: int) -> tuple[int, list[tuple[str, list[float]]], list[str]]:
    """plan[step] gunu icin (takvim konumu, [(ticker, ozellik vektoru)], dislama gerekceleri)."""
    p, d, have = _WORK["plan"][step]
    hist, fn = _WORK["hist"], _WORK["fn"]
    fs = fn({t: hist[t] for t in have}, _WORK["xu"], d, _WORK["calendar"])
    out: list[tuple[str, list[float]]] = []
    for t in sorted(fs.rows):
        feats = fs.rows[t].features
        missing = [n for n in FEATURE_NAMES if n not in feats]
        if missing:
            raise ValueError(f"{d.date()} {t}: ozellik fonksiyonu eksik ozellik dondurdu: {missing[:5]}")
        out.append((t, [feats[n] for n in FEATURE_NAMES]))
    return p, out, [r.split(":")[0] for r in fs.excluded.values()]


def _compute_chunk(steps: range) -> list:
    return [_compute_day(s) for s in steps]


def _resolve_jobs(n_jobs: int | None, n_steps: int) -> int:
    if n_jobs is None:   # otomatik: kucuk islerde tek surec (test, kisa aralik); buyukte en fazla 4 surec
        n_jobs = 1 if n_steps < 400 else min(4, os.cpu_count() or 1)
    if n_jobs < 1:
        raise ValueError(f"n_jobs >= 1 olmali: {n_jobs}")
    if n_jobs > 1 and "fork" not in multiprocessing.get_all_start_methods():
        return 1
    return n_jobs


def build_dataset_from_frames(
    histories: Mapping[str, pd.DataFrame],
    xu100: pd.DataFrame,
    calendar: TradingCalendar,
    membership_rows,
    cfg: Mapping[str, Any],
    *,
    start=None,
    end=None,
    preflight_dates: int = 3,
    feature_fn: FeatureFn | None = None,
    n_jobs: int | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> Dataset:
    """Saf cekirdek (DB'siz). `start`/`end` made_at tarihlerini (dahil) sinirlar; etiketler `end` sonrasi veriyi
    kullanabilir (vade). `feature_fn` varsayilan `build_features` (test icin sizintili fonksiyon enjekte edilebilir).
    `n_jobs`: gun basina ozellik hesabi fork'lu surec havuzunda paralel yapilabilir (sonuc ayni; None = otomatik).

    meta anahtarlari: feature_version, built_at (UTC ISO), n_rows, n_days, date_min, date_max, start, end,
    skipped_tickers (uyeligi olup fiyati hic olmayanlar), skipped_days (XU100 bari olmayan gunler),
    excluded_counts (ozellik uretilemeyen (gun, hisse) gerekce sayilari), preflight_dates_checked.
    """
    horizon = int(cfg["horizon_days"])
    thr = float(cfg["label_threshold"])
    if horizon < 1:
        raise ValueError(f"horizon_days >= 1 olmali: {horizon}")
    custom_fn = feature_fn is not None and feature_fn is not build_features
    fn: FeatureFn = feature_fn if feature_fn is not None else build_features

    xu = _prepare(xu100, "xu100")
    hist = {t: _prepare(df, t) for t, df in histories.items()}
    days = calendar.days
    start_ts = pd.Timestamp(start).normalize() if start is not None else days[0]
    end_ts = pd.Timestamp(end).normalize() if end is not None else days[-1]
    if start_ts > end_ts:
        raise ValueError(f"start ({start_ts.date()}) end ({end_ts.date()}) sonrasinda")

    rows_membership = list(membership_rows)
    in_range = days[(days >= start_ts) & (days <= end_ts)]
    xu_grid = _grid_series(xu, "close_adj", days)
    xu_days = set(days[np.isfinite(xu_grid)])

    # --- gun listesi ve uyeler (tarihli evren)
    plan: list[tuple[int, pd.Timestamp, list[str]]] = []   # (takvim konumu, gun, fiyati olan uyeler)
    skipped_tickers: set[str] = set()
    skipped_days = 0
    for d in in_range:
        if d not in xu_days:
            skipped_days += 1
            continue
        members = members_on(rows_membership, d.date())
        have = [t for t in members if t in hist and len(hist[t]) > 0]
        skipped_tickers.update(t for t in members if t not in have)
        if have:
            plan.append((int(days.get_loc(d)), d, have))

    # --- ozellikler: her gun build_features(made_at = o gun); etiketlerden tamamen ayri
    _WORK.clear()
    _WORK.update(plan=plan, hist=hist, xu=xu, calendar=calendar, fn=fn)
    results: list = []
    total = len(plan)
    jobs = _resolve_jobs(n_jobs, total)
    try:
        if jobs == 1:
            for step in range(total):
                results.append(_compute_day(step))
                if progress is not None and (step % 25 == 0 or step == total - 1):
                    progress(step + 1, total)
        else:
            size = max(1, -(-total // (jobs * 8)))
            chunks = [range(i, min(i + size, total)) for i in range(0, total, size)]
            ctx = multiprocessing.get_context("fork")
            with ProcessPoolExecutor(max_workers=jobs, mp_context=ctx) as ex:
                for chunk_res in ex.map(_compute_chunk, chunks):
                    results.extend(chunk_res)
                    if progress is not None:
                        progress(len(results), total)
    finally:
        _WORK.clear()

    feat_rows: list[list[float]] = []
    keys: list[tuple[int, str]] = []      # (takvim konumu, ticker)
    excluded_counts: dict[str, int] = {}
    for p, rows, reasons in results:
        for t, vec in rows:
            feat_rows.append(vec)
            keys.append((p, t))
        for key in reasons:
            excluded_counts[key] = excluded_counts.get(key, 0) + 1

    # --- etiketler: ozellik hesabindan ayri, tum seriden
    label_cols: dict[str, np.ndarray] = {k: np.full(len(keys), np.nan) for k in LABEL_COLUMNS[1:]}
    end_dates = np.full(len(keys), _NAT, dtype="datetime64[ns]")
    if keys:
        key_tickers = np.array([t for _, t in keys])
        key_pos = np.array([p for p, _ in keys])
        days_ns = days.to_numpy(dtype="datetime64[ns]")
        for t in sorted(set(key_tickers)):
            lab = compute_labels(_grid_series(hist[t], "close_adj", days), _open_adj_series(hist[t], days), xu_grid, horizon, thr)
            sel = np.flatnonzero(key_tickers == t)
            pp = key_pos[sel]
            for name in ("fwd_ret", "fwd_rel_ret", "tradable_ret", "label_up", "label_rel_up"):
                label_cols[name][sel] = lab[name][pp]
            ei = lab["end_idx"][pp]
            end_dates[sel] = np.where(ei >= 0, days_ns[np.where(ei >= 0, ei, 0)], _NAT)
        date_vals = days_ns[key_pos]
        ticker_vals = key_tickers
    else:
        date_vals = np.array([], dtype="datetime64[ns]")
        ticker_vals = np.array([], dtype=str)

    frame = pd.DataFrame(np.array(feat_rows, dtype=float).reshape(len(keys), len(FEATURE_NAMES)),
                         columns=list(FEATURE_NAMES))
    frame.insert(0, "ticker", pd.Series(ticker_vals, dtype=object).astype(str).to_numpy(dtype=object))
    frame.insert(0, "date", pd.DatetimeIndex(date_vals).astype("datetime64[ns]"))
    frame["end_date"] = pd.DatetimeIndex(end_dates).astype("datetime64[ns]")
    for name in ("fwd_ret", "fwd_rel_ret", "tradable_ret", "label_up", "label_rel_up"):
        frame[name] = label_cols[name]
    frame = frame.sort_values(["date", "ticker"], kind="mergesort").reset_index(drop=True)
    frame = frame[list(FRAME_COLUMNS)]

    # --- preflight (sizinti dedektoru): olusan veri setinin gunleri arasindan esit aralikli
    checked = _run_preflight(fn, hist, xu, calendar, rows_membership, frame, plan, preflight_dates)

    meta: dict[str, Any] = {
        "feature_version": FEATURE_VERSION,
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "n_rows": int(len(frame)),
        "n_days": int(frame["date"].nunique()) if len(frame) else 0,
        "date_min": frame["date"].min().date().isoformat() if len(frame) else None,
        "date_max": frame["date"].max().date().isoformat() if len(frame) else None,
        "start": start_ts.date().isoformat(),
        "end": end_ts.date().isoformat(),
        "skipped_tickers": sorted(skipped_tickers),
        "skipped_days": int(skipped_days),
        "excluded_counts": dict(sorted(excluded_counts.items())),
        "preflight_dates_checked": [d.date().isoformat() for d in checked],
        "custom_feature_fn": bool(custom_fn),
        "horizon_days": horizon,
        "label_threshold": thr,
    }
    return Dataset(frame=frame, horizon_days=horizon, label_threshold=thr,
                   feature_names=list(FEATURE_NAMES), meta=meta)


def _run_preflight(fn, hist, xu, calendar, rows_membership, frame, plan, k) -> list[pd.Timestamp]:
    """Veri setinde satiri olan ve sonrasinda verisi bulunan gunlerden esit aralikli k tanesinde sizinti dedektoru.
    AssertionError yutulmaz. Kontrol edilen gunleri doner (bos olabilir: dogrulanmis sayilmaz, meta'ya yazilir)."""
    if k <= 0 or len(frame) == 0:
        return []
    members_by_day = {d: have for _, d, have in plan}
    row_days = pd.DatetimeIndex(frame["date"].unique())
    last_any = max([calendar.last_day, xu.index.max(), *[df.index.max() for df in hist.values() if len(df)]])
    candidates = [d for d in row_days if d < last_any and d in members_by_day]
    checked: list[pd.Timestamp] = []
    for d in _pick_evenly(candidates, k):
        sub = {t: hist[t] for t in members_by_day[d]}
        assert_no_lookahead(fn, sub, xu, d, calendar)
        checked.append(d)
    return checked


# --- DB girisi -----------------------------------------------------------------------------------------------

def build_dataset(
    conn, cfg, *, start=None, end=None, tickers=None, preflight_dates: int = 3,
    progress: Callable[[int, int], None] | None = None,
) -> Dataset:
    """DB'den okuyup `build_dataset_from_frames` cagirir. `tickers` verilirse evren o kumeye daraltilir
    (uyelik yine tarihlidir). Fiyatlar tum gecmisle (ozellik gecmisi ve vade icin) okunur."""
    calendar = store.read_calendar(conn)
    membership = store.read_membership(conn)
    if tickers is not None:
        keep = {t.upper() for t in tickers}
        membership = [m for m in membership if m.ticker in keep]
    ever = sorted({m.ticker for m in membership})
    prices = store.read_prices(conn, [*ever, INDEX_SYMBOL])
    xu = prices.pop(INDEX_SYMBOL, None)
    if xu is None or len(xu) == 0:
        raise ValueError("XU100 fiyat gecmisi yok; once `bist fetch` calistirin")
    histories = {t: prices[t] for t in ever if t in prices}
    return build_dataset_from_frames(histories, xu, calendar, membership, cfg, start=start, end=end,
                                     preflight_dates=preflight_dates, progress=progress)


# --- kaydet / yukle ------------------------------------------------------------------------------------------

def save_dataset(ds: Dataset, path: str | os.PathLike[str]) -> None:
    """pickle (pandas.to_pickle) ile atomik yazim (gecici dosya + rename). Yalniz kendi uretiminiz olan dosyalari
    yukleyin: pickle guvenilmeyen girdiyle calistirilmamalidir."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": DATASET_FORMAT, "frame": ds.frame, "horizon_days": ds.horizon_days,
        "label_threshold": ds.label_threshold, "feature_names": list(ds.feature_names), "meta": dict(ds.meta),
    }
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".", suffix=".tmp")
    os.close(fd)
    try:
        pd.to_pickle(payload, tmp, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_dataset(path: str | os.PathLike[str]) -> Dataset:
    payload = pd.read_pickle(path)
    if not isinstance(payload, dict) or payload.get("format") != DATASET_FORMAT:
        raise ValueError(f"{path}: taninmayan veri seti bicimi (beklenen format={DATASET_FORMAT})")
    ds = Dataset(frame=payload["frame"], horizon_days=int(payload["horizon_days"]),
                 label_threshold=float(payload["label_threshold"]),
                 feature_names=list(payload["feature_names"]), meta=dict(payload["meta"]))
    missing = [c for c in [*ds.feature_names, "date", "ticker", *LABEL_COLUMNS] if c not in ds.frame.columns]
    if missing:
        raise ValueError(f"{path}: veri seti kolonlari eksik: {missing[:5]}")
    if ds.meta.get("feature_version") != FEATURE_VERSION:
        log.warning("%s: veri seti ozellik surumu %r, kodun surumu %r", path, ds.meta.get("feature_version"),
                    FEATURE_VERSION)
    return ds
