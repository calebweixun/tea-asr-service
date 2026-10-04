"""Per-session stream diagnostics (`tea_asr.diagnostics`, docs/04「W11」).

The wall clock is a plain float the test advances, and PCM is synthesized at a
known level, so every heartbeat field and warning threshold is checked exactly
rather than "roughly after a while".
"""

from __future__ import annotations

import asyncio
import logging
import math
import queue
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tea_asr import diagnostics
from tea_asr.api.stream import StreamSession
from tea_asr.config import ServiceConfig, validate_debug_capture_or_raise
from tea_asr.diagnostics import AudioCapture, SessionDiagnostics, to_dbfs
from tea_asr.segmenter import ContinuousSegmenter

RATE = 16_000
FRAME = 1_600  # 100 ms


@contextmanager
def records() -> Iterator[list[logging.LogRecord]]:
    """Collect `tea_asr.stream` records directly (see test_lan_mode for why
    not caplog)."""

    collected: list[logging.LogRecord] = []

    class Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            collected.append(record)

    target = logging.getLogger("tea_asr.stream")
    handler = Collector(level=logging.DEBUG)
    previous = target.level
    target.addHandler(handler)
    target.setLevel(logging.DEBUG)
    try:
        yield collected
    finally:
        target.removeHandler(handler)
        target.setLevel(previous)


def named(collected: list[logging.LogRecord], message: str) -> list[dict[str, Any]]:
    return [record.fields for record in collected if record.getMessage() == message]  # type: ignore[attr-defined]


class Clock:
    """Keeps whole milliseconds, so 300 x 0.1 s is exactly 30 s."""

    def __init__(self) -> None:
        self._ms = 1_000_000

    @property
    def now(self) -> float:
        return self._ms / 1000

    @now.setter
    def now(self, value: float) -> None:
        self._ms = round(value * 1000)

    def __call__(self) -> float:
        return self.now


def sine(dbfs_rms: float, samples: int, freq: float = 440.0) -> bytes:
    """int16 sine whose RMS is `dbfs_rms` dBFS."""

    amplitude = math.sqrt(2) * 10 ** (dbfs_rms / 20)
    t = np.arange(samples) / RATE
    wave_ = amplitude * np.sin(2 * np.pi * freq * t)
    return (wave_ * 32767).round().astype("<i2").tobytes()


def make(clock: Clock, **kwargs: Any) -> SessionDiagnostics:
    diag = SessionDiagnostics("session-1234", clock=clock, **kwargs)
    diag.session_started(profile="continuous")
    return diag


def feed(diag: SessionDiagnostics, clock: Clock, pcm: bytes, seconds: float) -> None:
    """Send `seconds` of `pcm` (one frame's worth, repeated) at real time."""

    for _ in range(round(seconds * 10)):
        clock.now += 0.1
        diag.on_frame(pcm)


class ScriptedVad:
    """Returns the scripted probabilities in order, then the last one forever."""

    def __init__(self, script: list[float]) -> None:
        self.script = list(script)

    def probability(self, window: np.ndarray, session: Any) -> float:
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]


# -- dBFS ----------------------------------------------------------------------


def test_dbfs_is_finite_for_digital_silence() -> None:
    assert to_dbfs(0.0) == diagnostics.DBFS_FLOOR
    assert to_dbfs(1.0) == 0.0
    assert to_dbfs(0.1) == -20.0


# -- heartbeat -----------------------------------------------------------------


