"""Per-session translation: ASR finals in, append-only `translation.*` events out.

The ASR path never waits on anything here. `submit` is synchronous and never
blocks: a final goes onto a bounded queue or, if the queue is full, gets an
explicit `translation.error` (docs/06 #4). One consumer task talks to the
translation worker; the ASR `transcript.final` it translates is emitted before
`submit` is called and is never modified (docs/06 #5).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from tea_asr.errors import ApiError
from tea_asr.logs import event
from tea_asr.wire import ServerModel, TranslationError, TranslationSegment

logger = logging.getLogger("tea_asr.translation")

#: Finals that may wait for translation in one session before new ones are
#: reported as dropped instead of queued.
MAX_PENDING_SEGMENTS = 16
#: How long `session.stop` waits for outstanding translations.
DRAIN_TIMEOUT_S = 30.0


class TranslationProvider(Protocol):
    state: str
    generation: int
    task_timeout_s: float

    async def start_session(self, direction: str, latency_mode: str) -> int: ...
    async def translate(self, text: str, *, force: bool) -> dict[str, Any]: ...


@dataclass(slots=True)
class _Final:
    segment_id: str
    text: str


class SessionTranslator:
    def __init__(
        self,
        provider: TranslationProvider,
        emit: Callable[[ServerModel], None],
        *,
        session_id: str,
        direction: str,
        latency_mode: str,
        max_pending: int = MAX_PENDING_SEGMENTS,
    ) -> None:
        self._provider = provider
        self._emit = emit
        self._session_id = session_id
        self._direction = direction
        self._latency_mode = latency_mode
        self._queue: asyncio.Queue[_Final] = asyncio.Queue(max_pending)
        self._task: asyncio.Task[None] | None = None
        self._generation: int | None = None
        self._next_index = 0
        #: Segments whose text the model has buffered (WAIT) but not committed.
        self._carried: list[str] = []
        #: Segments in the call currently running on the worker.
        self._inflight: list[str] = []
        self._stopped = False
        self._closed = False

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run())

    # -- producer (ASR side) -------------------------------------------------

    def submit(self, segment_id: str, text: str) -> None:
        if self._closed or self._stopped:
            return
        try:
            self._queue.put_nowait(_Final(segment_id, text))
        except asyncio.QueueFull:
            event(
                logger,
                "translation.dropped",
                level="warning",
                session_id=self._session_id,
                segment_id=segment_id,
            )
            self._error(
                ApiError(
                    "queue_full",
                    f"翻譯落後超過 {self._queue.maxsize} 段，這一段不翻譯；ASR 原稿不受影響。",
                    retryable=False,
                ),
                [segment_id],
            )

    # -- consumer ------------------------------------------------------------

    def _error(self, error: ApiError, segment_ids: list[str], *, stopped: bool = False) -> None:
        if self._closed:
            return
        self._emit(
            TranslationError(
                session_id=self._session_id,
                event_id=0,
                code=error.code,
                message=error.message,
                retryable=error.retryable,
                source_segment_ids=segment_ids,
                stopped=stopped,
            )
        )

    async def _ensure_session(self) -> None:
        if self._generation == self._provider.generation:
            return
        if self._generation is not None and self._carried:
            # The worker restarted and lost the source it had buffered.
            self._error(
                ApiError(
                    "translation_failed",
                    "翻譯 worker 重新啟動，這幾段暫存的原文沒有翻出來。",
                    retryable=False,
                ),
                self._carried,
            )
            self._carried = []
        self._generation = await self._provider.start_session(
            self._direction, self._latency_mode
        )

    async def _run(self) -> None:
        while True:
            first = await self._queue.get()
            batch = [first]
            # Coalesce whatever is already waiting: one call instead of many
            # when translation fell behind, so the backlog cannot grow.
            while not self._queue.empty():
                batch.append(self._queue.get_nowait())
            text = "".join(item.text for item in batch)
            ids = self._carried + [item.segment_id for item in batch]
            self._inflight = ids
            try:
                await self._ensure_session()
                ids = self._carried + [item.segment_id for item in batch]
                self._inflight = ids
                response = await self._provider.translate(text, force=True)
            except ApiError as exc:
                fatal = exc.code == "translation_unavailable" and not exc.retryable
                if self._carried:
                    # The worker still buffers that source; start clean so it
                    # cannot resurface under a later segment's IDs.
                    self._generation = None
                self._carried = []
                self._error(exc, ids, stopped=fatal)
                if fatal:
                    self._stopped = True
                    self._drain_queue()
                    return
                continue
            finally:
                self._inflight = []
                for _ in batch:
                    self._queue.task_done()
            if response.get("action") != "TRANS":
                self._carried = ids
                continue
            self._carried = []
            if self._closed:
                return
            self._emit(
                TranslationSegment(
                    session_id=self._session_id,
                    event_id=0,
                    translation_index=self._next_index,
                    source_segment_ids=ids,
                    source_text=str(response.get("source", "")),
                    text=str(response.get("text", "")),
                    direction=self._direction,
                    latency_mode=self._latency_mode,
                    forced=bool(response.get("forced", True)),
                    inference_ms=int(response.get("inference_ms", 0)),
                )
            )
            self._next_index += 1

    def _drain_queue(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()

    # -- teardown ------------------------------------------------------------

    async def drain(self, timeout: float = DRAIN_TIMEOUT_S) -> None:
        """Wait for queued finals at `session.stop`; report what did not finish."""

        if self._task is None or self._task.done():
            return
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout)
        except TimeoutError:
            unfinished = (self._inflight or self._carried) + [
                item.segment_id for item in self._pending_items()
            ]
            await self._cancel_task()
            self._error(
                ApiError(
                    "translation_timeout",
                    f"session 結束前 {timeout:g} 秒內沒有翻完。",
                    retryable=False,
                ),
                unfinished,
                stopped=True,
            )
            self._closed = True
            return
        if self._carried:
            # A forced call still came back empty (the model insisted on WAIT).
            self._error(
                ApiError("translation_failed", "模型沒有產出這幾段的譯文。", retryable=False),
                self._carried,
            )
            self._carried = []

    def _pending_items(self) -> list[_Final]:
        items: list[_Final] = []
        while not self._queue.empty():
            items.append(self._queue.get_nowait())
            self._queue.task_done()
        return items

    async def _cancel_task(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def close(self) -> None:
        """Stop translating; nothing is emitted after this returns."""

        self._closed = True
        await self._cancel_task()
