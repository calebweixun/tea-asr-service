from __future__ import annotations

import struct
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tea_asr.worker.supervisor import WorkerError
from tests.conftest import AUTH, FakeSupervisor, FakeVad, build_client

START = {
    "type": "session.start",
    "request_id": "start-1",
    "profile": "utterance",
    "audio": {"sample_rate": 16_000, "channels": 1, "format": "pcm_s16le"},
    "language": "Chinese",
    "durable": False,
}


#: docs/04-api.md caps one binary frame at 6,400 PCM bytes (200 ms).
FRAME_SAMPLES = 1600


def frame(seq: int, start_sample: int, samples: int = FRAME_SAMPLES) -> bytes:
    return struct.pack("<QQ", seq, start_sample) + b"\0\0" * samples


def send_audio(socket: Any, frames: int, *, first_seq: int = 0) -> int:
    """Send `frames` contiguous frames and return the next free seq."""

    for offset in range(frames):
        seq = first_seq + offset
        socket.send_bytes(frame(seq, seq * FRAME_SAMPLES))
    return first_seq + frames


def drain_until(socket: Any, event_type: str) -> list[dict[str, Any]]:
    events = []
    while True:
        event = socket.receive_json()
        events.append(event)
        if event["type"] == event_type:
            return events


def test_hello_reports_the_real_model_state() -> None:
    with build_client(FakeSupervisor(state="loading")) as http, http.websocket_connect(
        "/v1/stream", headers=AUTH
    ) as socket:
        hello = socket.receive_json()
        assert hello == {"type": "hello", "protocol_version": "1.0", "model_state": "loading"}
        socket.send_json(START)
        error = socket.receive_json()
        assert error["type"] == "error"
        assert error["code"] == "model_loading"


def test_unauthenticated_upgrade_is_closed(client: TestClient) -> None:
    with (
        client as http,
        pytest.raises(WebSocketDisconnect) as caught,
        http.websocket_connect("/v1/stream") as socket,
    ):
        socket.receive_json()
    assert caught.value.code == 1008


@pytest.mark.parametrize(
    "override",
    [
        {"profile": "continuous"},
        {"durable": True},
        {"transcript_mode": "revisable"},
    ],
)
def test_unverified_options_are_refused(client: TestClient, override: dict[str, Any]) -> None:
    with client as http, http.websocket_connect("/v1/stream", headers=AUTH) as socket:
        socket.receive_json()
        socket.send_json({**START, **override})
        error = socket.receive_json()
        assert error["type"] == "error"
        assert error["code"] == "unsupported_option"
        assert error["retryable"] is False


def test_unknown_client_field_is_refused(client: TestClient) -> None:
    with client as http, http.websocket_connect("/v1/stream", headers=AUTH) as socket:
        socket.receive_json()
        socket.send_json({**START, "hotwords": ["TEA-ASR"]})
        error = socket.receive_json()
        assert error["type"] == "error"
        assert error["code"] == "protocol_error"


def test_binary_before_session_start_is_refused(client: TestClient) -> None:
    with client as http, http.websocket_connect("/v1/stream", headers=AUTH) as socket:
        socket.receive_json()
        socket.send_bytes(frame(0, 0))
        error = socket.receive_json()
        assert error["code"] == "protocol_error"


def test_frames_are_acknowledged_and_flow_window_only_grows(client: TestClient) -> None:
    with client as http, http.websocket_connect("/v1/stream", headers=AUTH) as socket:
        socket.receive_json()
        socket.send_json(START)
        started = socket.receive_json()
        assert started["type"] == "session.started"
        assert started["event_id"] == 0
        assert started["send_until_sample"] == 80_000
        assert started["preview_policy"] is None

        window = started["send_until_sample"]
        for seq in range(30):
            socket.send_bytes(frame(seq, seq * 1600))
        acks = 0
        while acks < 30:
            event = socket.receive_json()
            if event["type"] == "audio.ack":
                acks += 1
                assert event["persisted_seq"] is None
            elif event["type"] == "flow.control":
                assert event["send_until_sample"] >= window
                window = event["send_until_sample"]
            if acks and event["type"] == "audio.ack" and event["received_seq"] == 29:
                break
        assert window > started["send_until_sample"]


