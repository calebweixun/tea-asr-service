from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Protocol

from fastapi import Depends, FastAPI, Header, Query, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from tea_asr.api.stream import (
    ContinuousSessionAdmission,
    StreamSession,
    filter_private_use_characters,
    private_use_warnings,
    run_stream,
)
from tea_asr.config import (
    AppPaths,
    ServiceConfig,
    TokenAuthenticator,
    validate_translation_or_raise,
)
from tea_asr.errors import ApiError
from tea_asr.logs import MAX_LOG_EVENTS, event, read_recent_events, split_log_payload
from tea_asr.model_spec import TEA_ASR_1_1_MLX_4BIT
from tea_asr.rate_limit import AuthRateLimiter
from tea_asr.scheduler import Scheduler
from tea_asr.translation.session import MAX_PENDING_SEGMENTS
from tea_asr.translation.simt import LATENCY_MODES, VERIFIED_DIRECTIONS
from tea_asr.translation.supervisor import TranslationSupervisor
from tea_asr.vad import VAD_SHA256, SileroVad, locate_vad
from tea_asr.wire import (
    MAX_UTTERANCE_PCM_BYTES,
    Capabilities,
    CapabilityFeatures,
    CapabilityLimits,
    ErrorEnvelope,
    LogEntry,
    LogsResponse,
    QueueStatus,
    Segment,
    StatusResponse,
    TranscriptionResponse,
    TranslationCapability,
    make_host_allowlist,
    make_origin_allowlist,
)
from tea_asr.worker.supervisor import WorkerSupervisor

#: W9: the plain-text warning shown whenever `ServiceConfig.allow_lan` is on.
#: Repeated in three places on purpose (startup log, HTTP response header,
#: WS accept header) — see docs/06-handoff.md's LAN row and docs/04-api.md
#: for why this service ships without TLS and what that means for the
#: operator: encryption and peer identity are delegated to the LAN/Tailscale
#: transport, not provided by this application.
INSECURE_LAN_WARNING = (
    "TEA ASR 目前以 LAN 模式監聽：連線未加密，token 以明文傳輸；"
    "僅應在受信任的網路（LAN／Tailscale）使用，不得暴露於公開網路。"
)


class HostValidationMiddleware:
    """Reject HTTP requests whose ``Host`` header is not in the allowlist.

    docs/03-architecture.md requires Host validation as a DNS-rebinding guard:
    a malicious page served from an attacker-controlled domain that resolves
    to 127.0.0.1 must not be able to reach this loopback-only API just because
    the browser happily sends whatever Host the page's origin implies. This is
    the HTTP half of that guard; the WS half is the Origin allowlist in
    ``tea_asr.api.stream.run_stream``, which a plain HTTP TrustedHost check
    cannot safely stand in for (rejecting a WebSocket upgrade this way sends a
    malformed ASGI response instead of a clean close).

    Applies to every HTTP route, including ``/healthz``/``/readyz``, so an
    unauthenticated probe cannot be used to fingerprint the service from an
    off-allowlist host either.
    """

    def __init__(self, app: ASGIApp, is_allowed: Callable[[str], bool]) -> None:
        self._app = app
        self._is_allowed = is_allowed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        host = Headers(scope=scope).get("host", "").split(":")[0]
        if host and not self._is_allowed(host):
            error = ApiError("forbidden_origin", f"Host 不在允許清單：{host}")
            response = JSONResponse(status_code=error.http_status, content=error.envelope())
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


