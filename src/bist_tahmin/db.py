"""Veritabani kurulumu: roller, veritabani, migration (PROJE.md Bolum 6 ve 19).

Tasarim:
- `bist_owner`: NOLOGIN; sema sahibi. Yalnizca yonetici baglantisi `SET LOCAL ROLE` ile kullanir.
- `bist_app`: LOGIN; uygulama rolu, en az yetki (003_grants.sql).
- Migration'lar bist_owner olarak calisir; boylece bist_app tablo/tetikleyici sahibi olamaz.
"""

from __future__ import annotations

import importlib.resources as ir
import re
import zlib
from dataclasses import dataclass, field

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

OWNER_ROLE = "bist_owner"
APP_ROLE = "bist_app"
DEFAULT_DB_NAME = "bist_tahmin"
_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_MIGRATION_NAME = re.compile(r"^\d{3}_[a-z0-9_]+\.sql$")


class InitError(RuntimeError):
    """Kurulum on kosulu saglanmadi."""


@dataclass
class InitPlan:
    db_name: str
    create_owner_role: bool = False
    create_app_role: bool = False
    create_database: bool = False
    pending_migrations: list[str] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return not (self.create_owner_role or self.create_app_role or self.create_database
                    or self.pending_migrations)

    def describe(self) -> list[str]:
        lines = []
        if self.create_owner_role:
            lines.append(f"rol olustur: {OWNER_ROLE} (NOLOGIN)")
        if self.create_app_role:
            lines.append(f"rol olustur: {APP_ROLE} (LOGIN, statement_timeout=60s, baglanti siniri 5)")
        if self.create_database:
            lines.append(f"veritabani olustur: {self.db_name} (sahip: {OWNER_ROLE})")
        for m in self.pending_migrations:
            lines.append(f"migration uygula: {m}")
        return lines or ["yapilacak bir sey yok (sema guncel)"]


def with_overrides(conninfo: str, **kwargs: str) -> str:
    """Bir baglanti bilgisinin uzerine dbname/user/password gibi alanlari yazar."""
    return make_conninfo(conninfo, **kwargs)


def check_identifier(name: str) -> str:
    if not _IDENT.match(name):
        raise InitError(f"gecersiz veritabani adi: {name!r} (kucuk harf, rakam, alt cizgi)")
    return name


def list_migrations() -> list[tuple[str, str]]:
    """Paket icindeki (ad, sql metni) migration'lari sirali dondurur."""
    base = ir.files("bist_tahmin") / "sql"
    items = [p for p in base.iterdir() if _MIGRATION_NAME.match(p.name)]
    return [(p.name, p.read_text(encoding="utf-8")) for p in sorted(items, key=lambda p: p.name)]


def _role_exists(conn: psycopg.Connection, role: str) -> bool:
    return conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone() is not None


def _db_exists(conn: psycopg.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone() is not None


def _applied_migrations(conn: psycopg.Connection) -> set[str]:
    if conn.execute("SELECT to_regclass('public.schema_migrations')").fetchone()[0] is None:
        return set()
    return {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}


def plan_init(admin_conninfo: str, db_name: str = DEFAULT_DB_NAME) -> InitPlan:
    """Salt okunur: neyin yapilacagini hesaplar."""
    check_identifier(db_name)
    plan = InitPlan(db_name=db_name)
    migrations = [name for name, _ in list_migrations()]
    with psycopg.connect(admin_conninfo, autocommit=True) as admin:
        plan.create_owner_role = not _role_exists(admin, OWNER_ROLE)
        plan.create_app_role = not _role_exists(admin, APP_ROLE)
        plan.create_database = not _db_exists(admin, db_name)
    if plan.create_database:
        plan.pending_migrations = migrations
    else:
        with psycopg.connect(with_overrides(admin_conninfo, dbname=db_name), autocommit=True) as conn:
            applied = _applied_migrations(conn)
        plan.pending_migrations = [m for m in migrations if m not in applied]
    return plan


def _ensure_roles(admin: psycopg.Connection, plan: InitPlan, app_password: str | None) -> None:
    if plan.create_owner_role:
        admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(OWNER_ROLE)))
    if plan.create_app_role:
        if not app_password:
            raise InitError(f"{APP_ROLE} rolu olusturulacak ama BIST_APP_PASSWORD verilmedi")
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} CONNECTION LIMIT 5").format(
                sql.Identifier(APP_ROLE), sql.Literal(app_password)
            )
        )
        # Paylasimli sunucuyu korumak icin sinirlar.
        admin.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = '60s'").format(sql.Identifier(APP_ROLE)))
        admin.execute(
            sql.SQL("ALTER ROLE {} SET idle_in_transaction_session_timeout = '120s'").format(
                sql.Identifier(APP_ROLE)
            )
        )


def _apply_migrations(conninfo: str, pending: list[str]) -> list[str]:
    by_name = dict(list_migrations())
    applied: list[str] = []
    with psycopg.connect(conninfo, autocommit=True) as conn:
        for name in pending:
            with conn.transaction():
                # Esanli calisan iki init'i serilestir.
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (zlib.crc32(b"bist_tahmin_migrations"),))
                conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(OWNER_ROLE)))
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
                if conn.execute("SELECT 1 FROM schema_migrations WHERE version = %s", (name,)).fetchone():
                    continue  # baska bir surec uygulamis
                conn.execute(by_name[name])
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (name,))
                applied.append(name)
    return applied


def run_init(
    admin_conninfo: str,
    db_name: str = DEFAULT_DB_NAME,
    app_password: str | None = None,
    dry_run: bool = False,
) -> InitPlan:
    """Rolleri, veritabanini ve migration'lari idempotent olarak kurar."""
    plan = plan_init(admin_conninfo, db_name)
    if dry_run or plan.is_noop:
        return plan
    with psycopg.connect(admin_conninfo, autocommit=True) as admin:
        _ensure_roles(admin, plan, app_password)
        if plan.create_database:
            admin.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(db_name), sql.Identifier(OWNER_ROLE))
            )
    _apply_migrations(with_overrides(admin_conninfo, dbname=db_name), plan.pending_migrations)
    return plan
