from __future__ import annotations

import asyncio
import json
from typing import Any

from tea_asr.api.stream import ContinuousSessionAdmission, StreamSession
from tea_asr.config import ServiceConfig
from tea_asr.errors import ApiError

CONTINUOUS_START = {
    "type": "session.start",
    "request_id": "start",
    "profile": "continuous",
    "audio": {"sample_rate": 16_000, "channels": 1, "format": "pcm_s16le"},
    "language": "Chinese",
    "durable": False,
}


class SimultaneousStartSocket:
    def __init__(self, barrier: asyncio.Barrier) -> None:
        self._barrier = barrier

    async def receive(self) -> dict[str, Any]:
        await self._barrier.wait()
        return {"type": "websocket.receive", "text": json.dumps(CONTINUOUS_START)}


def test_simultaneous_continuous_starts_compete_for_one_slot() -> None:
    async def scenario() -> tuple[list[Any], bool]:
        barrier = asyncio.Barrier(2)
        admission = ContinuousSessionAdmission(1)
        sessions = [
            StreamSession(
                SimultaneousStartSocket(barrier),  # type: ignore[arg-type]
                scheduler=None,  # type: ignore[arg-type]
                config=ServiceConfig(),
                model_state="ready",
                vad=object(),  # type: ignore[arg-type]
                continuous_admission=admission,
            )
            for _ in range(2)
        ]
        results = await asyncio.gather(
            *(session._read_session_start() for session in sessions),
            return_exceptions=True,
        )
        for session in sessions:
            session.release_continuous_admission()
        return results, await admission.try_acquire(object())

    results, acquired_after_release = asyncio.run(scenario())

    admitted = [result for result in results if not isinstance(result, ApiError)]
    rejected = [result for result in results if isinstance(result, ApiError)]
    assert len(admitted) == 1
    assert len(rejected) == 1
    assert rejected[0].code == "concurrent_session_limit"
    assert rejected[0].retryable is True
    assert acquired_after_release is True
