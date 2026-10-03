from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import struct
import time
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from tea_asr.api.events import EventWriter
from tea_asr.config import ServiceConfig
from tea_asr.context import (
    ContextDictionaryStore,
    ContextPlan,
    InvalidDictionary,
    ReplacementRule,
    UnknownDictionaryProfile,
    apply_replacements,
    contains_context_echo,
    resolve_context,
)
from tea_asr.diagnostics import TICK_S, AudioCapture, SessionDiagnostics, WarningLimiter
from tea_asr.errors import ApiError
from tea_asr.logs import event as log_event
from tea_asr.rate_limit import AuthRateLimiter
from tea_asr.repetition import TrimmedRepetition, trim_repetitions
from tea_asr.scheduler import StaleTaskDropped
from tea_asr.segmenter import ContinuousSegmenter, SegmentClosed, SegmenterConfig, SpeechStarted
from tea_asr.singing import AudioClassDecision, SegmentLabeler
from tea_asr.singing_session import SessionSinging, SingingRuntime
from tea_asr.stable import StablePrefixTracker, StableUpdate
from tea_asr.translation.session import SessionTranslator
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
    ContextEcho,
    ErrorEvent,
    FlowControl,
    Hello,
    Pong,
    PreviewPolicy,
    PreviewStatus,
    SegmentAudioClass,
    SegmentError,
    SegmentQueued,
    SegmentSkipped,
    SessionCancelled,
    SessionStart,
    SessionStarted,
    SessionStopped,
    TranscriptFinal,
    TranscriptPartial,
    TranscriptStable,
    TranslationStarted,
)
from tea_asr.wire import SpeechStarted as SpeechStartedEvent

FRAME_HEADER_BYTES = 16
IDLE_TIMEOUT_S = 120.0
SESSION_START_TIMEOUT_S = 5.0
FLOW_WINDOW_REFILL_SAMPLES = 80_000
FLOW_WINDOW_LOW_WATER_SAMPLES = 40_000
CONTROL_DEDUP_LIMIT = 256
SAMPLE_RATE = 16_000

# Carry-over is bounded independently of a session's total received audio. The
# extra two seconds cover the continuous segmenter's pre-roll and a full frame
# when a segment opens partway through it.
CARRY_HISTORY_LOOKBACK_S = 2.0
CARRY_OVERLAP_MIN_CHARS = 4
CARRY_OVERLAP_MAX_CHARS = 256
CARRY_OVERLAP_FUZZY_MAX_CHARS = 64

#: Longest wait for queued segments to finish after session.stop. A stuck
#: consumer must not hold the connection open forever.
DRAIN_TIMEOUT_S = 120.0

#: docs/03: at most 16 segments may be waiting on one session.
MAX_PENDING_SEGMENTS = 16

#: Errors that concern one control message, not the session as a whole.
RECOVERABLE_CODES = frozenset({"conflict", "queue_full", "session_limit"})

#: The preview cadence (new-audio threshold, minimum interval, load factor)
#: is server config: `ServiceConfig.preview_min_audio_ms` and friends.

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
        self,
        pcm: bytes,
        *,
        language: str = "Chinese",
        kind: str = "interactive",
        is_stale: Callable[[], bool] | None = None,
        system_prompt: str | None = None,
    ) -> tuple[dict[str, Any], int]: ...


class StreamTranslationProvider(Protocol):
    """The separate translation provider (`tea_asr.translation.supervisor`)."""

    state: str
    generation: int
    task_timeout_s: float
    model: str
    model_revision: str

    def availability_error(self) -> ApiError | None: ...
    def try_acquire(self, owner: object) -> bool: ...
    def release(self, owner: object) -> None: ...
    async def start_session(self, direction: str, latency_mode: str) -> int: ...
    async def translate(self, text: str, *, force: bool) -> dict[str, Any]: ...


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


def normalize_carry_text(text: str) -> tuple[str, list[int]]:
    """Fold width, case, punctuation, and whitespace for carry matching.

    The returned indexes map each normalized character to the exclusive end
    of its source character in ``text``, so a match can be removed without
    changing the spelling or punctuation of the remaining text.
    """

    normalized: list[str] = []
    source_ends: list[int] = []
    for index, char in enumerate(text):
        folded = unicodedata.normalize("NFKC", char).casefold()
        for candidate in folded:
            if candidate.isspace() or unicodedata.category(candidate).startswith("P"):
                continue
            normalized.append(candidate)
            source_ends.append(index + 1)
    return "".join(normalized), source_ends


