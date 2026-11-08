"""Sinama kumesi kilidi (PROJE.md Bolum 5): holdout_start sonrasi veri varsayilan olarak verilmez.

Kilitliyken iki sey engellenir:
1. Tarihi holdout_start'a esit/sonra olan satirlar (erisim).
2. Etiketi holdout'a uzanan satirlar (end_date >= holdout_start): etiket bilgisi sinama kumesinden gelir.
Kilit yalnizca acik bayrakla (use_holdout=True) acilir; bu durum loglanir ve access_note() deney
kaydina yazilmak uzere metin dondurur.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


class HoldoutLocked(RuntimeError):
    """Kilitli sinama kumesi verisine erisim denemesi."""


def _to_naive_ts(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts is pd.NaT:
        raise ValueError("eksik (NaT) tarih")
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    return ts


def _naive_series(values: Any) -> pd.Series:
    s = pd.to_datetime(pd.Series(values))
    if getattr(s.dt, "tz", None) is not None:
        s = s.dt.tz_localize(None)
    return s


@dataclass(frozen=True)
class HoldoutGuard:
    holdout_start: pd.Timestamp
    use_holdout: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "holdout_start", _to_naive_ts(self.holdout_start).normalize())
        if self.use_holdout:
            log.warning(
                "SINAMA KUMESI KILIDI ACIK (holdout_start=%s): yalnizca terfi asamasinda kullanilmali; "
                "bu erisim deney kaydina yazilmali.",
                self.holdout_start.date(),
            )

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any], use_holdout: bool = False) -> HoldoutGuard:
        return cls(holdout_start=_to_naive_ts(cfg["holdout"]["holdout_start"]), use_holdout=use_holdout)

    def access_note(self) -> str:
        """Deney kaydina yazilacak metin: kilit durumu."""
        if self.use_holdout:
            return f"SINAMA KUMESI ACIK: holdout_start={self.holdout_start.date()}, --use-holdout ile erisildi"
        return f"sinama kumesi kilitli: holdout_start={self.holdout_start.date()}"

    def check(self, *dates: Any) -> None:
        """Kilitliyken herhangi bir tarih >= holdout_start ise HoldoutLocked. Skaler veya dizi kabul eder."""
        if self.use_holdout:
            return
        for item in dates:
            if isinstance(item, (str, dt.date, np.datetime64, pd.Timestamp)):
                values = [_to_naive_ts(item)]
            else:
                values = list(_naive_series(item))
            for ts in values:
                if pd.isna(ts):
                    raise ValueError("eksik (NaT) tarih kontrol edilemez")
                if ts >= self.holdout_start:
                    raise HoldoutLocked(
                        f"sinama kumesi kilitli: {ts.date()} >= holdout_start ({self.holdout_start.date()})"
                    )

    def clip_end(self) -> pd.Timestamp | None:
        """Kilitliyken izinli son gun (holdout_start - 1 gun); kilit aciksa None."""
        if self.use_holdout:
            return None
        return self.holdout_start - pd.Timedelta(days=1)

    def filter_frame(self, df: pd.DataFrame, date_col: str = "date", end_col: str | None = None) -> pd.DataFrame:
        """Kilitliyken holdout satirlarini ve etiketi holdout'a uzanan satirlari dusurur.

        end_col verilirse etiket bitisi bilinmeyen (NaT) satirlar da dusurulur (muhafazakar).
        Kilit aciksa df.copy().
        """
        if self.use_holdout:
            return df.copy()
        if date_col not in df.columns:
            raise ValueError(f"sutun yok: {date_col}")
        keep = (_naive_series(df[date_col]) < self.holdout_start).to_numpy()
        if end_col is not None:
            if end_col not in df.columns:
                raise ValueError(f"sutun yok: {end_col}")
            ends = _naive_series(df[end_col])
            keep = keep & (ends < self.holdout_start).to_numpy()  # NaT < x -> False: dusurulur
        dropped = int((~keep).sum())
        if dropped:
            log.info("sinama kumesi kilidi: %d satir dusuruldu (holdout_start=%s)", dropped, self.holdout_start.date())
        return df.loc[keep].copy()
