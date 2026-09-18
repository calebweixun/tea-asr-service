from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


class ModelCompatibilityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Transcription:
    text: str
    audio_samples: int
    model_input_samples: int
    total_time_s: float
    prompt_tokens: int
    generation_tokens: int


@contextlib.contextmanager
def _mixed_quantization_loader() -> Iterator[None]:
    """Allow the checkpoint's per-layer config to quantize its audio tower.

    mlx-audio 0.4.5 excludes the whole audio tower before consulting per-layer
    overrides. This checkpoint intentionally stores those linear layers in 8 bit.
    Keep the monkey patch local to model loading and restore it afterwards.
    """

    from mlx_audio.stt.models.qwen3_asr import qwen3_asr

    owner = qwen3_asr.Qwen3ASRModel
    original = owner.model_quant_predicate
    owner.model_quant_predicate = lambda self, path, module: True
    try:
        yield
    finally:
        owner.model_quant_predicate = original


class TeaMlxBackend:
    def __init__(self, model_path: Path) -> None:
        self._model_path = model_path
        self._model: Any | None = None

    def load(self) -> None:
        if not self._model_path.is_dir():
            raise ModelCompatibilityError(f"Model snapshot does not exist: {self._model_path}")

        from mlx_audio.stt.utils import load_model

        try:
            with _mixed_quantization_loader():
                self._model = load_model(self._model_path, lazy=False, strict=True)
        except Exception as exc:
            raise ModelCompatibilityError(f"Strict MLX model load failed: {exc}") from exc

        if not hasattr(self._model, "generate"):
            raise ModelCompatibilityError("Loaded model does not expose generate()")
        if not hasattr(self._model, "_tokenizer") or not hasattr(self._model, "_feature_extractor"):
            raise ModelCompatibilityError("Tokenizer or feature extractor was not initialized")

    def transcribe(self, audio: np.ndarray, *, language: str = "Chinese") -> Transcription:
        if self._model is None:
            raise RuntimeError("Model has not been loaded")
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if samples.size == 0:
            return Transcription("", 0, 0, 0.0, 0, 0)
        if not np.isfinite(samples).all():
            raise ValueError("Audio contains NaN or infinity")

        result = self._model.generate(
            samples,
            language=language,
            temperature=0.0,
            batch_size=1,
            max_tokens=512,
            min_chunk_duration=1.0,
            verbose=False,
        )
        model_samples = max(samples.size, 16_000)
        return Transcription(
            text=str(result.text).strip(),
            audio_samples=int(samples.size),
            model_input_samples=model_samples,
            total_time_s=float(result.total_time),
            prompt_tokens=int(result.prompt_tokens),
            generation_tokens=int(result.generation_tokens),
        )

    def close(self) -> None:
        self._model = None
        try:
            import mlx.core as mx

            mx.clear_cache()
        except ImportError:
            pass
