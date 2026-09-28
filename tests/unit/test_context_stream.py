from __future__ import annotations

import asyncio
from collections import deque
from itertools import pairwise
from typing import Any

from tea_asr.api.stream import ClosedSegment, StreamSession
from tea_asr.config import ServiceConfig
from tea_asr.context import ContextPlan, ReplacementRule, build_system_prompt

_NOT_PASSED = object()


class CaptureScheduler:
    def __init__(self, texts: list[str]) -> None:
        self.texts = deque(texts)
        self.requests: list[dict[str, Any]] = []

    async def transcribe(
        self,
        pcm: bytes,
        *,
        language: str = "Chinese",
        kind: str = "interactive",
        is_stale: Any = None,
        system_prompt: Any = _NOT_PASSED,
    ) -> tuple[dict[str, Any], int]:
        request: dict[str, Any] = {"pcm": pcm, "language": language, "kind": kind}
        if system_prompt is not _NOT_PASSED:
            request["system_prompt"] = system_prompt
        self.requests.append(request)
        return {
            "text": self.texts.popleft(),
            "total_time_s": 0.01,
            "prompt_tokens": 23,
        }, 0


class RecordingSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


def _session(
    texts: list[str], *, context_prompt_enabled: bool = False
) -> tuple[StreamSession, CaptureScheduler, RecordingSocket]:
    scheduler = CaptureScheduler(texts)
    socket = RecordingSocket()
    session = StreamSession(
        socket,  # type: ignore[arg-type]
        scheduler,
        config=ServiceConfig(
            revisable_preview=True,
            context_hints_enabled=True,
            context_prompt_enabled=context_prompt_enabled,
        ),
        model_state="ready",
    )
    session._transcript_mode = "revisable"
    return session, scheduler, socket


def _run_steps(session: StreamSession, *steps: Any) -> list[dict[str, Any]]:
    async def scenario() -> None:
        writer = asyncio.create_task(session.writer.run())
        for step in steps:
            await step()
            await session.writer.drain(timeout=1.0)
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)

    asyncio.run(scenario())
    return session._websocket.sent  # type: ignore[attr-defined]


def test_context_is_frozen_when_segment_opens_and_reaches_preview_and_final() -> None:
    session, scheduler, _ = _session(
        ["第一段", "第一段定稿"], context_prompt_enabled=True
    )
    first_plan = ContextPlan(
        profile="church",
        domain="sermon one",
        hotwords=("聖經",),
        replacements=(),
        system_prompt=build_system_prompt("sermon one", ("聖經",)),
    )
    session._context_plan = first_plan
    segment = session._open_segment(0)
    session._context_plan = ContextPlan(
        profile="church",
        domain="sermon two",
        hotwords=("禱告",),
        replacements=(),
        system_prompt="new prompt that must not reach the open segment",
    )
    pcm = b"\0\0" * 16_000
    async def final_step() -> None:
        segment.terminal = True
        await session._transcribe_segment(ClosedSegment(segment, pcm, 32_000, "manual"))

    _run_steps(
        session,
        lambda: session._run_preview(pcm, 16_000, segment),
        final_step,
    )

    assert scheduler.requests[0]["system_prompt"] == first_plan.system_prompt
    assert scheduler.requests[1]["system_prompt"] == first_plan.system_prompt


def test_no_context_is_not_added_to_the_preview_request() -> None:
    session, scheduler, _ = _session(["文字"])
    segment = session._open_segment(0)

    asyncio.run(session._run_preview(b"\0\0" * 16_000, 16_000, segment))

    assert "system_prompt" not in scheduler.requests[0]


