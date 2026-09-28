"""The ASR worker pipe stays in sync however a caller goes away.

Production 2026-09-28: an OBS reconnect closed a session while its preview was
on the worker. The cancelled preview left its response unread, the next request
read it instead of its own, and every request after that failed with
`invalid_ipc` for the rest of the process's life. These tests drive a real
subprocess (`tests/fake_asr_worker.py`) over the real framed pipe.
"""

from __future__ import annotations

import asyncio
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

    texts, generation = asyncio.run(asyncio.wait_for(scenario(), timeout=4.5))
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

    unsent, text = asyncio.run(asyncio.wait_for(scenario(), timeout=4.5))
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

    code, texts, generation = asyncio.run(asyncio.wait_for(scenario(), timeout=4.5))
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

    replaced, generation = asyncio.run(asyncio.wait_for(scenario(), timeout=4.5))
    # Nothing but the 0.3 s task timeout can have replaced the process here.
    assert replaced and generation == 2


def test_inference_timeout_kills_and_reaps_a_hung_worker_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_ASR_HANG", "1")

    async def scenario() -> None:
        worker = make(task_timeout_s=0.1)
        await worker.start()
        process = worker.process
        assert process is not None
        inference = asyncio.create_task(worker.transcribe(pcm(100)))
        started = asyncio.get_running_loop().time()
        error: WorkerError | None = None
        try:
            try:
                await asyncio.wait_for(asyncio.shield(inference), 1)
            except WorkerError as exc:
                error = exc
            except TimeoutError as exc:
                raise AssertionError("hung worker was not reaped within one second") from exc
            elapsed = asyncio.get_running_loop().time() - started

            assert error is not None and error.code == "inference_timeout"
            assert elapsed < 1, f"timeout cleanup took {elapsed:.3f}s"
            assert process.returncode is not None, "the hung child must be reaped before return"
            assert worker._recovering is not None, "timeout must schedule a restart"
        finally:
            if process.returncode is None:
                process.kill()
            await asyncio.wait_for(process.wait(), 1)
            if not inference.done():
                await asyncio.wait_for(asyncio.gather(inference, return_exceptions=True), 1)
            await asyncio.wait_for(worker.stop(), 2)

    asyncio.run(asyncio.wait_for(scenario(), timeout=4.5))


def test_stop_waits_for_the_exchange_on_the_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_ASR_DELAY_S", "0.2")

    async def scenario() -> tuple[str, str]:
        worker = make()
        await worker.start()
        running = asyncio.create_task(worker.transcribe(pcm(100)))
        await asyncio.sleep(0.05)
        await worker.stop()  # idle unload, after-wake probe, shutdown
        return (await running)["text"], worker.state

    text, state = asyncio.run(asyncio.wait_for(scenario(), timeout=4.5))
    assert text == "samples=100", "the request on the pipe finishes instead of being cut off"
    assert state == "unprepared"


def test_stop_cancels_restart_while_subprocess_spawn_is_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        worker = make()
        spawn_entered = asyncio.Event()
        allow_spawn_return = asyncio.Event()
        children: list[asyncio.subprocess.Process] = []
        original_spawn = asyncio.create_subprocess_exec

        async def delayed_spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
            child = await original_spawn(*args, **kwargs)  # type: ignore[arg-type]
            children.append(child)
            spawn_entered.set()
            await allow_spawn_return.wait()
            return child

        monkeypatch.setattr(supervisor_module.asyncio, "create_subprocess_exec", delayed_spawn)
        worker._schedule_restart()
        restart = worker._recovering
        stop_task: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(spawn_entered.wait(), 2)
            stop_task = asyncio.create_task(worker.stop())
            await asyncio.sleep(0.02)
            stop_waited_for_spawn = not stop_task.done()
            allow_spawn_return.set()
            await asyncio.wait_for(stop_task, 2)
            assert restart is not None
            await asyncio.wait_for(asyncio.gather(restart, return_exceptions=True), 2)

            assert stop_waited_for_spawn, "stop must await and reap a start already spawning"
            assert restart.cancelled(), "stop must cancel the restart loop"
            assert worker.process is None
            assert worker.state in {"unprepared", "failed"}
            assert children and children[0].returncode is not None
        finally:
            allow_spawn_return.set()
            if restart is not None and not restart.done():
                restart.cancel()
                await asyncio.wait_for(asyncio.gather(restart, return_exceptions=True), 2)
            if stop_task is not None and not stop_task.done():
                stop_task.cancel()
                await asyncio.wait_for(asyncio.gather(stop_task, return_exceptions=True), 2)
            for child in children:
                if child.returncode is None:
                    child.kill()
                await asyncio.wait_for(child.wait(), 2)
            await asyncio.wait_for(worker.stop(), 2)

    asyncio.run(asyncio.wait_for(scenario(), timeout=4.5))


