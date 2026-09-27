"""Per-session diagnostics over the real `/v1/stream` route (docs/04「W11」)."""

from __future__ import annotations

import logging
import time
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from tea_asr import diagnostics
from tea_asr.api import stream
from tea_asr.config import AppPaths
from tea_asr.diagnostics import WarningLimiter
from tests.conftest import AUTH, FakeSupervisor, FakeVad, build_client
from tests.integration.test_ws_contract import (
    CONTINUOUS,
    drain_until,
    send_continuous,
)

REVISABLE_STABLE = {**CONTINUOUS, "transcript_mode": "revisable", "stable": {"agreement": 2}}


@contextmanager
def records() -> Iterator[list[logging.LogRecord]]:
    collected: list[logging.LogRecord] = []

    class Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            collected.append(record)

    target = logging.getLogger("tea_asr.stream")
    handler = Collector(level=logging.DEBUG)
    previous = target.level
    target.addHandler(handler)
    target.setLevel(logging.DEBUG)
    try:
        yield collected
    finally:
        target.removeHandler(handler)
        target.setLevel(previous)


def named(collected: list[logging.LogRecord], message: str) -> list[dict[str, Any]]:
    return [r.fields for r in collected if r.getMessage() == message]  # type: ignore[attr-defined]


def run_session(
    http: Any, start: dict[str, Any], pattern: list[tuple[str, int]], *, pause_s: float = 0.0
) -> str:
    with http.websocket_connect("/v1/stream", headers=AUTH) as socket:
        socket.receive_json()
        socket.send_json(start)
        started = socket.receive_json()
        assert started["type"] == "session.started"
        seq = send_continuous(socket, pattern)
        if pause_s:
            time.sleep(pause_s)
        socket.send_json({"type": "session.stop", "request_id": "stop", "through_seq": seq - 1})
        drain_until(socket, "session.stopped")
    return str(started["session_id"])


def test_lifecycle_lines_cover_one_continuous_session() -> None:
    with records() as collected, build_client(
        FakeSupervisor(), revisable_preview=True, vad=FakeVad()
    ) as http:
        session_id = run_session(
            http, REVISABLE_STABLE, [("silence", 3), ("tone", 10), ("silence", 12)]
        )
        # session_ended is written during teardown, just after the close frame.
        deadline = time.monotonic() + 2
        while not named(collected, "stream.session_ended") and time.monotonic() < deadline:
            time.sleep(0.01)

    (started,) = named(collected, "stream.session_started")
    assert started["session_id"] == session_id
    assert started["profile"] == "continuous"
    assert started["transcript_mode"] == "revisable"
    assert started["stable_agreement"] == 2
    assert started["end_silence_ms"] == stream.REVISABLE_END_SILENCE_MS
    assert started["preview_min_audio_ms"] == 300
    assert started["preview_load_factor"] == 2.0
    assert started["capture_dir"] is None

    (speech,) = named(collected, "stream.speech_started")
    assert (speech["segment_index"], speech["cause"]) == (0, "vad")
    (closed,) = named(collected, "stream.segment_closed")
    assert closed["boundary"] == "silence"
    assert closed["audio_ms"] > 900
    (done,) = named(collected, "stream.segment_done")
    assert done["outcome"] == "final"
    assert done["chars"] == len("測試文字")
    assert done["stable_state"] in {"final", "diverged"}
    assert done["latency_ms"] >= 0

    (ended,) = named(collected, "stream.session_ended")
    assert ended["reason"] == "stopped"
    assert ended["finals"] == 1
    assert ended["frames"] == 25
    assert ended["boundaries"] == {"silence": 1}
    assert all(r.fields["session_id"] == session_id for r in collected)  # type: ignore[attr-defined]


def test_heartbeat_and_stall_warning_come_from_the_session_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stream, "TICK_S", 0.02)
    monkeypatch.setattr(diagnostics, "HEARTBEAT_INTERVAL_S", 0.1)
    monkeypatch.setattr(diagnostics, "AUDIO_STALL_WARN_S", 0.15)
    with records() as collected, build_client(FakeSupervisor(), vad=FakeVad()) as http:
        run_session(http, CONTINUOUS, [("tone", 5)], pause_s=0.5)

    beats = named(collected, "stream.heartbeat")
    assert len(beats) >= 2
    assert sum(beat["frames"] for beat in beats) <= 5
    assert {"rms_dbfs", "peak_dbfs", "vad_max", "vad_mean", "seg_state", "max_gap_ms"} <= set(
        beats[0]
    )
    assert beats[-1]["seg_state"] in {"silence", "pending", "speech"}
    (stall,) = named(collected, "stream.audio_stalled")
    assert stall["gap_ms"] >= 150
    assert [
        r.levelno for r in collected if r.getMessage() == "stream.audio_stalled"
    ] == [logging.WARNING]


def test_rejections_are_logged_with_their_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stream, "_rejection_limiter", WarningLimiter())
    with records() as collected, build_client(FakeSupervisor(), vad=FakeVad()) as http:
        with pytest.raises(WebSocketDisconnect), http.websocket_connect("/v1/stream") as socket:
            socket.receive_json()
        with pytest.raises(WebSocketDisconnect), http.websocket_connect("/v1/stream") as socket:
            socket.receive_json()
        with http.websocket_connect("/v1/stream", headers=AUTH) as socket:
            socket.receive_json()
            socket.send_json({**CONTINUOUS, "durable": True})
            assert socket.receive_json()["code"] == "unsupported_option"
        deadline = time.monotonic() + 2
        while not named(collected, "stream.session_rejected") and time.monotonic() < deadline:
            time.sleep(0.01)

    rejected = named(collected, "stream.connection_rejected")
    assert [(r["reason"], r["suppressed"]) for r in rejected] == [("unauthenticated", 0)]
    (refused,) = named(collected, "stream.session_rejected")
    assert refused["reason"] == "error:unsupported_option"
    assert named(collected, "stream.session_started") == []


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(support=tmp_path / "support", logs=tmp_path / "logs")


def test_debug_capture_writes_the_received_audio_only_when_enabled(tmp_path: Path) -> None:
    off = _paths(tmp_path / "off")
    with build_client(FakeSupervisor(), vad=FakeVad(), paths=off) as http:
        run_session(http, CONTINUOUS, [("tone", 5), ("silence", 5)])
    assert not (off.logs / "captures").exists()

    on = _paths(tmp_path / "on")
    with records() as collected, build_client(
        FakeSupervisor(), vad=FakeVad(), paths=on, debug_capture_audio=True
    ) as http:
        run_session(http, CONTINUOUS, [("tone", 5), ("silence", 5)])
        deadline = time.monotonic() + 2
        while not named(collected, "stream.session_ended") and time.monotonic() < deadline:
            time.sleep(0.01)
    (started,) = named(collected, "stream.session_started")
    folder = Path(started["capture_dir"])
    assert folder.parent == on.logs / "captures"
    assert started["capture_max_mb"] == pytest.approx(21.1)  # 11 one-minute files
    (chunk,) = folder.glob("chunk-*.wav")
    # The writer thread may still be flushing the last frames.
    deadline = time.monotonic() + 2
    while True:
        with wave.open(str(chunk)) as recorded:
            frames = recorded.getnframes()
        if frames == 10 * 1600 or time.monotonic() > deadline:
            break
        time.sleep(0.01)
    assert frames == 10 * 1600
