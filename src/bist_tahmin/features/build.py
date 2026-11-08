"""Sizintisiz ozellik uretimi (PROJE.md Bolum 5 ve 9).

Saf pandas/numpy: veritabanina veya ag'a bagli degildir. Girdi `read_prices` ciktisi biciminde gunluk barlardir.

Sizinti kurallari (hepsi burada uygulanir ve tests/test_leakage.py ile sinanir):
- Her girdi (hisseler, XU100, takvim) once `index <= made_at` ile kesilir; made_at sonrasi satirlar hicbir
  sonucu degistiremez.
- Tum gostergeler, kesilmis serinin SON penceresinden dogrudan hesaplanir (kayan pencere nesnesi, ileri/geri
  kaydirma, ortalanmis pencere yok). Yani bir degerin kullanabilecegi tek bilgi made_at ve oncesidir.
- Hisseler arasi z-skor yalnizca made_at gununun kesitinden hesaplanir; egitim penceresi/gecmis gunler kullanilmaz.
- Takvim ozellikleri yalnizca made_at ve ONCEKI islem gunune bakar.

Fiyat tabanli her sey `close_adj` (bolunme/bedelsiz duzeltmeli) uzerindedir: ham `close` bedelsiz gunu sahte
-%50 gosterir (PROJE.md Bolum 4). Hacim ayri ele alinir, bkz. `_volume_adj`.

Hicbir sey uydurulmaz/doldurulmaz: eksik bar, gecersiz fiyat ya da hesaplanamayan ozellik ticker'i o gun
`excluded` listesine, gerekceyle, koyar.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..calendar import TradingCalendar

FEATURE_VERSION = "f1"

# Hisseye ozgu ham ozellikler; her biri icin `<ad>_z` (hisseler arasi z-skor) de uretilir.
STOCK_FEATURES: tuple[str, ...] = (
    "ret_1", "ret_5", "ret_10", "ret_20",
    "ma5", "ma20", "ma50",
    "vol_10", "vol_20",
    "rsi_14",
    "volume_ratio",
    "rs_5", "rs_20",
)
Z_FEATURES: tuple[str, ...] = tuple(f"{n}_z" for n in STOCK_FEATURES)
# Piyasa ve takvim ozellikleri o gun tum hisseler icin aynidir, z'lenmez.
MARKET_FEATURES: tuple[str, ...] = ("mkt_ret_1", "mkt_ret_5", "mkt_ret_20", "mkt_vol_20", "mkt_ma20", "breadth")
CALENDAR_FEATURES: tuple[str, ...] = ("weekday", "post_holiday")
FEATURE_NAMES: tuple[str, ...] = STOCK_FEATURES + Z_FEATURES + MARKET_FEATURES + CALENDAR_FEATURES

# pre_holiday BILEREK YOK: "yarin tatil mi" bilgisi, bir sonraki islem gununun takvimde olmamasindan cikar. Takvim
# XU100 barlarindan turetilir (calendar.py); made_at aninda bir sonraki gunun XU100 bari henuz yoktur, yani takvim
# bunu BILEMEZ. Statik resmi tatil listesinden doldurmak ise takvimin tek-kaynak kuralini bozar. Ileriye bakan
# (sizintili) bir bayrak uretmektense hic uretmeyiz. post_holiday ise yalnizca gecmise bakar.

Z_CLIP = 3.0
_Z_STD_EPS = 1e-12      # bu esigin altindaki std "sabit" sayilir (z = 0); kayan nokta gurultusunden korur
_ZERO_SNAP = 1e-12      # |x| bunun altindaysa 0 yazilir (toplama sirasindan dogan 1e-17 farklari hash'i bozmasin)
_RSI_PERIOD = 14
# Wilder yumusatmasinin hafizasi sonsuzdur; hafiza (13/14)^k ile sonumlenir. Ayni degerler her girdi uzunlugunda
# cikabilsin diye son bu kadar bar kullanilir ((13/14)^250 ~ 1e-8).
_RSI_MAX_BARS = 250


@dataclass(frozen=True)
class FeatureRow:
    ticker: str
    features: dict[str, float]
    features_json: str
    feature_hash: str


@dataclass
class FeatureSet:
    made_at: pd.Timestamp
    rows: dict[str, FeatureRow] = field(default_factory=dict)
    excluded: dict[str, str] = field(default_factory=dict)  # ticker -> gerekce


# --- kanonik JSON ve hash ------------------------------------------------------------------------------------

def _canon(x: float) -> float:
    """10 anlamli basamaga yuvarla; -0.0 ve toplama gurultusu 0.0 olur. NaN/sonsuz ValueError."""
    x = float(x)
    if not math.isfinite(x):
        raise ValueError(f"ozellik degeri sonlu degil: {x!r}")
    if abs(x) < _ZERO_SNAP:
        return 0.0
    return float(f"{x:.10g}") + 0.0


def make_feature_row(ticker: str, features: Mapping[str, float]) -> FeatureRow:
    """Ozellik sozlugunden kanonik FeatureRow: yuvarla, sirasiz-JSON, sha256. Satir/ticker sirasindan bagimsiz."""
    canon = {str(k): _canon(v) for k, v in features.items()}
    payload = json.dumps({"_v": FEATURE_VERSION, **canon}, sort_keys=True, separators=(",", ":"))
    return FeatureRow(
        ticker=ticker,
        features=canon,
        features_json=payload,
        feature_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    )


# --- girdi dogrulama -----------------------------------------------------------------------------------------

def _check_frame(df: pd.DataFrame, name: str) -> pd.DataFrame:
    """Sozlesme ihlalleri sessizce duzeltilmez, ValueError. Sirali kopya doner (girdi degistirilmez)."""
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError(f"{name}: index DatetimeIndex olmali")
    if df.index.tz is not None:
        raise ValueError(f"{name}: index tz-naive olmali")
    missing = [c for c in ("close", "close_adj", "volume") if c not in df.columns]
    if missing:
        raise ValueError(f"{name}: eksik kolon(lar): {missing}")
    if df.index.has_duplicates:
        raise ValueError(f"{name}: tekrar eden tarih var")
    return df.sort_index()


def _norm_made_at(made_at) -> pd.Timestamp:
    ts = pd.Timestamp(made_at)
    if ts.tzinfo is not None:
        raise ValueError(f"made_at tz-naive olmali: {made_at!r}")
    return ts.normalize()


# --- gosterge hesaplari (hepsi serinin SON penceresinden; ileriye bakmaz) ----------------------------------

def _ret(c: np.ndarray, k: int) -> float:
    """k barlik getiri: c[-1] / c[-1-k] - 1."""
    if len(c) <= k:
        return math.nan
    return float(c[-1] / c[-1 - k] - 1.0)


def _ma_ratio(c: np.ndarray, k: int) -> float:
    """fiyat / MA(k) - 1; MA son k bar (bugun dahil), basit ortalama."""
    if len(c) < k:
        return math.nan
    return float(c[-1] / np.mean(c[-k:]) - 1.0)


def _vol(c: np.ndarray, k: int) -> float:
    """Son k gunluk basit getirinin std'si (ddof=1). k getiri icin k+1 kapanis gerekir."""
    if len(c) < k + 1:
        return math.nan
    r = c[-k:] / c[-k - 1:-1] - 1.0
    return float(np.std(r, ddof=1))


