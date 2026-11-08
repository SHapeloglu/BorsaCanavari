"""Veri kaynagi soyutlamasi (PROJE.md Bolum 4 ve 18: saglayici degisikligi riski).

Cikti sozlesmesi (history ve index_history): DataFrame; index = tz-naive, normalize edilmis
`date` (DatetimeIndex, adi "date", artan, tekrarsiz); kolonlar kucuk harf:
open, high, low, close, volume, close_adj.
- `close`  : HAM kapanis (bolunme/bedelsiz duzeltmesi geri alinmis; borsapy adjust=False).
- `close_adj`: borsapy varsayilan kapanisi (TradingView bolunme duzeltmeli; temettu duzeltmesi YOK).
- open/high/low/volume ham serinin degerleridir (close ile ayni olcekte).
Bu modul bugunun yarim bari gibi kalite konularini cozmez; yalnizca ham veriyi dondurur.
"""

from __future__ import annotations

import logging
from typing import Protocol

import pandas as pd

log = logging.getLogger(__name__)

COLUMNS = ["open", "high", "low", "close", "volume", "close_adj"]
_OHLCV = {"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}
# Ham/duzeltmeli kapanis orani beklenen bedelsiz carpanindan bu kadar saparsa uyari verilir.
_RATIO_TOLERANCE = 0.01


class SourceError(RuntimeError):
    """Veri kaynagi cevap vermedi ya da guvenilmez (ham fiyat dogrulanamadi vb.)."""


class DataSource(Protocol):
    name: str

    def history(self, ticker: str, start: str | None = None, end: str | None = None) -> pd.DataFrame: ...

    def index_history(self, symbol: str, start: str | None = None, end: str | None = None) -> pd.DataFrame: ...

    def index_components(self, symbol: str) -> list[str]: ...

    def corporate_actions(self, ticker: str) -> pd.DataFrame:
        """Kolonlar: ex_date (datetime64, tz-naive), kind ('split'|'dividend'), value (float)."""
        ...


def normalize_index(idx: pd.Index) -> pd.DatetimeIndex:
    """tz'li (09:00+03:00 damgali) indeksi tz-naive, normalize edilmis `date` indeksine cevirir."""
    out = pd.DatetimeIndex(idx)
    if out.tz is not None:
        out = out.tz_localize(None)  # duvar saati korunur (Istanbul tarihi); UTC'ye cevirmeyiz
    out = out.normalize().as_unit("ns")
    out.name = "date"
    return out


def normalize_frame(raw: pd.DataFrame, adj: pd.DataFrame | None = None) -> pd.DataFrame:
    """borsapy ciktisini (Open/High/Low/Close/Volume, tz'li indeks) sozlesmeye cevirir.

    `raw`: adjust=False ciktisi. `adj`: varsayilan (duzeltmeli) cikti; None ise close_adj = close.
    Ayni tarihe dusen tekrar barlar varsa sonuncusu tutulur.
    """
    empty = pd.DataFrame(
        {c: pd.Series(dtype="float64") for c in COLUMNS},
        index=pd.DatetimeIndex([], name="date"),
    )
    if raw is None or raw.empty:
        return empty
    df = raw.rename(columns=_OHLCV)[list(_OHLCV.values())].astype("float64")
    df.index = normalize_index(raw.index)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    if adj is None:
        df["close_adj"] = df["close"]
    else:
        a = adj["Close"].astype("float64").copy()
        a.index = normalize_index(adj.index)
        a = a[~a.index.duplicated(keep="last")]
        df["close_adj"] = a.reindex(df.index)
    return df[COLUMNS]


def _naive_day(d) -> pd.Timestamp:
    ts = pd.Timestamp(d)
    return (ts.tz_localize(None) if ts.tzinfo else ts).normalize()


def bonus_events(splits: pd.DataFrame | None) -> list[tuple[pd.Timestamp, float]]:
    """borsapy `splits` tablosundan (tarih, ham/duzeltmeli carpan) listesi. Yalnizca bedelsiz.

    Carpan = 1 + (BonusFromCapital + BonusFromDividend) / 100. Bedelli (RightsIssue) hesaba katilmaz:
    borsapy'nin adjust=False geri almasi da yalnizca bedelsizi kapsar.
    """
    if splits is None or splits.empty:
        return []
    events = []
    for d, row in splits.iterrows():
        pct = float(row.get("BonusFromCapital", 0) or 0) + float(row.get("BonusFromDividend", 0) or 0)
        if pct > 0:
            events.append((_naive_day(d), 1.0 + pct / 100.0))
    return sorted(events)


def check_raw_vs_adjusted(df: pd.DataFrame, events: list[tuple[pd.Timestamp, float]], label: str = "") -> None:
    """Ham serinin gercekten ham oldugunu bedelsiz olaylariyla dogrular.

    borsapy `adjust=False` icinde `splits` (Is Yatirim) alinamazsa hatayi sessizce yutup DUZELTMELI seriyi
    dondurur. Bunu yakalamak icin: araliktaki her bedelsiz olayindan once en az bir bar varsa, o barda
    close/close_adj orani, sonraki olaylarin carpanlari carpimina (~%1) esit olmali.
    Hic uyusmuyorsa (oran ~1) SourceError; kucuk sapmada yalnizca uyari.
    """
    if df.empty or not events:
        return
    first, last = df.index[0], df.index[-1]
    inside = [(d, r) for d, r in events if first < d <= last]
    if not inside:
        return
    ratio = (df["close"] / df["close_adj"]).dropna()
    if ratio.empty:
        return
    expected = 1.0
    for _, r in inside:
        expected *= r
    got = float(ratio.iloc[0])
    if abs(got - 1.0) < 1e-6:
        raise SourceError(
            f"{label}: ham kapanis duzeltmeli kapanisla ayni ama aralikta {len(inside)} bedelsiz olayi var "
            "(borsapy adjust=False geri alma basarisiz; splits alinamamis olabilir)"
        )
    if abs(got / expected - 1.0) > _RATIO_TOLERANCE:
        log.warning("%s: ham/duzeltmeli oran %.4f, beklenen %.4f (bedelsiz olaylariyla uyumsuz)", label, got, expected)


def _range_kwargs(start: str | None, end: str | None) -> dict:
    """start verilirse tarih araligi; yoksa period='max' (borsapy en fazla ~3650 bar dondurur)."""
    if start:
        return {"start": start, "end": end}
    return {"period": "max"}


class BorsapySource:
    """borsapy 0.11 uzerinden veri. Ag erisimi gerektirir."""

    name = "borsapy"

    def __init__(self) -> None:
        import borsapy as bp  # gec import: testlerde sahte kaynak kullanilirken gerekmez

        self._bp = bp

    def history(self, ticker: str, start: str | None = None, end: str | None = None) -> pd.DataFrame:
        t = self._bp.Ticker(ticker)
        # splits'i acikca al: adjust=False icindeki ayni cagri hatayi yutar (bkz. check_raw_vs_adjusted).
        try:
            splits = t.splits
        except Exception as exc:
            raise SourceError(f"{ticker}: splits alinamadi, ham fiyat dogrulanamaz: {exc}") from exc
        kw = _range_kwargs(start, end)
        raw = t.history(adjust=False, **kw)
        adj = t.history(adjust=True, **kw)
        df = normalize_frame(raw, adj)
        check_raw_vs_adjusted(df, bonus_events(splits), label=ticker)
        return df

    def index_history(self, symbol: str, start: str | None = None, end: str | None = None) -> pd.DataFrame:
        idx = self._bp.Index(symbol)
        return normalize_frame(idx.history(**_range_kwargs(start, end)), None)

    def index_components(self, symbol: str) -> list[str]:
        return sorted(str(s).upper() for s in self._bp.Index(symbol).component_symbols)

    def corporate_actions(self, ticker: str) -> pd.DataFrame:
        t = self._bp.Ticker(ticker)
        rows: list[dict] = []
        for d, r in bonus_events(t.splits):
            rows.append({"ex_date": d, "kind": "split", "value": r})
        divs = t.dividends
        # Gelecekteki (henuz gerceklesmemis, degisebilecek) temettu duyurulari yazilmaz.
        today = pd.Timestamp.now(tz="Europe/Istanbul").tz_localize(None).normalize()
        if divs is not None and not divs.empty and "Amount" in divs.columns:
            for d, row in divs.iterrows():
                amt = row["Amount"]
                if amt is not None and pd.notna(amt) and amt > 0:
                    rows.append({"ex_date": _naive_day(d), "kind": "dividend", "value": float(amt)})
        out = pd.DataFrame(rows, columns=["ex_date", "kind", "value"])
        out = out[out["ex_date"] <= today]
        # Ayni (ex_date, kind) tekrar ederse (or. ayni gun iki temettu satiri) degerleri topla.
        if not out.empty:
            out = out.groupby(["ex_date", "kind"], as_index=False)["value"].sum()
        return out
