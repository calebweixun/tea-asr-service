from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol

from fastapi import Depends, FastAPI, Header, Query, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from tea_asr.api.stream import private_use_warnings, run_stream
from tea_asr.config import ServiceConfig, load_or_create_token
from tea_asr.errors import ApiError
from tea_asr.model_spec import TEA_ASR_1_1_MLX_4BIT
from tea_asr.scheduler import Scheduler
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
) -> FastAPI:
    auth_token = token or load_or_create_token()
    settings = config or ServiceConfig.from_env()
    worker = supervisor or WorkerSupervisor(model_path)
    scheduler = Scheduler(worker)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.worker = worker
        app.state.scheduler = scheduler
        app.state.config = settings
        try:
            await worker.start()
        except ApiError:
            # Keep the API alive so health and status can explain the failure.
            pass
        yield
        await worker.stop()

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
            profiles=["utterance"],
            features=CapabilityFeatures(
                # Only flip these once the matching acceptance in docs/05 passes.
                partial_transcripts=settings.revisable_preview,
            ),
            limits=CapabilityLimits(
                max_continuous_sessions=0,
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
            queue=QueueStatus(
                waiting_tasks=scheduler.waiting_tasks,
                waiting_samples=scheduler.waiting_samples,
                max_waiting_tasks=scheduler.max_waiting_tasks,
                max_waiting_samples=scheduler.max_waiting_samples,
            ),
        )

    @app.websocket("/v1/stream")
    async def stream(websocket: WebSocket) -> None:
        await run_stream(
            websocket,
            scheduler,
            auth_token=auth_token,
            config=settings,
            model_state=worker.state,
        )

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
            raise ApiError(
                "model_loading" if worker.state == "loading" else "model_unavailable",
                f"模型目前狀態為 {worker.state}。",
                request_id=request_id,
            )

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