def test_stop_supersedes_concurrent_start_while_subprocess_spawn_is_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        worker = make()
        spawn_entered = asyncio.Event()
        allow_spawn_return = asyncio.Event()
        children: list[asyncio.subprocess.Process] = []
        original_spawn = asyncio.create_subprocess_exec

        async def delayed_spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
            child = await original_spawn(*args, **kwargs)  # type: ignore[arg-type]
            children.append(child)
            spawn_entered.set()
            await allow_spawn_return.wait()
            return child

        monkeypatch.setattr(supervisor_module.asyncio, "create_subprocess_exec", delayed_spawn)
        starting = asyncio.create_task(worker.start())
        stopping: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(spawn_entered.wait(), 2)
            stopping = asyncio.create_task(worker.stop())
            await asyncio.sleep(0.02)
            stop_waited_for_spawn = not stopping.done()
            allow_spawn_return.set()
            await asyncio.wait_for(stopping, 2)
            result = await asyncio.wait_for(
                asyncio.gather(starting, return_exceptions=True), 2
            )

            assert stop_waited_for_spawn, "stop must await the concurrent start's spawn"
            assert isinstance(result[0], WorkerError), "superseded start must return WorkerError"
            assert result[0].code == "model_unavailable"
            assert not starting.cancelled(), "stop must not cancel the start caller"
            assert starting.cancelling() == 0
            assert worker.process is None
            assert worker.state in {"unprepared", "failed"}
            assert children and children[0].returncode is not None
        finally:
            allow_spawn_return.set()
            if not starting.done():
                starting.cancel()
                await asyncio.wait_for(asyncio.gather(starting, return_exceptions=True), 2)
            if stopping is not None and not stopping.done():
                stopping.cancel()
                await asyncio.wait_for(asyncio.gather(stopping, return_exceptions=True), 2)
            for child in children:
                if child.returncode is None:
                    child.kill()
                await asyncio.wait_for(child.wait(), 2)
            await asyncio.wait_for(worker.stop(), 2)

    asyncio.run(asyncio.wait_for(scenario(), timeout=4.5))


def test_external_cancellation_of_start_reaps_pending_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        worker = make()
        spawn_entered = asyncio.Event()
        allow_spawn_return = asyncio.Event()
        children: list[asyncio.subprocess.Process] = []
        original_spawn = asyncio.create_subprocess_exec

        async def delayed_spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
            child = await original_spawn(*args, **kwargs)  # type: ignore[arg-type]
            children.append(child)
            spawn_entered.set()
            await allow_spawn_return.wait()
            return child

        monkeypatch.setattr(supervisor_module.asyncio, "create_subprocess_exec", delayed_spawn)
        starting = asyncio.create_task(worker.start())
        try:
            await asyncio.wait_for(spawn_entered.wait(), 2)
            starting.cancel()
            allow_spawn_return.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(starting, 2)

            assert starting.cancelled()
            assert worker.process is None
            assert children and children[0].returncode is not None
        finally:
            allow_spawn_return.set()
            if not starting.done():
                starting.cancel()
                await asyncio.wait_for(asyncio.gather(starting, return_exceptions=True), 2)
            for child in children:
                if child.returncode is None:
                    child.kill()
                await asyncio.wait_for(child.wait(), 2)
            await asyncio.wait_for(worker.stop(), 2)

    asyncio.run(asyncio.wait_for(scenario(), timeout=4.5))


