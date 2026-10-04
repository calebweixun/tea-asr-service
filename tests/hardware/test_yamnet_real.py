"""Real YAMNet asset: verifies the pinned file's contract. Skipped when absent.

    uv run pytest -m hardware tests/hardware/test_yamnet_real.py

The asset comes from `tea-asr model-prepare`; CI never downloads it.
"""

from __future__ import annotations

import numpy as np
import pytest

from tea_asr.singing import ClassGroups, frame_features
from tea_asr.yamnet import (
    HOP_SAMPLES,
    NUM_CLASSES,
    PATCH_SAMPLES,
    SAMPLE_RATE,
    YamnetModel,
    complete_frames,
    load_class_names,
    locate_yamnet,
    verify_assets,
)

pytestmark = pytest.mark.hardware


@pytest.fixture(scope="module")
def real() -> tuple[YamnetModel, tuple[str, ...]]:
    try:
        model_path, class_map_path = locate_yamnet()
    except Exception as exc:  # noqa: BLE001 - any lookup failure means "not prepared"
        pytest.skip(f"YAMNet is not prepared: {exc}")
    assert verify_assets(model_path, class_map_path)
    return YamnetModel(model_path), load_class_names(class_map_path)


def test_frame_contract_and_class_groups(real: tuple[YamnetModel, tuple[str, ...]]) -> None:
    model, names = real
    ClassGroups.from_names(names)
    assert names[0] == "Speech" and names[24] == "Singing" and names[132] == "Music"
    wave = (np.random.default_rng(0).standard_normal(SAMPLE_RATE * 5) * 0.05).astype(np.float32)
    scores = model.scores(wave)
    assert scores.shape == (complete_frames(wave.size), NUM_CLASSES)
    assert float(scores.min()) >= 0.0 and float(scores.max()) <= 1.0


def test_frames_are_independent_of_the_surrounding_audio(
    real: tuple[YamnetModel, tuple[str, ...]],
) -> None:
    """The streaming design scores frames in small batches; they must match."""

    model, _ = real
    wave = (np.random.default_rng(1).standard_normal(SAMPLE_RATE * 12) * 0.1).astype(np.float32)
    whole = model.scores(wave)
    for index in (0, 3, 9):
        alone = model.scores(wave[index * HOP_SAMPLES : index * HOP_SAMPLES + PATCH_SAMPLES])
        assert np.abs(alone[0] - whole[index]).max() < 1e-4


def test_silence_is_not_singing(real: tuple[YamnetModel, tuple[str, ...]]) -> None:
    model, names = real
    groups = ClassGroups.from_names(names)
    features = frame_features(model.scores(np.zeros(SAMPLE_RATE * 3, dtype=np.float32)), groups)
    assert features[:, 2].max() < 0.2  # no vocal-music evidence in silence
