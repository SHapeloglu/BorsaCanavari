"""Kohort portfoy simulasyonu (PROJE.md Bolum 11).

Saf fonksiyonlar: veritabanina bagli degildir, yalnizca numpy ve pandas. Girdi `df` tek bir
modelin COZULMUS tahminleridir; kolonlar: made_at (tarih), ticker, score (buyuk = daha cok al),
tradable_ret (made_at+1 acilis -> made_at+H kapanis getirisi; resolver.py).

Kurallar
- Yalnizca uzun pozisyon, esit agirlik. Her made_at gunu bir kohorttur: o gunun evreninde score'a
  gore en ust ceil(top_frac * n) hisse alinir. Esik skoruna ESIT olan hisselerin HEPSI dahil
  edilir; keyfi sira kirma yoktur (iki degerli baseline skorlari ve 'hep yukselis' icin gerekli:
  tum skorlar esitse hepsi secilir ve kohort getirisi benchmark'a esit olur).
- NaN score veya tradable_ret iceren satirlar o gunden dusurulur ve sayilir
  (summary['dropped_nan_rows']). Benchmark da ayni temizlenmis evrenden hesaplanir; boylece
  secim ve benchmark ayni hisse kumesine dayanir. Temizlendikten sonra evreni `min_universe`
  altinda kalan (ya da hic satiri kalmayan) gunler atlanir ve sayilir (summary['skipped_days']).
- Maliyet: `cost_bps` BIR YONLU islem maliyetidir. Kohort girer ve cikar, bu yuzden
  cost = 2 * cost_bps / 1e4 (pozisyon degerinin oraninda, esit agirlikli oldugu icin kohort
  getirisinden dogrudan dusulur). Benchmark getirisi MALIYETSIZDIR (pasif evren, islem yok) ve
  `benchmark_ret` olarak ayri tutulur; excess_net = net_ret - benchmark_ret bu yuzden
  MUHAFAZAKARDIR: model tum evreni secse bile excess_net = -cost olur.
- Her gunluk kohort sermayenin 1/H'sidir (H = horizon_days). Kohortlar cakisir; getiriler
  `equity_curve` icinde 1/H ile olceklenerek toplanir, boylece cift sayim olmaz.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

REQUIRED_COLS = ("made_at", "ticker", "score", "tradable_ret")
COHORT_COLS = (
    "n_universe", "n_selected", "gross_ret", "benchmark_ret",
    "cost", "net_ret", "excess_gross", "excess_net",
)
_BASE_COLS = ("n_universe", "n_selected", "gross_ret", "benchmark_ret")
_HIT_EPS = 1e-12  # 'benchmark'i gecti' sayilmak icin gereken asgari fark (kayan nokta gurultusu)


@dataclass
class PortfolioResult:
    cohorts: pd.DataFrame  # index made_at; kolonlar COHORT_COLS
    summary: dict


# ---------------------------------------------------------------- dogrulama

def round_trip_cost(cost_bps: float) -> float:
    """Bir yonlu `cost_bps` icin gidis-donus maliyet orani: 2 * cost_bps / 1e4."""
    return 2.0 * validate_cost_bps(cost_bps) / 1e4


def validate_cost_bps(cost_bps) -> float:
    try:
        v = float(cost_bps)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"cost_bps sayisal olmali: {cost_bps!r}") from exc
    if not math.isfinite(v) or v < 0.0:
        raise ValueError(f"cost_bps sonlu ve >= 0 olmali: {cost_bps!r}")
    return v


def _validate_top_frac(top_frac) -> float:
    try:
        v = float(top_frac)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"top_frac sayisal olmali: {top_frac!r}") from exc
    if not (0.0 < v <= 1.0):
        raise ValueError(f"top_frac (0, 1] araliginda olmali: {top_frac!r}")
    return v


def _validate_min_universe(min_universe) -> int:
    if int(min_universe) != min_universe or min_universe < 1:
        raise ValueError("min_universe pozitif tam sayi olmali")
    return int(min_universe)


def _validate_horizon(horizon_days) -> int:
    if int(horizon_days) != horizon_days or horizon_days < 1:
        raise ValueError("horizon_days pozitif tam sayi olmali")
    return int(horizon_days)


def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Girdiyi dogrular; yalniz gerekli kolonlarin KOPYASINI (made_at normalize) dondurur."""
    if not isinstance(df, pd.DataFrame):
        raise ValueError("df bir pandas DataFrame olmali")
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"eksik sutun(lar): {missing}")
    work = df.loc[:, list(REQUIRED_COLS)].copy()
    try:
        work["made_at"] = pd.to_datetime(work["made_at"])
        work["score"] = pd.to_numeric(work["score"]).astype(float)
        work["tradable_ret"] = pd.to_numeric(work["tradable_ret"]).astype(float)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"sutun tipleri gecersiz: {exc}") from exc
    if work["made_at"].isna().any():
        raise ValueError("made_at eksik (NaT) olamaz")
    work["made_at"] = work["made_at"].dt.normalize().astype("datetime64[ns]")  # birim girdiden bagimsiz
    for col in ("score", "tradable_ret"):
        if np.isinf(work[col].to_numpy()).any():
            raise ValueError(f"{col} sonsuz deger iceremez (NaN serbest, satir dusurulur)")
    if work.duplicated(["made_at", "ticker"]).any():
        raise ValueError("ayni (made_at, ticker) birden fazla satir: tek model icin cagrilmali")
    return work


