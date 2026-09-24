"""`transcript.stable` over the real WebSocket route (docs/04「穩定字幕流」)."""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, FakeSupervisor, build_client
from tests.integration.test_ws_contract import START, frame

REVISABLE = {**START, "transcript_mode": "revisable"}
STEP_FRAMES = 8  # 8 x 1600 samples = the 800 ms preview step


class ScriptedSupervisor(FakeSupervisor):
    """Text depends only on how much audio was sent, so replays are exact."""

    def __init__(self, script: list[tuple[int, str]]) -> None:
        super().__init__()
        self.script = script

    async def transcribe(self, pcm: bytes, *, language: str = "Chinese") -> dict[str, Any]:
        result = await super().transcribe(pcm, language=language)
        samples = len(pcm) // 2
        result["text"] = next(text for limit, text in self.script if samples < limit)
        return result


SCRIPT = [
    (25_600, "我們需要權限"),
    (38_400, "我們需要全線停駛"),
    (44_800, "我們需要全線停駛，才能"),
    (10**9, "我們需要全線停駛，才能進行維修。"),
]


@pytest.fixture(autouse=True)
def no_preview_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    # Wall-clock throttling would make the number of partials depend on test speed.
    monkeypatch.setattr("tea_asr.api.stream.PREVIEW_MIN_INTERVAL_S", 0.0)


def receive_until(socket: Any, event_type: str, into: list[dict[str, Any]]) -> dict[str, Any]:
    while True:
        event = socket.receive_json()
        into.append(event)
        if event["type"] == event_type:
            return event


def dialogue(script: list[tuple[int, str]], start: dict[str, Any]) -> list[dict[str, Any]]:
    """Three 800 ms previews, 400 ms more, commit, stop; every event received."""

    events: list[dict[str, Any]] = []
    with build_client(ScriptedSupervisor(script), revisable_preview=True) as http, (
        http.websocket_connect("/v1/stream", headers=AUTH)
    ) as socket:
        socket.receive_json()
        socket.send_json(start)
        receive_until(socket, "session.started", events)
        seq = 0
        for _ in range(3):
            for _ in range(STEP_FRAMES):
                socket.send_bytes(frame(seq, seq * 1600))
                seq += 1
            receive_until(socket, "transcript.partial", events)
        for _ in range(4):
            socket.send_bytes(frame(seq, seq * 1600))
            seq += 1
        socket.send_json({"type": "audio.commit", "request_id": "c1", "through_seq": seq - 1})
        receive_until(socket, "transcript.final", events)
        socket.send_json({"type": "session.stop", "request_id": "s1", "through_seq": seq - 1})
        receive_until(socket, "session.stopped", events)
    return events


def comparable(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop what legitimately differs between two runs (ids, timing, coalesced acks)."""

    out = []
    for event in events:
        if event["type"] in {"audio.ack", "flow.control", "transcript.stable"}:
            continue
        item = {k: v for k, v in event.items() if k not in {"session_id", "event_id"}}
        for key in ("segment_id", "queue_ms", "inference_ms"):
            if key in item and item[key] is not None:
                item[key] = "<varies>"
        out.append(item)
    return out


def test_stable_is_append_only_and_closes_with_the_final() -> None:
    events = dialogue(SCRIPT, {**REVISABLE, "stable": {"agreement": 2}})
    stable = [e for e in events if e["type"] == "transcript.stable"]
    final = next(e for e in events if e["type"] == "transcript.final")

    assert [(e["text"], e["state"]) for e in stable] == [
        ("我們需要", "open"),
        ("我們需要全線停駛", "open"),
        ("我們需要全線停駛，才能進行維修。", "final"),
    ]
    assert [e["stable_revision"] for e in stable] == [1, 2, 3]
    for older, newer in itertools.pairwise(stable):
        assert newer["text"].startswith(older["text"])
        assert newer["text"].encode().startswith(older["text"].encode())
    assert {e["segment_id"] for e in stable} == {final["segment_id"]}
    closing = stable[-1]
    assert events.index(closing) == events.index(final) + 1
    assert closing["text"] == final["text"]
    assert closing["source_revision"] == final["revision"]
    assert closing["diverged_chars"] == 0
    assert closing["end_sample"] == final["end_sample"]


def test_agreement_three_waits_for_one_more_partial() -> None:
    events = dialogue(SCRIPT, {**REVISABLE, "stable": {"agreement": 3}})
    stable = [(e["text"], e["state"]) for e in events if e["type"] == "transcript.stable"]
    assert stable == [("我們需要", "open"), ("我們需要全線停駛，才能進行維修。", "final")]


def test_diverged_final_keeps_what_was_shown_and_final_stays_authoritative() -> None:
    diverging = [*SCRIPT[:-1], (10**9, "我門需要全線停駛，才能進行維修。")]
    events = dialogue(diverging, {**REVISABLE, "stable": {"agreement": 2}})
    stable = [e for e in events if e["type"] == "transcript.stable"]
    final = next(e for e in events if e["type"] == "transcript.final")

    assert final["text"] == "我門需要全線停駛，才能進行維修。", "final is never rewritten"
    closing = stable[-1]
    assert closing["state"] == "diverged"
    assert closing["diverged_chars"] == 7  # 們需要全線停駛: from the first mismatch on
    assert closing["text"].startswith(stable[-2]["text"])
    assert closing["text"] == "我們需要全線停駛，才能進行維修。"


def test_without_stable_request_nothing_changes() -> None:
    plain = dialogue(SCRIPT, REVISABLE)
    with_stable = dialogue(SCRIPT, {**REVISABLE, "stable": {"agreement": 2}})

    assert not [e for e in plain if e["type"] == "transcript.stable"]
    assert comparable(plain) == comparable(with_stable)
    # Field by field, including keys that are null: no event grew a field.
    for event in plain:
        twin = next(
            e
            for e in with_stable
            if e["type"] == event["type"] and e.get("revision") == event.get("revision")
        )
        assert set(twin) == set(event)


def test_stable_requires_revisable(supervisor: FakeSupervisor) -> None:
    with build_client(supervisor, revisable_preview=True) as http, http.websocket_connect(
        "/v1/stream", headers=AUTH
    ) as socket:
        socket.receive_json()
        socket.send_json({**START, "stable": {"agreement": 2}})
        error = socket.receive_json()
    assert error["type"] == "error"
    assert error["code"] == "unsupported_option"


@pytest.mark.parametrize("agreement", [1, 4, "2"])
def test_unmeasured_agreement_is_refused(supervisor: FakeSupervisor, agreement: Any) -> None:
    with build_client(supervisor, revisable_preview=True) as http, http.websocket_connect(
        "/v1/stream", headers=AUTH
    ) as socket:
        socket.receive_json()
        socket.send_json({**REVISABLE, "stable": {"agreement": agreement}})
        error = socket.receive_json()
    assert error["type"] == "error"
    assert error["code"] == "unsupported_option"


def test_capabilities_offer_stable_only_with_revisable_preview(
    supervisor: FakeSupervisor, client: TestClient
) -> None:
    with build_client(supervisor, revisable_preview=True) as http:
        on = http.get("/v1/capabilities", headers=AUTH).json()
    with client as http:
        off = http.get("/v1/capabilities", headers=AUTH).json()
    assert on["features"]["stable_transcripts"] is True
    assert "stable_transcripts" not in off["features"]
