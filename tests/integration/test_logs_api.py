from __future__ import annotations

import logging
from pathlib import Path

from tea_asr.config import AppPaths, ServiceConfig
from tea_asr.logs import event, setup_logging
from tests.conftest import AUTH, FakeSupervisor, build_client


def _seed(tmp_path: Path, *, backup_count: int = 3, max_bytes: int = 5 * 1024 * 1024) -> AppPaths:
    paths = AppPaths(support=tmp_path / "support", logs=tmp_path / "logs")
    setup_logging(
        paths, config=ServiceConfig(log_backup_count=backup_count, log_max_bytes=max_bytes)
    )
    return paths


def test_logs_requires_auth(tmp_path: Path) -> None:
    paths = _seed(tmp_path)
    with build_client(FakeSupervisor(), paths=paths) as http:
        response = http.get("/v1/logs")
        assert response.status_code == 401


def test_logs_returns_events_across_levels(tmp_path: Path) -> None:
    paths = _seed(tmp_path)
    logger = logging.getLogger("tea_asr.test.logs_api")

    # Open the client (and let its own lifespan "service.started" write land)
    # before emitting the events under test, so ours are unambiguously the
    # newest and the app's own startup noise cannot be mistaken for them.
    with build_client(FakeSupervisor(), paths=paths) as http:
        event(logger, "test.routine_info", level="info", model_state="ready")
        event(logger, "test.worker_slow", level="warning", queue_ms=500)
        event(logger, "test.worker_restart_failed", level="error", code="inference_failed")
        for handler in logging.getLogger("tea_asr").handlers:
            handler.flush()

        body = http.get("/v1/logs", headers=AUTH).json()
        messages = [item["message"] for item in body["items"]]
        assert "test.routine_info" in messages
        assert "test.worker_slow" in messages
        assert "test.worker_restart_failed" in messages
        # Newest first.
        assert messages[0] == "test.worker_restart_failed"


def test_logs_level_filter_is_a_minimum_severity(tmp_path: Path) -> None:
    paths = _seed(tmp_path)
    logger = logging.getLogger("tea_asr.test.logs_api")

    with build_client(FakeSupervisor(), paths=paths) as http:
        event(logger, "test.routine_info", level="info")
        event(logger, "test.worker_slow", level="warning")
        event(logger, "test.worker_restart_failed", level="error")
        for handler in logging.getLogger("tea_asr").handlers:
            handler.flush()

        body = http.get("/v1/logs", headers=AUTH, params={"level": "warning"}).json()
        levels = {item["level"] for item in body["items"]}
        assert levels <= {"WARNING", "ERROR"}
        assert "INFO" not in levels
        messages = {item["message"] for item in body["items"]}
        assert messages == {"test.worker_slow", "test.worker_restart_failed"}


def test_logs_limit_is_hard_capped(tmp_path: Path) -> None:
    paths = _seed(tmp_path)
    with build_client(FakeSupervisor(), paths=paths) as http:
        response = http.get("/v1/logs", headers=AUTH, params={"limit": 501})
        assert response.status_code == 422

        response = http.get("/v1/logs", headers=AUTH, params={"limit": 500})
        assert response.status_code == 200


def test_logs_has_more_flag_reflects_additional_matches(tmp_path: Path) -> None:
    paths = _seed(tmp_path)
    logger = logging.getLogger("tea_asr.test.logs_api")

    with build_client(FakeSupervisor(), paths=paths) as http:
        # The app's own lifespan writes a "service.started" event on
        # startup, so measure the baseline instead of assuming an exact
        # count -- this test is about the has_more/count contract, not about
        # how much unrelated logging the app itself does.
        baseline = http.get("/v1/logs", headers=AUTH, params={"limit": 500}).json()["count"]

        for index in range(5):
            event(logger, f"test.event.{index}", level="info", index=index)
        for handler in logging.getLogger("tea_asr").handlers:
            handler.flush()

        total = baseline + 5
        body = http.get("/v1/logs", headers=AUTH, params={"limit": 2}).json()
        assert body["count"] == 2
        assert body["limit"] == 2
        assert body["has_more"] is True

        body = http.get("/v1/logs", headers=AUTH, params={"limit": total}).json()
        assert body["count"] == total
        assert body["has_more"] is False


def test_logs_never_leak_forbidden_fields(tmp_path: Path) -> None:
    paths = _seed(tmp_path)
    logger = logging.getLogger("tea_asr.test.logs_api")
    # A caller passing exactly the fields docs/03 forbids in the log.
    event(
        logger,
        "transcription.debug_attempt",
        level="error",
        token="super-secret-token",
        authorization="Bearer super-secret-token",
        pcm="raw-audio-bytes",
        text="逐字稿內容",
        raw_text="逐字稿內容",
        transcript="逐字稿內容",
        request_id="safe-to-keep",
    )
    for handler in logging.getLogger("tea_asr").handlers:
        handler.flush()

    with build_client(FakeSupervisor(), paths=paths) as http:
        body = http.get("/v1/logs", headers=AUTH).json()
        raw_body = http.get("/v1/logs", headers=AUTH)
        dump = raw_body.text
        for forbidden in ("super-secret-token", "raw-audio-bytes", "逐字稿內容"):
            assert forbidden not in dump
        entry = next(
            item for item in body["items"] if item["message"] == "transcription.debug_attempt"
        )
        for forbidden_key in ("token", "authorization", "pcm", "text", "raw_text", "transcript"):
            assert forbidden_key not in entry["fields"]
        assert entry["fields"]["request_id"] == "safe-to-keep"


def test_log_retention_evicts_old_backups(tmp_path: Path) -> None:
    """Small size cap + small backup count => old events are provably gone,
    not just "not returned this time"."""

    paths = _seed(tmp_path, backup_count=1, max_bytes=400)
    logger = logging.getLogger("tea_asr.test.logs_api")
    for index in range(200):
        event(logger, f"filler.{index}", level="info", index=index)
    for handler in logging.getLogger("tea_asr").handlers:
        handler.flush()

    family_files = [paths.log_file] + [
        paths.log_file.with_name(f"{paths.log_file.name}.{n}") for n in (1, 2, 3)
    ]
    existing = [path for path in family_files if path.exists()]
    # backup_count=1 means at most the live file plus one rotated backup.
    assert len(existing) <= 2
    assert not paths.log_file.with_name(f"{paths.log_file.name}.2").exists()

    with build_client(FakeSupervisor(), paths=paths, log_backup_count=1) as http:
        body = http.get("/v1/logs", headers=AUTH, params={"limit": 500}).json()
        messages = {item["message"] for item in body["items"]}
        # The earliest events must have been rotated away, not merely
        # truncated by `limit` (which is 500, far above what fits in 800
        # bytes of retained data).
        assert "filler.0" not in messages
        assert "filler.199" in messages
