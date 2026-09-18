from __future__ import annotations

import asyncio
import json
import os
import struct
import sys
import uuid
from pathlib import Path
from typing import Any

from ..errors import ApiError
from .protocol import MAX_HEADER_BYTES, MAX_PCM_BYTES, read_response


class WorkerError(ApiError):
    """A worker failure carrying a wire error code.

    It is an `ApiError` so HTTP and WebSocket callers map it the same way and
    neither has to translate worker internals into a status by hand.
    """


class WorkerSupervisor:
    def __init__(self, model_path: Path, *, load_timeout_s: float = 120, task_timeout_s: float = 30) -> None:
        self.model_path = model_path
        self.load_timeout_s = load_timeout_s
        self.task_timeout_s = task_timeout_s
        self.process: asyncio.subprocess.Process | None = None
        self.state = "unprepared"
        self.last_error: str | None = None
        self.load_ms: int | None = None
        self.generation = 0
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self.process and self.process.returncode is None:
            return
        self.state = "loading"
        self.last_error = None
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tea_asr.worker.entry",
            "--model-path",
            str(self.model_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        try:
            assert self.process.stdout is not None
            ready = await asyncio.wait_for(read_response(self.process.stdout), self.load_timeout_s)
        except Exception as exc:
            await self.stop()
            self.state = "failed"
            self.last_error = f"Worker did not become ready: {exc}"
            raise WorkerError("model_unavailable", self.last_error) from exc
        if ready.get("status") != "ready":
            await self.stop()
            self.state = "failed"
            self.last_error = str(ready.get("message", "Model load failed"))
            raise WorkerError(str(ready.get("code", "model_unavailable")), self.last_error)
        self.load_ms = int(ready.get("load_ms", 0))
        self.generation += 1
        self.state = "ready"

    async def transcribe(self, pcm: bytes, *, language: str = "Chinese") -> dict[str, Any]:
        if len(pcm) > MAX_PCM_BYTES or len(pcm) % 2:
            raise ValueError("PCM must be even-length and no longer than 30 seconds")
        async with self._lock:
            if not self.process or self.process.returncode is not None or self.state != "ready":
                raise WorkerError("model_unavailable", "ASR worker is not ready")
            request_id = str(uuid.uuid4())
            header = json.dumps(
                {
                    "ipc_version": 1,
                    "request_id": request_id,
                    "pcm_bytes": len(pcm),
                    "sample_rate": 16_000,
                    "language": language,
                    "max_tokens": 512,
                },
                separators=(",", ":"),
            ).encode()
            if len(header) > MAX_HEADER_BYTES:
                raise ValueError("IPC header is too large")
            assert self.process.stdin is not None and self.process.stdout is not None
            self.process.stdin.write(struct.pack(">I", len(header)) + header + pcm)
            await self.process.stdin.drain()
            try:
                response = await asyncio.wait_for(
                    read_response(self.process.stdout), self.task_timeout_s
                )
            except TimeoutError as exc:
                await self.stop()
                self.state = "failed"
                self.last_error = "Inference timed out"
                raise WorkerError("inference_timeout", self.last_error) from exc
            if response.get("request_id") != request_id:
                raise WorkerError("invalid_ipc", "Worker response ID does not match")
            if response.get("status") != "ok":
                raise WorkerError(
                    str(response.get("code", "inference_failed")),
                    str(response.get("message", "Inference failed")),
                )
            return response

    async def stop(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        if process.stdin:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
        if self.state != "failed":
            self.state = "unprepared"