def _wilder_rsi(c: np.ndarray, period: int = _RSI_PERIOD) -> float:
    """RSI, Wilder yumusatmasi (Wilder'in orijinal tanimi; kayan-ortalama RSI'dan farklidir).

    Tohum: ilk `period` degisimin basit ortalamasi (ortalama kazanc/kayip). Sonra
    avg = (avg * (period - 1) + yeni) / period. Seri, son kesintisiz (NaN'siz) bar kosusudur ve en fazla
    _RSI_MAX_BARS bar. Kayip ortalamasi 0 ve kazanc > 0 ise RSI = 100; ikisi de 0 (tamamen duz seri) ise
    tanimsiz, NaN doner (ticker 'nan_feature:rsi_14' ile elenir; 50 uydurulmaz).
    """
    bad = ~np.isfinite(c)
    if bad.any():
        c = c[np.flatnonzero(bad)[-1] + 1:]  # son NaN'dan sonraki kesintisiz kosu
    c = c[-(_RSI_MAX_BARS + 1):]
    if len(c) < period + 1:
        return math.nan
    d = np.diff(c)
    gain, loss = np.maximum(d, 0.0), np.maximum(-d, 0.0)
    avg_g, avg_l = float(gain[:period].mean()), float(loss[:period].mean())
    for g_t, l_t in zip(gain[period:], loss[period:]):
        avg_g = (avg_g * (period - 1) + g_t) / period
        avg_l = (avg_l * (period - 1) + l_t) / period
    if avg_l == 0.0:
        return 100.0 if avg_g > 0.0 else math.nan
    return float(100.0 - 100.0 / (1.0 + avg_g / avg_l))