def test_heartbeat_reports_level_frames_gap_and_vad_from_the_segmenter() -> None:
    clock = Clock()
    with records() as collected:
        diag = make(clock)
        # 50 windows of 0.9 then 106 of 0.1: 156 windows in 5 s of audio.
        vad = ScriptedVad([0.9] * 50 + [0.1])
        segmenter = ContinuousSegmenter(vad, on_window=diag.on_vad_window)  # type: ignore[arg-type]
        pcm = sine(-20.0, FRAME)
        for _ in range(50):
            clock.now += 0.1
            diag.on_frame(pcm)
            segmenter.push(pcm)
        fields = diag.heartbeat(seg_state=segmenter.state, segment_open=False, pending_segments=0)

    assert named(collected, "stream.heartbeat") == [
        {"session_id": "session-1234", **fields}
    ]
    assert fields["frames"] == 50
    assert fields["samples"] == 50 * FRAME
    assert fields["window_ms"] == 5_000
    assert fields["max_gap_ms"] == 100
    assert fields["rms_dbfs"] == pytest.approx(-20.0, abs=0.1)
    assert fields["peak_dbfs"] == pytest.approx(-17.0, abs=0.1)
    windows = (50 * FRAME) // 512
    assert fields["vad_windows"] == windows
    assert fields["vad_max"] == 0.9
    assert fields["vad_mean"] == round((50 * 0.9 + (windows - 50) * 0.1) / windows, 3)
    assert fields["vad_speech_frac"] == round(50 / windows, 3)
    assert fields["seg_state"] == "silence"


def test_heartbeat_window_resets_and_an_open_receive_gap_is_counted() -> None:
    clock = Clock()
    diag = make(clock)
    feed(diag, clock, sine(-30.0, FRAME), 1.0)
    diag.heartbeat()
    clock.now += 4.0  # client went quiet on the wire
    fields = diag.heartbeat()
    assert fields["frames"] == 0
    assert fields["samples"] == 0
    assert fields["rms_dbfs"] is None
    assert fields["vad_max"] is None
    assert fields["max_gap_ms"] == 4_000


def test_heartbeat_includes_latest_singing_score() -> None:
    diag = SessionDiagnostics("session-1234")
    diag.singing_score = 0.87654
    assert diag.heartbeat()["singing_score"] == 0.877


def test_audio_class_diagnostic_logs_counts_only() -> None:
    with records() as collected:
        diag = SessionDiagnostics("session-1234")
        diag.audio_class(speech=4, singing=2)
    assert named(collected, "stream.audio_class") == [
        {"session_id": "session-1234", "speech": 4, "singing": 2}
    ]


def test_heartbeat_distinguishes_silence_from_quiet_audio() -> None:
    clock = Clock()
    diag = make(clock)
    feed(diag, clock, b"\0\0" * FRAME, 1.0)
    silent = diag.heartbeat()
    feed(diag, clock, sine(-65.0, FRAME), 1.0)
    quiet = diag.heartbeat()
    assert silent["rms_dbfs"] == diagnostics.DBFS_FLOOR
    assert silent["peak_dbfs"] == diagnostics.DBFS_FLOOR
    assert quiet["rms_dbfs"] == pytest.approx(-65.0, abs=0.2)


def test_tick_emits_a_heartbeat_every_interval() -> None:
    clock = Clock()
    with records() as collected:
        diag = make(clock)
        for _ in range(12):
            feed(diag, clock, sine(-30.0, FRAME), 1.0)
            diag.tick(seg_state="silence")
    beats = named(collected, "stream.heartbeat")
    assert len(beats) == 2
    assert all(beat["seg_state"] == "silence" for beat in beats)
    assert [beat["frames"] for beat in beats] == [50, 50]


def test_heartbeat_reports_worker_busy_fraction() -> None:
    clock = Clock()
    busy = {"s": 0.0}
    diag = make(clock, busy_seconds=lambda: busy["s"])
    clock.now += 5.0
    busy["s"] = 2.0
    assert diag.heartbeat()["worker_busy"] == 0.4


def test_heartbeat_counts_previews() -> None:
    clock = Clock()
    with records() as collected:
        diag = make(clock)
        diag.preview_done(outcome="published", segment_index=0, decode_ms=120, audio_ms=900)
        diag.preview_done(outcome="stale", segment_index=0, decode_ms=80, audio_ms=1200)
        diag.preview_done(outcome="dropped", segment_index=0, audio_ms=1500)
        diag.preview_deferred(segment_index=1, wait_ms=150, gap_ms=400)
        clock.now += 5.0
        fields = diag.heartbeat()
    assert fields["preview_run"] == 2
    assert fields["preview_published"] == 1
    assert fields["preview_dropped"] == 2
    assert fields["preview_deferred"] == 1
    assert fields["preview_decode_ms"] == 200
    per_preview = [
        record for record in collected if record.getMessage() == "stream.preview"
    ]
    assert len(per_preview) == 4
    assert all(record.levelno == logging.DEBUG for record in per_preview)


