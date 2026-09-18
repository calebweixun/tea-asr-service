from __future__ import annotations

import json
import logging
import logging.handlers
import time
from pathlib import Path

from .config import AppPaths

#: Never logged, whatever a caller passes: docs/03 forbids bearer tokens, PCM,
#: prompts and transcripts in the log.
FORBIDDEN_KEYS = frozenset({"token", "authorization", "pcm", "text", "raw_text", "transcript"})


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(
                {key: value for key, value in extra.items() if key not in FORBIDDEN_KEYS}
            )
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info).splitlines()[-1]
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(paths: AppPaths | None = None, *, level: int = logging.INFO) -> Path:
    """JSON lines with rotation, so the log cannot grow without bound."""

    paths = paths or AppPaths.macos_default()
    paths.logs.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        paths.log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("tea_asr")
    root.setLevel(level)
    root.handlers = [handler]
    root.propagate = False
    return paths.log_file


def event(logger: logging.Logger, message: str, **fields: object) -> None:
    logger.info(message, extra={"fields": fields})
