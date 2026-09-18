from __future__ import annotations

import pytest
from pydantic import ValidationError

from tea_asr.wire import ClientEnvelope, SessionStart, ws_event_schema


def _parse(payload: dict[str, object]) -> object:
    return ClientEnvelope.model_validate({"event": payload}).event


def test_session_start_defaults_to_final_only() -> None:
    start = _parse(
        {
            "type": "session.start",
            "request_id": "s1",
            "profile": "utterance",
            "audio": {"sample_rate": 16_000, "channels": 1, "format": "pcm_s16le"},
        }
    )
    assert isinstance(start, SessionStart)
    assert start.transcript_mode == "final_only"
    assert start.durable is False


def test_unknown_client_field_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _parse(
            {
                "type": "session.start",
                "request_id": "s1",
                "profile": "utterance",
                "audio": {"sample_rate": 16_000, "channels": 1, "format": "pcm_s16le"},
                "hotwords": ["TEA-ASR"],
            }
        )


def test_unsupported_audio_format_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _parse(
            {
                "type": "session.start",
                "request_id": "s1",
                "profile": "utterance",
                "audio": {"sample_rate": 48_000, "channels": 1, "format": "pcm_s16le"},
            }
        )


def test_schema_covers_every_wire_event() -> None:
    models = ws_event_schema()["$defs"]["models"]
    for name in ("AudioAck", "SegmentSkipped", "SegmentError", "TranscriptPartial", "Pong"):
        assert name in models
