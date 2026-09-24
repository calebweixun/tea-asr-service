"""With translation off (the default), the wire is exactly what it was before.

These goldens were captured from the pre-translation code (d3bd900) and run
unchanged against it; they only use APIs that existed then, so a regression in
the default path shows up as a diff here rather than as a vague behaviour change.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.integration.test_ws_contract import START, drain_until, send_audio

GOLDEN_CAPABILITIES = {
    "protocol_version": "1.0",
    "audio": {"sample_rate": 16000, "channels": 1, "format": "pcm_s16le"},
    "profiles": ["utterance"],
    "features": {
        "native_audio_streaming": False,
        "partial_transcripts": False,
        "word_timestamps": False,
        "translation": False,
        "diarization": False,
        "hotwords": False,
        "context_biasing": False,
        "durable_sessions": False,
        "durable_revisable": False,
        "batch_jobs": False,
    },
    "limits": {
        "max_frame_pcm_bytes": 6400,
        "max_utterance_ms": 30000,
        "max_continuous_sessions": 0,
        "max_total_connections": 4,
    },
}

#: Every non-transport event of a two-commit utterance session, in order.
#: IDs and timings are replaced by placeholders; ACK/flow events are left out
#: because the writer legitimately merges them depending on timing.
GOLDEN_DIALOGUE = [
    {"type": "session.started", "event_id": 0, "request_id": "start-1", "profile": "utterance",
     "transcript_mode": "final_only", "next_seq": 0, "next_sample": 0,
     "send_until_sample": 80000, "preview_policy": None},
    {"type": "audio.committed", "request_id": "c1", "segment_id": "<segment-0>",
     "reason": "committed"},
    {"type": "segment.queued", "segment_id": "<segment-0>", "segment_index": 0,
     "start_sample": 0, "end_sample": 16000, "boundary": "manual"},
    {"type": "transcript.final", "segment_id": "<segment-0>", "segment_index": 0,
     "revision": 1, "start_sample": 0, "end_sample": 16000, "timestamp_quality": "segment",
     "text": "測試文字", "raw_text": "測試文字", "audio_ms": 1000, "queue_ms": "<ms>",
     "inference_ms": "<ms>", "warnings": []},
    {"type": "audio.committed", "request_id": "c2", "segment_id": "<segment-1>",
     "reason": "committed"},
    {"type": "segment.queued", "segment_id": "<segment-1>", "segment_index": 1,
     "start_sample": 16000, "end_sample": 32000, "boundary": "manual"},
    {"type": "transcript.final", "segment_id": "<segment-1>", "segment_index": 1,
     "revision": 1, "start_sample": 16000, "end_sample": 32000,
     "timestamp_quality": "segment", "text": "測試文字", "raw_text": "測試文字",
     "audio_ms": 1000, "queue_ms": "<ms>", "inference_ms": "<ms>", "warnings": []},
    {"type": "audio.committed", "request_id": "stop-1", "segment_id": None,
     "reason": "no_audio"},
    {"type": "session.stopped", "request_id": "stop-1", "last_seq": 19,
     "status": "completed", "failed_segments": []},
]


def normalize(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    segments: dict[str, str] = {}
    session_ids = {event["session_id"] for event in events}
    assert len(session_ids) == 1
    out = []
    for event in events:
        if event["type"] in {"audio.ack", "flow.control"}:
            continue
        item = {k: v for k, v in event.items() if k != "session_id"}
        if item["type"] != "session.started":
            item.pop("event_id")
        if item.get("segment_id"):
            item["segment_id"] = segments.setdefault(
                item["segment_id"], f"<segment-{len(segments)}>"
            )
        for key in ("queue_ms", "inference_ms"):
            if key in item:
                item[key] = "<ms>"
        out.append(item)
    return out


def test_capabilities_are_unchanged_without_translation(client: TestClient) -> None:
    with client as http:
        body = http.get("/v1/capabilities", headers=AUTH).json()
    assert body == GOLDEN_CAPABILITIES
    assert "translation" not in body


def test_utterance_dialogue_is_unchanged_without_translation(client: TestClient) -> None:
    with client as http, http.websocket_connect("/v1/stream", headers=AUTH) as socket:
        assert socket.receive_json() == {
            "type": "hello",
            "protocol_version": "1.0",
            "model_state": "ready",
        }
        socket.send_json(START)
        events = [socket.receive_json()]
        send_audio(socket, 10)
        socket.send_json({"type": "audio.commit", "request_id": "c1", "through_seq": 9})
        events += drain_until(socket, "transcript.final")
        send_audio(socket, 10, first_seq=10)
        socket.send_json({"type": "audio.commit", "request_id": "c2", "through_seq": 19})
        events += drain_until(socket, "transcript.final")
        socket.send_json({"type": "session.stop", "request_id": "stop-1", "through_seq": 19})
        events += drain_until(socket, "session.stopped")
    assert normalize(events) == GOLDEN_DIALOGUE
