from __future__ import annotations

import pytest

from tea_asr.worker.token_budget import max_tokens_for_pcm, max_tokens_for_samples


def test_generation_budget_scales_with_pcm_duration_and_saturates() -> None:
    assert max_tokens_for_samples(16_000) == 19
    assert max_tokens_for_samples(102_608) == 79
    assert max_tokens_for_samples(240_000) == 173
    assert max_tokens_for_samples(480_000) == 338
    assert max_tokens_for_samples(960_000) == 512


def test_pcm_budget_uses_16khz_s16le_duration() -> None:
    assert max_tokens_for_pcm(b"\x00\x00" * 16_000) == 19


def test_generation_budget_rejects_negative_audio_length() -> None:
    with pytest.raises(ValueError):
        max_tokens_for_samples(-1)
