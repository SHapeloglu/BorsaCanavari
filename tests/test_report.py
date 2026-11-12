"""Rapor modulu ve CLI testleri (PROJE.md Bolum 11, 16). Her test KENDI izole veritabaninda calisir."""

from __future__ import annotations

import copy
import datetime as dt
import math
import uuid

import numpy as np
import pandas as pd
import psycopg
import pytest
from conftest import APP_PASSWORD

from bist_tahmin import cli
from bist_tahmin.config import load_config
from bist_tahmin.db import APP_ROLE, run_init, with_overrides
from bist_tahmin.evaluate.report import (
    BASE_RATE_VERSION,
    DISCLAIMER,
    Report,
    build_report,
    render_markdown,
)

CFG = load_config("config/default.yaml")
AS_OF = dt.date(2026, 12, 31)
TICKERS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
H = 5


def _cfg(**boot):
    c = copy.deepcopy(CFG)
    c["terfi"]["bootstrap"].update(resamples=200, **boot)
    return c


@pytest.fixture()
def db(admin_conninfo):
    name = f"bist_report_{uuid.uuid4().hex[:8]}"
    run_init(admin_conninfo, db_name=name, app_password=APP_PASSWORD)
    info = with_overrides(admin_conninfo, dbname=name, user=APP_ROLE, password=APP_PASSWORD)
    with psycopg.connect(info, autocommit=True) as conn:
        yield conn, info


def _put(conn, *, ticker, made_at, p, y, ret=None, model, role="baseline", score="same", resolved=True,
         resolved_at=None):
    made_at = pd.Timestamp(made_at).date()
    score = p if score == "same" else score
    conn.execute(
        "INSERT INTO predictions (ticker, made_at, horizon_days, prob_up, score, model_version, model_role,"
        " feature_hash, features_json, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,'h','{}'::jsonb, now())",
        (ticker, made_at, H, p, score, model, role))
    if resolved:
        r_at = resolved_at or made_at + dt.timedelta(days=7)
        conn.execute(
            "UPDATE predictions SET resolved_at=%s, realized_ret=%s, realized_rel_ret=%s, tradable_ret=%s,"
            " label_up=%s, label_rel_up=%s WHERE ticker=%s AND made_at=%s AND horizon_days=%s"
            " AND model_version=%s",
            (r_at, ret if ret is not None else (0.01 if y else -0.01), 0.0, 0.0, y, y, ticker, made_at, H, model))


