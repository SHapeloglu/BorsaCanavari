"""Veri kalite kontrolleri (PROJE.md Bolum 4).

Ilke: hicbir kontrol veriyi sessizce degistirmez, girdiyi mutate etmez. Sorunlar raporlanir;
"error" bir sorun varsa o hisse bugun kullanilamaz (usable=False), "flag" yalnizca isarettir.

Veri sozlesmesi: tek hisse icin DataFrame; index = tz-naive normalize edilmis DatetimeIndex (ad "date");
kolonlar: open, high, low, close, volume, close_adj.

Yarim bar kurali: `now` (Europe/Istanbul) bugunun tarihini tasiyan bir bar varsa ve now.time() <
close_cutoff_time ise o bar eksik/yarim sayilir. Sinir dahil: now.time() == close_cutoff_time ise bar
KABUL edilir (kapanis + gecikme penceresi dolmus). `check_bars` bu barin silinmis halini inceler ve
"incomplete_today_bar" flag'i yazar; barin gercekten dusurulmesi cagiranin isidir
(`drop_incomplete_today_bar`). Gelecek tarihli bar (bugunden sonra) her zaman error'dur.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ERROR = "error"
FLAG = "flag"

REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume", "close_adj")


@dataclass(frozen=True)
class QualityIssue:
    code: str
    severity: str  # "error" | "flag"
    detail: str
    dates: tuple[pd.Timestamp, ...] = ()


@dataclass
class QualityReport:
    ticker: str
    issues: list[QualityIssue] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return not any(i.severity == ERROR for i in self.issues)

    @property
    def codes(self) -> set[str]:
        return {i.code for i in self.issues}

    @property
    def summary(self) -> str:
        """Hata (error) seviyesindeki sorunlarin kisa ozeti; dislama gerekcesi olarak kullanilir."""
        errs = [f"{i.code}: {i.detail}" for i in self.issues if i.severity == ERROR]
        return "; ".join(errs) if errs else "sorun yok"

    def get(self, code: str) -> QualityIssue | None:
        return next((i for i in self.issues if i.code == code), None)


def _now_local(now, tz: str) -> pd.Timestamp:
    """`now` verilmezse simdi; tz-naive ise `tz` saatinde sayilir; tz-aware ise `tz`ye cevrilir."""
    zone = ZoneInfo(tz)
    ts = pd.Timestamp.now(tz=zone) if now is None else pd.Timestamp(now)
    return ts.tz_localize(zone) if ts.tzinfo is None else ts.tz_convert(zone)


def _today_incomplete(now, close_cutoff_time: dt.time, tz: str) -> tuple[pd.Timestamp, bool]:
    local = _now_local(now, tz)
    today = pd.Timestamp(local.date())
    return today, local.time() < close_cutoff_time


def drop_incomplete_today_bar(
    bars: pd.DataFrame,
    now=None,
    *,
    close_cutoff_time: dt.time = dt.time(18, 30),
    tz: str = "Europe/Istanbul",
) -> pd.DataFrame:
    """Bugunun yarim barini siler (cutoff'tan once). Girdiyi degistirmez; her zaman yeni bir DataFrame doner.

    Cutoff sonrasi (sinir dahil) bugunun bari korunur. Bugunden sonraki barlara dokunulmaz
    (onlar `check_bars`te error olur).
    """
    today, incomplete = _today_incomplete(now, close_cutoff_time, tz)
    if not incomplete:
        return bars.copy()
    return bars.loc[bars.index != today].copy()


def _fmt(dates: Iterable[pd.Timestamp], limit: int = 5) -> str:
    ds = [d.date().isoformat() for d in dates]
    more = f" (+{len(ds) - limit})" if len(ds) > limit else ""
    return ", ".join(ds[:limit]) + more


def _jumps(series: pd.Series, threshold: float) -> list[pd.Timestamp]:
    """Ardisik (gecerli) barlar arasi |getiri| > threshold olan gunler (ikinci barin tarihi)."""
    s = series.where(series > 0)
    prev = s.shift(1)
    ret = s / prev - 1.0
    return list(ret.index[ret.abs() > threshold])


def check_bars(
    bars: pd.DataFrame,
    ticker: str,
    *,
    trading_days: Iterable | None = None,
    now=None,
    jump_threshold: float = 0.35,
    close_cutoff_time: dt.time = dt.time(18, 30),
    tz: str = "Europe/Istanbul",
    missing_window: int = 250,
    max_missing_ratio: float = 0.05,
) -> QualityReport:
    """Tek hisse icin kalite kontrolu. `bars` mutate edilmez, hicbir bar silinmez/duzeltilmez.

    Kodlar (severity):
    - missing_columns, empty (error): yapisal sorun, diger kontroller atlanir.
    - bad_index (error): DatetimeIndex degil / tz-aware / normalize degil / NaT iceriyor.
    - not_monotonic (error), duplicate_dates (error)
    - bad_close, bad_close_adj (error): <= 0 ya da NaN
    - future_dates (error): bugunden sonraki bar
    - jump_close, jump_close_adj (flag): ardisik gunler arasi |getiri| > jump_threshold
    - zero_volume (flag), nan_volume (flag)
    - incomplete_today_bar (flag): bugunun yarim bari (cutoff oncesi); kalan kontroller bu bar olmadan yapilir
    - missing_trading_days (flag): trading_days'te olup barsiz gunler ([ilk bar, bugun] araliginda)
    - excessive_missing_days (error): son `missing_window` beklenen gunde eksik orani > max_missing_ratio
    - bars_on_non_trading_days (flag): barin oldugu ama trading_days'te olmayan gunler
    """
    report = QualityReport(ticker=ticker)
    add = report.issues.append

    missing_cols = [c for c in REQUIRED_COLUMNS if c not in bars.columns]
    if missing_cols:
        add(QualityIssue("missing_columns", ERROR, f"eksik kolonlar: {missing_cols}"))
        return report
    if len(bars) == 0:
        add(QualityIssue("empty", ERROR, "bos veri"))
        return report

    idx = bars.index
    if not isinstance(idx, pd.DatetimeIndex):
        add(QualityIssue("bad_index", ERROR, f"index DatetimeIndex degil: {type(idx).__name__}"))
        return report
    if idx.tz is not None:
        add(QualityIssue("bad_index", ERROR, "index tz-aware (tz-naive bekleniyor)"))
        return report
    nat = idx.isna()
    if nat.any():
        add(QualityIssue("bad_index", ERROR, f"index {int(nat.sum())} adet NaT iceriyor"))
        return report
    if not (idx == idx.normalize()).all():
        add(QualityIssue("bad_index", ERROR, "index normalize edilmemis (saat bilgisi var)"))
        return report

    today, incomplete = _today_incomplete(now, close_cutoff_time, tz)

    # (a) siralama, (b) tekrar
    if not idx.is_monotonic_increasing:
        bad = idx[1:][np.asarray(idx[1:] < idx[:-1])]
        add(QualityIssue("not_monotonic", ERROR, f"tarih sirasi bozuk ({len(bad)} geri adim): {_fmt(bad)}", tuple(bad)))
    dup = idx[idx.duplicated(keep=False)].unique()
    if len(dup):
        add(QualityIssue("duplicate_dates", ERROR, f"tekrar eden tarih ({len(dup)}): {_fmt(dup)}", tuple(dup)))

    # gelecek tarih (bugunden sonrasi)
    future = idx[idx > today].unique()
    if len(future):
        add(QualityIssue("future_dates", ERROR, f"gelecek tarihli bar ({len(future)}): {_fmt(future)}", tuple(future)))

    # (g) bugunun yarim bari
    if incomplete and (idx == today).any():
        add(QualityIssue(
            "incomplete_today_bar", FLAG,
            f"bugunun bari ({today.date()}) {close_cutoff_time:%H:%M} oncesi yarim sayildi; "
            "kontroller bu bar olmadan yapildi, kullanmadan once drop_incomplete_today_bar uygulayin",
            (today,),
        ))
        view = bars.loc[idx != today]
    else:
        view = bars

    # sonraki kontroller icin yalnizca ic gorunum: siralanmis, tekrarsiz (ilk kayit); bars'a dokunulmaz
    work = view.sort_index(kind="stable")
    work = work[~work.index.duplicated(keep="first")]

    # (c) close <= 0 veya NaN
    for col, code in (("close", "bad_close"), ("close_adj", "bad_close_adj")):
        s = pd.to_numeric(work[col], errors="coerce")
        bad_mask = ~(s > 0)  # NaN ve <= 0 ikisi de yakalanir
        if bad_mask.any():
            n_nan = int(s.isna().sum())
            n_nonpos = int(bad_mask.sum()) - n_nan
            add(QualityIssue(
                code, ERROR, f"{col} gecersiz: {n_nan} NaN, {n_nonpos} sifir/negatif: {_fmt(work.index[bad_mask])}",
                tuple(work.index[bad_mask]),
            ))

    # (d) sicrama (silme yok, yalnizca isaret)
    for col, code in (("close", "jump_close"), ("close_adj", "jump_close_adj")):
        s = pd.to_numeric(work[col], errors="coerce")
        js = _jumps(s, jump_threshold)
        if js:
            add(QualityIssue(
                code, FLAG, f"{col}: |gunluk getiri| > {jump_threshold:.0%} ({len(js)} gun): {_fmt(js)}", tuple(js),
            ))

    # (e) hacim
    vol = pd.to_numeric(work["volume"], errors="coerce")
    zero = work.index[vol == 0]
    if len(zero):
        add(QualityIssue("zero_volume", FLAG, f"hacmi sifir gun ({len(zero)}): {_fmt(zero)}", tuple(zero)))
    nanv = work.index[vol.isna()]
    if len(nanv):
        add(QualityIssue("nan_volume", FLAG, f"hacmi NaN gun ({len(nanv)}): {_fmt(nanv)}", tuple(nanv)))

    # (f) eksik islem gunleri
    if trading_days is not None and len(work):
        td = pd.DatetimeIndex(pd.to_datetime(list(trading_days)))
        if td.tz is not None:
            td = td.tz_localize(None)
        td = td.normalize().unique().sort_values()
        have = work.index
        extra = have[~have.isin(td)]
        if len(extra):
            add(QualityIssue(
                "bars_on_non_trading_days", FLAG,
                f"trading_days'te olmayan gunlerde bar ({len(extra)}): {_fmt(extra)}", tuple(extra),
            ))
        expected = td[(td >= have[0]) & (td <= today)]
        if incomplete:
            expected = expected[expected != today]  # bugunun barini bekleme: yarim sayildi
        missing = expected[~expected.isin(have)]
        if len(missing):
            add(QualityIssue(
                "missing_trading_days", FLAG,
                f"bu hissede eksik islem gunu ({len(missing)}): {_fmt(missing)}", tuple(missing),
            ))
            window = expected[-missing_window:]
            miss_w = window[~window.isin(have)]
            ratio = len(miss_w) / len(window) if len(window) else 0.0
            if ratio > max_missing_ratio:
                add(QualityIssue(
                    "excessive_missing_days", ERROR,
                    f"son {len(window)} beklenen gunde eksik orani {ratio:.1%} > {max_missing_ratio:.1%} "
                    f"({len(miss_w)} gun)", tuple(miss_w),
                ))

    return report
