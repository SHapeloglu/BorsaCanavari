"""PROJE.md Bolum 13: tahmin defteri append-only. Gercek Postgres uzerinde, iki savunma hattiyla:
1) bist_app'in yetkileri (sutun duzeyinde UPDATE, DELETE/TRUNCATE yok)
2) tetikleyici (tablo sahibi dahil herkes icin)
"""

import psycopg
import pytest
from psycopg import errors


def _resolve(conn, pid, resolved_at="2026-11-09", ret=0.02, label=1):
    """Alti cozum alaninin hepsini yazar (005: kismi cozum yok). new_prediction: made_at 2026-11-02, H=5."""
    return conn.execute(
        "UPDATE predictions SET resolved_at=%s, realized_ret=%s, realized_rel_ret=%s, tradable_ret=%s,"
        " label_up=%s, label_rel_up=%s WHERE id=%s",
        (resolved_at, ret, ret / 2, ret / 3, label, label, pid),
    )


# --- Yetki katmani (bist_app) -------------------------------------------------

@pytest.mark.parametrize("column,value", [
    ("prob_up", "0.9"),
    ("score", "0.9"),
    ("feature_hash", "'zzz'"),
    ("features_json", "'{\"a\": 1}'::jsonb"),
    ("made_at", "'2026-01-01'"),
    ("model_version", "'x'"),
])
def test_app_cannot_update_prediction_fields(app_conn, new_prediction, insert_prediction, column, value):
    pid = insert_prediction(app_conn, new_prediction())
    with pytest.raises(errors.InsufficientPrivilege):
        app_conn.execute(f"UPDATE predictions SET {column} = {value} WHERE id = %s", (pid,))


def test_app_cannot_delete_or_truncate(app_conn, new_prediction, insert_prediction):
    pid = insert_prediction(app_conn, new_prediction())
    with pytest.raises(errors.InsufficientPrivilege):
        app_conn.execute("DELETE FROM predictions WHERE id = %s", (pid,))
    with pytest.raises(errors.InsufficientPrivilege):
        app_conn.execute("TRUNCATE predictions")


def test_app_cannot_drop_or_disable_guard(app_conn):
    with pytest.raises(errors.InsufficientPrivilege):
        app_conn.execute("DROP TRIGGER predictions_guard ON predictions")
    with pytest.raises(errors.InsufficientPrivilege):
        app_conn.execute("ALTER TABLE predictions DISABLE TRIGGER predictions_guard")


# --- Tetikleyici katmani (tablo sahibi bile degistiremez) ------------------------

def test_owner_still_blocked_by_trigger(owner_conn, new_prediction, insert_prediction):
    pid = insert_prediction(owner_conn, new_prediction())
    with pytest.raises(errors.RestrictViolation):
        owner_conn.execute("UPDATE predictions SET prob_up = 0.9 WHERE id = %s", (pid,))
    with pytest.raises(errors.RestrictViolation):
        owner_conn.execute("UPDATE predictions SET features_json = '{\"a\":1}'::jsonb WHERE id = %s", (pid,))
    with pytest.raises(errors.RestrictViolation):
        owner_conn.execute("DELETE FROM predictions WHERE id = %s", (pid,))
    with pytest.raises(errors.RestrictViolation):
        owner_conn.execute("TRUNCATE predictions")


# --- Cozum: tek seferlik -----------------------------------------------------------

def test_resolution_is_write_once(app_conn, new_prediction, insert_prediction):
    pid = insert_prediction(app_conn, new_prediction())
    _resolve(app_conn, pid)
    row = app_conn.execute("SELECT resolved_at, realized_ret, label_up FROM predictions WHERE id=%s", (pid,)).fetchone()
    assert str(row[0]) == "2026-11-09" and row[1] == 0.02 and row[2] == 1
    with pytest.raises(errors.RestrictViolation):
        _resolve(app_conn, pid, ret=-0.05, label=0)


