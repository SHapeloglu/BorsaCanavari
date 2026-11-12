"""Tahmin defteri yazicisi (PROJE.md Bolum 6, 12). Gercek Postgres; her test benzersiz ticker kullanir."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import uuid

import pytest

from bist_tahmin.predict.ledger import (
    PredictionRow,
    WriteResult,
    baseline_feature_hash,
    write_predictions,
)

MADE_AT = dt.date(2026, 11, 2)


def make_row(**overrides) -> PredictionRow:
    base = dict(
        ticker=f"L{uuid.uuid4().hex[:9].upper()}",
        made_at=MADE_AT,
        horizon_days=5,
        prob_up=0.55,
        score=0.1,
        model_version="baseline_ma20_v1",
        model_role="baseline",
        feature_hash="h1",
        features_json='{"rule":"ma20"}',
    )
    base.update(overrides)
    return PredictionRow(**base)


def fetch(conn, ticker):
    return conn.execute(
        "SELECT prob_up, score, model_version, model_role, feature_hash, features_json, created_at,"
        " resolved_at, label_up FROM predictions WHERE ticker = %s ORDER BY model_version",
        (ticker,),
    ).fetchall()


def count(conn, ticker) -> int:
    return conn.execute("SELECT count(*) FROM predictions WHERE ticker = %s", (ticker,)).fetchone()[0]


@pytest.mark.fast
def test_write_inserts_rows(app_conn):
    rows = [make_row(), make_row(prob_up=0.0, score=None, model_role="champion", model_version="logreg_v1")]
    res = write_predictions(app_conn, rows)
    assert res == WriteResult(inserted=2, skipped_existing=0, dry_run=False)
    got = fetch(app_conn, rows[0].ticker)
    assert len(got) == 1
    prob, score, version, role, fhash, fjson, created, resolved, label = got[0]
    assert (prob, score, version, role, fhash) == (0.55, 0.1, "baseline_ma20_v1", "baseline", "h1")
    assert fjson == {"rule": "ma20"}
    assert created.tzinfo is not None and abs((dt.datetime.now(dt.timezone.utc) - created).total_seconds()) < 120
    assert resolved is None and label is None
    got2 = fetch(app_conn, rows[1].ticker)
    assert got2[0][0] == 0.0 and got2[0][1] is None and got2[0][3] == "champion"


@pytest.mark.fast
def test_created_at_is_used_when_given(app_conn):
    row = make_row()
    ts = dt.datetime(2026, 11, 2, 16, 0, tzinfo=dt.timezone.utc)
    write_predictions(app_conn, [row], created_at=ts)
    assert fetch(app_conn, row.ticker)[0][6] == ts
    with pytest.raises(ValueError):
        write_predictions(app_conn, [make_row()], created_at=dt.datetime(2026, 11, 2, 16, 0))


@pytest.mark.fast
def test_idempotent_second_run(app_conn):
    rows = [make_row(), make_row()]
    first = write_predictions(app_conn, rows)
    second = write_predictions(app_conn, rows)
    assert first.inserted == 2 and first.skipped_existing == 0
    assert second.inserted == 0 and second.skipped_existing == 2
    assert [count(app_conn, r.ticker) for r in rows] == [1, 1]


@pytest.mark.fast
def test_partial_overlap_counts(app_conn):
    a, b = make_row(), make_row()
    write_predictions(app_conn, [a])
    res = write_predictions(app_conn, [a, b])
    assert (res.inserted, res.skipped_existing) == (1, 1)


@pytest.mark.fast
def test_duplicate_key_within_batch_inserts_first_only(app_conn):
    a = make_row(prob_up=0.3)
    dup = dataclasses.replace(a, prob_up=0.9)
    res = write_predictions(app_conn, [a, dup])
    assert (res.inserted, res.skipped_existing) == (1, 1)
    assert fetch(app_conn, a.ticker)[0][0] == 0.3
    # dry-run ayni sayilari verir
    b = make_row()
    assert write_predictions(app_conn, [b, dataclasses.replace(b, prob_up=0.1)], dry_run=True) == WriteResult(1, 1, True)


@pytest.mark.fast
def test_existing_prediction_fields_are_preserved(app_conn):
    original = make_row(prob_up=0.42, score=0.5, feature_hash="orig", features_json='{"a":1}')
    write_predictions(app_conn, [original])
    changed = dataclasses.replace(original, prob_up=0.9, score=-1.0, feature_hash="new", features_json='{"a":2}',
                                  model_role="champion")
    res = write_predictions(app_conn, [changed])
    assert (res.inserted, res.skipped_existing) == (0, 1)
    prob, score, _, role, fhash, fjson, *_ = fetch(app_conn, original.ticker)[0]
    assert (prob, score, role, fhash, fjson) == (0.42, 0.5, "baseline", "orig", {"a": 1})


@pytest.mark.fast
def test_different_model_version_same_day_is_separate_row(app_conn):
    a = make_row()
    b = dataclasses.replace(a, model_version="baseline_yesterday_v1")
    assert write_predictions(app_conn, [a, b]).inserted == 2
    assert count(app_conn, a.ticker) == 2


@pytest.mark.fast
def test_dry_run_writes_nothing_but_counts(app_conn):
    existing, new = make_row(), make_row()
    write_predictions(app_conn, [existing])
    res = write_predictions(app_conn, [existing, new], dry_run=True)
    assert res == WriteResult(inserted=1, skipped_existing=1, dry_run=True)
    assert count(app_conn, new.ticker) == 0
    assert count(app_conn, existing.ticker) == 1
    # dry-run sonrasi gercek yazim ayni sayilari verir
    real = write_predictions(app_conn, [existing, new])
    assert (real.inserted, real.skipped_existing, real.dry_run) == (1, 1, False)


@pytest.mark.parametrize(
    "bad",
    [
        {"prob_up": 1.01},
        {"prob_up": -0.01},
        {"prob_up": float("nan")},
        {"prob_up": float("inf")},
        {"prob_up": "0.5"},
        {"model_role": "unknown"},
        {"horizon_days": 0},
        {"horizon_days": -5},
        {"horizon_days": 2.5},
        {"features_json": "not json"},
        {"features_json": '{"a": NaN}'},
        {"features_json": ""},
        {"ticker": ""},
        {"ticker": "  "},
        {"score": float("nan")},
        {"model_version": ""},
        {"feature_hash": ""},
        {"made_at": dt.datetime(2026, 11, 2)},
        {"made_at": "2026-11-02"},
    ],
)
@pytest.mark.fast
def test_validation_errors_raise_value_error(app_conn, bad):
    good = make_row()
    with pytest.raises(ValueError):
        write_predictions(app_conn, [good, make_row(**bad)])
    assert count(app_conn, good.ticker) == 0  # hicbir sey yazilmadi
    with pytest.raises(ValueError):
        write_predictions(app_conn, [make_row(**bad)], dry_run=True)


@pytest.mark.fast
def test_boundary_probabilities_are_accepted(app_conn):
    rows = [make_row(prob_up=0.0), make_row(prob_up=1.0)]
    assert write_predictions(app_conn, rows).inserted == 2


@pytest.mark.fast
def test_empty_batch(app_conn):
    assert write_predictions(app_conn, []) == WriteResult(0, 0, False)


@pytest.mark.fast
def test_db_error_rolls_back_whole_batch(app_conn):
    # DB seviyesinde CHECK'e takilan satir (dogrulamayi gecer ama NUL karakter jsonb'de gecersiz) tum islemi geri alir
    good = make_row()
    bad = make_row(features_json='{"a":"\\u0000"}')
    with pytest.raises(Exception):
        write_predictions(app_conn, [good, bad])
    assert count(app_conn, good.ticker) == 0


# --- baseline_feature_hash ---

@pytest.mark.fast
def test_feature_hash_stable_and_canonical():
    a = {"rule": "ma20", "close": 10.5, "ma20": 10.025000000000001, "n_bars": 20}
    b = {"n_bars": 20, "ma20": 10.025, "rule": "ma20", "close": 10.5}  # sira ve 1e-10 alti fark onemsiz
    ja, ha = baseline_feature_hash(a)
    jb, hb = baseline_feature_hash(b)
    assert (ja, ha) == (jb, hb)
    assert ja == '{"close":10.5,"ma20":10.025,"n_bars":20,"rule":"ma20"}'
    assert len(ha) == 64
    assert baseline_feature_hash({**a, "close": 10.6})[1] != ha
    # sabit deger (regresyon): kanonik bicim degisirse hash degisir ve bu test uyarir
    assert baseline_feature_hash({"rule": "always_up"}) == (
        '{"rule":"always_up"}',
        "7a964a5ecf5606e6b9e38234ec28c6454f00d4bfa62cb4d86d2994ac967d3d3b",
    )


@pytest.mark.fast
def test_feature_hash_known_value_is_sha256_of_json():
    import hashlib

    js, h = baseline_feature_hash({"rule": "always_up"})
    assert h == hashlib.sha256(js.encode()).hexdigest()


@pytest.mark.fast
def test_feature_hash_handles_numpy_and_nesting_and_rejects_bad():
    import numpy as np

    js, _ = baseline_feature_hash({"x": np.float64(0.1 + 0.2), "n": np.int64(3), "l": (1, 2), "b": np.bool_(True)})
    assert json.loads(js) == {"x": 0.3, "n": 3, "l": [1, 2], "b": True}
    for bad in ({"x": float("nan")}, {"x": float("inf")}, {"x": object()}, {1: 2}):
        with pytest.raises(ValueError):
            baseline_feature_hash(bad)


@pytest.mark.fast
def test_feature_hash_roundtrips_into_ledger(app_conn):
    js, h = baseline_feature_hash({"rule": "yesterday", "ret_1d": 0.0123456789012})
    row = make_row(feature_hash=h, features_json=js)
    assert write_predictions(app_conn, [row]).inserted == 1
    got = fetch(app_conn, row.ticker)[0]
    assert got[4] == h and got[5] == json.loads(js)
