from __future__ import annotations

import asyncio
import dataclasses
import threading
import time
from collections.abc import Callable

import numpy as np

from tea_asr.singing_session import (
    MAX_FRAMES_PER_CALL,
    SessionSinging,
    SingingRuntime,
)
from tea_asr.yamnet import HOP_SAMPLES, PATCH_SAMPLES, frame_end_sample
from tests.conftest import FakeYamnet, fake_singing_runtime

LOUD = (np.full(1600, 12_000, dtype="<i2")).tobytes()  # 100 ms of tone-like energy
QUIET = bytes(3200)


def make(
    model: object | None = None, *, local_frames: int | None = None
) -> tuple[SessionSinging, list[str], list[int], SingingRuntime]:
    runtime = fake_singing_runtime(model)
    if local_frames is not None:
        runtime.params = dataclasses.replace(runtime.params, local_frames=local_frames)
    failures: list[str] = []
    wakeups: list[int] = []
    session = SessionSinging(
        runtime,
        on_frames=lambda: wakeups.append(session.tracker.frames_seen),
        on_failure=failures.append,
    )
    return session, failures, wakeups, runtime


def feed(session: SessionSinging, pcm: bytes, seconds: float, start: int = 0) -> int:
    """Push `seconds` of 100 ms chunks, returning the next sample."""

    sample = start
    for _ in range(round(seconds * 10)):
        session.push(sample, pcm)
        sample += len(pcm) // 2
    return sample


def run(scenario: Callable[[], object]) -> None:
    asyncio.run(scenario())  # type: ignore[arg-type]


def test_frames_are_scored_in_audio_order_with_exact_alignment() -> None:
    model = FakeYamnet()
    seen: list[int] = []
    real_scores = model.scores

    def spy(waveform: np.ndarray) -> np.ndarray:
        seen.append(waveform.size)
        return real_scores(waveform)

    model.scores = spy  # type: ignore[method-assign]

    async def scenario() -> None:
        session, failures, _, runtime = make(model)
        end = feed(session, LOUD, 4.0)
        await session.settle()
        assert not failures
        # Every frame completed by the audio so far, none invented.
        assert session.tracker.frames_seen == 1 + (end - PATCH_SAMPLES) // HOP_SAMPLES
        # Each call is a whole number of frames: 15 600 + 7 680 * (k - 1).
        assert all((size - PATCH_SAMPLES) % HOP_SAMPLES == 0 for size in seen)
        await session.close()
        runtime.close()

    run(scenario)


def test_scores_use_the_right_slice_of_the_stream() -> None:
    """Quiet then loud audio: frame i must read samples [i * 7680, i * 7680 + 15600)."""

    async def scenario() -> None:
        session, _, _, runtime = make(local_frames=1)
        sample = feed(session, QUIET, 3.0)  # 48 000 samples of silence
        feed(session, LOUD, 3.0, start=sample)
        await session.settle()
        t = session.tracker
        assert t.frames_seen >= 8
        for i in range(t.frames_seen):
            expected_loud = frame_end_sample(i) > 48_000
            assert (t.local_score(i + 1) > 0.5) == expected_loud, i
        await session.close()
        runtime.close()

    run(scenario)


def test_push_does_not_block_while_the_model_is_busy() -> None:
    release = threading.Event()
    entered = threading.Event()

    class Blocking(FakeYamnet):
        def scores(self, waveform: np.ndarray) -> np.ndarray:
            entered.set()
            assert release.wait(timeout=5)
            return super().scores(waveform)

    async def scenario() -> None:
        session, failures, _, runtime = make(Blocking())
        feed(session, LOUD, 2.0)
        await asyncio.sleep(0.05)
        assert entered.is_set()
        started = time.perf_counter()
        feed(session, LOUD, 5.0, start=32_000)  # intake continues while the call hangs
        assert time.perf_counter() - started < 0.2
        release.set()
        await session.settle()
        assert not failures
        assert session.tracker.frames_seen == 1 + (7 * 16_000 - PATCH_SAMPLES) // HOP_SAMPLES
        await session.close()
        runtime.close()

    run(scenario)


def test_backlog_is_scored_in_bounded_batches() -> None:
    model = FakeYamnet()

    async def scenario() -> None:
        session, _, _, runtime = make(model)
        # No await between pushes: nothing runs until the loop gets control.
        feed(session, LOUD, 20.0)
        await session.settle()
        assert max(model.calls) <= MAX_FRAMES_PER_CALL
        assert sum(model.calls) == session.tracker.frames_seen
        await session.close()
        runtime.close()

    run(scenario)


def test_overflowing_the_backlog_stops_labelling_instead_of_skipping_frames() -> None:
    async def scenario() -> None:
        session, failures, _, runtime = make()
        chunk = bytes(6400)
        sample = 0
        for _ in range(500):  # 100 s of audio; the loop never runs, so the executor is "behind"
            session.push(sample, chunk)
            sample += len(chunk) // 2
            if session.failed:
                break
        assert session.failed == "backlog_overflow"
        assert failures == ["backlog_overflow"]
        session.push(sample, chunk)  # further audio is ignored, not an error
        assert failures == ["backlog_overflow"]
        await session.close()
        runtime.close()

    run(scenario)


def test_a_discontinuity_disables_labelling() -> None:
    async def scenario() -> None:
        session, failures, _, runtime = make()
        session.push(0, LOUD)
        session.push(5_000, LOUD)  # the wire guarantees contiguity; if not, stop
        assert failures == ["sample_gap"]
        await session.close()
        runtime.close()

    run(scenario)


def test_model_errors_disable_labelling_without_raising() -> None:
    async def scenario() -> None:
        session, failures, wakeups, runtime = make(FakeYamnet(fail=RuntimeError("boom")))
        feed(session, LOUD, 3.0)
        await session.settle()
        assert failures == ["RuntimeError"]
        assert wakeups == []
        assert session.tracker.frames_seen == 0
        await session.close()
        runtime.close()

    run(scenario)


def test_on_frames_fires_after_each_scored_batch() -> None:
    async def scenario() -> None:
        session, _, wakeups, runtime = make()
        feed(session, LOUD, 3.0)
        await session.settle()
        assert wakeups and wakeups == sorted(wakeups)
        assert wakeups[-1] == session.tracker.frames_seen
        await session.close()
        runtime.close()

    run(scenario)


def test_buffer_is_trimmed_as_frames_are_consumed() -> None:
    async def scenario() -> None:
        session, _, _, runtime = make()
        feed(session, LOUD, 30.0)
        await session.settle()
        # Only audio not yet part of a dispatched frame is kept.
        assert len(session._buffer) // 2 < PATCH_SAMPLES + HOP_SAMPLES
        await session.close()
        runtime.close()

    run(scenario)
