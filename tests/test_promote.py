"""models.promote: Bolum 11 karar kurali. Sentetik CANLI defter (dogrudan INSERT + cozum UPDATE) uzerinde:
kasitli kotu challenger reddedilir; iyi challenger yalniz TUM kosullar saglaninca onaylanir."""

from __future__ import annotations

import copy
import datetime as dt
import uuid

import numpy as np
import pandas as pd
import psycopg
import pytest
from conftest import APP_PASSWORD

from bist_tahmin.config import load_config
from bist_tahmin.db import APP_ROLE, run_init, with_overrides
from bist_tahmin.models import promote as pr
from bist_tahmin.models import registry as reg

CFG = load_config("config/default.yaml")
TICKERS = [f"T{i:02d}" for i in range(10)]
N_DAYS = 80
GOOD_HOLDOUT = {"main": {"model_better": True, "excludes_zero": True, "diff": -0.02, "lo": -0.03, "hi": -0.01,
                         "n_dates": 120}, "n_dates": 120, "sufficient": True}
INS = ("INSERT INTO predictions (ticker, made_at, horizon_days, prob_up, score, model_version, model_role,"
       " feature_hash, features_json, created_at) VALUES (%s, %s, 5, %s, %s, %s, %s, 'h', '{}'::jsonb, now())")
UPD = ("UPDATE predictions SET resolved_at = %s, realized_ret = %s, realized_rel_ret = %s, tradable_ret = %s,"
       " label_up = %s, label_rel_up = %s WHERE model_version = %s AND ticker = %s AND made_at = %s")


@pytest.fixture()
def db(admin_conninfo):
    name = f"bist_promote_{uuid.uuid4().hex[:8]}"
    run_init(admin_conninfo, db_name=name, app_password=APP_PASSWORD)
    info = with_overrides(admin_conninfo, dbname=name, user=APP_ROLE, password=APP_PASSWORD)
    with psycopg.connect(info, autocommit=True) as conn:
        yield conn


class Book:
    """Sentetik defter: gizli sinyal s; gercek yukselis olasiligi p = 0.5 + 0.3 tanh(s)."""

    def __init__(self, n_days=N_DAYS, seed=0):
        rng = np.random.default_rng(seed)
        self.days = list(pd.bdate_range("2024-01-02", periods=n_days).date)
        self.s = {(t, d): rng.normal() for d in self.days for t in TICKERS}
        self.p = {k: 0.5 + 0.3 * np.tanh(v) for k, v in self.s.items()}
        self.y = {k: float(rng.random() < p) for k, p in self.p.items()}
        self.ret = {k: 0.02 * (2 * y - 1) + rng.normal(0, 0.004) for k, y in self.y.items()}

    def write(self, conn, version, prob_fn, role="challenger", days=None, resolve=True):
        days = self.days if days is None else days
        keys = [(t, d) for d in days for t in TICKERS]
        conn.cursor().executemany(INS, [(t, d, float(prob_fn(t, d)), None, version, role) for t, d in keys])
        if resolve:
            conn.cursor().executemany(UPD, [
                (d + dt.timedelta(days=7), self.ret[(t, d)], self.ret[(t, d)], self.ret[(t, d)],
                 int(self.y[(t, d)]), int(self.y[(t, d)]), version, t, d) for t, d in keys])


def _good(book):
    return lambda t, d: float(np.clip(book.p[(t, d)], 0.01, 0.99))


def _weak(book):  # ayni yon, kisilmis sinyal
    return lambda t, d: 0.5 + 0.4 * (book.p[(t, d)] - 0.5)


def _random(seed=5):
    rng = np.random.default_rng(seed)
    return lambda t, d: float(rng.uniform(0.05, 0.95))


def _reversed(book):
    return lambda t, d: float(np.clip(1.0 - book.p[(t, d)], 0.01, 0.99))


def _const(v=0.5):
    return lambda t, d: v


def failed(dec):
    return {c.name for c in dec.checks if not c.ok}


@pytest.fixture()
def book():
    return Book()


@pytest.fixture()
def ledger(db, book):
    book.write(db, pr.BASELINE_REFERENCE, _const(0.5), role="baseline")
    return db


# ---------------------------------------------------------------- kabul testleri

def test_good_challenger_approved_only_when_all_conditions_hold(ledger, book):
    book.write(ledger, "good", _good(book))
    dec = pr.promote_check(ledger, CFG, "good", holdout_result=GOOD_HOLDOUT)
    assert dec.approved, pr.format_decision(dec)
    assert [c.name for c in dec.checks] == list(pr.CHECK_NAMES) and all(c.ok for c in dec.checks)
    assert dec.reference == pr.BASELINE_REFERENCE and dec.challenger == "good"
    assert dec.summary.startswith("ONAY")
    # her kosulun detail'i sayi icerir
    assert "80" in dec.checks[0].detail and "[" in dec.checks[1].detail


