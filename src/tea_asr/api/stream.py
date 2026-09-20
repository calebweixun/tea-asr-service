from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import struct
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from tea_asr.api.events import EventWriter
from tea_asr.config import ServiceConfig
from tea_asr.errors import ApiError
from tea_asr.logs import event as log_event
from tea_asr.rate_limit import AuthRateLimiter
from tea_asr.segmenter import ContinuousSegmenter, SegmentClosed, SegmenterConfig, SpeechStarted
from tea_asr.vad import SileroVad
from tea_asr.wire import (
    ALLOWED_WS_ORIGINS,
    INITIAL_FLOW_WINDOW_SAMPLES,
    MAX_FRAME_PCM_BYTES,
    MAX_SAFE_INT,
    MAX_UTTERANCE_PCM_BYTES,
    AudioAck,
    AudioCommitted,
    ClientEnvelope,
    ErrorEvent,
    FlowControl,
    Hello,
    Pong,
    PreviewPolicy,
    PreviewStatus,
    SegmentError,
    SegmentQueued,
    SegmentSkipped,
    SessionCancelled,
    SessionStart,
    SessionStarted,
    SessionStopped,
    TranscriptFinal,
    TranscriptPartial,
)
from tea_asr.wire import SpeechStarted as SpeechStartedEvent

FRAME_HEADER_BYTES = 16
IDLE_TIMEOUT_S = 120.0
SESSION_START_TIMEOUT_S = 5.0
FLOW_WINDOW_REFILL_SAMPLES = 80_000
FLOW_WINDOW_LOW_WATER_SAMPLES = 40_000
CONTROL_DEDUP_LIMIT = 256

#: Longest wait for queued segments to finish after session.stop. A stuck
#: consumer must not hold the connection open forever.
DRAIN_TIMEOUT_S = 120.0

#: docs/03: at most 16 segments may be waiting on one session.
MAX_PENDING_SEGMENTS = 16

#: Errors that concern one control message, not the session as a whole.
RECOVERABLE_CODES = frozenset({"conflict", "queue_full", "session_limit"})

#: docs/07: preview waits for 800 ms of new audio before running again.
PREVIEW_MIN_AUDIO_SAMPLES = 12_800
PREVIEW_MIN_INTERVAL_S = 0.8

#: How much audio a single preview may cover. docs/07 proposed 8 s to bound the
#: cost, but a continuous segment now runs to 14 s, so the preview froze partway
#: through a long sentence while the speaker was still talking. Measured RTF is
#: ~0.03, so 15 s of preview costs well under half a second.
PREVIEW_MAX_AUDIO_SAMPLES = 240_000

#: docs/07: a revisable continuous session waits longer before closing a
#: segment, so a late correction still lands before the final.
REVISABLE_END_SILENCE_MS = 900

logger = logging.getLogger("tea_asr.stream")

# Unicode reserves three disjoint ranges for application-private characters.
# ASR output is ordinary human text, so none of these code points is a valid
# Traditional Chinese character or punctuation mark. Keep the ranges explicit
# instead of applying broad Unicode normalization/sanitization.
PRIVATE_USE_RANGES: tuple[tuple[int, int], ...] = (
    (0xE000, 0xF8FF),  # Basic Multilingual Plane
    (0xF0000, 0xFFFFD),  # Supplementary Private Use Area-A (Plane 15)
    (0x100000, 0x10FFFD),  # Supplementary Private Use Area-B (Plane 16)
)


class StreamScheduler(Protocol):
    async def transcribe(
        self, pcm: bytes, *, language: str = "Chinese", kind: str = "interactive"
    ) -> tuple[dict[str, Any], int]: ...


def _is_private_use_codepoint(codepoint: int) -> bool:
    return any(start <= codepoint <= end for start, end in PRIVATE_USE_RANGES)


def private_use_warnings(text: str) -> list[str]:
    return ["private_use_characters"] if any(
        _is_private_use_codepoint(ord(char)) for char in text
    ) else []


