"""Tarihe gore blok bootstrap (PROJE.md Bolum 11).

Ayni gunun tum hisseleri birlikte kalir; gunler `block_days` uzunlugunda ardisik (dairesel)
bloklar halinde yeniden orneklenir. Boylece hem gun ici korelasyon (hisseler arasi) hem de
gunler arasi otokorelasyon (cakisan 5 gunluk pencereler) guven araligina yansir.

Yontem ayrintilari:
- Benzersiz tarihler siralanir (0..n_dates-1). Her yeniden ornekte ceil(n_dates / block_days)
  blok baslangici duzgun rastgele secilir; blok [s, s+block_days) araligidir ve sondan basa
  sarar (dairesel). Birlestirilen gun listesi tam n_dates uzunluguna kesilir.
- Secilen her gunun TUM satirlari alinir; gun tekrarlanirsa satirlari da tekrarlanir.
- `block_days > n_dates` ise block_days n_dates'e indirilir (details["block_days_used"]).
- Guven araligi yuzdelik (percentile) yontemidir: replikatlarin alfa/2 ve 1-alfa/2 kantilleri.
- stat_fn'e verilen DataFrame'e, her secilen gun orneklemine ozgu `_boot_day` sutunu eklenir.
  Tekrarlanan bir gun farkli `_boot_day` degeri alir; gunluk gruplayan istatistikler (ornegin
  gunluk IC) `_boot_day` ile gruplamalidir, yoksa tekrarlanan gunler birlesir.
- Deterministik: tum rastgelelik `np.random.default_rng(seed)` uzerinden gelir.
"""

from __future__ import annotations

import math
from typing import Callable

import numpy as np
import pandas as pd

BOOT_DAY_COL = "_boot_day"


def bonferroni_alpha(alpha: float, n_trials: int) -> float:
    """Bonferroni duzeltmeli alfa: alpha / n_trials (PROJE.md terfi.multiple_testing)."""
    if not (0.0 < alpha < 1.0):
        raise ValueError("alpha (0, 1) araliginda olmali")
    if int(n_trials) != n_trials or n_trials < 1:
        raise ValueError("n_trials pozitif tam sayi olmali")
    return alpha / int(n_trials)


def _check_common(resamples: int, ci: float) -> None:
    if int(resamples) != resamples or resamples < 1:
        raise ValueError("resamples pozitif tam sayi olmali")
    if not (0.0 < ci < 1.0):
        raise ValueError("ci (0, 1) araliginda olmali")


def _factorize_dates(df: pd.DataFrame, date_col: str) -> tuple[np.ndarray, int]:
    if date_col not in df.columns:
        raise ValueError(f"sutun yok: {date_col}")
    if len(df) == 0:
        raise ValueError("bos veri")
    codes, uniques = pd.factorize(df[date_col], sort=True)
    if (codes < 0).any():
        raise ValueError(f"{date_col} sutununda eksik tarih var")
    return codes.astype(np.int64), int(len(uniques))


def _select_days(n_dates: int, block_days: int, rng: np.random.Generator) -> np.ndarray:
    """Dairesel blok bootstrap ile n_dates uzunlugunda gun indeksi dizisi."""
    n_blocks = math.ceil(n_dates / block_days)
    starts = rng.integers(0, n_dates, size=n_blocks)
    sel = (starts[:, None] + np.arange(block_days)[None, :]) % n_dates
    return sel.ravel()[:n_dates]


def _row_indices(sel: np.ndarray, order: np.ndarray, offsets: np.ndarray) -> np.ndarray:
    """Secilen gunlerin tum satir konumlari (gun tekrari satir tekrari demektir)."""
    lens = offsets[sel + 1] - offsets[sel]
    total = int(lens.sum())
    shift = np.repeat(offsets[sel] - (np.cumsum(lens) - lens), lens)
    return order[shift + np.arange(total)]


def _percentile_ci(reps: np.ndarray, ci: float) -> tuple[float, float]:
    reps = reps[np.isfinite(reps)]
    if reps.size == 0:
        return float("nan"), float("nan")
    alpha = 1.0 - ci
    lo, hi = np.quantile(reps, [alpha / 2.0, 1.0 - alpha / 2.0])
    return float(lo), float(hi)


def _details(reps: np.ndarray, **extra) -> dict:
    ok = reps[np.isfinite(reps)]
    return {
        "n_valid": int(ok.size),
        "n_nan": int(reps.size - ok.size),
        "se": float(ok.std(ddof=1)) if ok.size > 1 else float("nan"),
        "replicates": reps,
        **extra,
    }


