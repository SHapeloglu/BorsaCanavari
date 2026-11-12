"""`bist init`: idempotans, dry-run ve baglanti kisitlari."""

import uuid

import psycopg
import pytest
from psycopg import errors

from bist_tahmin import cli
from bist_tahmin.db import InitError, list_migrations, plan_init, run_init, with_overrides


@pytest.mark.fast
def test_migrations_are_ordered_and_present():
    names = [n for n, _ in list_migrations()]
    assert names == sorted(names) and len(names) >= 3
    assert names[0].startswith("001_")


@pytest.mark.fast
def test_init_is_idempotent(bist_db):
    admin, name = bist_db
    plan = plan_init(admin, name)
    assert plan.is_noop
    again = run_init(admin, db_name=name)  # parola gerekmez, zaten kurulu
    assert again.is_noop


@pytest.mark.fast
def test_dry_run_changes_nothing(admin_conninfo):
    name = f"bist_dry_{uuid.uuid4().hex[:8]}"
    plan = run_init(admin_conninfo, db_name=name, dry_run=True)
    assert plan.create_database and plan.pending_migrations
    with psycopg.connect(admin_conninfo, autocommit=True) as conn:
        assert conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone() is None


def test_new_database_requires_password_only_if_role_missing(admin_conninfo, bist_db):
    # bist_db fixture'i app rolunu zaten olusturdu -> yeni DB icin parola gerekmez
    name = f"bist_t2_{uuid.uuid4().hex[:8]}"
    plan = run_init(admin_conninfo, db_name=name)
    assert plan.create_database and not plan.create_app_role
    _, first = bist_db
    assert first != name


@pytest.mark.fast
def test_invalid_db_name_rejected(admin_conninfo):
    with pytest.raises(InitError):
        plan_init(admin_conninfo, "bist; DROP DATABASE postgres")


@pytest.mark.fast
def test_other_roles_cannot_connect(bist_db):
    """Paylasimli sunucudaki baska bir kullanici bu veritabanina baglanamaz."""
    admin, name = bist_db
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute("DROP ROLE IF EXISTS bist_outsider")
        conn.execute("CREATE ROLE bist_outsider LOGIN PASSWORD 'x'")
    try:
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(with_overrides(admin, dbname=name, user="bist_outsider", password="x")).close()
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute("DROP ROLE bist_outsider")


@pytest.mark.fast
def test_cli_init_requires_admin_url(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_ADMIN_URL", raising=False)
    assert cli.main(["init", "--dry-run"]) == 2
    assert "DATABASE_ADMIN_URL" in capsys.readouterr().err


@pytest.mark.fast
def test_cli_init_dry_run(monkeypatch, admin_conninfo, capsys):
    monkeypatch.setenv("DATABASE_ADMIN_URL", admin_conninfo)
    name = f"bist_cli_{uuid.uuid4().hex[:8]}"
    assert cli.main(["--config", "config/default.yaml", "init", "--dry-run", "--db-name", name]) == 0
    out = capsys.readouterr().out
    assert "[dry-run]" in out and "001_schema.sql" in out
