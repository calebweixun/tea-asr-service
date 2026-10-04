from __future__ import annotations

import hashlib
from pathlib import Path

from huggingface_hub import snapshot_download

from .model_spec import ModelSpec, default_model_cache


class ModelUnavailableError(RuntimeError):
    """The explicitly selected ASR model is missing or does not match its pin."""

    code = "model_unavailable"


_REBUILD_8BIT = (
    "Recreate it from the already-prepared BF16 source with:\n"
    "  uv run python benchmarks/convert_quant.py --bits 8 --out models/mlx-8bit-selfconv"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_local_model(spec: ModelSpec, cache_dir: Path | None = None) -> Path:
    """Verify the complete file set and SHA-256 pin of a local model artifact."""

    if spec.local_dir is None or not spec.files:
        raise ValueError("verify_local_model requires a local model spec with file pins")
    root = cache_dir or default_model_cache()
    model_path = root / spec.local_dir
    if not model_path.is_dir():
        raise ModelUnavailableError(
            f"model_unavailable: local {spec.variant or 'ASR'} model directory is missing: "
            f"{model_path}. {_REBUILD_8BIT}"
        )

    expected = dict(spec.files)
    actual_paths = {
        path.relative_to(model_path).as_posix(): path
        for path in model_path.rglob("*")
        if path.is_file()
    }
    missing = sorted(expected.keys() - actual_paths.keys())
    extra = sorted(actual_paths.keys() - expected.keys())
    mismatched = [
        name
        for name, expected_hash in expected.items()
        if name in actual_paths and _sha256(actual_paths[name]) != expected_hash
    ]
    if missing or extra or mismatched:
        details = []
        if missing:
            details.append(f"missing files: {', '.join(missing)}")
        if extra:
            details.append(f"unpinned files: {', '.join(extra)}")
        if mismatched:
            details.append(f"sha256 mismatch: {', '.join(mismatched)}")
        raise ModelUnavailableError(
            f"model_unavailable: local model pin check failed at {model_path} "
            f"({'; '.join(details)}). {_REBUILD_8BIT}"
        )
    return model_path


def prepare_model(spec: ModelSpec, cache_dir: Path | None = None) -> Path:
    if spec.local_dir is not None:
        return verify_local_model(spec, cache_dir)
    root = cache_dir or default_model_cache()
    root.mkdir(parents=True, exist_ok=True)
    snapshot = snapshot_download(
        repo_id=spec.repo_id,
        revision=spec.revision,
        cache_dir=root,
    )
    return Path(snapshot)


def locate_prepared_model(spec: ModelSpec, cache_dir: Path | None = None) -> Path:
    if spec.local_dir is not None:
        return verify_local_model(spec, cache_dir)
    root = cache_dir or default_model_cache()
    snapshot = snapshot_download(
        repo_id=spec.repo_id,
        revision=spec.revision,
        cache_dir=root,
        local_files_only=True,
    )
    return Path(snapshot)
