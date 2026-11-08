"""Komut satiri girisi (PROJE.md Bolum 14): init, fetch, universe sync."""

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
    return psycopg.connect(url, autocommit=True)


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
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "websocket"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
