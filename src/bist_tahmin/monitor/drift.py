"""Drift izleme (PROJE.md Bolum 8, 12): modelin Brier'i, geçmiş-oran baseline'inin Brier'ina gore ne kadar kotu?

Olcut: son `window_dates` FARKLI made_at gunundeki, model ve referansin ORTAK (made_at, ticker) satirlari
uzerinde  Brier(model) - Brier(referans).  Pozitif fark = model referanstan kotu. Fark `brier_over_base_rate_alert`
degerini asarsa alarm. Yetersiz gun varsa alarm YOK ve 'veri yetersiz' notu verilir (az veriyle alarm gurultudur;
etkin orneklem ~ gun sayisi, bkz. PROJE.md Bolum 0).

Saf cekirdek `rolling_brier_gap` / `drift_alert` / `drift_check` veritabanina bagli degildir; `load_resolved`
yalnizca okur.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable
from dataclasses import dataclass

import pandas as pd
import psycopg

from ..evaluate.metrics import brier

REFERENCE_VERSION = "baseline_base_rate_v1"
RESOLVED_COLUMNS = ("made_at", "ticker", "model_version", "model_role", "prob_up", "score", "label_up",
                    "tradable_ret", "resolved_at")


@dataclass(frozen=True)
class DriftResult:
    model_version: str
    gap: float                 # NaN = hesaplanamadi (veri yetersiz)
    alert: bool
    n_days: int                # penceredeki farkli gun sayisi
    n_rows: int
    model_brier: float
    reference_brier: float
    note: str

    def as_dict(self) -> dict:
        return {"model_version": self.model_version, "gap": self.gap, "alert": self.alert, "n_days": self.n_days,
                "n_rows": self.n_rows, "model_brier": self.model_brier, "reference_brier": self.reference_brier,
                "note": self.note}


def load_resolved(conn: psycopg.Connection, versions: Iterable[str]) -> pd.DataFrame:
    """Verilen model surumlerinin COZULMUS canli tahminleri (salt okunur). made_at datetime64[ns]."""
    versions = sorted(set(versions))
    cols = list(RESOLVED_COLUMNS)
    if not versions:
        return pd.DataFrame({c: [] for c in cols})
    rows = conn.execute(
        "SELECT made_at, ticker, model_version, model_role, prob_up, score, label_up, tradable_ret, resolved_at "
        "FROM predictions WHERE resolved_at IS NOT NULL AND model_version = ANY(%s) ORDER BY made_at, ticker",
        (versions,),
    ).fetchall()
    df = pd.DataFrame(rows, columns=cols)
    df["made_at"] = pd.to_datetime(df["made_at"]).astype("datetime64[ns]")
    for c in ("prob_up", "score", "tradable_ret"):
        df[c] = pd.to_numeric(df[c]).astype(float)
    return df


def _matched(df: pd.DataFrame, model_version: str, reference_version: str) -> pd.DataFrame:
    need = {"made_at", "ticker", "model_version", "prob_up", "label_up"}
    if not need.issubset(df.columns):
        raise ValueError(f"eksik sutun(lar): {sorted(need - set(df.columns))}")
    d = df.loc[df["model_version"].isin([model_version, reference_version]), ["made_at", "ticker", "model_version",
                                                                             "prob_up", "label_up"]]
    d = d.dropna(subset=["prob_up", "label_up"])
    if d.empty:   # ornegin reset sonrasi kesim tum satirlari dusurdu: eslesme yok (NaN, alarm yok)
        return pd.DataFrame(columns=["made_at", "ticker", "p_model", "p_ref", "y"])
    if d.duplicated(["made_at", "ticker", "model_version"]).any():
        raise ValueError("ayni (made_at, ticker, model_version) birden fazla satir")
    wide = d.pivot(index=["made_at", "ticker"], columns="model_version", values=["prob_up", "label_up"])
    if model_version not in wide["prob_up"].columns or reference_version not in wide["prob_up"].columns:
        return pd.DataFrame(columns=["made_at", "ticker", "p_model", "p_ref", "y"])
    out = pd.DataFrame({
        "p_model": wide["prob_up"][model_version],
        "p_ref": wide["prob_up"][reference_version],
        "y": wide["label_up"][model_version],
    }).dropna()
    return out.reset_index()


def _gap_details(df: pd.DataFrame, window_dates: int, model_version: str, reference_version: str):
    if int(window_dates) != window_dates or window_dates < 1:
        raise ValueError("window_dates pozitif tam sayi olmali")
    m = _matched(df, model_version, reference_version)
    n_days_all = int(m["made_at"].nunique()) if len(m) else 0
    if n_days_all < window_dates:
        return float("nan"), n_days_all, len(m), float("nan"), float("nan")
    last = sorted(m["made_at"].unique())[-int(window_dates):]
    w = m[m["made_at"].isin(last)]
    bm = brier(w["p_model"].to_numpy(), w["y"].to_numpy())
    br = brier(w["p_ref"].to_numpy(), w["y"].to_numpy())
    return bm - br, int(len(last)), int(len(w)), bm, br


def rolling_brier_gap(
    df: pd.DataFrame, window_dates: int, model_version: str, reference_version: str = REFERENCE_VERSION
) -> float:
    """Modelin son `window_dates` farkli gundeki Brier'i eksi referansin Brier'i (eslesen satirlar).

    `df` kolonlari: made_at, ticker, model_version, prob_up, label_up. Eslesen gun sayisi `window_dates`'ten azsa
    NaN (alarm yok; `drift_check` notu 'veri yetersiz' yazar)."""
    return _gap_details(df, window_dates, model_version, reference_version)[0]


def drift_alert(gap: float | None, threshold: float) -> bool:
    """Fark esigi ASARSA (strict >) alarm. NaN/None (veri yetersiz) alarm vermez."""
    if gap is None or not math.isfinite(float(gap)):
        return False
    return float(gap) > float(threshold)


def drift_check(
    df: pd.DataFrame, model_version: str, cfg: dict, reference_version: str = REFERENCE_VERSION,
    *, after: dt.date | str | pd.Timestamp | None = None,
) -> DriftResult:
    """cfg['drift'] ayarlariyla tek modelin drift durumu (not dahil).

    `after`: yalnizca bu tarihten ITIBAREN (dahil) made_at satirlari degerlendirilir (devre kesici reset'i sonrasi
    yeniden baslangic; eski drift penceresi reset'ten sonra yeniden alarm uretmesin)."""
    window = int(cfg["drift"]["rolling_window"])
    threshold = float(cfg["drift"]["brier_over_base_rate_alert"])
    if after is not None and len(df):
        df = df[pd.to_datetime(df["made_at"]) >= pd.Timestamp(after)]
    gap, n_days, n_rows, bm, br = _gap_details(df, window, model_version, reference_version)
    if not math.isfinite(gap):
        note = f"veri yetersiz: eslesen {n_days} gun < pencere {window}; alarm yok"
        return DriftResult(model_version, float("nan"), False, n_days, n_rows, bm, br, note)
    alert = drift_alert(gap, threshold)
    note = (f"son {n_days} gun: model Brier {bm:.4f}, referans {br:.4f}, fark {gap:+.4f} "
            f"(esik {threshold:+.4f})" + ("; DRIFT ALARMI" if alert else ""))
    return DriftResult(model_version, float(gap), alert, n_days, n_rows, bm, br, note)


__all__ = ["REFERENCE_VERSION", "DriftResult", "drift_alert", "drift_check", "load_resolved", "rolling_brier_gap"]
