"""models.registry: champion/challenger yasam dongusu, tek champion garantisi (gercek Postgres, bist_app yetkisi)."""

from __future__ import annotations

import datetime as dt
import threading
import uuid

import psycopg
import pytest
from conftest import APP_PASSWORD

from bist_tahmin.db import APP_ROLE, run_init, with_overrides
from bist_tahmin.models import registry as reg

UTC = dt.timezone.utc
END = dt.date(2026, 10, 1)


@pytest.fixture()
def db(admin_conninfo):
    """Taze izole veritabani + bist_app baglantisi (bist_app yetkileriyle)."""
    name = f"bist_models_{uuid.uuid4().hex[:8]}"
    run_init(admin_conninfo, db_name=name, app_password=APP_PASSWORD)
    info = with_overrides(admin_conninfo, dbname=name, user=APP_ROLE, password=APP_PASSWORD)
    with psycopg.connect(info, autocommit=True) as conn:
        yield conn, info


def _reg(conn, v, role="challenger", hour=0, **kw):
    return reg.register_model(conn, v, {"kind": "logreg_v1", "n": 1}, END, role=role,
                              trained_at=dt.datetime(2026, 11, 1, hour, tzinfo=UTC), **kw)


def _roles(conn):
    return dict(conn.execute("SELECT version, role FROM models").fetchall())


def test_register_and_get_roundtrip(db):
    conn, _ = db
    rec = _reg(conn, "m1")
    assert rec == reg.get_model(conn, "m1")
    assert rec.role == "challenger" and rec.train_end == END and rec.params == {"kind": "logreg_v1", "n": 1}
    assert rec.trained_at == dt.datetime(2026, 11, 1, tzinfo=UTC)
    assert reg.get_model(conn, "yok") is None


def test_register_duplicate_raises_and_does_not_overwrite(db):
    conn, _ = db
    _reg(conn, "m1")
    with pytest.raises(ValueError, match="zaten kayitli"):
        reg.register_model(conn, "m1", {"degisti": True}, dt.date(2020, 1, 1))
    assert reg.get_model(conn, "m1").params == {"kind": "logreg_v1", "n": 1}


def test_register_validation(db):
    conn, _ = db
    with pytest.raises(ValueError):
        reg.register_model(conn, "", {}, END)
    with pytest.raises(ValueError, match="rolu"):
        reg.register_model(conn, "x", {}, END, role="retired")
    with pytest.raises(ValueError, match="JSON"):
        reg.register_model(conn, "x", {"a": float("nan")}, END)
    with pytest.raises(ValueError, match="tz-aware"):
        reg.register_model(conn, "x", {}, END, trained_at=dt.datetime(2026, 1, 1))
    assert reg.list_models(conn) == []


def test_list_and_active_filters_ordered_by_trained_at(db):
    conn, _ = db
    _reg(conn, "b", hour=3)
    _reg(conn, "a", hour=1)
    _reg(conn, "c", hour=2)
    reg.retire(conn, "c")
    assert [m.version for m in reg.list_models(conn)] == ["a", "c", "b"]
    assert [m.version for m in reg.list_models(conn, ["retired"])] == ["c"]
    assert [m.version for m in reg.load_active_models(conn)] == ["a", "b"]
    assert reg.list_models(conn, []) == []
    with pytest.raises(ValueError):
        reg.list_models(conn, ["bogus"])


def test_promote_swaps_champion_atomically(db):
    conn, _ = db
    _reg(conn, "c1")
    _reg(conn, "c2")
    _reg(conn, "c3")
    rec = reg.promote(conn, "c1")
    assert rec.role == "champion" and rec.version == "c1"
    assert _roles(conn) == {"c1": "champion", "c2": "challenger", "c3": "challenger"}
    rec2 = reg.promote(conn, "c2")
    assert rec2.role == "champion"
    assert _roles(conn) == {"c1": "retired", "c2": "champion", "c3": "challenger"}


def test_invalid_transitions_raise(db):
    conn, _ = db
    _reg(conn, "c1")
    _reg(conn, "c2")
    reg.promote(conn, "c1")
    with pytest.raises(ValueError, match="yalniz challenger"):
        reg.promote(conn, "c1")          # champion zaten
    reg.promote(conn, "c2")              # c1 retired
    with pytest.raises(ValueError, match="yalniz challenger"):
        reg.promote(conn, "c1")          # retired terminal
    with pytest.raises(ValueError, match="terminal"):
        reg.retire(conn, "c1")
    with pytest.raises(ValueError, match="model yok"):
        reg.promote(conn, "yok")
    with pytest.raises(ValueError, match="model yok"):
        reg.retire(conn, "yok")
    assert _roles(conn) == {"c1": "retired", "c2": "champion"}


def test_failed_promote_changes_nothing(db):
    conn, _ = db
    _reg(conn, "c1")
    reg.promote(conn, "c1")
    before = _roles(conn)
    with pytest.raises(ValueError):
        reg.promote(conn, "yok")
    assert _roles(conn) == before


def test_retire_champion_leaves_no_champion(db):
    conn, _ = db
    _reg(conn, "c1")
    reg.promote(conn, "c1")
    reg.retire(conn, "c1")
    assert _roles(conn) == {"c1": "retired"}
    _reg(conn, "c2")
    assert reg.promote(conn, "c2").role == "champion"


def test_register_champion_only_when_none_exists(db):
    conn, _ = db
    assert _reg(conn, "first", role="champion").role == "champion"
    with pytest.raises(ValueError, match="champion"):
        _reg(conn, "second", role="champion")
    assert list(_roles(conn)) == ["first"]


def test_other_fields_unchanged_by_lifecycle(db):
    conn, _ = db
    rec = _reg(conn, "c1")
    reg.promote(conn, "c1")
    after = reg.get_model(conn, "c1")
    assert (after.version, after.trained_at, after.train_end, after.params) == \
        (rec.version, rec.trained_at, rec.train_end, rec.params)


def test_works_inside_caller_transaction_and_rolls_back(db):
    conn, _ = db
    _reg(conn, "c1")
    with pytest.raises(RuntimeError):
        with conn.transaction():
            reg.promote(conn, "c1")
            raise RuntimeError("geri al")
    assert _roles(conn) == {"c1": "challenger"}


def test_concurrent_promotes_leave_exactly_one_champion(db):
    conn, info = db
    for i in range(6):
        _reg(conn, f"c{i}")
    errors: list[Exception] = []

    def worker(v):
        try:
            with psycopg.connect(info, autocommit=True) as c:
                reg.promote(c, v)
        except Exception as exc:  # noqa: BLE001 - sonuc sayilacak
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"c{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    roles = list(_roles(conn).values())
    assert roles.count("champion") == 1, roles
    # kazanandan sonra gelenler eski champion'i emekli eder; kimse hata almamali (hepsi challenger idi)
    assert not errors, errors
    assert roles.count("retired") == 5
