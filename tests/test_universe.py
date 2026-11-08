"""universe.py: saf diff mantigi ve DB senkronizasyonu."""

from __future__ import annotations

import datetime as dt

import pytest

from bist_tahmin.data import store
from bist_tahmin.data.universe import Membership, UniverseError, diff_membership, fetch_components, members_on

D = dt.date


def test_first_sync_adds_all_with_as_of():
    add, close = diff_membership([], ["B", "A"], D(2026, 11, 8))
    assert add == [Membership("A", D(2026, 11, 8)), Membership("B", D(2026, 11, 8))]
    assert close == []


def test_removal_closes_open_row_with_as_of():
    rows = [("A", D(2026, 1, 1), None), ("B", D(2026, 1, 1), None)]
    add, close = diff_membership(rows, ["A"], D(2026, 11, 8))
    assert add == []
    assert close == [Membership("B", D(2026, 1, 1), D(2026, 11, 8))]


def test_no_change():
    rows = [{"ticker": "A", "valid_from": D(2026, 1, 1), "valid_to": None}]
    assert diff_membership(rows, ["a "], D(2026, 11, 8)) == ([], [])


def test_closed_rows_ignored_and_readd_opens_new_row():
    rows = [Membership("A", D(2025, 1, 1), D(2025, 6, 1))]
    add, close = diff_membership(rows, ["A"], D(2026, 11, 8))
    assert add == [Membership("A", D(2026, 11, 8))] and close == []


def test_close_never_precedes_valid_from():
    rows = [("A", D(2026, 11, 8), None)]
    _, close = diff_membership(rows, [], D(2026, 11, 1))
    assert close[0].valid_to >= close[0].valid_from


def test_members_on_half_open_interval():
    rows = [Membership("A", D(2026, 1, 1), D(2026, 6, 1)), Membership("B", D(2026, 3, 1))]
    assert members_on(rows, D(2025, 12, 31)) == []
    assert members_on(rows, D(2026, 1, 1)) == ["A"]
    assert members_on(rows, D(2026, 5, 31)) == ["A", "B"]
    assert members_on(rows, D(2026, 6, 1)) == ["B"]  # valid_to dahil degil


class FakeSource:
    def __init__(self, comps):
        self.comps = comps

    def index_components(self, symbol):
        return self.comps


def test_fetch_components_empty_is_error():
    with pytest.raises(UniverseError):
        fetch_components(FakeSource([]))


def test_sync_universe_db_roundtrip_and_idempotent(app_conn):
    idx = "TESTIDX_SYNC"
    as_of = D(2026, 11, 8)
    add, close = store.sync_universe(app_conn, ["AAA", "BBB"], as_of, index_name=idx)
    assert len(add) == 2 and close == []
    # ayni girdiyle tekrar: degisiklik yok, satir sayisi ayni
    assert store.sync_universe(app_conn, ["AAA", "BBB"], as_of, index_name=idx) == ([], [])
    # BBB cikti, CCC girdi
    add, close = store.sync_universe(app_conn, ["AAA", "CCC"], D(2026, 12, 1), index_name=idx)
    assert [m.ticker for m in add] == ["CCC"] and [m.ticker for m in close] == ["BBB"]
    rows = {m.ticker: m for m in store.read_membership(app_conn, idx)}
    assert rows["BBB"].valid_to == D(2026, 12, 1)
    assert rows["CCC"].valid_from == D(2026, 12, 1) and rows["CCC"].valid_to is None
    assert rows["AAA"].valid_from == as_of and rows["AAA"].valid_to is None
    assert len(rows) == 3


def test_sync_universe_dry_run_writes_nothing(app_conn):
    idx = "TESTIDX_DRY"
    add, _ = store.sync_universe(app_conn, ["AAA"], D(2026, 11, 8), index_name=idx, dry_run=True)
    assert len(add) == 1
    assert store.read_membership(app_conn, idx) == []


def test_sync_universe_empty_refused(app_conn):
    with pytest.raises(ValueError):
        store.sync_universe(app_conn, [], D(2026, 11, 8), index_name="TESTIDX_EMPTY")
