"""A session closed mid-preview leaves the real worker pipe usable (2026-09-28).

The production failure: OBS reconnected, the old session closed while its
preview was on the worker, and the new session got `invalid_ipc` on every
segment. This drives the real `WorkerSupervisor` over a real pipe to a fake
worker process (`tests/fake_asr_worker.py`), not the in-process FakeSupervisor,
because only a pipe can be left holding an unread response.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from tea_asr.worker.supervisor import WorkerSupervisor
from tests.conftest import AUTH, FakeVad, build_client
from tests.integration.test_ws_contract import CONTINUOUS, drain_until, send_continuous

REVISABLE = {**CONTINUOUS, "transcript_mode": "revisable", "segmentation": {"end_silence_ms": 300}}


def wait_until_busy(worker: WorkerSupervisor, timeout_s: float = 5.0) -> None:
    """Block until a request is on the pipe (read across threads: a plain bool)."""

    deadline = time.monotonic() + timeout_s
    while not worker._lock.locked():
        if time.monotonic() > deadline:
            raise AssertionError("no preview ever reached the worker")
        time.sleep(0.005)


def run_session_to_the_end(http: Any) -> list[dict[str, Any]]:
    with http.websocket_connect("/v1/stream", headers=AUTH) as socket:
        socket.receive_json()
        socket.send_json(REVISABLE)
        assert socket.receive_json()["type"] == "session.started"
        seq = send_continuous(socket, [("silence", 2), ("tone", 8), ("silence", 6)])
        socket.send_json({"type": "session.stop", "request_id": "s1", "through_seq": seq - 1})
        return drain_until(socket, "session.stopped")


@pytest.mark.parametrize("closes", [1, 3])
def test_a_session_closed_mid_preview_does_not_break_the_next_session(
    monkeypatch: pytest.MonkeyPatch, closes: int
) -> None:
    # Each "inference" takes long enough that a preview is certainly on the
    # worker when the socket goes away.
    monkeypatch.setenv("FAKE_ASR_DELAY_S", "0.15")
    worker = WorkerSupervisor(Path("unused"), worker_module="tests.fake_asr_worker")
    with build_client(
        worker,  # type: ignore[arg-type]
        revisable_preview=True,
        vad=FakeVad(),
        preview_min_interval_ms=100,
        preview_min_audio_ms=100,
    ) as http:
        for _ in range(closes):
            with http.websocket_connect("/v1/stream", headers=AUTH) as socket:
                socket.receive_json()
                socket.send_json(REVISABLE)
                assert socket.receive_json()["type"] == "session.started"
                send_continuous(socket, [("silence", 2), ("tone", 10)])
                drain_until(socket, "transcript.partial")
                send_continuous(socket, [("tone", 4)], first_seq=12)
                wait_until_busy(worker)
            # Leaving the block closes the socket abruptly, mid-speech.

        events = run_session_to_the_end(http)
        generation = worker.generation

    failures = [event for event in events if event["type"] in {"segment.error", "error"}]
    finals = [event for event in events if event["type"] == "transcript.final"]
    assert not failures, failures
    assert finals and all(final["text"].startswith("samples=") for final in finals)
    assert events[-1]["status"] == "completed"
    # In sync by construction, not by a restart after the fact.
    assert generation == 1
