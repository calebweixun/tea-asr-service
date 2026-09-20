from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: docs/04-api.md keeps every sample/seq integer inside the JavaScript safe range.
MAX_SAFE_INT = (2**53) - 1

SAMPLE_RATE = 16_000
MAX_UTTERANCE_MS = 30_000
MAX_UTTERANCE_PCM_BYTES = 960_000
MAX_FRAME_PCM_BYTES = 6_400
FRAME_HEADER_BYTES = 16
INITIAL_FLOW_WINDOW_SAMPLES = 80_000

#: docs/03-architecture.md: the service only binds 127.0.0.1, so both the HTTP
#: Host allowlist and the WS Origin allowlist are anchored to these two
#: hostnames. A native client sending no Host/Origin override at all is a
#: different case, handled at each call site.
ALLOWED_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})
ALLOWED_WS_ORIGINS = frozenset(f"http://{host}" for host in ALLOWED_LOCAL_HOSTS)

TimestampQuality = Literal["segment"]
Boundary = Literal["manual", "silence", "max_duration", "stop"]


class ClientModel(BaseModel):
    """Client payloads reject unknown fields instead of silently ignoring them."""

    model_config = ConfigDict(extra="forbid")


class ServerModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AudioFormat(ClientModel):
    sample_rate: Literal[16_000]
    channels: Literal[1]
    format: Literal["pcm_s16le"]


# --- HTTP -------------------------------------------------------------------


class ErrorBody(ServerModel):
    code: str
    message: str
    retryable: bool
    request_id: str | None = None


class ErrorEnvelope(ServerModel):
    error: ErrorBody


class Segment(ServerModel):
    segment_id: str
    start_sample: int = Field(ge=0, le=MAX_SAFE_INT)
    end_sample: int = Field(ge=0, le=MAX_SAFE_INT)
    text: str
    timestamp_quality: TimestampQuality = "segment"


class TranscriptionResponse(ServerModel):
    request_id: str
    model_revision: str
    text: str
    raw_text: str
    language: str
    segments: list[Segment]
    audio_ms: int
    queue_ms: int
    inference_ms: int
    warnings: list[str] = Field(default_factory=list)


class CapabilityAudio(ServerModel):
    sample_rate: int = SAMPLE_RATE
    channels: int = 1
    format: str = "pcm_s16le"


class CapabilityFeatures(ServerModel):
    native_audio_streaming: bool = False
    partial_transcripts: bool = False
    word_timestamps: bool = False
    translation: bool = False
    diarization: bool = False
    hotwords: bool = False
    context_biasing: bool = False
    durable_sessions: bool = False
    durable_revisable: bool = False
    batch_jobs: bool = False


class CapabilityLimits(ServerModel):
    max_frame_pcm_bytes: int = MAX_FRAME_PCM_BYTES
    max_utterance_ms: int = MAX_UTTERANCE_MS
    #: The value the server actually enforces (`/v1/stream` rejects an
    #: additional `continuous` session past this with `concurrent_session_limit`
    #: once it is reached), not a document-derived guess — see
    #: docs/benchmarks/concurrency-report.md and `ServiceConfig.max_continuous_sessions`.
    max_continuous_sessions: int = 2
    max_total_connections: int = 4


class Capabilities(ServerModel):
    protocol_version: str
    audio: CapabilityAudio = Field(default_factory=CapabilityAudio)
    profiles: list[str]
    features: CapabilityFeatures
    limits: CapabilityLimits


class StatusResponse(ServerModel):
    model_state: str
    model: str
    model_revision: str
    worker_generation: int
    worker_load_ms: int | None
    last_error: str | None
    idle_s: int
    active_sessions: int
    queue: QueueStatus


class QueueStatus(ServerModel):
    waiting_tasks: int
    waiting_samples: int
    max_waiting_tasks: int
    max_waiting_samples: int


# --- WebSocket: client control events ---------------------------------------


class SessionStart(ClientModel):
    type: Literal["session.start"]
    request_id: str = Field(min_length=1, max_length=64)
    profile: Literal["utterance", "continuous"]
    audio: AudioFormat
    language: str = "Chinese"
    durable: bool = False
    transcript_mode: Literal["final_only", "revisable"] = "final_only"


class AudioCommit(ClientModel):
    type: Literal["audio.commit"]
    request_id: str = Field(min_length=1, max_length=64)
    through_seq: int = Field(ge=0, le=MAX_SAFE_INT)


class SessionStop(ClientModel):
    type: Literal["session.stop"]
    request_id: str = Field(min_length=1, max_length=64)
    through_seq: int | None = Field(default=None, ge=0, le=MAX_SAFE_INT)


class SessionCancel(ClientModel):
    type: Literal["session.cancel"]
    request_id: str = Field(min_length=1, max_length=64)


class Ping(ClientModel):
    type: Literal["ping"]
    request_id: str = Field(min_length=1, max_length=64)


ClientEvent = Annotated[
    SessionStart | AudioCommit | SessionStop | SessionCancel | Ping,
    Field(discriminator="type"),
]


