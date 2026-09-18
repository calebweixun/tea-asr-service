from __future__ import annotations

from pathlib import Path

from huggingface_hub import snapshot_download

from .model_spec import ModelSpec, default_model_cache


def prepare_model(spec: ModelSpec, cache_dir: Path | None = None) -> Path:
    root = cache_dir or default_model_cache()
    root.mkdir(parents=True, exist_ok=True)
    snapshot = snapshot_download(
        repo_id=spec.repo_id,
        revision=spec.revision,
        cache_dir=root,
    )
    return Path(snapshot)


def locate_prepared_model(spec: ModelSpec, cache_dir: Path | None = None) -> Path:
    root = cache_dir or default_model_cache()
    snapshot = snapshot_download(
        repo_id=spec.repo_id,
        revision=spec.revision,
        cache_dir=root,
        local_files_only=True,
    )
    return Path(snapshot)
