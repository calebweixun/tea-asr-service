"""Per-session diagnostics for `/v1/stream`.

Built for one question a live-subtitle user cannot answer from the client:
"there were no captions for a while. Did audio stop arriving, was it too quiet,
or did the VAD decide it was not speech?" Everything here goes to the regular
JSON-lines log through `tea_asr.logs.event()`, so `GET /v1/logs` and the Mac
log page show it without any extra plumbing.

What is logged, and at which level, is documented in docs/04-api.md
「W11｜串流診斷日誌」. In short:

- lifecycle lines (INFO): session start/end, speech started, segment closed,
  segment done;
- one heartbeat line per active session every `HEARTBEAT_INTERVAL_S` (INFO):
  frames and receive gap, RMS/peak in dBFS, VAD max/mean/speech fraction,
  segmenter state, preview accounting and worker busy fraction;
- one DEBUG line per preview;
- WARNING lines for stalled audio, very quiet audio, loud audio the VAD does
  not score as speech, and slow queue waits, each rate-limited per session.

Nothing here runs a model: the VAD statistics reuse the probabilities the
segmenter already computes. Transcript text is never logged, only character
counts (docs/06-handoff.md P3: the log filters tokens, PCM and transcripts).

`AudioCapture` is the optional, off-by-default rolling WAV recorder
(`ServiceConfig.debug_capture_audio`).
"""

from __future__ import annotations

import contextlib
import logging
import math
import queue
import shutil
import threading
import time
import wave
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .logs import event as log_event

SAMPLE_RATE = 16_000

#: One heartbeat line per active session this often. 5 s keeps INFO volume at
#: roughly 0.4 MB/hour per session (docs/04「W11」has the arithmetic).
HEARTBEAT_INTERVAL_S = 5.0
#: How often the wall-clock checks (stalled audio, heartbeat due) run.
TICK_S = 1.0
#: Each warning kind is written at most once per this interval per session;
#: repeats in between are counted and reported as `suppressed`.
WARN_INTERVAL_S = 30.0

#: No binary frame for longer than this while the session is active.
AUDIO_STALL_WARN_S = 3.0
#: Frames keep arriving but the RMS of the most recent `QUIET_WARN_S` of them
#: is below this level. RMS over the whole window, not "every frame quiet":
#: speech attenuated to -65 dBFS still has syllables above -60 dBFS.
QUIET_DBFS = -60.0
QUIET_WARN_S = 10.0
#: The sliding window advances in blocks of this much audio.
QUIET_BLOCK_S = 1.0
#: Audio at or above this level counts as "something is playing".
LOUD_DBFS = -40.0
#: This much loud audio with no VAD window reaching the speech threshold.
VAD_DEAF_WARN_S = 10.0
#: A stretch of quieter audio this long ends a "loud but not speech" run, so
#: two loud bursts an hour apart are not added together.
VAD_DEAF_RESET_QUIET_S = 2.0
#: A preview or final waiting longer than this for the single worker.
QUEUE_WAIT_WARN_MS = 2_000

#: dBFS reported for digital silence (log10(0) has no value and JSON has no
#: -Infinity that every client can decode).
DBFS_FLOOR = -120.0

logger = logging.getLogger("tea_asr.stream")


def to_dbfs(amplitude: float) -> float:
    """`amplitude` is linear full scale (1.0 = int16 full scale)."""

    if amplitude <= 0.0 or not math.isfinite(amplitude):
        return DBFS_FLOOR
    return max(DBFS_FLOOR, round(20.0 * math.log10(amplitude), 1))


class WarningLimiter:
    """At most one emission per key per `interval_s`; counts the rest."""

    def __init__(self, interval_s: float = WARN_INTERVAL_S) -> None:
        self.interval_s = interval_s
        self._last: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}
        self.emitted = 0
        self.suppressed = 0

    def allow(self, key: str, now: float) -> int | None:
        """Suppressed count since the last emission, or None if rate-limited."""

        last = self._last.get(key)
        if last is not None and now - last < self.interval_s:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            self.suppressed += 1
            return None
        self._last[key] = now
        self.emitted += 1
        return self._suppressed.pop(key, 0)


