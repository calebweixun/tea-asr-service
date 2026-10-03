from __future__ import annotations

import struct

import numpy as np

from tea_asr.api.stream import StreamSession
from tea_asr.config import ServiceConfig
from tea_asr.segmenter import SpeechStarted


class StartedOnceSegmenter:
    def __init__(self) -> None:
        self.started = False
        self.state = "speech"

    def push(self, pcm: bytes) -> list[SpeechStarted]:
        del pcm
        if self.started:
            return []
        self.started = True
        return [SpeechStarted(start_sample=0)]


def test_streaming_hook_emits_first_class_and_never_more_than_one_revision() -> None:
    session = StreamSession(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        config=ServiceConfig(singing_detection_enabled=True),
        model_state="ready",
    )
    session._profile = "continuous"
    session._segmenter = StartedOnceSegmenter()  # type: ignore[assignment]
    chunk_samples = 3_200
    first_decision_s = None
    for index in range(15):
        start_sample = index * chunk_samples
        time = np.arange(chunk_samples, dtype=np.float64) / 16_000 + start_sample / 16_000
        pcm = (0.35 * np.sin(2 * np.pi * 220 * time) * 32767).astype("<i2").tobytes()
        session._handle_frame(struct.pack("<QQ", index, start_sample) + pcm)
        if first_decision_s is None and any(
            event["type"] == "segment.audio_class" for event in session._writer._queue
        ):
            first_decision_s = (index + 1) * chunk_samples / 16_000

    events = [event for event in session._writer._queue if event["type"] == "segment.audio_class"]
    assert first_decision_s is not None and first_decision_s <= 1.5
    assert len(events) in {1, 2}
    assert events[0]["revision"] == 0
    if len(events) == 2:
        assert events[1]["revision"] == 1
        assert events[1]["class"] != events[0]["class"]
