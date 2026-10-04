from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ModelSpec:
    repo_id: str
    revision: str
    mlx_audio_version: str
    variant: str | None = None
    local_dir: str | None = None
    files: tuple[tuple[str, str], ...] = ()
    source_repo_id: str | None = None
    source_revision: str | None = None
    mlx_version: str | None = None
    mlx_lm_version: str | None = None


ASR_MODEL_DEFAULT = "tea-1.1-mlx-4bit"
ASR_MODEL_4BIT = "tea-1.1-mlx-4bit"
ASR_MODEL_8BIT = "tea-1.1-mlx-8bit"
ASR_MODEL_VARIANTS = frozenset({ASR_MODEL_4BIT, ASR_MODEL_8BIT})


TEA_ASR_1_1_MLX_4BIT = ModelSpec(
    repo_id="Alkd/TEA-ASR-1.1-MLX-4bit",
    revision="caee57a908b6d64be08a6462c7a21ececbd4d7cb",
    mlx_audio_version="0.4.5",
    variant=ASR_MODEL_4BIT,
)

TEA_ASR_1_1_MLX_8BIT = ModelSpec(
    repo_id="local/tea-asr-1.1-mlx-8bit",
    revision="sha256:6ba852674f27082159edabaad8c6d933906e31389661ef2392ae9ba84f9bdf87",
    mlx_audio_version="0.4.5",
    variant=ASR_MODEL_8BIT,
    local_dir="mlx-8bit-selfconv",
    files=(
        (
            "added_tokens.json",
            "de40784677cbd1843cabe5fbee078c7e042cd0b62155f0810af5a13842e5722a",
        ),
        (
            "chat_template.jinja",
            "a103579066642f0e035873f12de6046a1bbc909a7a2833732fda9fd6bc7c528d",
        ),
        (
            "config.json",
            "3c61906f643f4b44188a87c42767b9e091b47192f66f1e596b343f56e46ba808",
        ),
        (
            "generation_config.json",
            "c82553802c5a4871569fcc8f155b529a7188a2abc71e2616b029fb4494fa26ff",
        ),
        (
            "minimal_injection_deployability.json",
            "4937fa4b09695e5bac6d1fc8be5b106da54682fd2a538aee5c8f9bc55e5ae4c6",
        ),
        (
            "model.safetensors",
            "3840ecc2ec9c641505aeb21029705ba6efe33289c06ef1a59d61befacda99de2",
        ),
        (
            "model.safetensors.index.json",
            "0a5d0ec11188602242ff81a9969883d0fdeb98cd5d85cd1413089d897c201af5",
        ),
        (
            "preprocessor_config.json",
            "0443a40d411d3caad769e7b59971fa6eae75926ed4b7e337e60da909eb38c1f7",
        ),
        (
            "special_tokens_map.json",
            "7b376c510ccf9d88bb9bbee41dfc5052122e16e0dec1124a8d8983c59259a9f3",
        ),
        (
            "tokenizer.json",
            "cd76abfab7f2edf5346061f556310d8c06e28e067c99c176fbc2ba304a14b1df",
        ),
        (
            "tokenizer_config.json",
            "7f335cbd3b6f1036808e23613c1c1919f8d5570c11cb55420e5094e036f45669",
        ),
    ),
    source_repo_id="JacobLinCool/TEA-ASR-1.1",
    source_revision="bda08df76d4fd6b487b4a1dd7f0bddf8541696f8",
    mlx_version="0.32.2",
    mlx_lm_version="0.31.3",
)

ASR_MODELS = {
    ASR_MODEL_4BIT: TEA_ASR_1_1_MLX_4BIT,
    ASR_MODEL_8BIT: TEA_ASR_1_1_MLX_8BIT,
}


def asr_model_spec(variant: str) -> ModelSpec:
    """Return the one explicitly selected, pinned ASR model variant."""

    try:
        return ASR_MODELS[variant]
    except KeyError as exc:
        allowed = ", ".join(sorted(ASR_MODELS))
        raise ValueError(f"未知的 ASR model variant {variant!r}; 可用值：{allowed}") from exc


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
