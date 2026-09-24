from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tea_asr.errors import ApiError
from tea_asr.translation.session import SessionTranslator
from tea_asr.wire import ServerModel


class FakeProvider:
    def __init__(self) -> None:
        self.state = "ready"
        self.generation = 1
        self.task_timeout_s = 20.0
        self.calls: list[str] = []
        self.starts = 0
        self.delay_s = 0.0
        self.gate: asyncio.Event | None = None
        self.results: list[dict[str, Any] | ApiError] = []

    async def start_session(self, direction: str, latency_mode: str) -> int:
        self.starts += 1
        return self.generation

    async def translate(self, text: str, *, force: bool) -> dict[str, Any]:
        self.calls.append(text)
        if self.gate is not None:
            await self.gate.wait()
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        result = self.results.pop(0) if self.results else None
        if isinstance(result, ApiError):
            raise result
        if result is not None:
            return result
        return {"action": "TRANS", "source": text, "text": f"EN({text})", "forced": force}


def make(provider: FakeProvider, **kwargs: Any) -> tuple[SessionTranslator, list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []

    def emit(event: ServerModel) -> None:
        events.append(event.model_dump(mode="json"))

    translator = SessionTranslator(
        provider, emit, session_id="s", direction="zh2en", latency_mode="native", **kwargs
    )
    return translator, events


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


def test_each_final_becomes_one_append_only_translation() -> None:
    async def scenario() -> list[dict[str, Any]]:
        provider = FakeProvider()
        translator, events = make(provider)
        translator.start()
        translator.submit("seg-0", "你好")
        await settle()
        translator.submit("seg-1", "世界")
        await translator.drain()
        await translator.close()
        return events

    events = asyncio.run(scenario())
    assert [e["type"] for e in events] == ["translation.segment", "translation.segment"]
    assert [e["translation_index"] for e in events] == [0, 1]
    assert events[0]["source_segment_ids"] == ["seg-0"]
    assert events[0]["source_text"] == "你好"
    assert events[1]["text"] == "EN(世界)"


def test_backlog_is_coalesced_into_one_call() -> None:
    async def scenario() -> tuple[FakeProvider, list[dict[str, Any]]]:
        provider = FakeProvider()
        provider.gate = asyncio.Event()
        translator, events = make(provider)
        translator.start()
        translator.submit("a", "一")
        await settle()
        translator.submit("b", "二")
        translator.submit("c", "三")
        provider.gate.set()
        await translator.drain()
        await translator.close()
        return provider, events

    provider, events = asyncio.run(scenario())
    assert provider.calls == ["一", "二三"]
    assert events[1]["source_segment_ids"] == ["b", "c"]


def test_full_queue_reports_the_dropped_final_instead_of_blocking() -> None:
    async def scenario() -> list[dict[str, Any]]:
        provider = FakeProvider()
        provider.gate = asyncio.Event()
        translator, events = make(provider, max_pending=1)
        translator.start()
        translator.submit("a", "一")
        await settle()
        translator.submit("b", "二")
        translator.submit("c", "三")  # queue (size 1) already holds b
        provider.gate.set()
        await translator.drain()
        await translator.close()
        return events

    events = asyncio.run(scenario())
    errors = [e for e in events if e["type"] == "translation.error"]
    assert errors[0]["code"] == "queue_full"
    assert errors[0]["source_segment_ids"] == ["c"]
    assert errors[0]["stopped"] is False
    translated = [e["source_segment_ids"] for e in events if e["type"] == "translation.segment"]
    assert translated == [["a"], ["b"]]


def test_wait_on_a_forced_call_carries_ids_into_the_next_commit() -> None:
    async def scenario() -> list[dict[str, Any]]:
        provider = FakeProvider()
        provider.results = [{"action": "WAIT", "source": "一", "text": ""}]
        translator, events = make(provider)
        translator.start()
        translator.submit("a", "一")
        await settle()
        translator.submit("b", "二")
        await translator.drain()
        await translator.close()
        return events

    events = asyncio.run(scenario())
    assert [e["source_segment_ids"] for e in events] == [["a", "b"]]


def test_unrecoverable_provider_stops_translation_visibly() -> None:
    async def scenario() -> tuple[FakeProvider, list[dict[str, Any]]]:
        provider = FakeProvider()
        provider.results = [ApiError("translation_unavailable", "SSD 不見了", retryable=False)]
        translator, events = make(provider)
        translator.start()
        translator.submit("a", "一")
        await settle()
        translator.submit("b", "二")
        await translator.drain()
        await translator.close()
        return provider, events

    provider, events = asyncio.run(scenario())
    assert len(events) == 1
    assert events[0]["type"] == "translation.error"
    assert events[0]["stopped"] is True
    assert events[0]["source_segment_ids"] == ["a"]
    assert provider.calls == ["一"]


def test_retryable_failure_reports_those_segments_and_keeps_going() -> None:
    async def scenario() -> list[dict[str, Any]]:
        provider = FakeProvider()
        provider.results = [ApiError("translation_timeout", "slow", retryable=True)]
        translator, events = make(provider)
        translator.start()
        translator.submit("a", "一")
        await settle()
        translator.submit("b", "二")
        await translator.drain()
        await translator.close()
        return events

    events = asyncio.run(scenario())
    assert [(e["type"], e["source_segment_ids"]) for e in events] == [
        ("translation.error", ["a"]),
        ("translation.segment", ["b"]),
    ]


def test_worker_restart_restarts_the_session_and_reports_lost_buffer() -> None:
    async def scenario() -> tuple[FakeProvider, list[dict[str, Any]]]:
        provider = FakeProvider()
        provider.results = [{"action": "WAIT", "source": "一", "text": ""}]
        translator, events = make(provider)
        translator.start()
        translator.submit("a", "一")
        await settle()
        provider.generation = 2
        translator.submit("b", "二")
        await translator.drain()
        await translator.close()
        return provider, events

    provider, events = asyncio.run(scenario())
    assert provider.starts == 2
    assert [(e["type"], e["source_segment_ids"]) for e in events] == [
        ("translation.error", ["a"]),
        ("translation.segment", ["b"]),
    ]


def test_drain_timeout_names_every_unfinished_segment() -> None:
    async def scenario() -> list[dict[str, Any]]:
        provider = FakeProvider()
        provider.gate = asyncio.Event()
        translator, events = make(provider)
        translator.start()
        translator.submit("a", "一")
        await settle()
        translator.submit("b", "二")
        await translator.drain(timeout=0.05)
        translator.submit("c", "三")
        await settle()
        return events

    events = asyncio.run(scenario())
    assert len(events) == 1
    assert events[0]["code"] == "translation_timeout"
    assert events[0]["source_segment_ids"] == ["a", "b"]
    assert events[0]["stopped"] is True


def test_nothing_is_emitted_after_close() -> None:
    async def scenario() -> list[dict[str, Any]]:
        provider = FakeProvider()
        provider.delay_s = 0.05
        translator, events = make(provider)
        translator.start()
        translator.submit("a", "一")
        await settle()
        await translator.close()
        translator.submit("b", "二")
        await asyncio.sleep(0.1)
        return events

    assert asyncio.run(scenario()) == []


@pytest.mark.parametrize("max_pending", [1, 16])
def test_submit_never_blocks(max_pending: int) -> None:
    async def scenario() -> None:
        provider = FakeProvider()
        provider.gate = asyncio.Event()
        translator, _ = make(provider, max_pending=max_pending)
        translator.start()
        for index in range(100):
            translator.submit(f"s{index}", "字")
        await translator.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=2))