# ---------------------------------------------------------------- secim

def _top_mask(scores: np.ndarray, top_frac: float) -> np.ndarray:
    """Esik skoru k-inci en buyuk degerdir; esikten buyuk-esit olanlarin hepsi secilir."""
    n = scores.shape[0]
    if n == 0:
        return np.zeros(0, dtype=bool)
    # round: 0.1 * 30 = 3.0000000000000004 gibi kayan nokta artiklari ceil'i sasirtmasin
    k = math.ceil(round(top_frac * n, 9))
    k = min(max(k, 1), n)
    threshold = np.partition(scores, n - k)[n - k]
    return scores >= threshold


def select_top(day_df: pd.DataFrame, top_frac: float) -> pd.Index:
    """Bir gunun satirlarindan secilenlerin index etiketlerini dondurur.

    k = ceil(top_frac * n) (n = score'u NaN olmayan satir sayisi); esik skoruna esit olanlarin
    HEPSI dahil edilir, bu yuzden secilen sayisi k'den buyuk olabilir. NaN score satirlari secilmez.
    """
    top_frac = _validate_top_frac(top_frac)
    if "score" not in day_df.columns:
        raise ValueError("sutun yok: score")
    scores = pd.to_numeric(day_df["score"]).astype(float)
    valid = scores.notna()
    sub = scores[valid]
    mask = _top_mask(sub.to_numpy(), top_frac)
    return sub.index[mask]


# ---------------------------------------------------------------- simulasyon

def _empty_cohorts(cols) -> pd.DataFrame:
    out = pd.DataFrame({c: pd.Series(dtype=float) for c in cols})
    out.index = pd.DatetimeIndex([], name="made_at")
    return out


def _base_cohorts(work: pd.DataFrame, top_frac: float, min_universe: int):
    """Maliyetsiz kohort tablosu ve sayaclar. `work` _prepare ciktisidir."""
    valid = work.dropna(subset=["score", "tradable_ret"])
    dropped = int(len(work) - len(valid))
    total_days = int(work["made_at"].nunique())

    dates, n_univ, n_sel, gross, bench = [], [], [], [], []
    for date, g in valid.groupby("made_at", sort=True):
        n = len(g)
        if n < min_universe:
            continue
        scores = g["score"].to_numpy()
        rets = g["tradable_ret"].to_numpy()
        mask = _top_mask(scores, top_frac)
        dates.append(date)
        n_univ.append(n)
        n_sel.append(int(mask.sum()))
        gross.append(float(rets[mask].mean()))
        bench.append(float(rets.mean()))

    if dates:
        base = pd.DataFrame(
            {"n_universe": n_univ, "n_selected": n_sel, "gross_ret": gross, "benchmark_ret": bench},
            index=pd.DatetimeIndex(dates, name="made_at"),
        )
    else:
        base = _empty_cohorts(_BASE_COLS)
    counters = {
        "n_input_rows": int(len(work)),
        "dropped_nan_rows": dropped,
        "n_days_input": total_days,
        "skipped_days": total_days - int(len(base)),
    }
    return base, counters


