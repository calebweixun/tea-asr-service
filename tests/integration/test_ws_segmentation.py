"""`session.start.segmentation` over the real WebSocket route (docs/04「切段控制」)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tea_asr.api import stream
from tea_asr.segmenter import SegmenterConfig
from tests.conftest import AUTH, FakeSupervisor, FakeVad, build_client
from tests.integration.test_ws_contract import (
    CONTINUOUS,
    START,
    drain_until,
    send_continuous,
)

REVISABLE = {**CONTINUOUS, "transcript_mode": "revisable"}


def started_event(start: dict[str, Any]) -> dict[str, Any]:
    with build_client(FakeSupervisor(), revisable_preview=True, vad=FakeVad()) as http, (
        http.websocket_connect("/v1/stream", headers=AUTH)
    ) as socket:
        socket.receive_json()
        socket.send_json(start)
        return socket.receive_json()


def refused(start: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """The error event and the close code a rejected session.start gets."""

    with build_client(FakeSupervisor(), revisable_preview=True, vad=FakeVad()) as http, (
        http.websocket_connect("/v1/stream", headers=AUTH)
    ) as socket:
        socket.receive_json()
        socket.send_json(start)
        error = socket.receive_json()
        with pytest.raises(WebSocketDisconnect) as caught:
            socket.receive_json()
    return error, caught.value.code


def finals_for(start: dict[str, Any], pattern: list[tuple[str, int]]) -> list[dict[str, Any]]:
    with build_client(FakeSupervisor(), revisable_preview=True, vad=FakeVad()) as http, (
        http.websocket_connect("/v1/stream", headers=AUTH)
    ) as socket:
        socket.receive_json()
        socket.send_json(start)
        assert socket.receive_json()["type"] == "session.started"
        seq = send_continuous(socket, pattern)
        socket.send_json({"type": "session.stop", "request_id": "s1", "through_seq": seq - 1})
        events = drain_until(socket, "session.stopped")
    return [event for event in events if event["type"] == "transcript.final"]


# --- accepted ----------------------------------------------------------------


@pytest.mark.parametrize("end_silence_ms", [300, 600, 1200, 3000])
def test_preview_policy_reports_the_requested_end_silence(end_silence_ms: int) -> None:
    started = started_event({**REVISABLE, "segmentation": {"end_silence_ms": end_silence_ms}})
    assert started["type"] == "session.started"
    assert started["preview_policy"]["endpoint_silence_ms"] == end_silence_ms


def test_omitted_segmentation_keeps_the_revisable_default() -> None:
    started = started_event(REVISABLE)
    assert started["preview_policy"]["endpoint_silence_ms"] == 900


@pytest.mark.parametrize(
    ("start", "expected"),
    [
        (CONTINUOUS, SegmenterConfig()),
        (REVISABLE, SegmenterConfig(end_silence_ms=900)),
        (
            {**CONTINUOUS, "segmentation": {"end_silence_ms": 1200}},
            SegmenterConfig(end_silence_ms=1200),
        ),
        (
            {**REVISABLE, "segmentation": {"end_silence_ms": 600}},
            SegmenterConfig(end_silence_ms=600),
        ),
    ],
    ids=["final_only-default", "revisable-default", "final_only-1200", "revisable-600"],
)
def test_only_end_silence_reaches_the_segmenter(
    monkeypatch: pytest.MonkeyPatch, start: dict[str, Any], expected: SegmenterConfig
) -> None:
    seen: list[SegmenterConfig] = []
    real = stream.ContinuousSegmenter

    def recording(vad: Any, config: SegmenterConfig, **hooks: Any) -> Any:
        seen.append(config)
        return real(vad, config, **hooks)

    monkeypatch.setattr(stream, "ContinuousSegmenter", recording)
    started_event(start)
    # Whole-config equality: min_speech, pre_roll, max_segment and the split
    # search all stay at their defaults.
    assert seen == [expected]


# 1 s of speech, a 700 ms pause, 1 s more speech, then a long silence.
PAUSE_700 = [("silence", 3), ("tone", 10), ("silence", 7), ("tone", 10), ("silence", 20)]


def test_a_longer_end_silence_keeps_a_pause_inside_one_segment() -> None:
    assert len(finals_for(CONTINUOUS, PAUSE_700)) == 2  # default 500 ms splits at 700
    kept = finals_for({**CONTINUOUS, "segmentation": {"end_silence_ms": 1200}}, PAUSE_700)
    assert len(kept) == 1


def test_a_shorter_end_silence_splits_at_a_pause_the_default_keeps() -> None:
    assert len(finals_for(REVISABLE, PAUSE_700)) == 1  # default 900 ms keeps it
    split = finals_for({**REVISABLE, "segmentation": {"end_silence_ms": 600}}, PAUSE_700)
    assert len(split) == 2
    assert split[0]["end_sample"] <= split[1]["start_sample"]


# --- refused -----------------------------------------------------------------


@pytest.mark.parametrize("value", [299, 3001, 0, -1, "900", 900.5, True, None])
def test_out_of_range_or_non_integer_end_silence_is_refused(value: Any) -> None:
    error, code = refused({**REVISABLE, "segmentation": {"end_silence_ms": value}})
    assert error["type"] == "error"
    assert error["code"] == "unsupported_option"
    assert "segmentation.end_silence_ms" in error["message"]
    assert code == 1008


def test_missing_end_silence_is_refused() -> None:
    error, code = refused({**REVISABLE, "segmentation": {}})
    assert error["code"] == "unsupported_option"
    assert code == 1008


def test_unknown_segmentation_key_is_refused() -> None:
    error, code = refused(
        {**REVISABLE, "segmentation": {"end_silence_ms": 900, "min_speech_ms": 100}}
    )
    # Same as any unknown client field (docs/04 error table).
    assert error["code"] == "protocol_error"
    assert code == 1008


@pytest.mark.parametrize("mode", ["final_only", "revisable"])
def test_segmentation_needs_the_continuous_profile(mode: str) -> None:
    error, code = refused(
        {**START, "transcript_mode": mode, "segmentation": {"end_silence_ms": 900}}
    )
    assert error["code"] == "unsupported_option"
    assert "continuous" in error["message"]
    assert code == 1008


# --- capabilities --------------------------------------------------------------


def test_capabilities_advertise_the_enforced_range_only_with_continuous(
    client: TestClient, continuous_client: TestClient
) -> None:
    with continuous_client as http:
        on = http.get("/v1/capabilities", headers=AUTH).json()
    with client as http:
        off = http.get("/v1/capabilities", headers=AUTH).json()
    assert on["features"]["segmentation_control"] == {
        "end_silence_ms": {"min": 300, "max": 3000, "default": 900, "default_final_only": 500}
    }
    assert "segmentation_control" not in off["features"]
