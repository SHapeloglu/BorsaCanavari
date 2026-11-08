"""Indirme: kaynak -> (dogrulama) -> depolama (PROJE.md Bolum 4, 12 adim 1-2).

- Tek hissenin hatasi butun isi dusurmez; hatalar sonuca yazilir.
- Art arda istek atilmaz: istekler arasi kisa bekleme; gecici hatada sinirli yeniden deneme.
- `validator(ticker, df)` verilirse, donen nesnenin `usable` ozelligi False ise o hisse YAZILMAZ ve
  "dislanan" olarak loglanir. Validator yoksa hepsi yazilir.
- Takvimin kaynagi XU100 bar gunleridir; XU100 barlari ayrica `prices`a ticker='XU100' olarak yazilir
  (ozellikler ve cozucu XU100 acilis/kapanisina ihtiyac duyar).
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

import pandas as pd

from . import store
from .quality import check_bars, drop_incomplete_today_bar

log = logging.getLogger(__name__)

CALENDAR_SYMBOL = "XU100"


@dataclass
class FetchResult:
    dry_run: bool = False
    written: dict[str, int] = field(default_factory=dict)       # ticker -> degisen fiyat satiri
    fetched_rows: dict[str, int] = field(default_factory=dict)  # ticker -> kaynaktan gelen satir
    actions_written: dict[str, int] = field(default_factory=dict)
    excluded: dict[str, str] = field(default_factory=dict)      # ticker -> validator gerekcesi
    errors: dict[str, str] = field(default_factory=dict)        # anahtar -> hata mesaji
    flags: dict[str, list[str]] = field(default_factory=dict)   # ticker -> kalite bayraklari (yazilir, silinmez)
    calendar_rows: int = 0                                      # XU100'den gelen gun sayisi
    calendar_written: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary_lines(self) -> list[str]:
        verb = "yazilacak (kuru calisma)" if self.dry_run else "yazildi"
        lines = [f"{t}: kaynaktan {self.fetched_rows[t]} satir; {verb}: {self.written.get(t, 0)}"
                 for t in sorted(self.fetched_rows) if t not in self.excluded]
        lines += [f"BAYRAK {t}: {f}" for t, fl in sorted(self.flags.items()) for f in fl]
        lines += [f"DISLANAN {t}: {why}" for t, why in sorted(self.excluded.items())]
        lines += [f"HATA {k}: {msg}" for k, msg in sorted(self.errors.items())]
        lines.append(f"takvim: {self.calendar_rows} XU100 gunu; {verb}: {self.calendar_written}")
        return lines


def _with_retry(fn: Callable[[], object], what: str, retries: int, pause: float, sleep: Callable[[float], None]):
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as exc:  # kaynak hatalari cesitli; sinirli yeniden deneme
            last = exc
            log.warning("%s basarisiz (deneme %d/%d): %s", what, attempt, retries, exc)
            if attempt < retries:
                sleep(pause * attempt)
    assert last is not None
    raise last


def fetch_all(
    source,
    conn,
    tickers: Iterable[str],
    since: str | dt.date,
    *,
    dry_run: bool = False,
    validator: Callable[[str, pd.DataFrame], object] | None = None,
    use_quality: bool = True,
    drop_incomplete_today: bool = True,
    now: dt.datetime | None = None,
    end: str | None = None,
    retries: int = 3,
    pause: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> FetchResult:
    """`tickers` icin fiyat + kurumsal islem, ayrica XU100 (fiyat + takvim) indirir ve yazar.

    `now`: fetched_at damgasi ve "bugunun bari tam mi" karari icin saat (varsayilan simdi, UTC).
    `since`: 'YYYY-MM-DD' ya da date. dry_run=True: kaynaga okuma yapilir, DB'ye HICBIR sey yazilmaz.

    Sira: once XU100 (takvim kaynagi), sonra hisseler; boylece kalite kontrolu eksik islem gunlerini
    gercek takvimle karsilastirir. `drop_incomplete_today=True` iken kapanis penceresinden (data/quality.py,
    18:30 Istanbul) once gelen bugunun yarim bari ne `prices`a ne `trading_calendar`a yazilir.
    `validator` verilirse o kullanilir; yoksa `use_quality` iken `check_bars` calisir. Hata ("error")
    veren hisse yazilmaz; bayraklar ("flag") yazilir ve `res.flags`e eklenir, hicbir bar silinmez/duzeltilmez.
    """
    clock = now or dt.datetime.now(dt.timezone.utc)
    fetched_at = clock
    start = since.isoformat() if isinstance(since, dt.date) else str(since)
    res = FetchResult(dry_run=dry_run)
    src_name = getattr(source, "name", "borsapy")

    def pace() -> None:
        if pause > 0:
            sleep(pause)

    # Takvim kaynagi: XU100 bar gunleri (once cekilir; kalite kontrolu bunu kullanir)
    trading_days = None
    try:
        idx = _with_retry(lambda: source.index_history(CALENDAR_SYMBOL, start, end), f"{CALENDAR_SYMBOL} history",
                          retries, pause, sleep)
        if idx.empty:
            raise RuntimeError("XU100 bos veri dondurdu; takvim guncellenmedi")
        if drop_incomplete_today:
            idx = drop_incomplete_today_bar(idx, clock)
        res.calendar_rows = len(idx)
        trading_days = list(idx.index)
        if not dry_run:
            store.upsert_prices(conn, CALENDAR_SYMBOL, idx, src_name, fetched_at)
            res.calendar_written = store.upsert_trading_calendar(conn, idx.index)
        else:
            res.calendar_written = len(idx)
    except Exception as exc:
        res.errors[CALENDAR_SYMBOL] = f"{type(exc).__name__}: {exc}"
        log.error("%s hatasi: %s", CALENDAR_SYMBOL, exc)
    pace()

    for ticker in sorted({t.strip().upper() for t in tickers}):
        try:
            df = _with_retry(lambda t=ticker: source.history(t, start, end), f"{ticker} history",
                             retries, pause, sleep)
            if drop_incomplete_today:
                df = drop_incomplete_today_bar(df, clock)
            res.fetched_rows[ticker] = len(df)
            if df.empty:
                raise RuntimeError("kaynak bos veri dondurdu")
            verdict = None
            if validator is not None:
                verdict = validator(ticker, df)
            elif use_quality:
                verdict = check_bars(df, ticker, trading_days=trading_days, now=clock)
            if verdict is not None:
                flags = [f"{i.code}: {i.detail}" for i in getattr(verdict, "issues", []) if i.severity != "error"]
                if flags:
                    res.flags[ticker] = flags
                if not bool(getattr(verdict, "usable", True)):
                    reason = str(getattr(verdict, "summary", None) or verdict)
                    res.excluded[ticker] = reason[:500]
                    log.warning("%s DISLANDI (kalite): %s", ticker, reason)
                    continue
            if not dry_run:
                res.written[ticker] = store.upsert_prices(conn, ticker, df, src_name, fetched_at)
            else:
                res.written[ticker] = len(df)
            pace()
            actions = _with_retry(lambda t=ticker: source.corporate_actions(t), f"{ticker} actions",
                                  retries, pause, sleep)
            if not dry_run:
                res.actions_written[ticker] = store.upsert_corporate_actions(conn, ticker, actions, fetched_at)
            else:
                res.actions_written[ticker] = len(actions)
        except Exception as exc:  # tek hisse tum isi dusurmesin
            res.errors[ticker] = f"{type(exc).__name__}: {exc}"
            log.error("%s hatasi: %s", ticker, exc)
        pace()
    return res
