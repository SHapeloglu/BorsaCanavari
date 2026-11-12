"""007_champion_guard.sql: tek champion (kismi unique index), champion yalniz promote ile, devre kesici durum korumasi.

Gercek Postgres; her test kendi taze veritabaninda calisir (paylasilan oturum veritabani durumu sizdirirdi).
"""

from __future__ import annotations

import datetime as dt
import uuid

import psycopg
import pytest
from conftest import APP_PASSWORD
from psycopg import errors
from psycopg.types.json import Jsonb

from bist_tahmin.db import APP_ROLE, list_migrations, run_init, with_overrides
from bist_tahmin.models import registry as reg
from bist_tahmin.monitor import breaker
from bist_tahmin.monitor.breaker import BreakerVerdict

END = dt.date(2026, 10, 1)


@pytest.fixture()
def fresh(admin_conninfo):
    name = f"bist_m7_{uuid.uuid4().hex[:8]}"
    run_init(admin_conninfo, db_name=name, app_password=APP_PASSWORD)
    return admin_conninfo, name


@pytest.fixture()
def app(fresh):
    admin, name = fresh
    info = with_overrides(admin, dbname=name, user=APP_ROLE, password=APP_PASSWORD)
    with psycopg.connect(info, autocommit=True) as conn:
        yield conn


@pytest.fixture()
def owner(fresh):
    admin, name = fresh
    with psycopg.connect(with_overrides(admin, dbname=name), autocommit=True) as conn:
        conn.execute("SET ROLE bist_owner")
        yield conn


def _model(conn, version, role="challenger"):
    conn.execute(
        "INSERT INTO models (version, trained_at, train_end, params_json, role)"
        " VALUES (%s, now(), %s, '{}'::jsonb, %s)",
        (version, END, role),
    )


def _roles(conn):
    return dict(conn.execute("SELECT version, role FROM models").fetchall())


# --- migration ve tek champion indexi -----------------------------------------------------------------------------

@pytest.mark.fast
def test_007_is_listed_after_006():
    names = [n for n, _ in list_migrations()]
    assert "007_champion_guard.sql" in names and names.index("007_champion_guard.sql") > names.index(
        "006_system_state.sql")


def test_one_champion_index_is_partial_unique_on_champion(app):
    row = app.execute(
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'models' AND indexname = 'models_one_champion'"
    ).fetchone()
    assert row is not None
    assert "UNIQUE" in row[0] and "WHERE" in row[0] and "'champion'" in row[0]


def test_unique_index_blocks_second_champion_even_for_owner(owner):
    """Tetikleyici ve indeks birlikte: ikinci champion, sahip rolunde bile (ayar acik olsa) unique ihlali."""
    with owner.transaction():
        owner.execute("SET LOCAL app.allow_promote = 'on'")
        _model(owner, "c1", role="champion")
    _model(owner, "c2")
    with pytest.raises(errors.UniqueViolation):
        with owner.transaction():
            owner.execute("SET LOCAL app.allow_promote = 'on'")
            owner.execute("UPDATE models SET role = 'champion' WHERE version = 'c2'")
    assert _roles(owner) == {"c1": "champion", "c2": "challenger"}


# --- champion yalniz promote ile -----------------------------------------------------------------------------------

def test_bist_app_direct_champion_update_rejected(app):
    _model(app, "c1")
    with pytest.raises(errors.RestrictViolation):
        app.execute("UPDATE models SET role = 'champion' WHERE version = 'c1'")
    assert _roles(app) == {"c1": "challenger"}


def test_bist_app_direct_champion_insert_rejected(app):
    with pytest.raises(errors.RestrictViolation):
        _model(app, "c1", role="champion")
    assert _roles(app) == {}


def test_champion_via_promote_is_the_only_app_path(app):
    _model(app, "c1")
    _model(app, "c2")
    assert reg.promote(app, "c1").role == "champion"
    with pytest.raises(errors.RestrictViolation):
        app.execute("UPDATE models SET role = 'champion' WHERE version = 'c2'")
    assert _roles(app) == {"c1": "champion", "c2": "challenger"}


def test_illegal_role_transitions_rejected_for_bist_app(app):
    _model(app, "c1")
    _model(app, "c2")
    reg.promote(app, "c1")  # c1 champion
    with pytest.raises(errors.RestrictViolation):
        app.execute("UPDATE models SET role = 'challenger' WHERE version = 'c1'")  # champion -> challenger
    app.execute("UPDATE models SET role = 'retired' WHERE version = 'c2'")  # challenger -> retired: serbest
    with pytest.raises(errors.RestrictViolation):
        app.execute("UPDATE models SET role = 'challenger' WHERE version = 'c2'")  # retired terminal
    with pytest.raises(errors.RestrictViolation):
        app.execute("UPDATE models SET role = 'champion' WHERE version = 'c2'")  # retired -> champion
    app.execute("UPDATE models SET role = 'retired' WHERE version = 'c1'")  # champion -> retired (retire): serbest
    assert _roles(app) == {"c1": "retired", "c2": "retired"}


