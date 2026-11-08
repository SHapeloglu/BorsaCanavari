"""Islem gunu takvimi ve ufuk sayimi (PROJE.md Bolum 3, 4, 5).

Gunler = XU100'de bar olan gunler (sirali, tekrarsiz). Hafta ici olup XU100'de bar olmayan gun tatildir.
Takvim sonrasi hicbir sey tahmin edilmez/uydurulmaz: takvimin bilmedigi sonuc icin `None` doner.

Kenar durum kurallari (hepsi burada ve testlerde sabitlenmistir):
- Girdi tarihleri tz-naive olmali (tz-aware ise ValueError); saat bilgisi atilir (normalize).
- `next_trading_day` / `prev_trading_day`: d'den KESINLIKLE sonraki / onceki islem gunu. Bulunamazsa None.
- `add_trading_days(d, n)`: d'den sonraki n'inci islem gunu (d dahil degil). d islem gunu olmak zorunda degil:
  cumartesi + 1 = pazartesi. n == 0 yalnizca d bir islem gunuyse d'yi dondurur; degilse ValueError
  (sessizce kaydirma yok). n < 0 ValueError. Takvim yetmezse None.
- `trading_days_between(a, b)`: (a, b] araligindaki islem gunu sayisi; a > b ise negatif. a ya da b
  takvim araligi [ilk gun, son gun] disindaysa sayim eksik olacagindan None.
- `horizon_end(made_at, horizon_days)`: made_at'ten horizon_days islem gunu sonrasi. made_at islem gunu
  olmak zorunda: takvim icindeki islem gunu olmayan tarih icin ValueError; takvimin son gununden sonraki
  tarih icin None (bilinemez). horizon_days >= 1.
- `is_trading_day`: takvimde yoksa False (takvim disi tarihler dahil; kapsam icin `covers`).
"""

from __future__ import annotations

import datetime as dt
import operator
from collections.abc import Iterable

import holidays
import pandas as pd


def _to_day(d) -> pd.Timestamp:
    ts = pd.Timestamp(d)
    if ts.tzinfo is not None:
        raise ValueError(f"tz-naive tarih bekleniyor, gelen: {d!r}")
    return ts.normalize()


def _to_index(days: Iterable) -> pd.DatetimeIndex:
    """Tarih benzeri elemanlardan tz-naive normalize edilmis, sirali, tekrarsiz indeks (karisik bicimler olabilir)."""
    if isinstance(days, pd.DatetimeIndex):
        idx = days
    else:
        items = [pd.Timestamp(x) for x in days]
        if any(t.tzinfo is not None for t in items):
            raise ValueError("tz-naive tarihler bekleniyor")
        idx = pd.DatetimeIndex(items)
    if idx.tz is not None:
        raise ValueError("tz-naive tarihler bekleniyor")
    if idx.isna().any():
        raise ValueError("gecersiz (NaT) tarih var")
    return pd.DatetimeIndex(idx.normalize().unique().sort_values(), name="date")


def _as_int(n, name: str) -> int:
    try:
        return operator.index(n)
    except TypeError as exc:
        raise ValueError(f"{name} tam sayi olmali: {n!r}") from exc


class TradingCalendar:
    """XU100 bar gunlerinden turetilen islem gunu takvimi."""

    def __init__(self, days: Iterable) -> None:
        idx = _to_index(days)
        if len(idx) == 0:
            raise ValueError("takvim bos olamaz")
        self._days = idx

    # --- temel ozellikler ---
    @property
    def days(self) -> pd.DatetimeIndex:
        return self._days

    @property
    def first_day(self) -> pd.Timestamp:
        return self._days[0]

    @property
    def last_day(self) -> pd.Timestamp:
        return self._days[-1]

    def __len__(self) -> int:
        return len(self._days)

    def __contains__(self, d) -> bool:
        return self.is_trading_day(d)

    def covers(self, d) -> bool:
        """d, takvimin bildigi aralikta [ilk gun, son gun] mi?"""
        return self.first_day <= _to_day(d) <= self.last_day

    # --- sorgular ---
    def is_trading_day(self, d) -> bool:
        return _to_day(d) in self._days

    def next_trading_day(self, d) -> pd.Timestamp | None:
        i = self._days.searchsorted(_to_day(d), side="right")
        return self._days[i] if i < len(self._days) else None

    def prev_trading_day(self, d) -> pd.Timestamp | None:
        i = self._days.searchsorted(_to_day(d), side="left") - 1
        return self._days[i] if i >= 0 else None

    def add_trading_days(self, d, n: int) -> pd.Timestamp | None:
        n = _as_int(n, "n")
        if n < 0:
            raise ValueError(f"n >= 0 olmali: {n}")
        day = _to_day(d)
        if n == 0:
            if day not in self._days:
                raise ValueError(f"n=0 icin tarih islem gunu olmali: {day.date()}")
            return day
        i = self._days.searchsorted(day, side="right") + n - 1
        return self._days[i] if i < len(self._days) else None

    def trading_days_between(self, a, b) -> int | None:
        da, db = _to_day(a), _to_day(b)
        if not (self.covers(da) and self.covers(db)):
            return None
        ia = self._days.searchsorted(da, side="right")
        ib = self._days.searchsorted(db, side="right")
        return int(ib - ia)

    def horizon_end(self, made_at, horizon_days: int) -> pd.Timestamp | None:
        h = _as_int(horizon_days, "horizon_days")
        if h < 1:
            raise ValueError(f"horizon_days >= 1 olmali: {h}")
        day = _to_day(made_at)
        if day > self.last_day:
            return None
        if day not in self._days:
            raise ValueError(f"made_at islem gunu degil: {day.date()}")
        return self.add_trading_days(day, h)


