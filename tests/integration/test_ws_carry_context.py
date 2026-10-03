from __future__ import annotations

import struct
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AUTH, FakeSupervisor, build_client
from tests.integration.test_ws_contract import START, drain_until


class ScriptedCarrySupervisor(FakeSupervisor):
    def __init__(self, texts: list[str]) -> None:
        super().__init__()
        self.texts = texts
        self.audio: list[bytes] = []

    async def transcribe(
        self,
        pcm: bytes,
        *,
        language: str = "Chinese",
        system_prompt: str | None = None,
    ) -> dict[str, Any]:
        result = await super().transcribe(
            pcm, language=language, system_prompt=system_prompt
        )
        self.audio.append(pcm)
        result["text"] = self.texts.pop(0)
        return result


def _pcm_frames(socket: Any, *, first_seq: int, count: int, value: int) -> int:
    sample = struct.pack("<h", value)
    for offset in range(count):
        seq = first_seq + offset
        socket.send_bytes(struct.pack("<QQ", seq, seq * 1600) + sample * 1600)
    return first_seq + count


def _two_finals(
    client: TestClient,
) -> list[dict[str, Any]]:
    with client as http, http.websocket_connect("/v1/stream", headers=AUTH) as socket:
        socket.receive_json()
        socket.send_json(START)
        socket.receive_json()
        next_seq = _pcm_frames(socket, first_seq=0, count=10, value=1000)
        socket.send_json(
            {"type": "audio.commit", "request_id": "first", "through_seq": next_seq - 1}
        )
        first_events = drain_until(socket, "transcript.final")
        next_seq = _pcm_frames(socket, first_seq=next_seq, count=10, value=2000)
        socket.send_json(
            {"type": "audio.commit", "request_id": "second", "through_seq": next_seq - 1}
        )
        second_events = drain_until(socket, "transcript.final")
    return [
        next(event for event in first_events if event["type"] == "transcript.final"),
        next(event for event in second_events if event["type"] == "transcript.final"),
    ]


def test_final_decodes_with_carry_strips_overlap_and_preserves_raw_text() -> None:
    supervisor = ScriptedCarrySupervisor(
        ["上一段文字重複短語。", "重複短語，本段新增。"]
    )
    client = build_client(
        supervisor, carry_context_s=1.0, carry_context_max_gap_s=1.5
    )

    first, second = _two_finals(client)

    assert [len(audio) // 2 for audio in supervisor.audio] == [16_000, 32_000]
    assert supervisor.audio[1][: len(supervisor.audio[0])] == supervisor.audio[0]
    assert supervisor.audio[1][len(supervisor.audio[0]) :] == b"\xd0\x07" * 16_000
    assert second["text"] == "本段新增。"
    assert second["raw_text"] == "重複短語，本段新增。"
    assert second["audio_ms"] == 1000
    assert second["warnings"] == ["carry_overlap_stripped"]
    assert first["text"] == "上一段文字重複短語。"


def test_uncertain_overlap_retries_segment_without_carry() -> None:
    supervisor = ScriptedCarrySupervisor(
        ["上一段文字結尾", "完全不同的句首", "本段單獨結果"]
    )
    client = build_client(
        supervisor, carry_context_s=1.0, carry_context_max_gap_s=1.5
    )

    _, second = _two_finals(client)

    assert [len(audio) // 2 for audio in supervisor.audio] == [16_000, 32_000, 16_000]
    assert second["text"] == "本段單獨結果"
    assert second["raw_text"] == "本段單獨結果"
    assert second["warnings"] == ["carry_overlap_uncertain"]