@pytest.mark.parametrize(
    ("bad_frame", "code"),
    [
        (struct.pack("<QQ", 1, 0) + b"\0\0", "protocol_error"),
        (struct.pack("<QQ", 0, 16) + b"\0\0", "protocol_error"),
        (struct.pack("<QQ", 0, 0) + b"\0", "protocol_error"),
        (struct.pack("<QQ", 0, 0) + b"\0\0" * 3300, "protocol_error"),
        (struct.pack("<QQ", 0, 0), "protocol_error"),
    ],
)
def test_malformed_frames_are_protocol_errors(
    client: TestClient, bad_frame: bytes, code: str
) -> None:
    with client as http, http.websocket_connect("/v1/stream", headers=AUTH) as socket:
        socket.receive_json()
        socket.send_json(START)
        socket.receive_json()
        socket.send_bytes(bad_frame)
        events = drain_until(socket, "error")
        assert events[-1]["code"] == code


def test_utterance_longer_than_thirty_seconds_is_refused(client: TestClient) -> None:
    with client as http, http.websocket_connect("/v1/stream", headers=AUTH) as socket:
        socket.receive_json()
        socket.send_json(START)
        socket.receive_json()
        seq = 0
        sample = 0
        with pytest.raises(WebSocketDisconnect) as caught:
            while True:
                socket.send_bytes(frame(seq, sample, samples=3200))
                seq += 1
                sample += 3200
                while True:
                    event = socket.receive_json()
                    if event["type"] == "error":
                        assert event["code"] == "payload_too_large"
                        socket.receive_json()
                    if event["type"] in {"audio.ack", "flow.control"}:
                        break
        assert caught.value.code == 1009


def test_commit_produces_queued_then_immutable_final(client: TestClient) -> None:
    with client as http, http.websocket_connect("/v1/stream", headers=AUTH) as socket:
        socket.receive_json()
        socket.send_json(START)
        socket.receive_json()
        send_audio(socket, 10)
        socket.send_json({"type": "audio.commit", "request_id": "c1", "through_seq": 9})
        events = drain_until(socket, "transcript.final")
        by_type = {event["type"]: event for event in events}
        assert by_type["audio.committed"]["reason"] == "committed"
        assert by_type["segment.queued"]["boundary"] == "manual"
        final = by_type["transcript.final"]
        assert final["segment_index"] == 0
        assert final["revision"] == 1
        assert final["start_sample"] == 0
        assert final["end_sample"] == 16_000
        assert final["audio_ms"] == 1000
        assert final["queue_ms"] >= 0
        assert final["timestamp_quality"] == "segment"
        ids = [event["event_id"] for event in events]
        assert ids == sorted(ids)


def test_silent_segment_is_skipped_not_invented(supervisor: FakeSupervisor) -> None:
    supervisor.text = ""
    with build_client(supervisor) as http, http.websocket_connect(
        "/v1/stream", headers=AUTH
    ) as socket:
        socket.receive_json()
        socket.send_json(START)
        socket.receive_json()
        send_audio(socket, 10)
        socket.send_json({"type": "audio.commit", "request_id": "c1", "through_seq": 9})
        events = drain_until(socket, "segment.skipped")
        assert events[-1]["reason"] == "no_speech"
        assert not any(event["type"] == "transcript.final" for event in events)


def test_inference_failure_is_segment_scoped(supervisor: FakeSupervisor) -> None:
    supervisor.failure = WorkerError("inference_failed", "worker exploded")
    with build_client(supervisor) as http, http.websocket_connect(
        "/v1/stream", headers=AUTH
    ) as socket:
        socket.receive_json()
        socket.send_json(START)
        socket.receive_json()
        send_audio(socket, 10)
        socket.send_json({"type": "audio.commit", "request_id": "c1", "through_seq": 9})
        events = drain_until(socket, "segment.error")
        assert events[-1]["code"] == "inference_failed"
        # The session survives a failed segment.
        socket.send_json({"type": "ping", "request_id": "p1"})
        assert drain_until(socket, "pong")[-1]["request_id"] == "p1"
        socket.send_json({"type": "session.stop", "request_id": "s1", "through_seq": 9})
        stopped = drain_until(socket, "session.stopped")[-1]
        assert stopped["status"] == "completed_with_errors"
        assert stopped["failed_segments"] == [0]


