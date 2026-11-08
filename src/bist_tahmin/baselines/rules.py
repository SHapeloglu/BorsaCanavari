"""Baseline kurallari (PROJE.md Bolum 10). Saf fonksiyonlar: veritabanina bagli degildir.

Sizinti kurali (Bolum 5): her fonksiyon girdiyi KENDISI `index <= made_at` ile keser; made_at sonrasi
satirlar sonucu degistirmez. Yetersiz/eksik veri -> None (deger uydurulmaz).

Kararlar:
- Veriye dayanan baseline'lar (yesterday, ma20) icin made_at gununun kapanisi serinin SON satiri
  olmak zorundadir; yoksa (bayat veri) None doner. Eski bir barla "bugunku" karar verilmez.
- always_up ve random veriye bakmaz; closes bos olsa da deger uretir.
- Tum sonuclar `clip` araligina kirpilir.
"""

from __future__ import annotations

import hashlib
import math

import numpy as np
import pandas as pd

BASELINES = ("always_up", "yesterday", "random", "ma20", "base_rate")
DEFAULT_CLIP = (0.01, 0.99)
MA_WINDOW = 20


def baseline_model_version(name: str) -> str:
    _check_name(name)
    return f"baseline_{name}_v1"


def _check_name(name: str) -> None:
    if name not in BASELINES:
        raise ValueError(f"bilinmeyen baseline: {name!r}; gecerliler: {BASELINES}")


def _day(d) -> pd.Timestamp:
    ts = pd.Timestamp(d)
    if ts.tzinfo is not None:
        raise ValueError(f"tz-naive tarih bekleniyor, gelen: {d!r}")
    return ts.normalize()


def _check_clip(clip) -> tuple[float, float]:
    lo, hi = clip
    if not (0.0 <= lo < hi <= 1.0):
        raise ValueError(f"clip 0 <= alt < ust <= 1 olmali: {clip}")
    return float(lo), float(hi)


def _clip(p: float, clip) -> float:
    lo, hi = _check_clip(clip)
    return float(min(max(p, lo), hi))


def _usable_closes(closes: pd.Series, made_at: pd.Timestamp) -> pd.Series | None:
    """`index <= made_at` ile keser, gecersiz kapanislari atar. made_at gunu son satir degilse None."""
    s = pd.Series(closes).dropna().sort_index()
    if s.index.tz is not None:
        raise ValueError("closes indeksi tz-naive olmali")
    s = s[(s.index.normalize() <= made_at)]
    s = s[np.isfinite(s.astype(float)) & (s.astype(float) > 0)]
    if len(s) == 0 or s.index[-1].normalize() != made_at:
        return None
    if s.index.has_duplicates:
        raise ValueError("closes indeksinde tekrar eden tarih var")
    return s.astype(float)


def _random_value(seed: int, ticker: str, made_at: pd.Timestamp) -> float:
    """[0.4, 0.6] icinde deterministik deger; global random durumu kullanilmaz."""
    digest = hashlib.sha256(f"{seed}|{ticker}|{made_at.date()}".encode()).digest()
    u = int.from_bytes(digest[:8], "big") / 2**64  # [0, 1)
    return 0.4 + 0.2 * u


def baseline_prob(
    name: str,
    ticker: str,
    closes: pd.Series,
    made_at,
    *,
    seed: int = 0,
    base_rate: float | None = None,
    clip: tuple[float, float] = DEFAULT_CLIP,
) -> float | None:
    _check_name(name)
    _check_clip(clip)
    day = _day(made_at)
    if name == "always_up":
        return _clip(1.0, clip)
    if name == "random":
        return _clip(_random_value(seed, ticker, day), clip)
    if name == "base_rate":
        if base_rate is None or (isinstance(base_rate, float) and math.isnan(base_rate)):
            return None
        if not 0.0 <= base_rate <= 1.0:
            raise ValueError(f"base_rate 0..1 olmali: {base_rate}")
        return _clip(float(base_rate), clip)
    s = _usable_closes(closes, day)
    if name == "yesterday":
        if s is None or len(s) < 2:
            return None
        return _clip(0.6 if s.iloc[-1] / s.iloc[-2] - 1.0 > 0.0 else 0.4, clip)
    # ma20
    if s is None or len(s) < MA_WINDOW:
        return None
    return _clip(0.6 if s.iloc[-1] > s.iloc[-MA_WINDOW:].mean() else 0.4, clip)


