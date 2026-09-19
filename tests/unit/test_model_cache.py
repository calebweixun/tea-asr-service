from __future__ import annotations

from pathlib import Path

from tea_asr.model_spec import default_model_cache


def test_model_is_not_kept_in_a_purgeable_cache() -> None:
    """macOS deletes ~/Library/Caches under disk pressure.

    It really happened: the whole 1.2 GB model vanished and the service only
    failed on the next request. Anything expensive to re-download must live
    somewhere the system will not reclaim.
    """

    path = default_model_cache()
    purgeable = Path.home() / "Library" / "Caches"
    assert purgeable not in path.parents, f"{path} sits under a purgeable cache"


def test_model_cache_follows_the_hugging_face_default() -> None:
    from huggingface_hub.constants import HF_HUB_CACHE

    assert default_model_cache() == Path(HF_HUB_CACHE)
