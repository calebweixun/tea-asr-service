from __future__ import annotations

import itertools
from typing import Any

import numpy as np

from tea_asr.segmenter import ContinuousSegmenter, SegmentClosed, SegmenterConfig, SpeechStarted
from tea_asr.vad import VAD_WINDOW_SAMPLES


class EnergyVad:
    """Any window with real energy is speech; keeps the test deterministic."""

    def probability(self, window: np.ndarray, session: Any) -> float:
        return 1.0 if float(np.abs(window).max()) > 0.05 else 0.0


def tone(ms: int) -> bytes:
    samples = np.full(ms * 16, 8000, dtype="<i2")
    return samples.tobytes()


def silence(ms: int) -> bytes:
    return b"\0\0" * (ms * 16)


def segment(stream: bytes, config: SegmenterConfig | None = None) -> list[Any]:
    machine = ContinuousSegmenter(EnergyVad(), config)
    events: list[Any] = []
    for offset in range(0, len(stream), 3200):
        events.extend(machine.push(stream[offset : offset + 3200]))
    events.extend(machine.flush())
    return events


def test_speech_between_silence_becomes_one_segment() -> None:
    events = segment(silence(1000) + tone(1000) + silence(1000))
    started = [e for e in events if isinstance(e, SpeechStarted)]
    closed = [e for e in events if isinstance(e, SegmentClosed)]
    assert len(started) == 1
    assert len(closed) == 1
    assert closed[0].boundary == "silence"
    # Pre-roll pulls the start earlier than the first speech window, and the
    # range is an absolute position on the source clock.
    assert 12_000 <= closed[0].start_sample <= 16_000
    assert closed[0].end_sample > closed[0].start_sample
    assert len(closed[0].pcm) == (closed[0].end_sample - closed[0].start_sample) * 2


def test_two_utterances_produce_two_segments_in_order() -> None:
    events = segment(silence(600) + tone(800) + silence(900) + tone(800) + silence(700))
    closed = [e for e in events if isinstance(e, SegmentClosed)]
    assert len(closed) == 2
    assert closed[0].end_sample <= closed[1].start_sample
    assert all(event.boundary == "silence" for event in closed)


def test_short_blip_below_min_speech_is_not_a_segment() -> None:
    events = segment(silence(500) + tone(60) + silence(800))
    assert not [e for e in events if isinstance(e, SegmentClosed)]


def test_endless_speech_is_hard_split_without_gap_or_overlap() -> None:
    config = SegmenterConfig(max_segment_ms=1000)
    events = segment(silence(300) + tone(3500) + silence(800), config)
    closed = [e for e in events if isinstance(e, SegmentClosed)]
    assert len(closed) >= 3
    assert closed[0].boundary == "max_duration"
    for previous, following in itertools.pairwise(closed):
        assert following.start_sample == previous.end_sample
    assert closed[-1].boundary in {"silence", "stop"}


def test_stop_closes_speech_that_is_still_running() -> None:
    events = segment(silence(300) + tone(1200))
    closed = [e for e in events if isinstance(e, SegmentClosed)]
    assert len(closed) == 1
    assert closed[0].boundary == "stop"


def test_buffer_does_not_grow_without_bound_during_silence() -> None:
    machine = ContinuousSegmenter(EnergyVad())
    for _ in range(200):  # 20 s of silence
        machine.push(silence(100))
    # Only the pre-roll window is retained.
    assert len(machine._buffer) <= (SegmenterConfig().samples(200) + VAD_WINDOW_SAMPLES) * 2
