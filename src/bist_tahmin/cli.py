"""Komut satiri girisi (PROJE.md Bolum 14): init, fetch, universe sync, predict daily, resolve, run-daily."""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from zoneinfo import ZoneInfo

from . import __version__
from .config import ConfigError, load_config
from .db import DEFAULT_DB_NAME, InitError, run_init

DISCLAIMER = "Bu cikti yatirim tavsiyesi degildir; yalnizca model olcum amaclidir."


def _cmd_init(args: argparse.Namespace) -> int:
    admin = os.environ.get("DATABASE_ADMIN_URL")
    if not admin:
        print("hata: DATABASE_ADMIN_URL ortam degiskeni gerekli", file=sys.stderr)
        return 2
    try:
        load_config(args.config)
        plan = run_init(
            admin,
            db_name=args.db_name,
            app_password=os.environ.get("BIST_APP_PASSWORD"),
            dry_run=args.dry_run,
        )
    except (ConfigError, InitError) as exc:
        print(f"hata: {exc}", file=sys.stderr)
        return 2
    prefix = "[dry-run] " if args.dry_run else ""
    for line in plan.describe():
        print(f"{prefix}{line}")
    if not args.dry_run:
        print("init tamam")
    return 0


def _app_conn_or_none():
    """DATABASE_URL (bist_app) ile baglanti; ortam degiskeni yoksa None. URL asla yazdirilmaz."""
    import psycopg

    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    return psycopg.connect(url, autocommit=True, connect_timeout=15)


def _need_db_message() -> None:
    print("hata: DATABASE_URL (bist_app) ortam degiskeni gerekli", file=sys.stderr)


def _make_source():
    from .data.source import BorsapySource

    return BorsapySource()


def _today_istanbul() -> dt.date:
    return dt.datetime.now(ZoneInfo("Europe/Istanbul")).date()


def _cmd_universe_sync(args: argparse.Namespace) -> int:
    from .data import store
    from .data.universe import UniverseError, fetch_components

    conn = _app_conn_or_none()
    if conn is None:
        _need_db_message()
        return 2
    prefix = "[dry-run] " if args.dry_run else ""
    try:
        as_of = dt.date.fromisoformat(args.as_of) if args.as_of else _today_istanbul()
        with conn:
            symbols = fetch_components(_make_source())
            added, closed = store.sync_universe(conn, symbols, as_of, dry_run=args.dry_run)
    except (UniverseError, ValueError) as exc:
        print(f"hata: {exc}", file=sys.stderr)
        return 2
    print(f"{prefix}kaynak bilesen sayisi: {len(symbols)}; as_of={as_of}")
    for m in added:
        print(f"{prefix}uyelik ekle: {m.ticker} valid_from={m.valid_from}")
    for m in closed:
        print(f"{prefix}uyelik kapat: {m.ticker} valid_to={m.valid_to}")
    if not added and not closed:
        print(f"{prefix}degisiklik yok")
    return 0


def _cmd_universe_load_history(args: argparse.Namespace) -> int:
    from .data import store
    from .data.universe import HISTORY_CSV, HistoryConflict, read_history_csv

    conn = _app_conn_or_none()
    if conn is None:
        _need_db_message()
        return 2
    prefix = "[dry-run] " if args.dry_run else ""
    try:
        target = read_history_csv(args.file or HISTORY_CSV)
        with conn:
            ins, upd = store.load_membership_history(conn, target, dry_run=args.dry_run)
    except (OSError, ValueError, HistoryConflict) as exc:
        print(f"hata: {exc}", file=sys.stderr)
        return 2
    print(f"{prefix}gecmis uyelik: {len(target)} aralik okundu; eklenecek/eklendi {ins}, valid_from erkene cekilen {upd}")
    return 0


def _cmd_fetch(args: argparse.Namespace) -> int:
    from .data import store
    from .data.fetch import fetch_all

    try:
        since = dt.date.fromisoformat(args.since)
    except ValueError:
        print("hata: --since YYYY-MM-DD bicimli olmali", file=sys.stderr)
        return 2
    conn = _app_conn_or_none()
    if conn is None:
        _need_db_message()
        return 2
    prefix = "[dry-run] " if args.dry_run else ""
    with conn:
        if args.tickers:
            tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        else:
            rows = store.read_membership(conn)
            tickers = sorted({m.ticker for m in rows if m.valid_to is None or m.valid_to >= since})
        if not tickers:
            print("hata: evren bos; once `bist universe sync` calistirin ya da --tickers verin", file=sys.stderr)
            return 2
        print(f"{prefix}{len(tickers)} hisse + XU100, since={since}")
        # Kalite kontrolu (data/quality.py) ve bugunun yarim barinin elenmesi varsayilan olarak acik.
        res = fetch_all(_make_source(), conn, tickers, since, dry_run=args.dry_run)
    for line in res.summary_lines():
        print(f"{prefix}{line}")
    return 0 if res.ok else 1