def test_resolution_requires_complete_fields_and_later_date(app_conn, new_prediction, insert_prediction):
    pid = insert_prediction(app_conn, new_prediction())
    # eksik alan: CHECK ((resolved_at IS NULL) = (label_up IS NULL)) ya da tetikleyici reddeder
    with pytest.raises((errors.RestrictViolation, errors.CheckViolation)):
        app_conn.execute("UPDATE predictions SET resolved_at='2026-11-09' WHERE id=%s", (pid,))
    with pytest.raises((errors.RestrictViolation, errors.CheckViolation)):
        app_conn.execute("UPDATE predictions SET label_up=1 WHERE id=%s", (pid,))
    # made_at'ten once ya da ayni gun cozum olmaz
    with pytest.raises(errors.RestrictViolation):
        _resolve(app_conn, pid, resolved_at="2026-11-02")
    with pytest.raises(errors.RestrictViolation):
        _resolve(app_conn, pid, resolved_at="2026-10-30")


_SIX_FIELDS = ["resolved_at", "realized_ret", "realized_rel_ret", "tradable_ret", "label_up", "label_rel_up"]
_SIX_VALUES = {"resolved_at": "'2026-11-09'", "realized_ret": "0.02", "realized_rel_ret": "0.01",
               "tradable_ret": "0.005", "label_up": "1", "label_rel_up": "1"}


@pytest.mark.parametrize("missing", _SIX_FIELDS)
def test_resolution_requires_all_six_fields(app_conn, new_prediction, insert_prediction, missing):
    pid = insert_prediction(app_conn, new_prediction())
    sets = ", ".join(f"{c}={v}" for c, v in _SIX_VALUES.items() if c != missing)
    with pytest.raises((errors.RestrictViolation, errors.CheckViolation)):
        app_conn.execute(f"UPDATE predictions SET {sets} WHERE id = %s", (pid,))
    # reddedilen yazim hicbir sey birakmaz; tam cozum hala yazilabilir
    assert app_conn.execute("SELECT resolved_at FROM predictions WHERE id=%s", (pid,)).fetchone()[0] is None
    _resolve(app_conn, pid)


def test_early_resolution_rejected_but_due_accepted(app_conn, new_prediction, insert_prediction):
    pid = insert_prediction(app_conn, new_prediction())  # made_at 2026-11-02, H=5 -> en erken 2026-11-07
    with pytest.raises(errors.RestrictViolation):
        _resolve(app_conn, pid, resolved_at="2026-11-03")
    with pytest.raises(errors.RestrictViolation):
        _resolve(app_conn, pid, resolved_at="2026-11-06")
    assert app_conn.execute("SELECT resolved_at FROM predictions WHERE id=%s", (pid,)).fetchone()[0] is None
    _resolve(app_conn, pid, resolved_at="2026-11-07")  # tam made_at + H takvim gunu: kabul
    assert str(app_conn.execute("SELECT resolved_at FROM predictions WHERE id=%s", (pid,)).fetchone()[0]) == "2026-11-07"


def test_insert_cannot_carry_resolution(app_conn, new_prediction):
    row = new_prediction()
    with pytest.raises((errors.RestrictViolation, errors.CheckViolation)):
        app_conn.execute(
            "INSERT INTO predictions (ticker, made_at, horizon_days, prob_up, model_version, model_role,"
            " feature_hash, features_json, created_at, resolved_at, realized_ret, label_up)"
            " VALUES (%s,'2026-11-02',5,0.5,'m','baseline','h','{}'::jsonb, now(),'2026-11-09',0.01,1)",
            (row["ticker"],),
        )


# --- Idempotans ve girdi dogrulama -----------------------------------------------------

def test_duplicate_insert_is_idempotent(app_conn, new_prediction, insert_prediction):
    row = new_prediction()
    first = insert_prediction(app_conn, row)
    second = insert_prediction(app_conn, row)  # ON CONFLICT DO NOTHING
    assert first is not None and second is None
    count = app_conn.execute("SELECT count(*) FROM predictions WHERE ticker=%s", (row["ticker"],)).fetchone()[0]
    assert count == 1


@pytest.mark.parametrize("bad", [-0.01, 1.01])
def test_prob_up_must_be_probability(app_conn, new_prediction, insert_prediction, bad):
    with pytest.raises(errors.CheckViolation):
        insert_prediction(app_conn, new_prediction(prob_up=bad))


