from __future__ import annotations

import wave
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("torch")

from benchmarks.embedding_asr_probe import (
    ProbeItem,
    ServiceData,
    _load_embeddings,
    embed_modality,
)


class FakeEncoder:
    def __init__(self) -> None:
        self.text_calls = 0
        self.audio_calls = 0

    def encode_texts(self, texts: Sequence[str]) -> np.ndarray:
        self.text_calls += len(texts)
        vectors = np.zeros((len(texts), 768), dtype=np.float32)
        for row, text in enumerate(texts):
            vectors[row, ord(text[0]) % 768] = 1.0
        return vectors

    def encode_audio(self, samples: np.ndarray) -> np.ndarray:
        self.audio_calls += 1
        vector = np.zeros(768, dtype=np.float32)
        vector[1 if samples.mean() >= 0 else 2] = 1.0
        return vector


def _write_test_wav(path: Path) -> None:
    pcm = np.zeros(3200, dtype="<i2")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(pcm.tobytes())


def test_fake_encoder_builds_resumable_768_and_256_caches(tmp_path: Path) -> None:
    wav_path = tmp_path / "fake.wav"
    _write_test_wav(wav_path)
    items = (
        ProbeItem("one", 0.0, 0.05, "alpha", "alpha"),
        ProbeItem("two", 0.05, 0.1, "bravo", "bravo"),
    )
    data = ServiceData(
        service="fake",
        items=items,
        wav_path=wav_path,
        rules=(),
        rule_pairs=(),
        source_signature="fake-input-signature",
    )
    encoder = FakeEncoder()
    cache_dir = tmp_path / "cache"

    text_timing = embed_modality(
        data,
        cache_dir,
        "reference",
        encoder,
        "cpu",
        "float32",
        1.0,
        end_row=2,
        batch_size=2,
    )
    audio_timing = embed_modality(
        data,
        cache_dir,
        "audio",
        encoder,
        "cpu",
        "float32",
        1.0,
        end_row=2,
        batch_size=2,
    )

    assert text_timing["count"] == 2
    assert audio_timing["count"] == 2
    assert encoder.text_calls == 2
    assert encoder.audio_calls == 2
    assert _load_embeddings(cache_dir, "reference", 768, 2).shape == (2, 768)
    truncated = _load_embeddings(cache_dir, "reference", 256, 2)
    np.testing.assert_allclose(np.linalg.norm(truncated, axis=1), 1.0)

    embed_modality(
        data,
        cache_dir,
        "reference",
        encoder,
        "cpu",
        "float32",
        1.0,
        end_row=2,
        batch_size=2,
    )
    assert encoder.text_calls == 2
