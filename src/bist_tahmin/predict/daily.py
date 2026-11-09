"""Gunluk tahmin uretimi (PROJE.md Bolum 12, adim 3-4): ozellikler + baseline'lar -> tahmin defteri.

Baseline'lar (model_role='baseline') HER ZAMAN once yazilir. Sonra (Faz 4) aktif modeller (champion/challenger,
`models.registry.load_active_models`) AYRI bir blokta, AYRI write_predictions cagrilariyla calisir: bir modelin hatasi
(ValueError, eksik/NaN ozellik, bozuk parametre...) yalniz o modeli atlatir, baseline'lari ve diger modelleri etkilemez
(`DailyResult.model_errors`). Devre kesici aciksa model satiri URETILMEZ ama baseline'lar yazilir (baseline'lar olcum
kontroludur, model iddiasi degildir); neden `models_paused`ta. Yazim penceresi (LateWrite) modeller icin de gecerlidir.
Ozellik uretimi evreni
belirler: ozellik hesaplanabilen (yeterli gecmis, made_at'te bar var) hisseler "degerlendirme evreni"dir ve
TUM baseline'lar ayni evrene yazilir; boylece modeller/baseline'lar ayni (hisse, gun) kumeleri uzerinde
karsilastirilir.

Defter ileriye donuk bir kanittir: varsayilan olarak yalnizca takvimin SON gunu icin tahmin yazilir.
Gecmis bir gun icin yazmak (`allow_backfill=True`) yalnizca gelistirme/test icindir; uretim defterinde kullanilmaz.
"""

from __future__ import annotations

import datetime as dt
import math
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
from ..monitor import breaker
from .ledger import PredictionRow, baseline_feature_hash, write_predictions

INDEX_SYMBOL = "XU100"
MIN_HISTORY_DAYS = 60


class LateWrite(ValueError):
    """Tahmin, made_at'i izleyen seansin acilisindan sonra yazilmak isteniyor (ileriye donuk kanit olmaz).

    Modeller icin firlatildiginda baseline'lar zaten yazilmistir; o ana kadarki sonuc `exc.result` (DailyResult)."""

    result: DailyResult | None = None


# --- models paketi sozlesmesi (T): import GEC yapilir ---------------------------------------------------------------
# `models` paketi yoksa (Faz 4 oncesi / birlestirme oncesi) aktif model yok sayilir; baska her hata gorunur olur.
# Testler bu iki ismi `bist_tahmin.predict.daily` uzerinde ya da kaynak modulde (models.registry / models.logreg)
# degistirebilir: ikisi de cagri aninda cozulur.

def load_active_models(conn: psycopg.Connection) -> list:
    """models.registry.load_active_models(conn) -> [ModelRecord(version, role, trained_at, train_end, params)]."""
    try:
        from ..models import registry
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("bist_tahmin.models"):
            return []
        raise
    return list(registry.load_active_models(conn))


def predict_prob(params: dict, features) -> float:
    """models.logreg.predict_prob(params, features) -> float (eksik/NaN ozellik ValueError)."""
    from ..models import logreg

    return logreg.predict_prob(params, features)


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
    rows: list[PredictionRow] = field(default_factory=list)            # yalniz baseline satirlari
    # Faz 4 (modeller). `inserted`/`skipped_existing` yalniz baseline'lari sayar.
    model_rows: list[PredictionRow] = field(default_factory=list)
    models_written: int = 0                                           # yazilan YENI model satiri
    models_skipped_existing: int = 0
    models_ok: list[str] = field(default_factory=list)                # satiri uretilip yazilan model surumleri
    model_errors: dict[str, str] = field(default_factory=dict)        # surum -> "Tip: mesaj" (atlanan model)
    models_paused: str | None = None                                  # devre kesici acik ise nedeni

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
        if self.models_paused:
            lines.append(f"MODELLER DURDURULDU: {self.models_paused}")
        elif self.models_ok or self.model_errors:
            lines.append(f"model satiri {verb}: {self.models_written}; zaten vardi: {self.models_skipped_existing}; "
                         f"modeller: {', '.join(self.models_ok) or '-'}")
        lines += [f"MODEL ATLANDI {v}: {why}" for v, why in sorted(self.model_errors.items())]
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

    def check_window(candidate: list[PredictionRow]) -> None:
        if not allow_backfill and when > deadline:
            probe = write_predictions(conn, candidate, dry_run=True)
            if probe.inserted > 0:
                raise LateWrite(
                    f"made_at={day.date()} icin yazim penceresi kapandi (son: {deadline:%Y-%m-%d %H:%M} Istanbul, "
                    f"simdi: {when.tz_convert('Europe/Istanbul'):%Y-%m-%d %H:%M}); {probe.inserted} yeni satir yazilmadi"
                )

    check_window(rows)
    wr = write_predictions(conn, rows, dry_run=dry_run, created_at=created_at)
    res.inserted, res.skipped_existing = wr.inserted, wr.skipped_existing

    # --- Faz 4: modeller (baseline'lar yukarida yazildi; buradaki hicbir hata onlari etkileyemez) ---------------
    _run_models(conn, cfg, fs, res, day=day, horizon=horizon, clip=clip, dry_run=dry_run,
                created_at=created_at, check_window=check_window)
    return res