def _cmd_predict_daily(args: argparse.Namespace) -> int:
    from .jobs import JobLocked, tracked_job
    from .predict.daily import run_daily

    conn = _app_conn_or_none()
    if conn is None:
        _need_db_message()
        return 2
    prefix = "[dry-run] " if args.dry_run else ""
    try:
        cfg = load_config(args.config)
        with conn, tracked_job(conn, "predict", _today_istanbul(), record=not args.dry_run) as run:
            res = run_daily(conn, cfg, made_at=args.made_at, dry_run=args.dry_run,
                            allow_backfill=args.allow_backfill)
            run.detail.update({"made_at": str(res.made_at), "inserted": res.inserted,
                               "skipped_existing": res.skipped_existing, "evaluated": len(res.evaluated)})
    except JobLocked as exc:
        print(f"atlandi: {exc}", file=sys.stderr)
        return 4
    except (ConfigError, ValueError) as exc:
        print(f"hata: {exc}", file=sys.stderr)
        return 2
    for line in res.summary_lines():
        print(f"{prefix}{line}")
    return 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    from .data import store
    from .jobs import JobLocked, tracked_job
    from .resolve.resolver import count_overdue, resolve_due

    conn = _app_conn_or_none()
    if conn is None:
        _need_db_message()
        return 2
    prefix = "[dry-run] " if args.dry_run else ""
    try:
        cfg = load_config(args.config)
        with conn, tracked_job(conn, "resolve", _today_istanbul(), record=not args.dry_run) as run:
            cal = store.read_calendar(conn)
            res = resolve_due(conn, cal, label_threshold=float(cfg["label_threshold"]), dry_run=args.dry_run)
            overdue = count_overdue(conn, cal)
            run.detail.update({"resolved": res.resolved, "skipped": dict(res.skipped), "details": res.details[:20],
                               "overdue": overdue})
    except JobLocked as exc:
        print(f"atlandi: {exc}", file=sys.stderr)
        return 4
    except (ConfigError, ValueError) as exc:
        print(f"hata: {exc}", file=sys.stderr)
        return 2
    print(f"{prefix}cozulen: {res.resolved}; atlanan: {dict(res.skipped) or '-'}")
    if overdue["overdue"]:
        print(f"{prefix}UYARI: vadesi gecmis ama COZULMEMIS {overdue['overdue']} tahmin "
              f"(en eski made_at {overdue['oldest_made_at']}; hisseler {overdue['tickers'][:8]})")
    for line in res.details[:20]:
        print(f"{prefix}  {line}")
    return 0


def _cmd_run_daily(args: argparse.Namespace) -> int:
    from .jobs import JobLocked
    from .pipeline import run_pipeline

    conn = _app_conn_or_none()
    if conn is None:
        _need_db_message()
        return 2
    prefix = "[dry-run] " if args.dry_run else ""
    try:
        cfg = load_config(args.config)
        with conn:
            res = run_pipeline(conn, cfg, _make_source(), dry_run=args.dry_run)
    except JobLocked as exc:
        print(f"atlandi: {exc}", file=sys.stderr)
        return 4
    except (ConfigError, ValueError, RuntimeError) as exc:
        print(f"hata: {exc}", file=sys.stderr)
        return 2
    for line in res.lines:
        print(f"{prefix}{line}")
    print(DISCLAIMER)
    return res.exit_code


def _cmd_health(args: argparse.Namespace) -> int:
    """Saglik kontrolu (salt okunur): 0 saglikli, 1 uyari, 2 kritik. cron_health.sh sonucu ALERT.txt'ye yansitir."""
    import psycopg

    from .monitor.health import run_health

    conn = _app_conn_or_none()
    if conn is None:
        _need_db_message()
        return 2
    try:
        cfg = load_config(args.config)
        with conn:
            report = run_health(conn, cfg, max_age_hours=args.max_age_hours)
    except (ConfigError, ValueError) as exc:
        print(f"hata: {exc}", file=sys.stderr)
        return 2
    except psycopg.Error as exc:
        print(f"KRITIK: saglik kontrolu veritabanina erisemedi: {type(exc).__name__}", file=sys.stderr)
        return 2
    for line in report.lines():
        print(line)
    return report.exit_code


