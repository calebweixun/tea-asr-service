from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Literal

import numpy as np

from .vad import VAD_WINDOW_SAMPLES, SileroVad, VadSession

Boundary = Literal["silence", "max_duration", "stop"]


@dataclass(frozen=True, slots=True)
class SpeechStarted:
    start_sample: int


@dataclass(frozen=True, slots=True)
class SegmentClosed:
    start_sample: int
    end_sample: int
    pcm: bytes
    boundary: Boundary


SegmenterEvent = SpeechStarted | SegmentClosed


@dataclass(frozen=True, slots=True)
class SegmenterConfig:
    """Initial values from docs/03; they need calibration against real speech."""

    min_speech_ms: int = 160
    end_silence_ms: int = 500
    #: Mandarin onsets ramp slowly (the ㄓ in 「這」, the ㄇ in 「明」). Measured
    #: on real recordings: 200 ms lost the first word, 600 ms recovered it.
    pre_roll_ms: int = 600
    tail_ms: int = 200
    max_segment_ms: int = 12_000
    threshold: float = 0.5
    #: Hysteresis: once speech is running it takes a lower score to keep it,
    #: so a brief dip inside a word does not close the segment.
    neg_threshold: float = 0.35
    #: Once speech is confirmed, walk back over windows still above this score
    #: to find where the utterance actually began, then apply the pre-roll.
    onset_threshold: float = 0.2
    #: Calibrated on real speech: at 8 s a long sentence was cut mid-phrase and
    #: the model completed it anyway, so the same words came out on both sides
    #: of the split. Most speakers pause well before 12 s, so the cap now rarely
    #: fires and a natural silence closes the segment instead.
    #:
    #: A hard split lands mid-word and the model then transcribes the same word
    #: on both sides. Look for the quietest window in this much audio before the
    #: cap and cut there instead.
    split_search_ms: int = 1_200
    #: If nothing quiet turns up at the cap, wait this much longer rather than
    #: cutting mid-word. Only after the grace does the hard cut apply, so the
    #: real upper bound on a segment is max_segment_ms + split_grace_ms.
    split_grace_ms: int = 2_000

    def samples(self, milliseconds: int) -> int:
        return milliseconds * 16

    @property
    def hard_cap_ms(self) -> int:
        """The real upper bound a client can rely on."""

        return self.max_segment_ms + self.split_grace_ms


