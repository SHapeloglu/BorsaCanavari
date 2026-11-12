"""Faz 4 izleme: sistem durumu (migration 006), devre kesici, drift. Saf cekirdekler sentetik veriyle; durum testleri
gercek Postgres'te (bist_app yetkileriyle)."""

from __future__ import annotations

import datetime as dt
import json
import math
import uuid

import numpy as np
import pandas as pd
import psycopg
import pytest
from conftest import APP_PASSWORD

from bist_tahmin import cli
from bist_tahmin.config import load_config
from bist_tahmin.db import APP_ROLE, list_migrations, plan_init, run_init, with_overrides
from bist_tahmin.monitor import breaker, drift
from bist_tahmin.monitor.breaker import BreakerVerdict, evaluate_breaker
from bist_tahmin.models import promote as pr
from bist_tahmin.monitor.state import get_state, set_state, to_jsonable

CFG = load_config("config/default.yaml")
UTC = dt.timezone.utc


@pytest.fixture()
def mdb(admin_conninfo):
    """Taze veritabani + bist_app baglantisi (devre kesici durumu testler arasi sizmasin)."""
    name = f"bist_mon_{uuid.uuid4().hex[:8]}"
    run_init(admin_conninfo, db_name=name, app_password=APP_PASSWORD)
    info = with_overrides(admin_conninfo, dbname=name, user=APP_ROLE, password=APP_PASSWORD)
    with psycopg.connect(info, autocommit=True) as conn:
        yield conn, info, admin_conninfo, name


# --- migration 006 / state ----------------------------------------------------------------------------------------

def test_migration_006_is_listed_and_idempotent(mdb):
    conn, _, admin, name = mdb
    names = [n for n, _ in list_migrations()]
    assert "006_system_state.sql" in names and names == sorted(names)
    assert plan_init(admin, name).is_noop
    sql = dict(list_migrations())["006_system_state.sql"]
    with psycopg.connect(with_overrides(admin, dbname=name), autocommit=True) as owner:
        owner.execute("SET ROLE bist_owner")
        owner.execute(sql)       # ikinci uygulama hata vermez, veriyi silmez
        owner.execute(sql)
    set_state(conn, "k_idem", {"a": 1})
    with psycopg.connect(with_overrides(admin, dbname=name), autocommit=True) as owner:
        owner.execute("SET ROLE bist_owner")
        owner.execute(sql)
    assert get_state(conn, "k_idem") == {"a": 1}


def test_bist_app_cannot_delete_or_truncate_system_state(mdb):
    conn, *_ = mdb
    set_state(conn, "k_nodel", {"x": 1})
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("DELETE FROM system_state WHERE key = 'k_nodel'")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("TRUNCATE system_state")
    assert get_state(conn, "k_nodel") == {"x": 1}


def test_state_upsert_roundtrip_and_json_safety(mdb):
    conn, *_ = mdb
    assert get_state(conn, "yok") is None
    set_state(conn, "k1", {"n": np.int64(3), "f": np.float64(0.5), "nan": float("nan"), "inf": math.inf,
                           "d": dt.date(2026, 11, 9), "ts": pd.Timestamp("2026-11-09 19:00"), "arr": np.arange(2),
                           "b": np.bool_(True), "s": {"b", "a"}, 5: "int-key"})
    got = get_state(conn, "k1")
    assert got["n"] == 3 and got["f"] == 0.5 and got["nan"] is None and got["inf"] is None
    assert got["d"] == "2026-11-09" and got["ts"].startswith("2026-11-09") and got["arr"] == [0, 1]
    assert got["b"] is True and got["s"] == ["a", "b"] and got["5"] == "int-key"
    t1 = conn.execute("SELECT updated_at FROM system_state WHERE key='k1'").fetchone()[0]
    set_state(conn, "k1", {"v": 2})
    row = conn.execute("SELECT value, updated_at FROM system_state WHERE key='k1'").fetchone()
    assert row[0] == {"v": 2} and row[1] >= t1
    assert conn.execute("SELECT count(*) FROM system_state WHERE key='k1'").fetchone()[0] == 1