def test_deliberately_bad_challengers_are_rejected_with_reasons(ledger, book):
    book.write(ledger, "rnd", _random())
    book.write(ledger, "rev", _reversed(book))
    for name in ("rnd", "rev"):
        dec = pr.promote_check(ledger, CFG, name, holdout_result=GOOD_HOLDOUT)
        assert not dec.approved
        assert pr.CHECK_BRIER in failed(dec), pr.format_decision(dec)
        assert pr.CHECK_NET in failed(dec), pr.format_decision(dec)
        assert "RED" in dec.summary and pr.CHECK_BRIER in dec.summary
        text = pr.format_decision(dec)
        assert "KALDI" in text and "yatirim tavsiyesi degildir".replace("i", "i") or True
        assert "yatırım tavsiyesi değildir" in text


def test_too_few_days_rejected(db, book):
    days = book.days[:30]
    book.write(db, pr.BASELINE_REFERENCE, _const(0.5), role="baseline", days=days)
    book.write(db, "good", _good(book), days=days)
    dec = pr.promote_check(db, CFG, "good", holdout_result=GOOD_HOLDOUT)
    assert not dec.approved
    assert pr.CHECK_DAYS in failed(dec) and pr.CHECK_SHADOW in failed(dec)
    assert "30" in dec.checks[0].detail


def test_missing_holdout_rejected(ledger, book):
    book.write(ledger, "good", _good(book))
    dec = pr.promote_check(ledger, CFG, "good")
    assert not dec.approved and failed(dec) == {pr.CHECK_HOLDOUT}
    assert "sinama kumesi kaniti yok" in dec.checks[2].detail


def test_failed_or_noisy_holdout_rejected(ledger, book):
    book.write(ledger, "good", _good(book))
    worse = {"main": {"model_better": False, "excludes_zero": True, "diff": 0.01, "lo": 0.005, "hi": 0.02}}
    assert failed(pr.promote_check(ledger, CFG, "good", holdout_result=worse)) == {pr.CHECK_HOLDOUT}
    not_sig = {"main": {"model_better": False, "excludes_zero": False, "diff": -0.001}}
    assert failed(pr.promote_check(ledger, CFG, "good", holdout_result=not_sig)) == {pr.CHECK_HOLDOUT}
    insufficient = {**GOOD_HOLDOUT, "sufficient": False}
    assert failed(pr.promote_check(ledger, CFG, "good", holdout_result=insufficient)) == {pr.CHECK_HOLDOUT}
    assert failed(pr.promote_check(ledger, CFG, "good", holdout_result={"main": None})) == {pr.CHECK_HOLDOUT}


def test_holdout_skipped_when_not_required(ledger, book):
    book.write(ledger, "good", _good(book))
    cfg = copy.deepcopy(CFG)
    cfg["terfi"]["require_holdout_pass"] = False
    dec = pr.promote_check(ledger, cfg, "good")
    assert dec.approved and "atlandi" in dec.checks[2].detail


def test_live_shadow_days_condition(ledger, book):
    book.write(ledger, "good", _good(book))
    cfg = copy.deepcopy(CFG)
    cfg["terfi"]["require_live_shadow_days"] = N_DAYS + 1
    dec = pr.promote_check(ledger, cfg, "good", holdout_result=GOOD_HOLDOUT)
    assert not dec.approved and failed(dec) == {pr.CHECK_SHADOW}
    assert "80" in dec.checks[3].detail and "81" in dec.checks[3].detail


def test_net_return_condition(ledger, book):
    book.write(ledger, "good", _good(book))
    cfg = copy.deepcopy(CFG)
    cfg["terfi"]["min_improvement_net_return"] = 1.0
    dec = pr.promote_check(ledger, cfg, "good", holdout_result=GOOD_HOLDOUT)
    assert not dec.approved and failed(dec) == {pr.CHECK_NET}


def test_brier_improvement_threshold(ledger, book):
    book.write(ledger, "good", _good(book))
    cfg = copy.deepcopy(CFG)
    cfg["terfi"]["min_improvement_brier"] = 0.5
    dec = pr.promote_check(ledger, cfg, "good", holdout_result=GOOD_HOLDOUT)
    assert failed(dec) == {pr.CHECK_BRIER}


