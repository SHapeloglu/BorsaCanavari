"""`bist health` (monitor/health.py) ve deploy/cron_health.sh: saglikli / uyari / kritik durumlari."""

from __future__ import annotations

import datetime as dt
import os
import stat
import subprocess
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg
import pytest
from conftest import APP_PASSWORD
from psycopg.types.json import Jsonb

from bist_tahmin import cli
from bist_tahmin.data import store
from bist_tahmin.db import APP_ROLE, run_init, with_overrides
from bist_tahmin.monitor import breaker
from bist_tahmin.monitor.breaker import BreakerVerdict
from bist_tahmin.monitor.health import CRIT, OK, WARN, expected_run_day, run_health

TZ = ZoneInfo("Europe/Istanbul")
FRI = dt.date(2026, 10, 30)
NOW = dt.datetime(2026, 10, 30, 20, 30, tzinfo=TZ)          # Cuma 20:30 Istanbul (cron saati)
ROOT = Path(__file__).resolve().parent.parent


def _ist(y, m, d, hh, mm=0, ss=0):
    return dt.datetime(y, m, d, hh, mm, ss, tzinfo=TZ)


@pytest.fixture()
def hdb(admin_conninfo):
    """Taze DB; takvim Cuma 2026-10-30'a kadar dolu (veri guncel)."""
    name = f"bist_hlt_{uuid.uuid4().hex[:8]}"
    run_init(admin_conninfo, db_name=name, app_password=APP_PASSWORD)
    info = with_overrides(admin_conninfo, dbname=name, user=APP_ROLE, password=APP_PASSWORD)
    with psycopg.connect(info, autocommit=True) as conn:
        store.upsert_trading_calendar(conn, pd.bdate_range(end=pd.Timestamp(FRI), periods=40))
        yield conn, info


def _run(conn, started, status="ok", detail=None, job="daily", finished=True):
    conn.execute(
        "INSERT INTO runs (job, run_date, started_at, finished_at, status, detail) VALUES (%s, %s, %s, %s, %s, %s)",
        (job, started.astimezone(TZ).date(), started, started + dt.timedelta(minutes=2) if finished else None,
         status, Jsonb(detail) if detail is not None else None),
    )


def _healthy_run(conn):
    _run(conn, _ist(2026, 10, 30, 19, 0, 5), "ok", {"exit_code": 0})


def _levels(report):
    return {c.name: c.level for c in report.checks}


def test_healthy_system_exits_zero(hdb):
    conn, _ = hdb
    _healthy_run(conn)
    rep = run_health(conn, now=NOW)
    assert rep.exit_code == 0, rep.lines()
    assert rep.lines()[-1] == "SONUC: SAGLIKLI (cikis kodu 0)"
    assert all(ord(ch) < 128 for line in rep.lines() for ch in line)       # okunur ASCII Turkce


def test_partial_problem_exit_code_is_a_warning(hdb):
    conn, _ = hdb
    _run(conn, _ist(2026, 10, 30, 19, 0, 5), "ok", {"exit_code": 1})
    rep = run_health(conn, now=NOW)
    assert rep.exit_code == WARN and _levels(rep)["gunluk akis"] == WARN
    assert any("cikis kodu 1" in line for line in rep.lines())


def test_skipped_run_with_exit_3_is_a_warning(hdb):
    conn, _ = hdb
    _run(conn, _ist(2026, 10, 30, 19, 0, 5), "skipped", {"exit_code": 3})
    rep = run_health(conn, now=NOW)
    assert rep.exit_code == WARN
    assert _levels(rep)["gunluk akis"] == WARN and _levels(rep)["son basarili akis"] == WARN


def test_stuck_daily_run_is_critical(hdb):
    conn, _ = hdb
    _run(conn, _ist(2026, 10, 30, 19, 0, 5), "running", None, finished=False)     # 90 dk once basladi, hala running
    rep = run_health(conn, now=NOW)
    assert _levels(rep)["gunluk akis"] == CRIT and rep.exit_code == CRIT
    assert "is calismadi" in "\n".join(rep.lines())


def test_retry_that_succeeded_supersedes_an_earlier_skipped_run(hdb):
    conn, _ = hdb
    _run(conn, _ist(2026, 10, 30, 19, 0, 5), "skipped", {"exit_code": 3})
    _run(conn, _ist(2026, 10, 30, 19, 45, 5), "ok", {"exit_code": 0})
    assert run_health(conn, now=NOW).exit_code == OK