def _volume_adj(volume: np.ndarray, close: np.ndarray, close_adj: np.ndarray) -> np.ndarray:
    """Bolunme duzeltmeli hacim = volume * close / close_adj.

    Neden: bedelsiz/bolunmede hisse adedi artar, ham hacim (adet) o gun kalici olarak basamak atlar (ornek %100
    bedelsiz: ayni islem faaliyeti 2x adet). close_adj gecmisi asagi duzeltir (duzeltme orani = close_adj/close);
    gecmis hacmi bu oranin tersiyle carpmak onu bugunku adet cinsine cevirir. Boylece 20 gunluk hacim ortalamasi
    bolunme gununu kesmez ve volume_ratio sahte sicrama/dusus vermez.
    """
    with np.errstate(all="ignore"):
        out = volume * close / close_adj
    out[~np.isfinite(out)] = math.nan
    return out


def _z_scores(values: dict[str, float]) -> dict[str, float]:
    """Bir gunun kesitinde z-skor: ddof=0, std==0 ise 0, +-Z_CLIP'e kirp. Yalniz o gunun degerleri."""
    tickers = list(values)
    x = np.array([values[t] for t in tickers], dtype=float)
    mean, std = float(x.mean()), float(x.std(ddof=0))
    if std <= _Z_STD_EPS * max(1.0, abs(mean)):
        z = np.zeros_like(x)
    else:
        z = np.clip((x - mean) / std, -Z_CLIP, Z_CLIP)
    return dict(zip(tickers, z.tolist()))


# --- ana fonksiyon -------------------------------------------------------------------------------------------

