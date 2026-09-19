from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tea_asr.api.app import create_app
from tea_asr.config import ServiceConfig

AUTH = {"Authorization": "Bearer test-token"}


class FakeSupervisor:
    """Test double for the MLX worker.

    docs/06 forbids a production fallback to a fake backend, so this lives in
    tests only and is injected explicitly through `create_app`.
    """

    def __init__(self, *, text: str = "測試文字", state: str = "ready") -> None:
        self.state = state
        self.last_error: str | None = None
        self.load_ms = 10
        self.generation = 1
        self.text = text
        self.calls = 0
        self.failure: Exception | None = None
        self.delay_s = 0.0

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def transcribe(self, pcm: bytes, *, language: str = "Chinese") -> dict[str, Any]:
        self.calls += 1
        if self.delay_s:
            import asyncio

            await asyncio.sleep(self.delay_s)
        if self.failure is not None:
            raise self.failure
        return {
            "status": "ok",
            "text": self.text,
            "audio_samples": len(pcm) // 2,
            "model_input_samples": max(16_000, len(pcm) // 2),
            "total_time_s": 0.01,
            "prompt_tokens": 10,
            "generation_tokens": 3,
        }


class FakeVad:
    """Deterministic stand-in for Silero.

    Any window with real energy counts as speech, so segmentation tests do not
    depend on a downloaded ONNX asset or on model behaviour.
    """

    def probability(self, window: np.ndarray, session: Any) -> float:
        return 1.0 if float(np.abs(window).max()) > 0.05 else 0.0


def build_client(
    supervisor: FakeSupervisor,
    *,
    revisable_preview: bool = False,
    filter_pua: bool = True,
    vad: Any = None,
) -> TestClient:
    app = create_app(
        Path("unused"),
        token="test-token",
        supervisor=supervisor,
        config=ServiceConfig(revisable_preview=revisable_preview, filter_pua=filter_pua),
        vad_model=vad,
    )
    return TestClient(app)


@pytest.fixture
def supervisor() -> FakeSupervisor:
    return FakeSupervisor()


@pytest.fixture
def client(supervisor: FakeSupervisor) -> TestClient:
    return build_client(supervisor)


@pytest.fixture
def continuous_client(supervisor: FakeSupervisor) -> TestClient:
    return build_client(supervisor, vad=FakeVad())


@pytest.fixture
def preview_client(supervisor: FakeSupervisor) -> TestClient:
    return build_client(supervisor, revisable_preview=True)
