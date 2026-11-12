"""Deney kaydi (gercek Postgres, bist_app yetkileri). Her test benzersiz aile adi kullanir."""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg
import pytest

from bist_tahmin.backtest.registry import (
    adjusted_alpha,
    count_trials,
    list_experiments,
    record_experiment,
    update_result,
)
from bist_tahmin.db import APP_ROLE, with_overrides


@pytest.fixture()
def family():
    return f"fam_{uuid.uuid4().hex[:10]}"


@pytest.mark.fast
def test_record_returns_id_and_row_has_null_result(app_conn, family):
    eid = record_experiment(app_conn, family, "ilk deneme", {"lr": 0.1, "feat": ["a", "b"]})
    assert isinstance(eid, int)
    (row,) = list_experiments(app_conn, family)
    assert row["id"] == eid and row["family"] == family and row["description"] == "ilk deneme"
    assert row["params_json"] == {"lr": 0.1, "feat": ["a", "b"]}
    assert row["result_json"] is None
    assert row["created_at"].tzinfo is not None


@pytest.mark.fast
def test_created_at_explicit_and_naive_treated_as_utc(app_conn, family):
    stamp = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc)
    record_experiment(app_conn, family, "a", {}, created_at=stamp)
    record_experiment(app_conn, family, "b", {}, created_at=dt.datetime(2026, 11, 9, 12, 0))
    a, b = list_experiments(app_conn, family)
    assert a["created_at"] == stamp
    assert b["created_at"] == dt.datetime(2026, 11, 9, 12, 0, tzinfo=dt.timezone.utc)


@pytest.mark.fast
def test_json_conversion_of_non_json_types(app_conn, family):
    params = {
        "i": np.int64(3),
        "f": np.float32(0.5),
        "flag": np.bool_(True),
        "arr": np.array([1, 2, 3]),
        "ts": pd.Timestamp("2026-05-01 10:00"),
        "d": dt.date(2026, 5, 1),
        "when": dt.datetime(2026, 5, 1, 9, 30),
        "set": {3, 1, 2},
        "tup": (1, 2),
        "path": Path("a/b"),
        "nan": float("nan"),
        "inf": np.float64("inf"),
        "none": None,
        "nested": {np.int64(7): [np.float64(1.5), {"x": pd.Timestamp("2026-01-02")}]},
        "dt64": np.datetime64("2026-05-01"),
        "idx": pd.DatetimeIndex(["2026-01-01"]),
    }
    eid = record_experiment(app_conn, family, "json", params)
    got = list_experiments(app_conn, family)[0]["params_json"]
    assert list_experiments(app_conn, family)[0]["id"] == eid
    assert got["i"] == 3 and got["f"] == 0.5 and got["flag"] is True
    assert got["arr"] == [1, 2, 3]
    assert got["ts"] == "2026-05-01T10:00:00"
    assert got["d"] == "2026-05-01" and got["when"] == "2026-05-01T09:30:00"
    assert got["set"] == [1, 2, 3] and got["tup"] == [1, 2] and got["path"] == "a/b"
    assert got["nan"] is None and got["inf"] is None and got["none"] is None
    assert got["nested"] == {"7": [1.5, {"x": "2026-01-02T00:00:00"}]}
    assert got["dt64"] == "2026-05-01T00:00:00"
    assert got["idx"] == ["2026-01-01T00:00:00"]


@pytest.mark.fast
def test_unserializable_value_raises_and_writes_nothing(app_conn, family):
    with pytest.raises(TypeError):
        record_experiment(app_conn, family, "x", {"obj": object()})
    assert count_trials(app_conn, family) == 0


@pytest.mark.fast
def test_params_must_be_mapping(app_conn, family):
    with pytest.raises(TypeError):
        record_experiment(app_conn, family, "x", [1, 2])  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["", "   ", None])