def _cmd_breaker_status(args: argparse.Namespace) -> int:
    import psycopg

    from .monitor import breaker

    conn = _app_conn_or_none()
    if conn is None:
        _need_db_message()
        return 2
    try:
        with conn:
            st = breaker.status(conn)
    except psycopg.Error as exc:
        print(f"hata: veritabani: {type(exc).__name__} (migration 006 uygulandi mi?)", file=sys.stderr)
        return 2
    if st["tripped"]:
        print(f"devre kesici: ACIK (since {st['since']}, model {st['model_version']}, "
              f"goreli dusus {st['relative_drawdown']})")
        print(f"neden: {st['reason']}")
        print("model uretimi durdu (baseline'lar surer). Kapatmak icin: bist breaker reset --reason \"...\"")
    else:
        print("devre kesici: kapali")
        if st.get("reset_at"):
            print(f"son sifirlama: {st['reset_at']} ({st['reset_reason']}); degerlendirme {st['evaluate_after']} "
                  "sonrasi kohortlarla yeniden basladi")
    print(f"gecmis kayit sayisi: {len(st['history'])}")
    return 1 if st["tripped"] else 0


def _cmd_breaker_reset(args: argparse.Namespace) -> int:
    import psycopg

    from .monitor import breaker

    reason = (args.reason or "").strip()
    if not reason:
        print("hata: --reason (acik gerekce) zorunlu; devre kesici gerekcesiz sifirlanmaz", file=sys.stderr)
        return 2
    conn = _app_conn_or_none()
    if conn is None:
        _need_db_message()
        return 2
    prefix = "[dry-run] " if args.dry_run else ""
    try:
        with conn:
            before = breaker.status(conn)
            if args.dry_run:
                print(f"{prefix}devre kesici su an {'ACIK' if before['tripped'] else 'kapali'}; gerekce: {reason}")
                return 0
            new = breaker.reset(conn, reason)
    except psycopg.Error as exc:
        print(f"hata: veritabani: {type(exc).__name__}", file=sys.stderr)
        return 2
    if not before["tripped"]:
        print("not: devre kesici zaten kapaliydi; yalnizca kayit eklendi")
    print(f"devre kesici SIFIRLANDI (gerekce: {reason}); degerlendirme {new['evaluate_after']} sonrasi "
          "kohortlarla yeniden baslar. Modeller bir sonraki gunluk akista tekrar uretilir.")
    return 0


def _emit_markdown(text: str, out: str | None) -> None:
    """Markdown'i stdout'a basar; --out verilirse dosyaya da yazar (ust dizin yoksa olusturulur)."""
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:   # C locale / ASCII terminal: ham UTF-8 bayt yaz
        sys.stdout.flush()
        sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.flush()
    if out:
        from pathlib import Path

        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"rapor yazildi: {path}", file=sys.stderr)


