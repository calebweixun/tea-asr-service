from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Any

from fastapi import WebSocket

from tea_asr.errors import ApiError
from tea_asr.wire import ServerModel

#: Events that carry session state a client cannot reconstruct. Never dropped,
#: never merged (docs/04-api.md, "慢 client 與故障").
TERMINAL_TYPES = frozenset(
    {
        "session.started",
        "audio.committed",
        "segment.queued",
        "transcript.final",
        "segment.skipped",
        "segment.error",
        "session.stopped",
        "session.cancelled",
        "error",
        "pong",
        # Translation is append-only: a dropped piece would silently lose text.
        "translation.started",
        "translation.segment",
        "translation.error",
    }
)

#: Events whose meaning is "the latest value", so an unsent one may be replaced.
COALESCED_TYPES = frozenset({"audio.ack", "flow.control", "preview.status"})


class SlowClientError(ApiError):
    def __init__(self) -> None:
        super().__init__("slow_client", "事件佇列持續滿載，連線已中止。", retryable=False)


class EventWriter:
    """Bounded outgoing event queue with a single writer task.

    `event_id` is assigned at write time so it stays monotonic even when a
    pending event is merged away, which docs/04-api.md permits for flow, ACK and
    partial events but forbids for finals.
    """

    def __init__(
        self,
        websocket: WebSocket,
        session_id: str,
        *,
        max_items: int = 256,
        max_bytes: int = 1024 * 1024,
        overflow_grace_s: float = 5.0,
    ) -> None:
        self._websocket = websocket
        self._session_id = session_id
        self._max_items = max_items
        self._max_bytes = max_bytes
        self._overflow_grace_s = overflow_grace_s
        self._queue: deque[dict[str, Any]] = deque()
        self._bytes = 0
        self._wakeup = asyncio.Event()
        self._over_since: float | None = None
        self._failure: BaseException | None = None
        self._next_event_id = 0
        self._drained = asyncio.Event()
        self._drained.set()

    # -- producer side -------------------------------------------------------

    def emit(self, event: ServerModel) -> None:
        payload = event.model_dump(mode="json", exclude_none=False)
        payload.pop("session_id", None)
        payload.pop("event_id", None)
        event_type = str(payload["type"])
        if event_type in COALESCED_TYPES:
            self._replace(event_type, payload)
        elif event_type == "transcript.partial":
            self._replace_partial(payload)
        else:
            self._append(payload)
        if event_type in {"transcript.final", "segment.skipped", "segment.error"}:
            self._drop_partials(str(payload["segment_id"]))
        self._wakeup.set()

    def _size(self, payload: dict[str, Any]) -> int:
        return len(json.dumps(payload, ensure_ascii=False).encode())

    def _append(self, payload: dict[str, Any]) -> None:
        self._queue.append(payload)
        self._bytes += self._size(payload)
        self._drained.clear()
        self._check_overflow()

    def _replace(self, event_type: str, payload: dict[str, Any]) -> None:
        for index, pending in enumerate(self._queue):
            if pending["type"] == event_type:
                self._bytes += self._size(payload) - self._size(pending)
                self._queue[index] = payload
                return
        self._append(payload)

    def _replace_partial(self, payload: dict[str, Any]) -> None:
        for index, pending in enumerate(self._queue):
            if (
                pending["type"] == "transcript.partial"
                and pending["segment_id"] == payload["segment_id"]
            ):
                self._bytes += self._size(payload) - self._size(pending)
                self._queue[index] = payload
                return
        self._append(payload)

    def _drop_partials(self, segment_id: str) -> None:
        kept = deque(
            item
            for item in self._queue
            if not (item["type"] == "transcript.partial" and item["segment_id"] == segment_id)
        )
        if len(kept) != len(self._queue):
            self._queue = kept
            self._bytes = sum(self._size(item) for item in self._queue)

    def _check_overflow(self) -> None:
        over = len(self._queue) > self._max_items or self._bytes > self._max_bytes
        if over and self._over_since is None:
            self._over_since = time.monotonic()
        elif not over:
            self._over_since = None

    # -- writer side ---------------------------------------------------------

    async def run(self) -> None:
        try:
            while True:
                if not self._queue:
                    self._drained.set()
                    self._wakeup.clear()
                    await self._wakeup.wait()
                    continue
                payload = self._queue.popleft()
                self._bytes -= self._size(payload)
                self._check_overflow()
                payload = {
                    "type": payload.pop("type"),
                    "session_id": self._session_id,
                    "event_id": self._next_event_id,
                    **payload,
                }
                self._next_event_id += 1
                await self._websocket.send_json(payload)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # surfaced to the read loop
            self._failure = exc
            raise

    async def watchdog(self) -> None:
        """Fail the session when the client cannot keep up for the grace period."""

        while True:
            await asyncio.sleep(0.25)
            if self._over_since and time.monotonic() - self._over_since >= self._overflow_grace_s:
                raise SlowClientError()

    async def drain(self, timeout: float = 5.0) -> None:
        try:
            await asyncio.wait_for(self._drained.wait(), timeout=timeout)
        except TimeoutError:
            pass

    @property
    def failure(self) -> BaseException | None:
        return self._failure