def baseline_inputs(
    name: str,
    ticker: str,
    closes: pd.Series,
    made_at,
    *,
    seed: int = 0,
    base_rate: float | None = None,
) -> dict:
    """Kararda gercekten kullanilan girdiler (json'lanabilir; features_json icin).

    Veri yetersizse {"rule": name, "status": "insufficient_data"} doner (uydurma deger yok).
    """
    _check_name(name)
    day = _day(made_at)
    out: dict = {"rule": name}
    if name == "always_up":
        return out
    if name == "random":
        out.update(seed=int(seed), ticker=ticker, date=str(day.date()))
        return out
    if name == "base_rate":
        if base_rate is None or (isinstance(base_rate, float) and math.isnan(base_rate)):
            out["status"] = "insufficient_data"
        else:
            out["base_rate"] = float(base_rate)
        return out
    s = _usable_closes(closes, day)
    if name == "yesterday":
        if s is None or len(s) < 2:
            out["status"] = "insufficient_data"
            return out
        out.update(
            date=str(day.date()),
            close=float(s.iloc[-1]),
            prev_close=float(s.iloc[-2]),
            ret_1d=float(s.iloc[-1] / s.iloc[-2] - 1.0),
        )
        return out
    if s is None or len(s) < MA_WINDOW:
        out["status"] = "insufficient_data"
        return out
    out.update(
        date=str(day.date()),
        close=float(s.iloc[-1]),
        ma20=float(s.iloc[-MA_WINDOW:].mean()),
        n_bars=MA_WINDOW,
    )
    return out


def historical_base_rate(
    histories: dict[str, pd.DataFrame],
    made_at,
    calendar,
    horizon_days: int,
    *,
    window_days: int = 250,
    label_threshold: float = 0.0,
    min_samples: int = 30,
) -> float | None:
    """Havuzlanmis (tum hisseler) yukselis orani; yalnizca etiketi made_at'te ZATEN BILINEN ornekler.

    Ornek gunu s uygundur ancak calendar.add_trading_days(s, horizon_days) <= made_at ise (s ve s+H
    takvim gunleridir). Uygun s'lerin SON `window_days` tanesi kullanilir. Etiket =
    close_adj[s+H] / close_adj[s] - 1 > label_threshold. Her iki kapanisi da gecerli olan (hisse, s)
    cifti ornek sayilir; ornek sayisi < min_samples ise None.
    """
    if horizon_days < 1 or window_days < 1:
        raise ValueError("horizon_days ve window_days >= 1 olmali")
    day = _day(made_at)
    days = calendar.days
    days = days[days <= day]
    if len(days) <= horizon_days:
        return None
    # days[i] icin add_trading_days(days[i], H) == days[i + H]; <= made_at olanlar: i + H < len(days)
    starts = days[: len(days) - horizon_days][-window_days:]
    ends = days[horizon_days:][-len(starts):]
    ups = 0
    total = 0
    for df in histories.values():
        s = df["close_adj"]
        if s.index.tz is not None:
            raise ValueError("histories indeksi tz-naive olmali")
        if s.index.has_duplicates:
            raise ValueError("histories indeksinde tekrar eden tarih var")
        c0 = s.reindex(starts).to_numpy(dtype=float)
        c1 = s.reindex(ends).to_numpy(dtype=float)
        ok = np.isfinite(c0) & np.isfinite(c1) & (c0 > 0) & (c1 > 0)
        if not ok.any():
            continue
        ret = c1[ok] / c0[ok] - 1.0
        ups += int((ret > label_threshold).sum())
        total += int(ok.sum())
    if total < min_samples:
        return None
    return ups / total
