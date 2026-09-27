"""Revisable-preview cadence: new-audio gate, interval floor, load guard, and
final-before-preview ordering (docs/07「輕量化與排程」).

The fake schedulers here control how long a "decode" takes, so the timing
assertions do not depend on a real model.
"""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path
from typing import Any

import pytest

from tea_asr.api.app import create_app
from tea_asr.api.stream import StreamSession
from tea_asr.config import ServiceConfig
from tea_asr.scheduler import Scheduler, StaleTaskDropped
from tests.conftest import FakeSupervisor

SAMPLES_PER_MS = 16


class RecordingSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


class TimedScheduler:
    """Every call takes `decode_s` of wall time; records when each one started."""

    def __init__(self, decode_s: float = 0.0) -> None:
        self.decode_s = decode_s
        self.starts: list[float] = []
        self.samples: list[int] = []

    async def transcribe(
        self,
        pcm: bytes,
        *,
        language: str = "Chinese",
        kind: str = "interactive",
        is_stale: Any = None,
    ) -> tuple[dict[str, Any], int]:
        loop = asyncio.get_running_loop()
        self.starts.append(loop.time())
        self.samples.append(len(pcm) // 2)
        if self.decode_s:
            await asyncio.sleep(self.decode_s)
        return {"text": "測試", "total_time_s": self.decode_s}, 0


def revisable_session(scheduler: Any, **cadence: Any) -> StreamSession:
    session = StreamSession(
        RecordingSocket(),  # type: ignore[arg-type]
        scheduler,
        config=ServiceConfig(**cadence),
        model_state="ready",
    )
    session._transcript_mode = "revisable"
    return session


def feed(session: StreamSession, ms: int) -> None:
    """Append `ms` of audio to the open utterance and let the gates decide."""

    state = session._state
    if state.segment is None:
        session._open_segment(state.next_sample)
    samples = ms * SAMPLES_PER_MS
    state.pcm.extend(b"\1\0" * samples)
    state.next_sample += samples
    session._maybe_schedule_preview()


async def settle(session: StreamSession) -> None:
    task = session._preview_task
    if task is not None:
        await task


# -- gates ---------------------------------------------------------------------


def test_first_preview_waits_only_for_the_configured_new_audio() -> None:
    async def scenario() -> list[int]:
        scheduler = TimedScheduler()
        session = revisable_session(
            scheduler, preview_min_audio_ms=300, preview_min_interval_ms=300
        )
        feed(session, 200)
        await asyncio.sleep(0)
        assert scheduler.samples == [], "200 ms is below the 300 ms new-audio gate"
        feed(session, 100)
        await settle(session)
        return scheduler.samples

    # 300 ms, not the old fixed 800 ms.
    assert asyncio.run(scenario()) == [300 * SAMPLES_PER_MS]


def test_next_preview_needs_new_audio_since_the_last_published_one() -> None:
    async def scenario() -> list[int]:
        scheduler = TimedScheduler()
        session = revisable_session(
            scheduler, preview_min_audio_ms=300, preview_min_interval_ms=100
        )
        feed(session, 300)
        await settle(session)
        await asyncio.sleep(0.15)  # the interval gate is open again
        feed(session, 200)  # only 200 ms new since the published 300 ms
        await asyncio.sleep(0.05)
        assert len(scheduler.samples) == 1
        feed(session, 100)
        await settle(session)
        return scheduler.samples

    assert asyncio.run(scenario()) == [300 * SAMPLES_PER_MS, 600 * SAMPLES_PER_MS]


def test_interval_gate_retries_on_its_own_without_another_frame() -> None:
    """A preview held back by the interval runs when the gate opens, not on
    the client's next frame (the cadence must not depend on frame size)."""

    async def scenario() -> list[float]:
        scheduler = TimedScheduler()
        session = revisable_session(
            scheduler, preview_min_audio_ms=300, preview_min_interval_ms=300
        )
        feed(session, 300)
        await settle(session)
        feed(session, 300)  # enough audio, but only ~0 ms since the last start
        assert len(scheduler.starts) == 1
        await asyncio.sleep(0.45)  # no further frames
        return scheduler.starts

    starts = asyncio.run(scenario())
    assert len(starts) == 2
    assert 0.29 <= starts[1] - starts[0] < 0.6


def test_preview_gap_is_the_floor_or_k_times_the_last_decode() -> None:
    session = revisable_session(
        TimedScheduler(), preview_min_interval_ms=300, preview_load_factor=2.0
    )
    session._preview_last_decode_s = 0.05
    assert session._preview_gap_s() == pytest.approx(0.3)
    session._preview_last_decode_s = 0.4
    assert session._preview_gap_s() == pytest.approx(0.8)
    unguarded = revisable_session(
        TimedScheduler(), preview_min_interval_ms=300, preview_load_factor=0.0
    )
    unguarded._preview_last_decode_s = 0.4
    assert unguarded._preview_gap_s() == pytest.approx(0.3)


def test_load_guard_keeps_one_sessions_previews_under_half_the_worker() -> None:
    """Slow decodes stretch the interval to k x decode time (k=2 -> <=50%)."""

    async def scenario(load_factor: float) -> list[float]:
        scheduler = TimedScheduler(decode_s=0.2)
        session = revisable_session(
            scheduler,
            preview_min_audio_ms=100,
            preview_min_interval_ms=100,
            preview_load_factor=load_factor,
        )
        # A talker who never stops: 100 ms of audio every 100 ms for 2 s.
        for _ in range(20):
            feed(session, 100)
            await asyncio.sleep(0.1)
        await settle(session)
        return scheduler.starts

    def duty(starts: list[float]) -> float:
        # Worker time of every preview but the last, over the span they cover.
        return (len(starts) - 1) * 0.2 / (starts[-1] - starts[0])

    starts = asyncio.run(scenario(2.0))
    gaps = [later - earlier for earlier, later in itertools.pairwise(starts)]
    assert len(gaps) >= 3, "the session should still get several previews"
    assert min(gaps) >= 0.39, gaps  # 2 x 0.2 s decode, minus timer slack
    assert duty(starts) <= 0.51

    # Without the guard the same talker keeps the worker busy back to back.
    unguarded = asyncio.run(scenario(0.0))
    assert len(unguarded) > len(starts)
    assert duty(unguarded) > 0.9


def test_decode_time_excludes_time_spent_queued() -> None:
    class Queued(TimedScheduler):
        async def transcribe(self, pcm: bytes, **kwargs: Any) -> tuple[dict[str, Any], int]:
            await asyncio.sleep(0.3)  # 300 ms waiting behind someone's final
            result, _ = await super().transcribe(pcm, **kwargs)
            return result, 300

    async def scenario() -> float:
        session = revisable_session(Queued(decode_s=0.05), preview_min_audio_ms=100)
        feed(session, 100)
        await settle(session)
        return session._preview_last_decode_s

    assert asyncio.run(scenario()) == pytest.approx(0.05, abs=0.03)


# -- finals before previews ------------------------------------------------------


class BlockingWorker:
    state = "ready"

    def __init__(self) -> None:
        self.order: list[str] = []
        self.release = asyncio.Event()

    async def transcribe(self, pcm: bytes, *, language: str = "Chinese") -> dict[str, object]:
        self.order.append(pcm[:2].decode())
        await self.release.wait()
        return {"text": "ok", "total_time_s": 0.0}


async def _until(predicate: Any) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


def test_another_sessions_final_runs_before_a_queued_preview() -> None:
    async def scenario() -> list[str]:
        worker = BlockingWorker()
        scheduler = Scheduler(worker)
        running = asyncio.create_task(scheduler.transcribe(b"00", kind="realtime"))
        await _until(lambda: scheduler.waiting_tasks == 1)
        preview_a = asyncio.create_task(scheduler.transcribe(b"pa", kind="preview"))
        final_b = asyncio.create_task(scheduler.transcribe(b"fb", kind="realtime"))
        await _until(lambda: scheduler.waiting_tasks == 3)
        worker.release.set()
        await asyncio.gather(running, preview_a, final_b)
        return worker.order

    assert asyncio.run(scenario()) == ["00", "fb", "pa"]


def test_stale_preview_is_dropped_instead_of_running_after_the_final() -> None:
    async def scenario() -> tuple[list[str], bool]:
        worker = BlockingWorker()
        scheduler = Scheduler(worker)
        stale = False
        running = asyncio.create_task(scheduler.transcribe(b"00", kind="realtime"))
        await _until(lambda: scheduler.waiting_tasks == 1)
        preview = asyncio.create_task(
            scheduler.transcribe(b"pp", kind="preview", is_stale=lambda: stale)
        )
        final = asyncio.create_task(scheduler.transcribe(b"ff", kind="realtime"))
        await _until(lambda: scheduler.waiting_tasks == 3)
        stale = True  # its segment closed
        scheduler.drop_stale()
        with pytest.raises(StaleTaskDropped):
            await asyncio.wait_for(preview, timeout=1.0)
        # Dropped while the worker is still busy: nothing waited on the worker.
        dropped_early = not worker.release.is_set()
        worker.release.set()
        await asyncio.gather(running, final)
        assert scheduler.waiting_tasks == 0
        return worker.order, dropped_early

    order, dropped_early = asyncio.run(scenario())
    assert order == ["00", "ff"]
    assert dropped_early


def test_cancelling_a_dropped_preview_does_not_free_the_busy_worker() -> None:
    async def scenario() -> list[str]:
        worker = BlockingWorker()
        scheduler = Scheduler(worker)
        running = asyncio.create_task(scheduler.transcribe(b"00", kind="realtime"))
        await _until(lambda: scheduler.waiting_tasks == 1)
        preview = asyncio.create_task(
            scheduler.transcribe(b"pp", kind="preview", is_stale=lambda: True)
        )
        await asyncio.sleep(0)
        scheduler.drop_stale()
        preview.cancel()
        await asyncio.gather(preview, return_exceptions=True)
        # The worker is still running "00": a new final must wait for it.
        final = asyncio.create_task(scheduler.transcribe(b"ff", kind="realtime"))
        await asyncio.sleep(0)
        assert worker.order == ["00"]
        worker.release.set()
        await asyncio.gather(running, final)
        return worker.order

    assert asyncio.run(scenario()) == ["00", "ff"]


def test_closing_a_segment_drops_its_queued_preview_in_the_session() -> None:
    """End to end in one session: the preview queued behind another session's
    final never reaches the worker once its segment closes."""

    async def scenario() -> tuple[list[str], list[dict[str, Any]]]:
        worker = BlockingWorker()
        scheduler = Scheduler(worker)
        other_final = asyncio.create_task(scheduler.transcribe(b"00", kind="realtime"))
        await _until(lambda: scheduler.waiting_tasks == 1)
        session = revisable_session(scheduler, preview_min_audio_ms=100)
        session._state.pcm.extend(b"pp" * 1_600)  # logged as "pp" by the worker
        session._state.next_sample = 1_600
        session._open_segment(0)
        session._maybe_schedule_preview()
        await _until(lambda: scheduler.waiting_tasks == 2)
        segment = session._state.segment
        assert segment is not None
        session._settle_preview_soon(segment)
        await asyncio.wait_for(session._settle_preview(), timeout=1.0)
        worker.release.set()
        await other_final
        return worker.order, session._websocket.sent  # type: ignore[attr-defined]

    order, sent = asyncio.run(scenario())
    assert order == ["00"]
    assert not [event for event in sent if event.get("type") == "preview.status"]


# -- config --------------------------------------------------------------------


def test_cadence_is_server_config_with_env_overrides() -> None:
    config = ServiceConfig.load(
        env={
            "TEA_ASR_PREVIEW_MIN_INTERVAL_MS": "450",
            "TEA_ASR_PREVIEW_MIN_AUDIO_MS": "350",
            "TEA_ASR_PREVIEW_LOAD_FACTOR": "3",
        }
    )
    assert config.preview_min_interval_ms == 450
    assert config.preview_min_audio_ms == 350
    assert config.preview_load_factor == 3.0


@pytest.mark.parametrize(
    "override",
    [
        {"preview_min_interval_ms": 50},
        {"preview_min_audio_ms": 6000},
        {"preview_load_factor": -1.0},
        {"preview_load_factor": 11.0},
    ],
)
def test_out_of_range_cadence_refuses_to_start(override: dict[str, Any]) -> None:
    with pytest.raises(RuntimeError):
        create_app(
            Path("unused"),
            token="test-token",
            supervisor=FakeSupervisor(),
            config=ServiceConfig(**override),
            vad_model=None,
        )
