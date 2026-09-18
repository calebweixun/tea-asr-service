from __future__ import annotations

import json
import logging
from pathlib import Path

from tea_asr.config import AppPaths
from tea_asr.logs import JsonFormatter, event, setup_logging


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