class InsecureLanWarningMiddleware:
    """Stamp every HTTP response with the "this is unencrypted" warning.

    Only active when `ServiceConfig.allow_lan` is on. Deliberately a plain
    response header rather than a body field: `/v1/status` and every other
    JSON route return a fixed Pydantic model whose schema is checked against
    `docs/api/openapi.json` (`tests/integration/test_schema_export.py`), so
    adding a body field would mean growing that generated contract for a
    warning that has nothing to do with the wire protocol. A header is
    visible with `curl -i` or any HTTP client without touching that schema.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_warning(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-tea-asr-security", b"unencrypted-lan-mode"))
                headers.append(
                    (
                        b"x-tea-asr-security-notice",
                        (
                            b"unencrypted; bearer token sent in cleartext; "
                            b"LAN/Tailscale use only, do not expose publicly"
                        ),
                    )
                )
                message = {**message, "headers": headers}
            await send(message)

        await self._app(scope, receive, send_with_warning)


#: Sentinel so a caller can say "no VAD" (tests) instead of "load the default".
_AUTO_VAD: Any = object()
#: Sentinel: build the translation provider from `ServiceConfig` (tests inject one).
_AUTO_TRANSLATION: Any = object()


def _load_vad() -> SileroVad | None:
    """Load the pinned VAD asset, or run without continuous support.

    A missing asset must not break utterance sessions, so capabilities simply
    stops advertising `continuous` and `session.start` says why.
    """

    try:
        return SileroVad(locate_vad(), expected_sha256=VAD_SHA256)
    except Exception:  # noqa: BLE001 - any lookup or load failure means no continuous
        return None


logger = logging.getLogger("tea_asr.api")


async def detect_sleep(
    on_wake: Any,
    *,
    interval: float = 5.0,
    floor: float = 10.0,
    wall_clock: Any = time.time,
    monotonic_clock: Any = time.monotonic,
) -> None:
    """Notice that the machine slept.

    On Darwin `time.monotonic()` stops during sleep while `time.time()` keeps
    going, so the divergence between them is the time spent asleep. This avoids
    pulling in pyobjc just to hear a wake notification.
    """

    wall, mono = wall_clock(), monotonic_clock()
    while True:
        await asyncio.sleep(interval)
        now_wall, now_mono = wall_clock(), monotonic_clock()
        slept = (now_wall - wall) - (now_mono - mono)
        wall, mono = now_wall, now_mono
        if slept >= floor:
            await on_wake(slept)


class Activity:
    """Tracks whether the model is still earning its memory."""

    def __init__(self) -> None:
        self.last_used = time.monotonic()
        self.sessions = 0

    def touch(self) -> None:
        self.last_used = time.monotonic()

    def idle_for(self) -> float:
        return 0.0 if self.sessions else time.monotonic() - self.last_used


class InferenceSupervisor(Protocol):
    state: str
    last_error: str | None
    load_ms: int | None
    generation: int

    async def start(self) -> None: ...
    async def transcribe(self, pcm: bytes, *, language: str = "Chinese") -> dict[str, Any]: ...
    async def stop(self) -> None: ...


def create_app(
    model_path: Path,
    *,
    token: str | None = None,
    supervisor: InferenceSupervisor | None = None,
    config: ServiceConfig | None = None,
    vad_model: Any = _AUTO_VAD,
    token_authenticator: TokenAuthenticator | None = None,
    rate_limiter: AuthRateLimiter | None = None,
    paths: AppPaths | None = None,
    translation_provider: Any = _AUTO_TRANSLATION,
) -> FastAPI:
    # A fixed `token` string (tests, `export-schemas`) is checked with a
    # constant-time comparison and never touches the filesystem. Otherwise a
    # `TokenAuthenticator` follows the token file, so `tea-asr token
    # rotate`/`revoke` (see `tea_asr.config`) take effect on this already
    # running process without a restart.
    if token is not None:
        _fixed_token = token

        def token_matches(presented: str) -> bool:
            return secrets.compare_digest(presented, _fixed_token)
    else:
        authenticator = token_authenticator or TokenAuthenticator()
        token_matches = authenticator.matches
    limiter = rate_limiter or AuthRateLimiter()
    settings = config or ServiceConfig.from_env()
    log_paths = paths or AppPaths.macos_default()
    host_allowed = make_host_allowlist(
        allow_lan=settings.allow_lan, extra_hosts=frozenset(settings.extra_allowed_hosts)
    )
    origin_allowed = make_origin_allowlist(
        allow_lan=settings.allow_lan, extra_hosts=frozenset(settings.extra_allowed_hosts)
    )
    worker = supervisor or WorkerSupervisor(model_path)
    #: Opt-in, separate translation provider (docs/06 #2). `None` when off,
    #: which leaves every ASR code path exactly as it was.
    validate_translation_or_raise(settings)
    translation: Any = None
    if translation_provider is not _AUTO_TRANSLATION:
        translation = translation_provider
    elif settings.translation_enabled:
        translation = TranslationSupervisor(
            Path(settings.translation_model_path).expanduser(),
            max_memory_gib=settings.translation_max_memory_gib,
        )
    scheduler = Scheduler(worker)
    vad = _load_vad() if vad_model is _AUTO_VAD else vad_model
    activity = Activity()
    loading = asyncio.Lock()
    sessions: set[StreamSession] = set()
    continuous_admission = ContinuousSessionAdmission(
        settings.max_continuous_sessions if vad is not None else None
    )
    #: docs/04-api.md `limits.max_total_connections`: caps concurrent
    #: `/v1/stream` connections regardless of profile, separately from the
    #: continuous-only cap above.
    connection_admission = ContinuousSessionAdmission(settings.max_total_connections)

    async def ensure_loaded() -> None:
        """Bring the worker back after an idle unload.

        The caller still gets `model_loading` rather than being held for
        minutes inside one request (docs/04).
        """

        if loading.locked():
            return
        async with loading:
            if worker.state in {"ready", "loading"}:
                return
            try:
                await worker.start()
                event(logger, "model.reloaded", state=worker.state)
            except ApiError as exc:
                event(logger, "model.reload_failed", code=exc.code)

    async def after_wake(slept: float) -> None:
        event(logger, "service.woke", slept_s=round(slept))
        # Any live session's sample clock now has a hole in it. There is no
        # resume in v0.1, so the gap is reported and the session ends rather
        # than splicing audio across it (docs/03).
        for session in list(sessions):
            await session.interrupt(
                f"機器睡眠了約 {round(slept)} 秒，時間軸出現缺口；請開新的 session。"
            )
        if worker.state != "ready":
            return
        try:
            # A real probe, not an assumption: Metal state can be broken by sleep.
            await asyncio.wait_for(
                scheduler.transcribe(b"\x00\x00" * 1_600, kind="interactive"), timeout=30
            )
            event(logger, "worker.healthy_after_wake")
        except (ApiError, TimeoutError) as exc:
            event(logger, "worker.unhealthy_after_wake", error=type(exc).__name__)
            await worker.stop()
            try:
                await worker.start()
                event(logger, "worker.restarted_after_wake", state=worker.state)
            except ApiError as restart_error:
                event(logger, "worker.restart_failed", code=restart_error.code)

    async def idle_watcher() -> None:
        unload_after = settings.unload_after_s
        if unload_after <= 0:
            return
        while True:
            await asyncio.sleep(min(30, max(5, unload_after // 10)))
            if worker.state != "ready":
                continue
            if scheduler.waiting_tasks or activity.idle_for() < unload_after:
                continue
            await worker.stop()
            worker.state = "idle_unloaded"
            event(logger, "model.idle_unloaded", idle_s=round(activity.idle_for()))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.worker = worker
        app.state.scheduler = scheduler
        app.state.config = settings
        app.state.activity = activity
        try:
            await worker.start()
        except ApiError:
            # Keep the API alive so health and status can explain the failure.
            pass
        watcher = asyncio.create_task(idle_watcher())
        sleep_watcher = asyncio.create_task(detect_sleep(after_wake))
        if settings.allow_lan:
            # Loud on purpose: this is the one moment guaranteed to reach
            # whoever's tailing the log before anything else happens, and the
            # decision to skip TLS (docs/06-handoff.md) only stays informed
            # consent if the operator is told every time it takes effect.
            event(
                logger,
                "service.insecure_lan_bind",
                warning=INSECURE_LAN_WARNING,
                extra_allowed_hosts=list(settings.extra_allowed_hosts),
            )
        if translation is not None:
            # In the background: a 7.7 GiB load (or a missing SSD) must not
            # hold up ASR start-up.
            translation.start_in_background()
        event(logger, "service.started", model_state=worker.state, continuous=vad is not None)
        yield
        if translation is not None:
            await translation.close()
        sleep_watcher.cancel()
        watcher.cancel()
        # Stop admitting work, then let what is already running finish.
        deadline = time.monotonic() + 30
        while scheduler.waiting_tasks and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        await worker.stop()
        event(logger, "service.stopped")

    app = FastAPI(
        title="TEA ASR Service",
        version="0.1.0",
        lifespan=lifespan,
        responses={"default": {"model": ErrorEnvelope}},
    )
    if settings.allow_lan:
        app.add_middleware(InsecureLanWarningMiddleware)
    app.add_middleware(HostValidationMiddleware, is_allowed=host_allowed)

    def _envelope(error: ApiError, request_id: str | None = None) -> JSONResponse:
        return JSONResponse(status_code=error.http_status, content=error.envelope(request_id))

    def _client_key(request: Request) -> str:
        return request.client.host if request.client is not None else "unknown"

    _BEARER_PREFIX = "Bearer "

    async def authorize(
        request: Request, authorization: str | None = Header(default=None)
    ) -> None:
        key = _client_key(request)
        if limiter.is_blocked(key):
            raise ApiError("rate_limited", "認證失敗次數過多，請稍後再試。")
        valid = (
            authorization is not None
            and authorization.startswith(_BEARER_PREFIX)
            and token_matches(authorization[len(_BEARER_PREFIX) :])
        )
        if not valid:
            limiter.record_failure(key)
            raise ApiError("unauthenticated", "缺少或不正確的 bearer token。")
        limiter.record_success(key)

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        return _envelope(exc, request.headers.get("x-request-id"))

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"][1:]) or "request"
        return _envelope(
            ApiError("invalid_audio", f"{location}: {first['msg']}"),
            request.headers.get("x-request-id"),
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        ready = worker.state == "ready"
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ready" if ready else worker.state},
        )

    # exclude_none: without translation the body is byte-for-byte what it was
    # before the `translation` block existed.
    @app.get(
        "/v1/capabilities", dependencies=[Depends(authorize)], response_model_exclude_none=True
    )
    async def capabilities() -> Capabilities:
        return Capabilities(
            protocol_version=settings.protocol_version,
            profiles=["utterance", "continuous"] if vad is not None else ["utterance"],
            features=CapabilityFeatures(
                # Only flip these once the matching acceptance in docs/05 passes.
                partial_transcripts=settings.revisable_preview,
                translation=translation is not None and translation.state == "ready",
            ),
            limits=CapabilityLimits(
                max_continuous_sessions=settings.max_continuous_sessions if vad is not None else 0,
                max_total_connections=settings.max_total_connections,
            ),
            translation=(
                TranslationCapability(
                    state=translation.state,
                    model=translation.model,
                    model_revision=translation.model_revision,
                    directions=list(VERIFIED_DIRECTIONS),
                    latency_modes=list(LATENCY_MODES),
                    max_sessions=1,
                    max_pending_segments=MAX_PENDING_SEGMENTS,
                    request_timeout_ms=round(translation.task_timeout_s * 1000),
                    last_error=translation.last_error,
                )
                if translation is not None
                else None
            ),
        )

    @app.get("/v1/status", dependencies=[Depends(authorize)])
    async def service_status() -> StatusResponse:
        return StatusResponse(
            model_state=worker.state,
            model=TEA_ASR_1_1_MLX_4BIT.repo_id,
            model_revision=TEA_ASR_1_1_MLX_4BIT.revision,
            worker_generation=worker.generation,
            worker_load_ms=worker.load_ms,
            last_error=worker.last_error,
            idle_s=round(activity.idle_for()),
            active_sessions=activity.sessions,
            queue=QueueStatus(
                waiting_tasks=scheduler.waiting_tasks,
                waiting_samples=scheduler.waiting_samples,
                max_waiting_tasks=scheduler.max_waiting_tasks,
                max_waiting_samples=scheduler.max_waiting_samples,
            ),
        )

    @app.get("/v1/logs", dependencies=[Depends(authorize)])
    async def logs(
        level: Literal["debug", "info", "warning", "error"] | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=MAX_LOG_EVENTS),
    ) -> LogsResponse:
        """Recent structured events for a log page, newest first.

        `level` (when given) is a *minimum* severity: `level=warning` returns
        warning and error, not warning alone. `limit` is hard-capped at
        `MAX_LOG_EVENTS` by the query validation above — a request for more
        is rejected outright (422) rather than silently truncated, so a
        client can never mistake "we capped this" for "there were only this
        many". There is no `before`/`since` pagination in v0.1: this is a
        recent-activity view, not a historical log browser (docs/06 #6 —
        do not claim a capability that is not implemented).

        File I/O runs in a worker thread via `asyncio.to_thread`, so it never
        blocks the event loop the WS session and other HTTP routes share.
        """

        raw = await asyncio.to_thread(
            read_recent_events,
            log_paths.log_file,
            level=level,
            limit=limit + 1,
            backup_count=settings.log_backup_count,
        )
        has_more = len(raw) > limit
        items = [
            LogEntry(
                ts=str(payload.get("ts", "")),
                level=str(payload.get("level", "")),
                logger=str(payload.get("logger", "")),
                message=str(payload.get("message", "")),
                fields=split_log_payload(payload),
            )
            for payload in raw[:limit]
        ]
        return LogsResponse(items=items, count=len(items), limit=limit, has_more=has_more)

    @app.websocket("/v1/stream")
    async def stream(websocket: WebSocket) -> None:
        model_state = worker.state
        if worker.state == "idle_unloaded":
            # Reload is intentionally asynchronous so opening a stream never
            # blocks for model load.  Publish the transient state captured for
            # this connection before the task can advance the worker; passing
            # the stale `idle_unloaded` value would make session.start report
            # non-retryable `model_unavailable` and strand clients that arrived
            # during the reload window.
            asyncio.create_task(ensure_loaded())
            model_state = "loading"
        activity.sessions += 1
        activity.touch()
        try:
            await run_stream(
                websocket,
                scheduler,
                token_matches=token_matches,
                origin_allowed=origin_allowed,
                rate_limiter=limiter,
                insecure_lan=settings.allow_lan,
                config=settings,
                model_state=model_state,
                vad=vad,
                registry=sessions,
                continuous_admission=continuous_admission,
                connection_admission=connection_admission,
                translation=translation,
            )
        finally:
            activity.sessions -= 1
            activity.touch()

    @app.post("/v1/transcriptions", dependencies=[Depends(authorize)])
    async def transcribe(
        request: Request,
        sample_rate: int = Query(...),
        channels: int = Query(...),
        format: str = Query(...),
        x_request_id: str | None = Header(default=None),
    ) -> TranscriptionResponse:
        request_id = x_request_id or str(uuid.uuid4())
        if sample_rate != 16_000 or channels != 1 or format != "pcm_s16le":
            raise ApiError(
                "invalid_audio",
                "只接受 sample_rate=16000&channels=1&format=pcm_s16le。",
                request_id=request_id,
            )
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_UTTERANCE_PCM_BYTES:
                raise ApiError(
                    "payload_too_large", "音訊超過 30 秒上限。", request_id=request_id
                )
        if not body or len(body) % 2:
            raise ApiError(
                "invalid_audio", "PCM body 不得為空且長度必須為偶數。", request_id=request_id
            )

        if worker.state != "ready":
            if worker.state == "idle_unloaded":
                asyncio.create_task(ensure_loaded())
            raise ApiError(
                "model_loading"
                if worker.state in {"loading", "idle_unloaded"}
                else "model_unavailable",
                f"模型目前狀態為 {worker.state}。",
                request_id=request_id,
            )
        activity.touch()

        response, queue_ms = await scheduler.transcribe(
            bytes(body), language="Chinese", kind="interactive"
        )
        raw_text = str(response["text"])
        audio_samples = len(body) // 2
        warnings = private_use_warnings(raw_text)
        if not raw_text:
            warnings.insert(0, "no_speech")
            text = raw_text
        else:
            text = filter_private_use_characters(raw_text) if settings.filter_pua else raw_text
            if not text:
                # Whole result was PUA noise; see filter_private_use_characters
                # for why this is distinct from "no_speech" (something was
                # recognized, it was just entirely filtered).
                warnings.append("empty_after_filter")
            removed = len(raw_text) - len(text)
            if removed:
                event(logger, "pua_filtered", request_id=request_id, removed_chars=removed)
        return TranscriptionResponse(
            request_id=request_id,
            model_revision=TEA_ASR_1_1_MLX_4BIT.revision,
            text=text,
            raw_text=raw_text,
            language="Chinese",
            segments=(
                [
                    Segment(
                        segment_id=str(uuid.uuid4()),
                        start_sample=0,
                        end_sample=audio_samples,
                        text=text,
                    )
                ]
                if text
                else []
            ),
            audio_ms=audio_samples // 16,
            queue_ms=queue_ms,
            inference_ms=round(float(response["total_time_s"]) * 1000),
            warnings=warnings,
        )

    return app