def _models_pause_reason(conn: psycopg.Connection, res: DailyResult) -> str | None:
    """Devre kesici aciksa nedeni; durum OKUNAMAZSA da durdurur (belirsizlikte model iddiasi uretilmez)."""
    try:
        with conn.transaction():
            st = breaker.status(conn)
    except Exception as exc:
        res.model_errors["circuit_breaker"] = f"{type(exc).__name__}: {exc}"[:300]
        return f"devre kesici durumu okunamadi ({type(exc).__name__}); modeller guvenli tarafta durduruldu"
    if not st.get("tripped"):
        return None
    return (f"devre kesici ACIK (since {st.get('since')}, model {st.get('model_version')}): {st.get('reason')}; "
            "yalnizca `bist breaker reset --reason ...` ile kapanir")


def _run_models(conn, cfg, fs, res: DailyResult, *, day, horizon, clip, dry_run, created_at, check_window) -> None:
    paused = _models_pause_reason(conn, res)
    if paused:
        res.models_paused = paused
        return
    try:
        with conn.transaction():
            records = load_active_models(conn)
    except Exception as exc:  # kayit defteri okunamadi: model yok, ama baseline'lar yazildi
        res.model_errors["registry"] = f"{type(exc).__name__}: {exc}"[:300]
        return
    # Once TUM modellerin satirlari uretilir (saf hesap), sonra yazilir; boylece model hatasi yazimi yarida kesmez.
    for rec in records:
        version, role = str(rec.version), str(rec.role)
        try:
            if role not in ("champion", "challenger"):
                raise ValueError(f"model rolu champion/challenger olmali: {role!r}")
            mrows: list[PredictionRow] = []
            for t in res.evaluated:
                fr = fs.rows[t]
                p = float(predict_prob(rec.params, fr.features))
                if not math.isfinite(p):
                    raise ValueError(f"{t}: model olasiligi sonlu degil")
                # prob_up kirpilir (Bolum 3: prob_clip); score KIRPILMAMIS olasiliktir: kirpma esitlik uretip
                # kohort siralamasini bozmasin.
                mrows.append(PredictionRow(
                    ticker=t, made_at=day.date(), horizon_days=horizon, prob_up=min(max(p, clip[0]), clip[1]),
                    score=p, model_version=version, model_role=role, feature_hash=fr.feature_hash,
                    features_json=fr.features_json,
                ))
        except Exception as exc:
            res.model_errors[version] = f"{type(exc).__name__}: {exc}"[:300]
            continue
        if not mrows:
            res.model_errors[version] = "degerlendirilen hisse yok"
            continue
        try:
            check_window(mrows)
        except LateWrite as exc:
            exc.result = res
            raise
        try:
            wr = write_predictions(conn, mrows, dry_run=dry_run, created_at=created_at)
        except Exception as exc:
            res.model_errors[version] = f"yazim hatasi {type(exc).__name__}: {exc}"[:300]
            continue
        res.model_rows += mrows
        res.models_ok.append(version)
        res.models_written += wr.inserted
        res.models_skipped_existing += wr.skipped_existing