@pytest.mark.fast
def test_to_jsonable_output_is_strict_json():
    out = to_jsonable({"a": [float("nan"), np.float32(1.5), pd.NaT]})
    json.dumps(out, allow_nan=False)


# --- devre kesici: saf karar --------------------------------------------------------------------------------------

def _idx(n):
    return pd.date_range("2026-01-05", periods=n, freq="B", name="made_at")


@pytest.mark.fast
def test_breaker_known_relative_drawdown_value():
    net = pd.Series([-0.05] * 5, index=_idx(5))          # her kohort 1/5 sermaye: 5 * (-0.01) => esdeger 0.95
    bench = pd.Series([0.0] * 5, index=_idx(5))
    v = evaluate_breaker(net, bench, 5, 0.04)
    assert v.relative_drawdown == pytest.approx(0.05) and v.tripped
    assert not evaluate_breaker(net, bench, 5, 0.06).tripped


@pytest.mark.fast
def test_breaker_threshold_is_strict_and_uses_relative_not_absolute():
    # Piyasa ve model ayni oranda duser: mutlak dusus buyuk, GORELI dusus 0 => tetiklenmez.
    both = pd.Series([-0.05] * 30, index=_idx(30))
    v = evaluate_breaker(both, both, 5, 0.10)
    assert v.relative_drawdown == pytest.approx(0.0) and not v.tripped
    # model benchmark'a esit => tetiklenmez
    assert not evaluate_breaker(pd.Series([0.01] * 10, index=_idx(10)), pd.Series([0.01] * 10, index=_idx(10)),
                                5, 0.10).tripped


@pytest.mark.fast
def test_breaker_measures_peak_to_trough_not_from_start():
    net = pd.Series([0.10] * 10 + [-0.10] * 10, index=_idx(20))
    bench = pd.Series([0.0] * 20, index=_idx(20))
    # model ozkaynagi 1.2'ye cikar sonra 1.0'a iner: oran zirvesi 1.2 => dusus 1 - 1/1.2 = 16.7%
    v = evaluate_breaker(net, bench, 5, 0.10)
    assert v.relative_drawdown == pytest.approx(1 - 1 / 1.2) and v.tripped


@pytest.mark.fast
def test_breaker_no_data_does_not_trip_and_invalid_equity_does():
    v = evaluate_breaker(pd.Series(dtype=float), pd.Series(dtype=float), 5, 0.10)
    assert not v.tripped and "kohort yok" in v.detail
    bad = evaluate_breaker(pd.Series([-10.0], index=_idx(1)), pd.Series([0.0], index=_idx(1)), 5, 0.10)
    assert bad.tripped and bad.relative_drawdown == 1.0
    with pytest.raises(ValueError):
        evaluate_breaker(pd.Series([0.0]), pd.Series([0.0]), 5, 0.0)


@pytest.mark.fast
def test_breaker_misaligned_and_nan_cohorts_are_dropped():
    net = pd.Series([-0.05, np.nan, -0.05], index=_idx(3))
    bench = pd.Series([0.0, 0.0], index=_idx(3)[[0, 2]])
    v = evaluate_breaker(net, bench, 5, 0.5)
    assert "2 kohort" in v.detail


# --- devre kesici: model kohortlari -------------------------------------------------------------------------------

def _resolved_frame(version="m1", days=30, bad=True, tickers=("A", "B", "C", "D", "E", "F")):
    rows = []
    for d in pd.bdate_range("2026-01-05", periods=days):
        for i, t in enumerate(tickers):
            top = i < 2
            rows.append({"made_at": d, "ticker": t, "model_version": version, "score": 1.0 - i * 0.1,
                         "tradable_ret": (-0.05 if top and bad else 0.01 if top else 0.0)})
    return pd.DataFrame(rows)