@dataclass(slots=True)
class _Window:
    """Counters for one heartbeat interval; replaced after every heartbeat."""

    started_at: float
    frames: int = 0
    samples: int = 0
    sum_squares: float = 0.0
    peak: float = 0.0
    max_gap_s: float = 0.0
    vad_windows: int = 0
    vad_sum: float = 0.0
    vad_max: float = 0.0
    vad_speech: int = 0
    preview_run: int = 0
    preview_published: int = 0
    preview_dropped: int = 0
    preview_deferred: int = 0
    preview_failed: int = 0
    preview_decode_ms: int = 0


@dataclass(slots=True)
class _Totals:
    frames: int = 0
    samples: int = 0
    speech_started: int = 0
    segments_closed: int = 0
    finals: int = 0
    skipped: int = 0
    failed: int = 0
    preview_run: int = 0
    preview_published: int = 0
    preview_dropped: int = 0
    preview_deferred: int = 0
    preview_failed: int = 0
    boundaries: dict[str, int] = field(default_factory=dict)


class SessionDiagnostics:
    """Collects one session's audio/VAD/preview statistics and writes the log.

    Every entry point takes an optional `now` so tests can drive the wall
    clock; production passes nothing and gets `time.monotonic()`.
    """

    def __init__(
        self,
        session_id: str,
        *,
        vad_threshold: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        busy_seconds: Callable[[], float] | None = None,
        log: logging.Logger = logger,
    ) -> None:
        self.session_id = session_id
        self._vad_threshold = vad_threshold
        self._clock = clock
        self._busy_seconds = busy_seconds
        self._log = log
        self._limiter = WarningLimiter()
        now = clock()
        self._started_at = now
        self._window = _Window(started_at=now)
        self._totals = _Totals()
        self._last_frame_at: float | None = None
        self._last_heartbeat_at = now
        self._busy_at_heartbeat = busy_seconds() if busy_seconds is not None else 0.0
        #: The quiet and no-speech trackers run on the audio clock (samples),
        #: so they are exact however the frames happen to be sized.
        #: (samples, sum of squares, peak) per closed 1 s block, newest last.
        self._quiet_blocks: deque[tuple[int, float, float]] = deque(
            maxlen=round(QUIET_WARN_S / QUIET_BLOCK_S)
        )
        self._block_samples = 0
        self._block_sum_squares = 0.0
        self._block_peak = 0.0
        self._loud_nonspeech_samples = 0
        self._loud_nonspeech_vad_max = 0.0
        self._soft_run_samples = 0
        #: False once the client said stop/cancel or the session failed: no
        #: more frames are expected, so silence is not a stall.
        self.receiving = True
        self.started = False
        self.capture_dropped_frames = 0

    # -- helpers ---------------------------------------------------------------

    def _now(self, now: float | None) -> float:
        return self._clock() if now is None else now

    def emit(self, message: str, *, level: str = "info", **fields: Any) -> None:
        log_event(self._log, message, level=level, session_id=self.session_id, **fields)

    def _warn(self, key: str, message: str, now: float, **fields: Any) -> bool:
        suppressed = self._limiter.allow(key, now)
        if suppressed is None:
            return False
        self.emit(message, level="warning", suppressed=suppressed, **fields)
        return True

    # -- lifecycle -------------------------------------------------------------

    def session_started(self, **fields: Any) -> None:
        self.started = True
        now = self._clock()
        self._started_at = now
        self._last_heartbeat_at = now
        self._window = _Window(started_at=now)
        self.emit("stream.session_started", **fields)

    def speech_started(self, *, segment_index: int, start_sample: int, cause: str) -> None:
        self._totals.speech_started += 1
        self.emit(
            "stream.speech_started",
            segment_index=segment_index,
            start_sample=start_sample,
            cause=cause,
        )

    def segment_closed(
        self, *, segment_index: int, start_sample: int, end_sample: int, boundary: str
    ) -> None:
        totals = self._totals
        totals.segments_closed += 1
        totals.boundaries[boundary] = totals.boundaries.get(boundary, 0) + 1
        self.emit(
            "stream.segment_closed",
            segment_index=segment_index,
            boundary=boundary,
            start_sample=start_sample,
            end_sample=end_sample,
            audio_ms=(end_sample - start_sample) // 16,
        )

    def segment_done(
        self,
        *,
        segment_index: int,
        outcome: str,
        latency_ms: int | None,
        queue_ms: int | None = None,
        inference_ms: int | None = None,
        chars: int | None = None,
        stable_state: str | None = None,
        code: str | None = None,
    ) -> None:
        """`outcome`: final / no_speech / empty / error."""

        totals = self._totals
        if outcome == "final":
            totals.finals += 1
        elif outcome == "error":
            totals.failed += 1
        else:
            totals.skipped += 1
        self.emit(
            "stream.segment_done",
            segment_index=segment_index,
            outcome=outcome,
            latency_ms=latency_ms,
            queue_ms=queue_ms,
            inference_ms=inference_ms,
            chars=chars,
            stable_state=stable_state,
            code=code,
        )
        if queue_ms is not None:
            self.queue_wait("final", queue_ms, segment_index=segment_index)

    def session_ended(self, *, reason: str, now: float | None = None, **fields: Any) -> None:
        moment = self._now(now)
        totals = self._totals
        if not self.started:
            self.emit("stream.session_rejected", reason=reason, **fields)
            return
        self.emit(
            "stream.session_ended",
            reason=reason,
            duration_s=round(moment - self._started_at, 1),
            frames=totals.frames,
            audio_s=round(totals.samples / SAMPLE_RATE, 1),
            speech_started=totals.speech_started,
            segments_closed=totals.segments_closed,
            boundaries=dict(totals.boundaries),
            finals=totals.finals,
            skipped=totals.skipped,
            failed=totals.failed,
            preview_run=totals.preview_run,
            preview_published=totals.preview_published,
            preview_dropped=totals.preview_dropped,
            preview_deferred=totals.preview_deferred,
            preview_failed=totals.preview_failed,
            warnings=self._limiter.emitted,
            warnings_suppressed=self._limiter.suppressed,
            capture_dropped_frames=self.capture_dropped_frames,
            **fields,
        )

    # -- audio -----------------------------------------------------------------

    def on_frame(self, pcm: bytes, *, now: float | None = None) -> None:
        moment = self._now(now)
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0
        count = int(samples.size)
        sum_squares = float(np.dot(samples, samples))
        peak = float(np.abs(samples).max()) if count else 0.0

        window = self._window
        reference = self._last_frame_at if self._last_frame_at is not None else self._started_at
        window.max_gap_s = max(window.max_gap_s, moment - reference)
        self._last_frame_at = moment
        window.frames += 1
        window.samples += count
        window.sum_squares += sum_squares
        window.peak = max(window.peak, peak)
        self._totals.frames += 1
        self._totals.samples += count

        self._block_samples += count
        self._block_sum_squares += sum_squares
        self._block_peak = max(self._block_peak, peak)
        if self._block_samples >= QUIET_BLOCK_S * SAMPLE_RATE:
            self._close_quiet_block(moment)

    def _close_quiet_block(self, moment: float) -> None:
        blocks = self._quiet_blocks
        blocks.append((self._block_samples, self._block_sum_squares, self._block_peak))
        self._block_samples = 0
        self._block_sum_squares = 0.0
        self._block_peak = 0.0
        if len(blocks) < (blocks.maxlen or 0):
            return
        samples = sum(block[0] for block in blocks)
        rms = math.sqrt(sum(block[1] for block in blocks) / samples)
        if to_dbfs(rms) >= QUIET_DBFS:
            return
        self._warn(
            "audio_quiet",
            "stream.audio_quiet",
            moment,
            quiet_s=round(samples / SAMPLE_RATE, 1),
            rms_dbfs=to_dbfs(rms),
            peak_dbfs=to_dbfs(max(block[2] for block in blocks)),
            threshold_dbfs=QUIET_DBFS,
        )
        # The next detection needs another full window of quiet audio.
        blocks.clear()

    def on_vad_window(
        self, window: np.ndarray, probability: float, *, now: float | None = None
    ) -> None:
        """One 512-sample window the segmenter just scored."""

        stats = self._window
        stats.vad_windows += 1
        stats.vad_sum += probability
        stats.vad_max = max(stats.vad_max, probability)
        speech = probability >= self._vad_threshold
        if speech:
            stats.vad_speech += 1
            self._loud_nonspeech_samples = 0
            self._loud_nonspeech_vad_max = 0.0
            self._soft_run_samples = 0
            return
        size = int(window.size)
        rms = math.sqrt(float(np.dot(window, window)) / size) if size else 0.0
        if to_dbfs(rms) >= LOUD_DBFS:
            self._soft_run_samples = 0
            self._loud_nonspeech_samples += size
            self._loud_nonspeech_vad_max = max(self._loud_nonspeech_vad_max, probability)
            if self._loud_nonspeech_samples >= VAD_DEAF_WARN_S * SAMPLE_RATE:
                self._warn(
                    "vad_no_speech",
                    "stream.vad_no_speech",
                    self._now(now),
                    loud_s=round(self._loud_nonspeech_samples / SAMPLE_RATE, 1),
                    vad_max=round(self._loud_nonspeech_vad_max, 3),
                    threshold=self._vad_threshold,
                    loud_dbfs=LOUD_DBFS,
                )
                # The next detection needs another 10 s of it.
                self._loud_nonspeech_samples = 0
                self._loud_nonspeech_vad_max = 0.0
        else:
            self._soft_run_samples += size
            if self._soft_run_samples >= VAD_DEAF_RESET_QUIET_S * SAMPLE_RATE:
                self._loud_nonspeech_samples = 0
                self._loud_nonspeech_vad_max = 0.0

    # -- previews and queue ----------------------------------------------------

    def preview_done(
        self,
        *,
        outcome: str,
        segment_index: int,
        decode_ms: int | None = None,
        audio_ms: int | None = None,
        queue_ms: int | None = None,
    ) -> None:
        """`outcome`: published / stale / dropped / failed."""

        window = self._window
        totals = self._totals
        if outcome in {"published", "stale"}:
            window.preview_run += 1
            totals.preview_run += 1
            window.preview_decode_ms += decode_ms or 0
        if outcome == "published":
            window.preview_published += 1
            totals.preview_published += 1
        elif outcome in {"stale", "dropped"}:
            window.preview_dropped += 1
            totals.preview_dropped += 1
        elif outcome == "failed":
            window.preview_failed += 1
            totals.preview_failed += 1
        self.emit(
            "stream.preview",
            level="debug",
            segment_index=segment_index,
            outcome=outcome,
            decode_ms=decode_ms,
            audio_ms=audio_ms,
            queue_ms=queue_ms,
        )
        if queue_ms is not None:
            self.queue_wait("preview", queue_ms, segment_index=segment_index)

    def preview_deferred(self, *, segment_index: int, wait_ms: int, gap_ms: int) -> None:
        """The load guard (not the fixed interval) postponed a preview."""

        self._window.preview_deferred += 1
        self._totals.preview_deferred += 1
        self.emit(
            "stream.preview",
            level="debug",
            segment_index=segment_index,
            outcome="deferred",
            wait_ms=wait_ms,
            gap_ms=gap_ms,
        )

    def queue_wait(
        self, kind: str, queue_ms: int, *, segment_index: int, now: float | None = None
    ) -> None:
        if queue_ms <= QUEUE_WAIT_WARN_MS:
            return
        self._warn(
            f"queue_wait:{kind}",
            "stream.queue_wait_slow",
            self._now(now),
            kind=kind,
            queue_ms=queue_ms,
            segment_index=segment_index,
            threshold_ms=QUEUE_WAIT_WARN_MS,
        )

    # -- wall clock ------------------------------------------------------------

    def tick(self, *, now: float | None = None, **state: Any) -> None:
        """Wall-clock checks; call about once per `TICK_S`.

        `state` is the session's current segmenter snapshot (seg_state,
        segment_open, pending_segments) for the heartbeat line.
        """

        moment = self._now(now)
        if self.receiving:
            reference = self._last_frame_at if self._last_frame_at is not None else self._started_at
            gap = moment - reference
            if gap > AUDIO_STALL_WARN_S:
                self._warn(
                    "audio_stalled",
                    "stream.audio_stalled",
                    moment,
                    gap_ms=round(gap * 1000),
                    frames_total=self._totals.frames,
                    threshold_ms=round(AUDIO_STALL_WARN_S * 1000),
                )
        if moment - self._last_heartbeat_at >= HEARTBEAT_INTERVAL_S:
            self.heartbeat(now=moment, **state)

    def heartbeat(self, *, now: float | None = None, **state: Any) -> dict[str, Any]:
        moment = self._now(now)
        window = self._window
        elapsed = max(moment - window.started_at, 1e-9)
        # A gap still open at heartbeat time counts too, otherwise a client
        # that stopped sending shows max_gap_ms=0 in every silent window.
        reference = self._last_frame_at if self._last_frame_at is not None else self._started_at
        max_gap_s = max(window.max_gap_s, moment - reference) if self.receiving else window.max_gap_s
        rms = math.sqrt(window.sum_squares / window.samples) if window.samples else None
        worker_busy = None
        if self._busy_seconds is not None:
            busy_now = self._busy_seconds()
            worker_busy = round(min(1.0, max(0.0, (busy_now - self._busy_at_heartbeat) / elapsed)), 3)
            self._busy_at_heartbeat = busy_now
        fields: dict[str, Any] = {
            "window_ms": round(elapsed * 1000),
            "frames": window.frames,
            "samples": window.samples,
            "max_gap_ms": round(max_gap_s * 1000),
            "rms_dbfs": to_dbfs(rms) if rms is not None else None,
            "peak_dbfs": to_dbfs(window.peak) if window.samples else None,
            "vad_windows": window.vad_windows,
            "vad_max": round(window.vad_max, 3) if window.vad_windows else None,
            "vad_mean": (
                round(window.vad_sum / window.vad_windows, 3) if window.vad_windows else None
            ),
            "vad_speech_frac": (
                round(window.vad_speech / window.vad_windows, 3) if window.vad_windows else None
            ),
            **state,
            "preview_run": window.preview_run,
            "preview_published": window.preview_published,
            "preview_dropped": window.preview_dropped,
            "preview_deferred": window.preview_deferred,
            "preview_failed": window.preview_failed,
            "preview_decode_ms": window.preview_decode_ms,
            "worker_busy": worker_busy,
        }
        self.emit("stream.heartbeat", **fields)
        self._window = _Window(started_at=moment)
        self._last_heartbeat_at = moment
        return fields


