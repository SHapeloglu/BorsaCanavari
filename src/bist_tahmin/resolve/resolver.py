"""Vadesi gelen tahminleri gerceklesen sonuclariyla cozer (PROJE.md Bolum 5, 6, 11).

Alanlar (hepsi BOLUNME/BEDELSIZ DUZELTMELI, TEMETTU ICERMEZ: toplam getiri degil, fiyat getirisidir):
- realized_ret     = close_adj[end] / close_adj[made_at] - 1                (kapanistan kapanisa)
- realized_rel_ret = realized_ret - XU100'un ayni donem getirisi
- tradable_ret     = close_adj[end] / open_adj[next_day] - 1                (ertesi gun acilisinda giris)
- label_up         = int(realized_ret > label_threshold); label_rel_up = int(realized_rel_ret > 0)
- resolved_at      = end_date = takvimde made_at'ten horizon_days ISLEM gunu sonrasi (tatil atlanir)

Kurallar:
- Vade gelmeden cozulmez: ticker'in ve XU100'un end_date barlari yoksa atlanir. Eksik veri uydurulmaz/doldurulmaz.
- Cozum TEK SEFERLIK ve geri alinamaz (002_guard.sql), bu yuzden belirsiz durumda YAZMAK yerine ATLANIR.

DUZELTME TUTARSIZLIGI KORUMASI: `close_adj` fetch zamaninda donmus bir goruntudur. made_at ile end arasinda YENI bir
bolunme olduysa, made_at satiri eski olcekte kalmis olabilir (ve getiri sahte -%50 gibi gorunur). Bu yuzden her getiri
iki yoldan hesaplanir ve fark `MISMATCH_TOL`u asarsa sonuc YAZILMAZ ('adjustment_mismatch'):
  (a) duzeltmeli oran:  close_adj[end] / close_adj[made_at] - 1
  (b) ham oran:         close[end] * carpan(made_at, end] / close[made_at] - 1
      carpan(a, b] = (a, b] araligindaki bolunme carpanlarinin carpimi (BIMAS %100 bedelsiz -> 2.0).
tradable_ret ayni sekilde korunur: (a) close_adj[end] / open_adj[next_day] - 1 ile
(b) close[end] * carpan(next_day, end] / open[next_day] - 1. Bolunme gunu (ex_date) acilisi zaten yeni olcektedir,
bu yuzden next_day'deki bolunme carpana GIRMEZ (aralik next_day'i dislar). Bu yol, corporate_actions tablosunda
bolunme eksikse de yakalar (ham dusus var, duzeltmeli yok -> fark buyuk -> atla). Esik mutlak getiri farkidir (1e-3):
fiyat tiklerinin yuvarlanmasi cok ucuz hisselerde bolunme penceresinde birkac on bin
baz puanlik fark uretebilir; belirsizlikte atlamak (geri alinamaz yazmaktan) bilincli tercihtir.

ACIKLANAMAYAN SICRAMA KORUMASI ('unexplained_jump'): yukaridaki iki yol AYNI sahte dususu gosterebilir: kaynak bir
bedelsizi hic uygulamamissa (close ve close_adj ikisi de 102 -> 51) VE corporate_actions'ta kayit yoksa carpan 1.0'dir,
fark 0 cikar, koruma gecer ve yanlis getiri (-%50) geri alinamaz yazilirdi. Bu yuzden made_at ile end_date (dahil)
arasindaki barlarda, hem ticker hem XU100 icin, ardisik barlar arasi close_adj getirisinin mutlak degeri
`MAX_DAILY_MOVE`i asarsa sonuc YAZILMAZ. Esik 0.15: BIST'te gunluk fiyat limiti yaklasik %10'dur, yani gercek bir
gunluk kapanis hareketi bunu asamaz; %15 limit, tick yuvarlamasi ve limitin degistigi/ilk islem gunu gibi nadir
durumlar icin pay birakir, ayni zamanda en kucuk bedelsiz/bolunme dususunu (%100 bedelsiz = -%50; %25 bedelsiz = -%20)
yakalar. Bu bir 'suphede yazma' kararidir: satir sonraki calistirmada yeniden degerlendirilir (veri duzelirse cozulur).

TAKILI SATIRLAR: ticker'in barlari durduysa (islem durdurma, delist, fetch bozuklugu) ve takvim end_date'i en az
`STALE_TRADING_DAYS` islem gunu gecmisse neden 'not_due' degil 'stale_ticker' olur; boylece sonsuza dek takilan
satirlar ayrisir. Toplam gecikme icin `count_overdue`.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable
from dataclasses import dataclass, field

import pandas as pd
import psycopg

from ..calendar import TradingCalendar
from ..data.store import read_prices

XU100 = "XU100"
MISMATCH_TOL = 1e-3  # duzeltmeli ve ham yoldan getiri farkinin (mutlak) azami degeri
MAX_DAILY_MOVE = 0.15  # ardisik barlar arasi close_adj getirisinin azami mutlak degeri (BIST limiti ~%10)
STALE_TRADING_DAYS = 3  # takvim end_date'i bu kadar islem gunu gectiyse ve ticker bari yoksa 'stale_ticker'
MAX_DETAILS_PER_REASON = 20  # her atlama nedeni icin details'e yazilan en fazla ornek satir
MAX_OVERDUE_TICKERS = 20


@dataclass(frozen=True)
class Outcome:
    resolved_at: dt.date
    realized_ret: float
    realized_rel_ret: float
    tradable_ret: float
    label_up: int
    label_rel_up: int


@dataclass
class ResolveResult:
    resolved: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    dry_run: bool = False
    details: list[str] = field(default_factory=list)

    def skip(self, reason: str, detail: str | None = None) -> None:
        """Nedeni sayar; details'e neden basina en fazla MAX_DETAILS_PER_REASON ornek satir yazar."""
        n = self.skipped[reason] = self.skipped.get(reason, 0) + 1
        if detail and n <= MAX_DETAILS_PER_REASON:
            self.details.append(f"{reason}: {detail}")


