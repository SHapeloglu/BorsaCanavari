"""models.promote: Bolum 11 karar kurali. Sentetik CANLI defter (dogrudan INSERT + cozum UPDATE) uzerinde:
kasitli kotu challenger reddedilir; iyi challenger yalniz TUM kosullar saglaninca onaylanir.
Baz oran (baseline_base_rate_v1) her zaman referanstir (PROJE Bolum 0). Champion verilirse kosul 2 (Brier) ve kosul 5
(net getiri) icin challenger champion'dan da iyi olmalidir; iki karsilastirma da gecmelidir. Champion yoksa yalnizca
baz orana karsi. Sinama kosulu holdout'u disarida tutmus ve ayni spec'ten bir challenger ister. Canli golge kosulu
gun sayisina ek olarak baz orana karsi iyilesme ister (champion kosulu degistirmez)."""

from __future__ import annotations

import copy
import datetime as dt
import uuid

import numpy as np
import pandas as pd
import psycopg
import pytest
from conftest import APP_PASSWORD

from bist_tahmin.backtest import lab
from bist_tahmin.config import load_config
from bist_tahmin.db import APP_ROLE, run_init, with_overrides
from bist_tahmin.models import promote as pr
from bist_tahmin.models import registry as reg

CFG = load_config("config/default.yaml")
TICKERS = [f"T{i:02d}" for i in range(10)]
N_DAYS = 80
FEATS_P = ["ret_1_z", "ret_5_z", "mkt_ret_1"]
SPEC_P = lab.ModelSpec(name="p", features=FEATS_P, C=1.0)
SPEC_OTHER = lab.ModelSpec(name="p", features=FEATS_P, C=0.5)     # ayni ad, farkli parametre
SPEC_REL = lab.ModelSpec(name="rel", label="label_rel_up", features=FEATS_P)
BASE = pr.BASELINE_REFERENCE
GOOD_HOLDOUT = {"main": {"model_better": True, "excludes_zero": True, "diff": -0.02, "lo": -0.03, "hi": -0.01,
                         "n_dates": 120}, "n_dates": 120, "sufficient": True,
                "run_info": {"spec": SPEC_P.params()}}
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


def _reg(conn, version, spec=SPEC_P, *, include_holdout=False):
    """Challenger kaydi (egitim meta verisi). Zaten varsa dokunmaz."""
    if reg.get_model(conn, version) is not None:
        return
    extra = {"spec": spec.params(), "train_includes_holdout": include_holdout, "holdout_start": "2026-05-01"}
    reg.register_model(conn, version, {"label": spec.label, "extra": extra}, dt.date(2023, 12, 1))


class Book:
    """Sentetik defter: gizli sinyal s; gercek yukselis olasiligi p = 0.5 + 0.3 tanh(s)."""

    def __init__(self, n_days=N_DAYS, seed=0):
        rng = np.random.default_rng(seed)
        self.days = list(pd.bdate_range("2024-01-02", periods=n_days).date)
        self.s = {(t, d): rng.normal() for d in self.days for t in TICKERS}
        self.p = {k: 0.5 + 0.3 * np.tanh(v) for k, v in self.s.items()}
        self.y = {k: float(rng.random() < p) for k, p in self.p.items()}
        self.ret = {k: 0.02 * (2 * y - 1) + rng.normal(0, 0.004) for k, y in self.y.items()}

    def write(self, conn, version, prob_fn, role="challenger", days=None, resolve=True, spec=SPEC_P,
              include_holdout=False):
        if role == "challenger":  # challenger her zaman kayitli olur (promote kaydi gerektirir)
            _reg(conn, version, spec, include_holdout=include_holdout)
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


def _noisy(book, sigma=1.5, seed=7):  # kismen bilgili: siralamasi gercek sinyalden kotu (monoton donusum degil)
    rng = np.random.default_rng(seed)
    noise = {k: rng.normal() for k in book.s}
    return lambda t, d: float(0.5 + 0.3 * np.tanh(book.s[(t, d)] + sigma * noise[(t, d)]))


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
    book.write(db, BASE, _const(0.5), role="baseline")
    return db


# ---------------------------------------------------------------- kabul testleri