def block_bootstrap_ci(
    df: pd.DataFrame,
    stat_fn: Callable[[pd.DataFrame], float],
    *,
    date_col: str,
    block_days: int = 10,
    resamples: int = 2000,
    ci: float = 0.95,
    seed: int = 0,
) -> tuple[float, float, float, dict]:
    """Tarihe gore dairesel blok bootstrap guven araligi.

    Dondurulen: (nokta_tahmin, alt, ust, ayrintilar). Nokta tahmin stat_fn(df) (orijinal veri).
    NaN dondeken replikatlar atilir (details["n_nan"]).
    """
    _check_common(resamples, ci)
    if int(block_days) != block_days or block_days < 1:
        raise ValueError("block_days pozitif tam sayi olmali")
    if BOOT_DAY_COL in df.columns:
        raise ValueError(f"{BOOT_DAY_COL} sutunu veride zaten var")
    codes, n_dates = _factorize_dates(df, date_col)
    block_used = min(int(block_days), n_dates)

    order = np.argsort(codes, kind="stable")
    offsets = np.concatenate([[0], np.cumsum(np.bincount(codes, minlength=n_dates))])

    point = float(stat_fn(df))
    rng = np.random.default_rng(seed)
    reps = np.empty(int(resamples), dtype=float)
    base = df.reset_index(drop=True)
    lens_all = np.diff(offsets)
    for i in range(int(resamples)):
        sel = _select_days(n_dates, block_used, rng)
        rows = _row_indices(sel, order, offsets)
        sample = base.iloc[rows].reset_index(drop=True)
        sample[BOOT_DAY_COL] = np.repeat(np.arange(n_dates), lens_all[sel])
        reps[i] = float(stat_fn(sample))
    lo, hi = _percentile_ci(reps, ci)
    return point, lo, hi, _details(
        reps, method="circular_block_by_date", n_rows=int(len(df)), n_dates=n_dates,
        block_days=int(block_days), block_days_used=block_used, resamples=int(resamples),
        ci=float(ci), seed=int(seed),
    )


def iid_bootstrap_ci(
    df: pd.DataFrame,
    stat_fn: Callable[[pd.DataFrame], float],
    *,
    resamples: int = 2000,
    ci: float = 0.95,
    seed: int = 0,
) -> tuple[float, float, float, dict]:
    """Satirlari bagimsiz (iid) ornekleyen bootstrap. Yalnizca karsilastirma/test icin:
    gun ici korelasyon varsa guven araligi fazla dar cikar."""
    _check_common(resamples, ci)
    n = len(df)
    if n == 0:
        raise ValueError("bos veri")
    point = float(stat_fn(df))
    rng = np.random.default_rng(seed)
    base = df.reset_index(drop=True)
    reps = np.empty(int(resamples), dtype=float)
    for i in range(int(resamples)):
        rows = rng.integers(0, n, size=n)
        reps[i] = float(stat_fn(base.iloc[rows].reset_index(drop=True)))
    lo, hi = _percentile_ci(reps, ci)
    return point, lo, hi, _details(
        reps, method="iid_rows", n_rows=n, resamples=int(resamples), ci=float(ci), seed=int(seed),
    )


def paired_diff_ci(
    df: pd.DataFrame,
    loss_a_col: str,
    loss_b_col: str,
    *,
    date_col: str,
    block_days: int = 10,
    resamples: int = 2000,
    ci: float = 0.95,
    seed: int = 0,
) -> tuple[float, float, float, dict]:
    """Eslestirilmis fark (a - b) ortalamasi icin blok bootstrap guven araligi.

    Ayni satirlar (ayni gun, ayni hisse) uzerinde kayip farki alinir; negatif fark a'nin
    daha iyi (daha dusuk kayip) oldugunu gosterir. details["excludes_zero"] araligin sifiri
    dislayip dislamadigini verir. Bonferroni icin ci = 1 - bonferroni_alpha(...) gecilir;
    uc kantiller icin resamples yeterince buyuk olmali (details["tail_draws"] bir uc
    kuyruktaki beklenen replikat sayisidir).
    Sonuc, ayni tohumla block_bootstrap_ci(stat=fark ortalamasi) ile ayni diziyi kullanir.
    """
    _check_common(resamples, ci)
    if int(block_days) != block_days or block_days < 1:
        raise ValueError("block_days pozitif tam sayi olmali")
    for col in (loss_a_col, loss_b_col):
        if col not in df.columns:
            raise ValueError(f"sutun yok: {col}")
    codes, n_dates = _factorize_dates(df, date_col)
    a = df[loss_a_col].to_numpy(dtype=float)
    b = df[loss_b_col].to_numpy(dtype=float)
    if not (np.all(np.isfinite(a)) and np.all(np.isfinite(b))):
        raise ValueError("kayip sutunlari NaN/sonsuz iceremez")
    diff = a - b
    block_used = min(int(block_days), n_dates)

    day_sum = np.bincount(codes, weights=diff, minlength=n_dates)
    day_n = np.bincount(codes, minlength=n_dates).astype(float)
    rng = np.random.default_rng(seed)
    reps = np.empty(int(resamples), dtype=float)
    for i in range(int(resamples)):
        sel = _select_days(n_dates, block_used, rng)
        reps[i] = day_sum[sel].sum() / day_n[sel].sum()
    point = float(diff.mean())
    lo, hi = _percentile_ci(reps, ci)
    excludes = bool(math.isfinite(lo) and math.isfinite(hi) and (lo > 0.0 or hi < 0.0))
    return point, lo, hi, _details(
        reps, method="circular_block_by_date_paired", n_rows=int(len(df)), n_dates=n_dates,
        block_days=int(block_days), block_days_used=block_used, resamples=int(resamples),
        ci=float(ci), seed=int(seed), excludes_zero=excludes,
        tail_draws=float(resamples) * (1.0 - ci) / 2.0,
    )
