"""Yapilandirma okuma ve dogrulama (PROJE.md Bolum 8)."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

DEFAULT_PATH = Path("config/default.yaml")

_REQUIRED_TOP = (
    "universe", "timezone", "schedule", "horizon_days", "label_threshold", "prob_clip",
    "costs", "training", "holdout", "terfi", "drift", "circuit_breaker",
)


class ConfigError(ValueError):
    """Gecersiz yapilandirma."""


def config_path(path: str | os.PathLike[str] | None = None) -> Path:
    if path is not None:
        return Path(path)
    return Path(os.environ.get("BIST_CONFIG", DEFAULT_PATH))


def load_config(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    p = config_path(path)
    try:
        with p.open(encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"yapilandirma dosyasi bulunamadi: {p}") from exc
    if not isinstance(cfg, dict):
        raise ConfigError(f"{p}: kok eleman bir sozluk olmali")
    validate(cfg)
    return cfg


def validate(cfg: dict[str, Any]) -> None:
    missing = [k for k in _REQUIRED_TOP if k not in cfg]
    if missing:
        raise ConfigError(f"eksik anahtarlar: {', '.join(missing)}")

    try:
        ZoneInfo(cfg["timezone"])
    except Exception as exc:  # bilinmeyen zaman dilimi
        raise ConfigError(f"gecersiz timezone: {cfg['timezone']!r}") from exc

    horizon = cfg["horizon_days"]
    if not isinstance(horizon, int) or isinstance(horizon, bool) or horizon < 1:
        raise ConfigError("horizon_days pozitif tam sayi olmali")

    # Sizinti kurali (PROJE.md Bolum 5): purge, ufuktan kisa olamaz.
    purge = cfg["training"].get("purge_days")
    if not isinstance(purge, int) or purge < horizon:
        raise ConfigError(f"training.purge_days ({purge!r}) horizon_days ({horizon}) degerinden kucuk olamaz")

    clip = cfg["prob_clip"]
    if not (isinstance(clip, list) and len(clip) == 2 and 0 <= clip[0] < clip[1] <= 1):
        raise ConfigError("prob_clip [alt, ust] olmali ve 0 <= alt < ust <= 1")

    try:
        hh, mm = str(cfg["schedule"]["daily_run_time"]).split(":")
        dt.time(int(hh), int(mm))
    except Exception as exc:
        raise ConfigError("schedule.daily_run_time 'SS:DD' bicimli olmali") from exc

    try:
        dt.date.fromisoformat(str(cfg["holdout"]["holdout_start"]))
    except Exception as exc:
        raise ConfigError("holdout.holdout_start YYYY-AA-GG bicimli olmali") from exc

    costs = cfg["costs"]
    for key in ("commission_bps", "spread_bps"):
        v = costs.get(key)
        if v is not None and (not isinstance(v, (int, float)) or v < 0):
            raise ConfigError(f"costs.{key} null veya >= 0 olmali")
    grid = costs.get("sensitivity_grid_bps")
    if not (isinstance(grid, list) and grid and all(isinstance(x, (int, float)) and x >= 0 for x in grid)):
        raise ConfigError("costs.sensitivity_grid_bps bos olmayan, negatif olmayan sayi listesi olmali")

    boot = cfg["terfi"].get("bootstrap", {})
    if boot.get("kind") != "block_by_date":
        raise ConfigError("terfi.bootstrap.kind 'block_by_date' olmali (tarihe gore blok bootstrap)")
    if cfg["terfi"].get("min_resolved_dates", 0) < 1:
        raise ConfigError("terfi.min_resolved_dates >= 1 olmali")
