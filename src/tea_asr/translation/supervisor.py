"""Supervisor for the translation worker subprocess.

Mirrors the ASR `WorkerSupervisor` on purpose, but is a separate process with
its own model, queue and timeouts (docs/06 #2, #4): a hung or crashed
translation worker is killed and restarted without touching ASR.
"""

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

from tea_asr.errors import ApiError
from tea_asr.logs import event
from tea_asr.worker.protocol import read_response

from .worker import missing_model_reason

logger = logging.getLogger("tea_asr.translation")

RESTART_BACKOFF_S = (1.0, 2.0, 4.0)
RESTART_WINDOW_S = 60.0
MAX_RESTARTS_PER_WINDOW = 3

#: Source checkpoint and local conversion (models.lock.json `translation`).
T3PO_REPO_ID = "netease-youdao/Confucius4-T3PO"
T3PO_REVISION = "446e5dcca080740f2c2dc9d06a91ed66a9920410"
T3PO_LOCAL_BUILD = "mlx-lm 0.31.3 convert -q --q-bits 4 --q-group-size 64 --q-mode affine"


class TranslationSupervisor:
    """Owns the one translation worker and the one session allowed to use it.

    The worker keeps a single KV cache and history, so exactly one stream
    session may translate at a time (`try_acquire`); a second one is refused
    with `translation_unavailable` instead of silently sharing context.
    """

    model = T3PO_REPO_ID
    model_revision = T3PO_REVISION

    def __init__(
        self,
        model_path: Path,
        *,
        max_memory_gib: float,
        load_timeout_s: float = 180.0,
        task_timeout_s: float = 20.0,
        worker_module: str = "tea_asr.translation.worker",
    ) -> None:
        self.model_path = model_path
        self.max_memory_gib = max_memory_gib
        self.load_timeout_s = load_timeout_s
        self.task_timeout_s = task_timeout_s
        self.worker_module = worker_module
        self.process: asyncio.subprocess.Process | None = None
        self.state = "unprepared"
        self.last_error: str | None = None
        self.load_ms: int | None = None
        self.generation = 0
        self._lock = asyncio.Lock()
        self._restarts: deque[float] = deque()
        self._recovering: asyncio.Task[None] | None = None
        self._starting: asyncio.Task[None] | None = None
        self._owner: object | None = None

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        if self.process and self.process.returncode is None and self.state == "ready":
            return
        reason = missing_model_reason(self.model_path)
        if reason is not None:
            # docs: an unplugged external SSD must fail loudly, not disable
            # translation quietly or pretend it still works.
            self.state = "failed"
            self.last_error = reason
            event(logger, "translation.model_missing", level="warning", path=str(self.model_path))
            raise ApiError("translation_unavailable", reason, retryable=False)
        self.state = "loading"
        self.last_error = None
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            self.worker_module,
            "--model-path",
            str(self.model_path),
            "--max-memory-gib",
            str(self.max_memory_gib),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        try:
            assert self.process.stdout is not None
            ready = await asyncio.wait_for(read_response(self.process.stdout), self.load_timeout_s)
        except Exception as exc:  # any failure to come up is reported
            await self.stop()
            self.state = "failed"
            self.last_error = f"翻譯 worker 沒有在時限內就緒：{type(exc).__name__}"
            raise ApiError("translation_unavailable", self.last_error, retryable=False) from exc
        if ready.get("status") != "ready":
            await self.stop()
            self.state = "failed"
            self.last_error = str(ready.get("message", "翻譯模型載入失敗"))
            raise ApiError("translation_unavailable", self.last_error, retryable=False)
        self.load_ms = int(ready.get("load_ms", 0))
        self.generation += 1
        self.state = "ready"

    def start_in_background(self) -> None:
        """Load without holding up service start-up or a WS handshake."""

        if self._starting is not None and not self._starting.done():
            return
        if self.state in {"ready", "loading", "recovering"}:
            return

        async def run() -> None:
            try:
                await self.start()
            except ApiError:
                pass

        self._starting = asyncio.get_running_loop().create_task(run())

    async def stop(self, *, kill: bool = False) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        if kill and process.returncode is None:
            # A worker that stopped answering will not read a closed stdin.
            process.kill()
        if process.stdin:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()
        if self.state != "failed":
            self.state = "unprepared"

    async def close(self) -> None:
        for task in (self._starting, self._recovering):
            if task is not None and not task.done():
                task.cancel()
        await self.stop()

    # -- admission -----------------------------------------------------------

    def availability_error(self) -> ApiError | None:
        """Why a new session cannot translate right now, or None."""

        if self.state == "ready":
            if self._owner is not None:
                return ApiError(
                    "translation_unavailable",
                    "翻譯 provider 一次只服務一個 session，目前已有其他 session 在使用。",
                    retryable=True,
                )
            return None
        if self.state == "failed" and missing_model_reason(self.model_path) is None:
            # The disk came back (or the path was fixed): try again for the
            # next session, but do not pretend this one can translate yet.
            self.start_in_background()
            return ApiError(
                "translation_unavailable",
                f"翻譯模型重新載入中（上次失敗：{self.last_error}）",
                retryable=True,
            )
        if self.state in {"loading", "recovering", "unprepared"}:
            return ApiError(
                "translation_unavailable", f"翻譯模型目前狀態為 {self.state}。", retryable=True
            )
        return ApiError(
            "translation_unavailable",
            self.last_error or f"翻譯模型目前狀態為 {self.state}。",
            retryable=False,
        )

    def try_acquire(self, owner: object) -> bool:
        if self._owner is not None and self._owner is not owner:
            return False
        self._owner = owner
        return True

    def release(self, owner: object) -> None:
        if self._owner is owner:
            self._owner = None

    # -- requests ------------------------------------------------------------

    async def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        # A caller that goes away (session closed) must not abandon a
        # half-read frame on the pipe; the exchange finishes on its own and
        # its result is dropped.
        future = asyncio.ensure_future(self._exchange(payload))
        future.add_done_callback(lambda done: done.cancelled() or done.exception())
        return await asyncio.shield(future)

    async def _exchange(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            if self.process and self.process.returncode is not None:
                self.last_error = f"翻譯 worker 已結束（exit {self.process.returncode}）"
                self.state = "recovering"
                self._schedule_restart()
                raise ApiError("translation_failed", self.last_error, retryable=True)
            if not self.process or self.state != "ready":
                raise ApiError(
                    "translation_unavailable", f"翻譯模型目前狀態為 {self.state}。", retryable=True
                )
            request_id = str(uuid.uuid4())
            body = json.dumps(
                {**payload, "request_id": request_id}, ensure_ascii=False, separators=(",", ":")
            ).encode()
            assert self.process.stdin is not None and self.process.stdout is not None
            self.process.stdin.write(struct.pack(">I", len(body)) + body)
            await self.process.stdin.drain()
            try:
                response = await asyncio.wait_for(
                    read_response(self.process.stdout), self.task_timeout_s
                )
            except (TimeoutError, asyncio.IncompleteReadError, ConnectionResetError) as exc:
                # A stuck Metal kernel cannot be interrupted any other way.
                await self.stop(kill=True)
                self.state = "recovering"
                timed_out = isinstance(exc, TimeoutError)
                self.last_error = (
                    f"翻譯超過 {self.task_timeout_s:g} 秒未完成" if timed_out
                    else f"翻譯 worker 連線中斷：{type(exc).__name__}"
                )
                self._schedule_restart()
                raise ApiError(
                    "translation_timeout" if timed_out else "translation_failed",
                    self.last_error,
                    retryable=True,
                ) from exc
            if response.get("request_id") != request_id:
                raise ApiError("translation_failed", "翻譯 worker 回應 ID 不符", retryable=True)
            if response.get("status") != "ok":
                raise ApiError(
                    str(response.get("code", "translation_failed")),
                    str(response.get("message", "翻譯失敗")),
                    retryable=True,
                )
            return response

    async def start_session(self, direction: str, latency_mode: str) -> int:
        """Reset the worker's history for a new session; returns the generation."""

        await self._request({"op": "start", "direction": direction, "latency_mode": latency_mode})
        return self.generation

    async def translate(self, text: str, *, force: bool) -> dict[str, Any]:
        return await self._request({"op": "translate", "text": text, "force": force})

    # -- recovery ------------------------------------------------------------

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
                break
            self._restarts.append(now)
            await asyncio.sleep(delay)
            try:
                await self.start()
            except ApiError:
                if self.state == "failed" and missing_model_reason(self.model_path):
                    return
                continue
            event(logger, "translation.restarted", generation=self.generation)
            return
        self.state = "failed"
        self.last_error = (
            f"翻譯 worker 在 {int(RESTART_WINDOW_S)} 秒內重啟 {MAX_RESTARTS_PER_WINDOW} 次仍失敗，"
            "已停止重試；請重新啟動服務。"
        )
        event(logger, "translation.gave_up", level="warning")