# --- system_state: circuit_breaker korumalari -------------------------------------------------------------------

def _verdict(detail="test: goreli dusus asildi"):
    return BreakerVerdict(tripped=True, relative_drawdown=0.2, detail=detail)


def _breaker_value(app):
    return breaker.status(app)


def _write_breaker(conn, value):
    conn.execute("UPDATE system_state SET value = %s WHERE key = 'circuit_breaker'", (Jsonb(value),))


def test_legit_trip_reset_and_repeat_reset_work(app):
    _model(app, "m1")
    assert breaker.trip(app, _verdict(), "m1") is True
    assert breaker.trip(app, _verdict("baska"), "m1") is False  # acikken degismez
    assert _breaker_value(app)["reason"] == "test: goreli dusus asildi"
    breaker.reset(app, "analiz tamam")
    st = _breaker_value(app)
    assert st["tripped"] is False and len(st["history"]) == 1
    breaker.reset(app, "kapaliyken ikinci kayit")  # cli: zaten kapaliyken de kayit eklenir
    assert len(_breaker_value(app)["history"]) == 2


def test_history_cannot_be_deleted_or_rewritten(app):
    _model(app, "m1")
    breaker.trip(app, _verdict(), "m1")
    breaker.reset(app, "ilk sifirlama")
    with pytest.raises(errors.RestrictViolation):
        app.execute("UPDATE system_state SET value = jsonb_set(value, '{history}', '[]'::jsonb)"
                    " WHERE key = 'circuit_breaker'")
    with pytest.raises(errors.RestrictViolation):
        app.execute("UPDATE system_state SET value = jsonb_set(value, '{history,0,reset_reason}', '\"yok\"'::jsonb)"
                    " WHERE key = 'circuit_breaker'")
    assert len(_breaker_value(app)["history"]) == 1


def test_reset_without_reason_is_rejected(app):
    _model(app, "m1")
    breaker.trip(app, _verdict(), "m1")
    st = _breaker_value(app)
    entry = {"tripped": True, "since": st["since"], "reason": st["reason"], "model_version": "m1",
             "relative_drawdown": 0.2, "reset_at": "2026-11-10T00:00:00+00:00", "reset_reason": "  "}
    new = {**st, "tripped": False, "reset_reason": "  ", "history": [*st["history"], entry]}
    with pytest.raises(errors.RestrictViolation):
        _write_breaker(app, new)
    assert _breaker_value(app)["tripped"] is True


def test_reset_must_append_history_entry(app):
    _model(app, "m1")
    breaker.trip(app, _verdict(), "m1")
    st = _breaker_value(app)
    # gerekce dolu ama history'ye kayit eklenmemis: reset sayilmaz
    new = {**st, "tripped": False, "reset_reason": "gecerli gerekce"}
    with pytest.raises(errors.RestrictViolation):
        _write_breaker(app, new)


def test_trip_requires_reason(app):
    breaker.reset(app, "ilk kayit")  # satir olussun, kapali
    st = _breaker_value(app)
    with pytest.raises(errors.RestrictViolation):
        _write_breaker(app, {**st, "tripped": True, "reason": "", "since": "2026-11-10T00:00:00+00:00"})
    assert _breaker_value(app)["tripped"] is False


def test_open_breaker_cannot_be_silently_changed(app):
    _model(app, "m1")
    breaker.trip(app, _verdict(), "m1")
    st = _breaker_value(app)
    with pytest.raises(errors.RestrictViolation):
        _write_breaker(app, {**st, "since": "2000-01-01T00:00:00+00:00"})
    with pytest.raises(errors.RestrictViolation):
        _write_breaker(app, {**st, "reason": "baska gerekce"})
    assert _breaker_value(app)["since"] == st["since"]


def test_breaker_row_cannot_be_deleted_or_renamed(app, owner):
    breaker.reset(app, "ilk kayit")
    with pytest.raises(errors.InsufficientPrivilege):
        app.execute("DELETE FROM system_state WHERE key = 'circuit_breaker'")
    with pytest.raises(errors.RestrictViolation):
        app.execute("UPDATE system_state SET key = 'x' WHERE key = 'circuit_breaker'")
    with pytest.raises(errors.RestrictViolation):  # sahip rolu de: tetikleyici
        owner.execute("DELETE FROM system_state WHERE key = 'circuit_breaker'")
    with pytest.raises(errors.RestrictViolation):
        owner.execute("TRUNCATE system_state")
    assert _breaker_value(app)["history"] != []


def test_other_system_state_keys_unaffected(app):
    app.execute("INSERT INTO system_state (key, value) VALUES ('k1', '{\"a\": 1}'::jsonb)")
    app.execute("UPDATE system_state SET value = '{\"a\": 2}'::jsonb WHERE key = 'k1'")
    assert app.execute("SELECT value FROM system_state WHERE key = 'k1'").fetchone()[0] == {"a": 2}
