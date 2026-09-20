from __future__ import annotations

from pathlib import Path

import pytest

from tea_asr.config import (
    AppPaths,
    ServiceConfig,
    TokenAuthenticator,
    load_or_create_token,
    revoke_token,
    rotate_token,
    validate_bind_or_raise,
)


def app_paths(tmp_path: Path) -> AppPaths:
    return AppPaths(support=tmp_path / "support", logs=tmp_path / "logs")


def test_defaults_match_what_has_been_accepted() -> None:
    config = ServiceConfig()
    # Streaming preview passed the docs/07 acceptance measurements, so it ships on.
    assert config.revisable_preview is True
    assert config.protocol_version == "1.1"
    assert config.unload_after_s == 15 * 60
    # docs/benchmarks/pua-bf16-ab-report.md: the deployed MLX 4bit checkpoint
    # leaks PUA characters into 70% of sentences, so filtering ships on.
    assert config.filter_pua is True
    # docs/benchmarks/concurrency-report.md: measured safe up to 4 concurrent
    # continuous sessions; ships at 2 for latency-budget reasons on a
    # single-user desktop service, not because more was found unsafe.
    assert config.max_continuous_sessions == 2


def test_max_continuous_sessions_is_configurable(tmp_path: Path) -> None:
    paths = app_paths(tmp_path)
    paths.support.mkdir(parents=True)
    paths.config_file.write_text("[service]\nmax_continuous_sessions = 4\n")
    config = ServiceConfig.load(paths, env={})
    assert config.max_continuous_sessions == 4


def test_pua_filter_can_be_turned_off_explicitly() -> None:
    config = ServiceConfig.load(env={"TEA_ASR_FILTER_PUA": "0"})
    assert config.filter_pua is False


def test_pua_filter_environment_wins_over_the_file(tmp_path: Path) -> None:
    paths = app_paths(tmp_path)
    paths.support.mkdir(parents=True)
    paths.config_file.write_text("[service]\nfilter_pua = true\n")
    config = ServiceConfig.load(paths, env={"TEA_ASR_FILTER_PUA": "0"})
    assert config.filter_pua is False


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


# --- W9: bind opt-in ---------------------------------------------------------


def test_loopback_bind_never_needs_opt_in() -> None:
    for host in ("127.0.0.1", "localhost", "::1"):
        validate_bind_or_raise(host, allow_lan=False)  # must not raise


def test_non_loopback_bind_is_rejected_without_explicit_opt_in() -> None:
    with pytest.raises(RuntimeError, match="allow_lan"):
        validate_bind_or_raise("0.0.0.0", allow_lan=False)
    with pytest.raises(RuntimeError, match="allow_lan"):
        validate_bind_or_raise("192.168.1.5", allow_lan=False)


def test_non_loopback_bind_is_permitted_once_opted_in() -> None:
    validate_bind_or_raise("0.0.0.0", allow_lan=True)  # must not raise
    validate_bind_or_raise("192.168.1.5", allow_lan=True)  # must not raise


def test_service_config_defaults_to_lan_closed() -> None:
    config = ServiceConfig()
    assert config.allow_lan is False
    assert config.extra_allowed_hosts == ()


def test_allow_lan_is_configurable_by_env() -> None:
    config = ServiceConfig.load(env={"TEA_ASR_ALLOW_LAN": "1"})
    assert config.allow_lan is True


def test_allow_lan_env_accepts_falsey_strings() -> None:
    config = ServiceConfig.load(env={"TEA_ASR_ALLOW_LAN": "0"})
    assert config.allow_lan is False


def test_extra_allowed_hosts_from_env_are_split_and_trimmed() -> None:
    config = ServiceConfig.load(
        env={"TEA_ASR_EXTRA_ALLOWED_HOSTS": "mymac.tailnet.ts.net, other-host "}
    )
    assert config.extra_allowed_hosts == ("mymac.tailnet.ts.net", "other-host")


def test_extra_allowed_hosts_from_file_become_a_tuple(tmp_path: Path) -> None:
    paths = app_paths(tmp_path)
    paths.support.mkdir(parents=True)
    paths.config_file.write_text(
        '[service]\nallow_lan = true\nextra_allowed_hosts = ["mymac.tailnet.ts.net"]\n'
    )
    config = ServiceConfig.load(paths, env={})
    assert config.allow_lan is True
    assert config.extra_allowed_hosts == ("mymac.tailnet.ts.net",)


# --- W9: token rotation and revocation ---------------------------------------


def test_rotate_token_issues_a_different_token_and_invalidates_the_old_one(
    tmp_path: Path,
) -> None:
    paths = app_paths(tmp_path)
    first = load_or_create_token(paths)
    second = rotate_token(paths)
    assert second != first
    assert len(second) >= 32
    assert oct(paths.token_file.stat().st_mode)[-3:] == "600"
    authenticator = TokenAuthenticator(paths)
    assert authenticator.matches(second) is True
    assert authenticator.matches(first) is False


def test_revoke_token_rejects_every_presented_token(tmp_path: Path) -> None:
    paths = app_paths(tmp_path)
    token = load_or_create_token(paths)
    revoke_token(paths)
    assert oct(paths.token_file.stat().st_mode)[-3:] == "600"
    authenticator = TokenAuthenticator(paths)
    assert authenticator.matches(token) is False
    assert authenticator.matches("") is False


def test_rotate_after_revoke_restores_access(tmp_path: Path) -> None:
    paths = app_paths(tmp_path)
    load_or_create_token(paths)
    revoke_token(paths)
    fresh = rotate_token(paths)
    authenticator = TokenAuthenticator(paths)
    assert authenticator.matches(fresh) is True


def test_token_authenticator_picks_up_rotation_on_an_already_running_process(
    tmp_path: Path,
) -> None:
    """A live server must not need a restart for `tea-asr token rotate` to take effect."""

    paths = app_paths(tmp_path)
    first = load_or_create_token(paths)
    authenticator = TokenAuthenticator(paths)
    assert authenticator.matches(first) is True
    second = rotate_token(paths)
    assert authenticator.matches(first) is False
    assert authenticator.matches(second) is True


def test_token_authenticator_creates_a_token_when_none_exists(tmp_path: Path) -> None:
    paths = app_paths(tmp_path)
    authenticator = TokenAuthenticator(paths)
    assert paths.token_file.exists()
    token = paths.token_file.read_text().strip()
    assert authenticator.matches(token) is True