def test_replacements_apply_to_partial_and_final_and_preserve_raw_text() -> None:
    raw = "基督教講道內容與聖經教導中的盛家"
    plan = ContextPlan(
        profile="church",
        domain="基督教講道內容與聖經教導的詞彙範圍",
        hotwords=("聖經",),
        replacements=(ReplacementRule("盛家", "聖經"),),
        system_prompt="Use hints only.",
    )
    session, _, socket = _session([raw, raw])
    session._context_plan = plan
    segment = session._open_segment(0)
    pcm = b"\0\0" * 16_000

    async def final_step() -> None:
        segment.terminal = True
        await session._transcribe_segment(ClosedSegment(segment, pcm, 32_000, "manual"))

    events = _run_steps(
        session,
        lambda: session._run_preview(pcm, 16_000, segment),
        final_step,
    )

    partial = next(event for event in events if event.get("type") == "transcript.partial")
    final = next(event for event in events if event.get("type") == "transcript.final")
    assert partial["text"].endswith("聖經")
    assert "replacements_applied" in partial["warnings"]
    assert final["text"].endswith("聖經")
    assert final["raw_text"] == raw
    assert "replacements_applied" in final["warnings"]
    assert "context_echo" in final["warnings"]
    assert final["text"].startswith("基督教講道內容與聖經教導中")
    assert socket.sent


def test_prompt_off_matches_no_context_request_while_replacements_apply() -> None:
    raw = "聖家"
    plan = ContextPlan(
        profile="church",
        domain="基督教會主日講道與禱告，繁體中文。",
        hotwords=("聖經", "住棚節"),
        replacements=(ReplacementRule("聖家", "聖經"),),
        system_prompt="domain and hotwords must stay out of the model request",
    )
    context_session, context_scheduler, _ = _session([raw, raw])
    context_session._context_plan = plan
    context_segment = context_session._open_segment(0)

    plain_session, plain_scheduler, _ = _session([raw, raw])
    plain_segment = plain_session._open_segment(0)
    pcm = b"\0\0" * 16_000

    async def context_final_step() -> None:
        context_segment.terminal = True
        await context_session._transcribe_segment(
            ClosedSegment(context_segment, pcm, 32_000, "manual")
        )

    async def plain_final_step() -> None:
        plain_segment.terminal = True
        await plain_session._transcribe_segment(ClosedSegment(plain_segment, pcm, 32_000, "manual"))

    context_events = _run_steps(
        context_session,
        lambda: context_session._run_preview(pcm, 16_000, context_segment),
        context_final_step,
    )
    _run_steps(
        plain_session,
        lambda: plain_session._run_preview(pcm, 16_000, plain_segment),
        plain_final_step,
    )

    assert context_segment.system_prompt is None
    assert context_scheduler.requests == plain_scheduler.requests
    final = next(event for event in context_events if event.get("type") == "transcript.final")
    assert final["text"] == "聖經"
    assert final["raw_text"] == raw


def test_replacement_crossing_stable_boundary_never_retracts_committed_text() -> None:
    session, _, _ = _session(["盛", "盛", "盛家", "盛家"])
    session._stable_agreement = 2
    session._context_plan = ContextPlan(
        profile="church",
        domain=None,
        hotwords=(),
        replacements=(ReplacementRule("盛家", "聖經"),),
        system_prompt=None,
    )
    segment = session._open_segment(0)
    pcm = b"\0\0" * 16_000

    async def final_step() -> None:
        segment.terminal = True
        await session._transcribe_segment(ClosedSegment(segment, pcm, 64_000, "manual"))

    events = _run_steps(
        session,
        lambda: session._run_preview(pcm, 16_000, segment),
        lambda: session._run_preview(pcm, 32_000, segment),
        lambda: session._run_preview(pcm, 48_000, segment),
        final_step,
    )

    stable_texts = [
        event["text"] for event in events if event.get("type") == "transcript.stable"
    ]
    assert stable_texts[0] == "盛"
    assert all(later.startswith(earlier) for earlier, later in pairwise(stable_texts))
    assert stable_texts[-1].startswith(stable_texts[0])
    final = next(event for event in events if event.get("type") == "transcript.final")
    assert final["text"] == "聖經"
    assert final["raw_text"] == "盛家"
