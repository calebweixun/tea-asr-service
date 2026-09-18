from __future__ import annotations

import asyncio

from tea_asr.api.app import detect_sleep


def test_wall_clock_jump_without_monotonic_is_reported_as_sleep() -> None:
    """On Darwin the monotonic clock stops during sleep; the gap is the sleep."""

    async def scenario() -> list[float]:
        wall = [1_000.0]
        mono = [0.0]
        seen: list[float] = []

        async def on_wake(slept: float) -> None:
            seen.append(slept)

        async def tick() -> None:
            # First interval: both clocks advance together — no sleep.
            await asyncio.sleep(0)
            wall[0] += 5.0
            mono[0] += 5.0
            await asyncio.sleep(0.02)
            # Second: only the wall clock jumps — the machine was asleep.
            wall[0] += 905.0
            mono[0] += 5.0
            await asyncio.sleep(0.04)

        watcher = asyncio.create_task(
            detect_sleep(
                on_wake,
                interval=0.01,
                floor=10.0,
                wall_clock=lambda: wall[0],
                monotonic_clock=lambda: mono[0],
            )
        )
        await tick()
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        return seen

    seen = asyncio.run(scenario())
    assert seen, "a 900 s wall-clock jump should be reported"
    assert 890 <= seen[0] <= 910


def test_steady_clocks_do_not_report_sleep() -> None:
    async def scenario() -> list[float]:
        value = [0.0]
        seen: list[float] = []

        async def on_wake(slept: float) -> None:
            seen.append(slept)

        watcher = asyncio.create_task(
            detect_sleep(
                on_wake,
                interval=0.01,
                floor=10.0,
                wall_clock=lambda: value[0],
                monotonic_clock=lambda: value[0],
            )
        )
        for _ in range(5):
            value[0] += 1.0
            await asyncio.sleep(0.02)
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        return seen

    assert asyncio.run(scenario()) == []


class StubWebSocket:
    """Enough of the WebSocket surface for the failure path."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.close_code: int | None = None

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.close_code = code


def test_interrupt_tells_the_client_the_timeline_broke() -> None:
    from tea_asr.api.stream import StreamSession
    from tea_asr.config import ServiceConfig

    async def scenario() -> StubWebSocket:
        socket = StubWebSocket()
        session = StreamSession(
            socket,  # type: ignore[arg-type]
            scheduler=None,  # type: ignore[arg-type]
            config=ServiceConfig(),
            model_state="ready",
        )
        writer = asyncio.create_task(session.writer.run())
        await session.interrupt("機器睡眠了約 900 秒")
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)
        return socket

    socket = asyncio.run(scenario())
    errors = [event for event in socket.sent if event["type"] == "error"]
    assert errors, "the client must be told, not silently cut off"
    assert errors[0]["code"] == "timeline_gap"
    assert errors[0]["retryable"] is True
    # 1012 "service restart": the old sample clock cannot be continued.
    assert socket.close_code == 1012