def build_features(
    histories: Mapping[str, pd.DataFrame],
    xu100: pd.DataFrame,
    made_at,
    calendar: TradingCalendar,
    *,
    min_history_days: int = 60,
) -> FeatureSet:
    """made_at gunu kapanisi itibariyla her hisse icin ozellik vektoru.

    Haric tutma gerekceleri (`excluded`): 'no_bar_on_made_at', 'insufficient_history', 'invalid_close_adj',
    'nan_feature:<ad>' (FEATURE_NAMES sirasindaki ilk hesaplanamayan ham/piyasa/takvim ozelligi).
    Hisse serileri takvim gunlerine hizalanir (takvimde olmayan gun atilir); takvimde olup hissede olmayan gun
    NaN olur ve o gunu kapsayan pencereler hesaplanamaz (doldurulmaz) -> 'nan_feature'.
    `min_history_days`, takvim gridinde gecerli (sonlu, > 0) close_adj bar sayisidir.

    z-skor kesiti, o gun HAM ozellikleri sonlu olan (yani sonunda rows'ta yer alan) hisselerdir; elenen hisse
    baskalarinin z'sini etkilemez/etkilenmez. Piyasa genisligi (breadth) ise made_at'te bari ve gecerli MA20'si
    olan tum hisselerden hesaplanir (min_history_days'e takilanlar dahil).

    Raises ValueError: made_at tz-aware ya da islem gunu degil; girdi sozlesmesi ihlali; XU100'de made_at
    kapanisi yok (takvim XU100'den gelir, tutarsizlik yuksek sesle bildirilir).
    """
    made_at = _norm_made_at(made_at)
    if not calendar.is_trading_day(made_at):
        raise ValueError(f"made_at islem gunu degil: {made_at.date()}")

    grid = calendar.days[calendar.days <= made_at]  # takvim de made_at'te kesilir
    xu = _check_frame(xu100, "xu100")
    xu = xu.loc[xu.index <= made_at]
    if made_at not in xu.index:
        raise ValueError(f"xu100 {made_at.date()} barini icermiyor")

    def _close_adj_on_grid(df: pd.DataFrame) -> np.ndarray:
        s = df["close_adj"].astype("float64").reindex(grid)
        return s.where(np.isfinite(s) & (s > 0)).to_numpy(dtype=float)

    mc = _close_adj_on_grid(xu)
    if not np.isfinite(mc[-1]):
        raise ValueError(f"xu100 close_adj {made_at.date()} gunu gecersiz")
    market = {
        "mkt_ret_1": _ret(mc, 1), "mkt_ret_5": _ret(mc, 5), "mkt_ret_20": _ret(mc, 20),
        "mkt_vol_20": _vol(mc, 20), "mkt_ma20": _ma_ratio(mc, 20),
    }

    prev_day = calendar.prev_trading_day(made_at)
    if prev_day is None:
        post_holiday = math.nan  # onceki islem gunu bilinmiyor; uydurulmaz
    else:
        # made_at ile onceki islem gunu arasinda (ikisi haric) en az bir hafta ici gun varsa tatil sonrasi.
        post_holiday = float(np.busday_count((prev_day + pd.Timedelta(days=1)).date(), made_at.date()) > 0)
    calendar_feats = {"weekday": float(made_at.weekday()), "post_holiday": post_holiday}

    result = FeatureSet(made_at=made_at)
    raw: dict[str, dict[str, float]] = {}
    above_ma20: dict[str, bool] = {}

    for ticker in sorted(histories):
        df = _check_frame(histories[ticker], ticker)
        df = df.loc[df.index <= made_at]  # SIZINTI KAPISI: made_at sonrasi satir buradan oteye gecmez
        if made_at not in df.index:
            result.excluded[ticker] = "no_bar_on_made_at"
            continue
        c = _close_adj_on_grid(df)
        if int(np.isfinite(c).sum()) < min_history_days:
            result.excluded[ticker] = "insufficient_history"
            continue
        if not np.isfinite(c[-1]):
            result.excluded[ticker] = "invalid_close_adj"
            continue

        ma20_val = _ma_ratio(c, 20)
        if math.isfinite(ma20_val):
            above_ma20[ticker] = bool(ma20_val > 0.0)

        v = df["volume"].astype("float64").where(lambda s: np.isfinite(s) & (s >= 0)).reindex(grid).to_numpy(float)
        raw_close = df["close"].astype("float64").where(lambda s: np.isfinite(s) & (s > 0)).reindex(grid)
        vadj = _volume_adj(v, raw_close.to_numpy(dtype=float), c)
        vol_mean = float(np.mean(vadj[-20:])) if len(vadj) >= 20 else math.nan
        with np.errstate(all="ignore"):
            volume_ratio = float(vadj[-1] / vol_mean) if vol_mean > 0 else math.nan

        f = {
            "ret_1": _ret(c, 1), "ret_5": _ret(c, 5), "ret_10": _ret(c, 10), "ret_20": _ret(c, 20),
            "ma5": _ma_ratio(c, 5), "ma20": ma20_val, "ma50": _ma_ratio(c, 50),
            "vol_10": _vol(c, 10), "vol_20": _vol(c, 20),
            "rsi_14": _wilder_rsi(c),
            "volume_ratio": volume_ratio,
        }
        f["rs_5"] = f["ret_5"] - market["mkt_ret_5"]
        f["rs_20"] = f["ret_20"] - market["mkt_ret_20"]
        raw[ticker] = f

    breadth = (sum(above_ma20.values()) / len(above_ma20)) if above_ma20 else math.nan
    common = {**market, "breadth": breadth, **calendar_feats}

    # Ham ozelliklerden biri sonlu degilse eler (ilk ad FEATURE_NAMES sirasina gore); z kesiti yalniz kalanlardir.
    ok: dict[str, dict[str, float]] = {}
    for ticker, f in raw.items():
        bad = next((n for n in STOCK_FEATURES + MARKET_FEATURES + CALENDAR_FEATURES
                    if not math.isfinite({**f, **common}[n])), None)
        if bad is not None:
            result.excluded[ticker] = f"nan_feature:{bad}"
        else:
            ok[ticker] = f

    zs = {n: _z_scores({t: f[n] for t, f in ok.items()}) for n in STOCK_FEATURES} if ok else {}
    for ticker, f in ok.items():
        feats = {**f, **{f"{n}_z": zs[n][ticker] for n in STOCK_FEATURES}, **common}
        result.rows[ticker] = make_feature_row(ticker, feats)

    result.excluded = dict(sorted(result.excluded.items()))
    return result


