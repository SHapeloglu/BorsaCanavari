"""Tarihli evren uyeligi (PROJE.md Bolum 3, 4, 6): XU030 bilesenleri -> universe_membership.

Aralik kurali (belgelenmis, tutarli): YARI ACIK aralik [valid_from, valid_to).
- valid_from: hissenin uye olarak ILK GOZLENDIGI senkronizasyon gunu (dahil).
- valid_to  : hissenin uye OLMADIGI ILK GOZLENEN senkronizasyon gunu (dahil degil). NULL = hala uye.
- Gun d'de uyelik: valid_from <= d AND (valid_to IS NULL OR d < valid_to)  (bkz. `members_on`).
Bu secim, ayni gun eklenip cikan hisseyi bos aralik ([d, d)) yapar ve semadaki
CHECK (valid_to >= valid_from) ile uyumludur; "bir onceki gun" kuralinin valid_to < valid_from uretme
riskini tasimaz.

GECMIS UYDURULMAZ: ilk senkronizasyonda mevcut bilesenlerin valid_from'u `as_of`tir. Daha eski
degisiklikler (ornegin 2019-2026 ceyreklik degisiklikler) bilinmez; geriye donuk evren icin KAP
duyurularindan elle yuklenmesi gerekir. Bu yuzden DB'deki uyelik gecmisi yalnizca ilk senkronizasyondan
itibaren guvenilirdir.
"""

from __future__ import annotations

import csv
import datetime as dt
import logging
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger(__name__)

INDEX_NAME = "BIST30"
SOURCE_SYMBOL = "XU030"
EXPECTED_SIZE = 30


class UniverseError(RuntimeError):
    """Evren senkronizasyonu guvenli degil (bos/anlamsiz bilesen listesi vb.)."""


class Membership(NamedTuple):
    ticker: str
    valid_from: dt.date
    valid_to: dt.date | None = None


def _as_membership(row) -> Membership:
    if isinstance(row, Membership):
        return row
    if isinstance(row, dict):
        return Membership(row["ticker"], row["valid_from"], row.get("valid_to"))
    items = tuple(row)
    return Membership(items[0], items[1], items[2] if len(items) > 2 else None)


def diff_membership(
    current_rows: Iterable, fetched_symbols: Iterable[str], as_of: dt.date
) -> tuple[list[Membership], list[Membership]]:
    """Saf fonksiyon. `current_rows`: DB'deki satirlar (Membership, dict ya da (ticker, valid_from, valid_to)).

    Doner: (eklenecekler, kapatilacaklar).
    - eklenecek : fetched'te olup DB'de ACIK (valid_to NULL) satiri olmayan ticker -> Membership(t, as_of, None)
    - kapatilacak: DB'de acik olup fetched'te olmayan ticker -> Membership(t, valid_from, as_of)
    Degisiklik yoksa iki liste de bos. Kapali (valid_to dolu) satirlar dikkate alinmaz; bir hisse
    cikip geri gelirse yeni satir acilir. Cikti ticker'a gore sirali.
    """
    fetched = {str(s).strip().upper() for s in fetched_symbols if str(s).strip()}
    open_rows = {m.ticker: m for m in map(_as_membership, current_rows) if m.valid_to is None}
    to_add = [Membership(t, as_of, None) for t in sorted(fetched - open_rows.keys())]
    to_close = [Membership(t, open_rows[t].valid_from, max(as_of, open_rows[t].valid_from)) for t in sorted(open_rows.keys() - fetched)]
    return to_add, to_close


def members_on(rows: Iterable, day: dt.date) -> list[str]:
    """Yari acik aralik kuralina gore `day` gunundeki uyeler (sirali)."""
    out = []
    for m in map(_as_membership, rows):
        if m.valid_from <= day and (m.valid_to is None or day < m.valid_to):
            out.append(m.ticker)
    return sorted(set(out))