@pytest.mark.fast
def test_model_cohort_returns_and_after_cutoff():
    df = _resolved_frame()
    net, bench = breaker.model_cohort_returns(df, "m1")
    assert len(net) == 30 and net.iloc[0] == pytest.approx(-0.05) and bench.iloc[0] == pytest.approx(-0.05 * 2 / 6)
    cut = pd.bdate_range("2026-01-05", periods=30)[9]
    net2, _ = breaker.model_cohort_returns(df, "m1", after=cut)
    assert len(net2) == 21 and net2.index.min() == cut     # 'after' DAHIL: reset gununun kohortu sayilir
    assert len(breaker.model_cohort_returns(df, "m1", after=cut + pd.Timedelta(days=1))[0]) == 20
    assert breaker.model_cohort_returns(df, "baska")[0].empty


@pytest.mark.fast
def test_breaker_cost_is_promote_default_when_costs_missing():
    """Maliyet null iken devre kesici 0 degil, terfi ile ayni 10 bps varsayimini kullanir (tutarlilik, muhafazakar)."""
    assert breaker.configured_cost_bps({}) == pr.DEFAULT_COST_BPS == 10.0
    assert breaker.configured_cost_bps(CFG) == 10.0                      # default.yaml: komisyon/spread null
    assert breaker.configured_cost_bps({"costs": {"commission_bps": 8, "spread_bps": None}}) == 10.0
    assert breaker.configured_cost_bps({"costs": {"commission_bps": 8, "spread_bps": 4}}) == 12.0


@pytest.mark.fast
def test_evaluate_model_trips_on_bad_model_only():
    bad = breaker.evaluate_model(_resolved_frame(bad=True), "m1", CFG)
    good = breaker.evaluate_model(_resolved_frame(bad=False), "m1", CFG)
    assert bad.tripped and bad.relative_drawdown > CFG["circuit_breaker"]["max_relative_drawdown"]
    assert not good.tripped


# --- devre kesici: kalici durum -----------------------------------------------------------------------------------

def test_breaker_trip_status_and_no_auto_restart(mdb):
    conn, *_ = mdb
    st = breaker.status(conn)
    assert st["tripped"] is False and st["history"] == []
    now = dt.datetime(2026, 11, 9, 16, 0, tzinfo=UTC)
    v = BreakerVerdict(True, 0.23, "test dusus")
    assert breaker.trip(conn, v, "logreg_x@1", now=now) is True
    st = breaker.status(conn)
    assert st["tripped"] and st["model_version"] == "logreg_x@1" and st["relative_drawdown"] == pytest.approx(0.23)
    assert st["reason"] == "test dusus" and st["since"].startswith("2026-11-09")
    # ikinci tetikleme ilk kaydi DEGISTIRMEZ
    assert breaker.trip(conn, BreakerVerdict(True, 0.5, "baska"), "other", now=now + dt.timedelta(days=3)) is False
    st2 = breaker.status(conn)
    assert st2["since"] == st["since"] and st2["model_version"] == "logreg_x@1"
    # iyi bir degerlendirme (tripped=False) durumu KAPATMAZ: API'de otomatik kapatma yok
    assert breaker.evaluate_model(_resolved_frame(bad=False), "m1", CFG).tripped is False
    assert breaker.status(conn)["tripped"] is True


def test_breaker_reset_requires_a_reason_and_keeps_history(mdb):
    conn, *_ = mdb
    now = dt.datetime(2026, 11, 9, 16, 0, tzinfo=UTC)
    breaker.trip(conn, BreakerVerdict(True, 0.2, "d1"), "m1", now=now)
    for bad in ("", "   ", None):
        with pytest.raises(ValueError, match="gerekce"):
            breaker.reset(conn, bad)
    assert breaker.status(conn)["tripped"] is True               # basarisiz sifirlama durumu degistirmedi
    new = breaker.reset(conn, "rejim degisti, incelendi", now=now + dt.timedelta(days=2))
    st = breaker.status(conn)
    assert st["tripped"] is False and st["reset_reason"] == "rejim degisti, incelendi"
    assert st["evaluate_after"] == "2026-11-11" and new["evaluate_after"] == "2026-11-11"
    assert len(st["history"]) == 1 and st["history"][0]["tripped"] is True
    assert st["history"][0]["model_version"] == "m1" and st["history"][0]["reset_reason"] == "rejim degisti, incelendi"
    # yeniden tetiklenebilir; gecmis birikir; evaluate_after korunur
    breaker.trip(conn, BreakerVerdict(True, 0.3, "d2"), "m2", now=now + dt.timedelta(days=9))
    breaker.reset(conn, "ikinci", now=now + dt.timedelta(days=10))
    assert len(breaker.status(conn)["history"]) == 2


