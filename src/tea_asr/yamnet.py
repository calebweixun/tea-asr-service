"""YAMNet audio-event classifier on ONNX Runtime (CPU).

YAMNet is Google's AudioSet classifier (Apache-2.0, 521 classes). The ONNX file
is a tf2onnx conversion of the original `google/yamnet` v1 SavedModel with the
mel-spectrogram frontend baked into the graph, so the model consumes a raw
16 kHz mono float waveform and there is no feature extraction to reimplement.

Verified input/output contract (read from the pinned file, see
`tests/hardware/test_yamnet_real.py`):

* input  ``waveform`` float32 ``[num_samples]``, nominally in [-1, 1]
* output ``output_0`` float32 ``[frames, 521]``: per-class scores (sigmoid)
* frame ``i`` covers samples ``[i * 7680, i * 7680 + 15600)`` (0.975 s patch,
  0.48 s hop; measured: a frame scored alone equals the same frame inside a
  long run to 1e-6). The graph also emits one extra zero-padded frame for a
  trailing partial patch (and, from about 20 frames, for an exact fit); the
  wrapper drops it so every returned frame is fully backed by audio.

Licence and attribution: see ``NOTICE``.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .model_spec import default_model_cache

#: Pinned by repo revision and file hash like Silero VAD (src/tea_asr/vad.py).
#: Source: tf2onnx export of Google's `google/yamnet` v1 (Apache-2.0), hosted by
#: the Audio Magic project; the conversion changed no weights (see its README).
YAMNET_REPO_ID = "audiomagic/yamnet-onnx"
YAMNET_REVISION = "f25b741c2f0bdc6d7e6db24b5fddda23347dbafd"
YAMNET_FILENAME = "yamnet.onnx"
YAMNET_SHA256 = "d3835ffbbd4a1bb3e777f0ca217b5007907f5171dd5d17c4236b95b2af8f908e"
#: Byte-identical to Google's tensorflow/models research/audioset/yamnet file at
#: commit dfffd623b6be8d1d9744b8e261fbac370d17c46d.
YAMNET_CLASS_MAP_FILENAME = "yamnet_class_map.csv"
YAMNET_CLASS_MAP_SHA256 = "cdf24d193e196d9e95912a2667051ae203e92a2ba09449218ccb40ef787c6df2"

SAMPLE_RATE = 16_000
#: One patch is 96 STFT frames of 10 ms: (96 - 1) * 160 + 400 samples.
PATCH_SAMPLES = 15_600
HOP_SAMPLES = 7_680
NUM_CLASSES = 521


class YamnetUnavailableError(RuntimeError):
    pass


def frame_end_sample(frame_index: int) -> int:
    """Exclusive end sample of ``frame_index``; the frame is complete then."""

    return frame_index * HOP_SAMPLES + PATCH_SAMPLES


def complete_frames(total_samples: int) -> int:
    """Number of frames fully covered by ``total_samples`` samples."""

    if total_samples < PATCH_SAMPLES:
        return 0
    return 1 + (total_samples - PATCH_SAMPLES) // HOP_SAMPLES


def _download(filename: str, cache_dir: Path | None, *, local_only: bool) -> Path:
    from huggingface_hub import hf_hub_download

    root = cache_dir or default_model_cache()
    if not local_only:
        root.mkdir(parents=True, exist_ok=True)
    return Path(
        hf_hub_download(
            repo_id=YAMNET_REPO_ID,
            revision=YAMNET_REVISION,
            filename=filename,
            cache_dir=root,
            local_files_only=local_only,
        )
    )


def prepare_yamnet(cache_dir: Path | None = None) -> tuple[Path, Path]:
    """Download the pinned model and class map (about 16 MB). Explicit only."""

    return (
        _download(YAMNET_FILENAME, cache_dir, local_only=False),
        _download(YAMNET_CLASS_MAP_FILENAME, cache_dir, local_only=False),
    )


def locate_yamnet(cache_dir: Path | None = None) -> tuple[Path, Path]:
    return (
        _download(YAMNET_FILENAME, cache_dir, local_only=True),
        _download(YAMNET_CLASS_MAP_FILENAME, cache_dir, local_only=True),
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_assets(model_path: Path, class_map_path: Path) -> bool:
    return sha256(model_path) == YAMNET_SHA256 and sha256(class_map_path) == YAMNET_CLASS_MAP_SHA256


def load_class_names(path: Path) -> tuple[str, ...]:
    """Display names indexed by class id (`index,mid,display_name` CSV)."""

    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    names: list[str] = [""] * len(rows)
    for row in rows:
        names[int(row["index"])] = row["display_name"]
    if len(names) != NUM_CLASSES or not all(names):
        raise YamnetUnavailableError(f"unexpected YAMNet class map: {path}")
    return tuple(names)


@dataclass(frozen=True, slots=True)
class YamnetAssets:
    model_path: Path
    class_names: tuple[str, ...]


class YamnetModel:
    """Thin ONNX Runtime wrapper; one shared instance, safe to call from threads.

    ``InferenceSession.run`` is thread-safe, so the server shares a single
    session between executor threads and gives each stream session its own
    audio buffer and smoothing state (the model itself is stateless per frame).
    """

    def __init__(
        self,
        model_path: Path,
        *,
        expected_sha256: str | None = None,
        session: Any | None = None,
    ) -> None:
        if session is None:
            if not model_path.is_file():
                raise YamnetUnavailableError(f"YAMNet asset is missing: {model_path}")
            if expected_sha256 and sha256(model_path) != expected_sha256:
                raise YamnetUnavailableError(
                    f"YAMNet asset hash does not match the lock: {model_path}"
                )
            try:
                import onnxruntime
            except ImportError as exc:  # pragma: no cover - dependency is declared
                raise YamnetUnavailableError("onnxruntime is not installed") from exc
            options = onnxruntime.SessionOptions()
            options.inter_op_num_threads = 1
            options.intra_op_num_threads = 1
            session = onnxruntime.InferenceSession(
                str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
            )
        self._session: Any = session
        inputs = session.get_inputs()
        outputs = session.get_outputs()
        if len(inputs) != 1 or inputs[0].name != "waveform" or not outputs:
            raise YamnetUnavailableError("YAMNet input contract changed; refusing to guess")
        shape = outputs[0].shape
        if len(shape) != 2 or shape[1] != NUM_CLASSES:
            raise YamnetUnavailableError(f"unexpected YAMNet score shape {shape}")

    def scores(self, waveform: np.ndarray) -> np.ndarray:
        """Per-frame class scores, shape ``[frames, 521]`` (float32).

        ``waveform`` is 16 kHz mono float in [-1, 1]. Only frames fully backed
        by audio are returned; fewer than one patch is refused instead of being
        zero padded into a fake frame.
        """

        samples = np.ascontiguousarray(waveform, dtype=np.float32).reshape(-1)
        if samples.size < PATCH_SAMPLES:
            raise ValueError(f"YAMNet needs at least {PATCH_SAMPLES} samples")
        outputs = self._session.run(None, {"waveform": samples})
        scores = np.asarray(outputs[0], dtype=np.float32)
        if scores.ndim != 2 or scores.shape[1] != NUM_CLASSES:
            raise YamnetUnavailableError(f"unexpected YAMNet output shape {scores.shape}")
        return scores[: complete_frames(samples.size)]


def pcm16_to_float(pcm: bytes | bytearray | memoryview) -> np.ndarray:
    """int16 little-endian PCM bytes to float32 in [-1, 1)."""

    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
