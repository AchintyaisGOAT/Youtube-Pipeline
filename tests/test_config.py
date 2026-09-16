from __future__ import annotations

import pytest

from app.config import ChannelConfig, load_channel_config


def test_defaults_validate():
    cfg = ChannelConfig()
    assert cfg.timezone == "America/New_York"
    assert cfg.video.encoder == "auto"


def test_rejects_unknown_keys():
    with pytest.raises(Exception):
        ChannelConfig.model_validate({"not_a_real_field": True})


def test_rejects_bad_timezone():
    with pytest.raises(Exception):
        ChannelConfig.model_validate({"timezone": "Not/AZone"})


def test_rejects_bad_encoder():
    with pytest.raises(Exception):
        ChannelConfig.model_validate({"video": {"encoder": "vp9"}})


def test_load_channel_config_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_channel_config(tmp_path / "nope.yaml")


def test_load_channel_config_roundtrip(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("config_version: 1\ntimezone: America/New_York\n", encoding="utf-8")
    cfg = load_channel_config(p)
    assert cfg.config_version == 1