def strip_carried_overlap(previous_text: str, current_text: str) -> tuple[str, int] | None:
    """Strip a confident previous-final suffix from a carried decode prefix.

    Matching is NFKC/case/space/punctuation insensitive. A bounded edit
    distance allows roughly one change per four normalized characters. An
    overlap shorter than four normalized characters is ambiguous, so callers
    should retry the segment without carry when this returns ``None``.
    """

    previous, _ = normalize_carry_text(previous_text)
    current, source_ends = normalize_carry_text(current_text)
    if len(previous) < CARRY_OVERLAP_MIN_CHARS or len(current) < CARRY_OVERLAP_MIN_CHARS:
        return None

    previous = previous[-CARRY_OVERLAP_MAX_CHARS:]
    current = current[:CARRY_OVERLAP_MAX_CHARS]
    source_ends = source_ends[:CARRY_OVERLAP_MAX_CHARS]

    best: tuple[tuple[int, int, int, int, int], int] | None = None
    for size in range(min(len(previous), len(current)), CARRY_OVERLAP_MIN_CHARS - 1, -1):
        if previous[-size:] == current[:size]:
            best = ((size, 0, 0, size, -size), size)
            break

    # Fuzzy matching is limited to the last/first 64 normalized characters.
    # That covers a 5-second speech prefix while keeping work on the stream's
    # event loop bounded; exact overlaps above can be longer (up to 256).
    fuzzy_previous = previous[-CARRY_OVERLAP_FUZZY_MAX_CHARS:]
    fuzzy_current = current[:CARRY_OVERLAP_FUZZY_MAX_CHARS]
    for suffix_size in range(CARRY_OVERLAP_MIN_CHARS, len(fuzzy_previous) + 1):
        suffix = fuzzy_previous[-suffix_size:]
        row = list(range(len(fuzzy_current) + 1))
        for previous_index, previous_char in enumerate(suffix, start=1):
            next_row = [previous_index]
            for current_index, current_char in enumerate(fuzzy_current, start=1):
                next_row.append(
                    min(
                        row[current_index] + 1,
                        next_row[current_index - 1] + 1,
                        row[current_index - 1] + (previous_char != current_char),
                    )
                )
            row = next_row

        for prefix_size in range(CARRY_OVERLAP_MIN_CHARS, len(fuzzy_current) + 1):
            distance = row[prefix_size]
            longest = max(suffix_size, prefix_size)
            matched = longest - distance
            if distance * 4 > longest or min(suffix_size, prefix_size) < CARRY_OVERLAP_MIN_CHARS:
                continue
            # Prefer an exact shorter overlap over a fuzzy alignment that
            # absorbs the first new character as an insertion. This minimizes
            # accidental loss when the previous suffix is genuinely present.
            score = (
                matched,
                -distance,
                -abs(suffix_size - prefix_size),
                min(suffix_size, prefix_size),
                -prefix_size,
            )
            if best is None or score > best[0]:
                best = (score, prefix_size)

    if best is None:
        return None

    prefix_size = best[1]
    source_cut = source_ends[prefix_size - 1]
    # Separators after the duplicate belong to the carried phrase boundary.
    while source_cut < len(current_text):
        char = current_text[source_cut]
        if not char.isspace() and not unicodedata.category(char).startswith("P"):
            break
        source_cut += 1
    return current_text[source_cut:], prefix_size


@dataclass(slots=True)
class Segment:
    segment_id: str
    index: int
    start_sample: int
    revision: int = 0
    published_preview_end: int = 0
    terminal: bool = False
    #: Opt-in append-only subtitle state; `None` unless session.start asked.
    stable: StablePrefixTracker | None = None
    stable_revision: int = 0
    stable_end_sample: int = 0
    #: Context and replacement rules are copied when the segment opens so all
    #: previews and its final use one immutable recognition plan.
    system_prompt: str | None = None
    context_domain: str | None = None
    replacements: tuple[ReplacementRule, ...] = ()
    #: Singing/speech label (docs/04 `segment.audio_class`); `None` when the
    #: feature is off, unavailable or has not decided yet.
    labeler: SegmentLabeler | None = None
    audio_class: str | None = None
    #: Set when the segment closes; the labeler then finishes at this sample.
    end_sample: int | None = None
    #: A bounded snapshot immediately before `start_sample`, captured for a
    #: possible final-only carry after the preceding segment is finalized.
    carry_pcm: bytes = b""