def _day(d) -> pd.Timestamp:
    return pd.Timestamp(d).normalize()


def _value(bars: pd.DataFrame | None, day: pd.Timestamp, col: str) -> float | None:
    """Bar yoksa, sutun yoksa ya da deger NaN/sonlu degilse None."""
    if bars is None or col not in bars.columns or day not in bars.index:
        return None
    v = bars.at[day, col]
    if isinstance(v, pd.Series):  # tekrarlayan tarih: belirsiz
        return None
    if v is None or pd.isna(v) or not math.isfinite(float(v)):
        return None
    return float(v)


def _split_product(splits: Iterable, after: pd.Timestamp, upto: pd.Timestamp) -> float:
    """(after, upto] araligindaki bolunme carpanlarinin carpimi."""
    prod = 1.0
    for ex_date, factor in splits:
        if after < _day(ex_date) <= upto:
            prod *= float(factor)
    return prod


def _missing_reason(bars: pd.DataFrame | None, end: pd.Timestamp, calendar: TradingCalendar | None = None,
                    ref_day=None, *, check_stale: bool = False) -> str:
    """end barinin yoklugu: veri henuz gelmediyse 'not_due', ortada delik varsa 'missing_bar'.

    check_stale ve takvim verilmisse: son bar end'den ONCE ve referans gun (takvim sonu ya da as_of) end'i en az
    STALE_TRADING_DAYS islem gunu gecmisse 'stale_ticker' (bar akisi durmus; vade geldi ama veri gelmiyor)."""
    if bars is None or len(bars) == 0 or end > bars.index.max():
        if check_stale and calendar is not None and _stale_days(calendar, end, ref_day) >= STALE_TRADING_DAYS:
            return "stale_ticker"
        return "not_due"
    return "missing_bar"


def _stale_days(calendar: TradingCalendar, end: pd.Timestamp, ref_day=None) -> int:
    """(end, ref] araligindaki islem gunu sayisi; ref = takvim sonu (as_of daha erkense as_of). Bilinemezse 0."""
    ref = calendar.last_day if ref_day is None else min(_day(ref_day), calendar.last_day)
    if ref <= end:
        return 0
    n = calendar.trading_days_between(end, ref)
    return 0 if n is None else n


