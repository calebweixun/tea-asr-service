from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from tea_asr.audio import load_pcm16_wav


def _write_wav(path: Path, *, rate: int = 16_000, channels: int = 1) -> Path:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x01" * 160 * channels)
    return path


def test_loads_16k_mono(tmp_path: Path) -> None:
    audio = load_pcm16_wav(_write_wav(tmp_path / "ok.wav"))
    assert audio.dtype == np.float32
    assert audio.size == 160


@pytest.mark.parametrize(("rate", "channels"), [(48_000, 1), (16_000, 2)])
def test_rejects_wrong_format(tmp_path: Path, rate: int, channels: int) -> None:
    path = _write_wav(tmp_path / f"bad-{rate}-{channels}.wav", rate=rate, channels=channels)
    with pytest.raises(ValueError, match="16 kHz"):
        load_pcm16_wav(path)
