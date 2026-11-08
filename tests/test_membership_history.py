"""Gecmis BIST 30 uyeligi: veri degismezleri + yukleme (reference/bist30/membership_history.csv)."""

import datetime as dt
import uuid

import pytest

from bist_tahmin.data import store
from bist_tahmin.data.universe import (
    HistoryConflict, Membership, members_on, plan_history_load, read_history_csv,
)

D = dt.date
# 2026-11-08 borsapy XU030 (30): bugunku uyeler; gecmis zinciri bu listede bitmeli
CURRENT = set("AEFES AKBNK ASELS ASTOR BIMAS EKGYO ENKAI EREGL FROTO GARAN GUBRF ISCTR KCHOL KRDMD MGROS PETKM PGSUS "
              "SAHOL SASA SISE TAVHL TCELL THYAO TOASO TRALT TRMET TTKOM TUPRS VAKBN YKBNK".split())


@pytest.fixture(scope="module")
def hist():
    return read_history_csv()


def test_current_members_equal_known_xu030(hist):
    assert {m.ticker for m in hist if m.valid_to is None} == CURRENT


def test_exactly_30_members_on_every_day_across_history(hist):
    dates = sorted({m.valid_from for m in hist} | {m.valid_to for m in hist if m.valid_to})
    probes = {d for d in dates} | {d - dt.timedelta(days=1) for d in dates if d > D(2019, 7, 1)}
    probes |= {D(2019, 7, 1), D(2026, 11, 8), D(2026, 12, 31)}
    bad = {d: len(members_on(hist, d)) for d in sorted(probes) if len(members_on(hist, d)) != 30}
    assert not bad, bad


def test_no_overlapping_intervals_per_ticker(hist):
    by = {}
    for m in hist:
        by.setdefault(m.ticker, []).append(m)
    for t, ms in by.items():
        ms.sort(key=lambda m: m.valid_from)
        for a, b in zip(ms, ms[1:]):
            assert a.valid_to is not None and a.valid_to <= b.valid_from, (t, a, b)
    assert all(m.valid_to is None or m.valid_to > m.valid_from for m in hist)


def test_history_starts_2019_07_and_old_codes_are_mapped(hist):
    assert min(m.valid_from for m in hist) == D(2019, 7, 1)
    tickers = {m.ticker for m in hist}
    assert "KOZAL" not in tickers and "KOZAA" not in tickers and {"TRALT", "TRMET"} <= tickers


def test_known_official_changes(hist):
    on = lambda d: set(members_on(hist, d))                                    # noqa: E731
    assert "ENJSA" not in on(D(2019, 8, 1)) and "TSKB" in on(D(2019, 8, 1))    # 2019-T3: TSKB girdi, ENJSA cikti
    assert "FROTO" not in on(D(2020, 5, 1)) and "FROTO" in on(D(2021, 11, 1))  # 2020-T2 cikti, 2021-T3 geri girdi
    assert {"TRKCM", "SODA"}.isdisjoint(on(D(2021, 1, 22)))                    # Sisecam birlesmesi sonrasi
    assert "DSTKF" in on(D(2026, 5, 1)) and "DSTKF" not in on(D(2026, 11, 8))  # 2026-T4: DSTKF cikti
    assert "TRMET" in on(D(2026, 11, 8))


# --- yukleme plani / uygulama ------------------------------------------------------------------------------

def M(t, a, b=None):
    return Membership(t, a, b)


def test_plan_pulls_open_row_earlier_and_inserts_closed_rows():
    existing = [M("AAA", D(2026, 11, 8))]
    target = [M("AAA", D(2019, 7, 1)), M("BBB", D(2019, 7, 1), D(2021, 1, 1)), M("BBB", D(2023, 1, 1))]
    ins, upd = plan_history_load(existing, target)
    assert upd == [(M("AAA", D(2026, 11, 8)), D(2019, 7, 1))]
    assert set(ins) == {M("BBB", D(2019, 7, 1), D(2021, 1, 1)), M("BBB", D(2023, 1, 1))}


def test_plan_is_idempotent_and_conflicts_are_refused():
    target = [M("AAA", D(2019, 7, 1)), M("BBB", D(2019, 7, 1), D(2021, 1, 1))]
    assert plan_history_load(target, target) == ([], [])
    with pytest.raises(HistoryConflict):      # mevcut acik satir hedeften ERKEN basliyor => gozlem bozulmaz, celiski
        plan_history_load([M("AAA", D(2018, 1, 1))], [M("AAA", D(2019, 7, 1))])
    with pytest.raises(HistoryConflict):      # sonuc cakisan aralik uretir
        plan_history_load([M("CCC", D(2020, 1, 1), D(2022, 1, 1))], [M("CCC", D(2019, 1, 1), D(2021, 1, 1))])


def test_load_membership_history_db_idempotent_dry_run_and_no_delete(app_conn):
    idx = f"TEST_{uuid.uuid4().hex[:8]}"
    a, b = f"A{uuid.uuid4().hex[:6].upper()}", f"B{uuid.uuid4().hex[:6].upper()}"
    store.apply_membership_changes(app_conn, [M(a, D(2026, 11, 8))], [], index_name=idx)
    target = [M(a, D(2019, 7, 1)), M(b, D(2019, 7, 1), D(2021, 1, 1)), M(b, D(2023, 1, 1))]
    assert store.load_membership_history(app_conn, target, idx, dry_run=True) == (2, 1)
    assert len(store.read_membership(app_conn, idx)) == 1                       # dry-run yazmadi
    assert store.load_membership_history(app_conn, target, idx) == (2, 1)
    assert store.load_membership_history(app_conn, target, idx) == (0, 0)       # idempotent
    rows = store.read_membership(app_conn, idx)
    assert len(rows) == 3 and M(a, D(2019, 7, 1)) in rows
    assert set(members_on(rows, D(2020, 6, 1))) == {a, b} and set(members_on(rows, D(2022, 1, 1))) == {a}