def test_cli_breaker_reset_needs_reason_and_status_exit_codes(mdb, monkeypatch, capsys):
    conn, info, *_ = mdb
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert cli.main(["breaker", "reset"]) == 2                       # gerekce yok => baglantidan once reddedilir
    assert cli.main(["breaker", "reset", "--reason", "   "]) == 2
    monkeypatch.setenv("DATABASE_URL", info)
    assert cli.main(["breaker", "status"]) == 0
    assert "kapali" in capsys.readouterr().out
    breaker.trip(conn, BreakerVerdict(True, 0.2, "d1"), "m1")
    assert cli.main(["breaker", "status"]) == 1
    assert "ACIK" in capsys.readouterr().out
    assert cli.main(["breaker", "reset", "--reason", "x", "--dry-run"]) == 0
    assert breaker.status(conn)["tripped"] is True                    # dry-run yazmaz
    assert cli.main(["breaker", "reset", "--reason", "incelendi"]) == 0
    assert breaker.status(conn)["tripped"] is False


# --- drift --------------------------------------------------------------------------------------------------------

REF = drift.REFERENCE_VERSION


def _drift_frame(n_days=70, model_noise=0.0, tickers=("A", "B", "C", "D"), seed=0, late_only=False):
    """Referans sabit 0.55; model p = 0.55 + gurultu. Etiketler gercek yukselis orani ~0.55."""
    rng = np.random.default_rng(seed)
    rows = []
    days = pd.bdate_range("2026-01-05", periods=n_days)
    for k, d in enumerate(days):
        for t in tickers:
            y = int(rng.random() < 0.55)
            noisy = model_noise if (not late_only or k < n_days - 10) else 0.0
            p_model = float(np.clip(0.55 + noisy * (1 if rng.random() < 0.5 else -1), 0.01, 0.99))
            rows.append({"made_at": d, "ticker": t, "model_version": REF, "prob_up": 0.55, "label_up": y})
            rows.append({"made_at": d, "ticker": t, "model_version": "m1", "prob_up": p_model, "label_up": y})
    return pd.DataFrame(rows)


@pytest.mark.fast
def test_drift_after_cutoff_ignores_days_before_reset():
    # gurultu yalniz ilk 60 gunde: tum pencere alarm verir; reset gununden (dahil) sonra temiz 10 gun yetersiz -> alarm yok
    df = _drift_frame(n_days=70, model_noise=0.4, late_only=True)
    assert drift.drift_check(df, "m1", CFG).alert is True
    clean_from = pd.bdate_range("2026-01-05", periods=70)[60]
    res = drift.drift_check(df, "m1", CFG, after=clean_from)
    assert res.alert is False and res.n_days == 10 and "veri yetersiz" in res.note
    # kesimden sonra hic satir kalmazsa cokmez: eslesme yok -> NaN, alarm yok
    far = drift.drift_check(df, "m1", CFG, after="2030-01-01")
    assert far.alert is False and far.n_days == 0


@pytest.mark.fast
def test_rolling_brier_gap_known_value():
    # tek hisse, 3 gun, etiketler 1: referans p=0.5 -> Brier 0.25; model p=0.9 -> 0.01; fark -0.24
    days = pd.bdate_range("2026-03-02", periods=3)
    rows = []
    for d in days:
        rows += [{"made_at": d, "ticker": "A", "model_version": REF, "prob_up": 0.5, "label_up": 1},
                 {"made_at": d, "ticker": "A", "model_version": "m1", "prob_up": 0.9, "label_up": 1}]
    gap = drift.rolling_brier_gap(pd.DataFrame(rows), 3, "m1")
    assert gap == pytest.approx(0.01 - 0.25)