def test_stop_does_not_interrupt_external_start_spawn_reaping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        worker = make()
        spawn_entered = asyncio.Event()
        allow_spawn_return = asyncio.Event()
        children: list[asyncio.subprocess.Process] = []
        original_spawn = asyncio.create_subprocess_exec

        async def delayed_spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
            child = await original_spawn(*args, **kwargs)  # type: ignore[arg-type]
            children.append(child)
            spawn_entered.set()
            await allow_spawn_return.wait()
            return child

        monkeypatch.setattr(supervisor_module.asyncio, "create_subprocess_exec", delayed_spawn)
        starting = asyncio.create_task(worker.start())
        stopping: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(spawn_entered.wait(), 2)
            internal = worker._starting_task
            assert internal is not None
            starting.cancel()
            await asyncio.sleep(0)
            assert internal.cancelling() == 1

            stopping = asyncio.create_task(worker.stop())
            await asyncio.sleep(0)
            allow_spawn_return.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(starting, 2)
            await asyncio.wait_for(stopping, 2)

            assert worker.process is None
            assert children and children[0].returncode is not None
        finally:
            allow_spawn_return.set()
            if not starting.done():
                starting.cancel()
                await asyncio.wait_for(asyncio.gather(starting, return_exceptions=True), 2)
            if stopping is not None and not stopping.done():
                stopping.cancel()
                await asyncio.wait_for(asyncio.gather(stopping, return_exceptions=True), 2)
            for child in children:
                if child.returncode is None:
                    child.kill()
                await asyncio.wait_for(child.wait(), 2)
            await asyncio.wait_for(worker.stop(), 2)

    asyncio.run(asyncio.wait_for(scenario(), timeout=4.5))


def test_external_cancellation_while_stop_reaps_spawn_waits_for_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        worker = make()
        spawn_entered = asyncio.Event()
        allow_spawn_return = asyncio.Event()
        children: list[asyncio.subprocess.Process] = []
        original_spawn = asyncio.create_subprocess_exec

        async def delayed_spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
            child = await original_spawn(*args, **kwargs)  # type: ignore[arg-type]
            children.append(child)
            spawn_entered.set()
            await allow_spawn_return.wait()
            return child

        monkeypatch.setattr(supervisor_module.asyncio, "create_subprocess_exec", delayed_spawn)
        starting = asyncio.create_task(worker.start())
        stopping: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(spawn_entered.wait(), 2)
            internal = worker._starting_task
            assert internal is not None
            stopping = asyncio.create_task(worker.stop())
            await asyncio.sleep(0)
            assert internal.cancelling() == 1

            starting.cancel()
            await asyncio.sleep(0)
            assert not starting.done(), "external cancellation must wait for stop's reap"
            allow_spawn_return.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(starting, 2)
            await asyncio.wait_for(stopping, 2)

            assert worker.process is None
            assert children and children[0].returncode is not None
        finally:
            allow_spawn_return.set()
            if not starting.done():
                starting.cancel()
                await asyncio.wait_for(asyncio.gather(starting, return_exceptions=True), 2)
            if stopping is not None and not stopping.done():
                stopping.cancel()
                await asyncio.wait_for(asyncio.gather(stopping, return_exceptions=True), 2)
            for child in children:
                if child.returncode is None:
                    child.kill()
                await asyncio.wait_for(child.wait(), 2)
            await asyncio.wait_for(worker.stop(), 2)

    asyncio.run(asyncio.wait_for(scenario(), timeout=4.5))


def test_concurrent_start_calls_spawn_only_one_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        worker = make()
        first_spawned = asyncio.Event()
        second_spawned = asyncio.Event()
        allow_spawn_return = asyncio.Event()
        children: list[asyncio.subprocess.Process] = []
        original_spawn = asyncio.create_subprocess_exec

        async def delayed_spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
            child = await original_spawn(*args, **kwargs)  # type: ignore[arg-type]
            children.append(child)
            (first_spawned if len(children) == 1 else second_spawned).set()
            await allow_spawn_return.wait()
            return child

        monkeypatch.setattr(supervisor_module.asyncio, "create_subprocess_exec", delayed_spawn)
        starts = [asyncio.create_task(worker.start())]
        try:
            await asyncio.wait_for(first_spawned.wait(), 2)
            starts.append(asyncio.create_task(worker.start()))
            try:
                await asyncio.wait_for(second_spawned.wait(), 0.05)
            except TimeoutError:
                pass
            spawned_before_release = len(children)
            allow_spawn_return.set()
            await asyncio.wait_for(asyncio.gather(*starts), 2)

            assert spawned_before_release == 1, "a concurrent start must share the active spawn"
            assert len(children) == 1
            assert worker.generation == 1
            assert worker.process is children[0]
        finally:
            allow_spawn_return.set()
            if any(not task.done() for task in starts):
                for task in starts:
                    if not task.done():
                        task.cancel()
                await asyncio.wait_for(asyncio.gather(*starts, return_exceptions=True), 2)
            for child in children:
                if child.returncode is None:
                    child.kill()
                await asyncio.wait_for(child.wait(), 2)
            await asyncio.wait_for(worker.stop(), 2)

    asyncio.run(asyncio.wait_for(scenario(), timeout=4.5))
