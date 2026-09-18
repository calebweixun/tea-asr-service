"""Tests that need the real checkpoint on Apple Silicon.

They are excluded from the default `testpaths` so a normal run never downloads
weights. Run them explicitly:

    uv run pytest tests/hardware -m hardware
"""

from __future__ import annotations

import numpy as np
import pytest

from tea_asr.backend import TeaMlxBackend
from tea_asr.model_manager import locate_prepared_model
from tea_asr.model_spec import TEA_ASR_1_1_MLX_4BIT

pytestmark = pytest.mark.hardware


@pytest.fixture(scope="module")
def backend() -> TeaMlxBackend:
    try:
        model_path = locate_prepared_model(TEA_ASR_1_1_MLX_4BIT)
    except Exception as exc:  # noqa: BLE001 - any lookup failure means "not prepared"
        pytest.skip(f"model is not prepared: {exc}")
    loaded = TeaMlxBackend(model_path)
    loaded.load()
    return loaded


def test_strict_load_exposes_the_expected_surface(backend: TeaMlxBackend) -> None:
    assert backend.transcribe(np.zeros(0, dtype=np.float32)).text == ""


def test_silence_does_not_hallucinate(backend: TeaMlxBackend) -> None:
    silence = np.zeros(16_000, dtype=np.float32)
    result = backend.transcribe(silence)
    assert result.audio_samples == 16_000
    # A quality gate, not a smoke test: silence must not produce a sentence.
    assert len(result.text) <= 4