class ContinuousSegmenter:
    """Turns a continuous 16 kHz PCM stream into closed segments.

    The source sample clock always includes silence, so every emitted range is
    an absolute position in the session timeline and never an index into
    silence-stripped audio (docs/03, docs/04).
    """

    def __init__(
        self,
        vad: SileroVad,
        config: SegmenterConfig | None = None,
        *,
        start_sample: int = 0,
    ) -> None:
        self._vad = vad
        self._vad_session = VadSession.new()
        self._config = config or SegmenterConfig()
        self._buffer = bytearray()
        self._buffer_start = start_sample
        self._cursor = start_sample
        self._next_sample = start_sample
        self._state: Literal["silence", "pending", "speech"] = "silence"
        self._candidate_start: int | None = None
        self._pending_speech_samples = 0
        self._segment_start: int | None = None
        self._last_speech_end = start_sample
        self._trailing_silence = 0
        self._committed_end = start_sample
        #: (window_start, probability) for recent windows, used to backtrack to
        #: the true onset and to choose a quiet hard-split point.
        self._recent: deque[tuple[int, float]] = deque(maxlen=512)

    @property
    def next_sample(self) -> int:
        return self._next_sample

    @property
    def in_speech(self) -> bool:
        return self._state == "speech"

    def open_segment_audio(self) -> tuple[int, bytes] | None:
        """Audio of the segment currently being spoken, for streaming preview.

        Returns None outside speech. The returned range ends at the last
        analysed window, not at the newest byte, so preview and the eventual
        final read the same timeline.
        """

        if self._state != "speech" or self._segment_start is None:
            return None
        offset = (self._segment_start - self._buffer_start) * 2
        length = (self._cursor - self._segment_start) * 2
        if length <= 0:
            return None
        return self._segment_start, bytes(self._buffer[offset : offset + length])

    def push(self, pcm: bytes) -> list[SegmenterEvent]:
        if len(pcm) % 2:
            raise ValueError("PCM byte length must be even")
        self._buffer.extend(pcm)
        self._next_sample += len(pcm) // 2
        events: list[SegmenterEvent] = []
        while self._cursor + VAD_WINDOW_SAMPLES <= self._next_sample:
            offset = (self._cursor - self._buffer_start) * 2
            window = np.frombuffer(
                self._buffer[offset : offset + VAD_WINDOW_SAMPLES * 2], dtype="<i2"
            ).astype(np.float32) / 32768.0
            probability = self._vad.probability(window, self._vad_session)
            self._recent.append((self._cursor, probability))
            events.extend(self._step(probability, self._cursor, self._cursor + VAD_WINDOW_SAMPLES))
            self._cursor += VAD_WINDOW_SAMPLES
        self._trim()
        return events

    def flush(self) -> list[SegmenterEvent]:
        """Close whatever is open at session stop.

        A `pending` candidate is below the minimum speech length, so it is
        dropped here exactly as it would be mid-stream rather than becoming a
        one-window segment of noise.
        """

        if self._state != "speech":
            self._state = "silence"
            self._candidate_start = None
            return []
        return [self._close("stop")]

    # -- state machine -------------------------------------------------------

    def _step(self, probability: float, start: int, end: int) -> list[SegmenterEvent]:
        config = self._config
        events: list[SegmenterEvent] = []
        if self._state == "speech":
            speaking = probability >= config.neg_threshold
        else:
            speaking = probability >= config.threshold

        if self._state == "silence":
            if speaking:
                self._state = "pending"
                self._candidate_start = start
                self._pending_speech_samples = VAD_WINDOW_SAMPLES
                if self._pending_speech_samples >= config.samples(config.min_speech_ms):
                    events.append(self._confirm(end))
            return events

        if self._state == "pending":
            if not speaking:
                self._state = "silence"
                self._candidate_start = None
                self._pending_speech_samples = 0
                return events
            self._pending_speech_samples += VAD_WINDOW_SAMPLES
            if self._pending_speech_samples >= config.samples(config.min_speech_ms):
                events.append(self._confirm(end))
            return events

        # self._state == "speech"
        if speaking:
            self._last_speech_end = end
            self._trailing_silence = 0
        else:
            self._trailing_silence += VAD_WINDOW_SAMPLES
            if self._trailing_silence >= config.samples(config.end_silence_ms):
                events.append(self._close("silence"))
                return events

        assert self._segment_start is not None
        spoken = end - self._segment_start
        if spoken >= config.samples(config.max_segment_ms):
            point = self._quiet_split_point(end)
            forced = spoken >= config.samples(config.hard_cap_ms)
            if point >= end and not forced:
                # Still mid-word: keep going for now rather than cutting a word
                # in half and having the model transcribe it on both sides.
                return events
            split = self._close("max_duration", end=point)
            events.append(split)
            # An endless talker keeps speaking across the split, so the next
            # segment starts exactly where this one ended: no gap, no overlap.
            self._state = "speech"
            self._segment_start = split.end_sample
            self._last_speech_end = max(split.end_sample, end)
            self._trailing_silence = 0
        return events

    def _quiet_split_point(self, cap: int) -> int:
        """Pick the quietest recent window instead of cutting mid-word."""

        assert self._segment_start is not None
        config = self._config
        # Keep at least half the target length on this side, so a quiet spot
        # near the start of the segment cannot carve off a sliver.
        earliest = max(
            self._segment_start + config.samples(config.max_segment_ms // 2),
            cap - config.samples(config.split_search_ms),
        )
        candidates = [
            (probability, window_start)
            for window_start, probability in self._recent
            if earliest <= window_start < cap
        ]
        if not candidates:
            return cap
        probability, window_start = min(candidates)
        if probability >= config.neg_threshold:
            # Nothing quiet enough to cut cleanly; the hard cap still applies.
            return cap
        return window_start

    def _onset(self) -> int:
        """Walk back from the candidate over windows that still look like speech."""

        assert self._candidate_start is not None
        onset = self._candidate_start
        for window_start, probability in reversed(self._recent):
            if window_start >= onset:
                continue
            if probability < self._config.onset_threshold:
                break
            onset = window_start
        return onset

    def _confirm(self, end: int) -> SpeechStarted:
        assert self._candidate_start is not None
        config = self._config
        start = max(
            self._buffer_start,
            self._committed_end,
            self._onset() - config.samples(config.pre_roll_ms),
        )
        self._state = "speech"
        self._segment_start = start
        self._last_speech_end = end
        self._trailing_silence = 0
        self._candidate_start = None
        self._pending_speech_samples = 0
        return SpeechStarted(start_sample=start)

    def _close(self, boundary: Boundary, *, end: int | None = None) -> SegmentClosed:
        assert self._segment_start is not None
        config = self._config
        start = self._segment_start
        if end is None:
            end = min(self._next_sample, self._last_speech_end + config.samples(config.tail_ms))
        end = max(end, start)
        offset = (start - self._buffer_start) * 2
        pcm = bytes(self._buffer[offset : offset + (end - start) * 2])
        self._committed_end = end
        if boundary != "max_duration":
            self._state = "silence"
            self._segment_start = None
            self._trailing_silence = 0
        return SegmentClosed(start_sample=start, end_sample=end, pcm=pcm, boundary=boundary)

    def _trim(self) -> None:
        """Drop audio no future segment can need, so RAM stays bounded."""

        config = self._config
        if self._state == "speech" and self._segment_start is not None:
            keep_from = self._segment_start
        elif self._state == "pending" and self._candidate_start is not None:
            keep_from = self._candidate_start - config.samples(config.pre_roll_ms)
        else:
            # Speech is only confirmed after min_speech_ms, so the pre-roll has
            # to survive that long or the first word is already gone.
            keep_from = self._next_sample - config.samples(
                config.pre_roll_ms + config.min_speech_ms + config.split_search_ms // 6
            )
        keep_from = max(self._buffer_start, min(keep_from, self._cursor))
        drop = (keep_from - self._buffer_start) * 2
        if drop > 0:
            del self._buffer[:drop]
            self._buffer_start = keep_from
