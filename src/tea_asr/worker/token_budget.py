"""Duration-scaled output token budgets for bounded ASR requests."""

from __future__ import annotations

from math import ceil

SAMPLE_RATE = 16_000
MAX_GENERATION_TOKENS = 512
TOKENS_PER_AUDIO_SECOND = 11
TOKEN_BUFFER = 8


def max_tokens_for_samples(sample_count: int) -> int:
    """Return the generation budget for 16 kHz mono audio samples."""

    if sample_count < 0:
        raise ValueError("sample_count cannot be negative")
    audio_seconds = sample_count / SAMPLE_RATE
    return min(
        MAX_GENERATION_TOKENS,
        ceil(audio_seconds * TOKENS_PER_AUDIO_SECOND) + TOKEN_BUFFER,
    )


def max_tokens_for_pcm(pcm: bytes) -> int:
    """Return the generation budget for signed 16-bit, 16 kHz mono PCM."""

    return max_tokens_for_samples(len(pcm) // 2)
