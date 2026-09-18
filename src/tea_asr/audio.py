from __future__ import annotations

import wave
from pathlib import Path

import numpy as np


def load_pcm16_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wav:
        if wav.getframerate() != 16_000 or wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError("WAV must be 16 kHz, mono, signed 16-bit PCM")
        pcm = wav.readframes(wav.getnframes())
    if len(pcm) % 2:
        raise ValueError("PCM byte length must be even")
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0

