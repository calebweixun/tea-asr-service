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


def test_model_lives_beside_the_checkout_when_there_is_one() -> None:
    from tea_asr.model_spec import project_root

    root = project_root()
    assert root is not None, "the tests run from a source checkout"
    assert default_model_cache() == root / "models"


def test_an_explicit_override_wins(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TEA_ASR_MODELS_DIR", "/tmp/somewhere-else")
    assert default_model_cache() == Path("/tmp/somewhere-else")


def test_models_directory_is_not_tracked_by_git() -> None:
    from tea_asr.model_spec import project_root

    root = project_root()
    assert root is not None
    ignored = (root / ".gitignore").read_text().splitlines()
    assert any(line.strip() in {"models/", "/models/"} for line in ignored), (
        "a 1.2 GB download must never be committable"
    )