def test_load_guard_deferral_is_counted_by_the_session() -> None:
    class SlowScheduler:
        async def transcribe(self, pcm: bytes, **_: Any) -> tuple[dict[str, Any], int]:
            await asyncio.sleep(0.1)
            return {"text": "測試", "total_time_s": 0.1}, 0

    class Socket:
        async def send_json(self, payload: dict[str, Any]) -> None:
            return None

    async def scenario() -> dict[str, Any]:
        session = StreamSession(
            Socket(),  # type: ignore[arg-type]
            SlowScheduler(),
            config=ServiceConfig(
                preview_min_audio_ms=100, preview_min_interval_ms=100, preview_load_factor=3.0
            ),
            model_state="ready",
        )
        session._transcript_mode = "revisable"
        session._open_segment(0, cause="utterance")
        state = session._state
        for _ in range(8):
            state.pcm.extend(b"\1\0" * FRAME)
            state.next_sample += FRAME
            session._maybe_schedule_preview()
            await asyncio.sleep(0.12)
        await session._settle_preview()
        return session._diag.heartbeat()

    fields = asyncio.run(scenario())
    # 0.1 s decodes x 3 = 0.3 s gap > the 0.1 s floor: the load guard held some.
    assert fields["preview_deferred"] >= 1
    assert fields["preview_published"] >= 2


# -- warnings ------------------------------------------------------------------


def test_stalled_audio_warns_once_per_interval() -> None:
    clock = Clock()
    with records() as collected:
        diag = make(clock)
        feed(diag, clock, sine(-30.0, FRAME), 1.0)
        clock.now += 2.9
        diag.tick()
        assert named(collected, "stream.audio_stalled") == []
        clock.now += 0.2
        diag.tick()
        for _ in range(20):
            clock.now += 1.0
            diag.tick()
        assert len(named(collected, "stream.audio_stalled")) == 1
        clock.now += 10.5
        diag.tick()
    warnings = named(collected, "stream.audio_stalled")
    assert len(warnings) == 2
    assert warnings[0]["gap_ms"] == 3_100
    assert warnings[0]["suppressed"] == 0
    assert warnings[1]["suppressed"] == 20
    assert all(
        record.levelno == logging.WARNING
        for record in collected
        if record.getMessage() == "stream.audio_stalled"
    )


def test_no_stall_warning_once_the_client_stopped_sending() -> None:
    clock = Clock()
    with records() as collected:
        diag = make(clock)
        diag.receiving = False
        clock.now += 10.0
        diag.tick()
    assert named(collected, "stream.audio_stalled") == []


def test_quiet_audio_warns_after_ten_seconds_and_is_rate_limited() -> None:
    clock = Clock()
    quiet = sine(-65.0, FRAME)
    with records() as collected:
        diag = make(clock)
        feed(diag, clock, quiet, 9.9)
        assert named(collected, "stream.audio_quiet") == []
        feed(diag, clock, quiet, 0.1)
        assert len(named(collected, "stream.audio_quiet")) == 1
        # Re-detected after every further 10 s of quiet audio; the ones at 20 s
        # and 30 s fall inside the 30 s rate limit, the one at 40 s does not.
        feed(diag, clock, quiet, 29.9)
        assert len(named(collected, "stream.audio_quiet")) == 1
        feed(diag, clock, quiet, 0.1)
    warnings = named(collected, "stream.audio_quiet")
    assert [w["suppressed"] for w in warnings] == [0, 2]
    assert warnings[0]["quiet_s"] == 10.0
    assert warnings[0]["rms_dbfs"] == pytest.approx(-65.0, abs=0.2)
    assert warnings[0]["peak_dbfs"] == pytest.approx(-62.0, abs=0.2)


