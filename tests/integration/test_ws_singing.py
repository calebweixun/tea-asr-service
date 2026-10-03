"""`segment.audio_class` over the real WebSocket route (docs/04「歌唱偵測」).

The YAMNet model is replaced by `FakeYamnet` (loud frames read as worship
singing, quiet ones as speech), so nothing here needs the downloaded asset.
"""

from __future__ import annotations

from typing import Any

import pytest

from tea_asr.wire import ServerEnvelope
from tests.conftest import (
    AUTH,
    FakeSupervisor,
    FakeVad,
    FakeYamnet,
    build_client,
    fake_singing_runtime,
)
from tests.integration.test_ws_contract import (
    CONTINUOUS,
    START,
    drain_until,
    send_audio,
    send_continuous,
)

#: 6 s of tone, 1.5 s of silence (closes the segment), 6 s of tone, then stop.
TWO_SEGMENTS = [("tone", 60), ("silence", 15), ("tone", 60)]


def run_continuous(client: Any, pattern: list[tuple[str, int]]) -> list[dict[str, Any]]:
    with client as http, http.websocket_connect("/v1/stream", headers=AUTH) as socket:
        socket.receive_json()
        socket.send_json(CONTINUOUS)
        assert socket.receive_json()["type"] == "session.started"
        seq = send_continuous(socket, pattern)
        socket.send_json({"type": "session.stop", "request_id": "s1", "through_seq": seq - 1})
        return drain_until(socket, "session.stopped")


def of_type(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [event for event in events if event["type"] == kind]


def singing_client(model: FakeYamnet | None = None) -> Any:
    return build_client(
        FakeSupervisor(), vad=FakeVad(), singing_runtime=fake_singing_runtime(model)
    )


def test_each_segment_gets_a_label_before_its_final_and_the_final_repeats_it() -> None:
    events = run_continuous(singing_client(), TWO_SEGMENTS)
    finals = of_type(events, "transcript.final")
    labels = of_type(events, "segment.audio_class")
    assert [f["segment_index"] for f in finals] == [0, 1]
    for final in finals:
        mine = [e for e in labels if e["segment_id"] == final["segment_id"]]
        assert 1 <= len(mine) <= 2  # one early label, at most one revision
        assert [e["revision"] for e in mine] == list(range(len(mine)))
        assert final["audio_class"] == mine[-1]["class"]
        # The label is on the wire before the final it describes.
        assert events.index(mine[0]) < events.index(final)
    # The first song segment opens before the 8-frame evidence exists
    # (precision first); by the second segment the session state is singing.
    assert finals[0]["audio_class"] == "speech"
    assert finals[1]["audio_class"] == "singing"


def test_audio_class_events_match_the_wire_schema() -> None:
    events = run_continuous(singing_client(), TWO_SEGMENTS)
    labels = of_type(events, "segment.audio_class")
    assert labels
    for event in labels:
        assert set(event) == {
            "type",
            "session_id",
            "event_id",
            "segment_id",
            "segment_index",
            "class",
            "confidence",
            "revision",
        }
        assert event["class"] in {"speech", "singing"}
        assert 0.0 <= event["confidence"] <= 1.0
        assert event["revision"] in {0, 1}
        parsed = ServerEnvelope.model_validate({"event": event}).event
        assert parsed.type == "segment.audio_class"
    for final in of_type(events, "transcript.final"):
        ServerEnvelope.model_validate({"event": final})


def test_early_label_is_due_about_one_and_a_half_seconds_into_the_segment() -> None:
    events = run_continuous(singing_client(), TWO_SEGMENTS)
    started = {e["segment_id"]: e["start_sample"] for e in of_type(events, "speech.started")}
    acks = of_type(events, "audio.ack")
    first = of_type(events, "segment.audio_class")[0]
    due = started[first["segment_id"]] + 24_000
    # The label is emitted right after the audio that makes it due arrived.
    emitted_after = [a for a in acks if events.index(a) < events.index(first)]
    assert emitted_after and emitted_after[-1]["received_sample"] >= due
    assert emitted_after[-1]["received_sample"] <= due + 3 * 1600 + 7680


def test_disabled_runtime_changes_nothing_on_the_wire() -> None:
    client = build_client(FakeSupervisor(), vad=FakeVad())  # no runtime: feature off
    events = run_continuous(client, TWO_SEGMENTS)
    assert of_type(events, "segment.audio_class") == []
    for final in of_type(events, "transcript.final"):
        assert "audio_class" not in final


def test_model_failure_does_not_touch_transcription() -> None:
    model = FakeYamnet(fail=RuntimeError("boom"))
    events = run_continuous(singing_client(model), TWO_SEGMENTS)
    finals = of_type(events, "transcript.final")
    assert len(finals) == 2
    assert of_type(events, "segment.audio_class") == []
    assert all("audio_class" not in final for final in finals)
    assert of_type(events, "error") == []


def test_utterance_profile_is_labelled_too() -> None:
    client = build_client(FakeSupervisor(), singing_runtime=fake_singing_runtime())
    with client as http, http.websocket_connect("/v1/stream", headers=AUTH) as socket:
        socket.receive_json()
        socket.send_json(START)
        socket.receive_json()
        # Silence reads as speech to the fake model, so this is a speech label.
        seq = send_audio(socket, 40)
        socket.send_json({"type": "audio.commit", "request_id": "c1", "through_seq": seq - 1})
        events = drain_until(socket, "transcript.final")
    labels = of_type(events, "segment.audio_class")
    final = of_type(events, "transcript.final")[0]
    assert [e["class"] for e in labels] == ["speech"]
    assert final["audio_class"] == "speech"


@pytest.mark.parametrize("with_runtime", [True, False])
def test_capabilities_advertise_the_feature_only_with_a_runtime(with_runtime: bool) -> None:
    runtime = fake_singing_runtime() if with_runtime else None
    with build_client(FakeSupervisor(), singing_runtime=runtime) as http:
        features = http.get("/v1/capabilities", headers=AUTH).json()["features"]
    assert features.get("singing_detection") is (True if with_runtime else None)
