"""Olasilik ve siralama metrikleri (PROJE.md Bolum 11).

Saf istatistik: veritabanina veya veri kaynagina bagli degildir. Yalnizca numpy ve pandas.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

DEFAULT_LOG_LOSS_CLIP = (0.01, 0.99)  # config prob_clip ile ayni


def _validate_prob_label(prob, label) -> tuple[np.ndarray, np.ndarray]:
    """Olasilik ve etiket dizilerini dogrular; float64 dizileri dondurur."""
    try:
        p = np.asarray(prob, dtype=float)
        y = np.asarray(label, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"olasilik/etiket sayisal olmali: {exc}") from exc
    if p.ndim != 1 or y.ndim != 1:
        raise ValueError("olasilik ve etiket 1 boyutlu olmali")
    if p.shape[0] != y.shape[0]:
        raise ValueError(f"uzunluk uyumsuz: prob={p.shape[0]}, label={y.shape[0]}")
    if p.shape[0] == 0:
        raise ValueError("bos girdi")
    if not np.all(np.isfinite(p)):
        raise ValueError("olasilik NaN/sonsuz iceremez")
    if p.min() < 0.0 or p.max() > 1.0:
        raise ValueError("olasilik 0..1 disinda")
    if not np.all(np.isin(y, (0.0, 1.0))):
        raise ValueError("etiket yalnizca 0 veya 1 olabilir")
    return p, y


def brier(prob, label) -> float:
    """Brier skoru: mean((p - y)^2). Sabit 0.5 tahmin icin 0.25."""
    p, y = _validate_prob_label(prob, label)
    return float(np.mean((p - y) ** 2))


def log_loss(prob, label, clip: tuple[float, float] = DEFAULT_LOG_LOSS_CLIP) -> float:
    """Log loss; olasiliklar once `clip` araligina kirpilir, sonuc her zaman sonludur."""
    lo, hi = clip
    if not (0.0 < lo < hi < 1.0):
        raise ValueError(f"clip 0 < alt < ust < 1 olmali: {clip}")
    p, y = _validate_prob_label(prob, label)
    p = np.clip(p, lo, hi)
    return float(-np.mean(y * np.log(p) + (1.0 - y) * np.log(1.0 - p)))


def accuracy(prob, label, threshold: float = 0.5) -> float:
    """Isabet orani; `prob >= threshold` ise tahmin 1 sayilir.

    Yalnizca referans: baz oran ~%55 iken esik 0.5 neredeyse hep 'yukari' der (PROJE.md Bolum 11).
    """
    p, y = _validate_prob_label(prob, label)
    return float(np.mean((p >= threshold).astype(float) == y))


def base_rate_brier(labels) -> float:
    """Sabit tahmin (gercek yukselis orani p) referans Brier'i: p * (1 - p)."""
    y = np.asarray(labels, dtype=float)
    if y.ndim != 1 or y.shape[0] == 0:
        raise ValueError("etiketler 1 boyutlu ve bos olmayan olmali")
    if not np.all(np.isin(y, (0.0, 1.0))):
        raise ValueError("etiket yalnizca 0 veya 1 olabilir")
    p = float(y.mean())
    return p * (1.0 - p)


def calibration_table(prob, label, bins: int = 10) -> pd.DataFrame:
    """Kalibrasyon tablosu: esit genislikte `bins` kova.

    Kovalar [alt, ust) araligindadir; son kova 1.0'i icerir (kapali). Bos kovalar n=0 ile
    gorunur, ort. tahmin ve gerceklesen oran NaN olur.
    Sutunlar: bin_lo, bin_hi, n, mean_pred, obs_rate.
    """
    if not isinstance(bins, (int, np.integer)) or bins < 1:
        raise ValueError("bins pozitif tam sayi olmali")
    p, y = _validate_prob_label(prob, label)
    edges = np.linspace(0.0, 1.0, int(bins) + 1)
    idx = np.clip(np.searchsorted(edges, p, side="right") - 1, 0, bins - 1)
    n = np.bincount(idx, minlength=bins)
    sum_p = np.bincount(idx, weights=p, minlength=bins)
    sum_y = np.bincount(idx, weights=y, minlength=bins)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean_pred = np.where(n > 0, sum_p / np.maximum(n, 1), np.nan)
        obs_rate = np.where(n > 0, sum_y / np.maximum(n, 1), np.nan)
    return pd.DataFrame({
        "bin_lo": edges[:-1],
        "bin_hi": edges[1:],
        "n": n.astype(int),
        "mean_pred": mean_pred,
        "obs_rate": obs_rate,
    })


