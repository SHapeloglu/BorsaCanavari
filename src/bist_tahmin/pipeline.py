"""Gunluk akis (PROJE.md Bolum 12): evren -> indir + kalite -> vadesi gelenleri coz -> bugunun tahminleri.

Cikis kodlari (cron/izleme icin):
  0  tamam
  1  kismi sorun: indirme/kalite hatasi, kapsam dusuklugu, baseline atlandi, cozulemeyen/takili tahmin,
     adjustment_mismatch ya da XU100 takvim deligi suphesi. Akis tamamlandi ama INCELENMELI.
  3  tahmin YAZILMADI: veri bayat ya da yazim penceresi kapandi. Neden: eski bir gune gec yazilan tahmin ileriye
     donuk kanit olmaz (created_at ile made_at arasi acilir); bu yuzden durulur.
  4  (cli) is zaten calisiyor (kilit).
XU100 indirilemezse is basarisiz sayilir (takvim kaynagi odur) ve istisna firlatir.
Her adimin sonucu `runs.detail`e HEMEN yazilir; is sonradan patlasa bile ne yapildigi gorunur.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from .calendar import expected_last_bar_date
from .data import store
from .data.fetch import CALENDAR_SYMBOL, fetch_all
from .data.universe import fetch_components
from .jobs import SKIPPED, checkpoint, tracked_job
from .predict.daily import LateWrite, run_daily
from .resolve.resolver import count_overdue, resolve_due

TZ = ZoneInfo("Europe/Istanbul")
CALENDAR_HOLE_TICKERS = 3  # bu kadar hissede 'XU100'de olmayan gunde bar' bayragi = XU100 deligi suphesi


@dataclass
class PipelineResult:
    exit_code: int = 0
    lines: list[str] = field(default_factory=list)
    steps: dict[str, dict] = field(default_factory=dict)

    def bump(self, code: int) -> None:
        self.exit_code = max(self.exit_code, code)


def run_pipeline(
    conn,
    cfg: dict,
    source,
    *,
    dry_run: bool = False,
    now: dt.datetime | None = None,
    fetch_days: int = 420,
    pause: float = 0.5,
    universe_sync: bool = True,
    universe_min_size: int = 25,
    min_coverage: float = 0.8,
) -> PipelineResult:
    injected = now is not None
    clock = now or dt.datetime.now(dt.timezone.utc)
    today = clock.astimezone(TZ).date()
    out = PipelineResult()
    with tracked_job(conn, "daily", today, record=not dry_run) as run:

        def step(name: str, data: dict) -> None:
            out.steps[name] = data
            run.detail[name] = data
            checkpoint(conn, run)

        # 0) evren (PROJE.md Bolum 12 adim 2): kaynaktaki guncel uyeler; makul boyutta degilse UYGULAMA
        if universe_sync:
            try:
                symbols = fetch_components(source)
                if len(symbols) < universe_min_size:
                    out.bump(1)
                    out.lines.append(f"[evren] UYARI: kaynak {len(symbols)} bilesen verdi (< {universe_min_size}); "
                                     "senkron uygulanmadi")
                    step("universe", {"synced": False, "source_size": len(symbols)})
                else:
                    added, closed = store.sync_universe(conn, symbols, today, dry_run=dry_run)
                    out.lines.append(f"[evren] kaynak {len(symbols)}; eklenen {len(added)}, kapanan {len(closed)}")
                    step("universe", {"synced": True, "source_size": len(symbols),
                                      "added": [m.ticker for m in added], "closed": [m.ticker for m in closed]})
            except Exception as exc:  # evren senkronu basarisiz olursa eldeki uyelikle devam; uyar
                out.bump(1)
                out.lines.append(f"[evren] UYARI: senkron basarisiz: {type(exc).__name__}: {exc}")
                step("universe", {"synced": False, "error": f"{type(exc).__name__}: {exc}"[:300]})

        # 1) indir
        since = today - dt.timedelta(days=fetch_days)
        members = store.read_membership(conn)
        tickers = sorted({m.ticker for m in members if m.valid_to is None or m.valid_to >= since})
        if not tickers:
            raise ValueError("evren bos; once `bist universe sync` calistirin")
        fres = fetch_all(source, conn, tickers, since, dry_run=dry_run, now=clock, pause=pause)
        hole_tickers = sorted(t for t, fl in fres.flags.items()
                              if any(f.startswith("bars_on_non_trading_days") for f in fl))
        step("fetch", {"tickers": len(tickers), "errors": fres.errors, "excluded": sorted(fres.excluded),
                       "flags": {t: len(f) for t, f in fres.flags.items()}, "calendar_hole_tickers": hole_tickers})
        out.lines.append(f"[fetch] {len(tickers)} hisse; hata {len(fres.errors)}, dislanan {len(fres.excluded)}, "
                         f"bayrak {len(fres.flags)}")
        out.lines += [f"[fetch] HATA {k}: {v}" for k, v in sorted(fres.errors.items())]
        if CALENDAR_SYMBOL in fres.errors:
            raise RuntimeError(f"{CALENDAR_SYMBOL} indirilemedi: {fres.errors[CALENDAR_SYMBOL]}")
        if fres.errors or fres.excluded:
            out.bump(1)
        if len(hole_tickers) >= CALENDAR_HOLE_TICKERS:
            out.bump(1)
            out.lines.append(f"[veri] UYARI: {len(hole_tickers)} hissede XU100'de olmayan gunde bar var "
                             "(XU100 takvim deligi suphesi; ufuk sayimi etkilenebilir)")

        # 2) bayat veri kontrolu (takvimden bagimsiz ikinci kaynak: hafta sonu/tatil takvimi)
        cal = store.read_calendar(conn)
        expected = expected_last_bar_date(clock)
        stale = cal.last_day.date() < expected
        step("freshness", {"last_bar": str(cal.last_day.date()), "expected": str(expected), "stale": stale})
        if stale:
            out.lines.append(f"[veri] BAYAT: son bar {cal.last_day.date()}, beklenen {expected}"
                             + ("" if dry_run else "; tahmin yazilmadi"))

        # 3) coz (bayat veriyle de zararsiz: yalnizca eldeki barlarla vadesi gelenleri yazar)
        rres = resolve_due(conn, cal, label_threshold=float(cfg["label_threshold"]), dry_run=dry_run)
        overdue = count_overdue(conn, cal)
        step("resolve", {"resolved": rres.resolved, "skipped": dict(rres.skipped), "details": rres.details[:20],
                         "overdue": overdue})
        out.lines.append(f"[resolve] cozulen {rres.resolved}; atlanan {dict(rres.skipped) or '-'}")
        flagged = {k: v for k, v in rres.skipped.items()
                   if k in ("adjustment_mismatch", "unexplained_jump", "stale_ticker", "error")}
        if flagged:
            out.bump(1)
            out.lines.append(f"[resolve] UYARI: cozulmeyen satirlar {flagged} (ayrinti runs.detail)")
        if overdue["overdue"]:
            out.bump(1)
            out.lines.append(f"[resolve] UYARI: vadesi gecmis ama COZULMEMIS {overdue['overdue']} tahmin "
                             f"(en eski made_at {overdue['oldest_made_at']}; hisseler {overdue['tickers'][:8]}); "
                             "degerlendirmeden sessizce dusuyor, incelenmeli")

        # 4) tahmin (yalnizca veri guncelse ve yazim penceresi aciksa)
        if stale:
            out.bump(3)
        else:
            try:
                dres = run_daily(conn, cfg, dry_run=dry_run, created_at=clock if injected else None)
            except LateWrite as exc:
                out.bump(3)
                out.lines.append(f"[predict] GEC: {exc}")
                step("predict", {"late_write_refused": str(exc)[:300]})
            else:
                cov = len(dres.evaluated) / dres.universe if dres.universe else 0.0
                step("predict", {"made_at": str(dres.made_at), "inserted": dres.inserted,
                                 "skipped_existing": dres.skipped_existing, "evaluated": len(dres.evaluated),
                                 "universe": dres.universe, "coverage": round(cov, 3), "excluded": dres.excluded,
                                 "base_rate": dres.base_rate, "skipped_baselines": dres.skipped_baselines})
                out.lines += ["[predict] " + s for s in dres.summary_lines()]
                if cov < min_coverage:
                    out.bump(1)
                    out.lines.append(f"[predict] UYARI: kapsam {cov:.0%} < {min_coverage:.0%} "
                                     f"({len(dres.evaluated)}/{dres.universe}); veri sorunu olabilir")
                if dres.skipped_baselines or dres.base_rate is None:
                    out.bump(1)
                    out.lines.append("[predict] UYARI: bazi baseline'lar yazilamadi (girdi yetersiz / "
                                     "gecmis yukselis orani yok); karsilastirma referansi eksik")
        run.detail["exit_code"] = out.exit_code
        if out.exit_code == 3:
            run.status = SKIPPED
    return out