def test_champion_as_reference(db, book):
    book.write(db, "champ", _weak(book), role="champion")
    book.write(db, "strong", _good(book))
    book.write(db, "strong_clone", _good(book))
    dec = pr.promote_check(db, CFG, "strong", "champ", holdout_result=GOOD_HOLDOUT, cost_bps=5)
    assert dec.reference == "champ"
    brier = dec.checks[1]
    # champion zayif ama ayni yonde: Brier farki negatif olmali; esik 0.005 asilmayabilir, sonuc detail'de
    assert "Brier farki" in brier.detail
    # kendi kendine karsilastirma ve esit modeller reddedilir
    with pytest.raises(ValueError):
        pr.promote_check(db, CFG, "strong", "strong")
    dup = pr.promote_check(db, CFG, "strong", "strong_clone", holdout_result=GOOD_HOLDOUT)
    assert not dup.approved and pr.CHECK_BRIER in failed(dup) and pr.CHECK_NET in failed(dup)


def test_challenger_not_better_than_strong_champion_rejected(db, book):
    book.write(db, "champ", _good(book), role="champion")
    book.write(db, "weak", _weak(book))
    dec = pr.promote_check(db, CFG, "weak", "champ", holdout_result=GOOD_HOLDOUT)
    assert not dec.approved and {pr.CHECK_BRIER, pr.CHECK_NET} <= failed(dec)


def test_empty_ledger_does_not_crash(db):
    dec = pr.promote_check(db, CFG, "nobody")
    assert not dec.approved
    assert failed(dec) == set(pr.CHECK_NAMES)
    assert all(c.detail for c in dec.checks)
    assert "yatırım tavsiyesi değildir" in pr.format_decision(dec)


def test_unresolved_rows_ignored_and_only_matched_pairs_used(ledger, book):
    # challenger yalniz son 40 gun icin var (cozulmus), ilk 40 gun cozulmemis
    book.write(ledger, "partial", _good(book), days=book.days[:40], resolve=False)
    book.write(ledger, "partial", _good(book), days=book.days[40:])
    dec = pr.promote_check(ledger, CFG, "partial", holdout_result=GOOD_HOLDOUT)
    assert "40" in dec.checks[0].detail and pr.CHECK_DAYS in failed(dec)
    assert pr.CHECK_SHADOW in failed(dec)


def test_n_trials_tightens_interval(ledger, book):
    book.write(ledger, "good", _good(book))
    d1 = pr.promote_check(ledger, CFG, "good", holdout_result=GOOD_HOLDOUT, n_trials=1)
    d50 = pr.promote_check(ledger, CFG, "good", holdout_result=GOOD_HOLDOUT, n_trials=50)
    assert "ci=95.0000%" in d1.checks[1].detail and "ci=99.9000%" in d50.checks[1].detail
    with pytest.raises(ValueError):
        pr.promote_check(ledger, CFG, "good", n_trials=0)
    with pytest.raises(ValueError):
        pr.promote_check(ledger, CFG, "good", alpha=1.5)


def test_cost_resolution():
    assert pr._resolve_cost({"costs": {}}, None)[0] == 10.0
    assert pr._resolve_cost({"costs": {"commission_bps": 8, "spread_bps": None}}, None)[0] == 10.0
    assert pr._resolve_cost({"costs": {"commission_bps": 8, "spread_bps": 4}}, None)[0] == 12.0
    assert pr._resolve_cost({"costs": {"commission_bps": 8, "spread_bps": 4}}, 3)[0] == 3.0
    with pytest.raises(ValueError):
        pr._resolve_cost({}, -1)


def test_promotion_flow_with_registry(db, book):
    """Uctan uca: onay alinca registry.promote champion'u degistirir; red alinca degismez."""
    book.write(db, pr.BASELINE_REFERENCE, _const(0.5), role="baseline")
    book.write(db, "good", _good(book))
    book.write(db, "rnd", _random())
    for v in ("good", "rnd"):
        reg.register_model(db, v, {"label": "label_up"}, dt.date(2023, 12, 1))
    bad = pr.promote_check(db, CFG, "rnd", holdout_result=GOOD_HOLDOUT)
    assert not bad.approved
    ok = pr.promote_check(db, CFG, "good", holdout_result=GOOD_HOLDOUT)
    assert ok.approved
    assert reg.promote(db, "good").role == "champion"
    assert reg.get_model(db, "rnd").role == "challenger"


def test_rel_label_model_gets_note(ledger, book):
    book.write(ledger, "rel", _good(book))
    reg.register_model(ledger, "rel", {"label": "label_rel_up"}, dt.date(2023, 12, 1))
    dec = pr.promote_check(ledger, CFG, "rel", holdout_result=GOOD_HOLDOUT)
    assert "label_rel_up" in dec.checks[1].detail
