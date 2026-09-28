"""The ASR worker pipe stays in sync however a caller goes away.

Production 2026-09-28: an OBS reconnect closed a session while its preview was
on the worker. The cancelled preview left its response unread, the next request
read it instead of its own, and every request after that failed with
`invalid_ipc` for the rest of the process's life. These tests drive a real
subprocess (`tests/fake_asr_worker.py`) over the real framed pipe.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from tea_asr.worker import supervisor as supervisor_module
from tea_asr.worker.protocol import MAX_PCM_BYTES
from tea_asr.worker.supervisor import WorkerError, WorkerSupervisor

FAKE_WORKER = "tests.fake_asr_worker"


def pcm(samples: int) -> bytes:
    return b"\x00\x00" * samples


class Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def worker_log() -> Iterator[Records]:
    # A handler on the logger itself: the service's logging setup turns off
    # propagation on `tea_asr`, which would hide records from caplog.
    handler = Records()
    logger = logging.getLogger("tea_asr.worker")
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


@pytest.fixture(autouse=True)
def fast_restarts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supervisor_module, "RESTART_BACKOFF_S", (0.0, 0.0, 0.0))


def make(**kwargs: float) -> WorkerSupervisor:
    return WorkerSupervisor(Path("unused"), worker_module=FAKE_WORKER, **kwargs)  # type: ignore[arg-type]


async def until_ready(worker: WorkerSupervisor, generation: int) -> None:
    for _ in range(500):
        if worker.state == "ready" and worker.generation >= generation:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"worker never came back: {worker.state} gen={worker.generation}")


async def shutdown(worker: WorkerSupervisor) -> None:
    """Let a pending restart finish first, so a failing test fails, not hangs.

    Stopping while a restart is still spawning its process leaves asyncio's
    pipe setup pending, and `asyncio.run` then waits on it forever.
    """

    recovering = worker._recovering
    if recovering is not None and not recovering.done():
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(recovering), 10)
    await worker.stop()


async def cancel_after(task: asyncio.Task[object], delay: float) -> None:
    await asyncio.sleep(delay)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_a_caller_cancelled_during_inference_does_not_desync_the_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_ASR_DELAY_S", "0.3")

    async def scenario() -> tuple[list[str], int]:
        worker = make()
        await worker.start()
        try:
            preview = asyncio.create_task(worker.transcribe(pcm(100)))
            await cancel_after(preview, 0.1)  # the worker is still "inferring"
            texts = [
                (await worker.transcribe(pcm(200)))["text"],
                (await worker.transcribe(pcm(300)))["text"],
            ]
            return texts, worker.generation
        finally:
            await shutdown(worker)

    texts, generation = asyncio.run(scenario())
    assert texts == ["samples=200", "samples=300"], "each request gets its own response"
    assert generation == 1, "a cancelled caller must not cost a worker restart"


def test_a_caller_cancelled_while_its_request_is_still_being_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The worker waits before reading, so a 30 s request (960 KB, far more
    # than a pipe buffer) is still being written when the caller goes away.
    monkeypatch.setenv("FAKE_ASR_READ_DELAY_S", "0.4")

    async def scenario() -> tuple[int, str]:
        worker = make()
        await worker.start()
        try:
            big = asyncio.create_task(worker.transcribe(pcm(MAX_PCM_BYTES // 2)))
            await asyncio.sleep(0.1)
            assert worker.process is not None and worker.process.stdin is not None
            unsent = worker.process.stdin.transport.get_write_buffer_size()
            await cancel_after(big, 0)
            return unsent, (await worker.transcribe(pcm(100)))["text"]
        finally:
            await shutdown(worker)

    unsent, text = asyncio.run(scenario())
    assert unsent > 0, "the test must cancel mid-write to mean anything"
    assert text == "samples=100"


def test_a_mismatched_response_restarts_the_worker_and_the_next_request_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, worker_log: Records
) -> None:
    # Only the first process answers with a wrong ID; its replacement is healthy.
    monkeypatch.setenv("FAKE_ASR_MISMATCH_ONCE", str(tmp_path / "mismatched"))

    async def scenario() -> tuple[str, list[str], int]:
        worker = make()
        await worker.start()
        try:
            with pytest.raises(WorkerError) as caught:
                await worker.transcribe(pcm(100))
            await until_ready(worker, 2)
            texts = [
                (await worker.transcribe(pcm(200)))["text"],
                (await worker.transcribe(pcm(300)))["text"],
            ]
            return caught.value.code, texts, worker.generation
        finally:
            await shutdown(worker)

    code, texts, generation = asyncio.run(scenario())
    assert code == "invalid_ipc", "only the request that saw the mismatch fails"
    assert texts == ["samples=200", "samples=300"]
    assert generation == 2, "exactly one restart"
    desyncs = [record for record in worker_log.records if record.getMessage() == "worker.ipc_desync"]
    assert len(desyncs) == 1
    assert desyncs[0].levelno == logging.WARNING
    fields = desyncs[0].fields  # type: ignore[attr-defined]
    assert fields["got_request_id"] == "not-" + fields["expected_request_id"]


def test_the_timeout_still_restarts_a_worker_whose_caller_went_away(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_ASR_DELAY_S", "1")

    async def scenario() -> tuple[bool, int]:
        worker = make(task_timeout_s=0.3)
        await worker.start()
        try:
            assert worker.process is not None
            first_pid = worker.process.pid
            hung = asyncio.create_task(worker.transcribe(pcm(100)))
            await cancel_after(hung, 0.05)
            await until_ready(worker, 2)
            assert worker.process is not None
            return worker.process.pid != first_pid, worker.generation
        finally:
            await shutdown(worker)

    replaced, generation = asyncio.run(scenario())
    # Nothing but the 0.3 s task timeout can have replaced the process here.
    assert replaced and generation == 2


def test_stop_waits_for_the_exchange_on_the_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_ASR_DELAY_S", "0.2")

    async def scenario() -> tuple[str, str]:
        worker = make()
        await worker.start()
        running = asyncio.create_task(worker.transcribe(pcm(100)))
        await asyncio.sleep(0.05)
        await worker.stop()  # idle unload, after-wake probe, shutdown
        return (await running)["text"], worker.state

    text, state = asyncio.run(scenario())
    assert text == "samples=100", "the request on the pipe finishes instead of being cut off"
    assert state == "unprepared"