def test_good_challenger_approved_only_when_all_conditions_hold(ledger, book):
    book.write(ledger, "good", _good(book))
    dec = pr.promote_check(ledger, CFG, "good", holdout_result=GOOD_HOLDOUT)
    assert dec.approved, pr.format_decision(dec)
    assert [c.name for c in dec.checks] == list(pr.CHECK_NAMES) and all(c.ok for c in dec.checks)
    assert dec.reference == BASE and dec.challenger == "good"
    assert dec.summary.startswith("ONAY") and dec.info == []
    # her kosulun detail'i sayi icerir
    assert "80" in dec.checks[0].detail and "[" in dec.checks[1].detail


def test_deliberately_bad_challengers_are_rejected_with_reasons(ledger, book):
    book.write(ledger, "rnd", _random())
    book.write(ledger, "rev", _reversed(book))
    # rnd: rastgele secim, referans (sabit 0.5 = tum evren secilir, net = -maliyet) uzerinde net fazla getiri
    # orneklemde gecebilir (ortalama gross fazla ~0, referans -2*maliyet). Red yine Brier'dan gelir; net kosulu
    # yalniz ters isaret (rev) icin beklenir, cunku o gercekten kotu.
    expected_net_fail = {"rnd": False, "rev": True}
    for name in ("rnd", "rev"):
        dec = pr.promote_check(ledger, CFG, name, holdout_result=GOOD_HOLDOUT)
        assert not dec.approved
        assert pr.CHECK_BRIER in failed(dec), pr.format_decision(dec)
        assert (pr.CHECK_NET in failed(dec)) is expected_net_fail[name], pr.format_decision(dec)
        assert "RED" in dec.summary and pr.CHECK_BRIER in dec.summary
        text = pr.format_decision(dec)
        assert "KALDI" in text
        assert "yatırım tavsiyesi değildir" in text


def test_too_few_days_rejected(db, book):
    days = book.days[:30]
    book.write(db, BASE, _const(0.5), role="baseline", days=days)
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
    worse = {**GOOD_HOLDOUT, "main": {**GOOD_HOLDOUT["main"], "model_better": False, "excludes_zero": True,
                                      "diff": 0.01, "lo": 0.005, "hi": 0.02}}
    assert failed(pr.promote_check(ledger, CFG, "good", holdout_result=worse)) == {pr.CHECK_HOLDOUT}
    not_sig = {**GOOD_HOLDOUT, "main": {**GOOD_HOLDOUT["main"], "model_better": False, "excludes_zero": False,
                                        "diff": -0.001}}
    assert failed(pr.promote_check(ledger, CFG, "good", holdout_result=not_sig)) == {pr.CHECK_HOLDOUT}
    insufficient = {**GOOD_HOLDOUT, "sufficient": False}
    assert failed(pr.promote_check(ledger, CFG, "good", holdout_result=insufficient)) == {pr.CHECK_HOLDOUT}
    no_main = {**GOOD_HOLDOUT, "main": None}
    assert failed(pr.promote_check(ledger, CFG, "good", holdout_result=no_main)) == {pr.CHECK_HOLDOUT}
    # saglam kanit tek basina yeterli: ayni challenger icin GOOD_HOLDOUT geciyor
    assert pr.promote_check(ledger, CFG, "good", holdout_result=GOOD_HOLDOUT).approved


def test_holdout_skipped_when_not_required(ledger, book):
    book.write(ledger, "good", _good(book))
    cfg = copy.deepcopy(CFG)
    cfg["terfi"]["require_holdout_pass"] = False
    dec = pr.promote_check(ledger, cfg, "good")
    assert dec.approved and "atlandi" in dec.checks[2].detail


# ---------------------------------------------------------------- K1: sinama kumesi butunlugu

def test_holdout_including_training_is_rejected(ledger, book):
    """Holdout'u egitime katmis challenger: kanit ne kadar iyi olursa olsun kosul 3 KALDI."""
    book.write(ledger, "dirty", _good(book), include_holdout=True)
    dec = pr.promote_check(ledger, CFG, "dirty", holdout_result=GOOD_HOLDOUT)
    assert not dec.approved and failed(dec) == {pr.CHECK_HOLDOUT}
    assert "train_includes_holdout=true" in dec.checks[2].detail


def test_holdout_result_spec_must_match_challenger(ledger, book):
    book.write(ledger, "good", _good(book))
    other_spec = {**GOOD_HOLDOUT, "run_info": {"spec": SPEC_OTHER.params()}}
    dec = pr.promote_check(ledger, CFG, "good", holdout_result=other_spec)
    assert failed(dec) == {pr.CHECK_HOLDOUT} and "uyusmuyor" in dec.checks[2].detail
    no_spec = {k: v for k, v in GOOD_HOLDOUT.items() if k != "run_info"}
    dec = pr.promote_check(ledger, CFG, "good", holdout_result=no_spec)
    assert failed(dec) == {pr.CHECK_HOLDOUT} and "spec bilgisi yok" in dec.checks[2].detail


