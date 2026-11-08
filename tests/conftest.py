"""Test altyapisi: gercek PostgreSQL (tetikleyici ve yetki testleri SQLite'ta ayni sonucu vermez).

- BIST_TEST_ADMIN_URL verilirse (ornegin docker-compose.test.yml) o sunucu kullanilir.
- Verilmezse `pgserver` ile gomulu gecici bir Postgres baslatilir.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from bist_tahmin.db import APP_ROLE, run_init, with_overrides

APP_PASSWORD = "test-app-password"


@pytest.fixture(scope="session")
def admin_conninfo(tmp_path_factory):
    url = os.environ.get("BIST_TEST_ADMIN_URL")
    if url:
        yield url
        return
    pgserver = pytest.importorskip("pgserver")
    srv = pgserver.get_server(tmp_path_factory.mktemp("pg"), cleanup_mode="delete")
    try:
        yield srv.get_uri()
    finally:
        srv.cleanup()


@pytest.fixture(scope="session")
def bist_db(admin_conninfo):
    """Kurulmus (init edilmis) test veritabani; (admin_conninfo, db_name) dondurur."""
    name = f"bist_test_{uuid.uuid4().hex[:8]}"
    run_init(admin_conninfo, db_name=name, app_password=APP_PASSWORD)
    return admin_conninfo, name


@pytest.fixture()
def app_conn(bist_db):
    """bist_app rolu ile baglanti (uygulamanin gordugu yetkiler)."""
    admin, name = bist_db
    info = with_overrides(admin, dbname=name, user=APP_ROLE, password=APP_PASSWORD)
    with psycopg.connect(info, autocommit=True) as conn:
        yield conn


@pytest.fixture()
def owner_conn(bist_db):
    """Tablo sahibi (bist_owner) yetkisiyle baglanti: tetikleyicilerin sahibe de uygulandigini dogrular."""
    admin, name = bist_db
    with psycopg.connect(with_overrides(admin, dbname=name), autocommit=True) as conn:
        conn.execute("SET ROLE bist_owner")
        yield conn


@pytest.fixture()
def new_prediction():
    """Benzersiz ticker'li, gecerli bir tahmin satiri parametre sozlugu uretir."""

    def make(**overrides):
        row = {
            "ticker": f"T{uuid.uuid4().hex[:8].upper()}",
            "made_at": "2026-11-02",
            "horizon_days": 5,
            "prob_up": 0.55,
            "score": 0.1,
            "model_version": "baseline_base_rate",
            "model_role": "baseline",
            "feature_hash": "abc123",
            "features_json": "{}",
        }
        row.update(overrides)
        return row

    return make


INSERT_SQL = (
    "INSERT INTO predictions (ticker, made_at, horizon_days, prob_up, score, model_version, model_role,"
    " feature_hash, features_json, created_at) VALUES (%(ticker)s, %(made_at)s, %(horizon_days)s,"
    " %(prob_up)s, %(score)s, %(model_version)s, %(model_role)s, %(feature_hash)s,"
    " %(features_json)s::jsonb, now()) ON CONFLICT DO NOTHING RETURNING id"
)


@pytest.fixture()
def insert_prediction():
    def insert(conn, row):
        cur = conn.execute(INSERT_SQL, row)
        found = cur.fetchone()
        return found[0] if found else None

    return insert
