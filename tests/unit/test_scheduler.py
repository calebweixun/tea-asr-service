from __future__ import annotations

import asyncio

import pytest

from tea_asr import scheduler as scheduler_module
from tea_asr.errors import ApiError
from tea_asr.scheduler import Scheduler
from tea_asr.worker.token_budget import max_tokens_for_pcm


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


def test_a_cancelled_caller_keeps_the_worker_until_its_call_really_ends() -> None:
    """A session closing mid-preview cancels the caller, not the worker call.

    The call finishes on its own (the worker cannot drop a request halfway),
    so the next task must not be handed the worker until it has.
    """

    async def scenario() -> tuple[list[str], int, int]:
        worker = BlockingWorker()
        scheduler = Scheduler(worker)
        preview = asyncio.create_task(scheduler.transcribe(b"pp", kind="preview"))
        await _settle(scheduler, 1)
        final = asyncio.create_task(scheduler.transcribe(b"ff", kind="realtime"))
        await _settle(scheduler, 2)
        preview.cancel()
        with pytest.raises(asyncio.CancelledError):
            await preview
        for _ in range(10):
            await asyncio.sleep(0)
        started_while_busy = list(worker.order)
        still_waiting = scheduler.waiting_tasks
        worker.release.set()
        await final
        return started_while_busy, still_waiting, scheduler.waiting_tasks

    started_while_busy, still_waiting, after = asyncio.run(scenario())
    assert started_while_busy == ["pp"], "the final must wait for the abandoned preview"
    assert still_waiting == 2
    assert after == 0


def test_next_worker_call_starts_after_finished_result_is_handled() -> None:
    """The done callback wakes a waiter, but does not run its worker inline."""

    class OrderedWorker:
        state = "ready"

        def __init__(self) -> None:
            self.events: list[str] = []
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()

        async def transcribe(self, pcm: bytes, *, language: str = "Chinese") -> dict[str, str]:
            name = pcm.decode()
            self.events.append(f"start:{name}")
            if name == "final":
                self.first_started.set()
                await self.release_first.wait()
            self.events.append(f"finish:{name}")
            return {"text": name}

    async def scenario() -> list[str]:
        worker = OrderedWorker()
        scheduler = Scheduler(worker)

        async def handle_final() -> None:
            result, _ = await scheduler.transcribe(b"final", kind="realtime")
            worker.events.append(f"handled:{result['text']}")

        final = asyncio.create_task(handle_final())
        try:
            await asyncio.wait_for(worker.first_started.wait(), 1)
            preview = asyncio.create_task(scheduler.transcribe(b"preview", kind="preview"))
            await asyncio.wait_for(_settle(scheduler, 2), 1)
            worker.release_first.set()
            await asyncio.wait_for(asyncio.gather(final, preview), 1)
            return worker.events
        finally:
            worker.release_first.set()

    events = asyncio.run(scenario())
    assert events == [
        "start:final",
        "finish:final",
        "handled:final",
        "start:preview",
        "finish:preview",
    ]


def test_token_budget_hit_logs_audio_duration_kind_and_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []

    def record_event(_logger: object, name: str, **fields: object) -> None:
        events.append((name, fields))

    monkeypatch.setattr(scheduler_module, "event", record_event, raising=False)

    class RepeatingWorker:
        state = "ready"

        async def transcribe(self, pcm: bytes, *, language: str = "Chinese") -> dict[str, object]:
            cap = max_tokens_for_pcm(pcm)
            return {"text": "repeat" * cap, "generation_tokens": cap}

    async def scenario() -> None:
        await Scheduler(RepeatingWorker()).transcribe(  # type: ignore[arg-type]
            b"\x00\x00" * 16_000, kind="preview"
        )

    asyncio.run(scenario())
    assert events == [
        (
            "asr.max_tokens_hit",
            {"audio_ms": 1_000, "kind": "preview", "max_tokens": 19},
        )
    ]
