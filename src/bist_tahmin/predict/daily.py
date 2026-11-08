"""Gunluk tahmin uretimi (PROJE.md Bolum 12, adim 3-4): ozellikler + baseline'lar -> tahmin defteri.

Faz 2'de ML modeli yoktur; yalnizca baseline'lar yazilir (model_role='baseline'). Ozellik uretimi evreni
belirler: ozellik hesaplanabilen (yeterli gecmis, made_at'te bar var) hisseler "degerlendirme evreni"dir ve
TUM baseline'lar ayni evrene yazilir; boylece modeller/baseline'lar ayni (hisse, gun) kumeleri uzerinde
karsilastirilir.

Defter ileriye donuk bir kanittir: varsayilan olarak yalnizca takvimin SON gunu icin tahmin yazilir.
Gecmis bir gun icin yazmak (`allow_backfill=True`) yalnizca gelistirme/test icindir; uretim defterinde kullanilmaz.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pandas as pd
import psycopg

from ..baselines.rules import (
    BASELINES,
    baseline_inputs,
    baseline_model_version,
    baseline_prob,
    historical_base_rate,
)
from ..calendar import next_session_open
from ..data import store
from ..data.universe import members_on
from ..features.build import build_features
from .ledger import PredictionRow, baseline_feature_hash, write_predictions

INDEX_SYMBOL = "XU100"
MIN_HISTORY_DAYS = 60


class LateWrite(ValueError):
    """Tahmin, made_at'i izleyen seansin acilisindan sonra yazilmak isteniyor (ileriye donuk kanit olmaz)."""


@dataclass
class DailyResult:
    made_at: dt.date
    dry_run: bool
    universe: int = 0                      # o gunku uye sayisi
    evaluated: list[str] = field(default_factory=list)   # tahmin yazilan hisseler
    excluded: dict[str, str] = field(default_factory=dict)  # hisse -> gerekce
    base_rate: float | None = None
    inserted: int = 0
    skipped_existing: int = 0
    skipped_baselines: dict[str, int] = field(default_factory=dict)  # baseline -> girdi yetersiz hisse sayisi
    rows: list[PredictionRow] = field(default_factory=list)

    def summary_lines(self) -> list[str]:
        verb = "yazilacak (kuru calisma)" if self.dry_run else "yazildi"
        lines = [
            f"made_at={self.made_at}: evren {self.universe}, degerlendirilen {len(self.evaluated)}, "
            f"dislanan {len(self.excluded)}",
            f"tahmin {verb}: {self.inserted}; zaten vardi: {self.skipped_existing}",
            "geçmiş yukselis orani: " + ("yok (yetersiz ornek)" if self.base_rate is None else f"{self.base_rate:.4f}"),
        ]
        lines += [f"DISLANAN {t}: {why}" for t, why in sorted(self.excluded.items())]
        lines += [f"BASELINE ATLANDI {n}: {c} hisse (girdi yetersiz)" for n, c in sorted(self.skipped_baselines.items())]
        return lines


def run_daily(
    conn: psycopg.Connection,
    cfg: dict,
    *,
    made_at: dt.date | str | pd.Timestamp | None = None,
    dry_run: bool = False,
    allow_backfill: bool = False,
    created_at: dt.datetime | None = None,
    lookback_days: int = 500,
) -> DailyResult:
    cal = store.read_calendar(conn)
    day = cal.last_day if made_at is None else pd.Timestamp(made_at).normalize()
    if not cal.is_trading_day(day):
        raise ValueError(f"{day.date()} takvimde islem gunu degil")
    if day != cal.last_day and not allow_backfill:
        raise ValueError(
            f"made_at={day.date()} takvimin son gunu ({cal.last_day.date()}) degil; defter ileriye donuk bir "
            "kanittir, gecmise tahmin yazilmaz (yalnizca gelistirme icin allow_backfill)"
        )
    horizon = int(cfg["horizon_days"])
    clip = tuple(cfg["prob_clip"])
    label_threshold = float(cfg["label_threshold"])

    members = members_on(store.read_membership(conn), day.date())
    res = DailyResult(made_at=day.date(), dry_run=dry_run, universe=len(members))
    if not members:
        rows_all = store.read_membership(conn)
        if rows_all:
            first = min(m.valid_from for m in rows_all)
            raise ValueError(
                f"{day.date()} tarihinde gecerli uye yok: uyelik kayitlari en erken {first} tarihinden basliyor "
                "(gecmis uyelik uydurulmaz; defter bu tarihten sonraki ilk islem gunuyle baslar)"
            )
        raise ValueError(f"{day.date()} icin evren bos; once `bist universe sync` calistirin")

    since = day - pd.Timedelta(days=lookback_days)
    prices = store.read_prices(conn, members + [INDEX_SYMBOL], until=day, since=since)
    xu = prices.pop(INDEX_SYMBOL, None)
    if xu is None or day not in xu.index:
        raise ValueError(f"XU100 {day.date()} bari yok; veri guncel degil (once `bist fetch`)")
    histories = {t: prices[t] for t in members if t in prices}
    for t in members:
        if t not in histories:
            res.excluded[t] = "no_prices"

    fs = build_features(histories, xu, day, cal, min_history_days=MIN_HISTORY_DAYS)
    res.excluded.update(fs.excluded)
    res.evaluated = sorted(fs.rows)

    eval_hist = {t: histories[t] for t in res.evaluated}
    res.base_rate = historical_base_rate(
        eval_hist, day, cal, horizon, label_threshold=label_threshold
    )

    rows: list[PredictionRow] = []
    for name in BASELINES:
        version = baseline_model_version(name)
        for t in res.evaluated:
            closes = histories[t]["close_adj"]
            p = baseline_prob(name, t, closes, day, base_rate=res.base_rate, clip=clip)
            if p is None:
                res.skipped_baselines[name] = res.skipped_baselines.get(name, 0) + 1
                continue
            inputs = baseline_inputs(name, t, closes, day, base_rate=res.base_rate)
            fjson, fhash = baseline_feature_hash(inputs)
            rows.append(PredictionRow(
                ticker=t, made_at=day.date(), horizon_days=horizon, prob_up=p, score=p,
                model_version=version, model_role="baseline", feature_hash=fhash, features_json=fjson,
            ))
    res.rows = rows
    # Ileriye donuk kanit: YENI satirlar ertesi seans acilmadan yazilmali. Zaten yazilmis satirlar (idempotent
    # tekrar) bu kurala takilmaz. `tradable_ret` ertesi acilis girisini varsayar; gec yazim bu varsayimi bozar.
    when = pd.Timestamp(created_at) if created_at is not None else pd.Timestamp.now(tz="UTC")
    if when.tzinfo is None:
        when = when.tz_localize("Europe/Istanbul")
    deadline = next_session_open(day)
    if not allow_backfill and when > deadline:
        probe = write_predictions(conn, rows, dry_run=True)
        if probe.inserted > 0:
            raise LateWrite(
                f"made_at={day.date()} icin yazim penceresi kapandi (son: {deadline:%Y-%m-%d %H:%M} Istanbul, "
                f"simdi: {when.tz_convert('Europe/Istanbul'):%Y-%m-%d %H:%M}); {probe.inserted} yeni satir yazilmadi"
            )
    wr = write_predictions(conn, rows, dry_run=dry_run, created_at=created_at)
    res.inserted, res.skipped_existing = wr.inserted, wr.skipped_existing
    return res
