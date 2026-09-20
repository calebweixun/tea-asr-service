from __future__ import annotations

import json
import logging
import logging.handlers
import time
from pathlib import Path
from typing import Any

from .config import AppPaths, ServiceConfig, validate_log_retention_or_raise

#: Never logged, whatever a caller passes: docs/03 forbids bearer tokens, PCM,
#: prompts and transcripts in the log.
FORBIDDEN_KEYS = frozenset({"token", "authorization", "pcm", "text", "raw_text", "transcript"})

#: Severity entry points `event()` accepts, least to most severe. Same
#: vocabulary as `ServiceConfig.log_level` / `tea_asr.config.LOG_LEVELS`, so a
#: config value and a call site's `level=` argument always mean the same thing.
LEVELS: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

#: Keys `JsonFormatter` always writes itself. Anything else in a decoded
#: payload came from a caller's `**fields` and belongs in "fields" when a
#: log line is replayed back out through `read_recent_events`.
_RESERVED_KEYS = frozenset({"ts", "level", "logger", "message", "error"})

#: Hard ceiling on a single `read_recent_events()` call, regardless of what a
#: caller asks for. docs/06-handoff.md #4: every read of a bounded resource
#: needs its own upper bound, not just "however big the file happens to be".
MAX_LOG_EVENTS = 500


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


def setup_logging(paths: AppPaths | None = None, *, config: ServiceConfig | None = None) -> Path:
    """JSON lines with per-level rotation, so the log cannot grow without bound.

    Two rotating families share the same JSON schema and both go through
    `JsonFormatter` (so `FORBIDDEN_KEYS` is scrubbed identically for either):

    - `paths.log_file` (+ `.1..log_backup_count`): every event at or above
      `config.log_level`. Short retention by default because this is the
      high-volume "what's happening right now" stream a log page tails.
    - `paths.error_log_file` (+ `.1..log_error_backup_count`): warning/error
      only. Longer retention by default, because failures are rare enough
      that the same byte budget buys much more retention *in time*, and
      that's exactly what matters once someone is trying to find out what
      broke after the main log has already rotated the entry away.

    Both files are capped in size and backup count — see
    `validate_log_retention_or_raise`, which this calls first — so there is
    no configuration that means "keep everything forever".
    """

    paths = paths or AppPaths.macos_default()
    config = config or ServiceConfig()
    validate_log_retention_or_raise(config)
    paths.logs.mkdir(parents=True, exist_ok=True)
    threshold = LEVELS.get(config.log_level, logging.INFO)

    main_handler = logging.handlers.RotatingFileHandler(
        paths.log_file,
        maxBytes=config.log_max_bytes,
        backupCount=config.log_backup_count,
        encoding="utf-8",
    )
    main_handler.setLevel(threshold)
    main_handler.setFormatter(JsonFormatter())

    error_handler = logging.handlers.RotatingFileHandler(
        paths.error_log_file,
        maxBytes=config.log_max_bytes,
        backupCount=config.log_error_backup_count,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(JsonFormatter())

    root = logging.getLogger("tea_asr")
    root.setLevel(min(threshold, logging.WARNING))
    root.handlers = [main_handler, error_handler]
    root.propagate = False
    return paths.log_file


def event(logger: logging.Logger, message: str, *, level: str = "info", **fields: object) -> None:
    """Emit a structured event at the given severity.

    `level` defaults to "info", matching every call site written before this
    parameter existed — they keep working unchanged. Pass "debug", "warning"
    or "error" for anything else; an unrecognized string falls back to info
    rather than raising, so a typo degrades gracefully instead of taking the
    request down.
    """

    logger.log(LEVELS.get(level, logging.INFO), message, extra={"fields": fields})


def _rotation_family(base: Path, backup_count: int) -> list[Path]:
    """`base` plus its existing `RotatingFileHandler` backups, newest first.

    `.1` is the most recently rotated backup and `.N` the oldest (that's the
    stdlib's own naming), so this order is already newest-to-oldest at the
    file granularity — no sorting by mtime needed.
    """

    candidates = [base] + [base.with_name(f"{base.name}.{n}") for n in range(1, backup_count + 1)]
    return [path for path in candidates if path.exists()]


def read_recent_events(
    log_file: Path,
    *,
    level: str | None = None,
    limit: int,
    backup_count: int = 3,
) -> list[dict[str, Any]]:
    """Read up to `limit` most-recent events (newest first) from the rotating
    log family rooted at `log_file`, optionally filtered to `level` or more
    severe.

    Bounded on every axis a caller could otherwise blow up on: `limit` is
    always respected — this never returns "everything" — and the file set
    scanned is exactly the fixed, already-bounded rotation family (current
    file + its backups), never an unbounded historical archive. This does
    synchronous file I/O and must be called off the event loop, e.g. via
    `asyncio.to_thread`.
    """

    if level is not None and level not in LEVELS:
        raise ValueError(f"unknown log level: {level!r}")
    if limit <= 0:
        return []
    min_level = LEVELS[level] if level else None
    collected: list[dict[str, Any]] = []
    for path in _rotation_family(log_file, backup_count):
        if len(collected) >= limit:
            break
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if len(collected) >= limit:
                break
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if min_level is not None:
                entry_level = LEVELS.get(str(payload.get("level", "")).lower())
                if entry_level is None or entry_level < min_level:
                    continue
            collected.append(payload)
    return collected


def split_log_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """The caller-supplied part of a decoded log line, `FORBIDDEN_KEYS` scrubbed.

    `JsonFormatter` already drops `FORBIDDEN_KEYS` at write time; this
    filters again on the way out so a bug in the writer (or a line written
    before that filtering existed) can never turn into a leak through the
    read API. Defense in depth, not the primary control.
    """

    return {
        key: value
        for key, value in payload.items()
        if key not in _RESERVED_KEYS and key not in FORBIDDEN_KEYS
    }
