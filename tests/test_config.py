from __future__ import annotations

import pytest

from app.config import ChannelConfig, load_config


def test_defaults_validate():
    cfg = ChannelConfig()
    assert cfg.timezone == "America/New_York"
    assert cfg.video.encoder == "auto"


def test_committed_config_yaml_validates():
    cfg = load_config("config.yaml")
    assert cfg.channel.name == "PantherTellsHistory"
    assert cfg.llm.checker != cfg.llm.writer


def test_rejects_unknown_keys():
    with pytest.raises(ValueError):
        ChannelConfig.model_validate({"not_a_real_field": True})


def test_rejects_bad_timezone():
    with pytest.raises(ValueError):
        ChannelConfig.model_validate({"timezone": "Not/AZone"})


def test_rejects_bad_encoder():
    with pytest.raises(ValueError):
        ChannelConfig.model_validate({"video": {"encoder": "h264_nvenc"}})


def test_rejects_checker_same_as_writer():
    with pytest.raises(ValueError):
        ChannelConfig.model_validate({"llm": {"writer": "m", "checker": "m"}})


def test_rejects_bad_schedule_slot():
    with pytest.raises(ValueError):
        ChannelConfig.model_validate({"publish": {"schedule": {"shorts": ["every monday"]}}})


def test_load_config_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")
