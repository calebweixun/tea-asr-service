from __future__ import annotations

import io

import pytest

from tea_asr.worker.protocol import MAX_PCM_BYTES, encode_message, read_request


def test_round_trip() -> None:
    payload = {"ipc_version": 1, "request_id": "r1", "pcm_bytes": 4, "sample_rate": 16_000}
    stream = io.BytesIO(encode_message(payload) + b"\x00\x01\x02\x03")
    header, pcm = read_request(stream)
    assert header["request_id"] == "r1"
    assert pcm == b"\x00\x01\x02\x03"


@pytest.mark.parametrize("pcm_bytes", [MAX_PCM_BYTES + 2, 3])
def test_rejects_invalid_pcm_length(pcm_bytes: int) -> None:
    stream = io.BytesIO(encode_message({"pcm_bytes": pcm_bytes}))
    with pytest.raises(ValueError, match="PCM length"):
        read_request(stream)
