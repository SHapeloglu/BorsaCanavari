"""Depolama: bist_app yetkileriyle idempotent upsert'ler (PROJE.md Bolum 6; 003_grants.sql).

bist_app bu tablolarda SELECT/INSERT/UPDATE yapabilir, DELETE yapamaz; bu yuzden her yazma
`INSERT ... ON CONFLICT DO UPDATE`dir ve ayni veriyle tekrar calistirmak satir sayisini degistirmez.
Her fonksiyon kendi islemini (`conn.transaction()`) acar; autocommit baglantida da atomiktir.
Fonksiyonlar, degisen (eklenen ya da guncellenen) satir sayisini dondurur.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

import numpy as np
import pandas as pd
import psycopg

from .universe import INDEX_NAME, Membership, diff_membership, plan_history_load

PRICE_COLUMNS = ["open", "high", "low", "close", "close_adj", "volume"]


def _clean(v):
    """NaN/NaT -> None; numpy skalerleri -> Python."""
    if v is None:
        return None
    if isinstance(v, float) and np.isnan(v):
        return None
    if isinstance(v, np.generic):
        v = v.item()
        if isinstance(v, float) and np.isnan(v):
            return None
    return v


def _executemany(conn: psycopg.Connection, query: str, rows: list[tuple]) -> int:
    if not rows:
        return 0
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany(query, rows)
            # executemany rowcount'u toplam etkilenen satirdir (DO UPDATE ... WHERE ile degismeyenler sayilmaz).
            return max(cur.rowcount, 0)


_UPSERT_PRICES = """
INSERT INTO prices (ticker, date, open, high, low, close, close_adj, volume, source, fetched_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (ticker, date) DO UPDATE SET
    open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low, close = EXCLUDED.close,
    close_adj = EXCLUDED.close_adj, volume = EXCLUDED.volume,
    source = EXCLUDED.source, fetched_at = EXCLUDED.fetched_at
WHERE (prices.open, prices.high, prices.low, prices.close, prices.close_adj, prices.volume, prices.source)
      IS DISTINCT FROM
      (EXCLUDED.open, EXCLUDED.high, EXCLUDED.low, EXCLUDED.close, EXCLUDED.close_adj, EXCLUDED.volume,
       EXCLUDED.source)
"""


def upsert_prices(
    conn: psycopg.Connection, ticker: str, df: pd.DataFrame, source: str, fetched_at: dt.datetime
) -> int:
    """`df`: source.py sozlesmesi (date indeksi + kucuk harfli kolonlar). Veri degismediyse satira dokunmaz
    (fetched_at yalnizca veri degisince ya da yeni satirda guncellenir)."""
    if fetched_at.tzinfo is None:
        raise ValueError("fetched_at tz'li olmali")
    rows = []
    for ts, r in df.iterrows():
        rows.append(
            (ticker, pd.Timestamp(ts).date(), *(_clean(r.get(c)) for c in
                                                  ("open", "high", "low", "close", "close_adj", "volume")),
             source, fetched_at)
        )
    return _executemany(conn, _UPSERT_PRICES, rows)


_UPSERT_ACTIONS = """
INSERT INTO corporate_actions (ticker, ex_date, kind, value, fetched_at)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (ticker, ex_date, kind) DO UPDATE SET value = EXCLUDED.value, fetched_at = EXCLUDED.fetched_at
WHERE corporate_actions.value IS DISTINCT FROM EXCLUDED.value
"""


def upsert_corporate_actions(
    conn: psycopg.Connection, ticker: str, df: pd.DataFrame, fetched_at: dt.datetime
) -> int:
    """`df` kolonlari: ex_date, kind ('split'|'dividend'), value."""
    if fetched_at.tzinfo is None:
        raise ValueError("fetched_at tz'li olmali")
    rows = [
        (ticker, pd.Timestamp(r.ex_date).date(), r.kind, float(r.value), fetched_at)
        for r in df.itertuples(index=False)
    ]
    return _executemany(conn, _UPSERT_ACTIONS, rows)


def upsert_trading_calendar(
    conn: psycopg.Connection, dates: Iterable, half_days: Iterable = ()
) -> int:
    """Takvim gunlerini ekler. Var olan satirin is_half_day bayragi bu cagriyla ASLA temizlenmez
    (yalnizca FALSE -> TRUE olabilir); yarim gun bilgisi calendar.py'den gelir."""
    half = {pd.Timestamp(d).date() for d in half_days}
    rows = sorted({(pd.Timestamp(d).date(), pd.Timestamp(d).date() in half) for d in dates})
    return _executemany(
        conn,
        "INSERT INTO trading_calendar (date, is_half_day) VALUES (%s, %s) "
        "ON CONFLICT (date) DO UPDATE SET is_half_day = EXCLUDED.is_half_day "
        "WHERE NOT trading_calendar.is_half_day AND EXCLUDED.is_half_day",
        rows,
    )


def read_membership(conn: psycopg.Connection, index_name: str = INDEX_NAME) -> list[Membership]:
    cur = conn.execute(
        "SELECT ticker, valid_from, valid_to FROM universe_membership WHERE index_name = %s "
        "ORDER BY ticker, valid_from",
        (index_name,),
    )
    return [Membership(*r) for r in cur.fetchall()]


def apply_membership_changes(
    conn: psycopg.Connection, to_add: list[Membership], to_close: list[Membership], index_name: str = INDEX_NAME
) -> None:
    with conn.transaction():
        for m in to_close:
            conn.execute(
                "UPDATE universe_membership SET valid_to = %s "
                "WHERE ticker = %s AND index_name = %s AND valid_from = %s AND valid_to IS NULL",
                (m.valid_to, m.ticker, index_name, m.valid_from),
            )
        for m in to_add:
            conn.execute(
                "INSERT INTO universe_membership (ticker, index_name, valid_from, valid_to) "
                "VALUES (%s, %s, %s, NULL) ON CONFLICT (ticker, index_name, valid_from) DO NOTHING",
                (m.ticker, index_name, m.valid_from),
            )


def sync_universe(
    conn: psycopg.Connection,
    fetched_symbols: Iterable[str],
    as_of: dt.date,
    *,
    index_name: str = INDEX_NAME,
    dry_run: bool = False,
) -> tuple[list[Membership], list[Membership]]:
    """DB'deki uyeligi kaynak bilesenleriyle esler. (eklenen, kapatilan) doner; dry_run yazmaz.
    Idempotent: ayni bilesenlerle ikinci cagri (ya da ayni gun tekrar) hicbir sey degistirmez."""
    fetched = list(fetched_symbols)
    if not fetched:
        raise ValueError("bos bilesen listesiyle evren senkronize edilmez")
    to_add, to_close = diff_membership(read_membership(conn, index_name), fetched, as_of)
    if not dry_run and (to_add or to_close):
        apply_membership_changes(conn, to_add, to_close, index_name)
    return to_add, to_close


# --- Okuma (Faz 2: ozellikler, baseline'lar, cozucu ortak kullanir) --------------------------------------

READ_COLUMNS = ["open", "high", "low", "close", "close_adj", "open_adj", "volume"]


def read_calendar(conn: psycopg.Connection):
    """`trading_calendar` tablosundan TradingCalendar (XU100 bar gunleri). Bos tablo ValueError verir."""
    from ..calendar import TradingCalendar

    days = [r[0] for r in conn.execute("SELECT date FROM trading_calendar ORDER BY date").fetchall()]
    if not days:
        raise ValueError("trading_calendar bos; once `bist fetch` calistirin")
    return TradingCalendar(days)


def read_prices(
    conn: psycopg.Connection,
    tickers: Iterable[str],
    *,
    until: dt.date | str | pd.Timestamp | None = None,
    since: dt.date | str | pd.Timestamp | None = None,
) -> dict[str, pd.DataFrame]:
    """Her ticker icin gunluk bar DataFrame'i: index = tz-naive `date` (DatetimeIndex), kolonlar READ_COLUMNS.

    `until` DAHILDIR (made_at kapanisina kadar); sizinti kurali icin cagiran bunu mutlaka verir. Verisi olmayan
    ticker sozlukte yer almaz. `close` ham, `close_adj` bolunme duzeltmeli, `open_adj = open*close_adj/close`.
    """
    tk = sorted({t.upper() for t in tickers})
    if not tk:
        return {}
    where, params = ["ticker = ANY(%s)"], [tk]
    if since is not None:
        where.append("date >= %s")
        params.append(pd.Timestamp(since).date())
    if until is not None:
        where.append("date <= %s")
        params.append(pd.Timestamp(until).date())
    cols = ", ".join(READ_COLUMNS)
    cur = conn.execute(f"SELECT ticker, date, {cols} FROM prices WHERE {' AND '.join(where)} ORDER BY ticker, date",
                       params)
    rows = cur.fetchall()
    if not rows:
        return {}
    df = pd.DataFrame(rows, columns=["ticker", "date", *READ_COLUMNS])
    df["date"] = pd.to_datetime(df["date"])
    for c in READ_COLUMNS:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    out: dict[str, pd.DataFrame] = {}
    for t, g in df.groupby("ticker", sort=True):
        out[t] = g.drop(columns="ticker").set_index("date").sort_index()
        out[t].index.name = "date"
    return out


def load_membership_history(
    conn: psycopg.Connection, target: Iterable[Membership], index_name: str = INDEX_NAME, *, dry_run: bool = False
) -> tuple[int, int]:
    """Gecmis uyelik araliklarini yukler (tek islem, idempotent). (eklenen, guncellenen) doner.

    Mevcut acik satirlarin valid_from'u yalnizca daha ERKEN bir tarihe cekilir (bkz. universe.plan_history_load);
    celiski varsa HistoryConflict firlatir ve hicbir sey yazmaz. bist_app'in DELETE yetkisi yoktur: silme yok.
    """
    existing = read_membership(conn, index_name)
    inserts, updates = plan_history_load(existing, target)
    if dry_run:
        return len(inserts), len(updates)
    with conn.transaction():
        for old, new_from in updates:
            conn.execute(
                "UPDATE universe_membership SET valid_from = %s "
                "WHERE ticker = %s AND index_name = %s AND valid_from = %s AND valid_to IS NULL",
                (new_from, old.ticker, index_name, old.valid_from),
            )
        for m in inserts:
            conn.execute(
                "INSERT INTO universe_membership (ticker, index_name, valid_from, valid_to) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (ticker, index_name, valid_from) DO NOTHING",
                (m.ticker, index_name, m.valid_from, m.valid_to),
            )
    return len(inserts), len(updates)