def filter_private_use_characters(text: str) -> str:
    """Strip all Unicode Private Use Area characters.

    This covers BMP U+E000-U+F8FF, Plane 15 U+F0000-U+FFFFD, and Plane 16
    U+100000-U+10FFFD. It deliberately does not apply NFKC or remove other
    symbols/control categories: the server must preserve legitimate CJK,
    punctuation, emoji, and mixed-language text.

    Stopgap for a defect in `Alkd/TEA-ASR-1.1-MLX-4bit`, not a permanent
    feature. Measurements in docs/benchmarks/pua-bf16-ab-report.md show:
    - 70% of sentences from this quantized checkpoint contain PUA characters;
      the upstream BF16 checkpoint produces 0% on the same corpus, so the
      defect is introduced by the 4bit quantization of the language-model
      weights, not by decoding or by this service.
    - Re-quantizing to 8bit only drops the rate to 63.3%, at +92% size, +74%
      memory and +20% latency, so changing bit width does not fix it.
    - Sentence-by-sentence diffing confirms PUA codepoints are *inserted*
      between otherwise-identical, correct text, never substituted for it,
      the tested BMP PUA range never legitimately appears in Traditional
      Chinese, punctuation, or mixed CJK/Latin output. Stripping PUA therefore
      cannot delete real ASR content; raw output remains available for
      diagnostics.

    Remove this function and `ServiceConfig.filter_pua` once the service
    switches to a model revision (e.g. a clean BF16 or a correctly quantized
    checkpoint) that upstream confirms no longer emits PUA output.
    """

    return "".join(
        char for char in text if not _is_private_use_codepoint(ord(char))
    )


@dataclass(slots=True)
class Segment:
    segment_id: str
    index: int
    start_sample: int
    revision: int = 0
    published_preview_end: int = 0
    terminal: bool = False


@dataclass(slots=True)
class ClosedSegment:
    segment: Segment
    pcm: bytes
    end_sample: int
    boundary: str


@dataclass(slots=True)
class SessionState:
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    next_seq: int = 0
    next_sample: int = 0
    send_until_sample: int = INITIAL_FLOW_WINDOW_SAMPLES
    pcm: bytearray = field(default_factory=bytearray)
    segment: Segment | None = None
    next_index: int = 0
    failed_segments: list[int] = field(default_factory=list)
    cancelled: bool = False


class ContinuousSessionAdmission:
    """Atomically reserve slots for live ``continuous`` sessions.

    The WebSocket handshake for each connection runs in its own task.  A plain
    count of the sessions whose profile has already been assigned has a race:
    two tasks can both inspect the count before either task reaches
    ``StreamSession.run``'s profile assignment.  Reservations are made while
    holding one event-loop lock, so an admitted-but-not-yet-started session is
    counted immediately.

    Despite the name, this is a plain counted-slot reservation with no
    ``continuous``-specific logic, so ``run_stream`` reuses it unchanged to
    enforce `limits.max_total_connections` (docs/04-api.md) across every
    profile, keyed by a token distinct from the per-session one above.
    """

    def __init__(self, max_sessions: int | None) -> None:
        self.max_sessions = max_sessions
        self._reserved: set[object] = set()
        self._lock = asyncio.Lock()

    async def try_acquire(self, session: object) -> bool:
        """Reserve one slot, returning ``False`` when the limit is reached."""

        if self.max_sessions is None:
            return True
        async with self._lock:
            if len(self._reserved) >= self.max_sessions:
                return False
            self._reserved.add(session)
            return True

    def release(self, session: object) -> None:
        """Release a reservation.

        This is intentionally synchronous.  All callers run on the same
        asyncio event loop, and ``try_acquire`` has no suspension point after
        entering its critical section, so a discard cannot interleave with the
        count-and-add operation.  Keeping release non-awaiting also means a
        cancelled WebSocket task cannot leak a slot while tearing down.
        """

        self._reserved.discard(session)