def _first_jump(bars: pd.DataFrame, made: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.Timestamp, float] | None:
    """[made, end] araligindaki ardisik barlar arasi close_adj getirisi MAX_DAILY_MOVE'u asan ilk (tarih, getiri)."""
    s = bars["close_adj"].astype(float).sort_index().loc[made:end].dropna()
    if len(s) < 2:
        return None
    rets = s.pct_change().dropna()
    bad = rets[rets.abs() > MAX_DAILY_MOVE]
    if bad.empty:
        return None
    return bad.index[0], float(bad.iloc[0])


def _evaluate(
    made_at, end_date, ticker_bars, xu_bars, calendar, label_threshold, splits, as_of=None
) -> tuple[Outcome | None, str | None, str | None]:
    """(sonuc, atlama_nedeni, ayrinti). Sonuc varsa neden None."""
    made = _day(made_at)
    end = _day(end_date)
    if end <= made:
        return None, "invalid_row", f"end_date {end.date()} made_at {made.date()} sonrasi degil"
    nxt = calendar.next_trading_day(made)
    if nxt is None or nxt > end:
        return None, "no_next_day", f"made_at {made.date()} sonrasi islem gunu yok ya da end_date oncesi degil"
    nxt = _day(nxt)
    splits = list(splits)

    # Vade kontrolu: iki serinin de end barinda gecerli kapanis olmali.
    for name, bars in (("ticker", ticker_bars), ("XU100", xu_bars)):
        if _value(bars, end, "close_adj") is None:
            reason = _missing_reason(bars, end, calendar, as_of, check_stale=(name == "ticker"))
            last = "yok" if bars is None or len(bars) == 0 else str(bars.index.max().date())
            return None, reason, f"{name} end_date {end.date()} bari yok (son bar {last}, takvim son gun {calendar.last_day.date()})"

    c0 = _value(ticker_bars, made, "close_adj")
    c1 = _value(ticker_bars, end, "close_adj")
    r0 = _value(ticker_bars, made, "close")
    r1 = _value(ticker_bars, end, "close")
    o1a = _value(ticker_bars, nxt, "open_adj")
    o1 = _value(ticker_bars, nxt, "open")
    x0 = _value(xu_bars, made, "close_adj")
    x1 = _value(xu_bars, end, "close_adj")
    needed = {"close_adj[made_at]": c0, "close[made_at]": r0, "close[end]": r1, "open_adj[next_day]": o1a,
              "open[next_day]": o1}
    missing = [k for k, v in needed.items() if v is None]
    if x0 is None:
        missing.append("XU100 close_adj[made_at]")
    if missing:
        return None, "missing_bar", f"{made.date()}->{end.date()}: eksik/gecersiz: {', '.join(missing)}"
    if min(c0, c1, r0, r1, o1a, o1, x0, x1) <= 0:
        return None, "invalid_price", f"{made.date()}->{end.date()}: sifir/negatif fiyat"

    ret_adj = c1 / c0 - 1.0
    ret_raw = r1 * _split_product(splits, made, end) / r0 - 1.0
    if abs(ret_adj - ret_raw) > MISMATCH_TOL:
        return None, "adjustment_mismatch", (
            f"{made.date()}->{end.date()}: realized duzeltmeli {ret_adj:+.4%} ham+bolunme {ret_raw:+.4%}")

    trad_adj = c1 / o1a - 1.0
    trad_raw = r1 * _split_product(splits, nxt, end) / o1 - 1.0
    if abs(trad_adj - trad_raw) > MISMATCH_TOL:
        return None, "adjustment_mismatch", (
            f"{made.date()}->{end.date()}: tradable duzeltmeli {trad_adj:+.4%} ham+bolunme {trad_raw:+.4%}")

    # Aciklanamayan sicrama: iki yol da ayni sahte dususu gosterebilir (kaynak bolunmeyi uygulamamis, kayit yok).
    for name, bars in (("ticker", ticker_bars), ("XU100", xu_bars)):
        jump = _first_jump(bars, made, end)
        if jump is not None:
            day, ret = jump
            return None, "unexplained_jump", (
                f"{name} {day.date()} close_adj getirisi {ret:+.2%} (esik {MAX_DAILY_MOVE:.0%}); "
                f"pencere {made.date()}->{end.date()}")

    rel = ret_adj - (x1 / x0 - 1.0)
    out = Outcome(
        resolved_at=end.date(),
        realized_ret=ret_adj,
        realized_rel_ret=rel,
        tradable_ret=trad_adj,
        label_up=int(ret_adj > label_threshold),
        label_rel_up=int(rel > 0),
    )
    return out, None, None