@pytest.mark.fast
def test_empty_family_rejected(app_conn, bad):
    with pytest.raises(ValueError):
        record_experiment(app_conn, bad, "x", {})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        count_trials(app_conn, bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        adjusted_alpha(app_conn, bad)  # type: ignore[arg-type]
    if bad is not None:  # None = "tum aileler"
        with pytest.raises(ValueError):
            list_experiments(app_conn, bad)


@pytest.mark.fast
def test_empty_description_rejected(app_conn, family):
    with pytest.raises(ValueError):
        record_experiment(app_conn, family, "  ", {})
    assert count_trials(app_conn, family) == 0


@pytest.mark.fast
def test_count_trials_counts_records_even_without_result_and_per_family(app_conn, family):
    other = family + "_other"
    assert count_trials(app_conn, family) == 0
    e1 = record_experiment(app_conn, family, "1", {})
    record_experiment(app_conn, family, "2", {})  # sonucu hic yazilmayan deneme de sayilir
    record_experiment(app_conn, other, "3", {})
    assert count_trials(app_conn, family) == 2
    update_result(app_conn, e1, {"brier": 0.25})
    assert count_trials(app_conn, family) == 2
    assert count_trials(app_conn, other) == 1


@pytest.mark.fast
def test_trials_cannot_be_deleted_by_app_role(app_conn, family):
    record_experiment(app_conn, family, "1", {})
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("DELETE FROM experiments WHERE family = %s", (family,))
    assert count_trials(app_conn, family) == 1


@pytest.mark.fast
def test_adjusted_alpha_known_n(app_conn, family):
    assert adjusted_alpha(app_conn, family) == 0.05  # 0 deneme -> max(1, 0)
    record_experiment(app_conn, family, "1", {})
    assert adjusted_alpha(app_conn, family) == pytest.approx(0.05)
    for i in range(3):
        record_experiment(app_conn, family, f"{i + 2}", {})
    assert count_trials(app_conn, family) == 4
    assert adjusted_alpha(app_conn, family) == pytest.approx(0.0125)
    assert adjusted_alpha(app_conn, family, alpha=0.1) == pytest.approx(0.025)
    with pytest.raises(ValueError):
        adjusted_alpha(app_conn, family, alpha=1.5)


@pytest.mark.fast
def test_update_result_only_once(app_conn, family):
    eid = record_experiment(app_conn, family, "1", {"a": 1})
    update_result(app_conn, eid, {"brier_diff": np.float64(-0.004), "n_days": np.int64(80)})
    (row,) = list_experiments(app_conn, family)
    assert row["result_json"] == {"brier_diff": -0.004, "n_days": 80}
    with pytest.raises(ValueError, match="zaten"):
        update_result(app_conn, eid, {"brier_diff": 0.1})
    (row,) = list_experiments(app_conn, family)
    assert row["result_json"] == {"brier_diff": -0.004, "n_days": 80}  # degismedi
    assert row["params_json"] == {"a": 1}


@pytest.mark.fast
def test_update_result_unknown_id_and_bad_result(app_conn, family):
    with pytest.raises(ValueError, match="yok"):
        update_result(app_conn, 2**40, {"a": 1})
    eid = record_experiment(app_conn, family, "1", {})
    with pytest.raises(TypeError):
        update_result(app_conn, eid, {"x": object()})
    update_result(app_conn, eid, {"ok": True})  # basarisiz deneme yuvayi tuketmedi


@pytest.mark.fast
def test_app_role_cannot_change_params_or_family(app_conn, family):
    eid = record_experiment(app_conn, family, "1", {"a": 1})
    for col, val in [("params_json", "{}"), ("family", "baska"), ("description", "x")]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app_conn.execute(f"UPDATE experiments SET {col} = %s WHERE id = %s", (val, eid))


@pytest.mark.fast
def test_list_experiments_order_and_filter(app_conn, family):
    ids = [record_experiment(app_conn, family, f"d{i}", {"i": i}) for i in range(3)]
    record_experiment(app_conn, family + "_x", "baska", {})
    rows = list_experiments(app_conn, family)
    assert [r["id"] for r in rows] == ids
    assert [r["params_json"]["i"] for r in rows] == [0, 1, 2]
    everything = list_experiments(app_conn)
    assert {r["id"] for r in rows} <= {r["id"] for r in everything}
    assert any(r["family"] == family + "_x" for r in everything)
    assert list_experiments(app_conn, family + "_yok") == []


@pytest.mark.fast
def test_concurrent_records_all_counted(bist_db, family):
    admin, name = bist_db
    info = with_overrides(admin, dbname=name, user=APP_ROLE, password="test-app-password")
    n_threads, per_thread = 3, 5  # bist_app CONNECTION LIMIT 5 (app_conn dahil)
    ids: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def work():
        try:
            with psycopg.connect(info, autocommit=True) as conn:
                for _ in range(per_thread):
                    eid = record_experiment(conn, family, "es", {"t": threading.get_ident()})
                    with lock:
                        ids.append(eid)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(set(ids)) == n_threads * per_thread
    with psycopg.connect(info, autocommit=True) as conn:
        assert count_trials(conn, family) == n_threads * per_thread


@pytest.mark.fast
def test_works_on_non_autocommit_connection_and_commits(bist_db, family):
    admin, name = bist_db
    info = with_overrides(admin, dbname=name, user=APP_ROLE, password="test-app-password")
    with psycopg.connect(info) as conn:  # autocommit kapali
        eid = record_experiment(conn, family, "1", {})
        update_result(conn, eid, {"r": 1})
    with psycopg.connect(info, autocommit=True) as other:  # baska baglanti kaydi gorur (commit edildi)
        assert count_trials(other, family) == 1
        assert list_experiments(other, family)[0]["result_json"] == {"r": 1}
