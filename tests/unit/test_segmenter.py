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
    # Pre-roll plus onset backtracking pull the start earlier than the first
    # window that crossed the threshold, and the range is an absolute position
    # on the source clock.
    config = SegmenterConfig()
    earliest = 16_000 - config.samples(config.pre_roll_ms) - VAD_WINDOW_SAMPLES
    assert earliest <= closed[0].start_sample <= 16_000
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
    config = SegmenterConfig(max_segment_ms=1000, split_grace_ms=0)
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
    config = SegmenterConfig()
    machine = ContinuousSegmenter(EnergyVad(), config)
    for _ in range(200):  # 20 s of silence
        machine.push(silence(100))
    # Speech is confirmed retroactively, so the retained window is the pre-roll
    # plus the confirmation delay — bounded, and far below the 20 s pushed in.
    retained = config.samples(
        config.pre_roll_ms + config.min_speech_ms + config.split_search_ms // 6
    )
    assert len(machine._buffer) <= (retained + VAD_WINDOW_SAMPLES) * 2


def test_hard_split_waits_for_a_quiet_moment_before_cutting() -> None:
    """A cut in the middle of a word makes the model emit it on both sides."""

    config = SegmenterConfig(max_segment_ms=1000, split_grace_ms=2000)
    # Unbroken speech: nothing quiet to cut on until the grace runs out.
    events = segment(silence(300) + tone(4000) + silence(800), config)
    closed = [e for e in events if isinstance(e, SegmentClosed)]
    forced = [e for e in closed if e.boundary == "max_duration"]
    assert forced, "the hard cap still applies once the grace is spent"
    for item in forced:
        spoken = (item.end_sample - item.start_sample) / 16
        assert spoken <= config.hard_cap_ms + 100
        assert spoken >= config.max_segment_ms


def test_a_quiet_gap_is_preferred_over_the_exact_cap() -> None:
    config = SegmenterConfig(max_segment_ms=1000, split_grace_ms=2000, end_silence_ms=5000)
    # A short dip 300 ms after the cap: the split should land in it rather than
    # waiting for the full grace.
    events = segment(silence(300) + tone(1200) + silence(200) + tone(1200) + silence(6000), config)
    closed = [e for e in events if isinstance(e, SegmentClosed)]
    assert closed[0].boundary == "max_duration"
    spoken_ms = (closed[0].end_sample - closed[0].start_sample) / 16
    assert spoken_ms < config.hard_cap_ms, "it should not have waited out the grace"


def test_segment_audio_length_always_matches_its_sample_range() -> None:
    """A mismatch here means the model was handed audio from the wrong place."""

    config = SegmenterConfig(max_segment_ms=1000, split_grace_ms=500)
    stream = silence(400) + tone(2500) + silence(700) + tone(900) + silence(800)
    for event in segment(stream, config):
        if isinstance(event, SegmentClosed):
            expected = (event.end_sample - event.start_sample) * 2
            assert len(event.pcm) == expected, (
                f"{event.boundary} 片段的 PCM 長度與 sample 範圍不符"
            )
            assert event.start_sample >= 0
            assert event.end_sample > event.start_sample