# --- optional debug audio capture -------------------------------------------

#: Each rolling file covers this much received audio.
CAPTURE_CHUNK_S = 60
#: Captures of at most this many sessions are kept; older session directories
#: are deleted when a new one starts. With the default 10 minutes (11 files of
#: 1.92 MB each) this bounds the capture folder at about 5 × 21 MB ≈ 106 MB.
CAPTURE_MAX_SESSIONS = 5
#: Frames waiting for the writer thread. At 100 ms frames this is two minutes;
#: when the disk cannot keep up, frames are dropped and counted, never queued
#: without bound (docs/06 #4).
CAPTURE_QUEUE_FRAMES = 1_200

_STOP = object()


class AudioCapture:
    """Keeps the most recent `minutes` of one session's received PCM as WAVs.

    Files are `chunk-<start_sample>.wav` (16 kHz mono s16le, each at most
    `CAPTURE_CHUNK_S` long, named by the session sample where they start)
    under `root/<timestamp>-<session>/`. One file more than `minutes` needs is
    kept, so at least the last `minutes` of audio are always on disk; the
    oldest file is deleted when a new one opens, and only the newest
    `CAPTURE_MAX_SESSIONS` session folders survive. Disk writes run on
    a dedicated thread, never on the event loop (docs/06 #3).

    This records whatever the client streams. It is off by default
    (`ServiceConfig.debug_capture_audio`); docs/06 #7 says ephemeral sessions
    do not persist audio unless the operator explicitly turns this on.
    """

    def __init__(
        self,
        root: Path,
        session_id: str,
        *,
        minutes: int,
        chunk_s: int = CAPTURE_CHUNK_S,
        max_sessions: int = CAPTURE_MAX_SESSIONS,
        queue_frames: int = CAPTURE_QUEUE_FRAMES,
    ) -> None:
        if minutes < 1:
            raise ValueError("minutes must be at least 1")
        self._chunk_samples = chunk_s * SAMPLE_RATE
        self._max_chunks = math.ceil(minutes * 60 / chunk_s) + 1
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _prune_sessions(root, keep=max_sessions - 1)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.directory = root / f"{stamp}-{session_id[:8]}"
        self.directory.mkdir(mode=0o700)
        self.dropped_frames = 0
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=queue_frames)
        self._thread = threading.Thread(
            target=self._run, name=f"tea-asr-capture-{session_id[:8]}", daemon=True
        )
        self._thread.start()

    @property
    def max_bytes(self) -> int:
        """Upper bound on this session's WAV data on disk."""

        return self._max_chunks * self._chunk_samples * 2

    def write(self, start_sample: int, pcm: bytes) -> None:
        """Non-blocking; drops (and counts) the frame if the writer lags."""

        try:
            self._queue.put_nowait((start_sample, pcm))
        except queue.Full:
            self.dropped_frames += 1

    def close(self, timeout: float = 5.0) -> None:
        """Blocking: flush and stop the writer. Call via `asyncio.to_thread`."""

        with contextlib.suppress(queue.Full):
            self._queue.put(_STOP, timeout=timeout)
        self._thread.join(timeout)

    # -- writer thread ---------------------------------------------------------

    def _run(self) -> None:
        current: wave.Wave_write | None = None
        chunk_end = 0
        chunks: list[Path] = []
        try:
            while True:
                item = self._queue.get()
                if item is _STOP:
                    return
                start_sample, pcm = item
                offset = 0
                while offset < len(pcm):
                    sample = start_sample + offset // 2
                    if current is None or sample >= chunk_end:
                        if current is not None:
                            current.close()
                        chunk_start = sample - sample % self._chunk_samples
                        chunk_end = chunk_start + self._chunk_samples
                        path = self.directory / f"chunk-{chunk_start:012d}.wav"
                        current = wave.open(str(path), "wb")  # noqa: SIM115 - spans frames
                        current.setnchannels(1)
                        current.setsampwidth(2)
                        current.setframerate(SAMPLE_RATE)
                        chunks.append(path)
                        while len(chunks) > self._max_chunks:
                            chunks.pop(0).unlink(missing_ok=True)
                    take = min(len(pcm) - offset, (chunk_end - sample) * 2)
                    # writeframes patches the header each call, so the file on
                    # disk is a valid WAV at every moment, even after a crash.
                    current.writeframes(pcm[offset : offset + take])
                    offset += take
        except OSError:
            # A full or read-only disk must not take the session down; the
            # capture just stops. Whatever is on disk stays readable.
            return
        finally:
            if current is not None:
                with contextlib.suppress(OSError):
                    current.close()


def _prune_sessions(root: Path, *, keep: int) -> None:
    folders = sorted(
        (entry for entry in root.iterdir() if entry.is_dir()), key=lambda entry: entry.name
    )
    excess = len(folders) - max(0, keep)
    for folder in folders[: max(0, excess)]:
        shutil.rmtree(folder, ignore_errors=True)
