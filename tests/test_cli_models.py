"""CLI `bist models ...` (PROJE.md Bolum 14): list, train, promote-check, promote, retire.

Guvenlik kurallari: `promote --confirm` olmadan hicbir sey yapilmaz; terfi yalniz kontrol onaylarsa yapilir;
`retire` gerekcesiz yapilmaz (gerekce runs tablosuna yazilir); holdout dosyasi yoksa sinama kosulu KALDI.
Her test kendi izole veritabanini kullanir (tests/test_daily.py'deki `db` deseni)."""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg
import pytest
from conftest import APP_PASSWORD

from bist_tahmin import cli
from bist_tahmin.backtest.dataset import Dataset, save_dataset
from bist_tahmin.db import APP_ROLE, run_init, with_overrides
from bist_tahmin.features.build import FEATURE_VERSION
from bist_tahmin.models import registry as reg

ROOT = Path(__file__).resolve().parents[1]
CFG = str(ROOT / "config" / "default.yaml")
FEATS = ["ret_1_z", "ret_5_z", "mkt_ret_1"]
TICKERS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
UTC = dt.timezone.utc
END = dt.date(2026, 10, 1)


@pytest.fixture()
def db(admin_conninfo, monkeypatch):
    """Taze izole veritabani; DATABASE_URL (bist_app) ortam degiskeni ona yonlendirilir."""
    name = f"bist_cli_models_{uuid.uuid4().hex[:8]}"
    run_init(admin_conninfo, db_name=name, app_password=APP_PASSWORD)
    info = with_overrides(admin_conninfo, dbname=name, user=APP_ROLE, password=APP_PASSWORD)
    monkeypatch.setenv("DATABASE_URL", info)
    with psycopg.connect(info, autocommit=True) as conn:
        yield conn


def _bist(*argv: str) -> int:
    return cli.main(["--config", CFG, *argv])


def _register(conn, version: str, role: str = "challenger") -> None:
    reg.register_model(conn, version, {"kind": "logreg_v1"}, END, role=role,
                       trained_at=dt.datetime(2026, 11, 1, tzinfo=UTC))


def _dataset(path: Path) -> Path:
    """Kucuk, sizintisiz (son h gunun etiketi yok) sentetik veri seti; egitim icin yeterli satir (>= 500)."""
    rng = np.random.default_rng(0)
    days = pd.bdate_range("2024-01-01", periods=260)
    h = 5
    rows = []
    for i, d in enumerate(days):
        mkt = rng.normal()
        for t in TICKERS:
            z1 = rng.normal()
            y = float(rng.random() < 1 / (1 + np.exp(-(1.5 * z1 + 0.3 * mkt))))
            end = days[i + h] if i + h < len(days) else pd.NaT
            rows.append({
                "date": d, "ticker": t, "ret_1_z": z1, "ret_5_z": rng.normal(), "mkt_ret_1": mkt,
                "end_date": end, "label_up": y if pd.notna(end) else np.nan,
                "label_rel_up": float(rng.random() < 0.5) if pd.notna(end) else np.nan,
                "fwd_ret": 0.0, "fwd_rel_ret": 0.0, "tradable_ret": 0.0,
            })
    frame = pd.DataFrame(rows)
    ds = Dataset(frame=frame, horizon_days=5, label_threshold=0.0, feature_names=FEATS, meta={
        "feature_version": FEATURE_VERSION, "date_min": str(days[0].date()), "date_max": str(days[-1].date()),
        "n_rows": len(frame), "built_at": "2026-11-10T00:00:00+00:00",
    })
    save_dataset(ds, path)
    return path


def test_models_help_lists_subcommands(capsys):
    with pytest.raises(SystemExit) as exc:
        _bist("models", "--help")
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for sub in ("list", "train", "promote-check", "promote", "retire"):
        assert sub in out


def test_no_database_url_exits_2(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for argv in (
        ["models", "list"],
        ["models", "train", "--name", "x"],
        ["models", "promote-check", "--challenger", "x"],
        ["models", "promote", "--challenger", "x", "--confirm"],
        ["models", "retire", "--version", "x", "--reason", "r"],
    ):
        assert _bist(*argv) == 2, argv


def test_retire_requires_nonempty_reason(db):
    _register(db, "m_r1")
    assert _bist("models", "retire", "--version", "m_r1") == 2
    assert _bist("models", "retire", "--version", "m_r1", "--reason", "   ") == 2
    assert reg.get_model(db, "m_r1").role == "challenger"


def test_retire_with_reason_records_reason_in_runs(db):
    _register(db, "m_r2")
    assert _bist("models", "retire", "--version", "m_r2", "--reason", "test emeklilik") == 0
    assert reg.get_model(db, "m_r2").role == "retired"
    detail = db.execute("SELECT detail FROM runs WHERE job = 'models-retire'").fetchone()[0]
    assert detail["reason"] == "test emeklilik" and detail["version"] == "m_r2"


def test_promote_without_confirm_does_nothing(db):
    _register(db, "m_p1")
    assert _bist("models", "promote", "--challenger", "m_p1") == 2
    assert reg.get_model(db, "m_p1").role == "challenger"
    assert reg.list_models(db, ["champion"]) == []
    assert db.execute("SELECT count(*) FROM runs WHERE job = 'models-promote'").fetchone()[0] == 0


def test_promote_with_confirm_is_refused_when_check_fails(db):
    _register(db, "m_p2")
    assert _bist("models", "promote", "--challenger", "m_p2", "--confirm") == 2
    assert reg.get_model(db, "m_p2").role == "challenger"
    assert reg.list_models(db, ["champion"]) == []


def test_promote_check_without_holdout_file_keeps_holdout_check_failed(db, capsys, tmp_path):
    _register(db, "m_c1")
    rc = _bist("models", "promote-check", "--challenger", "m_c1",
               "--holdout-result", str(tmp_path / "yok.json"))
    captured = capsys.readouterr()
    assert rc == 1  # RED
    assert "[KALDI] sinama_kumesi" in captured.out
    assert "holdout dosyasi yok" in captured.err
    assert "deneme sayisi (aile faz35, Bonferroni): 1" in captured.out
    assert reg.get_model(db, "m_c1").role == "challenger"  # promote-check yazmaz


def test_list_filters_by_role(db, capsys):
    _register(db, "m_l1")
    _register(db, "m_l2", role="retired")
    assert _bist("models", "list", "--roles", "challenger") == 0
    out = capsys.readouterr().out
    assert "m_l1" in out and "m_l2" not in out
    assert _bist("models", "list", "--roles", "champion,bilinmeyen") == 2


def test_train_dry_run_writes_nothing_and_real_run_registers_challenger(db, tmp_path):
    ds_path = _dataset(tmp_path / "ds.pkl")
    args = ["models", "train", "--dataset", str(ds_path), "--name", "t1",
            "--features", ",".join(FEATS), "--as-of", "2024-10-01"]
    assert _bist(*args, "--dry-run") == 0
    assert reg.list_models(db) == []
    assert db.execute("SELECT count(*) FROM experiments WHERE family = 'model-train'").fetchone()[0] == 0

    assert _bist(*args) == 0
    recs = reg.list_models(db)
    assert [r.version for r in recs] == ["logreg_t1@2024-10-01"]
    assert recs[0].role == "challenger"
    assert _bist(*args) == 2  # ayni version: sessizce ezilmez
