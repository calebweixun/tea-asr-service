from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import sys
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from ..errors import ApiError
from .protocol import MAX_HEADER_BYTES, MAX_PCM_BYTES, read_response

logger = logging.getLogger("tea_asr.worker")

#: docs/03: 1/2/4 second backoff, at most three restarts in a minute. Beyond
#: that the service stops trying and says so, instead of thrashing the GPU.
RESTART_BACKOFF_S = (1.0, 2.0, 4.0)
RESTART_WINDOW_S = 60.0
MAX_RESTARTS_PER_WINDOW = 3

#: Failures that mean the checkpoint itself is wrong. Retrying cannot help, so
#: they never enter the restart loop.
UNRECOVERABLE_CODES = frozenset({"model_incompatible"})


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
        self._restarts: deque[float] = deque()
        self._recovering: asyncio.Task[None] | None = None

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
            if self.process and self.process.returncode is not None:
                # The worker died between requests. Say so for this segment and
                # bring it back for the next one.
                self.last_error = f"Worker exited with code {self.process.returncode}"
                self.state = "recovering"
                self._schedule_restart()
                raise WorkerError("inference_failed", self.last_error)
            if not self.process or self.state != "ready":
                raise WorkerError("model_unavailable", f"ASR worker is {self.state}")
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
            except (TimeoutError, asyncio.IncompleteReadError, ConnectionResetError) as exc:
                # A hung or dead worker takes the whole process down and comes
                # back; a stuck Metal kernel cannot be interrupted any other way.
                await self.stop()
                self.state = "recovering"
                self.last_error = (
                    "Inference timed out"
                    if isinstance(exc, TimeoutError)
                    else f"Worker connection lost: {type(exc).__name__}"
                )
                self._schedule_restart()
                code = "inference_timeout" if isinstance(exc, TimeoutError) else "inference_failed"
                raise WorkerError(code, self.last_error) from exc
            if response.get("request_id") != request_id:
                raise WorkerError("invalid_ipc", "Worker response ID does not match")
            if response.get("status") != "ok":
                raise WorkerError(
                    str(response.get("code", "inference_failed")),
                    str(response.get("message", "Inference failed")),
                )
            return response

    def _schedule_restart(self) -> None:
        if self._recovering is not None and not self._recovering.done():
            return
        self._recovering = asyncio.get_running_loop().create_task(self._restart_loop())

    async def _restart_loop(self) -> None:
        for delay in RESTART_BACKOFF_S:
            now = time.monotonic()
            while self._restarts and now - self._restarts[0] > RESTART_WINDOW_S:
                self._restarts.popleft()
            if len(self._restarts) >= MAX_RESTARTS_PER_WINDOW:
                self._give_up()
                return
            self._restarts.append(now)
            await asyncio.sleep(delay)
            try:
                await self.start()
            except WorkerError as exc:
                if exc.code in UNRECOVERABLE_CODES:
                    self.state = "failed"
                    self.last_error = str(exc)
                    return
                continue
            logger.info(
                "worker.restarted", extra={"fields": {"generation": self.generation}}
            )
            return
        self._give_up()

    def _give_up(self) -> None:
        self.state = "failed"
        self.last_error = (
            f"Worker restarted {MAX_RESTARTS_PER_WINDOW} times in "
            f"{int(RESTART_WINDOW_S)}s; giving up until the service is restarted."
        )
        logger.warning(self.last_error, extra={"fields": {"restarts": len(self._restarts)}})

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
