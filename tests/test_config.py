import copy

import pytest

from bist_tahmin.config import ConfigError, load_config, validate


@pytest.fixture()
def cfg():
    return load_config("config/default.yaml")


@pytest.mark.fast
def test_default_config_is_valid(cfg):
    assert cfg["horizon_days"] == 5
    assert cfg["training"]["purge_days"] >= cfg["horizon_days"]
    assert cfg["timezone"] == "Europe/Istanbul"
    assert cfg["costs"]["commission_bps"] is None  # dogrulanmadan varsayilan: bos


@pytest.mark.fast
def test_purge_shorter_than_horizon_rejected(cfg):
    bad = copy.deepcopy(cfg)
    bad["training"]["purge_days"] = 3
    with pytest.raises(ConfigError, match="purge_days"):
        validate(bad)


@pytest.mark.parametrize("clip", [[0.5, 0.4], [-0.1, 0.9], [0.1, 1.2], [0.1]])
@pytest.mark.fast
def test_bad_prob_clip_rejected(cfg, clip):
    bad = copy.deepcopy(cfg)
    bad["prob_clip"] = clip
    with pytest.raises(ConfigError, match="prob_clip"):
        validate(bad)


@pytest.mark.fast
def test_unknown_timezone_rejected(cfg):
    bad = copy.deepcopy(cfg)
    bad["timezone"] = "Mars/Olympus"
    with pytest.raises(ConfigError, match="timezone"):
        validate(bad)


@pytest.mark.fast
def test_iid_bootstrap_rejected(cfg):
    bad = copy.deepcopy(cfg)
    bad["terfi"]["bootstrap"]["kind"] = "iid"
    with pytest.raises(ConfigError, match="block_by_date"):
        validate(bad)


@pytest.mark.fast
def test_missing_key_rejected(cfg):
    bad = {k: v for k, v in cfg.items() if k != "drift"}
    with pytest.raises(ConfigError, match="drift"):
        validate(bad)


@pytest.mark.fast
def test_missing_file(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "yok.yaml")
