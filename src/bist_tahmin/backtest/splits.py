"""Walk-forward bolmeleri (PROJE.md Bolum 5 purge kurali, Bolum 8 training.*). SAF: DB yok.

Kural:
- Genisleyen pencere: egitim her zaman days[0]'dan baslar.
- Ilk `min_train_days` gun yalnizca egitimdedir; ilk test gunu days[min_train_days]'dir.
- Test bloklari `retrain_every_days` islem gunu uzunlugunda, ardisik, orgusuz ve bosluksuzdur;
  son blok kisa olabilir.
- Her blok icin train_end = blogun ilk test gunuden `purge_days` islem gunu ONCEKI gundur.
  Egitim satiri d icin etiket bitisi d + horizon islem gunudur; d <= train_end ise etiket
  penceresi test gunune en fazla degerek biter, onu kesmez.
- start/end yalnizca test gunlerini kisitlar (dahil); blok sinirlari start'a bagli degildir
  (ayni takvimde start/end verilse de verilmese de fold'un train_end degeri ayni kalir).
  Elenen fold'lar cikarildiktan sonra fold_id 0'dan ardisik yeniden numaralanir.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Fold:
    fold_id: int
    train_end: pd.Timestamp  # egitimde kullanilabilecek son gun (dahil)
    test_dates: pd.DatetimeIndex  # ardisik islem gunleri


def _check_int(name: str, value: object, minimum: int) -> int:
    if isinstance(value, bool) or int(value) != value or value < minimum:  # type: ignore[call-overload]
        raise ValueError(f"{name} >= {minimum} olan tam sayi olmali: {value!r}")
    return int(value)  # type: ignore[call-overload]


def make_folds(
    days: pd.DatetimeIndex,
    *,
    horizon_days: int,
    purge_days: int,
    min_train_days: int,
    retrain_every_days: int,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
) -> list[Fold]:
    """Genisleyen pencereli, purge'lu walk-forward fold listesi uretir (zamana gore artan)."""
    horizon = _check_int("horizon_days", horizon_days, 1)
    purge = _check_int("purge_days", purge_days, 0)
    min_train = _check_int("min_train_days", min_train_days, 1)
    every = _check_int("retrain_every_days", retrain_every_days, 1)
    if purge < horizon:
        raise ValueError(f"purge_days ({purge}) horizon_days ({horizon}) degerinden kucuk olamaz (sizinti)")
    if min_train < purge:
        raise ValueError(f"min_train_days ({min_train}) purge_days ({purge}) degerinden kucuk olamaz")

    if not isinstance(days, pd.DatetimeIndex):
        raise ValueError("days bir DatetimeIndex olmali")
    if days.hasnans:
        raise ValueError("days eksik (NaT) tarih iceremez")
    if not days.is_monotonic_increasing or not days.is_unique:
        raise ValueError("days sirali ve tekrarsiz olmali")

    start_ts = pd.Timestamp(start) if start is not None else None
    end_ts = pd.Timestamp(end) if end is not None else None
    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        raise ValueError(f"start ({start_ts.date()}) end ({end_ts.date()}) degerinden sonra olamaz")

    folds: list[Fold] = []
    for first in range(min_train, len(days), every):
        block = days[first : first + every]
        if start_ts is not None:
            block = block[block >= start_ts]
        if end_ts is not None:
            block = block[block <= end_ts]
        if len(block) == 0:
            continue
        folds.append(Fold(fold_id=len(folds), train_end=days[first - purge], test_dates=block))
    return folds


def assert_no_label_overlap(train_end_dates: pd.Series, first_test_date: pd.Timestamp) -> None:
    """Egitim satirlarinin etiket bitis gunu (end_date) ilk test gununu asarsa AssertionError.

    Etiket bitisi bilinmeyen (NaT) satir da hata sayilir: etiketi henuz kapanmamis satir egitimde olamaz.
    """
    ends = pd.to_datetime(pd.Series(train_end_dates))
    first = pd.Timestamp(first_test_date)
    unknown = int(ends.isna().sum())
    if unknown:
        raise AssertionError(f"{unknown} egitim satirinin etiket bitis gunu bilinmiyor (NaT)")
    late = ends[ends > first]
    if len(late):
        raise AssertionError(
            f"{len(late)} egitim satirinin etiketi ilk test gununu ({first.date()}) kesiyor "
            f"(en gec etiket bitisi {late.max().date()})"
        )
