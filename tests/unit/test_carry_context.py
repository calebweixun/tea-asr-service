import asyncio
from collections import deque
from itertools import pairwise
from typing import Any

from tea_asr.api.stream import ClosedSegment, StreamSession, strip_carried_overlap
from tea_asr.config import ServiceConfig


class ScriptedScheduler:
    def __init__(self, texts: list[str]) -> None:
        self.texts = deque(texts)
        self.audio: list[bytes] = []

    async def transcribe(self, pcm: bytes, **kwargs: Any) -> tuple[dict[str, Any], int]:
        self.audio.append(pcm)
        return {"text": self.texts.popleft(), "total_time_s": 0.01}, 0


class RecordingSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


def test_final_only_carry_keeps_each_stable_line_append_only() -> None:
    async def scenario() -> list[dict[str, Any]]:
        socket = RecordingSocket()
        scheduler = ScriptedScheduler(
            [
                "前段結尾重複短語。",
                "新內容",
                "新內容增加",
                "重複短語新內容增加完成。",
            ]
        )
        session = StreamSession(
            socket,  # type: ignore[arg-type]
            scheduler,  # type: ignore[arg-type]
            config=ServiceConfig(carry_context_s=1.0, carry_context_max_gap_s=1.5),
            model_state="ready",
        )
        session._transcript_mode = "revisable"
        session._stable_agreement = 2
        writer = asyncio.create_task(session.writer.run())
        first_pcm = b"\x01\x00" * 16_000
        second_pcm = b"\x02\x00" * 16_000
        session._remember_audio(0, first_pcm)
        first = session._open_segment(0)
        await session._transcribe_segment(
            ClosedSegment(first, first_pcm, 16_000, "manual")
        )
        await session.writer.drain(timeout=1.0)
        session._remember_audio(16_000, second_pcm)
        second = session._open_segment(16_000)
        await session._run_preview(second_pcm[:16_000], 24_000, second)
        await session.writer.drain(timeout=1.0)
        await session._run_preview(second_pcm, 32_000, second)
        await session.writer.drain(timeout=1.0)
        await session._transcribe_segment(
            ClosedSegment(second, second_pcm, 32_000, "manual")
        )
        await session.writer.drain(timeout=1.0)
        writer.cancel()
        return socket.sent

    events = asyncio.run(scenario())
    finals = [event for event in events if event["type"] == "transcript.final"]
    assert [event["text"] for event in finals] == [
        "前段結尾重複短語。",
        "新內容增加完成。",
    ]
    assert finals[1]["raw_text"] == "重複短語新內容增加完成。"
    assert finals[1]["warnings"] == ["carry_overlap_stripped"]

    stable_by_segment: dict[str, list[str]] = {}
    for event in events:
        if event["type"] == "transcript.stable":
            stable_by_segment.setdefault(event["segment_id"], []).append(event["text"])
    assert len(stable_by_segment) == 2
    for values in stable_by_segment.values():
        assert all(new.startswith(old) for old, new in pairwise(values))
    assert len(stable_by_segment[finals[1]["segment_id"]]) == 2


def test_strip_exact_previous_suffix_from_current_prefix() -> None:
    assert strip_carried_overlap("前段結尾重複短語。", "重複短語，新內容。") == (
        "新內容。",
        4,
    )


def test_strip_allows_about_one_edit_per_four_characters() -> None:
    assert strip_carried_overlap("之前共同片語重複短語", "共同片語重複短雨新句") == (
        "新句",
        8,
    )


def test_strip_ignores_punctuation_spaces_and_fullwidth_forms() -> None:
    assert strip_carried_overlap("早先 ABCD。", "Ａ，Ｂ　ＣＤ；新內容！") == ("新內容！", 4)


def test_no_confident_overlap_returns_none_for_safe_retry() -> None:
    assert strip_carried_overlap("上一段說明內容", "完全不同的句首") is None


def test_empty_previous_final_returns_none() -> None:
    assert strip_carried_overlap("", "目前這段文字") is None


def test_final_skips_carry_when_previous_final_gap_exceeds_configured_limit() -> None:
    async def scenario() -> tuple[list[bytes], list[dict[str, Any]]]:
        socket = RecordingSocket()
        scheduler = ScriptedScheduler(["獨立新段結果"])
        session = StreamSession(
            socket,  # type: ignore[arg-type]
            scheduler,  # type: ignore[arg-type]
            config=ServiceConfig(carry_context_s=1.0, carry_context_max_gap_s=0.5),
            model_state="ready",
        )
        session._previous_final = (0, "上一段已定稿文字")
        segment = session._open_segment(16_000)
        segment.carry_pcm = b"\x01\x00" * 16_000
        pcm = b"\x02\x00" * 16_000
        writer = asyncio.create_task(session.writer.run())
        await session._transcribe_segment(ClosedSegment(segment, pcm, 32_000, "manual"))
        await session.writer.drain(timeout=1.0)
        writer.cancel()
        return scheduler.audio, socket.sent

    audio, events = asyncio.run(scenario())
    final = next(event for event in events if event["type"] == "transcript.final")
    assert audio == [b"\x02\x00" * 16_000]
    assert final["text"] == "獨立新段結果"
    assert final["warnings"] == []