# --- resmi tatil capraz kontrolu ---

def _tr_holidays(years: Iterable[int]) -> dict[dt.date, str]:
    return dict(holidays.country_holidays("TR", years=sorted(set(years)), language="en_US"))


def _years_of(days: pd.DatetimeIndex, years: Iterable[int] | None) -> list[int]:
    if years is None:
        return sorted(set(int(y) for y in days.year))
    return sorted(set(int(y) for y in years))


def cross_check(days: Iterable, years: Iterable[int] | None = None) -> tuple[list[pd.Timestamp], list[pd.Timestamp]]:
    """XU100 gunlerini `holidays` paketinin Turkiye takvimiyle karsilastirir. Hata firlatmaz, supheli listeler dondurur.

    Donus: (veride_olan_ama_tatil_diyen, veride_olmayan_hafta_ici_ve_tatil_olmayan), ikisi de sirali.
    - 1. liste: veride bar olan ama resmi tatil (ya da hafta sonu) olan gunler.
    - 2. liste: [ilk bar, son bar] araliginda, `years` icinde, hafta ici, resmi tatil olmayan ama barsiz gunler.
      (Idari izin kopruleri ve yarim gun degil tam kapali gunler burada gorunur.)
    `years` verilmezse verideki yillar kullanilir. Arife yarim gunleri bu listelerde gorunmez
    (paket yarim gunleri tatil saymaz); bunlar icin `half_day_candidates`.
    """
    idx = _to_index(days)
    if len(idx) == 0:
        return [], []
    yrs = set(_years_of(idx, years))
    hol = _tr_holidays(yrs)
    present = set(idx)
    in_data_but_holiday = [
        d for d in idx if d.year in yrs and (d.weekday() >= 5 or d.date() in hol)
    ]
    full = pd.date_range(idx[0], idx[-1], freq="D")
    absent_suspect = [
        d for d in full
        if d.year in yrs and d.weekday() < 5 and d.date() not in hol and d not in present
    ]
    return in_data_but_holiday, absent_suspect


def half_day_candidates(days: Iterable, years: Iterable[int] | None = None) -> list[pd.Timestamp]:
    """Arife (yarim gun) adaylari: bayram blogunun (Ramazan/Kurban) ya da Cumhuriyet Bayrami'nin
    hemen onceki takvim gunu, o gun verideki bir hafta ici gunuyse. Paket yarim gunleri bilmez; bu yalnizca
    bir tahmindir (borsa takvimiyle dogrulanmadi), is_half_day alanini DOLDURMAK icin kullanilmamali."""
    idx = _to_index(days)
    if len(idx) == 0:
        return []
    yrs = _years_of(idx, years)
    # yil sinirindaki bayramlar icin komsu yillari da yukle
    hol = _tr_holidays(set(yrs) | {y + 1 for y in yrs})
    present = set(idx)
    out = []
    for d in sorted(hol):
        name = hol[d]
        if not (name.startswith("Eid") or name == "Republic Day"):
            continue
        prev = d - dt.timedelta(days=1)
        if hol.get(prev, "").startswith("Eid") and name.startswith("Eid"):
            continue  # blok baslangici degil
        ts = pd.Timestamp(prev)
        if ts.year in yrs and ts.weekday() < 5 and prev not in hol and ts in present:
            out.append(ts)
    return out