def test_challenger_without_training_record_rejected(ledger, book):
    """Egitim meta verisi olmayan (ya da holdout disarida tutuldugu kaydedilmemis) challenger kanitsiz kalir."""
    reg.register_model(ledger, "legacy", {"label": "label_up"}, dt.date(2023, 12, 1))
    book.write(ledger, "legacy", _good(book))  # kayit zaten var: dokunulmaz
    dec = pr.promote_check(ledger, CFG, "legacy", holdout_result=GOOD_HOLDOUT)
    assert failed(dec) == {pr.CHECK_HOLDOUT} and "dogrulanamadi" in dec.checks[2].detail
    dec = pr.promote_check(ledger, CFG, "unregistered_x", holdout_result=GOOD_HOLDOUT)
    assert pr.CHECK_HOLDOUT in failed(dec) and "dogrulanamadi" in dec.checks[2].detail


# ---------------------------------------------------------------- O2: canli golge iyilesmesi

def test_live_shadow_days_condition(ledger, book):
    book.write(ledger, "good", _good(book))
    cfg = copy.deepcopy(CFG)
    cfg["terfi"]["require_live_shadow_days"] = N_DAYS + 1
    dec = pr.promote_check(ledger, cfg, "good", holdout_result=GOOD_HOLDOUT)
    assert not dec.approved and failed(dec) == {pr.CHECK_SHADOW}
    assert "80" in dec.checks[3].detail and "81" in dec.checks[3].detail


def test_shadow_days_alone_are_not_enough(ledger, book):
    """Gun sayisi yeterli (80 >= 60) ama canli golgede baz orana karsi iyilesme yok -> canli_golge KALDI."""
    book.write(ledger, "rnd", _random())
    dec = pr.promote_check(ledger, CFG, "rnd", holdout_result=GOOD_HOLDOUT)
    assert dec.checks[0].ok and pr.CHECK_SHADOW in failed(dec)
    assert "iyilesme yok" in dec.checks[3].detail
    assert not dec.approved


def test_good_challenger_shadow_improvement_passes(ledger, book):
    book.write(ledger, "good", _good(book))
    dec = pr.promote_check(ledger, CFG, "good", holdout_result=GOOD_HOLDOUT)
    assert dec.checks[3].ok and "iyilesme var" in dec.checks[3].detail


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


# ---------------------------------------------------------------- champion: kosul 2 ve 5'te karar verici

def test_champion_must_be_beaten_as_well_as_base_rate(ledger, book):
    """Champion C baz orana karsi GUCLU; challenger X baz orana karsi iyi ama C'den kotu -> X REDDEDILIR.
    Kosul 2 (Brier) ve/veya kosul 5 (net) KALDI; detay champion farkini gosterir."""
    book.write(ledger, "C", _good(book))
    assert pr.promote_check(ledger, CFG, "C", holdout_result=GOOD_HOLDOUT).approved
    reg.promote(ledger, "C")
    book.write(ledger, "X", _weak(book))
    dec = pr.promote_check(ledger, CFG, "X", "C", holdout_result=GOOD_HOLDOUT)
    assert not dec.approved, pr.format_decision(dec)
    assert failed(dec) and failed(dec) <= {pr.CHECK_BRIER, pr.CHECK_NET}, pr.format_decision(dec)
    brier = dec.checks[1]
    assert brier.sub[pr.SUB_CHAMPION] is False and brier.sub[pr.SUB_BASE] is True, brier.detail
    assert "champion 'C' ile (ANA, karar kosulu)" in brier.detail
    assert "Brier farki (challenger - champion 'C')" in brier.detail
    assert "Brier farki (challenger - baz oran)" in brier.detail
    assert "champion 'C'" in dec.info[0] and "Not: champion 'C' verildi" in pr.format_decision(dec)