def compute_outcome(
    made_at,
    end_date,
    ticker_bars: pd.DataFrame,
    xu_bars: pd.DataFrame,
    calendar: TradingCalendar,
    *,
    label_threshold: float = 0.0,
    splits: Iterable = (),
    as_of=None,
) -> Outcome | None:
    """SAF fonksiyon (DB yok). Vade gelmediyse, bar eksikse ya da duzeltme tutarsizsa None.

    ticker_bars / xu_bars: index = tarih, kolonlar close, close_adj, open_adj (store.read_prices ciktisi).
    splits: (ex_date, carpan) ciftleri; yalnizca (made_at, end_date] araligindakiler kullanilir.
    Getiriler bolunme duzeltmeli, TEMETTU icermez. Nedeni gormek icin `explain_outcome`.
    """
    return _evaluate(made_at, end_date, ticker_bars, xu_bars, calendar, label_threshold, splits, as_of)[0]


def explain_outcome(
    made_at, end_date, ticker_bars, xu_bars, calendar, *, label_threshold: float = 0.0, splits: Iterable = (),
    as_of=None,
) -> tuple[Outcome | None, str | None, str | None]:
    """compute_outcome ile ayni, ek olarak (atlama_nedeni, ayrinti) dondurur."""
    return _evaluate(made_at, end_date, ticker_bars, xu_bars, calendar, label_threshold, splits, as_of)


_UPDATE_SQL = (
    "UPDATE predictions SET resolved_at = %s, realized_ret = %s, realized_rel_ret = %s, tradable_ret = %s,"
    " label_up = %s, label_rel_up = %s WHERE id = %s AND resolved_at IS NULL"
)


def resolve_due(
    conn: psycopg.Connection,
    calendar: TradingCalendar,
    *,
    label_threshold: float = 0.0,
    dry_run: bool = False,
    as_of: dt.date | None = None,
) -> ResolveResult:
    """Cozulmemis tahminlerden vadesi gelenleri cozer; yalniz cozum sutunlarini UPDATE eder.

    - end_date = calendar.horizon_end(made_at, satirin horizon_days'i).
    - `as_of` verilirse end_date > as_of olanlar atlanir ('not_due').
    - Zaten cozulmus satirlar sorguya girmez; UPDATE ayrica `resolved_at IS NULL` ister (ikinci calistirma 0).
    - Tek satir/ticker hatasi isi dusurmez: 'skipped' icine yazilir. dry_run hicbir sey yazmaz, sayilari hesaplar.
    - Ayni (ticker, made_at, horizon) icin sonuc bir kez hesaplanir; model satirlarina ayni sonuc yazilir.
    - Transaction yonetimi cagirana aittir: autocommit olmayan baglantida commit cagiran yapar.
    """
    result = ResolveResult(dry_run=dry_run)
    rows = conn.execute(
        "SELECT id, ticker, made_at, horizon_days FROM predictions WHERE resolved_at IS NULL ORDER BY id"
    ).fetchall()
    if not rows:
        return result

    # Yalniz as_of'a kadar olan veri (gelecek bar sizintisi olmasin); en eski made_at'ten itibaren.
    tickers = {r[1] for r in rows} | {XU100}
    since = min(r[2] for r in rows)
    prices = read_prices(conn, tickers, since=since, until=as_of)
    xu = prices.get(XU100)
    splits_by_ticker: dict[str, list[tuple[dt.date, float]]] = {}
    for t, d, v in conn.execute(
        "SELECT ticker, ex_date, value FROM corporate_actions WHERE kind = 'split' AND ex_date >= %s AND ticker = ANY(%s)",
        (since, sorted(tickers)),
    ).fetchall():
        splits_by_ticker.setdefault(t, []).append((d, float(v)))

    cache: dict[tuple, tuple[Outcome | None, str | None, str | None]] = {}
    for pid, ticker, made_at, horizon in rows:
        label = f"id={pid} {ticker} {made_at} h={horizon}"
        try:
            key = (ticker, made_at, horizon)
            if key not in cache:
                cache[key] = _outcome_for(ticker, made_at, horizon, prices, xu, calendar, label_threshold,
                                          splits_by_ticker.get(ticker, ()), as_of)
            out, reason, detail = cache[key]
            if out is None:
                result.skip(reason, f"{label}: {detail}")
                continue
            if dry_run:
                result.resolved += 1
                continue
            with conn.transaction():
                cur = conn.execute(_UPDATE_SQL, (out.resolved_at, out.realized_ret, out.realized_rel_ret,
                                                 out.tradable_ret, out.label_up, out.label_rel_up, pid))
                updated = cur.rowcount
            if updated == 1:
                result.resolved += 1
            else:
                result.skip("already_resolved", label)
        except Exception as exc:  # tek satir isi dusurmesin
            result.skip("error", f"{label}: {type(exc).__name__}: {exc}")
    return result