def test_app_role_has_safety_limits(bist_db):
    admin, _ = bist_db
    with psycopg.connect(admin, autocommit=True) as conn:
        cfg = conn.execute("SELECT rolconfig, rolconnlimit FROM pg_roles WHERE rolname='bist_app'").fetchone()
    assert any(c.startswith("statement_timeout=") for c in cfg[0])
    assert cfg[1] == 5


# --- 005: guvenilir kayit zamani ve ileri tarih yasagi ---------------------------------------

def _raw_insert(conn, ticker, made_at, extra_cols="", extra_vals=""):
    return conn.execute(
        "INSERT INTO predictions (ticker, made_at, horizon_days, prob_up, model_version, model_role,"
        f" feature_hash, features_json, created_at{extra_cols})"
        f" VALUES (%s,%s,5,0.5,'m','baseline','h','{{}}'::jsonb,'2020-01-02 19:00+03'{extra_vals}) RETURNING id",
        (ticker, made_at),
    ).fetchone()[0]


def test_recorded_at_is_db_clock_and_cannot_be_forged(app_conn, new_prediction):
    import datetime as dt
    ticker = new_prediction()["ticker"]
    before = dt.datetime.now(dt.timezone.utc)
    pid = _raw_insert(app_conn, ticker, "2026-11-02", ", recorded_at", ",'2020-01-02 19:00+03'")
    after = dt.datetime.now(dt.timezone.utc)
    rec, created = app_conn.execute("SELECT recorded_at, created_at FROM predictions WHERE id=%s", (pid,)).fetchone()
    assert before - dt.timedelta(seconds=5) <= rec <= after + dt.timedelta(seconds=5)   # gonderilen 2020 degeri degil
    assert created.year == 2020                                                          # created_at serbest kalir


def test_recorded_at_defaults_without_being_sent(app_conn, new_prediction, insert_prediction):
    pid = insert_prediction(app_conn, new_prediction())
    assert app_conn.execute("SELECT recorded_at IS NOT NULL FROM predictions WHERE id=%s", (pid,)).fetchone()[0]


def test_future_made_at_rejected(app_conn, new_prediction):
    ticker = new_prediction()["ticker"]
    with pytest.raises(errors.RestrictViolation):
        _raw_insert(app_conn, ticker, "2999-01-01")
    assert app_conn.execute("SELECT count(*) FROM predictions WHERE ticker=%s", (ticker,)).fetchone()[0] == 0
    # bugunun (Istanbul) tarihi serbesttir
    today = app_conn.execute("SELECT (now() AT TIME ZONE 'Europe/Istanbul')::date").fetchone()[0]
    assert _raw_insert(app_conn, ticker, today) is not None


def test_recorded_at_is_immutable(app_conn, owner_conn, new_prediction, insert_prediction):
    pid = insert_prediction(app_conn, new_prediction())
    with pytest.raises(errors.InsufficientPrivilege):
        app_conn.execute("UPDATE predictions SET recorded_at = '2020-01-01' WHERE id = %s", (pid,))
    with pytest.raises(errors.RestrictViolation):  # tablo sahibi bile degistiremez
        owner_conn.execute("UPDATE predictions SET recorded_at = '2020-01-01' WHERE id = %s", (pid,))


def test_migration_005_applied_once_and_idempotent(bist_db):
    from bist_tahmin.db import plan_init, run_init, with_overrides
    admin, name = bist_db
    assert plan_init(admin, name).is_noop
    assert run_init(admin, db_name=name).is_noop
    with psycopg.connect(with_overrides(admin, dbname=name), autocommit=True) as conn:
        n = conn.execute("SELECT count(*) FROM schema_migrations WHERE version='005_ledger_hardening.sql'").fetchone()[0]
        col = conn.execute("SELECT is_nullable, column_default FROM information_schema.columns"
                           " WHERE table_name='predictions' AND column_name='recorded_at'").fetchone()
    assert n == 1 and col[0] == "NO" and "clock_timestamp" in col[1]