@dataclass(slots=True)
class ClosedSegment:
    segment: Segment
    pcm: bytes
    end_sample: int
    boundary: str
    #: `time.monotonic()` when the segment closed, for the final's latency.
    closed_at: float = 0.0


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
        translation: StreamTranslationProvider | None = None,
        capture_root: Path | None = None,
        dictionaries: ContextDictionaryStore | None = None,
        singing: SingingRuntime | None = None,
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
        self._singing_counts = {"speech": 0, "singing": 0}
        #: Segments whose singing label is still undecided or unrevised.
        self._labeling: list[Segment] = []
        self._singing: SessionSinging | None = (
            SessionSinging(
                singing, on_frames=self._poll_singing, on_failure=self._singing_failed
            )
            if singing is not None
            else None
        )
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
        #: Wall time the worker spent on the last preview, excluding queueing;
        #: feeds the load guard in `_preview_gap_s`.
        self._preview_last_decode_s = 0.0
        #: Fires `_maybe_schedule_preview` when the interval gate opens, so the
        #: cadence does not depend on the client's frame size.
        self._preview_timer: asyncio.TimerHandle | None = None
        self._preview_min_audio_samples = config.preview_min_audio_ms * 16
        self._preview_min_interval_s = config.preview_min_interval_ms / 1000
        self._preview_load_factor = config.preview_load_factor
        self._previous_final: tuple[int, str] | None = None
        self._audio_history = bytearray()
        self._audio_history_start_sample = 0
        self._audio_history_max_samples = (
            round((config.carry_context_s + CARRY_HISTORY_LOOKBACK_S) * SAMPLE_RATE)
            + MAX_FRAME_PCM_BYTES // 2
            if config.carry_context_s > 0
            else 0
        )
        self._closing = False
        self._control_acks: dict[str, str] = {}
        #: Opt-in translation (docs/04「翻譯（opt-in）」). `None` unless the
        #: server enabled a provider *and* this session asked for it.
        self._translation_provider = translation
        self._translation_reserved = False
        self._translator: SessionTranslator | None = None
        #: LocalAgreement-n for `transcript.stable`; `None` means never sent.
        self._stable_agreement: int | None = None
        #: The continuous segmenter's end-of-segment silence actually in
        #: effect; `None` for utterance sessions, which have no segmenter.
        self._end_silence_ms: int | None = None
        #: Per-session diagnostics log (`tea_asr.diagnostics`): lifecycle,
        #: 5 s audio/VAD heartbeat, rate-limited warnings.
        busy = getattr(scheduler, "busy_seconds", None)
        self._diag = SessionDiagnostics(
            self._state.session_id,
            vad_threshold=SegmenterConfig().threshold,
            busy_seconds=(
                (lambda: float(scheduler.busy_seconds))  # type: ignore[attr-defined]
                if isinstance(busy, int | float)
                else None
            ),
        )
        self._diag_task: asyncio.Task[None] | None = None
        #: Where the opt-in debug audio capture goes; `None` means off.
        self._capture_root = capture_root
        self._capture: AudioCapture | None = None
        self._capture_minutes = config.debug_capture_minutes
        self._dictionaries = dictionaries
        self._context_plan: ContextPlan | None = None
        self._context_active = False
        self._end_reason: str | None = None
        self._end_fields: dict[str, Any] = {}
        self._ended_logged = False

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
        if start.context is not None:
            if not self._config.context_hints_enabled:
                raise ApiError(
                    "unsupported_option",
                    "recognition context 尚未由 server 啟用。",
                )
            try:
                self._context_plan = resolve_context(start.context, self._dictionaries)
            except (InvalidDictionary, UnknownDictionaryProfile) as exc:
                raise ApiError(
                    "unsupported_option",
                    "server dictionary profile is unavailable or invalid.",
                ) from exc
            self._context_active = True
            if self._context_plan.hotwords_truncated:
                log_event(
                    logger,
                    "context.hotwords_truncated",
                    level="warning",
                    input_count=len(self._context_plan.hotwords)
                    + self._context_plan.hotwords_truncated,
                    kept_count=len(self._context_plan.hotwords),
                    truncated_count=self._context_plan.hotwords_truncated,
                )
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
        if start.stable is not None and start.transcript_mode != "revisable":
            raise ApiError(
                "unsupported_option",
                "stable 由 revisable 預覽推導，需要 transcript_mode=revisable。",
            )
        if start.segmentation is not None and start.profile != "continuous":
            raise ApiError(
                "unsupported_option",
                "segmentation 只調整 VAD 切段，需要 profile=continuous。",
            )
        if self._model_state != "ready":
            raise ApiError(
                "model_loading" if self._model_state == "loading" else "model_unavailable",
                f"模型目前狀態為 {self._model_state}。",
            )
        if start.translation is not None:
            self._admit_translation()
        return start

    def _admit_translation(self) -> None:
        """Reserve the translation provider or refuse the session.start.

        A session that asked for translation and cannot have it is told so
        up front; it is never started as a silent ASR-only session.
        """

        provider = self._translation_provider
        if provider is None:
            raise ApiError(
                "unsupported_option",
                "這個服務沒有啟用翻譯 provider（translation_enabled=false）。",
            )
        error = provider.availability_error()
        if error is not None:
            raise error
        if not provider.try_acquire(self):
            raise ApiError(
                "translation_unavailable",
                "翻譯 provider 一次只服務一個 session，目前已有其他 session 在使用。",
                retryable=True,
            )
        self._translation_reserved = True

    def _preview_policy(self) -> PreviewPolicy | None:
        if self._transcript_mode != "revisable":
            return None
        continuous = self._profile == "continuous"
        return PreviewPolicy(
            min_audio_ms=self._preview_min_audio_samples // 16,
            min_interval_ms=round(self._preview_min_interval_s * 1000),
            max_preview_audio_ms=PREVIEW_MAX_AUDIO_SAMPLES // 16,
            endpoint_silence_ms=self._end_silence_ms,
            max_segment_ms=(
                SegmenterConfig().hard_cap_ms
                if continuous
                else MAX_UTTERANCE_PCM_BYTES // 2 // 16
            ),
            context_biasing=self._context_active,
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

    def _remember_audio(self, start_sample: int, pcm: bytes) -> None:
        """Keep only enough recent PCM to snapshot a future segment's prefix."""

        if self._audio_history_max_samples <= 0 or not pcm:
            return
        history_end = self._audio_history_start_sample + len(self._audio_history) // 2
        if not self._audio_history:
            self._audio_history_start_sample = start_sample
        elif start_sample != history_end:
            # The wire validator normally guarantees a continuous clock. Reset
            # rather than accidentally joining two unrelated sample ranges.
            self._audio_history.clear()
            self._audio_history_start_sample = start_sample
        self._audio_history.extend(pcm)
        max_bytes = self._audio_history_max_samples * 2
        if len(self._audio_history) > max_bytes:
            remove_bytes = len(self._audio_history) - max_bytes
            remove_bytes -= remove_bytes % 2
            del self._audio_history[:remove_bytes]
            self._audio_history_start_sample += remove_bytes // 2

    def _audio_before(self, end_sample: int) -> bytes:
        """Return up to `carry_context_s` samples ending at an absolute sample."""

        if not self._audio_history or self._config.carry_context_s <= 0:
            return b""
        history_end = self._audio_history_start_sample + len(self._audio_history) // 2
        end_sample = min(end_sample, history_end)
        start_sample = max(
            self._audio_history_start_sample,
            end_sample - round(self._config.carry_context_s * SAMPLE_RATE),
        )
        if end_sample <= start_sample:
            return b""
        start_offset = (start_sample - self._audio_history_start_sample) * 2
        end_offset = (end_sample - self._audio_history_start_sample) * 2
        return bytes(self._audio_history[start_offset:end_offset])

    def _open_segment(self, start_sample: int, *, cause: str = "vad") -> Segment:
        context = self._context_plan
        segment = Segment(
            segment_id=str(uuid.uuid4()),
            index=self._state.next_index,
            start_sample=start_sample,
            system_prompt=(
                context.system_prompt
                if context and self._config.context_prompt_enabled
                else None
            ),
            context_domain=context.domain if context else None,
            replacements=context.replacements if context else (),
            carry_pcm=self._audio_before(start_sample),
        )
        if self._stable_agreement is not None:
            segment.stable = StablePrefixTracker(self._stable_agreement)
        if self._singing is not None and self._singing.failed is None:
            segment.labeler = SegmentLabeler(self._singing.tracker, start_sample)
            self._labeling.append(segment)
        self._state.next_index += 1
        self._state.segment = segment
        self._diag.speech_started(
            segment_index=segment.index, start_sample=start_sample, cause=cause
        )
        return segment

    def _singing_failed(self, reason: str) -> None:
        """The labeller stopped; transcription is unaffected."""

        log_event(logger, "singing.disabled", session_id=self._state.session_id, reason=reason)
        self._labeling.clear()

    def _poll_singing(self) -> None:
        """Emit any label that has become due; called when audio or frames advance."""

        singing = self._singing
        if singing is None or singing.failed is not None:
            return
        if singing.tracker.last_score is not None:
            self._diag.singing_score = singing.tracker.last_score
        sample = self._state.next_sample
        for segment in list(self._labeling):
            labeler = segment.labeler
            if labeler is None:
                self._labeling.remove(segment)
                continue
            if segment.end_sample is None:
                decision = labeler.update(sample)
            else:
                decision = labeler.update(segment.end_sample, closing=True)
            if decision is not None:
                self._emit_audio_class(segment, decision)
            if labeler.finished:
                self._labeling.remove(segment)

    def _emit_audio_class(self, segment: Segment, decision: AudioClassDecision) -> None:
        if segment.audio_class is not None:
            self._singing_counts[segment.audio_class] -= 1
        segment.audio_class = decision.audio_class
        self._singing_counts[decision.audio_class] += 1
        self._writer.emit(
            SegmentAudioClass(
                session_id=self._state.session_id,
                event_id=0,
                segment_id=segment.segment_id,
                segment_index=segment.index,
                class_=decision.audio_class,
                confidence=decision.confidence,
                revision=decision.revision,
            )
        )
        self._diag.audio_class(**self._singing_counts)

    async def _finish_audio_class(self, segment: Segment) -> None:
        """Settle the label before the final goes out, so the final carries it."""

        if segment.labeler is None or self._singing is None:
            return
        await self._singing.settle()
        self._poll_singing()
        # Whatever is still undecided (labeller failed) stays unlabelled.
        segment.labeler = None
        if segment in self._labeling:
            self._labeling.remove(segment)

    def _enqueue(self, segment: Segment, pcm: bytes, end_sample: int, boundary: str) -> None:
        segment.terminal = True
        segment.end_sample = end_sample
        self._diag.segment_closed(
            segment_index=segment.index,
            start_sample=segment.start_sample,
            end_sample=end_sample,
            boundary=boundary,
        )
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
                ClosedSegment(
                    segment=segment,
                    pcm=pcm,
                    end_sample=end_sample,
                    boundary=boundary,
                    closed_at=time.monotonic(),
                )
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
                self._segment_done(closed, "error", code="internal_error")
            finally:
                self._pending.task_done()

    def _apply_pua_filter(
        self,
        text: str,
        *,
        segment: Segment,
        kind: str,
        repetition_trims: list[TrimmedRepetition] | None = None,
    ) -> str:
        """Apply output filters without changing the preserved raw transcript.

        Never logs `text`/`raw_text` (docs/03 forbids transcript content in
        the log; `JsonFormatter` also strips those keys as a second layer).
        Only a character count is observable, so the filter's impact can be
        monitored without leaking what was said.
        """

        filtered = filter_private_use_characters(text) if self._config.filter_pua else text
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
        filtered, trims = trim_repetitions(
            filtered,
            single_char_limit=self._config.repetition_single_char_limit,
            multi_char_limit=self._config.repetition_multi_char_limit,
        )
        if repetition_trims is not None:
            repetition_trims.extend(trims)
        for trim in trims:
            log_event(
                logger,
                "stream.repetition_trimmed",
                session_id=self._state.session_id,
                segment_id=segment.segment_id,
                segment_index=segment.index,
                kind=kind,
                unit_length=trim.unit_length,
                removed_chars=trim.removed_chars,
            )
        return filtered

    @staticmethod
    def _apply_context_replacements(text: str, segment: Segment) -> tuple[str, int]:
        replaced, matches = apply_replacements(text, segment.replacements)
        if matches:
            # Log counts only: dictionary terms and recognized text stay private.
            log_event(logger, "stream.replacements_applied", level="info", matches=matches)
        return replaced, matches

    @staticmethod
    def _log_context_prompt_tokens(
        response: dict[str, Any], segment: Segment, kind: str
    ) -> None:
        prompt_tokens = response.get("prompt_tokens")
        if segment.system_prompt is not None and prompt_tokens is not None:
            log_event(
                logger,
                "stream.context_prompt_tokens",
                segment_index=segment.index,
                kind=kind,
                prompt_tokens=int(prompt_tokens),
            )

    async def _transcribe_segment(self, closed: ClosedSegment) -> None:
        segment = closed.segment
        state = self._state
        previous_final = self._previous_final
        carry_pcm = b""
        if previous_final is not None and segment.carry_pcm:
            gap_samples = segment.start_sample - previous_final[0]
            if 0 <= gap_samples <= round(self._config.carry_context_max_gap_s * SAMPLE_RATE):
                carry_pcm = segment.carry_pcm

        decode_pcm = carry_pcm + closed.pcm
        carried = bool(carry_pcm and previous_final is not None and previous_final[1])
        carry_warning: str | None = None
        carry_stripped_text: str | None = None

        async def transcribe(pcm: bytes) -> tuple[dict[str, Any], int]:
            return await self._scheduler.transcribe(
                pcm,
                language=self._language,
                kind="realtime" if self._profile == "continuous" else "interactive",
                **(
                    {"system_prompt": segment.system_prompt}
                    if segment.system_prompt is not None
                    else {}
                ),
            )

        try:
            response, queue_ms = await transcribe(decode_pcm)
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
            self._segment_done(closed, "error", code=exc.code)
            return
        if state.cancelled:
            return
        self._log_context_prompt_tokens(response, segment, "final")
        inference_ms = round(float(response.get("total_time_s", 0.0)) * 1000)
        raw_text = str(response["text"])

        if carried:
            candidate_text = (
                filter_private_use_characters(raw_text) if self._config.filter_pua else raw_text
            )
            candidate_text, _ = trim_repetitions(
                candidate_text,
                single_char_limit=self._config.repetition_single_char_limit,
                multi_char_limit=self._config.repetition_multi_char_limit,
            )
            overlap = strip_carried_overlap(previous_final[1], candidate_text)
            if overlap is not None and overlap[0].strip():
                carry_warning = "carry_overlap_stripped"
                carry_stripped_text = overlap[0]
                log_event(
                    logger,
                    "stream.carry_overlap_stripped",
                    segment_index=segment.index,
                    carry_audio_ms=len(carry_pcm) // 32,
                    overlap_chars=overlap[1],
                )
            else:
                try:
                    response, retry_queue_ms = await transcribe(closed.pcm)
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
                    self._segment_done(closed, "error", code=exc.code)
                    return
                if state.cancelled:
                    return
                self._log_context_prompt_tokens(response, segment, "final")
                queue_ms += retry_queue_ms
                inference_ms += round(float(response.get("total_time_s", 0.0)) * 1000)
                raw_text = str(response["text"])
                carry_warning = "carry_overlap_uncertain"
                log_event(
                    logger,
                    "stream.carry_overlap_uncertain",
                    segment_index=segment.index,
                    carry_audio_ms=len(carry_pcm) // 32,
                )

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
            self._segment_done(
                closed, "no_speech", queue_ms=queue_ms, inference_ms=inference_ms, chars=0
            )
            return
        warnings = private_use_warnings(raw_text)
        repetition_trims: list[TrimmedRepetition] = []
        text = self._apply_pua_filter(
            raw_text,
            segment=segment,
            kind="final",
            repetition_trims=repetition_trims,
        )
        if carry_warning == "carry_overlap_stripped":
            assert carry_stripped_text is not None
            text = carry_stripped_text
        if repetition_trims:
            warnings.append("repetition_trimmed")
        if carry_warning is not None:
            warnings.append(carry_warning)
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
            self._segment_done(
                closed, "empty", queue_ms=queue_ms, inference_ms=inference_ms, chars=0
            )
            return
        text, replacement_count = self._apply_context_replacements(text, segment)
        if replacement_count:
            warnings.append("replacements_applied")
        if contains_context_echo(raw_text, segment.context_domain):
            warnings.append("context_echo")
        await self._finish_audio_class(segment)
        if state.cancelled:
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
                inference_ms=inference_ms,
                warnings=warnings,
                audio_class=segment.audio_class,
            )
        )
        stable_state: str | None = None
        if segment.stable is not None:
            # Right after the final, never instead of it (docs/06 #5).
            update = segment.stable.finalize(text)
            stable_state = update.state
            self._emit_stable(
                segment,
                update,
                source_revision=segment.revision + 1,
                end_sample=closed.end_sample,
            )
        self._diag.segment_done(
            segment_index=segment.index,
            outcome="final",
            latency_ms=round((time.monotonic() - closed.closed_at) * 1000),
            queue_ms=queue_ms,
            inference_ms=inference_ms,
            chars=len(text),
            stable_state=stable_state,
        )
        self._previous_final = (closed.end_sample, text)
        if self._translator is not None:
            # After the final is queued, never instead of it: translation only
            # reads the text the client already has (docs/06 #5).
            self._translator.submit(segment.segment_id, text)

    def _segment_done(
        self,
        closed: ClosedSegment,
        outcome: str,
        *,
        queue_ms: int | None = None,
        inference_ms: int | None = None,
        chars: int | None = None,
        code: str | None = None,
    ) -> None:
        """Close the stable line and log a segment that ended without a final."""

        segment = closed.segment
        self._previous_final = None
        self._abandon_stable(segment)
        self._diag.segment_done(
            segment_index=segment.index,
            outcome=outcome,
            latency_ms=round((time.monotonic() - closed.closed_at) * 1000),
            queue_ms=queue_ms,
            inference_ms=inference_ms,
            chars=chars,
            stable_state="abandoned" if segment.stable is not None else None,
            code=code,
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
        self._diag.on_frame(pcm)
        if self._singing is not None:
            self._singing.push(start_sample, pcm)
        if self._capture is not None:
            self._capture.write(start_sample, pcm)
        # The ring is bounded to the configured left-context window plus the
        # segmenter's pre-roll. It is only used to assemble later final audio.
        self._remember_audio(start_sample, pcm)

        if self._profile == "continuous":
            self._advance_continuous(pcm)
        else:
            if len(state.pcm) + len(pcm) > MAX_UTTERANCE_PCM_BYTES:
                raise ApiError("payload_too_large", "單一 utterance 不得超過 30 秒。")
            if state.segment is None:
                self._open_segment(start_sample, cause="utterance")
            state.pcm.extend(pcm)

        state.next_seq += 1
        state.next_sample = end_sample
        self._poll_singing()
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
                    reopened = self._open_segment(event.end_sample, cause="split")
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

    def _preview_gap_s(self) -> float:
        """Shortest allowed time between two preview starts in this session.

        `k × last decode` keeps one session's previews at most 1/k of the
        worker's time however long the segment grows; the fixed floor covers
        the cheap early previews.
        """

        return max(
            self._preview_min_interval_s,
            self._preview_load_factor * self._preview_last_decode_s,
        )

    def _preview_retry_at(self, delay_s: float) -> None:
        if self._preview_timer is not None:
            return
        loop = asyncio.get_running_loop()

        def fire() -> None:
            self._preview_timer = None
            self._maybe_schedule_preview()

        self._preview_timer = loop.call_later(delay_s, fire)

    def _cancel_preview_timer(self) -> None:
        if self._preview_timer is not None:
            self._preview_timer.cancel()
            self._preview_timer = None

    def _maybe_schedule_preview(self) -> None:
        if self._transcript_mode != "revisable" or self._closing or self._state.cancelled:
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
        if samples - segment.published_preview_end < self._preview_min_audio_samples:
            return
        if self._preview_task is not None:
            self._preview_pending = True
            return
        loop = asyncio.get_running_loop()
        gap_s = self._preview_gap_s()
        wait_s = self._preview_last_started + gap_s - loop.time()
        if wait_s > 0:
            self._preview_pending = True
            if self._preview_timer is None and gap_s > self._preview_min_interval_s:
                # The load guard, not the fixed floor, is what holds this one.
                self._diag.preview_deferred(
                    segment_index=segment.index,
                    wait_ms=round(wait_s * 1000),
                    gap_ms=round(gap_s * 1000),
                )
            self._preview_retry_at(wait_s)
            return
        self._cancel_preview_timer()
        self._preview_last_started = loop.time()
        self._preview_task = asyncio.create_task(self._run_preview(pcm, end_sample, segment))

    def _preview_is_stale(self, segment: Segment) -> bool:
        return segment.terminal or self._state.cancelled

    async def _run_preview(self, snapshot: bytes, end_sample: int, segment: Segment) -> None:
        loop = asyncio.get_running_loop()
        audio_ms = len(snapshot) // 32
        try:
            started = loop.time()
            response, queue_ms = await self._scheduler.transcribe(
                snapshot,
                language=self._language,
                kind="preview",
                is_stale=lambda: self._preview_is_stale(segment),
                **(
                    {"system_prompt": segment.system_prompt}
                    if segment.system_prompt is not None
                    else {}
                ),
            )
            self._preview_last_decode_s = max(0.0, loop.time() - started - queue_ms / 1000)
            decode_ms = round(self._preview_last_decode_s * 1000)
            if (
                segment.terminal
                or self._state.cancelled
                or end_sample - segment.start_sample <= segment.published_preview_end
            ):
                self._diag.preview_done(
                    outcome="stale",
                    segment_index=segment.index,
                    decode_ms=decode_ms,
                    audio_ms=audio_ms,
                    queue_ms=queue_ms,
                )
                return
            self._diag.preview_done(
                outcome="published",
                segment_index=segment.index,
                decode_ms=decode_ms,
                audio_ms=audio_ms,
                queue_ms=queue_ms,
            )
            self._log_context_prompt_tokens(response, segment, "partial")
            segment.revision += 1
            segment.published_preview_end = end_sample - segment.start_sample
            text = self._apply_pua_filter(
                str(response["text"]), segment=segment, kind="partial"
            )
            text, replacement_count = self._apply_context_replacements(text, segment)
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
                    warnings=(
                        ["replacements_applied"] if replacement_count else []
                    ),
                )
            )
            if segment.stable is not None:
                update = segment.stable.observe(text)
                if update is not None:
                    self._emit_stable(
                        segment, update, source_revision=segment.revision, end_sample=end_sample
                    )
        except StaleTaskDropped:
            # The segment closed while this preview was still queued; its final
            # is (or will be) ahead of it, so there is nothing to publish.
            self._diag.preview_done(
                outcome="dropped", segment_index=segment.index, audio_ms=audio_ms
            )
            return
        except ApiError as exc:
            self._diag.preview_done(
                outcome="failed", segment_index=segment.index, audio_ms=audio_ms
            )
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
            self._diag.preview_done(
                outcome="failed", segment_index=segment.index, audio_ms=audio_ms
            )
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
            # Re-check even when this preview's segment has closed: the next
            # segment may have asked for a preview while this one was running.
            if self._preview_pending:
                self._preview_pending = False
                self._maybe_schedule_preview()

    # -- stable prefix (opt-in) ------------------------------------------------

    def _emit_stable(
        self, segment: Segment, update: StableUpdate, *, source_revision: int, end_sample: int
    ) -> None:
        segment.stable_revision += 1
        segment.stable_end_sample = end_sample
        self._writer.emit(
            TranscriptStable(
                session_id=self._state.session_id,
                event_id=0,
                segment_id=segment.segment_id,
                segment_index=segment.index,
                stable_revision=segment.stable_revision,
                source_revision=source_revision,
                start_sample=segment.start_sample,
                end_sample=end_sample,
                text=update.text,
                state=update.state,
                diverged_chars=update.diverged_chars,
            )
        )

    def _abandon_stable(self, segment: Segment) -> None:
        """Close a segment's stable line when it ends without a final.

        Only a segment whose stable text was already shown needs closing; one
        that never committed anything stays silent, like before.
        """

        tracker = segment.stable
        if tracker is None or tracker.closed:
            return
        update = tracker.abandon()
        if segment.stable_revision == 0:
            return
        self._emit_stable(
            segment,
            update,
            source_revision=segment.revision,
            end_sample=segment.stable_end_sample,
        )

    def _settle_preview_soon(self, segment: Segment) -> None:
        """Mark the segment closed so an in-flight preview discards its result."""

        segment.terminal = True
        self._preview_pending = False
        self._drop_stale_previews()

    def _drop_stale_previews(self) -> None:
        # A preview of a closed segment that is still queued is removed now,
        # not run after the final (docs/07 step 6).
        drop = getattr(self._scheduler, "drop_stale", None)
        if drop is not None:
            drop()

    async def _settle_preview(self) -> None:
        self._preview_pending = False
        self._cancel_preview_timer()
        self._drop_stale_previews()
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
        if start.stable is not None:
            self._stable_agreement = start.stable.agreement
        self._control_acks[start.request_id] = "session.start:"
        if self._profile == "continuous":
            assert self._vad is not None
            self._end_silence_ms = _end_silence_ms(start)
            self._segmenter = ContinuousSegmenter(
                self._vad,
                SegmenterConfig(end_silence_ms=self._end_silence_ms),
                on_window=self._diag.on_vad_window,
            )
        if self._capture_root is not None:
            await self._start_capture()
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
                context=(
                    ContextEcho(
                        profile=self._context_plan.profile,
                        domain_chars=len(self._context_plan.domain or ""),
                        hotwords_count=len(self._context_plan.hotwords),
                        replacements_count=len(self._context_plan.replacements),
                        prompt_applied=bool(
                            self._config.context_prompt_enabled
                            and self._context_plan.system_prompt
                        ),
                    )
                    if self._context_plan is not None
                    else None
                ),
            )
        )
        if start.translation is not None and self._translation_provider is not None:
            self._translator = SessionTranslator(
                self._translation_provider,
                self._writer.emit,
                session_id=self._state.session_id,
                direction=start.translation.direction,
                latency_mode=start.translation.latency_mode,
            )
            self._writer.emit(
                TranslationStarted(
                    session_id=self._state.session_id,
                    event_id=0,
                    request_id=start.request_id,
                    direction=start.translation.direction,
                    latency_mode=start.translation.latency_mode,
                    model=self._translation_provider.model,
                    model_revision=self._translation_provider.model_revision,
                )
            )
            self._translator.start()
        self._log_session_started(start)
        self._diag_task = asyncio.create_task(self._diagnostics_loop())

        while True:
            try:
                message = await asyncio.wait_for(
                    self._websocket.receive(), timeout=IDLE_TIMEOUT_S
                )
            except TimeoutError as exc:
                # Letting the bare TimeoutError escape made `run_stream` drop
                # the session on the `except (WebSocketDisconnect, TimeoutError)`
                # branch without emitting anything, so a client whose capture
                # had stalled was never told and kept showing "listening".
                # docs/06 #4 requires the failure to be visible.
                raise ApiError(
                    "idle_timeout",
                    f"已經 {int(IDLE_TIMEOUT_S)} 秒沒有收到任何音訊或控制訊息，session 已結束。",
                ) from exc
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

    # -- diagnostics -----------------------------------------------------------

    async def _start_capture(self) -> None:
        assert self._capture_root is not None
        try:
            self._capture = await asyncio.to_thread(
                AudioCapture,
                self._capture_root,
                self._state.session_id,
                minutes=self._capture_minutes,
            )
        except OSError as exc:
            # Diagnostics must never cost the user their session.
            self._diag.emit(
                "stream.capture_unavailable", level="warning", error=type(exc).__name__
            )

    def _log_session_started(self, start: SessionStart) -> None:
        revisable = self._transcript_mode == "revisable"
        headers = getattr(self._websocket, "headers", None)
        user_agent = headers.get("user-agent") if headers is not None else None
        capture = self._capture
        self._diag.session_started(
            profile=self._profile,
            transcript_mode=self._transcript_mode,
            language=self._language,
            stable_agreement=self._stable_agreement,
            end_silence_ms=self._end_silence_ms,
            preview_min_audio_ms=self._preview_min_audio_samples // 16 if revisable else None,
            preview_min_interval_ms=(
                round(self._preview_min_interval_s * 1000) if revisable else None
            ),
            preview_load_factor=self._preview_load_factor if revisable else None,
            translation=start.translation.direction if start.translation is not None else None,
            user_agent=user_agent[:80] if user_agent else None,
            capture_dir=str(capture.directory) if capture is not None else None,
            capture_max_mb=round(capture.max_bytes / 1_000_000, 1) if capture else None,
        )

    def _diagnostics_state(self) -> dict[str, Any]:
        segment = self._state.segment
        return {
            "seg_state": self._segmenter.state if self._segmenter is not None else None,
            "segment_open": segment is not None and not segment.terminal,
            "pending_segments": self._pending.qsize(),
            "singing_score": self._diag.singing_score,
        }

    async def _diagnostics_loop(self) -> None:
        while not self._closing:
            await asyncio.sleep(TICK_S)
            try:
                self._diag.tick(**self._diagnostics_state())
            except Exception:  # diagnostics must never end a session
                logger.exception("stream.diagnostics_failed")
                return

    def note_end(self, reason: str, **fields: Any) -> None:
        """Record why the session ends; the first reason given wins."""

        self._diag.receiving = False
        if self._end_reason is None:
            self._end_reason = reason
            self._end_fields = fields

    def _log_session_ended(self) -> None:
        if self._ended_logged:
            return
        self._ended_logged = True
        self._diag.capture_dropped_frames = (
            self._capture.dropped_frames if self._capture is not None else 0
        )
        self._diag.session_ended(
            reason=self._end_reason or "connection_closed", **self._end_fields
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
            self.note_end("stopped")
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
                    self._abandon_stable(pending.segment)
                    self._pending.task_done()
            if self._translator is not None:
                await self._translator.drain()
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
            self.note_end("cancelled")
            state.cancelled = True
            if state.segment is not None:
                state.segment.terminal = True
            await self._settle_preview()
            await self._close_translation()
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
        self.note_end(f"error:{error.code}")
        self._state.cancelled = True
        if self._state.segment is not None:
            self._state.segment.terminal = True
        await self._settle_preview()
        await self._close_translation()
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

    async def _close_translation(self) -> None:
        if self._translator is not None:
            await self._translator.close()

    def release_translation(self) -> None:
        """Hand the translation provider back for the next session."""

        if self._translation_reserved and self._translation_provider is not None:
            self._translation_provider.release(self)
        self._translation_reserved = False

    async def shutdown(self) -> None:
        # Logged first and synchronously: teardown can be cancelled part way.
        self._log_session_ended()
        if self._diag_task is not None:
            self._diag_task.cancel()
        try:
            await self._close_translation()
        finally:
            # Only after the translator task is gone, so the next session
            # cannot interleave with this one's last request.
            self.release_translation()
        self._closing = True
        self._cancel_preview_timer()
        for task in (self._consumer, self._preview_task):
            if task is not None:
                task.cancel()
        tasks = [
            task
            for task in (self._consumer, self._preview_task, self._diag_task)
            if task is not None
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._singing is not None:
            await self._singing.close()
        capture, self._capture = self._capture, None
        if capture is not None:
            await asyncio.to_thread(capture.close)

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


def _end_silence_ms(start: SessionStart) -> int:
    """End-of-segment silence for a continuous session.

    Only this one segmenter field is client-tunable; everything else keeps the
    `SegmenterConfig` defaults.
    """

    if start.segmentation is not None:
        return start.segmentation.end_silence_ms
    if start.transcript_mode == "revisable":
        return REVISABLE_END_SILENCE_MS
    return SegmenterConfig().end_silence_ms


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

#: Connection rejections before a session exists are logged at most once per
#: reason per 30 s, so a misconfigured client retrying in a loop (or a
#: scanner) cannot flood the log; the skipped count is on the next line.
_rejection_limiter = WarningLimiter()


def _log_rejection(reason: str, client: str) -> None:
    suppressed = _rejection_limiter.allow(reason, time.monotonic())
    if suppressed is None:
        return
    log_event(
        logger, "stream.connection_rejected", reason=reason, client=client, suppressed=suppressed
    )


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
    translation: StreamTranslationProvider | None = None,
    capture_root: Path | None = None,
    dictionaries: ContextDictionaryStore | None = None,
    singing: SingingRuntime | None = None,
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
        _log_rejection("rate_limited", client_key)
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
        _log_rejection("unauthenticated", client_key)
        await websocket.close(code=1008, reason="unauthenticated")
        return
    if rate_limiter is not None:
        rate_limiter.record_success(client_key)

    origin = websocket.headers.get("origin")
    if origin and not _origin_allowed(origin):
        _log_rejection("forbidden_origin", client_key)
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
        _log_rejection("session_limit", client_key)
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
        translation=translation,
        capture_root=capture_root,
        dictionaries=dictionaries,
        singing=singing,
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
    except WebSocketDisconnect as exc:
        session.note_end("client_disconnect", close_code=exc.code)
    except TimeoutError:
        session.note_end("timeout")
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