# --- genel sizinti dedektoru ---------------------------------------------------------------------------------

FeatureFn = Callable[[dict[str, pd.DataFrame], pd.DataFrame, pd.Timestamp, TradingCalendar], FeatureSet]


def _same_float(a, b) -> bool:
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return a == b
    return (math.isnan(a) and math.isnan(b)) or a == b


def _diff_feature_sets(full: FeatureSet, cut: FeatureSet) -> list[str]:
    diffs: list[str] = []
    if pd.Timestamp(full.made_at) != pd.Timestamp(cut.made_at):
        diffs.append(f"made_at: {full.made_at} != {cut.made_at}")
    if full.excluded != cut.excluded:
        for t in sorted(set(full.excluded) | set(cut.excluded)):
            if full.excluded.get(t) != cut.excluded.get(t):
                diffs.append(f"excluded[{t}]: {full.excluded.get(t)!r} != {cut.excluded.get(t)!r}")
    for t in sorted(set(full.rows) | set(cut.rows)):
        a, b = full.rows.get(t), cut.rows.get(t)
        if a is None or b is None:
            diffs.append(f"rows[{t}]: yalnizca {'tam' if b is None else 'kesik'} veride var")
            continue
        for n in sorted(set(a.features) | set(b.features)):
            if n not in a.features or n not in b.features:
                diffs.append(f"{t}.{n}: yalnizca {'tam' if n not in b.features else 'kesik'} veride var")
            elif not _same_float(a.features[n], b.features[n]):
                diffs.append(f"{t}.{n}: tam={a.features[n]!r} kesik={b.features[n]!r}")
        if a.feature_hash != b.feature_hash:
            diffs.append(f"{t}.feature_hash farkli")
        if a.features_json != b.features_json:
            diffs.append(f"{t}.features_json farkli")
    return diffs


def assert_no_lookahead(
    feature_fn: FeatureFn,
    histories: Mapping[str, pd.DataFrame],
    xu100: pd.DataFrame,
    made_at,
    calendar: TradingCalendar,
    *,
    require_future_rows: bool = True,
) -> None:
    """Genel sizinti dedektoru: `feature_fn(histories, xu100, made_at, calendar) -> FeatureSet`.

    Fonksiyonu once tam veriyle, sonra made_at sonrasi satirlari (hisseler, XU100 ve takvim) kesilmis veriyle
    calistirir. features, hash ve excluded farkliysa AssertionError (fark ayrintisiyla). Gelecege bakan bir
    ozellik (ileri getiri, tum ornek istatistigi, vb.) kesik veride farkli deger/NaN verecegi icin yakalanir.

    made_at sonrasi hic satir yoksa kesme bir sey degistirmez ve test bos gecer; bu yuzden varsayilan olarak
    ValueError verilir (`require_future_rows=False` ile kapatilir). Girdiler degistirilmez.
    """
    made_at = _norm_made_at(made_at)
    cut_h = {t: df.loc[df.index <= made_at].copy() for t, df in histories.items()}
    cut_x = xu100.loc[xu100.index <= made_at].copy()
    cut_cal = TradingCalendar(calendar.days[calendar.days <= made_at])

    if require_future_rows:
        has_future = (
            any(len(df) > len(cut_h[t]) for t, df in histories.items())
            or len(xu100) > len(cut_x)
            or len(calendar) > len(cut_cal)
        )
        if not has_future:
            raise ValueError("made_at sonrasi satir yok: dedektor bu girdiyle bir sey kanitlamaz")

    full = feature_fn(dict(histories), xu100, made_at, calendar)
    cut = feature_fn(cut_h, cut_x, made_at, cut_cal)
    diffs = _diff_feature_sets(full, cut)
    if diffs:
        shown = "; ".join(diffs[:10]) + (f" ... (+{len(diffs) - 10} fark)" if len(diffs) > 10 else "")
        raise AssertionError(f"SIZINTI: made_at={made_at.date()} sonrasi veri sonucu degistiriyor: {shown}")