def test_stop_without_audio_reports_no_audio(client: TestClient) -> None:
    with client as http, http.websocket_connect("/v1/stream", headers=AUTH) as socket:
        socket.receive_json()
        socket.send_json(START)
        socket.receive_json()
        socket.send_json({"type": "session.stop", "request_id": "s1", "through_seq": None})
        events = drain_until(socket, "session.stopped")
        committed = next(e for e in events if e["type"] == "audio.committed")
        assert committed["reason"] == "no_audio"
        assert committed["segment_id"] is None
        assert events[-1]["last_seq"] is None


def test_stop_barrier_must_match_received_audio(client: TestClient) -> None:
    with client as http, http.websocket_connect("/v1/stream", headers=AUTH) as socket:
        socket.receive_json()
        socket.send_json(START)
        socket.receive_json()
        socket.send_bytes(frame(0, 0))
        socket.send_json({"type": "session.stop", "request_id": "s1", "through_seq": 5})
        assert drain_until(socket, "error")[-1]["code"] == "protocol_error"


def test_cancel_prevents_any_further_final(client: TestClient) -> None:
    with client as http, http.websocket_connect("/v1/stream", headers=AUTH) as socket:
        socket.receive_json()
        socket.send_json(START)
        socket.receive_json()
        send_audio(socket, 10)
        socket.send_json({"type": "session.cancel", "request_id": "x1"})
        events = drain_until(socket, "session.cancelled")
        assert not any(event["type"] == "transcript.final" for event in events)


def test_repeated_control_id_is_not_executed_twice(client: TestClient) -> None:
    with client as http, http.websocket_connect("/v1/stream", headers=AUTH) as socket:
        socket.receive_json()
        socket.send_json(START)
        socket.receive_json()
        socket.send_json({"type": "ping", "request_id": "p1"})
        assert drain_until(socket, "pong")[-1]["request_id"] == "p1"
        socket.send_json({"type": "ping", "request_id": "p1"})
        socket.send_json({"type": "ping", "request_id": "p2"})
        # Only the new request_id produces a pong.
        assert drain_until(socket, "pong")[-1]["request_id"] == "p2"


def test_reusing_a_request_id_for_a_different_control_is_a_conflict(client: TestClient) -> None:
    with client as http, http.websocket_connect("/v1/stream", headers=AUTH) as socket:
        socket.receive_json()
        socket.send_json(START)
        socket.receive_json()
        socket.send_json({"type": "ping", "request_id": "dup"})
        drain_until(socket, "pong")
        socket.send_bytes(frame(0, 0))
        socket.send_json({"type": "audio.commit", "request_id": "dup", "through_seq": 0})
        error = drain_until(socket, "error")[-1]
        assert error["code"] == "conflict"
        # The connection stays usable after a control-level conflict.
        socket.send_json({"type": "ping", "request_id": "after"})
        assert drain_until(socket, "pong")[-1]["request_id"] == "after"


def test_preview_replaces_text_and_final_wins(preview_client: TestClient) -> None:
    with preview_client as http, http.websocket_connect("/v1/stream", headers=AUTH) as socket:
        hello = socket.receive_json()
        assert hello["protocol_version"] == "1.1"
        socket.send_json({**START, "transcript_mode": "revisable"})
        started = socket.receive_json()
        assert started["transcript_mode"] == "revisable"
        assert started["preview_policy"]["min_audio_ms"] == 800
        assert started["preview_policy"]["context_biasing"] is False

        for seq in range(10):
            socket.send_bytes(frame(seq, seq * 1600))
        partial = drain_until(socket, "transcript.partial")[-1]
        assert partial["revision"] >= 1
        assert partial["segment_index"] == 0

        socket.send_json({"type": "session.stop", "request_id": "s1", "through_seq": 9})
        final = drain_until(socket, "transcript.final")[-1]
        assert final["segment_id"] == partial["segment_id"]
        assert final["revision"] > partial["revision"]
        assert final["end_sample"] == 16_000


# --- continuous profile -----------------------------------------------------

CONTINUOUS = {**START, "profile": "continuous"}