def test_no_run_since_expected_day_is_critical(hdb):
    conn, _ = hdb
    _run(conn, _ist(2026, 10, 29, 19, 0, 5), "ok", {"exit_code": 0})        # dunku kayit yeterli degil
    rep = run_health(conn, now=NOW)
    assert rep.exit_code == CRIT and "is calismadi" in "\n".join(rep.lines())


def test_failed_run_is_critical_and_shows_error(hdb):
    conn, _ = hdb
    _run(conn, _ist(2026, 10, 30, 19, 0, 5), "failed", {"error": "RuntimeError: XU100 indirilemedi"})
    rep = run_health(conn, now=NOW)
    assert rep.exit_code == CRIT and "XU100 indirilemedi" in "\n".join(rep.lines())


def test_no_runs_and_empty_calendar_are_critical(admin_conninfo):
    name = f"bist_hlt_{uuid.uuid4().hex[:8]}"
    run_init(admin_conninfo, db_name=name, app_password=APP_PASSWORD)
    info = with_overrides(admin_conninfo, dbname=name, user=APP_ROLE, password=APP_PASSWORD)
    with psycopg.connect(info, autocommit=True) as conn:
        rep = run_health(conn, now=NOW)
    assert rep.exit_code == CRIT
    lv = _levels(rep)
    assert lv["gunluk akis"] == CRIT and lv["veri"] == CRIT


def test_stale_bar_is_critical_even_if_a_run_exists(hdb):
    conn, _ = hdb
    # (takvimde silme yetkisi yok: "bayat" durumu, bugunu ileri alarak elde edilir)
    _healthy_run(conn)
    later = dt.datetime(2026, 11, 3, 20, 30, tzinfo=TZ)                    # Sali; son bar hala 30 Ekim
    _run(conn, _ist(2026, 11, 3, 19, 0, 5), "ok", {"exit_code": 0})
    rep = run_health(conn, now=later)
    assert _levels(rep)["veri"] == CRIT and rep.exit_code == CRIT
    assert any("BAYAT" in line for line in rep.lines())


def test_open_circuit_breaker_is_critical(hdb):
    conn, _ = hdb
    _healthy_run(conn)
    breaker.trip(conn, BreakerVerdict(True, 0.2, "test dusus"), "m1", now=NOW)
    rep = run_health(conn, now=NOW)
    assert rep.exit_code == CRIT and _levels(rep)["devre kesici"] == CRIT
    breaker.reset(conn, "incelendi", now=NOW)
    assert run_health(conn, now=NOW).exit_code == OK


def test_stuck_running_row_is_a_warning(hdb):
    conn, _ = hdb
    _healthy_run(conn)
    _run(conn, NOW - dt.timedelta(hours=2), "running", None, job="resolve", finished=False)
    rep = run_health(conn, now=NOW)
    assert rep.exit_code == WARN and _levels(rep)["takili isler"] == WARN
    # 10 dakikalik 'running' takili sayilmaz
    conn.execute("UPDATE runs SET status='failed' WHERE status='running'")
    _run(conn, NOW - dt.timedelta(minutes=10), "running", None, job="resolve", finished=False)
    assert run_health(conn, now=NOW).exit_code == OK


def test_overdue_unresolved_predictions_are_a_warning(hdb):
    conn, _ = hdb
    _healthy_run(conn)
    conn.execute(
        "INSERT INTO predictions (ticker, made_at, horizon_days, prob_up, score, model_version, model_role,"
        " feature_hash, features_json, created_at) VALUES ('AAA', '2026-10-12', 5, 0.5, 0.5, 'baseline_x_v1',"
        " 'baseline', 'h', '{}'::jsonb, now())")
    rep = run_health(conn, now=NOW)
    assert rep.exit_code == WARN and _levels(rep)["cozum"] == WARN


def test_run_before_19_is_not_expected_yet(hdb):
    conn, _ = hdb
    _run(conn, _ist(2026, 10, 30, 19, 0, 5), "ok", {"exit_code": 0})          # Cuma kaydi
    # Pazartesi 18:45: Pzt akisi 19:00'da kosacak; beklenen gun hala Cuma (hafta sonu atlanir)
    assert expected_run_day(_ist(2026, 11, 2, 18, 45)) == FRI
    assert expected_run_day(_ist(2026, 11, 2, 19, 5)) == dt.date(2026, 11, 2)
    assert expected_run_day(_ist(2026, 11, 2, 10, 0)) == FRI
    assert run_health(conn, now=_ist(2026, 11, 2, 18, 45)).exit_code == OK


