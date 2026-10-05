from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import Mock

import numpy as np
import pytest

from tea_asr.backend import MAX_CONTEXT_PROMPT_TOKENS, TeaMlxBackend
from tea_asr.context import build_system_prompt
from tea_asr.worker.token_budget import max_tokens_for_samples


class CharacterTokenizer:
    """Small deterministic tokenizer double for prompt passing and capping."""

    def encode(self, text: str, *, return_tensors: str) -> np.ndarray:
        assert return_tensors == "np"
        return np.array([[ord(character) for character in text]], dtype=np.int64)

    def decode(self, tokens: list[int], *, skip_special_tokens: bool) -> str:
        assert skip_special_tokens
        return "".join(chr(token) for token in tokens)


class RecordingModel:
    def __init__(self) -> None:
        self._tokenizer = CharacterTokenizer()
        self.calls: list[dict[str, Any]] = []

    def generate(self, audio: np.ndarray, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(
            text="辨識文字",
            total_time=0.01,
            prompt_tokens=42,
            generation_tokens=3,
        )


def _backend_with(model: RecordingModel) -> TeaMlxBackend:
    backend = TeaMlxBackend.__new__(TeaMlxBackend)
    backend._model_path = None  # type: ignore[assignment]
    backend._model = model
    return backend


def test_no_context_keeps_the_existing_generate_arguments_unchanged() -> None:
    model = RecordingModel()
    backend = _backend_with(model)

    backend.transcribe(np.ones(4, dtype=np.float32))

    assert model.calls == [
        {
            "language": "Chinese",
            "temperature": 0.0,
            "batch_size": 1,
            "max_tokens": max_tokens_for_samples(4),
            "min_chunk_duration": 1.0,
            "verbose": False,
        }
    ]


def test_audio_duration_sets_backend_budget_without_truncating_normal_text() -> None:
    model = RecordingModel()
    backend = _backend_with(model)

    for duration_s, expected_cap in ((15, 173), (30, 338), (60, 512)):
        result = backend.transcribe(np.ones(16_000 * duration_s, dtype=np.float32))
        assert model.calls[-1]["max_tokens"] == expected_cap
        assert result.text == "辨識文字"


def test_worker_supplied_budget_is_forwarded_to_model_generation() -> None:
    model = RecordingModel()
    backend = _backend_with(model)

    result = backend.transcribe(
        np.ones(16_000 * 15, dtype=np.float32),
        max_tokens=79,
    )

    assert model.calls[0]["max_tokens"] == 79
    assert result.text == "辨識文字"


def test_close_clears_model_and_mlx_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _backend_with(RecordingModel())
    mlx = ModuleType("mlx")
    core = ModuleType("mlx.core")
    core.clear_cache = Mock()
    mlx.core = core
    monkeypatch.setitem(sys.modules, "mlx", mlx)
    monkeypatch.setitem(sys.modules, "mlx.core", core)

    backend.close()

    assert backend._model is None
    core.clear_cache.assert_called_once_with()


def test_context_is_passed_to_model_and_capped_by_its_tokenizer() -> None:
    model = RecordingModel()
    backend = _backend_with(model)
    prompt = "Use vocabulary as hints only. " + "聖經" * 400

    backend.transcribe(np.ones(4, dtype=np.float32), system_prompt=prompt)

    passed = model.calls[0]["system_prompt"]
    assert passed.startswith("Use vocabulary as hints only.")
    assert len(model._tokenizer.encode(passed + "\n", return_tensors="np")[0]) <= (
        MAX_CONTEXT_PROMPT_TOKENS
    )
    assert model.calls[0]["language"] == "Chinese"


def test_prompt_builder_only_adds_domain_and_hotwords() -> None:
    assert build_system_prompt(None, ()) is None
    prompt = build_system_prompt("Christian sermons", ("聖經", "禱告"))
    assert prompt is not None
    assert "Christian sermons" in prompt
    assert "聖經、禱告" in prompt
    assert "Transcribe audible speech only" in prompt