def tone_frame(seq: int, start_sample: int) -> bytes:
    pcm = np.full(FRAME_SAMPLES, 8000, dtype="<i2").tobytes()
    return struct.pack("<QQ", seq, start_sample) + pcm


def send_continuous(socket: Any, pattern: list[tuple[str, int]], first_seq: int = 0) -> int:
    """Send `(kind, frames)` pairs where kind is "tone" or "silence"."""

    seq = first_seq
    for kind, count in pattern:
        for _ in range(count):
            builder = tone_frame if kind == "tone" else frame
            socket.send_bytes(builder(seq, seq * FRAME_SAMPLES))
            seq += 1
    return seq


def test_continuous_is_refused_without_a_vad_asset(client: TestClient) -> None:
    with client as http, http.websocket_connect("/v1/stream", headers=AUTH) as socket:
        socket.receive_json()
        socket.send_json(CONTINUOUS)
        error = socket.receive_json()
        assert error["code"] == "unsupported_option"
        assert "VAD" in error["message"]


def test_capabilities_advertise_continuous_only_with_a_vad(
    continuous_client: TestClient,
) -> None:
    with continuous_client as http:
        body = http.get("/v1/capabilities", headers=AUTH).json()
        assert body["profiles"] == ["utterance", "continuous"]
        assert body["limits"]["max_continuous_sessions"] == 1


def test_vad_closes_a_segment_on_silence_without_any_client_commit(
    continuous_client: TestClient,
) -> None:
    with continuous_client as http, http.websocket_connect(
        "/v1/stream", headers=AUTH
    ) as socket:
        socket.receive_json()
        socket.send_json(CONTINUOUS)
        started = socket.receive_json()
        assert started["profile"] == "continuous"

        # 1 s of speech then 1 s of silence: the server must segment by itself.
        send_continuous(socket, [("silence", 3), ("tone", 10), ("silence", 12)])
        events = drain_until(socket, "transcript.final")
        by_type = {event["type"]: event for event in events}
        assert by_type["speech.started"]["segment_index"] == 0
        assert by_type["segment.queued"]["boundary"] == "silence"
        final = by_type["transcript.final"]
        assert final["segment_index"] == 0
        assert final["segment_id"] == by_type["speech.started"]["segment_id"]
        assert final["end_sample"] > final["start_sample"]
        # No audio.committed: the client never asked for this segment.
        assert "audio.committed" not in by_type


def test_continuous_produces_ordered_segments_across_pauses(
    continuous_client: TestClient,
) -> None:
    with continuous_client as http, http.websocket_connect(
        "/v1/stream", headers=AUTH
    ) as socket:
        socket.receive_json()
        socket.send_json(CONTINUOUS)
        socket.receive_json()
        seq = send_continuous(
            socket,
            [("silence", 3), ("tone", 8), ("silence", 12), ("tone", 8), ("silence", 12)],
        )
        socket.send_json(
            {"type": "session.stop", "request_id": "s1", "through_seq": seq - 1}
        )
        events = drain_until(socket, "session.stopped")
        finals = [event for event in events if event["type"] == "transcript.final"]
        assert len(finals) == 2
        assert [final["segment_index"] for final in finals] == [0, 1]
        assert finals[0]["end_sample"] <= finals[1]["start_sample"]
        assert events[-1]["status"] == "completed"


def test_continuous_rejects_audio_commit(continuous_client: TestClient) -> None:
    with continuous_client as http, http.websocket_connect(
        "/v1/stream", headers=AUTH
    ) as socket:
        socket.receive_json()
        socket.send_json(CONTINUOUS)
        socket.receive_json()
        seq = send_continuous(socket, [("tone", 4)])
        socket.send_json(
            {"type": "audio.commit", "request_id": "c1", "through_seq": seq - 1}
        )
        error = drain_until(socket, "error")[-1]
        assert error["code"] == "unsupported_option"


