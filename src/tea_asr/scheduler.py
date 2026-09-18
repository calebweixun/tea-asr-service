from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .errors import ApiError
from .wire import SAMPLE_RATE

#: docs/03-architecture.md: interactive first, then realtime, then preview work.
#: Preview never delays a caller that is already waiting for an immutable final.
PRIORITY = {"interactive": 0, "realtime": 1, "preview": 2}


class InferenceWorker(Protocol):
    state: str

    async def transcribe(self, pcm: bytes, *, language: str = "Chinese") -> dict[str, Any]: ...


@dataclass(slots=True)
class _Waiter:
    priority: int
    ticket: int
    samples: int
    future: asyncio.Future[None] = field(repr=False)


class Scheduler:
    """Bounded admission in front of the single MLX worker.

    There is exactly one model, so inference is serialized. What this adds is a
    hard admission limit (nothing queues without bound), the priority order from
    docs/03, and an honest `queue_ms` measured from admission to the start of
    inference rather than inferred from the total elapsed time.
    """

    def __init__(
        self,
        worker: InferenceWorker,
        *,
        max_waiting_tasks: int = 16,
        max_waiting_samples: int = 60 * SAMPLE_RATE,
    ) -> None:
        self._worker = worker
        self.max_waiting_tasks = max_waiting_tasks
        self.max_waiting_samples = max_waiting_samples
        self._waiters: list[_Waiter] = []
        self._running_samples = 0
        self._running = False
        self._tickets = itertools.count()

    @property
    def waiting_tasks(self) -> int:
        return len(self._waiters) + (1 if self._running else 0)

    @property
    def waiting_samples(self) -> int:
        return sum(waiter.samples for waiter in self._waiters) + self._running_samples

    def _admit(self, samples: int) -> None:
        if self.waiting_tasks >= self.max_waiting_tasks:
            raise ApiError("queue_full", "辨識佇列已滿，請稍後重試。")
        if self.waiting_samples + samples > self.max_waiting_samples:
            raise ApiError("queue_full", "等待中的音訊已達上限，請稍後重試。")

    def _wake_next(self) -> None:
        if self._running or not self._waiters:
            return
        self._waiters.sort(key=lambda waiter: (waiter.priority, waiter.ticket))
        waiter = self._waiters.pop(0)
        self._running = True
        self._running_samples = waiter.samples
        waiter.future.set_result(None)

    async def transcribe(
        self,
        pcm: bytes,
        *,
        language: str = "Chinese",
        kind: str = "interactive",
    ) -> tuple[dict[str, Any], int]:
        samples = len(pcm) // 2
        self._admit(samples)
        waiter = _Waiter(
            priority=PRIORITY.get(kind, 0),
            ticket=next(self._tickets),
            samples=samples,
            future=asyncio.get_running_loop().create_future(),
        )
        self._waiters.append(waiter)
        queued_at = time.perf_counter()
        self._wake_next()
        try:
            await waiter.future
        except asyncio.CancelledError:
            if waiter in self._waiters:
                self._waiters.remove(waiter)
            else:
                self._release()
            raise
        queue_ms = round((time.perf_counter() - queued_at) * 1000)
        try:
            return await self._worker.transcribe(pcm, language=language), queue_ms
        finally:
            self._release()

    def _release(self) -> None:
        self._running = False
        self._running_samples = 0
        self._wake_next()