def _labels(n_days=12, seed=0):
    """(dates, {(ticker, date): y}) rastgele ama sabit etiketler."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2026-09-01", periods=n_days)
    return dates, {(t, d): int(rng.integers(0, 2)) for d in dates for t in TICKERS}


def _fill(conn, model, prob_fn, dates, labels, role="baseline", **kw):
    for d in dates:
        for t in TICKERS:
            y = labels[(t, d)]
            _put(conn, ticker=t, made_at=d, p=prob_fn(y), y=y, model=model, role=role, **kw)


def _one(report: Report, model: str):
    return next(m for m in report.models if m.model_version == model)


# --- elle hesaplanmis degerler ------------------------------------------------------------------------------

def test_brier_accuracy_logloss_match_hand_calculation(db):
    conn, _ = db
    mv = "baseline_hand_v1"
    rows = [("AAA", "2026-09-01", 0.8, 1), ("BBB", "2026-09-01", 0.6, 0),
            ("AAA", "2026-09-02", 0.3, 0), ("BBB", "2026-09-02", 0.5, 1)]
    for t, d, p, y in rows:
        _put(conn, ticker=t, made_at=d, p=p, y=y, model=mv)
    rep = build_report(conn, _cfg(), as_of=AS_OF, model_versions=[mv])
    s = _one(rep, mv)
    assert (s.n_predictions, s.n_dates) == (4, 2)
    assert s.brier == pytest.approx((0.04 + 0.36 + 0.09 + 0.25) / 4)       # 0.185
    assert s.accuracy == pytest.approx(0.75)                                # 0.5 >= esik -> 1
    assert s.log_loss == pytest.approx(-(math.log(0.8) + math.log(0.4) + math.log(0.7) + math.log(0.5)) / 4)
    assert s.label_base_rate == pytest.approx(0.5)
    assert s.brier_const_base_rate == pytest.approx(0.25)
    assert sum(c["n"] for c in s.calibration) == 4
    assert s.role == "baseline"


def test_ic_is_computed_from_score_and_realized_ret(db):
    conn, _ = db
    mv = "champ_ic_v1"
    dates, labels = _labels(12, seed=3)
    rng = np.random.default_rng(5)
    for d in dates:
        rets = rng.normal(0, 0.02, len(TICKERS))
        order = np.argsort(np.argsort(rets))            # skor = getiri sirasi -> IC = 1
        for t, r, o in zip(TICKERS, rets, order):
            _put(conn, ticker=t, made_at=d, p=0.5, y=int(r > 0), ret=float(r), model=mv, role="champion",
                 score=float(o))
    s = _one(build_report(conn, _cfg(), as_of=AS_OF, model_versions=[mv]), mv)
    assert s.ic_n_days == 12 and s.ic_mean == pytest.approx(1.0) and s.ic_score_source == "score"
    assert s.role == "champion"


def test_constant_baseline_has_undefined_ic_with_warning(db):
    conn, _ = db
    dates, labels = _labels(6)
    _fill(conn, BASE_RATE_VERSION, lambda y: 0.55, dates, labels)
    s = _one(build_report(conn, _cfg(), as_of=AS_OF), BASE_RATE_VERSION)
    assert s.ic_n_days == 0 and s.ic_mean is None and any("IC tanımsız" in w for w in s.warnings)


# --- baz orana gore Brier farki -----------------------------------------------------------------------------

def test_brier_vs_base_rate_sign_for_better_worse_equal(db):
    conn, _ = db
    dates, labels = _labels(12, seed=1)
    _fill(conn, BASE_RATE_VERSION, lambda y: 0.5, dates, labels)
    _fill(conn, "m_good_v1", lambda y: 0.9 if y else 0.1, dates, labels, role="challenger")
    _fill(conn, "m_bad_v1", lambda y: 0.1 if y else 0.9, dates, labels, role="challenger")
    _fill(conn, "m_equal_v1", lambda y: 0.5, dates, labels, role="challenger")
    rep = build_report(conn, _cfg(), as_of=AS_OF)
    good = _one(rep, "m_good_v1").brier_vs_base_rate
    bad = _one(rep, "m_bad_v1").brier_vs_base_rate
    eq = _one(rep, "m_equal_v1").brier_vs_base_rate
    ref = _one(rep, BASE_RATE_VERSION).brier_vs_base_rate
    # iyi: 0.01 - 0.25 = -0.24 ; kotu: 0.81 - 0.25 = +0.56 ; esit: 0
    assert good["point"] == pytest.approx(-0.24) and good["hi"] < 0 and good["excludes_zero"] is True
    assert bad["point"] == pytest.approx(0.56) and bad["lo"] > 0 and bad["excludes_zero"] is True
    assert eq["point"] == pytest.approx(0.0) and eq["excludes_zero"] is False
    assert eq["n_rows"] == 12 * len(TICKERS) and eq["n_dates"] == 12
    assert ref["is_reference"] is True and ref["excludes_zero"] is False


def test_noisy_small_difference_does_not_exclude_zero(db):
    """Gun-gun isaret degisen fark: aralik sifiri icerir (belirsizlik gosterilir, sismirilmez)."""
    conn, _ = db
    dates, labels = _labels(20, seed=2)
    _fill(conn, BASE_RATE_VERSION, lambda y: 0.5, dates, labels)
    for i, d in enumerate(dates):
        for t in TICKERS:
            y = labels[(t, d)]
            good_day = i % 2 == 0     # cift gunler model iyi, tek gunler kotu -> ortalama ~0
            w = 1 - math.sqrt(0.41)         # kayip 0.41 -> fark +0.16; iyi gun kayip 0.09 -> fark -0.16
            p = (0.7 if y else 0.3) if good_day else (w if y else 1 - w)
            _put(conn, ticker=t, made_at=d, p=p, y=y, model="m_flip_v1", role="challenger")
    d = _one(build_report(conn, _cfg(block_days=3), as_of=AS_OF), "m_flip_v1").brier_vs_base_rate
    assert d["point"] == pytest.approx(0.0, abs=1e-12)
    assert d["lo"] < 0 < d["hi"] and d["excludes_zero"] is False


def test_missing_base_rate_gives_none_and_warning(db):
    conn, _ = db
    dates, labels = _labels(6)
    _fill(conn, "m_only_v1", lambda y: 0.6, dates, labels, role="challenger")
    s = _one(build_report(conn, _cfg(), as_of=AS_OF), "m_only_v1")
    assert s.brier_vs_base_rate is None
    assert any(BASE_RATE_VERSION in w for w in s.warnings)


def test_unmatched_base_rate_rows_give_none_and_warning(db):
    conn, _ = db
    dates, labels = _labels(6)
    _fill(conn, BASE_RATE_VERSION, lambda y: 0.5, dates[:3], labels)
    _fill(conn, "m_late_v1", lambda y: 0.6, dates[3:], labels, role="challenger")
    s = _one(build_report(conn, _cfg(), as_of=AS_OF), "m_late_v1")
    assert s.brier_vs_base_rate is None and any("eşleşen" in w for w in s.warnings)


def test_partial_match_uses_only_matched_rows(db):
    conn, _ = db
    dates, labels = _labels(8, seed=4)
    _fill(conn, BASE_RATE_VERSION, lambda y: 0.5, dates[:6], labels)
    _fill(conn, "m_part_v1", lambda y: 0.9 if y else 0.1, dates, labels, role="challenger")
    d = _one(build_report(conn, _cfg(), as_of=AS_OF), "m_part_v1").brier_vs_base_rate
    assert d["n_rows"] == 6 * len(TICKERS) and d["n_dates"] == 6 and d["n_unmatched_model_rows"] == 2 * len(TICKERS)


def test_bootstrap_is_seeded_and_uses_config(db):
    conn, _ = db
    dates, labels = _labels(15, seed=6)
    _fill(conn, BASE_RATE_VERSION, lambda y: 0.5, dates, labels)
    rng = np.random.default_rng(8)
    for d in dates:
        for t in TICKERS:
            y = labels[(t, d)]
            _put(conn, ticker=t, made_at=d, p=float(np.clip(0.5 + (y - 0.5) * rng.uniform(-0.2, 0.5), 0.01, 0.99)),
                 y=y, model="m_seed_v1", role="challenger")
    a = _one(build_report(conn, _cfg(block_days=3), as_of=AS_OF, seed=7), "m_seed_v1").brier_vs_base_rate
    b = _one(build_report(conn, _cfg(block_days=3), as_of=AS_OF, seed=7), "m_seed_v1").brier_vs_base_rate
    assert a == b and a["resamples"] == 200 and a["block_days"] == 3 and a["ci"] == 0.95


def test_single_day_gives_no_interval(db):
    conn, _ = db
    dates, labels = _labels(1)
    _fill(conn, BASE_RATE_VERSION, lambda y: 0.5, dates, labels)
    _fill(conn, "m_one_v1", lambda y: 0.9 if y else 0.1, dates, labels, role="challenger")
    d = _one(build_report(conn, _cfg(), as_of=AS_OF), "m_one_v1").brier_vs_base_rate
    assert d["lo"] is None and d["hi"] is None and d["excludes_zero"] is False and d["point"] < 0


# --- uyarilar, bos veri, etiketler --------------------------------------------------------------------------

def test_insufficient_dates_warning_and_sufficient_case(db):
    conn, _ = db
    dates, labels = _labels(12)
    _fill(conn, BASE_RATE_VERSION, lambda y: 0.5, dates, labels)
    rep = build_report(conn, _cfg(), as_of=AS_OF)
    assert rep.effective["sufficient"] is False and "yetersiz/gürültü" in rep.effective["warning"]
    assert any("istatistiksel olarak yetersiz/gürültü" in w for w in rep.models[0].warnings)
    assert "yetersiz/gürültü" in rep.text
    cfg = _cfg()
    cfg["terfi"]["min_resolved_dates"] = 12
    rep2 = build_report(conn, cfg, as_of=AS_OF)
    assert rep2.effective["sufficient"] is True and rep2.effective["warning"] is None
    assert not any("yetersiz" in w for w in rep2.models[0].warnings)


def test_empty_database_does_not_crash(db):
    conn, _ = db
    rep = build_report(conn, _cfg(), as_of=AS_OF)
    assert rep.models == [] and rep.effective["n_resolved"] == 0 and rep.effective["n_dates"] == 0
    assert "çözülmüş tahmin yok" in rep.text and "horizon_days" in rep.text
    assert rep.text.rstrip().endswith(DISCLAIMER)
    assert rep.portfolio is None and "henüz hesaplanmadı" in rep.text


def test_only_unresolved_predictions_are_listed_with_zero_n(db):
    conn, _ = db
    for t in TICKERS:
        _put(conn, ticker=t, made_at="2026-09-01", p=0.6, y=1, model="m_pending_v1", role="challenger",
             resolved=False)
    rep = build_report(conn, _cfg(), as_of=AS_OF)
    s = _one(rep, "m_pending_v1")
    assert s.n_predictions == 0 and s.n_pending == len(TICKERS) and s.brier is None
    assert "çözülmüş tahmin yok" in rep.text


def test_as_of_excludes_rows_resolved_later(db):
    conn, _ = db
    for i, t in enumerate(TICKERS):
        _put(conn, ticker=t, made_at="2026-09-01", p=0.6, y=1, model="m_asof_v1", role="challenger",
             resolved_at=dt.date(2026, 9, 8) if i < 4 else dt.date(2026, 9, 20))
    s = _one(build_report(conn, _cfg(), as_of=dt.date(2026, 9, 10)), "m_asof_v1")
    assert s.n_predictions == 4 and s.n_pending == 2
    s2 = _one(build_report(conn, _cfg(), as_of=dt.date(2026, 9, 30)), "m_asof_v1")
    assert s2.n_predictions == 6 and s2.n_pending == 0


def test_role_filter_keeps_base_rate_reference(db):
    conn, _ = db
    dates, labels = _labels(6)
    _fill(conn, BASE_RATE_VERSION, lambda y: 0.5, dates, labels)
    _fill(conn, "m_champ_v1", lambda y: 0.9 if y else 0.1, dates, labels, role="champion")
    rep = build_report(conn, _cfg(), as_of=AS_OF, roles=["champion"])
    assert [m.model_version for m in rep.models] == ["m_champ_v1"]
    assert rep.models[0].brier_vs_base_rate["point"] < 0
    rep_b = build_report(conn, _cfg(), as_of=AS_OF, roles=["baseline"])
    assert [m.model_version for m in rep_b.models] == [BASE_RATE_VERSION]


@pytest.mark.fast
def test_disclaimer_in_every_report(db):
    conn, _ = db
    assert build_report(conn, _cfg(), as_of=AS_OF).text.rstrip().endswith(
        "Bu çıktı yatırım tavsiyesi değildir; yalnızca model ölçüm amaçlıdır.")
    dates, labels = _labels(6)
    _fill(conn, BASE_RATE_VERSION, lambda y: 0.5, dates, labels)
    rep = build_report(conn, _cfg(), as_of=AS_OF)
    assert rep.text.rstrip().endswith(DISCLAIMER) and rep.text == render_markdown(rep)


def test_costs_label_null_config_says_unverified(db):
    conn, _ = db
    rep = build_report(conn, _cfg(), as_of=AS_OF)
    assert "DOĞRULANMADI" in rep.costs_label and "tahmini/maliyetsiz" in rep.costs_label
    assert rep.costs_label in rep.text
    cfg = _cfg()
    cfg["costs"]["commission_bps"] = 5.0
    assert "DOĞRULANMADI" in build_report(conn, cfg, as_of=AS_OF).costs_label     # yalniz biri dolu
    cfg["costs"]["spread_bps"] = 3.0
    lab = build_report(conn, cfg, as_of=AS_OF).costs_label
    assert "DOĞRULANMADI" not in lab and "5.0 bps" in lab and "3.0 bps" in lab


# --- portfoy kancasi ----------------------------------------------------------------------------------------

def test_portfolio_hook_called_per_model_and_rendered(db):
    conn, _ = db
    dates, labels = _labels(6)
    _fill(conn, BASE_RATE_VERSION, lambda y: 0.5, dates, labels)
    _fill(conn, "m_pf_v1", lambda y: 0.6, dates, labels, role="challenger")
    calls = []

    def fn(df, cfg):
        calls.append((set(df["model_version"]), len(df), "tradable_ret" in df.columns, cfg is not None))
        if df["model_version"].iloc[0] == "m_pf_v1":
            return {"excess_return": 0.0123, "grid": [{"cost_bps": 0, "net": 0.01}, {"cost_bps": 10, "net": 0.005}]}
        return None

    rep = build_report(conn, _cfg(), as_of=AS_OF, portfolio_fn=fn)
    assert sorted(c[0].pop() for c in calls) == sorted([BASE_RATE_VERSION, "m_pf_v1"])
    assert all(c[1] == 6 * len(TICKERS) and c[2] and c[3] for c in calls)
    assert rep.portfolio["m_pf_v1"]["excess_return"] == 0.0123 and rep.portfolio[BASE_RATE_VERSION] is None
    assert "excess_return: 0.0123" in rep.text and "| cost_bps | net |" in rep.text
    assert "henüz hesaplanmadı" not in rep.text and "hesaplanamadı" in rep.text


def test_portfolio_hook_error_becomes_warning(db):
    conn, _ = db
    dates, labels = _labels(4)
    _fill(conn, BASE_RATE_VERSION, lambda y: 0.5, dates, labels)

    def boom(df, cfg):
        raise RuntimeError("kaboom")

    rep = build_report(conn, _cfg(), as_of=AS_OF, portfolio_fn=boom)
    assert rep.portfolio[BASE_RATE_VERSION] is None
    assert any("kaboom" in w for w in rep.models[0].warnings)


# --- markdown -----------------------------------------------------------------------------------------------

def test_render_markdown_contains_sample_and_day_counts(db):
    conn, _ = db
    dates, labels = _labels(12, seed=9)
    _fill(conn, BASE_RATE_VERSION, lambda y: 0.5, dates, labels)
    _fill(conn, "m_txt_v1", lambda y: 0.9 if y else 0.1, dates, labels, role="challenger")
    rep = build_report(conn, _cfg(), as_of=AS_OF)
    text = render_markdown(rep)
    n = 12 * len(TICKERS)
    row = next(l for l in text.splitlines() if l.startswith("| m_txt_v1 |"))
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert cells[2] == str(n) and cells[3] == "12"
    assert "excludes_zero=evet" in text and "Kalibrasyon" in text and BASE_RATE_VERSION in text
    assert f"{n} tahmin, 12 farklı gün" in text
    assert "2026-12-31" in text


def test_report_does_not_write_to_database(db):
    conn, _ = db
    dates, labels = _labels(4)
    _fill(conn, BASE_RATE_VERSION, lambda y: 0.5, dates, labels)
    before = conn.execute("SELECT (SELECT count(*) FROM predictions), (SELECT count(*) FROM runs),"
                          " (SELECT count(*) FROM experiments)").fetchone()
    build_report(conn, _cfg(), as_of=AS_OF)
    after = conn.execute("SELECT (SELECT count(*) FROM predictions), (SELECT count(*) FROM runs),"
                         " (SELECT count(*) FROM experiments)").fetchone()
    assert before == after


# --- CLI ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [["baseline", "report"], ["report", "weekly"]])
@pytest.mark.fast
def test_cli_requires_database_url(monkeypatch, capsys, argv):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert cli.main(["--config", "config/default.yaml", *argv]) == 2
    assert "DATABASE_URL" in capsys.readouterr().err


def test_cli_report_weekly_prints_and_writes_file(db, monkeypatch, capsys, tmp_path):
    conn, info = db
    dates, labels = _labels(6)
    _fill(conn, BASE_RATE_VERSION, lambda y: 0.5, dates, labels)
    _fill(conn, "m_cli_v1", lambda y: 0.6, dates, labels, role="champion")
    monkeypatch.setenv("DATABASE_URL", info)
    out = tmp_path / "yeni" / "alt" / "rapor.md"
    before = conn.execute("SELECT count(*) FROM predictions").fetchone()[0]
    rc = cli.main(["--config", "config/default.yaml", "report", "weekly", "--as-of", "2026-12-31",
                   "--out", str(out), "--dry-run"])
    cap = capsys.readouterr()
    assert rc == 0 and DISCLAIMER in cap.out and "m_cli_v1" in cap.out        # tum roller
    assert out.read_text(encoding="utf-8") == cap.out
    assert conn.execute("SELECT count(*) FROM predictions").fetchone()[0] == before


def test_cli_baseline_report_only_baseline_rows_and_no_default_file(db, monkeypatch, capsys, tmp_path):
    conn, info = db
    dates, labels = _labels(6)
    _fill(conn, BASE_RATE_VERSION, lambda y: 0.5, dates, labels)
    _fill(conn, "m_cli2_v1", lambda y: 0.6, dates, labels, role="champion")
    monkeypatch.setenv("DATABASE_URL", info)
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["--config", str(__import__("pathlib").Path(__file__).resolve().parents[1] / "config/default.yaml"),
                   "baseline", "report", "--as-of", "2026-12-31"])
    cap = capsys.readouterr()
    assert rc == 0 and BASE_RATE_VERSION in cap.out and "m_cli2_v1" not in cap.out
    assert list(tmp_path.iterdir()) == []                                      # varsayilan dosya yok


def test_cli_invalid_as_of_exits_2(db, monkeypatch, capsys):
    _, info = db
    monkeypatch.setenv("DATABASE_URL", info)
    assert cli.main(["--config", "config/default.yaml", "report", "weekly", "--as-of", "dun"]) == 2
    assert "hata" in capsys.readouterr().err