class ClientEnvelope(BaseModel):
    """Wrapper so a discriminated union can be validated from a raw payload."""

    event: ClientEvent


# --- WebSocket: server events -----------------------------------------------


class Hello(ServerModel):
    type: Literal["hello"] = "hello"
    protocol_version: str
    model_state: str


class PreviewPolicy(ServerModel):
    min_audio_ms: int
    min_interval_ms: int
    max_preview_audio_ms: int
    endpoint_silence_ms: int | None
    max_segment_ms: int
    context_biasing: bool = False


class SessionEvent(ServerModel):
    session_id: str
    event_id: int = Field(ge=0, le=MAX_SAFE_INT)


class SessionStarted(SessionEvent):
    type: Literal["session.started"] = "session.started"
    request_id: str
    profile: str
    transcript_mode: str
    next_seq: int
    next_sample: int
    send_until_sample: int
    preview_policy: PreviewPolicy | None = None


class AudioAck(SessionEvent):
    type: Literal["audio.ack"] = "audio.ack"
    received_seq: int
    received_sample: int
    persisted_seq: int | None = None
    persisted_sample: int | None = None


class SpeechStarted(SessionEvent):
    type: Literal["speech.started"] = "speech.started"
    segment_id: str
    segment_index: int
    start_sample: int


class AudioCommitted(SessionEvent):
    type: Literal["audio.committed"] = "audio.committed"
    request_id: str
    segment_id: str | None
    reason: Literal["committed", "no_audio"]


class SegmentQueued(SessionEvent):
    type: Literal["segment.queued"] = "segment.queued"
    segment_id: str
    segment_index: int
    start_sample: int
    end_sample: int
    boundary: Boundary


class TranscriptPartial(SessionEvent):
    type: Literal["transcript.partial"] = "transcript.partial"
    segment_id: str
    segment_index: int
    revision: int
    start_sample: int
    end_sample: int
    text: str
    timestamp_quality: TimestampQuality = "segment"


class TranscriptFinal(SessionEvent):
    type: Literal["transcript.final"] = "transcript.final"
    segment_id: str
    segment_index: int
    revision: int
    start_sample: int
    end_sample: int
    timestamp_quality: TimestampQuality = "segment"
    text: str
    raw_text: str
    audio_ms: int
    queue_ms: int
    inference_ms: int
    warnings: list[str] = Field(default_factory=list)


class SegmentSkipped(SessionEvent):
    type: Literal["segment.skipped"] = "segment.skipped"
    segment_id: str
    segment_index: int
    reason: Literal["no_speech", "empty"]


class SegmentError(SessionEvent):
    type: Literal["segment.error"] = "segment.error"
    segment_id: str
    segment_index: int
    code: str
    message: str
    retryable: bool


class FlowControl(SessionEvent):
    type: Literal["flow.control"] = "flow.control"
    send_until_sample: int
    reason: Literal["normal", "paused", "recovering"]


class PreviewStatus(SessionEvent):
    type: Literal["preview.status"] = "preview.status"
    state: Literal["active", "paused"]
    reason: Literal["normal", "load", "long_utterance", "backend_error"]


class SessionStopped(SessionEvent):
    type: Literal["session.stopped"] = "session.stopped"
    request_id: str
    last_seq: int | None
    status: Literal["completed", "completed_with_errors"]
    failed_segments: list[int] = Field(default_factory=list)


class SessionCancelled(SessionEvent):
    type: Literal["session.cancelled"] = "session.cancelled"
    request_id: str


class Pong(SessionEvent):
    type: Literal["pong"] = "pong"
    request_id: str


class ErrorEvent(SessionEvent):
    type: Literal["error"] = "error"
    code: str
    message: str
    retryable: bool
    request_id: str | None = None


ServerEvent = Annotated[
    SessionStarted
    | AudioAck
    | SpeechStarted
    | AudioCommitted
    | SegmentQueued
    | TranscriptPartial
    | TranscriptFinal
    | SegmentSkipped
    | SegmentError
    | FlowControl
    | PreviewStatus
    | SessionStopped
    | SessionCancelled
    | Pong
    | ErrorEvent,
    Field(discriminator="type"),
]


class ServerEnvelope(BaseModel):
    event: ServerEvent


def ws_event_schema() -> dict[str, Any]:
    """JSON Schema for every WebSocket message, for client contract tests."""

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "TEA ASR WebSocket events",
        "$defs": {
            "hello": Hello.model_json_schema(ref_template="#/$defs/models/{model}"),
            "client_event": ClientEnvelope.model_json_schema(
                ref_template="#/$defs/models/{model}"
            )["properties"]["event"],
            "server_event": ServerEnvelope.model_json_schema(
                ref_template="#/$defs/models/{model}"
            )["properties"]["event"],
            "models": {
                **ClientEnvelope.model_json_schema(ref_template="#/$defs/models/{model}").get(
                    "$defs", {}
                ),
                **ServerEnvelope.model_json_schema(ref_template="#/$defs/models/{model}").get(
                    "$defs", {}
                ),
                **Hello.model_json_schema(ref_template="#/$defs/models/{model}").get("$defs", {}),
            },
        },
    }
