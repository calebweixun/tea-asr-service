from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .model_spec import default_model_cache

#: Silero VAD v5, MIT licensed, pinned by repo revision and file hash so the
#: segmentation behaviour cannot drift under us (docs/03, docs/05).
VAD_REPO_ID = "onnx-community/silero-vad"
VAD_REVISION = "e71cae966052b992a7eca6b17738916ce0eca4ec"
VAD_FILENAME = "onnx/model.onnx"
VAD_SHA256 = "a4a068cd6cf1ea8355b84327595838ca748ec29a25bc91fc82e6c299ccdc5808"

#: Silero v5 accepts exactly 512 samples (32 ms) per call at 16 kHz.
VAD_WINDOW_SAMPLES = 512
VAD_SAMPLE_RATE = 16_000


class VadUnavailableError(RuntimeError):
    pass


def prepare_vad(cache_dir: Path | None = None) -> Path:
    from huggingface_hub import hf_hub_download

    root = cache_dir or default_model_cache()
    root.mkdir(parents=True, exist_ok=True)
    return Path(
        hf_hub_download(
            repo_id=VAD_REPO_ID,
            revision=VAD_REVISION,
            filename=VAD_FILENAME,
            cache_dir=root,
        )
    )


def locate_vad(cache_dir: Path | None = None) -> Path:
    from huggingface_hub import hf_hub_download

    root = cache_dir or default_model_cache()
    return Path(
        hf_hub_download(
            repo_id=VAD_REPO_ID,
            revision=VAD_REVISION,
            filename=VAD_FILENAME,
            cache_dir=root,
            local_files_only=True,
        )
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(slots=True)
class VadSession:
    """Per-session recurrent state.

    Silero is recurrent, so every session needs its own state or one speaker's
    audio biases another's segmentation (docs/05 P0 item 6).
    """

    state: np.ndarray

    @classmethod
    def new(cls) -> VadSession:
        return cls(state=np.zeros((2, 1, 128), dtype=np.float32))


class SileroVad:
    """Thin ONNX Runtime wrapper. No Torch, no upstream Python entry point."""

    def __init__(self, model_path: Path, *, expected_sha256: str | None = None) -> None:
        if not model_path.is_file():
            raise VadUnavailableError(f"VAD asset is missing: {model_path}")
        if expected_sha256 and sha256(model_path) != expected_sha256:
            raise VadUnavailableError(f"VAD asset hash does not match the lock: {model_path}")
        try:
            import onnxruntime
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise VadUnavailableError("onnxruntime is not installed") from exc

        options = onnxruntime.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        self._session: Any = onnxruntime.InferenceSession(
            str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._sample_rate = np.array(VAD_SAMPLE_RATE, dtype=np.int64)

    def probability(self, window: np.ndarray, session: VadSession) -> float:
        """Speech probability for exactly one 512-sample window."""

        if window.size != VAD_WINDOW_SAMPLES:
            raise ValueError(f"VAD window must be {VAD_WINDOW_SAMPLES} samples")
        outputs = self._session.run(
            None,
            {
                "input": window.reshape(1, -1).astype(np.float32),
                "state": session.state,
                "sr": self._sample_rate,
            },
        )
        session.state = outputs[1]
        return float(outputs[0].item())
