from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ModelSpec:
    repo_id: str
    revision: str
    mlx_audio_version: str


TEA_ASR_1_1_MLX_4BIT = ModelSpec(
    repo_id="Alkd/TEA-ASR-1.1-MLX-4bit",
    revision="caee57a908b6d64be08a6462c7a21ececbd4d7cb",
    mlx_audio_version="0.4.5",
)


def project_root() -> Path | None:
    """The source checkout this package is running from, if any."""

    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return None


def default_model_cache() -> Path:
    """Where the model and VAD assets live.

    Never `~/Library/Caches`: macOS treats it as purgeable and deleted the whole
    1.2 GB download under disk pressure, leaving a service that looked healthy
    until the next request.

    Order: an explicit `TEA_ASR_MODELS_DIR`, then a `models/` directory beside
    the checkout's pyproject.toml (gitignored, easy to find and easy to delete),
    then Application Support for an installed wheel that has no checkout.
    """

    override = os.environ.get("TEA_ASR_MODELS_DIR")
    if override:
        return Path(override).expanduser()
    root = project_root()
    if root is not None:
        return root / "models"
    return Path.home() / "Library" / "Application Support" / "TEA ASR" / "models"