def test_challenger_better_than_champion_and_base_rate_is_approved(ledger, book):
    """X hem champion'dan (C: kismen bilgili, siralamasi daha kotu) hem baz orandan iyi -> kosul 2 ve 5 iki
    karsilastirmada da GECER -> onay. (Monoton donusumlu bir champion net getiride X ile ayni kohortlari secer; bu
    yuzden champion siralamasi farkli olmali.)"""
    book.write(ledger, "C", _noisy(book))
    reg.promote(ledger, "C")
    book.write(ledger, "X", _good(book))
    dec = pr.promote_check(ledger, CFG, "X", "C", holdout_result=GOOD_HOLDOUT)
    assert dec.approved, pr.format_decision(dec)
    assert dec.checks[1].sub == {pr.SUB_CHAMPION: True, pr.SUB_BASE: True}, dec.checks[1].detail
    assert dec.checks[4].sub == {pr.SUB_CHAMPION: True, pr.SUB_BASE: True}, dec.checks[4].detail
    assert "champion 'C'" in dec.summary and dec.summary.startswith("ONAY")


def test_better_than_champion_but_worse_than_base_rate_is_rejected(ledger, book):
    """Champion'dan iyi ama baz orandan kotu challenger REDDEDILIR. Champion karsilastirmasi gecer, baz oran
    karsilastirmasi kalir: kosul 2 bu yuzden KALDI."""
    db = ledger
    book.write(db, "champ", _const(0.9))
    reg.promote(db, "champ")
    book.write(db, "rnd", _random())
    dec = pr.promote_check(db, CFG, "rnd", "champ", holdout_result=GOOD_HOLDOUT)
    assert dec.reference == BASE
    assert not dec.approved and pr.CHECK_BRIER in failed(dec)
    brier = dec.checks[1]
    assert brier.sub == {pr.SUB_CHAMPION: True, pr.SUB_BASE: False}, brier.detail
    assert "champion 'champ' ile (ANA, karar kosulu)" in brier.detail


def test_no_champion_only_base_rate_regression(ledger, book):
    """Champion yokken yalniz baz orana karsi karsilastirma (eski davranis); detayda champion gecmez."""
    book.write(ledger, "good", _good(book))
    dec = pr.promote_check(ledger, CFG, "good", holdout_result=GOOD_HOLDOUT)
    assert dec.approved and dec.info == []
    assert dec.checks[1].sub == {pr.SUB_BASE: True} and dec.checks[4].sub == {pr.SUB_BASE: True}
    assert "champion" not in dec.checks[1].detail and "champion" not in dec.checks[4].detail
    assert "champion" not in pr.format_decision(dec)


def test_champion_without_resolved_rows_fails_closed(ledger, book):
    """Champion'in cozulmus tahmini yoksa champion'a karsi iyilesme kanitlanamaz: kosul 2 ve 5 KALDI."""
    book.write(ledger, "good", _good(book))
    dec = pr.promote_check(ledger, CFG, "good", "hayalet", holdout_result=GOOD_HOLDOUT)
    assert not dec.approved
    assert failed(dec) >= {pr.CHECK_BRIER, pr.CHECK_NET}
    assert "hesaplanamadi" in dec.checks[1].detail and "hayalet" in dec.checks[1].detail


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
    assert pr.resolve_cost({"costs": {}}, None)[0] == 10.0 == pr.DEFAULT_COST_BPS
    assert pr.resolve_cost({"costs": {"commission_bps": 8, "spread_bps": None}}, None)[0] == 10.0
    assert pr.resolve_cost({"costs": {"commission_bps": 8, "spread_bps": 4}}, None)[0] == 12.0
    assert pr.resolve_cost({"costs": {"commission_bps": 8, "spread_bps": 4}}, 3)[0] == 3.0
    with pytest.raises(ValueError):
        pr.resolve_cost({}, -1)


def test_promotion_flow_with_registry(db, book):
    """Uctan uca: onay alinca registry.promote champion'u degistirir; red alinca degismez."""
    book.write(db, BASE, _const(0.5), role="baseline")
    book.write(db, "good", _good(book))
    book.write(db, "rnd", _random())
    bad = pr.promote_check(db, CFG, "rnd", holdout_result=GOOD_HOLDOUT)
    assert not bad.approved
    ok = pr.promote_check(db, CFG, "good", holdout_result=GOOD_HOLDOUT)
    assert ok.approved
    assert reg.promote(db, "good").role == "champion"
    assert reg.get_model(db, "rnd").role == "challenger"


def test_rel_label_model_gets_note(ledger, book):
    book.write(ledger, "rel", _good(book), spec=SPEC_REL)
    dec = pr.promote_check(ledger, CFG, "rel", holdout_result=GOOD_HOLDOUT)
    assert "label_rel_up" in dec.checks[1].detail
