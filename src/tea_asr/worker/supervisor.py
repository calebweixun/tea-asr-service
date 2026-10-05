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
from ..logs import event
from .protocol import MAX_HEADER_BYTES, MAX_PCM_BYTES, read_response
from .token_budget import max_tokens_for_pcm

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
    def __init__(
        self,
        model_path: Path,
        *,
        load_timeout_s: float = 120,
        task_timeout_s: float = 30,
        worker_module: str = "tea_asr.worker.entry",
    ) -> None:
        self.model_path = model_path
        self.worker_module = worker_module
        self.load_timeout_s = load_timeout_s
        self.task_timeout_s = task_timeout_s
        self.process: asyncio.subprocess.Process | None = None
        self.state = "unprepared"
        self.last_error: str | None = None
        self.load_ms: int | None = None
        self.generation = 0
        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._restarts: deque[float] = deque()
        self._recovering: asyncio.Task[None] | None = None
        self._starting_task: asyncio.Task[None] | None = None
        self._start_waiters: dict[asyncio.Task[None], int] = {}
        self._stop_generation = 0
        self._stopping = False

    async def start(self) -> None:
        if self._stopping:
            raise WorkerError("model_unavailable", "Worker is stopping")
        async with self._start_lock:
            if self._stopping:
                raise WorkerError("model_unavailable", "Worker start was superseded by stop")
            if self.process and self.process.returncode is None:
                return
            starting = self._starting_task
            if starting is None or starting.done():
                starting = asyncio.create_task(self._run_start(self._stop_generation))
                self._starting_task = starting
            self._start_waiters[starting] = self._start_waiters.get(starting, 0) + 1

        caller_cancelled = False
        try:
            await asyncio.shield(starting)
        except asyncio.CancelledError:
            caller = asyncio.current_task()
            caller_cancelled = caller is not None and caller.cancelling() > 0
            if not caller_cancelled:
                raise WorkerError(
                    "model_unavailable", "Worker start was superseded by stop"
                ) from None
            raise
        finally:
            await self._release_start_waiter(starting, cancel_if_unobserved=caller_cancelled)

    async def _run_start(self, generation: int) -> None:
        current = asyncio.current_task()
        try:
            await self._start_locked(generation)
        finally:
            if self._starting_task is current:
                self._starting_task = None

    async def _release_start_waiter(
        self,
        starting: asyncio.Task[None],
        *,
        cancel_if_unobserved: bool,
    ) -> None:
        wait_for_reap = False
        async with self._start_lock:
            waiters = self._start_waiters.get(starting, 0)
            if waiters <= 1:
                self._start_waiters.pop(starting, None)
                if cancel_if_unobserved and not starting.done():
                    if not starting.cancelling():
                        starting.cancel()
                    wait_for_reap = True
            else:
                self._start_waiters[starting] = waiters - 1
        if wait_for_reap:
            # A cancelled caller must not leave an unobserved spawn running.
            # _start_locked shields spawn setup and reaps its process handle.
            await asyncio.gather(starting, return_exceptions=True)

    async def _start_locked(self, generation: int) -> None:
        self.state = "loading"
        self.last_error = None
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                self.worker_module,
                "--model-path",
                str(self.model_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        )
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            # Do not cancel subprocess pipe setup. Let it return its handle,
            # then reap the child before this start task exits.
            try:
                process = await asyncio.shield(spawn)
            except Exception as exc:  # noqa: BLE001 - preserve the cancellation that interrupted start
                logger.debug("Worker spawn failed during cancelled start: %s", exc)
            else:
                self.process = process
                await self._terminate(kill=True)
            raise
        except Exception as exc:
            self.state = "failed"
            self.last_error = f"Worker could not be started: {exc}"
            raise WorkerError("model_unavailable", self.last_error) from exc

        self.process = process
        try:
            assert process.stdout is not None
            ready = await asyncio.wait_for(read_response(process.stdout), self.load_timeout_s)
        except asyncio.CancelledError:
            await self._terminate(kill=True)
            raise
        except Exception as exc:
            await self._terminate(kill=True)
            self.state = "failed"
            self.last_error = f"Worker did not become ready: {exc}"
            raise WorkerError("model_unavailable", self.last_error) from exc
        if ready.get("status") != "ready":
            await self._terminate(kill=True)
            self.state = "failed"
            self.last_error = str(ready.get("message", "Model load failed"))
            raise WorkerError(str(ready.get("code", "model_unavailable")), self.last_error)
        if self._stopping or generation != self._stop_generation:
            await self._terminate(kill=True)
            raise WorkerError("model_unavailable", "Worker start was superseded by stop")
        self.load_ms = int(ready.get("load_ms", 0))
        self.generation += 1
        self.state = "ready"

    async def transcribe(
        self,
        pcm: bytes,
        *,
        language: str = "Chinese",
        system_prompt: str | None = None,
    ) -> dict[str, Any]:
        if len(pcm) > MAX_PCM_BYTES or len(pcm) % 2:
            raise ValueError("PCM must be even-length and no longer than 30 seconds")
        # Waiting for the pipe is the only part a caller may abandon: nothing
        # has been sent yet. From here one request and its response are one
        # unit. A caller cancelled mid-exchange (a session closing while its
        # preview is on the worker) gets its CancelledError at once, but the
        # exchange runs on in its own task and keeps the lock until the
        # response is read. Otherwise that response stays in the pipe and every
        # later request reads the one before it (2026-09-28: `invalid_ipc` on
        # every segment after an OBS reconnect).
        await self._lock.acquire()
        try:
            exchange = asyncio.get_running_loop().create_task(
                self._exchange(pcm, language, system_prompt, max_tokens_for_pcm(pcm))
            )
        except BaseException:
            self._lock.release()
            raise
        exchange.add_done_callback(self._exchange_done)
        return await asyncio.shield(exchange)

    def _exchange_done(self, exchange: asyncio.Task[dict[str, Any]]) -> None:
        self._lock.release()
        if not exchange.cancelled():
            # Retrieved here too: a caller that went away never reads it.
            exchange.exception()

    async def _exchange(
        self, pcm: bytes, language: str, system_prompt: str | None, max_tokens: int
    ) -> dict[str, Any]:
        """Write one request and read its response; the caller holds the lock."""

        process = self.process
        if process and process.returncode is not None:
            # The worker died between requests. Say so for this segment and
            # bring it back for the next one.
            self.last_error = f"Worker exited with code {process.returncode}"
            self.state = "recovering"
            self._schedule_restart()
            raise WorkerError("inference_failed", self.last_error)
        if not process or self.state != "ready":
            raise WorkerError("model_unavailable", f"ASR worker is {self.state}")
        request_id = str(uuid.uuid4())
        request = {
            "ipc_version": 1,
            "request_id": request_id,
            "pcm_bytes": len(pcm),
            "sample_rate": 16_000,
            "language": language,
            "max_tokens": max_tokens,
        }
        if system_prompt is not None:
            request["system_prompt"] = system_prompt
        header = json.dumps(request, separators=(",", ":")).encode()
        if len(header) > MAX_HEADER_BYTES:
            raise ValueError("IPC header is too large")
        assert process.stdin is not None and process.stdout is not None
        stdin, stdout = process.stdin, process.stdout

        async def round_trip() -> dict[str, Any]:
            stdin.write(struct.pack(">I", len(header)) + header + pcm)
            await stdin.drain()
            return await read_response(stdout)

        try:
            response = await asyncio.wait_for(round_trip(), self.task_timeout_s)
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionError) as exc:
            # A hung or dead worker takes the whole process down and comes
            # back; a timed-out Metal kernel cannot be interrupted by waiting.
            await self._terminate(kill=isinstance(exc, TimeoutError))
            self.state = "recovering"
            self.last_error = (
                "Inference timed out"
                if isinstance(exc, TimeoutError)
                else f"Worker connection lost: {type(exc).__name__}"
            )
            self._schedule_restart()
            code = "inference_timeout" if isinstance(exc, TimeoutError) else "inference_failed"
            raise WorkerError(code, self.last_error) from exc
        except (ValueError, TypeError) as exc:
            # Oversized or undecodable frame: the stream position is unknown.
            raise await self._resync(
                f"unreadable response: {exc}", expected=request_id, got=None
            ) from exc
        except asyncio.CancelledError:
            # Only the event loop shutting down gets here (callers are behind
            # the shield). The frame is half done, so this process must not
            # serve another request; the next one sees it dead and restarts it.
            if process.returncode is None:
                process.kill()
            raise
        got = response.get("request_id") if isinstance(response, dict) else None
        if got != request_id:
            raise await self._resync("response ID does not match", expected=request_id, got=got)
        if response.get("status") != "ok":
            raise WorkerError(
                str(response.get("code", "inference_failed")),
                str(response.get("message", "Inference failed")),
            )
        return response

    async def _resync(self, reason: str, *, expected: str, got: object) -> WorkerError:
        """The pipe is out of step with its requests: restart, fail only this one.

        Nothing read from this pipe can be trusted to belong to the request
        that reads it, so the only way back in step is a new process. Without
        this, one stray frame fails every later request until the service is
        restarted by hand.
        """

        event(
            logger,
            "worker.ipc_desync",
            level="warning",
            reason=reason,
            expected_request_id=expected,
            got_request_id=got,
            generation=self.generation,
        )
        await self._terminate(kill=True)
        self.state = "recovering"
        self.last_error = f"Worker IPC out of sync ({reason}); restarting the worker"
        self._schedule_restart()
        return WorkerError("invalid_ipc", self.last_error)

    def _schedule_restart(self) -> None:
        if self._stopping:
            return
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
        async with self._stop_lock:
            self._stopping = True
            self._stop_generation += 1
            current = asyncio.current_task()
            try:
                restart = self._recovering
                if restart is not None and restart is not current and not restart.done():
                    restart.cancel()
                    await asyncio.gather(restart, return_exceptions=True)

                async with self._start_lock:
                    starting = self._starting_task
                    if (
                        starting is not None
                        and starting is not current
                        and not starting.done()
                        and not starting.cancelling()
                    ):
                        starting.cancel()
                if starting is not None and starting is not current:
                    await asyncio.gather(starting, return_exceptions=True)

                # After the exchange on the pipe, if any: cutting it off would
                # fail that request for nothing, and its caller may be gone.
                async with self._lock:
                    await self._terminate()
                if self.state != "failed":
                    self.state = "unprepared"
                if self._recovering is restart:
                    self._recovering = None
            finally:
                self._stopping = False

    async def _terminate(self, *, kill: bool = False) -> None:
        """Stop the process now; for callers that already hold the lock."""

        process, self.process = self.process, None
        if process is None:
            return
        if kill and process.returncode is None:
            # Its output can no longer be trusted, so do not wait on it.
            process.kill()
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
