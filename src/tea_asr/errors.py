from __future__ import annotations

from typing import Any

#: Wire error codes from docs/04-api.md. The table is the single source of truth
#: for HTTP status and WebSocket close code, so no call site invents a mapping.
ERROR_HTTP_STATUS: dict[str, int] = {
    "unauthenticated": 401,
    "forbidden_origin": 403,
    "invalid_audio": 422,
    "unsupported_option": 422,
    "payload_too_large": 413,
    "queue_full": 429,
    "session_limit": 429,
    "concurrent_session_limit": 429,
    "model_loading": 503,
    "model_unavailable": 503,
    "model_incompatible": 503,
    "inference_failed": 500,
    "inference_timeout": 504,
    "protocol_error": 400,
    "conflict": 409,
    "timeline_gap": 409,
    "internal_error": 500,
}

#: Close codes for session-level failures. Anything not listed keeps the
#: connection open and is reported as a segment-level event instead.
ERROR_WS_CLOSE: dict[str, int] = {
    "unauthenticated": 1008,
    "forbidden_origin": 1008,
    "protocol_error": 1008,
    "invalid_audio": 1008,
    "unsupported_option": 1008,
    "payload_too_large": 1009,
    "queue_full": 1013,
    "session_limit": 1013,
    "slow_client": 1013,
    # 1012 "service restart": the session cannot continue on the old clock.
    "timeline_gap": 1012,
    # `queue_full` / `session_limit` / `slow_client` all share 1013, so a
    # client cannot tell "my own segment queue is full" apart from "the
    # server is shedding load" from the close code alone (see docs/04). This
    # is a *different* condition again — the connection was refused before a
    # session even started, admission-side, not mid-session backpressure —
    # so it gets its own code in the private-use range (3000-3999 is IANA
    # registered; 4000-4999 is reserved for private/application use by
    # RFC 6455 §7.4.2) instead of joining that pile-up.
    "concurrent_session_limit": 4029,
}

RETRYABLE_CODES = frozenset(
    {
        "queue_full",
        "session_limit",
        "concurrent_session_limit",
        "model_loading",
        "inference_failed",
        "inference_timeout",
        "timeline_gap",
    }
)


class ApiError(Exception):
    """A failure that maps onto a wire error code.

    Carrying the code rather than an HTTP status keeps HTTP and WebSocket
    responses consistent, and keeps tracebacks away from the client.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = code in RETRYABLE_CODES if retryable is None else retryable
        self.request_id = request_id

    @property
    def http_status(self) -> int:
        return ERROR_HTTP_STATUS.get(self.code, 500)

    @property
    def ws_close_code(self) -> int | None:
        return ERROR_WS_CLOSE.get(self.code)

    def envelope(self, request_id: str | None = None) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "request_id": self.request_id or request_id,
            }
        }
