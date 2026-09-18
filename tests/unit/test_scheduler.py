from __future__ import annotations

import asyncio

import pytest

from tea_asr.errors import ApiError
from tea_asr.scheduler import Scheduler


class BlockingWorker:
    state = "ready"

    def __init__(self) -> None:
        self.order: list[str] = []
        self.release = asyncio.Event()

    async def transcribe(self, pcm: bytes, *, language: str = "Chinese") -> dict[str, object]:
        self.order.append(pcm.decode())
        await self.release.wait()
        return {"text": "ok", "total_time_s": 0.0}


async def _settle(scheduler: Scheduler, expected: int) -> None:
    for _ in range(100):
        if scheduler.waiting_tasks == expected:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"scheduler never reached {expected} in-flight tasks")


def test_rejects_when_task_limit_is_reached() -> None:
    async def scenario() -> None:
        worker = BlockingWorker()
        scheduler = Scheduler(worker, max_waiting_tasks=2)
        pending = [
            asyncio.create_task(scheduler.transcribe(b"aa", kind="interactive")),
            asyncio.create_task(scheduler.transcribe(b"bb", kind="interactive")),
        ]
        await _settle(scheduler, 2)
        with pytest.raises(ApiError) as caught:
            await scheduler.transcribe(b"cc", kind="interactive")
        assert caught.value.code == "queue_full"
        assert caught.value.retryable is True
        assert caught.value.http_status == 429
        worker.release.set()
        await asyncio.gather(*pending)

    asyncio.run(scenario())


def test_rejects_when_waiting_audio_limit_is_reached() -> None:
    async def scenario() -> None:
        worker = BlockingWorker()
        scheduler = Scheduler(worker, max_waiting_samples=4)
        running = asyncio.create_task(scheduler.transcribe(b"aaaaaaaa", kind="interactive"))
        await _settle(scheduler, 1)
        with pytest.raises(ApiError) as caught:
            await scheduler.transcribe(b"bbbbbbbb", kind="interactive")
        assert caught.value.code == "queue_full"
        worker.release.set()
        await running

    asyncio.run(scenario())


def test_interactive_work_runs_before_preview() -> None:
    async def scenario() -> None:
        worker = BlockingWorker()
        scheduler = Scheduler(worker)
        blocking = asyncio.create_task(scheduler.transcribe(b"00", kind="interactive"))
        await _settle(scheduler, 1)
        preview = asyncio.create_task(scheduler.transcribe(b"pp", kind="preview"))
        await _settle(scheduler, 2)
        final = asyncio.create_task(scheduler.transcribe(b"ff", kind="interactive"))
        await _settle(scheduler, 3)
        worker.release.set()
        await asyncio.gather(blocking, preview, final)
        # The preview was queued first but must not delay the pending final.
        assert worker.order == ["00", "ff", "pp"]

    asyncio.run(scenario())


def test_counters_return_to_zero_and_queue_ms_is_reported() -> None:
    async def scenario() -> None:
        worker = BlockingWorker()
        worker.release.set()
        scheduler = Scheduler(worker)
        _, queue_ms = await scheduler.transcribe(b"00", kind="interactive")
        assert queue_ms >= 0
        assert scheduler.waiting_tasks == 0
        assert scheduler.waiting_samples == 0

    asyncio.run(scenario())
