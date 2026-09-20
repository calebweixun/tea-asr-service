from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from tea_asr.config import AppPaths, ServiceConfig, validate_log_retention_or_raise
from tea_asr.logs import JsonFormatter, event, read_recent_events, setup_logging, split_log_payload


def record(**fields: object) -> logging.LogRecord:
    item = logging.LogRecord("tea_asr.test", logging.INFO, __file__, 1, "hello", None, None)
    item.fields = fields  # type: ignore[attr-defined]
    return item


def test_secrets_and_transcripts_never_reach_the_log() -> None:
    line = JsonFormatter().format(
        record(token="secret", text="逐字稿", pcm="audio", request_id="r1")
    )
    payload = json.loads(line)
    assert payload["request_id"] == "r1"
    for forbidden in ("token", "text", "pcm"):
        assert forbidden not in payload


def test_log_file_is_created_under_the_logs_directory(tmp_path: Path) -> None:
    paths = AppPaths(support=tmp_path / "support", logs=tmp_path / "logs")
    path = setup_logging(paths)
    event(logging.getLogger("tea_asr.test"), "service.started", model_state="ready")
    logging.getLogger("tea_asr").handlers[0].flush()
    assert path.exists()
    payload = json.loads(path.read_text().splitlines()[0])
    assert payload["message"] == "service.started"
    assert payload["model_state"] == "ready"


def _flush_all(paths: AppPaths) -> None:
    for handler in logging.getLogger("tea_asr").handlers:
        handler.flush()


def test_event_defaults_to_info_for_every_pre_existing_call_site(tmp_path: Path) -> None:
    """`event()` gained a `level=` keyword-only parameter; every call written
    before it existed must keep behaving exactly as before."""

    paths = AppPaths(support=tmp_path / "support", logs=tmp_path / "logs")
    setup_logging(paths)
    event(logging.getLogger("tea_asr.test"), "service.started", model_state="ready")
    _flush_all(paths)
    payload = json.loads(paths.log_file.read_text().splitlines()[0])
    assert payload["level"] == "INFO"


def test_event_writes_each_level(tmp_path: Path) -> None:
    paths = AppPaths(support=tmp_path / "support", logs=tmp_path / "logs")
    setup_logging(paths, config=ServiceConfig(log_level="debug"))
    logger = logging.getLogger("tea_asr.test")
    for level in ("debug", "info", "warning", "error"):
        event(logger, f"event.{level}", level=level)
    _flush_all(paths)
    lines = [json.loads(line) for line in paths.log_file.read_text().splitlines()]
    levels_seen = {entry["level"] for entry in lines}
    assert levels_seen == {"DEBUG", "INFO", "WARNING", "ERROR"}


def test_default_log_level_excludes_debug(tmp_path: Path) -> None:
    paths = AppPaths(support=tmp_path / "support", logs=tmp_path / "logs")
    setup_logging(paths)  # default config: log_level="info"
    logger = logging.getLogger("tea_asr.test")
    event(logger, "hidden.debug", level="debug")
    event(logger, "visible.info", level="info")
    _flush_all(paths)
    messages = [json.loads(line)["message"] for line in paths.log_file.read_text().splitlines()]
    assert "hidden.debug" not in messages
    assert "visible.info" in messages


def test_warning_and_error_are_duplicated_into_the_error_log(tmp_path: Path) -> None:
    paths = AppPaths(support=tmp_path / "support", logs=tmp_path / "logs")
    setup_logging(paths)
    logger = logging.getLogger("tea_asr.test")
    event(logger, "routine.info", level="info")
    event(logger, "worker.warn", level="warning")
    event(logger, "worker.error", level="error")
    _flush_all(paths)
    error_messages = [
        json.loads(line)["message"] for line in paths.error_log_file.read_text().splitlines()
    ]
    assert "routine.info" not in error_messages
    assert "worker.warn" in error_messages
    assert "worker.error" in error_messages


def test_read_recent_events_orders_newest_first_and_respects_limit(tmp_path: Path) -> None:
    paths = AppPaths(support=tmp_path / "support", logs=tmp_path / "logs")
    setup_logging(paths)
    logger = logging.getLogger("tea_asr.test")
    for index in range(5):
        event(logger, f"event.{index}", level="info")
    _flush_all(paths)
    events = read_recent_events(paths.log_file, limit=3)
    assert [entry["message"] for entry in events] == ["event.4", "event.3", "event.2"]


def test_read_recent_events_level_filter_is_minimum_severity(tmp_path: Path) -> None:
    paths = AppPaths(support=tmp_path / "support", logs=tmp_path / "logs")
    setup_logging(paths, config=ServiceConfig(log_level="debug"))
    logger = logging.getLogger("tea_asr.test")
    event(logger, "d", level="debug")
    event(logger, "i", level="info")
    event(logger, "w", level="warning")
    event(logger, "e", level="error")
    _flush_all(paths)
    events = read_recent_events(paths.log_file, level="warning", limit=10)
    assert {entry["message"] for entry in events} == {"w", "e"}


def test_read_recent_events_rejects_unknown_level(tmp_path: Path) -> None:
    paths = AppPaths(support=tmp_path / "support", logs=tmp_path / "logs")
    setup_logging(paths)
    with pytest.raises(ValueError):
        read_recent_events(paths.log_file, level="critical", limit=10)


def test_split_log_payload_scrubs_forbidden_keys_even_if_present() -> None:
    """Defense in depth: even if a forbidden key somehow made it into a
    decoded payload (e.g. a line written before FORBIDDEN_KEYS existed),
    the read-side helper must still drop it."""

    payload = {
        "ts": "2026-01-01T00:00:00",
        "level": "ERROR",
        "logger": "tea_asr.test",
        "message": "m",
        "token": "leaked",
        "text": "逐字稿",
        "request_id": "keep-me",
    }
    fields = split_log_payload(payload)
    assert fields == {"request_id": "keep-me"}


def test_validate_log_retention_rejects_unbounded_config() -> None:
    with pytest.raises(RuntimeError):
        validate_log_retention_or_raise(ServiceConfig(log_max_bytes=10**12))
    with pytest.raises(RuntimeError):
        validate_log_retention_or_raise(ServiceConfig(log_backup_count=10_000))
    with pytest.raises(RuntimeError):
        validate_log_retention_or_raise(ServiceConfig(log_error_backup_count=10_000))
    with pytest.raises(RuntimeError):
        validate_log_retention_or_raise(ServiceConfig(log_level="verbose"))


def test_validate_log_retention_accepts_defaults() -> None:
    validate_log_retention_or_raise(ServiceConfig())