def _run_report(args: argparse.Namespace, *, roles: list[str] | None) -> int:
    """Salt-okunur rapor; veritabanina yazmaz (--dry-run etkisizdir)."""
    import psycopg

    from .evaluate.report import build_report
    from .evaluate.report_portfolio import cohort_portfolio_section

    conn = _app_conn_or_none()
    if conn is None:
        _need_db_message()
        return 2
    try:
        as_of = dt.date.fromisoformat(args.as_of) if getattr(args, "as_of", None) else None
        cfg = load_config(args.config)
        with conn:
            report = build_report(conn, cfg, as_of=as_of, roles=roles, portfolio_fn=cohort_portfolio_section)
        _emit_markdown(report.text, getattr(args, "out", None))
    except (ConfigError, ValueError, OSError) as exc:
        print(f"hata: {exc}", file=sys.stderr)
        return 2
    except psycopg.Error as exc:
        print(f"hata: veritabani: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


def _cmd_baseline_report(args: argparse.Namespace) -> int:
    return _run_report(args, roles=["baseline"])


def _cmd_report_weekly(args: argparse.Namespace) -> int:
    return _run_report(args, roles=None)


def _cmd_backtest_build_dataset(args: argparse.Namespace) -> int:
    from .backtest.dataset import build_dataset, save_dataset

    conn = _app_conn_or_none()
    if conn is None:
        _need_db_message()
        return 2
    try:
        cfg = load_config(args.config)
        with conn:
            ds = build_dataset(conn, cfg, start=args.start, end=args.end,
                               progress=lambda i, n: print(f"  {i}/{n} gun", flush=True))
        save_dataset(ds, args.out)
    except (ConfigError, ValueError, OSError) as exc:
        print(f"hata: {exc}", file=sys.stderr)
        return 2
    m = ds.meta
    print(f"veri seti: {len(ds.frame)} satir, {ds.frame['date'].nunique()} gun, {ds.frame['ticker'].nunique()} hisse "
          f"({m.get('date_min')} .. {m.get('date_max')}) -> {args.out}")
    if m.get("skipped_tickers"):
        print(f"fiyati olmayan uye (atlandi): {m['skipped_tickers']}")
    return 0


def _cmd_backtest_run(args: argparse.Namespace) -> int:
    from .backtest import lab
    from .backtest.dataset import load_dataset
    from .backtest.guard import HoldoutLocked
    from .backtest.runner import run_experiment

    conn = _app_conn_or_none()
    if conn is None:
        _need_db_message()
        return 2
    prefix = "[dry-run] " if args.dry_run else ""
    try:
        cfg = load_config(args.config)
        ds = load_dataset(args.dataset)
        features = args.features.split(",") if "," in args.features else args.features
        spec = lab.ModelSpec(name=args.name, label=args.label, features=features, C=args.C, penalty=args.penalty,
                             window=args.window, rolling_days=args.rolling_days)
        if args.use_holdout:
            print("UYARI: --use-holdout: dokunulmaz SINAMA KUMESI aciliyor. Bu yalnizca terfi asamasi icindir; kullanim "
                  "deney kaydina yazilir ve geri alinamaz.", file=sys.stderr)
        with conn:
            run = run_experiment(conn, cfg, ds, spec, family=args.family, description=args.description or args.name,
                                 use_holdout=args.use_holdout, dry_run=args.dry_run, alpha=args.alpha, seed=args.seed)
    except HoldoutLocked as exc:
        print(f"hata: sinama kumesi kilitli: {exc}", file=sys.stderr)
        return 2
    except (ConfigError, ValueError, OSError) as exc:
        print(f"hata: {exc}", file=sys.stderr)
        return 2
    text = lab.format_result(run.result)
    print(f"{prefix}deney #{run.experiment_id} aile={run.family} deneme_sayisi={run.n_trials} fold={run.n_folds}")
    print(text)
    if args.out:
        from pathlib import Path
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0


class _DryRunRollback(Exception):
    """Kuru calistirmada tum yazimi geri almak icin ic istisna (islem yapilmaz, yalniz kontroller ve egitim)."""


def _models_roles_arg(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    return [r.strip() for r in raw.split(",") if r.strip()]


def _load_holdout(path: str | None) -> dict | None:
    """lab.result_json ciktisi (run_info.spec iceren). Dosya yoksa None: promote_check sinama kosulunu KALDI verir."""
    import json
    from pathlib import Path

    if path is None:
        print("UYARI: --holdout-result verilmedi; sinama kumesi kosulu KALDI sayilacak", file=sys.stderr)
        return None
    p = Path(path)
    if not p.exists():
        print(f"UYARI: holdout dosyasi yok ({p}); sinama kumesi kosulu KALDI sayilacak", file=sys.stderr)
        return None
    data = json.loads(p.read_text(encoding="utf-8"))  # bozuk JSON: ValueError -> exit 2
    if not isinstance(data, dict):
        raise ValueError(f"holdout dosyasi bir JSON nesnesi olmali: {p}")
    return data


def _family_trials(conn, family: str) -> int:
    """Bonferroni paydasi: backtest experiments tablosunda o ailedeki kayit sayisi (en az 1)."""
    from .backtest.registry import count_trials

    return max(1, count_trials(conn, family))


def _decide(conn, cfg: dict, args: argparse.Namespace):
    from .models.promote import promote_check

    holdout = _load_holdout(args.holdout_result)
    n_trials = _family_trials(conn, args.family)
    decision = promote_check(conn, cfg, args.challenger, args.champion,
                             holdout_result=holdout, n_trials=n_trials)
    return decision, n_trials


def _register_challenger(conn, cfg: dict, ds, spec, as_of, *, dry_run: bool):
    """train_and_register; dry_run ise tum islem geri alinir (kayit, deney satiri yok). Kontroller aynen calisir."""
    from .models.train import train_and_register

    if not dry_run:
        return train_and_register(conn, cfg, ds, spec, as_of)
    box = []
    try:
        with conn.transaction():
            box.append(train_and_register(conn, cfg, ds, spec, as_of))
            raise _DryRunRollback()
    except _DryRunRollback:
        pass
    return box[0]


def _cmd_models_list(args: argparse.Namespace) -> int:
    import psycopg

    from .models.registry import list_models

    conn = _app_conn_or_none()
    if conn is None:
        _need_db_message()
        return 2
    try:
        with conn:
            recs = list_models(conn, _models_roles_arg(args.roles))
    except ValueError as exc:
        print(f"hata: {exc}", file=sys.stderr)
        return 2
    except psycopg.Error as exc:
        print(f"hata: veritabani: {type(exc).__name__}", file=sys.stderr)
        return 2
    if not recs:
        print("kayitli model yok")
        return 0
    for r in recs:
        print(f"{r.version}  role={r.role}  trained_at={r.trained_at.isoformat(timespec='minutes')}  "
              f"train_end={r.train_end}")
    return 0


def _cmd_models_train(args: argparse.Namespace) -> int:
    import psycopg

    from .backtest import lab
    from .backtest.dataset import load_dataset
    from .jobs import JobLocked, tracked_job

    conn = _app_conn_or_none()
    if conn is None:
        _need_db_message()
        return 2
    prefix = "[dry-run] " if args.dry_run else ""
    today = _today_istanbul()
    try:
        cfg = load_config(args.config)
        as_of = dt.date.fromisoformat(args.as_of) if args.as_of else today
        ds = load_dataset(args.dataset)
        features = args.features.split(",") if "," in args.features else args.features
        spec = lab.ModelSpec(name=args.name, label=args.label, features=features, C=args.C)
        with conn, tracked_job(conn, "models-train", today, record=not args.dry_run) as run:
            rec = _register_challenger(conn, cfg, ds, spec, as_of, dry_run=args.dry_run)
            ex = rec.params.get("extra") or {}
            run.detail.update({"version": rec.version, "n_rows": ex.get("n_rows"), "dry_run": args.dry_run})
    except JobLocked as exc:
        print(f"atlandi: {exc}", file=sys.stderr)
        return 4
    except (ConfigError, ValueError, OSError) as exc:
        print(f"hata: {exc}", file=sys.stderr)
        return 2
    except psycopg.Error as exc:
        print(f"hata: veritabani: {type(exc).__name__}", file=sys.stderr)
        return 2
    verb = "hesaplandi (kayit geri alindi)" if args.dry_run else "challenger kaydedildi"
    print(f"{prefix}{verb}: {rec.version}")
    print(f"{prefix}role={rec.role} train_end={rec.train_end} satir={ex.get('n_rows')} "
          f"gun={ex.get('n_train_days')} tarih={ex.get('train_date_min')}..{ex.get('train_date_max')} "
          f"taban_oran={ex.get('train_base_rate')}")
    print(f"{prefix}sinama kumesi disarida: {ex.get('train_includes_holdout') is False} "
          f"(holdout_start={ex.get('holdout_start')})")
    return 0


def _cmd_models_promote_check(args: argparse.Namespace) -> int:
    import psycopg

    from .models.promote import format_decision

    conn = _app_conn_or_none()
    if conn is None:
        _need_db_message()
        return 2
    prefix = "[dry-run] " if args.dry_run else ""
    try:
        cfg = load_config(args.config)
        with conn:
            decision, n_trials = _decide(conn, cfg, args)
    except (ConfigError, ValueError, OSError) as exc:
        print(f"hata: {exc}", file=sys.stderr)
        return 2
    except psycopg.Error as exc:
        print(f"hata: veritabani: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(f"{prefix}deneme sayisi (aile {args.family}, Bonferroni): {n_trials}")
    print(format_decision(decision))
    return 0 if decision.approved else 1


def _cmd_models_promote(args: argparse.Namespace) -> int:
    import psycopg

    from .jobs import JobLocked, tracked_job
    from .models import registry as reg
    from .models.promote import format_decision

    if not args.confirm:
        print("hata: terfi icin --confirm gerekli; promote-check onayi tek basina terfi ettirmez. Hicbir sey yapilmadi.",
              file=sys.stderr)
        return 2
    conn = _app_conn_or_none()
    if conn is None:
        _need_db_message()
        return 2
    prefix = "[dry-run] " if args.dry_run else ""
    today = _today_istanbul()
    try:
        cfg = load_config(args.config)
        with conn:
            decision, n_trials = _decide(conn, cfg, args)
            print(f"deneme sayisi (aile {args.family}, Bonferroni): {n_trials}")
            print(format_decision(decision))
            if not decision.approved:
                print(f"hata: terfi reddedildi ({decision.summary}); hicbir sey degismedi", file=sys.stderr)
                return 2
            if args.dry_run:
                print(f"{prefix}terfi kosullari saglandi; yazilmadi")
                return 0
            with tracked_job(conn, "models-promote", today, record=True) as run:
                old = [r.version for r in reg.list_models(conn, ["champion"])]
                rec = reg.promote(conn, args.challenger)
                run.detail.update({"promoted": rec.version, "retired_champions": old, "n_trials": n_trials,
                                   "decision": decision.summary})
    except JobLocked as exc:
        print(f"atlandi: {exc}", file=sys.stderr)
        return 4
    except (ConfigError, ValueError, OSError) as exc:
        print(f"hata: {exc}", file=sys.stderr)
        return 2
    except psycopg.Error as exc:
        print(f"hata: veritabani: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(f"terfi tamam: {rec.version} champion oldu; emekliye ayrilan eski champion: {old or 'yok'}")
    return 0


def _cmd_models_retire(args: argparse.Namespace) -> int:
    import psycopg

    from .jobs import JobLocked, tracked_job
    from .models import registry as reg

    reason = (args.reason or "").strip()
    if not reason:
        print("hata: --reason (acik gerekce) zorunlu; gerekcesiz emeklilik yapilmaz", file=sys.stderr)
        return 2
    conn = _app_conn_or_none()
    if conn is None:
        _need_db_message()
        return 2
    prefix = "[dry-run] " if args.dry_run else ""
    today = _today_istanbul()
    try:
        with conn:
            cur = reg.get_model(conn, args.version)
            if cur is None:
                raise ValueError(f"model yok: {args.version}")
            if cur.role == "retired":
                raise ValueError(f"model zaten retired (terminal): {args.version}")
            if args.dry_run:
                print(f"{prefix}{cur.version} ({cur.role}) emekliye ayrilacak; gerekce: {reason}")
                return 0
            with tracked_job(conn, "models-retire", today, record=True) as run:
                run.detail.update({"version": cur.version, "from_role": cur.role, "reason": reason})
                reg.retire(conn, cur.version)
    except JobLocked as exc:
        print(f"atlandi: {exc}", file=sys.stderr)
        return 4
    except (ValueError, OSError) as exc:
        print(f"hata: {exc}", file=sys.stderr)
        return 2
    except psycopg.Error as exc:
        print(f"hata: veritabani: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(f"emekliye ayrildi: {args.version} (gerekce: {reason}; runs tablosuna kaydedildi)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bist", description="BIST 30 tahmin olcum laboratuvari")
    p.add_argument("--version", action="version", version=f"bist {__version__}")
    p.add_argument("--config", default=None, help="yapilandirma dosyasi (varsayilan config/default.yaml)")
    sub = p.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="rolleri, veritabanini ve semayi kur (idempotent)")
    init.add_argument("--db-name", default=DEFAULT_DB_NAME)
    init.add_argument("--dry-run", action="store_true", help="yazmadan ne olacagini goster")
    init.set_defaults(func=_cmd_init)

    fetch = sub.add_parser("fetch", help="fiyat ve kurumsal islemleri indir, XU100 takvimini guncelle")
    fetch.add_argument("--since", required=True, help="baslangic tarihi YYYY-MM-DD")
    fetch.add_argument("--tickers", default=None, help="virgullu liste (varsayilan: evrendeki hisseler)")
    fetch.add_argument("--dry-run", action="store_true", help="veritabanina yazmadan ne olacagini goster")
    fetch.set_defaults(func=_cmd_fetch)

    uni = sub.add_parser("universe", help="evren (BIST 30 uyeligi) islemleri")
    uni_sub = uni.add_subparsers(dest="universe_command", required=True)
    sync = uni_sub.add_parser("sync", help="XU030 bilesenlerini al, tarihli uyeligi guncelle")
    sync.add_argument("--as-of", default=None, help="uyelik tarihi YYYY-MM-DD (varsayilan: bugun, Istanbul)")
    sync.add_argument("--dry-run", action="store_true", help="yazmadan ne olacagini goster")
    sync.set_defaults(func=_cmd_universe_sync)
    hist = uni_sub.add_parser("load-history", help="reference/bist30 gecmis ceyreklik uyelik araliklarini yukle (idempotent)")
    hist.add_argument("--file", default=None, help="CSV (varsayilan reference/bist30/membership_history.csv)")
    hist.add_argument("--dry-run", action="store_true", help="yazmadan ne olacagini goster")
    hist.set_defaults(func=_cmd_universe_load_history)

    pred = sub.add_parser("predict", help="tahmin uretimi")
    pred_sub = pred.add_subparsers(dest="predict_command", required=True)
    daily = pred_sub.add_parser("daily", help="bugunun (takvimin son gunu) baseline tahminlerini yaz")
    daily.add_argument("--made-at", default=None, help="YYYY-MM-DD (varsayilan: takvimin son gunu)")
    daily.add_argument("--allow-backfill", action="store_true",
                       help="YALNIZCA gelistirme: gecmis gune tahmin yaz (uretim defterinde kullanilmaz)")
    daily.add_argument("--dry-run", action="store_true", help="yazmadan ne olacagini goster")
    daily.set_defaults(func=_cmd_predict_daily)

    resolve = sub.add_parser("resolve", help="vadesi gelen tahminleri coz")
    resolve.add_argument("--dry-run", action="store_true", help="yazmadan ne olacagini goster")
    resolve.set_defaults(func=_cmd_resolve)

    rd = sub.add_parser("run-daily", help="gunluk akis: indir -> coz -> tahmin (cron icin; cikis 3 = bayat veri)")
    rd.add_argument("--dry-run", action="store_true", help="veritabanina yazmadan goster")
    rd.set_defaults(func=_cmd_run_daily)

    hl = sub.add_parser("health", help="saglik kontrolu: akis calisti mi, veri guncel mi (0 saglikli, 1 uyari, 2 kritik)")
    hl.add_argument("--max-age-hours", type=float, default=72.0,
                    help="son BASARILI gunluk akisin en fazla yasi, saat (varsayilan 72: hafta sonu + tatil payi)")
    hl.set_defaults(func=_cmd_health)

    br_ = sub.add_parser("breaker", help="devre kesici (model uretimi duraklatma) durumu ve elle sifirlama")
    br_sub = br_.add_subparsers(dest="breaker_command", required=True)
    bst = br_sub.add_parser("status", help="devre kesici durumu (cikis 0 kapali, 1 acik)")
    bst.set_defaults(func=_cmd_breaker_status)
    brs = br_sub.add_parser("reset", help="devre kesiciyi elle kapat; --reason ZORUNLU (yoksa cikis 2)")
    brs.add_argument("--reason", default=None, help="sifirlama gerekcesi (kayda gecer)")
    brs.add_argument("--dry-run", action="store_true", help="yazmadan ne olacagini goster")
    brs.set_defaults(func=_cmd_breaker_reset)

    base = sub.add_parser("baseline", help="baseline islemleri")
    base_sub = base.add_subparsers(dest="baseline_command", required=True)
    brep = base_sub.add_parser("report", help="baseline performans raporu (yalnizca model_role='baseline'; salt-okunur)")
    brep.add_argument("--as-of", default=None, help="YYYY-MM-DD (varsayilan: bugun, Istanbul)")
    brep.add_argument("--out", default=None, help="markdown'i ayrica bu dosyaya yaz (varsayilan: yok)")
    brep.add_argument("--dry-run", action="store_true", help="kabul edilir, etkisiz (rapor zaten yazmaz)")
    brep.set_defaults(func=_cmd_baseline_report)

    rep = sub.add_parser("report", help="rapor uretimi")
    rep_sub = rep.add_subparsers(dest="report_command", required=True)
    weekly = rep_sub.add_parser("weekly", help="haftalik rapor (tum roller; salt-okunur)")
    weekly.add_argument("--out", default=None, help="markdown'i ayrica bu dosyaya yaz (varsayilan: yok)")
    weekly.add_argument("--as-of", default=None, help="YYYY-MM-DD (varsayilan: bugun, Istanbul)")
    weekly.add_argument("--dry-run", action="store_true", help="kabul edilir, etkisiz (rapor zaten yazmaz)")
    weekly.set_defaults(func=_cmd_report_weekly)

    bt = sub.add_parser("backtest", help="backtest laboratuvari (Faz 3.5)")
    bt_sub = bt.add_subparsers(dest="backtest_command", required=True)
    bd = bt_sub.add_parser("build-dataset", help="tarihli uyelikle ozellik+etiket veri setini uret ve dosyaya yaz")
    bd.add_argument("--out", default="data/dataset.pkl")
    bd.add_argument("--start", default=None, help="YYYY-MM-DD")
    bd.add_argument("--end", default=None, help="YYYY-MM-DD")
    bd.set_defaults(func=_cmd_backtest_build_dataset)
    br = bt_sub.add_parser("run", help="purge'lu walk-forward deneyi; her deneme experiments'a kaydedilir")
    br.add_argument("--dataset", default="data/dataset.pkl")
    br.add_argument("--name", required=True, help="model adi")
    br.add_argument("--family", default="faz35", help="coklu deneme duzeltmesi ailesi (ayni ailedeki kayit sayisi n_trials)")
    br.add_argument("--description", default=None)
    br.add_argument("--label", default="label_up", choices=["label_up", "label_rel_up"])
    br.add_argument("--features", default="all_z", help="grup (all, all_z, stock_z, stock_raw, market, calendar; a+b) ya da virgullu ad listesi")
    br.add_argument("--C", type=float, default=1.0)
    br.add_argument("--penalty", default="l2", choices=["l1", "l2"])
    br.add_argument("--window", default="expanding", choices=["expanding", "rolling"])
    br.add_argument("--rolling-days", type=int, default=None)
    br.add_argument("--alpha", type=float, default=0.05)
    br.add_argument("--seed", type=int, default=0)
    br.add_argument("--out", default=None, help="sonucu markdown olarak dosyaya da yaz")
    br.add_argument("--use-holdout", action="store_true", help="YALNIZCA terfi asamasi: dokunulmaz sinama kumesini ac (loglanir)")
    br.add_argument("--dry-run", action="store_true", help="veritabanina yazma (deneme sayilmaz)")
    br.set_defaults(func=_cmd_backtest_run)

    mdl = sub.add_parser("models", help="model yasam dongusu: liste, challenger egitimi, terfi kontrolu, terfi, emeklilik")
    mdl_sub = mdl.add_subparsers(dest="models_command", required=True)
    mlist = mdl_sub.add_parser("list", help="kayitli modeller (salt-okunur)")
    mlist.add_argument("--roles", default=None, help="virgullu roller: champion,challenger,retired (varsayilan: hepsi)")
    mlist.set_defaults(func=_cmd_models_list)
    mtrain = mdl_sub.add_parser("train", help="challenger egit ve CHALLENGER olarak kaydet (champion atanmaz)")
    mtrain.add_argument("--dataset", default="data/dataset.pkl")
    mtrain.add_argument("--name", required=True, help="model adi (version = logreg_NAME@AS_OF)")
    mtrain.add_argument("--features", default="all_z", help="grup ya da virgullu ad listesi")
    mtrain.add_argument("--label", default="label_up", choices=["label_up", "label_rel_up"])
    mtrain.add_argument("--C", type=float, default=1.0)
    mtrain.add_argument("--as-of", default=None, help="YYYY-MM-DD (varsayilan: bugun, Istanbul)")
    mtrain.add_argument("--dry-run", action="store_true", help="egitir ve kontrol eder, kaydi geri alir (hicbir sey kalici yazilmaz)")
    mtrain.set_defaults(func=_cmd_models_train)
    mpc = mdl_sub.add_parser("promote-check", help="terfi kuralini degerlendirir (yazmaz; cikis 0 ONAY, 1 RED)")
    mpc.add_argument("--challenger", required=True, help="challenger version")
    mpc.add_argument("--champion", default=None, help="yalnizca bilgi amacli karsilastirma")
    mpc.add_argument("--holdout-result", default=None, help="lab.result_json ciktisi (JSON); yoksa sinama kosulu KALDI")
    mpc.add_argument("--family", default="faz35", help="n_trials: backtest experiments'ta bu ailedeki kayit sayisi")
    mpc.add_argument("--dry-run", action="store_true", help="kabul edilir, etkisiz (kontrol zaten yazmaz)")
    mpc.set_defaults(func=_cmd_models_promote_check)
    mpro = mdl_sub.add_parser("promote", help="challenger'i champion yapar; YALNIZ terfi kontrolu onaylar VE --confirm verilirse")
    mpro.add_argument("--challenger", required=True)
    mpro.add_argument("--confirm", action="store_true", help="zorunlu onay bayragi; yoksa hicbir sey yapilmaz (cikis 2)")
    mpro.add_argument("--champion", default=None, help="yalnizca bilgi amacli karsilastirma")
    mpro.add_argument("--holdout-result", default=None, help="lab.result_json ciktisi (JSON)")
    mpro.add_argument("--family", default="faz35", help="n_trials ailesi")
    mpro.add_argument("--dry-run", action="store_true", help="kontrolu gosterir, terfi ettirmez")
    mpro.set_defaults(func=_cmd_models_promote)
    mret = mdl_sub.add_parser("retire", help="modeli emekliye ayirir; --reason ZORUNLU (gerekce runs tablosuna yazilir)")
    mret.add_argument("--version", required=True)
    mret.add_argument("--reason", default=None, help="emeklilik gerekcesi (zorunlu)")
    mret.add_argument("--dry-run", action="store_true", help="yazmadan ne olacagini goster")
    mret.set_defaults(func=_cmd_models_retire)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "websocket"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