def _apply_cost(base: pd.DataFrame, cost_bps: float) -> pd.DataFrame:
    out = base.copy()
    out["cost"] = round_trip_cost(cost_bps)
    out["net_ret"] = out["gross_ret"] - out["cost"]
    out["excess_gross"] = out["gross_ret"] - out["benchmark_ret"]
    out["excess_net"] = out["net_ret"] - out["benchmark_ret"]
    return out.loc[:, list(COHORT_COLS)]


def _summarize(cohorts: pd.DataFrame, cost_bps: float, counters: dict) -> dict:
    n = int(len(cohorts))
    nan = float("nan")

    def _mean(col: str) -> float:
        return float(cohorts[col].mean()) if n else nan

    hit = float((cohorts["excess_net"] > _HIT_EPS).mean()) if n else nan
    sel_frac = float((cohorts["n_selected"] / cohorts["n_universe"]).mean()) if n else nan
    return {
        "n_cohorts": n,
        "mean_gross": _mean("gross_ret"),
        "mean_net": _mean("net_ret"),
        "mean_benchmark": _mean("benchmark_ret"),
        "mean_excess_gross": _mean("excess_gross"),
        "mean_excess_net": _mean("excess_net"),
        "hit_rate_vs_benchmark": hit,  # excess_net > 0 olan kohort orani (esik 1e-12)
        "selected_frac_mean": sel_frac,
        "cost_bps": float(cost_bps),
        **counters,
    }


def simulate_cohort_portfolio(
    df: pd.DataFrame,
    *,
    top_frac: float = 0.2,
    cost_bps: float = 0.0,
    min_universe: int = 5,
) -> PortfolioResult:
    """Gunluk kohort portfoyu: her gun en ust `top_frac` dilim (esitlikler dahil), esit agirlik.

    Kohort getirileri tek tek (kohort basina) dondurulur; gunluk kohortlarin 1/H sermaye payi ve
    cakisma `equity_curve` icinde ele alinir. Ayrintilar icin modul docstring'ine bakin.
    summary ek anahtarlari: mean_excess_gross, n_input_rows, dropped_nan_rows, n_days_input,
    skipped_days.
    """
    top_frac = _validate_top_frac(top_frac)
    cost_bps = validate_cost_bps(cost_bps)
    min_universe = _validate_min_universe(min_universe)
    work = _prepare(df)
    base, counters = _base_cohorts(work, top_frac, min_universe)
    cohorts = _apply_cost(base, cost_bps)
    return PortfolioResult(cohorts=cohorts, summary=_summarize(cohorts, cost_bps, counters))


# ---------------------------------------------------------------- ozkaynak egrisi

_EQUITY_KINDS = {"net": "net_ret", "gross": "gross_ret", "benchmark": "benchmark_ret"}


def equity_curve(result: PortfolioResult, horizon_days: int, *, kind: str = "net") -> pd.Series:
    """Basit, toplamsal ozkaynak egrisi: equity_t = 1 + sum_{k<=t} (ret_k / H).

    `kind`: 'net' (net_ret, varsayilan), 'gross' (gross_ret) ya da 'benchmark' (benchmark_ret,
    maliyetsiz). Kohortlar made_at sirasina dizilir; her kohort sermayenin 1/H'si oldugu icin
    getirisi 1/H ile olceklenir ve tek seferde eklenir (cakisan kohortlar cift sayilmaz).

    SINIRLAR (dürüstce):
    - Gunluk piyasaya gore degerleme (MTM) YOKTUR. Bir kohortun tum H gunluk getirisi, kohortun
      made_at siradaki adimina yazilir; gercekte bu getiri made_at+H'de gerceklesir ve
      aradaki gunlerde dalgalanir. Egri bu yuzden gercek pnl'den ~H gun ONDE ve dalgasizdir:
      gun ici/ufuk ici dususler gorunmez, max_drawdown ALT SINIR kabul edilmelidir.
    - Toplamsaldir (bilesik degil): 1/H'lik dilimler bagimsiz sabit sermaye payi varsayilir.
    - Egri yalnizca cohorts'taki gunleri icerir; atlanan gunler (kucuk evren, NaN) sifir
      getirili adim bile sayilmaz. Takvim bosluklari gozardi edilir (sira esaslidir).
    - Egri baslangic noktasi (1.0) icermez; ilk eleman 1 + ret_0/H'dir. Drawdown icin
      `max_drawdown(..., initial=1.0)` ilk kohortun kaybini da sayar.
    - Maliyet kohort girisi/cikisi icin tek seferde dusulur (round_trip_cost).
    """
    if not isinstance(result, PortfolioResult):
        raise ValueError("result bir PortfolioResult olmali")
    h = _validate_horizon(horizon_days)
    if kind not in _EQUITY_KINDS:
        raise ValueError(f"kind {sorted(_EQUITY_KINDS)} icinden olmali: {kind!r}")
    col = _EQUITY_KINDS[kind]
    coh = result.cohorts
    if col not in coh.columns:
        raise ValueError(f"cohorts icinde sutun yok: {col}")
    rets = coh[col].sort_index().astype(float)
    eq = 1.0 + (rets / h).cumsum()
    eq.name = f"equity_{kind}"
    return eq