def test_quiet_is_judged_on_the_window_rms_not_every_frame() -> None:
    """Speech at -65 dBFS overall still has syllables above -60 dBFS."""

    clock = Clock()
    with records() as collected:
        diag = make(clock)
        for _ in range(10):
            feed(diag, clock, sine(-58.0, FRAME), 0.1)
            feed(diag, clock, sine(-80.0, FRAME), 0.9)
    (warning,) = named(collected, "stream.audio_quiet")
    assert warning["rms_dbfs"] < -60.0


def test_quiet_run_is_reset_by_audible_audio() -> None:
    clock = Clock()
    with records() as collected:
        diag = make(clock)
        feed(diag, clock, sine(-65.0, FRAME), 9.0)
        feed(diag, clock, sine(-30.0, FRAME), 0.1)
        feed(diag, clock, sine(-65.0, FRAME), 9.0)
    assert named(collected, "stream.audio_quiet") == []


def _vad_windows(diag: SessionDiagnostics, clock: Clock, level: float, prob: float, s: float) -> None:
    window = np.frombuffer(sine(level, 512), dtype="<i2").astype(np.float32) / 32768.0
    for _ in range(round(s * RATE / 512)):
        clock.now += 512 / RATE
        diag.on_vad_window(window, prob)


def test_loud_audio_without_speech_warns_and_is_rate_limited() -> None:
    clock = Clock()
    with records() as collected:
        diag = make(clock)
        _vad_windows(diag, clock, -20.0, 0.1, 9.9)
        assert named(collected, "stream.vad_no_speech") == []
        _vad_windows(diag, clock, -20.0, 0.2, 0.2)
        assert len(named(collected, "stream.vad_no_speech")) == 1
        # Re-detected every further 10 s; 20 s and 30 s are rate-limited.
        _vad_windows(diag, clock, -20.0, 0.1, 25.0)
        assert len(named(collected, "stream.vad_no_speech")) == 1
        _vad_windows(diag, clock, -20.0, 0.1, 5.5)
    warnings = named(collected, "stream.vad_no_speech")
    assert [w["suppressed"] for w in warnings] == [0, 2]
    assert warnings[0]["vad_max"] == 0.2
    assert warnings[0]["threshold"] == 0.5


def test_one_speech_window_resets_the_no_speech_run() -> None:
    clock = Clock()
    with records() as collected:
        diag = make(clock)
        _vad_windows(diag, clock, -20.0, 0.1, 9.0)
        _vad_windows(diag, clock, -20.0, 0.9, 0.04)
        _vad_windows(diag, clock, -20.0, 0.1, 9.0)
    assert named(collected, "stream.vad_no_speech") == []


def test_quiet_audio_never_counts_as_vad_deafness() -> None:
    clock = Clock()
    with records() as collected:
        diag = make(clock)
        _vad_windows(diag, clock, -50.0, 0.0, 30.0)
    assert named(collected, "stream.vad_no_speech") == []


def test_slow_queue_wait_warns_per_kind_and_is_rate_limited() -> None:
    clock = Clock()
    with records() as collected:
        diag = make(clock)
        diag.queue_wait("final", 1_500, segment_index=0)
        diag.queue_wait("final", 2_500, segment_index=1)
        diag.queue_wait("final", 3_000, segment_index=2)
        diag.queue_wait("preview", 2_100, segment_index=2)
        clock.now += 31.0
        diag.queue_wait("final", 2_200, segment_index=3)
    warnings = named(collected, "stream.queue_wait_slow")
    assert [(w["kind"], w["segment_index"], w["suppressed"]) for w in warnings] == [
        ("final", 1, 0),
        ("preview", 2, 0),
        ("final", 3, 1),
    ]


def test_session_end_carries_totals_and_warning_counts() -> None:
    clock = Clock()
    with records() as collected:
        diag = make(clock)
        feed(diag, clock, sine(-30.0, FRAME), 2.0)
        diag.speech_started(segment_index=0, start_sample=0, cause="vad")
        diag.segment_closed(segment_index=0, start_sample=0, end_sample=16_000, boundary="silence")
        diag.segment_done(segment_index=0, outcome="final", latency_ms=300, queue_ms=5, chars=4)
        clock.now += 5.0
        diag.tick()
        diag.session_ended(reason="stopped")
    (ended,) = named(collected, "stream.session_ended")
    assert ended["reason"] == "stopped"
    assert ended["frames"] == 20
    assert ended["audio_s"] == 2.0
    assert ended["finals"] == 1
    assert ended["boundaries"] == {"silence": 1}
    assert ended["warnings"] == 1  # the stall
    assert ended["duration_s"] == 7.0