@pytest.mark.fast
def test_gap_zero_when_model_equals_reference_and_positive_when_worse():
    assert drift.rolling_brier_gap(_drift_frame(model_noise=0.0), 60, "m1") == pytest.approx(0.0)
    worse = drift.rolling_brier_gap(_drift_frame(model_noise=0.3), 60, "m1")
    assert worse > 0.01 and drift.drift_alert(worse, 0.01)


@pytest.mark.fast
def test_window_uses_only_the_last_n_distinct_dates():
    df = _drift_frame(n_days=70, model_noise=0.4, late_only=True)     # gurultu yalniz ilk 60 gunde; son 10 gun temiz
    assert drift.rolling_brier_gap(df, 10, "m1") == pytest.approx(0.0)
    assert drift.rolling_brier_gap(df, 70, "m1") > 0.01


@pytest.mark.fast
def test_insufficient_days_gives_nan_and_no_alert():
    df = _drift_frame(n_days=20, model_noise=0.4)
    gap = drift.rolling_brier_gap(df, 60, "m1")
    assert math.isnan(gap) and drift.drift_alert(gap, 0.01) is False
    res = drift.drift_check(df, "m1", CFG)
    assert res.alert is False and "veri yetersiz" in res.note and res.n_days == 20


@pytest.mark.fast
def test_drift_alert_threshold_semantics():
    assert drift.drift_alert(0.0101, 0.01) and not drift.drift_alert(0.01, 0.01) and not drift.drift_alert(-0.5, 0.01)
    assert not drift.drift_alert(None, 0.01) and not drift.drift_alert(float("nan"), 0.01)


@pytest.mark.fast
def test_drift_check_uses_config_and_flags_bad_model():
    res = drift.drift_check(_drift_frame(model_noise=0.35), "m1", CFG)
    assert res.alert and res.gap > CFG["drift"]["brier_over_base_rate_alert"] and res.n_days == 60
    assert drift.drift_check(_drift_frame(model_noise=0.0), "m1", CFG).alert is False
    assert res.as_dict()["model_version"] == "m1"


@pytest.mark.fast
def test_drift_matches_only_common_rows():
    df = _drift_frame(n_days=65, model_noise=0.0)
    # model, referansin bulunmadigi ekstra bir gunde cok kotu: eslesmeyen satir sayilmaz
    extra = pd.DataFrame([{"made_at": pd.Timestamp("2027-01-04"), "ticker": "A", "model_version": "m1",
                           "prob_up": 0.99, "label_up": 0}])
    assert drift.rolling_brier_gap(pd.concat([df, extra]), 60, "m1") == pytest.approx(0.0)
    assert math.isnan(drift.rolling_brier_gap(df, 60, "olmayan_model"))


def test_load_resolved_reads_only_resolved_rows(mdb):
    conn, *_ = mdb
    ins = ("INSERT INTO predictions (ticker, made_at, horizon_days, prob_up, score, model_version, model_role,"
           " feature_hash, features_json, created_at) VALUES (%s, '2026-10-01', 5, 0.6, 0.6, 'mx@1', 'champion',"
           " 'h', '{}'::jsonb, now())")
    conn.execute(ins, ("AAA",))
    conn.execute(ins, ("BBB",))
    conn.execute("UPDATE predictions SET resolved_at='2026-10-08', realized_ret=0.01, realized_rel_ret=0.0,"
                 " tradable_ret=0.02, label_up=1, label_rel_up=0 WHERE ticker='AAA' AND model_version='mx@1'")
    df = drift.load_resolved(conn, ["mx@1"])
    assert list(df["ticker"]) == ["AAA"] and df["label_up"].iloc[0] == 1 and df["tradable_ret"].iloc[0] == 0.02
    assert str(df["made_at"].dtype).startswith("datetime64")
    assert drift.load_resolved(conn, []).empty