def test_old_successful_run_beyond_max_age_is_critical(hdb):
    conn, _ = hdb
    _healthy_run(conn)
    ok = run_health(conn, now=NOW, max_age_hours=72)
    assert ok.exit_code == OK
    rep = run_health(conn, now=NOW, max_age_hours=1.0)                         # 1.5 saat once > 1 saat
    assert rep.exit_code == CRIT and _levels(rep)["son basarili akis"] == CRIT


def test_locked_skipped_record_does_not_count_as_a_run(hdb):
    conn, _ = hdb
    _run(conn, _ist(2026, 10, 30, 19, 0, 5), "skipped", {"reason": "locked"})
    assert run_health(conn, now=NOW).exit_code == CRIT


def test_cli_health_exit_codes(hdb, monkeypatch, capsys):
    conn, info = hdb
    monkeypatch.setenv("DATABASE_URL", info)
    # gercek saat: 2026-10-30 verisi artik bayat ve 'daily' kaydi yok => kritik
    assert cli.main(["health"]) == 2
    out = capsys.readouterr().out
    assert "SONUC: KRITIK" in out
    monkeypatch.delenv("DATABASE_URL")
    assert cli.main(["health"]) == 2


# --- deploy/cron_health.sh ----------------------------------------------------------------------------------------

def _fake_docker(tmp_path: Path, rc: int, output: str) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    d = bindir / "docker"
    d.write_text(f"#!/bin/bash\ncat <<'EOT'\n{output}\nEOT\nexit {rc}\n")
    d.chmod(d.stat().st_mode | stat.S_IEXEC)
    return bindir


def _run_script(tmp_path: Path, rc: int, output: str, **extra_env):
    bindir = _fake_docker(tmp_path, rc, output)
    home = tmp_path / "bist"
    home.mkdir()
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "BIST_DIR": str(home), **extra_env}
    p = subprocess.run(["bash", str(ROOT / "deploy" / "cron_health.sh")], env=env, capture_output=True, text=True,
                       timeout=60)
    return p, home


def test_cron_health_success_does_not_touch_alert_file(tmp_path):
    p, home = _run_script(tmp_path, 0, "[TAMAM] veri: son bar 2026-10-30\nSONUC: SAGLIKLI (cikis kodu 0)")
    assert p.returncode == 0
    assert not (home / "logs" / "ALERT.txt").exists()
    assert "SAGLIKLI" in (home / "logs" / "health.log").read_text()


def test_cron_health_failure_appends_one_timestamped_alert_line(tmp_path):
    out = ("[TAMAM] veri: iyi\n[KRITIK] gunluk akis: is calismadi\n[UYARI] cozum: 3 cozulmemis\n"
           "SONUC: KRITIK (cikis kodu 2)")
    p, home = _run_script(tmp_path, 2, out)
    assert p.returncode == 2
    alert = (home / "logs" / "ALERT.txt").read_text().splitlines()
    assert len(alert) == 1
    assert "cikis=2" in alert[0] and "is calismadi" in alert[0] and "cozulmemis" in alert[0]
    assert alert[0][:4].isdigit() and "veri: iyi" not in alert[0]            # zaman damgasi var; sagliklilar yok
    (tmp_path / "x").mkdir()
    p2, _ = _run_script(tmp_path / "x", 1, "[UYARI] a: b")
    assert p2.returncode == 1                                                  # cikis kodu aynen iletilir


def test_cron_health_only_ist_hour_guard(tmp_path):
    never = "07" if dt.datetime.now(TZ).hour != 7 else "08"
    p, home = _run_script(tmp_path, 2, "[KRITIK] x: y", ONLY_IST_HOUR=never)
    assert p.returncode == 0 and not (home / "logs" / "ALERT.txt").exists()   # saat tutmadi: hicbir sey yapmaz


def test_cron_file_has_health_lines_and_keeps_existing_ones():
    text = (ROOT / "deploy" / "bist-tahmin.cron").read_text()
    assert "ONLY_IST_HOUR=20 /opt/bist-tahmin/deploy/cron_health.sh" in text
    assert "30 18,19 * * 1-5 root" in text
    assert "cron_run.sh run-daily run-daily" in text and "cron_weekly.sh" in text