def daily_ic(
    df: pd.DataFrame,
    date_col: str,
    score_col: str,
    ret_col: str,
    min_names: int = 5,
) -> pd.Series:
    """Gun basina hisseler arasi Spearman sira korelasyonu (rank + Pearson, scipy yok).

    Sira baglari ortalama sira ile cozulur. NaN iceren satirlar o gun icin atilir; kalan hisse
    sayisi `min_names` altindaysa ya da skor/getiri sabitse (sira korelasyonu tanimsiz) o gun NaN.
    Dondurulen Series tarihe gore sirali, adi "ic"; tum gunler (NaN olanlar dahil) gorunur.
    """
    if min_names < 2:
        raise ValueError("min_names en az 2 olmali")
    for col in (date_col, score_col, ret_col):
        if col not in df.columns:
            raise ValueError(f"sutun yok: {col}")

    def _one_day(g: pd.DataFrame) -> float:
        g = g[[score_col, ret_col]].dropna()
        if len(g) < min_names:
            return float("nan")
        rs = g[score_col].rank(method="average")
        rr = g[ret_col].rank(method="average")
        if rs.nunique() < 2 or rr.nunique() < 2:
            return float("nan")
        return float(rs.corr(rr))

    out = {date: _one_day(g) for date, g in df.groupby(date_col, sort=True)}
    s = pd.Series(out, dtype=float, name="ic")
    s.index.name = date_col
    return s


def default_nw_lags(horizon_days: int = 5) -> int:
    """Varsayilan Newey-West gecikmesi: ufuk - 1.

    5 gunluk getiri pencereleri ardisik gunlerde 4 gun cakisir; bu yuzden gunluk IC serisinde
    yaklasik MA(ufuk-1) yapida otokorelasyon beklenir. Gecikme 4'ten sonrasi icin teorik
    otokorelasyon sifirdir; daha fazlasi varyans tahminini gereksiz gurultulu yapar.
    """
    return max(int(horizon_days) - 1, 0)


def newey_west_variance_of_mean(x, lags: int) -> float:
    """Ortalamanin Newey-West (Bartlett agirlikli) varyans tahmini.

    var(mean) = (g0 + 2 * sum_{l=1..L} (1 - l/(L+1)) * g_l) / T,
    g_l = (1/T) * sum_t (x_t - m)(x_{t-l} - m). `x` NaN icermemeli.
    """
    a = np.asarray(x, dtype=float)
    t = a.shape[0]
    if t < 2:
        return float("nan")
    lags = min(int(lags), t - 1)
    if lags < 0:
        raise ValueError("lags negatif olamaz")
    d = a - a.mean()
    lrv = float(np.dot(d, d)) / t
    for lag in range(1, lags + 1):
        w = 1.0 - lag / (lags + 1.0)
        lrv += 2.0 * w * float(np.dot(d[lag:], d[:-lag])) / t
    return lrv / t


def ic_summary(ic_series, nw_lags: int | None = None, horizon_days: int = 5) -> dict:
    """IC ozeti: ortalama IC, Newey-West t-istatistigi, gun sayisi.

    `nw_lags` None ise `default_nw_lags(horizon_days)` (= ufuk - 1) kullanilir; en fazla gun
    sayisi - 1 ile sinirlanir. NaN gunler atilir ve kalan seri ardisik kabul edilir (bosluklar
    gecikme hesabinda gozardi edilir; bu hafif bir yaklasikliktir).
    Dondurulen: mean_ic, t_stat_nw, se_nw, t_stat_iid (karsilastirma icin), nw_lags, n_days
    (gecerli gun), n_days_total (NaN dahil).
    """
    s = pd.Series(ic_series, dtype=float)
    total = int(s.shape[0])
    x = s.dropna().to_numpy()
    n = int(x.shape[0])
    lags = default_nw_lags(horizon_days) if nw_lags is None else int(nw_lags)
    if lags < 0:
        raise ValueError("nw_lags negatif olamaz")
    nan = float("nan")
    out = {"mean_ic": nan, "t_stat_nw": nan, "se_nw": nan, "t_stat_iid": nan,
           "nw_lags": min(lags, max(n - 1, 0)), "n_days": n, "n_days_total": total}
    if n == 0:
        return out
    mean = float(x.mean())
    out["mean_ic"] = mean
    if n < 2:
        return out
    var_nw = newey_west_variance_of_mean(x, lags)
    if var_nw > 0.0 and math.isfinite(var_nw):
        out["se_nw"] = math.sqrt(var_nw)
        out["t_stat_nw"] = mean / out["se_nw"]
    sd = float(x.std(ddof=1))
    if sd > 0.0:
        out["t_stat_iid"] = mean / (sd / math.sqrt(n))
    return out


def effective_sample_size(n_names: int, mean_pairwise_corr: float) -> float:
    """Tasarim etkisi: n_eff = n / (1 + (n - 1) * rho).

    Ayni gunun hisseleri arasindaki ortalama korelasyon rho iken n hissenin kac bagimsiz
    gozleme denk oldugunu verir (PROJE.md Bolum 0: rho~0.42, n=30 -> ~2.3).
    """
    n = float(n_names)
    rho = float(mean_pairwise_corr)
    if n < 1:
        raise ValueError("n_names en az 1 olmali")
    denom = 1.0 + (n - 1.0) * rho
    if not math.isfinite(denom) or denom <= 0.0:
        raise ValueError("rho gecersiz: 1 + (n-1)*rho pozitif olmali (rho >= -1/(n-1))")
    if rho > 1.0:
        raise ValueError("rho 1'den buyuk olamaz")
    return n / denom