def test_session_that_never_started_is_logged_as_rejected() -> None:
    with records() as collected:
        diag = SessionDiagnostics("s")
        diag.session_ended(reason="error:unsupported_option")
    assert named(collected, "stream.session_rejected") == [
        {"session_id": "s", "reason": "error:unsupported_option"}
    ]


def test_no_transcript_text_is_ever_a_field() -> None:
    clock = Clock()
    with records() as collected:
        diag = make(clock)
        diag.segment_done(
            segment_index=0, outcome="final", latency_ms=1, chars=3, stable_state="final"
        )
    for record in collected:
        assert not {"text", "raw_text", "transcript", "pcm"} & set(record.fields)  # type: ignore[attr-defined]


# -- debug audio capture -------------------------------------------------------


def _write_seconds(capture: AudioCapture, seconds: int, start: int = 0) -> int:
    sample = start
    for _ in range(seconds * 10):
        pcm = np.full(FRAME, sample // RATE, dtype="<i2").tobytes()
        capture.write(sample, pcm)
        sample += FRAME
    return sample


def test_capture_keeps_only_the_most_recent_minutes(tmp_path: Path) -> None:
    capture = AudioCapture(tmp_path, "abcdef123456", minutes=1, chunk_s=5)
    _write_seconds(capture, 80)
    capture.close()
    files = sorted(capture.directory.glob("*.wav"))
    # 60 s in 5 s chunks is 12 files, plus one so a full minute always
    # survives the rollover: seconds 15..80.
    assert len(files) == 13
    assert files[0].name == f"chunk-{15 * RATE:012d}.wav"
    total = sum(path.stat().st_size for path in files)
    assert total <= capture.max_bytes + 44 * len(files)
    with wave.open(str(files[-1])) as stream:
        assert (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) == (
            RATE,
            1,
            2,
        )
        assert stream.getnframes() == 5 * RATE
        first = np.frombuffer(stream.readframes(1), dtype="<i2")[0]
    assert first == 75  # the value written at second 75


def test_capture_keeps_a_bounded_number_of_sessions(tmp_path: Path) -> None:
    for index in range(7):
        capture = AudioCapture(tmp_path, f"{index:02d}abcdefgh", minutes=1, max_sessions=3)
        _write_seconds(capture, 1)
        capture.close()
    folders = sorted(path.name for path in tmp_path.iterdir())
    assert [name.rsplit("-", 1)[1] for name in folders] == ["04abcdef", "05abcdef", "06abcdef"]


def test_capture_drops_instead_of_queueing_without_bound(tmp_path: Path) -> None:
    capture = AudioCapture(tmp_path, "abcdef123456", minutes=1)
    # Swap in a full-size-1 queue nobody drains, as if the disk had stalled.
    drained = capture._queue
    capture._queue = queue.Queue(maxsize=1)
    for index in range(5):
        capture.write(index * FRAME, b"\0\0" * FRAME)
    assert capture.dropped_frames == 4
    drained.put(diagnostics._STOP)
    capture._thread.join(2)
    assert not capture._thread.is_alive()


def test_capture_is_off_by_default_and_bounded() -> None:
    assert ServiceConfig().debug_capture_audio is False
    env = ServiceConfig.from_env(
        {"TEA_ASR_DEBUG_CAPTURE_AUDIO": "1", "TEA_ASR_DEBUG_CAPTURE_MINUTES": "3"}
    )
    assert (env.debug_capture_audio, env.debug_capture_minutes) == (True, 3)
    with pytest.raises(RuntimeError):
        validate_debug_capture_or_raise(ServiceConfig(debug_capture_minutes=0))
    with pytest.raises(RuntimeError):
        validate_debug_capture_or_raise(ServiceConfig(debug_capture_minutes=61))
