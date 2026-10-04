from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tea_asr import yamnet
from tea_asr.yamnet import (
    HOP_SAMPLES,
    NUM_CLASSES,
    PATCH_SAMPLES,
    YamnetModel,
    YamnetUnavailableError,
    complete_frames,
    frame_end_sample,
    load_class_names,
    pcm16_to_float,
    sha256,
    verify_assets,
)


class _Meta:
    def __init__(self, name: str, shape: list[Any]) -> None:
        self.name = name
        self.shape = shape


class FakeSession:
    """Mimics the real graph, which emits one extra zero-padded trailing frame."""

    def __init__(self, *, input_name: str = "waveform", classes: int = NUM_CLASSES) -> None:
        self._input = input_name
        self._classes = classes
        self.seen: list[np.ndarray] = []

    def get_inputs(self) -> list[_Meta]:
        return [_Meta(self._input, [None])]

    def get_outputs(self) -> list[_Meta]:
        return [_Meta("output_0", [None, self._classes])]

    def run(self, _names: Any, feeds: dict[str, np.ndarray]) -> list[np.ndarray]:
        waveform = feeds["waveform"]
        assert waveform.dtype == np.float32 and waveform.ndim == 1
        self.seen.append(waveform)
        frames = complete_frames(waveform.size) + 1  # padded trailing frame
        return [np.full((frames, self._classes), 0.25, dtype=np.float32)]


def test_frame_arithmetic_matches_the_measured_contract() -> None:
    assert (PATCH_SAMPLES, HOP_SAMPLES) == (15_600, 7_680)
    assert complete_frames(PATCH_SAMPLES - 1) == 0
    assert complete_frames(PATCH_SAMPLES) == 1
    assert complete_frames(PATCH_SAMPLES + HOP_SAMPLES - 1) == 1
    assert complete_frames(PATCH_SAMPLES + HOP_SAMPLES) == 2
    assert frame_end_sample(0) == PATCH_SAMPLES
    assert frame_end_sample(3) == 3 * HOP_SAMPLES + PATCH_SAMPLES
    # A frame is complete exactly when its end sample has arrived.
    for index in range(5):
        assert complete_frames(frame_end_sample(index)) == index + 1
        assert complete_frames(frame_end_sample(index) - 1) == index


def test_scores_drop_the_padded_trailing_frame() -> None:
    model = YamnetModel(Path("unused"), session=FakeSession())
    scores = model.scores(np.zeros(PATCH_SAMPLES + HOP_SAMPLES, dtype=np.float32))
    assert scores.shape == (2, NUM_CLASSES)


def test_scores_convert_input_to_contiguous_float32() -> None:
    session = FakeSession()
    model = YamnetModel(Path("unused"), session=session)
    model.scores(np.zeros(PATCH_SAMPLES * 2, dtype=np.float64)[::1])
    assert session.seen[0].dtype == np.float32
    assert session.seen[0].flags["C_CONTIGUOUS"]


def test_scores_refuse_less_than_one_patch() -> None:
    model = YamnetModel(Path("unused"), session=FakeSession())
    with pytest.raises(ValueError, match="at least"):
        model.scores(np.zeros(PATCH_SAMPLES - 1, dtype=np.float32))


def test_changed_input_contract_is_refused() -> None:
    with pytest.raises(YamnetUnavailableError, match="contract"):
        YamnetModel(Path("unused"), session=FakeSession(input_name="audio"))
    with pytest.raises(YamnetUnavailableError, match="shape"):
        YamnetModel(Path("unused"), session=FakeSession(classes=100))


def test_missing_asset_and_hash_mismatch_are_refused(tmp_path: Path) -> None:
    with pytest.raises(YamnetUnavailableError, match="missing"):
        YamnetModel(tmp_path / "nope.onnx")
    fake = tmp_path / "yamnet.onnx"
    fake.write_bytes(b"not a model")
    with pytest.raises(YamnetUnavailableError, match="hash"):
        YamnetModel(fake, expected_sha256="0" * 64)


def test_sha256_and_verify_assets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model = tmp_path / "m.onnx"
    classes = tmp_path / "c.csv"
    model.write_bytes(b"model")
    classes.write_bytes(b"classes")
    assert sha256(model) == hashlib.sha256(b"model").hexdigest()
    assert not verify_assets(model, classes)
    monkeypatch.setattr(yamnet, "YAMNET_SHA256", hashlib.sha256(b"model").hexdigest())
    monkeypatch.setattr(yamnet, "YAMNET_CLASS_MAP_SHA256", hashlib.sha256(b"classes").hexdigest())
    assert verify_assets(model, classes)


def test_class_map_loading_requires_all_521_names(tmp_path: Path) -> None:
    good = tmp_path / "good.csv"
    good.write_text(
        "index,mid,display_name\n"
        + "\n".join(f'{i},/m/{i},"Name {i}, with comma"' for i in range(NUM_CLASSES)),
        encoding="utf-8",
    )
    names = load_class_names(good)
    assert len(names) == NUM_CLASSES and names[24] == "Name 24, with comma"
    short = tmp_path / "short.csv"
    short.write_text("index,mid,display_name\n0,/m/0,Speech\n", encoding="utf-8")
    with pytest.raises(YamnetUnavailableError):
        load_class_names(short)


def test_pcm_conversion_is_little_endian_and_scaled() -> None:
    pcm = np.array([0, 16384, -32768, 32767], dtype="<i2").tobytes()
    out = pcm16_to_float(pcm)
    assert out.dtype == np.float32
    assert out.tolist() == pytest.approx([0.0, 0.5, -1.0, 32767 / 32768])
