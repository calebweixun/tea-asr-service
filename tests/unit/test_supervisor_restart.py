from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tea_asr.worker import supervisor as supervisor_module
from tea_asr.worker.supervisor import WorkerError, WorkerSupervisor


class DeadProcess:
    returncode = 137  # killed


def make_supervisor(monkeypatch: pytest.MonkeyPatch) -> WorkerSupervisor:
    monkeypatch.setattr(supervisor_module, "RESTART_BACKOFF_S", (0.0, 0.0, 0.0))
    monkeypatch.setattr(supervisor_module, "RESTART_WINDOW_S", 60.0)
    worker = WorkerSupervisor(Path("unused"))
    worker.state = "ready"
    worker.process = DeadProcess()  # type: ignore[assignment]
    return worker


def test_a_dead_worker_fails_the_segment_not_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> tuple[str, str]:
        worker = make_supervisor(monkeypatch)
        started: list[int] = []

        async def fake_start() -> None:
            started.append(1)
            worker.state = "ready"

        monkeypatch.setattr(worker, "start", fake_start)
        with pytest.raises(WorkerError) as caught:
            await worker.transcribe(b"\x00\x00" * 100)
        code = caught.value.code
        # The restart runs in the background; the caller is not held up by it.
        await asyncio.sleep(0.05)
        return code, worker.state

    code, state = asyncio.run(scenario())
    assert code == "inference_failed"
    assert state == "ready", "the worker should have been brought back"


def test_restarts_are_capped_so_a_broken_model_does_not_thrash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> WorkerSupervisor:
        worker = make_supervisor(monkeypatch)

        async def always_fails() -> None:
            raise WorkerError("model_unavailable", "nope")

        monkeypatch.setattr(worker, "start", always_fails)
        with pytest.raises(WorkerError):
            await worker.transcribe(b"\x00\x00" * 100)
        for _ in range(50):
            await asyncio.sleep(0.01)
            if worker.state == "failed":
                break
        return worker

    worker = asyncio.run(scenario())
    assert worker.state == "failed"
    assert worker.last_error is not None
    assert "giving up" in worker.last_error


def test_an_incompatible_checkpoint_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> WorkerSupervisor:
        worker = make_supervisor(monkeypatch)
        attempts: list[int] = []

        async def incompatible() -> None:
            attempts.append(1)
            raise WorkerError("model_incompatible", "quantisation mismatch")

        monkeypatch.setattr(worker, "start", incompatible)
        with pytest.raises(WorkerError):
            await worker.transcribe(b"\x00\x00" * 100)
        for _ in range(50):
            await asyncio.sleep(0.01)
            if worker.state == "failed":
                break
        assert len(attempts) == 1, "a wrong checkpoint cannot be fixed by retrying"
        return worker

    assert asyncio.run(scenario()).state == "failed"
