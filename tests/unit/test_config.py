from __future__ import annotations

from pathlib import Path

import pytest

from tea_asr.config import AppPaths, ServiceConfig, load_or_create_token


def app_paths(tmp_path: Path) -> AppPaths:
    return AppPaths(support=tmp_path / "support", logs=tmp_path / "logs")


def test_defaults_match_what_has_been_accepted() -> None:
    config = ServiceConfig()
    # Streaming preview passed the docs/07 acceptance measurements, so it ships on.
    assert config.revisable_preview is True
    assert config.protocol_version == "1.1"
    assert config.unload_after_s == 15 * 60


def test_preview_can_be_turned_off_explicitly() -> None:
    config = ServiceConfig.load(env={"TEA_ASR_REVISABLE_PREVIEW": "0"})
    assert config.revisable_preview is False
    assert config.protocol_version == "1.0"


def test_config_file_overrides_defaults(tmp_path: Path) -> None:
    paths = app_paths(tmp_path)
    paths.support.mkdir(parents=True)
    paths.config_file.write_text('[service]\nport = 9000\nidle_unload_s = 60\n')
    config = ServiceConfig.load(paths, env={})
    assert config.port == 9000
    assert config.unload_after_s == 60


def test_keep_warm_disables_unloading(tmp_path: Path) -> None:
    paths = app_paths(tmp_path)
    paths.support.mkdir(parents=True)
    paths.config_file.write_text("[service]\nkeep_warm = true\n")
    assert ServiceConfig.load(paths, env={}).unload_after_s == 0


def test_environment_wins_over_the_file(tmp_path: Path) -> None:
    paths = app_paths(tmp_path)
    paths.support.mkdir(parents=True)
    paths.config_file.write_text("[service]\nrevisable_preview = true\n")
    config = ServiceConfig.load(paths, env={"TEA_ASR_REVISABLE_PREVIEW": "0"})
    assert config.revisable_preview is False
    assert config.protocol_version == "1.0"


def test_unknown_config_key_is_reported_not_ignored(tmp_path: Path) -> None:
    paths = app_paths(tmp_path)
    paths.support.mkdir(parents=True)
    paths.config_file.write_text("[service]\nspeling_mistake = 1\n")
    with pytest.raises(RuntimeError, match="不認得的欄位"):
        ServiceConfig.load(paths, env={})


def test_token_file_is_private_and_stable(tmp_path: Path) -> None:
    paths = app_paths(tmp_path)
    first = load_or_create_token(paths)
    assert len(first) >= 32
    assert oct(paths.token_file.stat().st_mode)[-3:] == "600"
    assert load_or_create_token(paths) == first
