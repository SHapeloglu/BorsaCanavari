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
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "websocket"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
