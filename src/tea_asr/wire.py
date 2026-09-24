from __future__ import annotations

import ipaddress
from collections.abc import Callable
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

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

#: W9: address space trusted enough to accept as a Host/Origin *only when the
#: operator has explicitly opted into LAN mode* (`ServiceConfig.allow_lan`).
#: RFC1918 private ranges plus Tailscale's CGNAT allocation (100.64.0.0/10)
#: and its IPv6 ULA range, so "LAN or Tailscale" (docs/06-handoff.md's LAN
#: row) is the actual boundary, not "any Host header a browser can be made to
#: send" — a public IP or attacker-controlled DNS name must still be
#: rejected in LAN mode, or the DNS-rebinding guard this allowlist exists for
#: would be worthless the moment LAN mode is on.
_TRUSTED_LAN_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",  # Tailscale/CGNAT
        "fd00::/8",  # ULA, covers Tailscale's IPv6 range
        "::1/128",
    )
)


def parse_host_header(value: str) -> str:
    """Strip an optional ``:port`` suffix from a raw ``Host`` header value.

    Handles every shape the header can take: bracketed IPv6 with or without a
    port (``[::1]:8327`` / ``[::1]`` / ``[fd00::1]:8327``), a hostname or IPv4
    literal with or without a port (``localhost:8327`` / ``127.0.0.1``).

    A naive ``value.split(":")[0]`` (the historic bug here) turns
    ``[::1]:8327`` into ``"["`` — IPv6 addresses contain colons themselves, so
    splitting on the first one truncates the whole header instead of removing
    the port. That silently rejected every IPv6 Host, including the IPv6 ULA
    addresses W9's LAN mode is supposed to allow.

    This only strips the port; it does not decide whether the resulting host
    is *allowed* — `make_host_allowlist` still rejects anything not on the
    allowlist, bracket-stripped or not. Shared by the HTTP `Host` check
    (`HostValidationMiddleware`); the WS `Origin` check does not need this
    helper because `urlsplit(origin).hostname` already strips IPv6 brackets
    and the port correctly on its own.
    """

    if value.startswith("["):
        end = value.find("]")
        # No closing bracket: malformed input, not a real bracketed IPv6
        # host. Return it unchanged so the allowlist check rejects it below
        # rather than this function guessing at a repair.
        return value[1:end] if end != -1 else value
    if value.count(":") == 1:
        # Exactly one colon: an ordinary "host:port" or "ipv4:port". A bare
        # (unbracketed) IPv6 literal has two or more colons and falls
        # through unchanged instead, since a real Host header always
        # brackets IPv6 (RFC 3986 §3.2.2) — anything else is malformed and
        # should be left intact for the allowlist to reject.
        return value.rsplit(":", 1)[0]
    return value


def is_trusted_lan_address(host: str) -> bool:
    """True if `host` (no port, no brackets) parses as a private/Tailscale IP.

    A hostname (e.g. a Tailscale MagicDNS name) never matches here; those are
    only trusted when the operator lists them explicitly in
    `ServiceConfig.extra_allowed_hosts`, since this service does no DNS
    resolution or verification of its own to decide whether a *name* really
    points somewhere trustworthy.
    """

    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr in network for network in _TRUSTED_LAN_NETWORKS)


def make_host_allowlist(
    *, allow_lan: bool, extra_hosts: frozenset[str] = frozenset()
) -> Callable[[str], bool]:
    """Build the HTTP `Host` predicate for `HostValidationMiddleware`.

    Default (``allow_lan=False``) behaviour is byte-for-byte the historic
    exact-match check against `ALLOWED_LOCAL_HOSTS`, so opting out of LAN
    mode never changes what loopback-only deployments accept. Only when the
    operator has opted in does the predicate additionally accept a trusted
    LAN/Tailscale IP or one of their explicitly configured extra hostnames.
    """

    extra = frozenset(host.lower() for host in extra_hosts)

    def is_allowed(host: str) -> bool:
        if host in ALLOWED_LOCAL_HOSTS:
            return True
        if not allow_lan:
            return False
        return host.lower() in extra or is_trusted_lan_address(host)

    return is_allowed