def test_continuous_keeps_receiving_audio_while_inference_runs(
    supervisor: FakeSupervisor,
) -> None:
    supervisor.delay_s = 0.2
    with build_client(supervisor, vad=FakeVad()) as http, http.websocket_connect(
        "/v1/stream", headers=AUTH
    ) as socket:
        socket.receive_json()
        socket.send_json(CONTINUOUS)
        socket.receive_json()
        seq = send_continuous(socket, [("silence", 2), ("tone", 8), ("silence", 12)])
        # While the first segment is still being transcribed, more audio is
        # accepted and acknowledged: the receive loop is not blocked.
        seq = send_continuous(socket, [("tone", 8), ("silence", 12)], first_seq=seq)
        socket.send_json(
            {"type": "session.stop", "request_id": "s1", "through_seq": seq - 1}
        )
        events = drain_until(socket, "session.stopped")
        acks = [event for event in events if event["type"] == "audio.ack"]
        finals = [event for event in events if event["type"] == "transcript.final"]
        assert len(finals) == 2
        assert acks, "audio was acknowledged while the model was busy"
        assert events[-1]["status"] == "completed"


def test_cancel_during_preview_produces_no_later_text(supervisor: FakeSupervisor) -> None:
    """docs/07: after session.cancelled no partial or final may appear."""

    supervisor.delay_s = 0.15
    with build_client(supervisor, revisable_preview=True, vad=FakeVad()) as http, (
        http.websocket_connect("/v1/stream", headers=AUTH)
    ) as socket:
        socket.receive_json()
        socket.send_json({**CONTINUOUS, "transcript_mode": "revisable"})
        started = socket.receive_json()
        assert started["transcript_mode"] == "revisable"

        # Speak long enough that a preview is in flight, then cancel mid-segment.
        send_continuous(socket, [("silence", 2), ("tone", 16)])
        socket.send_json({"type": "session.cancel", "request_id": "x1"})

        events = drain_until(socket, "session.cancelled")
        cancelled_at = next(
            index for index, event in enumerate(events) if event["type"] == "session.cancelled"
        )
        after = events[cancelled_at + 1 :]
        assert not [
            event for event in after if event["type"] in {"transcript.partial", "transcript.final"}
        ], "cancel 之後不得再出現任何文字"


def test_preview_revisions_are_strictly_increasing(supervisor: FakeSupervisor) -> None:
    supervisor.text = "測試"
    with build_client(supervisor, revisable_preview=True, vad=FakeVad()) as http, (
        http.websocket_connect("/v1/stream", headers=AUTH)
    ) as socket:
        socket.receive_json()
        socket.send_json({**CONTINUOUS, "transcript_mode": "revisable"})
        socket.receive_json()
        seq = send_continuous(socket, [("silence", 2), ("tone", 20), ("silence", 12)])
        socket.send_json({"type": "session.stop", "request_id": "s1", "through_seq": seq - 1})
        events = drain_until(socket, "session.stopped")

        by_segment: dict[str, list[int]] = {}
        for event in events:
            if event["type"] in {"transcript.partial", "transcript.final"}:
                by_segment.setdefault(event["segment_id"], []).append(event["revision"])
        assert by_segment, "應該至少有一個片段"
        for revisions in by_segment.values():
            assert revisions == sorted(revisions)
            assert len(set(revisions)) == len(revisions)


def test_an_unexpected_segment_failure_does_not_wedge_the_session(
    supervisor: FakeSupervisor,
) -> None:
    """A bad segment must not take the consumer down with it."""

    class Exploding(FakeSupervisor):
        async def transcribe(self, pcm: bytes, *, language: str = "Chinese") -> dict:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("something nobody predicted")
            return await FakeSupervisor.transcribe(self, pcm, language=language)

    exploding = Exploding()
    with build_client(exploding, vad=FakeVad()) as http, (
        http.websocket_connect("/v1/stream", headers=AUTH)
    ) as socket:
        socket.receive_json()
        socket.send_json(CONTINUOUS)
        socket.receive_json()
        seq = send_continuous(
            socket,
            [("silence", 3), ("tone", 8), ("silence", 12), ("tone", 8), ("silence", 12)],
        )
        socket.send_json({"type": "session.stop", "request_id": "s1", "through_seq": seq - 1})
        events = drain_until(socket, "session.stopped")

        failures = [event for event in events if event["type"] == "segment.error"]
        finals = [event for event in events if event["type"] == "transcript.final"]
        assert failures, "the broken segment should be reported"
        assert failures[0]["code"] == "internal_error"
        assert finals, "the session must keep working after a bad segment"
        assert events[-1]["status"] == "completed_with_errors"
