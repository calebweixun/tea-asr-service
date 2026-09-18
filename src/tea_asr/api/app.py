from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol

from fastapi import Depends, FastAPI, Header, Query, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from tea_asr.api.stream import StreamSession, private_use_warnings, run_stream
from tea_asr.config import ServiceConfig, load_or_create_token
from tea_asr.errors import ApiError
from tea_asr.logs import event
from tea_asr.model_spec import TEA_ASR_1_1_MLX_4BIT
from tea_asr.scheduler import Scheduler
from tea_asr.vad import VAD_SHA256, SileroVad, locate_vad
from tea_asr.wire import (
    MAX_UTTERANCE_PCM_BYTES,
    Capabilities,
    CapabilityFeatures,
    CapabilityLimits,
    ErrorEnvelope,
    QueueStatus,
    Segment,
    StatusResponse,
    TranscriptionResponse,
)
from tea_asr.worker.supervisor import WorkerSupervisor

#: Sentinel so a caller can say "no VAD" (tests) instead of "load the default".
_AUTO_VAD: Any = object()


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
) -> FastAPI:
    auth_token = token or load_or_create_token()
    settings = config or ServiceConfig.from_env()
    worker = supervisor or WorkerSupervisor(model_path)
    scheduler = Scheduler(worker)
    vad = _load_vad() if vad_model is _AUTO_VAD else vad_model
    activity = Activity()
    loading = asyncio.Lock()
    sessions: set[StreamSession] = set()

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
        event(logger, "service.started", model_state=worker.state, continuous=vad is not None)
        yield
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

    def _envelope(error: ApiError, request_id: str | None = None) -> JSONResponse:
        return JSONResponse(status_code=error.http_status, content=error.envelope(request_id))

    async def authorize(authorization: str | None = Header(default=None)) -> None:
        if authorization != f"Bearer {auth_token}":
            raise ApiError("unauthenticated", "缺少或不正確的 bearer token。")

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

    @app.get("/v1/capabilities", dependencies=[Depends(authorize)])
    async def capabilities() -> Capabilities:
        return Capabilities(
            protocol_version=settings.protocol_version,
            profiles=["utterance", "continuous"] if vad is not None else ["utterance"],
            features=CapabilityFeatures(
                # Only flip these once the matching acceptance in docs/05 passes.
                partial_transcripts=settings.revisable_preview,
            ),
            limits=CapabilityLimits(
                max_continuous_sessions=1 if vad is not None else 0,
                max_total_connections=settings.max_total_connections,
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

    @app.websocket("/v1/stream")
    async def stream(websocket: WebSocket) -> None:
        if worker.state == "idle_unloaded":
            asyncio.create_task(ensure_loaded())
        activity.sessions += 1
        activity.touch()
        try:
            await run_stream(
                websocket,
                scheduler,
                auth_token=auth_token,
                config=settings,
                model_state=worker.state,
                vad=vad,
                registry=sessions,
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
        text = str(response["text"])
        audio_samples = len(body) // 2
        warnings = private_use_warnings(text)
        if not text:
            warnings.insert(0, "no_speech")
        return TranscriptionResponse(
            request_id=request_id,
            model_revision=TEA_ASR_1_1_MLX_4BIT.revision,
            text=text,
            raw_text=text,
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