def make_origin_allowlist(
    *, allow_lan: bool, extra_hosts: frozenset[str] = frozenset()
) -> Callable[[str], bool]:
    """Build the WS `Origin` predicate for `run_stream`.

    Same shape as `make_host_allowlist`: the default path is an exact match
    against `ALLOWED_WS_ORIGINS` (unchanged from before W9), and LAN mode
    only widens it by parsing the Origin's hostname and applying the same
    trusted-LAN/extra-hosts check, restricted to plain `http://` — W9
    deliberately ships without TLS (docs/06-handoff.md), so an `https://`
    Origin claiming to be this service would be lying about the transport.
    """

    def is_allowed(origin: str) -> bool:
        if origin in ALLOWED_WS_ORIGINS:
            return True
        if not allow_lan:
            return False
        parsed = urlsplit(origin)
        if parsed.scheme != "http" or not parsed.hostname:
            return False
        return make_host_allowlist(allow_lan=True, extra_hosts=extra_hosts)(parsed.hostname)

    return is_allowed

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


class TranslationCapability(ServerModel):
    """The opt-in translation provider (docs/04「翻譯（opt-in）」).

    Present only when the server was started with translation enabled; the
    capabilities route omits it otherwise, so a server without translation
    answers exactly as before.
    """

    state: str
    model: str
    model_revision: str
    #: Only directions that were actually exercised (docs/06 #6).
    directions: list[str]
    latency_modes: list[str]
    max_sessions: int
    max_pending_segments: int
    request_timeout_ms: int
    last_error: str | None = None


class Capabilities(ServerModel):
    protocol_version: str
    audio: CapabilityAudio = Field(default_factory=CapabilityAudio)
    profiles: list[str]
    features: CapabilityFeatures
    limits: CapabilityLimits
    translation: TranslationCapability | None = None


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


class LogEntry(ServerModel):
    ts: str
    level: str
    logger: str
    message: str
    #: Structured fields the call site passed to `tea_asr.logs.event()`,
    #: minus `tea_asr.logs.FORBIDDEN_KEYS` (already stripped at write time by
    #: `JsonFormatter`, stripped again here on read as defense in depth).
    fields: dict[str, Any] = Field(default_factory=dict)


class LogsResponse(ServerModel):
    items: list[LogEntry]
    count: int
    #: The `limit` this response actually honored (echoes the request; the
    #: query parameter itself is capped at `tea_asr.logs.MAX_LOG_EVENTS`).
    limit: int
    #: True when more matching events exist beyond `limit` — ask again with
    #: a larger (still capped) `limit` rather than assuming this is all there is.
    has_more: bool


# --- WebSocket: client control events ---------------------------------------


class TranslationOptions(ClientModel):
    """Opt-in translation of this session's finals by the separate provider."""

    direction: Literal["zh2en"]
    latency_mode: Literal["low", "native", "high"] = "native"


class SessionStart(ClientModel):
    type: Literal["session.start"]
    request_id: str = Field(min_length=1, max_length=64)
    profile: Literal["utterance", "continuous"]
    audio: AudioFormat
    language: str = "Chinese"
    durable: bool = False
    transcript_mode: Literal["final_only", "revisable"] = "final_only"
    translation: TranslationOptions | None = None


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


class TranslationStarted(SessionEvent):
    """Translation was accepted for this session; sent right after session.started."""

    type: Literal["translation.started"] = "translation.started"
    request_id: str
    direction: str
    latency_mode: str
    model: str
    model_revision: str


class TranslationSegment(SessionEvent):
    """One committed, append-only piece of translation.

    It never replaces or edits a `transcript.final`: the finals it covers are
    named by `source_segment_ids` and keep their own text, samples and IDs.
    `translation_index` counts up from 0 per session and is never reused or
    revised.
    """

    type: Literal["translation.segment"] = "translation.segment"
    translation_index: int = Field(ge=0, le=MAX_SAFE_INT)
    source_segment_ids: list[str]
    source_text: str
    text: str
    direction: str
    latency_mode: str
    forced: bool
    inference_ms: int


class TranslationError(SessionEvent):
    """These finals got no translation; `stopped=true` means none will follow."""

    type: Literal["translation.error"] = "translation.error"
    code: str
    message: str
    retryable: bool
    source_segment_ids: list[str]
    stopped: bool = False


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
    | ErrorEvent
    | TranslationStarted
    | TranslationSegment
    | TranslationError,
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