def _as_series(x, name: str) -> pd.Series:
    s = x if isinstance(x, pd.Series) else pd.Series(np.asarray(x, dtype=float))
    s = s.astype(float)
    if s.isna().any() or not np.isfinite(s.to_numpy()).all():
        raise ValueError(f"{name} NaN/sonsuz iceremez")
    if (s <= 0.0).any():
        raise ValueError(f"{name} pozitif olmali")
    return s


def _max_dd(values: np.ndarray, initial: float | None) -> float:
    if initial is not None:
        values = np.concatenate([[float(initial)], values])
    peak = np.maximum.accumulate(values)
    return float(np.max(1.0 - values / peak))


def max_drawdown(equity, *, initial: float | None = 1.0) -> float:
    """Zirveden en buyuk dusus orani (pozitif, orn. 0.25 = %25).

    `initial`: egrinin oncesindeki baslangic degeri (varsayilan 1.0: bu modulun egrileri 1.0
    sermayeyle baslar, boylece ilk kohortun kaybi da sayilir). Baslangici saymamak ya da 1.0
    tabanli olmayan keyfi bir seri icin initial=None verin. Bos seri icin NaN.
    """
    s = _as_series(equity, "equity")
    if len(s) == 0:
        return float("nan")
    return _max_dd(s.to_numpy(), initial)


def relative_drawdown(model_equity, benchmark_equity, *, initial: float | None = 1.0) -> float:
    """model_equity / benchmark_equity oraninin zirveden en buyuk dususu (devre kesici girdisi).

    PROJE.md Bolum 12: simule stratejinin esit agirlikli BIST 30'a gore goreli dususu. Mutlak
    dusus kullanilmaz (piyasa dusarken model bozulmamis olabilir). Iki seri ayni index'e (ya da
    ayni uzunluga) sahip olmali. `initial` oran icin baslangic degeridir (iki egri de 1.0'dan
    basladigi icin 1.0). Bos seri icin NaN. equity_curve sinirlari (MTM yok) burada da gecerlidir.
    """
    m = _as_series(model_equity, "model_equity")
    b = _as_series(benchmark_equity, "benchmark_equity")
    if len(m) != len(b):
        raise ValueError(f"uzunluk uyumsuz: {len(m)} != {len(b)}")
    if isinstance(model_equity, pd.Series) and isinstance(benchmark_equity, pd.Series):
        if not m.index.equals(b.index):
            raise ValueError("model ve benchmark egrilerinin index'i ayni olmali")
    if len(m) == 0:
        return float("nan")
    ratio = m.to_numpy() / b.to_numpy()
    return _max_dd(ratio, initial)


def __getattr__(name: str):
    # cost_sensitivity costs.py'de yasar (costs, portfolio'yu ice aktarir); dongusel ice aktarimi
    # onlemek icin burada tembel olarak sunulur: from ...portfolio import cost_sensitivity calisir.
    if name == "cost_sensitivity":
        from .costs import cost_sensitivity

        return cost_sensitivity
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
