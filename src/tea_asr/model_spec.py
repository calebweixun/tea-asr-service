from __future__ import annotations

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


def default_model_cache() -> Path:
    """The standard Hugging Face cache, as docs/03 specifies.

    An earlier implementation used `~/Library/Caches/TEA ASR/models`. macOS
    treats `~/Library/Caches` as purgeable and deleted the whole 1.2 GB model
    under disk pressure, leaving a service that looked healthy until the first
    request. Anything expensive to re-download does not belong there.
    """

    from huggingface_hub.constants import HF_HUB_CACHE

    return Path(HF_HUB_CACHE)

