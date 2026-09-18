from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tea_asr.api.events import EventWriter, SlowClientError
from tea_asr.wire import AudioAck, SegmentSkipped, TranscriptFinal, TranscriptPartial


class RecordingSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.gate = asyncio.Event()
        self.gate.set()

    async def send_json(self, payload: dict[str, Any]) -> None:
        await self.gate.wait()
        self.sent.append(payload)


def _ack(seq: int) -> AudioAck:
    return AudioAck(session_id="s", event_id=0, received_seq=seq, received_sample=seq * 1600)


def _partial(segment_id: str, revision: int, text: str) -> TranscriptPartial:
    return TranscriptPartial(
        session_id="s",
        event_id=0,
        segment_id=segment_id,
        segment_index=0,
        revision=revision,
        start_sample=0,
        end_sample=revision * 1600,
        text=text,
    )


def test_acks_are_merged_but_finals_are_not() -> None:
    async def scenario() -> None:
        socket = RecordingSocket()
        socket.gate.clear()
        writer = EventWriter(socket, "s")  # type: ignore[arg-type]
        task = asyncio.create_task(writer.run())
        for seq in range(5):
            writer.emit(_ack(seq))
        writer.emit(
            TranscriptFinal(
                session_id="s",
                event_id=0,
                segment_id="seg",
                segment_index=0,
                revision=1,
                start_sample=0,
                end_sample=1600,
                text="一",
                raw_text="一",
                audio_ms=100,
                queue_ms=0,
                inference_ms=1,
            )
        )
        socket.gate.set()
        await writer.drain()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        acks = [event for event in socket.sent if event["type"] == "audio.ack"]
        finals = [event for event in socket.sent if event["type"] == "transcript.final"]
        assert len(acks) == 1
        assert acks[0]["received_seq"] == 4
        assert len(finals) == 1
        event_ids = [event["event_id"] for event in socket.sent]
        assert event_ids == sorted(event_ids)

    asyncio.run(scenario())


def test_pending_partials_are_dropped_once_the_segment_is_terminal() -> None:
    async def scenario() -> None:
        socket = RecordingSocket()
        socket.gate.clear()
        writer = EventWriter(socket, "s")  # type: ignore[arg-type]
        task = asyncio.create_task(writer.run())
        writer.emit(_partial("seg", 1, "我們需要權"))
        writer.emit(_partial("seg", 2, "我們需要全線"))
        writer.emit(
            SegmentSkipped(
                session_id="s", event_id=0, segment_id="seg", segment_index=0, reason="no_speech"
            )
        )
        socket.gate.set()
        await writer.drain()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert [event["type"] for event in socket.sent] == ["segment.skipped"]

    asyncio.run(scenario())


def test_slow_client_is_disconnected_after_the_grace_period() -> None:
    async def scenario() -> None:
        socket = RecordingSocket()
        socket.gate.clear()
        writer = EventWriter(socket, "s", max_items=2, overflow_grace_s=0.05)  # type: ignore[arg-type]
        task = asyncio.create_task(writer.run())
        for index in range(5):
            writer.emit(_partial(f"seg-{index}", 1, "x"))
        with pytest.raises(SlowClientError):
            await writer.watchdog()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