class StreamSession:
    """One WebSocket connection: one audio timeline, one ordered segment stream.

    Inference never runs on the receive loop. Closed segments go onto a bounded
    queue that a single consumer drains in order, so audio keeps arriving while
    the model works and `segment_index` order is preserved by construction.
    """

    def __init__(
        self,
        websocket: WebSocket,
        scheduler: StreamScheduler,
        *,
        config: ServiceConfig,
        model_state: str,
        vad: SileroVad | None = None,
        registry: set[StreamSession] | None = None,
        continuous_admission: ContinuousSessionAdmission | None = None,
    ) -> None:
        self._websocket = websocket
        self._scheduler = scheduler
        self._config = config
        self._model_state = model_state
        self._vad = vad
        #: Other live sessions on this service, used by the sleep watcher.
        self._registry = registry
        #: Shared, atomic admission state for continuous sessions.  ``None``
        #: in tests that build a ``StreamSession`` directly keeps the old
        #: unbounded test-double behaviour.
        self._continuous_admission = continuous_admission
        self._continuous_reserved = False
        self._state = SessionState()
        self._writer = EventWriter(websocket, self._state.session_id)
        self._profile = "utterance"
        self._transcript_mode = "final_only"
        self._language = "Chinese"
        self._segmenter: ContinuousSegmenter | None = None
        self._pending: asyncio.Queue[ClosedSegment] = asyncio.Queue(MAX_PENDING_SEGMENTS)
        self._consumer: asyncio.Task[None] | None = None
        self._preview_task: asyncio.Task[None] | None = None
        self._preview_pending = False
        self._preview_last_started = 0.0
        self._control_acks: dict[str, str] = {}

    # -- handshake -----------------------------------------------------------

    async def _read_session_start(self) -> SessionStart:
        message = await asyncio.wait_for(
            self._websocket.receive(), timeout=SESSION_START_TIMEOUT_S
        )
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000))
        if message.get("text") is None:
            raise ApiError("protocol_error", "session.started 之前不接受 binary frame。")
        start = self._parse_control(message["text"])
        if not isinstance(start, SessionStart):
            raise ApiError("protocol_error", "連線後的第一則訊息必須是 session.start。")
        if start.profile == "continuous" and self._vad is None:
            raise ApiError(
                "unsupported_option",
                "continuous profile 需要 VAD 資產；請先執行 tea-asr model-prepare。",
            )
        if start.profile == "continuous" and self._continuous_admission is not None:
            if not await self._continuous_admission.try_acquire(self):
                raise ApiError(
                    "concurrent_session_limit",
                    f"併發 continuous session 已達上限（{self._continuous_admission.max_sessions}）"
                    "，請稍後再試或等其他 session 結束。",
                )
            self._continuous_reserved = True
        if start.durable:
            raise ApiError("unsupported_option", "durable session 要等 P4 完成才提供。")
        if start.transcript_mode == "revisable" and not self._config.revisable_preview:
            raise ApiError(
                "unsupported_option",
                "revisable 預覽尚未通過 P2a 驗收，請以 final_only 建立 session。",
            )
        if self._model_state != "ready":
            raise ApiError(
                "model_loading" if self._model_state == "loading" else "model_unavailable",
                f"模型目前狀態為 {self._model_state}。",
            )
        return start

    def _preview_policy(self) -> PreviewPolicy | None:
        if self._transcript_mode != "revisable":
            return None
        continuous = self._profile == "continuous"
        return PreviewPolicy(
            min_audio_ms=PREVIEW_MIN_AUDIO_SAMPLES // 16,
            min_interval_ms=int(PREVIEW_MIN_INTERVAL_S * 1000),
            max_preview_audio_ms=PREVIEW_MAX_AUDIO_SAMPLES // 16,
            endpoint_silence_ms=REVISABLE_END_SILENCE_MS if continuous else None,
            max_segment_ms=(
                SegmenterConfig().hard_cap_ms
                if continuous
                else MAX_UTTERANCE_PCM_BYTES // 2 // 16
            ),
            context_biasing=False,
        )

    # -- parsing -------------------------------------------------------------

    def _parse_control(self, raw: str) -> Any:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ApiError("protocol_error", "控制訊息不是合法 JSON。") from exc
        if not isinstance(payload, dict) or "type" not in payload:
            raise ApiError("protocol_error", "控制訊息缺少 type 欄位。")
        try:
            return ClientEnvelope.model_validate({"event": payload}).event
        except ValidationError as exc:
            raise ApiError(_validation_code(payload, exc), _first_error(exc)) from exc

    def _dedup(self, request_id: str, kind: str, fingerprint: str) -> bool:
        previous = self._control_acks.get(request_id)
        if previous is None:
            self._control_acks[request_id] = f"{kind}:{fingerprint}"
            while len(self._control_acks) > CONTROL_DEDUP_LIMIT:
                self._control_acks.pop(next(iter(self._control_acks)))
            return False
        if previous != f"{kind}:{fingerprint}":
            raise ApiError("conflict", f"request_id {request_id} 已用於不同的控制訊息。")
        return True

    # -- segments ------------------------------------------------------------

    def _open_segment(self, start_sample: int) -> Segment:
        segment = Segment(
            segment_id=str(uuid.uuid4()),
            index=self._state.next_index,
            start_sample=start_sample,
        )
        self._state.next_index += 1
        self._state.segment = segment
        return segment

    def _enqueue(self, segment: Segment, pcm: bytes, end_sample: int, boundary: str) -> None:
        segment.terminal = True
        self._writer.emit(
            SegmentQueued(
                session_id=self._state.session_id,
                event_id=0,
                segment_id=segment.segment_id,
                segment_index=segment.index,
                start_sample=segment.start_sample,
                end_sample=end_sample,
                boundary=boundary,  # type: ignore[arg-type]
            )
        )
        try:
            self._pending.put_nowait(
                ClosedSegment(segment=segment, pcm=pcm, end_sample=end_sample, boundary=boundary)
            )
        except asyncio.QueueFull as exc:
            raise ApiError("session_limit", "等待辨識的片段已達上限。") from exc

    async def _consume(self) -> None:
        """Drain closed segments in order; one terminal event each."""

        while True:
            closed = await self._pending.get()
            try:
                if self._state.cancelled:
                    continue
                await self._transcribe_segment(closed)
            except Exception as exc:  # noqa: BLE001 - one bad segment must not
                # kill the consumer; the rest of the session still has to finish.
                self._state.failed_segments.append(closed.segment.index)
                self._writer.emit(
                    SegmentError(
                        session_id=self._state.session_id,
                        event_id=0,
                        segment_id=closed.segment.segment_id,
                        segment_index=closed.segment.index,
                        code="internal_error",
                        message=type(exc).__name__,
                        retryable=False,
                    )
                )
            finally:
                self._pending.task_done()

    def _apply_pua_filter(self, text: str, *, segment: Segment, kind: str) -> str:
        """Filter PUA characters out of recognized text, per `filter_pua`.

        Never logs `text`/`raw_text` (docs/03 forbids transcript content in
        the log; `JsonFormatter` also strips those keys as a second layer).
        Only a character count is observable, so the filter's impact can be
        monitored without leaking what was said.
        """

        if not self._config.filter_pua:
            return text
        filtered = filter_private_use_characters(text)
        removed = len(text) - len(filtered)
        if removed:
            log_event(
                logger,
                "pua_filtered",
                session_id=self._state.session_id,
                segment_id=segment.segment_id,
                segment_index=segment.index,
                kind=kind,
                removed_chars=removed,
            )
        return filtered

    async def _transcribe_segment(self, closed: ClosedSegment) -> None:
        segment = closed.segment
        state = self._state
        try:
            response, queue_ms = await self._scheduler.transcribe(
                closed.pcm,
                language=self._language,
                kind="realtime" if self._profile == "continuous" else "interactive",
            )
        except ApiError as exc:
            state.failed_segments.append(segment.index)
            self._writer.emit(
                SegmentError(
                    session_id=state.session_id,
                    event_id=0,
                    segment_id=segment.segment_id,
                    segment_index=segment.index,
                    code=exc.code,
                    message=exc.message,
                    retryable=exc.retryable,
                )
            )
            return
        if state.cancelled:
            return
        raw_text = str(response["text"])
        if not raw_text:
            self._writer.emit(
                SegmentSkipped(
                    session_id=state.session_id,
                    event_id=0,
                    segment_id=segment.segment_id,
                    segment_index=segment.index,
                    reason="no_speech",
                )
            )
            return
        warnings = private_use_warnings(raw_text)
        text = self._apply_pua_filter(raw_text, segment=segment, kind="final")
        if not text:
            # The whole segment was PUA noise (see filter_private_use_characters):
            # sending an empty final would look like a real, silent recognition
            # and would desync a client that only tracks segment.skipped/final
            # for terminal state. "empty" says a result existed and was fully
            # filtered, as opposed to "no_speech" (nothing was recognized).
            self._writer.emit(
                SegmentSkipped(
                    session_id=state.session_id,
                    event_id=0,
                    segment_id=segment.segment_id,
                    segment_index=segment.index,
                    reason="empty",
                )
            )
            return
        self._writer.emit(
            TranscriptFinal(
                session_id=state.session_id,
                event_id=0,
                segment_id=segment.segment_id,
                segment_index=segment.index,
                revision=segment.revision + 1,
                start_sample=segment.start_sample,
                end_sample=closed.end_sample,
                text=text,
                raw_text=raw_text,
                audio_ms=(closed.end_sample - segment.start_sample) // 16,
                queue_ms=queue_ms,
                inference_ms=round(float(response["total_time_s"]) * 1000),
                warnings=warnings,
            )
        )

    # -- audio ---------------------------------------------------------------

    def _handle_frame(self, frame: bytes) -> None:
        state = self._state
        if len(frame) <= FRAME_HEADER_BYTES:
            raise ApiError("protocol_error", "Binary frame 缺少 PCM 內容。")
        pcm = frame[FRAME_HEADER_BYTES:]
        if len(pcm) > MAX_FRAME_PCM_BYTES or len(pcm) % 2:
            raise ApiError("protocol_error", "PCM 長度必須為偶數且不超過 6,400 bytes。")
        seq, start_sample = struct.unpack("<QQ", frame[:FRAME_HEADER_BYTES])
        if seq > MAX_SAFE_INT or start_sample > MAX_SAFE_INT:
            raise ApiError("protocol_error", "seq 或 start_sample 超過安全整數範圍。")
        if seq != state.next_seq or start_sample != state.next_sample:
            raise ApiError("protocol_error", "seq 或 sample clock 不連續。")
        end_sample = start_sample + len(pcm) // 2
        if end_sample > state.send_until_sample:
            raise ApiError("protocol_error", "Frame 超出 flow-control 窗口。")

        if self._profile == "continuous":
            self._advance_continuous(pcm)
        else:
            if len(state.pcm) + len(pcm) > MAX_UTTERANCE_PCM_BYTES:
                raise ApiError("payload_too_large", "單一 utterance 不得超過 30 秒。")
            if state.segment is None:
                self._open_segment(start_sample)
            state.pcm.extend(pcm)

        state.next_seq += 1
        state.next_sample = end_sample
        self._writer.emit(
            AudioAck(
                session_id=state.session_id,
                event_id=0,
                received_seq=seq,
                received_sample=end_sample,
                persisted_seq=None,
                persisted_sample=None,
            )
        )
        if state.send_until_sample - state.next_sample <= FLOW_WINDOW_LOW_WATER_SAMPLES:
            state.send_until_sample = min(
                MAX_SAFE_INT, state.next_sample + FLOW_WINDOW_REFILL_SAMPLES
            )
            self._writer.emit(
                FlowControl(
                    session_id=state.session_id,
                    event_id=0,
                    send_until_sample=state.send_until_sample,
                    reason="normal",
                )
            )
        self._maybe_schedule_preview()

    def _advance_continuous(self, pcm: bytes) -> None:
        assert self._segmenter is not None
        for event in self._segmenter.push(pcm):
            if isinstance(event, SpeechStarted):
                segment = self._open_segment(event.start_sample)
                self._writer.emit(
                    SpeechStartedEvent(
                        session_id=self._state.session_id,
                        event_id=0,
                        segment_id=segment.segment_id,
                        segment_index=segment.index,
                        start_sample=event.start_sample,
                    )
                )
            elif isinstance(event, SegmentClosed):
                segment = self._state.segment or self._open_segment(event.start_sample)
                self._state.segment = None
                self._settle_preview_soon(segment)
                self._enqueue(segment, event.pcm, event.end_sample, event.boundary)
                if event.boundary == "max_duration":
                    # The talker did not stop, so the next segment opens at the
                    # same sample the previous one ended on.
                    reopened = self._open_segment(event.end_sample)
                    self._writer.emit(
                        SpeechStartedEvent(
                            session_id=self._state.session_id,
                            event_id=0,
                            segment_id=reopened.segment_id,
                            segment_index=reopened.index,
                            start_sample=event.end_sample,
                        )
                    )

    # -- preview (P2a, experimental) ----------------------------------------

    def _current_preview_audio(self) -> tuple[bytes, int] | None:
        if self._profile == "continuous":
            if self._segmenter is None:
                return None
            open_audio = self._segmenter.open_segment_audio()
            if open_audio is None:
                return None
            start, pcm = open_audio
            return pcm, start + len(pcm) // 2
        if not self._state.pcm:
            return None
        return bytes(self._state.pcm), self._state.next_sample

    def _maybe_schedule_preview(self) -> None:
        if self._transcript_mode != "revisable":
            return
        segment = self._state.segment
        if segment is None or segment.terminal:
            return
        audio = self._current_preview_audio()
        if audio is None:
            return
        pcm, end_sample = audio
        samples = len(pcm) // 2
        if samples > PREVIEW_MAX_AUDIO_SAMPLES:
            return
        if samples - segment.published_preview_end < PREVIEW_MIN_AUDIO_SAMPLES:
            return
        if self._preview_task is not None:
            self._preview_pending = True
            return
        loop = asyncio.get_running_loop()
        if loop.time() - self._preview_last_started < PREVIEW_MIN_INTERVAL_S:
            self._preview_pending = True
            return
        self._preview_last_started = loop.time()
        self._preview_task = asyncio.create_task(self._run_preview(pcm, end_sample, segment))

    async def _run_preview(self, snapshot: bytes, end_sample: int, segment: Segment) -> None:
        try:
            response, _ = await self._scheduler.transcribe(
                snapshot, language=self._language, kind="preview"
            )
            if segment.terminal or self._state.cancelled:
                return
            if end_sample - segment.start_sample <= segment.published_preview_end:
                return
            segment.revision += 1
            segment.published_preview_end = end_sample - segment.start_sample
            text = self._apply_pua_filter(
                str(response["text"]), segment=segment, kind="partial"
            )
            self._writer.emit(
                TranscriptPartial(
                    session_id=self._state.session_id,
                    event_id=0,
                    segment_id=segment.segment_id,
                    segment_index=segment.index,
                    revision=segment.revision,
                    start_sample=segment.start_sample,
                    end_sample=end_sample,
                    text=text,
                )
            )
        except ApiError as exc:
            if not segment.terminal:
                self._writer.emit(
                    PreviewStatus(
                        session_id=self._state.session_id,
                        event_id=0,
                        state="paused",
                        reason="load" if exc.code == "queue_full" else "backend_error",
                    )
                )
        except Exception:  # noqa: BLE001 - preview must never take the session down
            if not segment.terminal:
                self._writer.emit(
                    PreviewStatus(
                        session_id=self._state.session_id,
                        event_id=0,
                        state="paused",
                        reason="backend_error",
                    )
                )
        finally:
            self._preview_task = None
            if self._preview_pending and not segment.terminal:
                self._preview_pending = False
                self._maybe_schedule_preview()

    def _settle_preview_soon(self, segment: Segment) -> None:
        """Mark the segment closed so an in-flight preview discards its result."""

        segment.terminal = True
        self._preview_pending = False

    async def _settle_preview(self) -> None:
        self._preview_pending = False
        task = self._preview_task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    # -- commit / stop -------------------------------------------------------

    async def _close_open_audio(self, request_id: str, boundary: str) -> None:
        state = self._state
        segment = state.segment
        if segment is not None:
            self._settle_preview_soon(segment)
        await self._settle_preview()

        if self._profile == "continuous":
            assert self._segmenter is not None
            closed_any = False
            for event in self._segmenter.flush():
                if isinstance(event, SegmentClosed):
                    active = state.segment or self._open_segment(event.start_sample)
                    state.segment = None
                    self._enqueue(active, event.pcm, event.end_sample, "stop")
                    closed_any = True
            if not closed_any and boundary == "stop":
                self._writer.emit(
                    AudioCommitted(
                        session_id=state.session_id,
                        event_id=0,
                        request_id=request_id,
                        segment_id=None,
                        reason="no_audio",
                    )
                )
            return

        if segment is None or not state.pcm:
            self._writer.emit(
                AudioCommitted(
                    session_id=state.session_id,
                    event_id=0,
                    request_id=request_id,
                    segment_id=None,
                    reason="no_audio",
                )
            )
            return
        self._writer.emit(
            AudioCommitted(
                session_id=state.session_id,
                event_id=0,
                request_id=request_id,
                segment_id=segment.segment_id,
                reason="committed",
            )
        )
        pcm = bytes(state.pcm)
        state.pcm.clear()
        state.segment = None
        self._enqueue(segment, pcm, state.next_sample, boundary)

    # -- main loop -----------------------------------------------------------

    async def run(self) -> None:
        await self._websocket.send_json(
            Hello(
                protocol_version=self._config.protocol_version, model_state=self._model_state
            ).model_dump(mode="json")
        )
        start = await self._read_session_start()
        self._profile = start.profile
        self._transcript_mode = start.transcript_mode
        self._language = start.language
        self._control_acks[start.request_id] = "session.start:"
        if self._profile == "continuous":
            assert self._vad is not None
            end_silence = (
                REVISABLE_END_SILENCE_MS
                if self._transcript_mode == "revisable"
                else SegmenterConfig().end_silence_ms
            )
            self._segmenter = ContinuousSegmenter(
                self._vad, SegmenterConfig(end_silence_ms=end_silence)
            )
        self._consumer = asyncio.create_task(self._consume())
        self._writer.emit(
            SessionStarted(
                session_id=self._state.session_id,
                event_id=0,
                request_id=start.request_id,
                profile=start.profile,
                transcript_mode=start.transcript_mode,
                next_seq=0,
                next_sample=0,
                send_until_sample=self._state.send_until_sample,
                preview_policy=self._preview_policy(),
            )
        )

        while True:
            message = await asyncio.wait_for(self._websocket.receive(), timeout=IDLE_TIMEOUT_S)
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))
            if message.get("bytes") is not None:
                self._handle_frame(message["bytes"])
                continue
            raw = message.get("text")
            if raw is None:
                continue
            control = self._parse_control(raw)
            try:
                if await self._handle_control(control):
                    return
            except ApiError as exc:
                if exc.code not in RECOVERABLE_CODES:
                    raise
                self._writer.emit(
                    ErrorEvent(
                        session_id=self._state.session_id,
                        event_id=0,
                        code=exc.code,
                        message=exc.message,
                        retryable=exc.retryable,
                        request_id=getattr(control, "request_id", None),
                    )
                )

    async def _handle_control(self, control: Any) -> bool:
        state = self._state
        kind = control.type
        if kind == "ping":
            if not self._dedup(control.request_id, kind, ""):
                self._writer.emit(
                    Pong(session_id=state.session_id, event_id=0, request_id=control.request_id)
                )
            return False
        if kind == "session.start":
            raise ApiError("protocol_error", "同一條連線只能有一次 session.start。")
        if kind == "audio.commit":
            if self._profile == "continuous":
                raise ApiError(
                    "unsupported_option", "continuous profile 由 VAD 切段，不接受 audio.commit。"
                )
            if control.through_seq != state.next_seq - 1:
                raise ApiError("protocol_error", "audio.commit 的 through_seq 與已收音訊不符。")
            if self._dedup(control.request_id, kind, str(control.through_seq)):
                return False
            await self._close_open_audio(control.request_id, "manual")
            return False
        if kind == "session.stop":
            expected = state.next_seq - 1 if state.next_seq else None
            if control.through_seq != expected:
                raise ApiError("protocol_error", "session.stop 的 through_seq 與已收音訊不符。")
            self._dedup(control.request_id, kind, str(expected))
            await self._close_open_audio(control.request_id, "stop")
            try:
                await asyncio.wait_for(self._pending.join(), timeout=DRAIN_TIMEOUT_S)
            except TimeoutError:
                # Report what did not finish instead of hanging the connection.
                while not self._pending.empty():
                    pending = self._pending.get_nowait()
                    state.failed_segments.append(pending.segment.index)
                    self._writer.emit(
                        SegmentError(
                            session_id=state.session_id,
                            event_id=0,
                            segment_id=pending.segment.segment_id,
                            segment_index=pending.segment.index,
                            code="inference_timeout",
                            message="片段在 session 結束前未完成。",
                            retryable=True,
                        )
                    )
                    self._pending.task_done()
            self._writer.emit(
                SessionStopped(
                    session_id=state.session_id,
                    event_id=0,
                    request_id=control.request_id,
                    last_seq=expected,
                    status="completed_with_errors" if state.failed_segments else "completed",
                    failed_segments=list(state.failed_segments),
                )
            )
            await self._writer.drain()
            await self._websocket.close(code=1000)
            return True
        if kind == "session.cancel":
            state.cancelled = True
            if state.segment is not None:
                state.segment.terminal = True
            await self._settle_preview()
            self._writer.emit(
                SessionCancelled(
                    session_id=state.session_id, event_id=0, request_id=control.request_id
                )
            )
            await self._writer.drain()
            await self._websocket.close(code=1000)
            return True
        raise ApiError("protocol_error", f"不支援的控制事件：{kind}")

    async def fail(self, error: ApiError) -> None:
        self._state.cancelled = True
        if self._state.segment is not None:
            self._state.segment.terminal = True
        await self._settle_preview()
        try:
            self._writer.emit(
                ErrorEvent(
                    session_id=self._state.session_id,
                    event_id=0,
                    code=error.code,
                    message=error.message,
                    retryable=error.retryable,
                    request_id=error.request_id,
                )
            )
            await self._writer.drain(timeout=1.0)
            await self._websocket.close(code=error.ws_close_code or 1011)
        except (RuntimeError, WebSocketDisconnect):
            pass

    async def interrupt(self, reason: str) -> None:
        """End the session because its sample clock can no longer be trusted.

        v0.1 has no resume, so pretending the recording continued across a gap
        would be a lie about the timeline (docs/03). The client starts a new
        session with a fresh clock instead.
        """

        await self.fail(ApiError("timeline_gap", reason))

    async def shutdown(self) -> None:
        for task in (self._consumer, self._preview_task):
            if task is not None:
                task.cancel()
        tasks = [task for task in (self._consumer, self._preview_task) if task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def release_continuous_admission(self) -> None:
        """Return this session's reserved continuous slot, if any."""

        if not self._continuous_reserved:
            return
        self._continuous_reserved = False
        if self._continuous_admission is not None:
            self._continuous_admission.release(self)

    @property
    def writer(self) -> EventWriter:
        return self._writer

    @property
    def is_continuous(self) -> bool:
        """True once this session's `session.start` picked the continuous profile.

        The admission limit uses ``ContinuousSessionAdmission`` reservations,
        so a session still being admitted is counted there before this profile
        field is assigned by ``run``.
        """

        return self._profile == "continuous"


_KNOWN_TYPES = {"session.start", "audio.commit", "session.stop", "session.cancel", "ping"}


def _validation_code(payload: dict[str, Any], exc: ValidationError) -> str:
    """Separate a broken message from a well-formed but unsupported option."""

    if payload.get("type") not in _KNOWN_TYPES:
        return "protocol_error"
    first = exc.errors()[0]
    if first["type"] == "extra_forbidden":
        return "protocol_error"
    if "audio" in first["loc"]:
        return "invalid_audio"
    return "unsupported_option"


def _first_error(exc: ValidationError) -> str:
    first = exc.errors()[0]
    location = ".".join(str(part) for part in first["loc"][1:]) or "payload"
    return f"{location}: {first['msg']}"


_BEARER_PREFIX = "Bearer "


async def run_stream(
    websocket: WebSocket,
    scheduler: StreamScheduler,
    *,
    auth_token: str | None = None,
    token_matches: Callable[[str], bool] | None = None,
    origin_allowed: Callable[[str], bool] | None = None,
    rate_limiter: AuthRateLimiter | None = None,
    insecure_lan: bool = False,
    config: ServiceConfig,
    model_state: str,
    vad: SileroVad | None = None,
    registry: set[StreamSession] | None = None,
    continuous_admission: ContinuousSessionAdmission | None = None,
    connection_admission: ContinuousSessionAdmission | None = None,
) -> None:
    """Authenticate and admit one `/v1/stream` connection.

    Either `auth_token` (a fixed string, exact `Bearer <token>` match — the
    legacy shape, still used directly by a couple of low-level tests) or
    `token_matches` (a predicate, W9's `TokenAuthenticator`-backed check) must
    be given; `tea_asr.api.app.create_app` always passes `token_matches`.
    `origin_allowed` defaults to the historic loopback-only exact match
    (`ALLOWED_WS_ORIGINS`) when omitted, so callers that do not opt into LAN
    mode see unchanged behaviour.
    """

    def _token_matches(presented: str) -> bool:
        if token_matches is not None:
            return token_matches(presented)
        return auth_token is not None and presented == auth_token

    def _origin_allowed(origin: str) -> bool:
        if origin_allowed is not None:
            return origin_allowed(origin)
        return origin in ALLOWED_WS_ORIGINS

    client_key = websocket.client.host if websocket.client is not None else "unknown"
    if rate_limiter is not None and rate_limiter.is_blocked(client_key):
        await websocket.close(code=1013, reason="rate_limited")
        return

    authorization = websocket.headers.get("authorization")
    presented = (
        authorization[len(_BEARER_PREFIX) :]
        if authorization is not None and authorization.startswith(_BEARER_PREFIX)
        else None
    )
    if presented is None or not _token_matches(presented):
        if rate_limiter is not None:
            rate_limiter.record_failure(client_key)
        await websocket.close(code=1008, reason="unauthenticated")
        return
    if rate_limiter is not None:
        rate_limiter.record_success(client_key)

    origin = websocket.headers.get("origin")
    if origin and not _origin_allowed(origin):
        await websocket.close(code=1008, reason="forbidden_origin")
        return

    #: docs/04-api.md `limits.max_total_connections`: reserved before `accept()`
    #: so two connections racing the handshake cannot both slip in over the
    #: cap (same race `ContinuousSessionAdmission` guards against above).
    connection_token = object()
    if connection_admission is not None and not await connection_admission.try_acquire(
        connection_token
    ):
        close_error = ApiError("session_limit", "同時連線數已達上限，請稍後再試。")
        await websocket.close(code=close_error.ws_close_code or 1013, reason="session_limit")
        return

    accept_headers = (
        [
            (b"x-tea-asr-security", b"unencrypted-lan-mode"),
            (
                b"x-tea-asr-security-notice",
                (
                    b"unencrypted; bearer token sent in cleartext; "
                    b"LAN/Tailscale use only, do not expose publicly"
                ),
            ),
        ]
        if insecure_lan
        else None
    )
    await websocket.accept(headers=accept_headers)
    session = StreamSession(
        websocket,
        scheduler,
        config=config,
        model_state=model_state,
        vad=vad,
        registry=registry,
        continuous_admission=continuous_admission,
    )
    if registry is not None:
        registry.add(session)
    writer_task = asyncio.create_task(session.writer.run())
    watchdog_task = asyncio.create_task(session.writer.watchdog())
    main_task = asyncio.create_task(session.run())
    try:
        done, _ = await asyncio.wait(
            {main_task, writer_task, watchdog_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc
    except ApiError as exc:
        await session.fail(exc)
    except (WebSocketDisconnect, TimeoutError):
        pass
    finally:
        if registry is not None:
            registry.discard(session)
        session.release_continuous_admission()
        if connection_admission is not None:
            connection_admission.release(connection_token)
        for task in (main_task, writer_task, watchdog_task):
            task.cancel()
        # Teardown runs while the connection is already going away, so a
        # cancellation arriving here must not escape as an endpoint failure.
        with contextlib.suppress(asyncio.CancelledError):
            await session.shutdown()
            await asyncio.gather(main_task, writer_task, watchdog_task, return_exceptions=True)