def _outcome_for(ticker, made_at, horizon, prices, xu, calendar, label_threshold, splits, as_of):
    if ticker not in prices:
        return None, "no_prices", f"{ticker} icin fiyat yok"
    if xu is None:
        return None, "no_prices", "XU100 icin fiyat yok"
    try:
        end = calendar.horizon_end(made_at, horizon)
    except ValueError as exc:
        return None, "invalid_row", str(exc)
    if end is None:
        return None, "horizon_unknown", f"takvim {calendar.last_day.date()} oncesi; vade bilinemiyor"
    if as_of is not None and end.date() > pd.Timestamp(as_of).date():
        return None, "not_due", f"end_date {end.date()} > as_of"
    return _evaluate(made_at, end, prices[ticker], xu, calendar, label_threshold, splits, as_of)


def count_overdue(conn: psycopg.Connection, calendar: TradingCalendar, *, grace_trading_days: int = 3) -> dict:
    """Vadesi `grace_trading_days` islem gunu ya da daha fazla gecmis AMA hala cozulmemis tahminleri sayar.

    Donus: {"overdue": satir sayisi, "oldest_made_at": en eski made_at ya da None,
            "tickers": etkilenen ticker'lar (sirali, en fazla MAX_OVERDUE_TICKERS)}.
    Gecikme = (end_date, takvim son gunu] araligindaki islem gunu sayisi. Vadesi hesaplanamayan satirlar
    (takvim yetmiyor / made_at takvimde degil) sayilmaz. Sorgu (made_at, horizon_days) gruplari uzerinden calisir."""
    rows = conn.execute(
        "SELECT made_at, horizon_days, count(*), array_agg(DISTINCT ticker) FROM predictions"
        " WHERE resolved_at IS NULL GROUP BY made_at, horizon_days"
    ).fetchall()
    overdue = 0
    oldest: dt.date | None = None
    tickers: set[str] = set()
    for made_at, horizon, n, group_tickers in rows:
        try:
            end = calendar.horizon_end(made_at, horizon)
        except ValueError:
            continue
        if end is None:
            continue
        late = calendar.trading_days_between(end, calendar.last_day)
        if late is None or late < grace_trading_days:
            continue
        overdue += int(n)
        tickers.update(group_tickers)
        if oldest is None or made_at < oldest:
            oldest = made_at
    return {"overdue": overdue, "oldest_made_at": oldest, "tickers": sorted(tickers)[:MAX_OVERDUE_TICKERS]}
