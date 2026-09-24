"""`transcript.stable` inside one session: per-segment isolation and coalescing."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from tea_asr.api.events import EventWriter
from tea_asr.api.stream import ClosedSegment, StreamSession
from tea_asr.config import ServiceConfig
from tea_asr.wire import TranscriptStable


class ScriptedScheduler:
    """Returns the queued texts in order, one per transcribe call."""

    def __init__(self, texts: list[str]) -> None:
        self.texts = deque(texts)

    async def transcribe(
        self, pcm: bytes, *, language: str = "Chinese", kind: str = "interactive"
    ) -> tuple[dict[str, Any], int]:
        return {"text": self.texts.popleft(), "total_time_s": 0.01}, 0


class RecordingSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


def session_with(texts: list[str]) -> StreamSession:
    session = StreamSession(
        RecordingSocket(),  # type: ignore[arg-type]
        ScriptedScheduler(texts),
        config=ServiceConfig(),
        model_state="ready",
    )
    session._transcript_mode = "revisable"
    session._stable_agreement = 2
    return session


def run_steps(session: StreamSession, *steps: Any) -> list[dict[str, Any]]:
    """Run each step with the real writer draining in between, like a live socket."""

    async def scenario() -> None:
        writer = asyncio.create_task(session.writer.run())
        for step in steps:
            await step()
            await session.writer.drain(timeout=1.0)
        writer.cancel()

    asyncio.run(scenario())
    return session._websocket.sent  # type: ignore[attr-defined]


def test_next_segment_partials_before_previous_final_stay_separate() -> None:
    """The OBS audit bug: segment 1's partial arriving before segment 0's final
    must not touch segment 0's line."""

    session = session_with(
        [
            "第一段的內容",  # seg 0 preview 1
            "第一段的內容很長",  # seg 0 preview 2 -> commits 第一段的內容
            "第二段",  # seg 1 preview 1 (before seg 0's final)
            "第二段開始了",  # seg 1 preview 2 -> commits 第二段
            "第一段的內容很長。",  # seg 0 final
            "第二段開始了。",  # seg 1 final
        ]
    )
    pcm = b"\0\0" * 16_000
    first = session._open_segment(0)

    async def close_first_open_second() -> None:
        nonlocal second
        first.terminal = True
        second = session._open_segment(32_000)

    second = first

    async def close_second() -> None:
        second.terminal = True

    events = run_steps(
        session,
        lambda: session._run_preview(pcm, 12_800, first),
        lambda: session._run_preview(pcm, 25_600, first),
        close_first_open_second,
        lambda: session._run_preview(pcm, 44_800, second),
        lambda: session._run_preview(pcm, 57_600, second),
        lambda: session._transcribe_segment(ClosedSegment(first, pcm, 32_000, "silence")),
        close_second,
        lambda: session._transcribe_segment(ClosedSegment(second, pcm, 64_000, "stop")),
    )
    ids = {e["segment_index"]: e["segment_id"] for e in events if "segment_index" in e}
    stable = [event for event in events if event["type"] == "transcript.stable"]
    by_segment: dict[str, list[dict[str, Any]]] = {}
    for event in stable:
        by_segment.setdefault(event["segment_id"], []).append(event)

    zero, one = by_segment[ids[0]], by_segment[ids[1]]
    assert [e["text"] for e in zero] == ["第一段的內容", "第一段的內容很長。"]
    assert [e["state"] for e in zero] == ["open", "final"]
    assert [e["text"] for e in one] == ["第二段", "第二段開始了。"]
    assert [e["stable_revision"] for e in zero] == [1, 2]
    assert [e["stable_revision"] for e in one] == [1, 2]
    # Segment 1's commit really was sent before segment 0 closed.
    order = [(e["segment_id"], e["state"]) for e in stable]
    assert order.index((ids[1], "open")) < order.index((ids[0], "final"))
    # Each closing stable comes right after its own final.
    for segment_id in (ids[0], ids[1]):
        final_at = next(
            i
            for i, e in enumerate(events)
            if e["type"] == "transcript.final" and e["segment_id"] == segment_id
        )
        after = events[final_at + 1]
        assert after["type"] == "transcript.stable" and after["segment_id"] == segment_id
        assert after["state"] == "final"
        assert after["text"] == events[final_at]["text"]
        assert after["source_revision"] == events[final_at]["revision"]


def test_segment_without_final_closes_as_abandoned_only_if_something_was_shown() -> None:
    session = session_with(["測試一下", "測試一下喔", "", "只有一次", ""])
    pcm = b"\0\0" * 16_000

    shown = session._open_segment(0)
    unseen = shown

    async def close_shown() -> None:
        shown.terminal = True
        await session._transcribe_segment(ClosedSegment(shown, pcm, 32_000, "silence"))

    async def unseen_segment() -> None:
        nonlocal unseen
        unseen = session._open_segment(32_000)
        await session._run_preview(pcm, 44_800, unseen)
        unseen.terminal = True
        await session._transcribe_segment(ClosedSegment(unseen, pcm, 48_000, "silence"))

    events = run_steps(
        session,
        lambda: session._run_preview(pcm, 12_800, shown),
        lambda: session._run_preview(pcm, 25_600, shown),
        close_shown,
        unseen_segment,
    )
    skipped = [e for e in events if e["type"] == "segment.skipped"]
    stable = [e for e in events if e["type"] == "transcript.stable"]
    assert len(skipped) == 2
    assert [(e["state"], e["text"]) for e in stable] == [
        ("open", "測試一下"),
        ("abandoned", "測試一下"),
    ]
    assert all(e["segment_id"] == skipped[0]["segment_id"] for e in stable)


def _stable(segment_id: str, revision: int, text: str, state: str = "open") -> TranscriptStable:
    return TranscriptStable(
        session_id="s",
        event_id=0,
        segment_id=segment_id,
        segment_index=0,
        stable_revision=revision,
        source_revision=revision,
        start_sample=0,
        end_sample=revision * 1600,
        text=text,
        state=state,  # type: ignore[arg-type]
    )


def test_writer_coalesces_open_stable_per_segment_and_keeps_the_closing_one() -> None:
    writer = EventWriter(object(), "s")  # type: ignore[arg-type]
    writer.emit(_stable("a", 1, "一"))
    writer.emit(_stable("b", 1, "乙"))
    writer.emit(_stable("a", 2, "一二"))
    assert [(e["segment_id"], e["text"]) for e in writer._queue] == [("a", "一二"), ("b", "乙")]

    writer.emit(_stable("a", 3, "一二三。", state="final"))
    items = [(e["segment_id"], e["text"], e["state"]) for e in writer._queue]
    assert ("a", "一二", "open") not in items, "superseded by the closing event"
    assert ("b", "乙", "open") in items, "another segment is untouched"
    assert ("a", "一二三。", "final") in items