def fetch_components(source, symbol: str = SOURCE_SYMBOL) -> list[str]:
    """Kaynaktan guncel bilesenleri alir ve makul mu diye bakar (bos liste = hata)."""
    symbols = sorted({str(s).strip().upper() for s in source.index_components(symbol) if str(s).strip()})
    if not symbols:
        raise UniverseError(f"{symbol} bilesen listesi bos; evren degistirilmedi")
    if len(symbols) != EXPECTED_SIZE:
        log.warning("%s bilesen sayisi %d (beklenen %d); KAP duyurusuyla kontrol edin", symbol, len(symbols),
                    EXPECTED_SIZE)
    return symbols


def open_tickers(rows: Sequence) -> list[str]:
    return sorted(m.ticker for m in map(_as_membership, rows) if m.valid_to is None)


# --- Gecmis uyelik yukleme (Faz 3.5 on kosulu) ------------------------------------------------------------

HISTORY_CSV = Path("reference/bist30/membership_history.csv")


class HistoryConflict(UniverseError):
    """Yuklenecek gecmis, DB'deki mevcut uyelikle celisiyor (otomatik cozulmez)."""


def read_history_csv(path: str | Path = HISTORY_CSV) -> list[Membership]:
    """tools/build_membership_history.py ciktisi: ticker,valid_from,valid_to,confidence (yari acik aralik)."""
    out: list[Membership] = []
    with Path(path).open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            out.append(Membership(r["ticker"].strip().upper(), dt.date.fromisoformat(r["valid_from"]),
                                  dt.date.fromisoformat(r["valid_to"]) if r["valid_to"].strip() else None))
    return out


def _overlaps(a: Membership, b: Membership) -> bool:
    a_end = a.valid_to or dt.date.max
    b_end = b.valid_to or dt.date.max
    return a.valid_from < b_end and b.valid_from < a_end


def plan_history_load(existing: Iterable, target: Iterable) -> tuple[list[Membership], list[tuple[Membership, dt.date]]]:
    """Saf fonksiyon: (eklenecekler, [(mevcut acik satir, yeni valid_from)]).

    Kural (gecmis YALNIZ geriye dogru uzatilir, mevcut gozlemler bozulmaz):
    - Mevcut acik satir (valid_to NULL) + hedefte acik aralik: mevcut satirin valid_from'u hedefinkine CEKILIR
      (yalniz daha erkene; daha gec ise celiski). Hedefin kapali araliklari eklenir.
    - Mevcutta hic satir yoksa hedefin hepsi eklenir.
    - Zaten ayni satir varsa atlanir (idempotent).
    - Sonuc uyelik, ayni hisse icin cakisan aralik icerirse HistoryConflict.
    """
    ex_by: dict[str, list[Membership]] = {}
    for m in map(_as_membership, existing):
        ex_by.setdefault(m.ticker, []).append(m)
    tg_by: dict[str, list[Membership]] = {}
    for m in map(_as_membership, target):
        tg_by.setdefault(m.ticker, []).append(m)
    inserts: list[Membership] = []
    updates: list[tuple[Membership, dt.date]] = []
    for t, tgs in sorted(tg_by.items()):
        exs = ex_by.get(t, [])
        ex_open = [m for m in exs if m.valid_to is None]
        tg_open = [m for m in tgs if m.valid_to is None]
        final: list[Membership] = [m for m in exs]
        if ex_open and tg_open:
            eo, to = ex_open[0], tg_open[0]
            if to.valid_from > eo.valid_from:
                raise HistoryConflict(f"{t}: hedef acik aralik {to.valid_from} mevcut {eo.valid_from}'den SONRA basliyor")
            if to.valid_from < eo.valid_from:
                updates.append((eo, to.valid_from))
                final[final.index(eo)] = Membership(t, to.valid_from, None)
        elif tg_open and not ex_open and exs:
            raise HistoryConflict(f"{t}: hedefte acik aralik var ama mevcutta yalnizca kapali satirlar var")
        for m in tgs:
            if m.valid_to is None and (ex_open or any(e.valid_to is None for e in exs)):
                continue  # acik aralik yukaridaki guncellemeyle ele alindi
            if m in exs:
                continue
            inserts.append(m)
            final.append(m)
        for i, a in enumerate(final):
            for b in final[i + 1:]:
                if _overlaps(a, b):
                    raise HistoryConflict(f"{t}: cakisan uyelik araliklari {a} / {b}")
    return inserts, updates
