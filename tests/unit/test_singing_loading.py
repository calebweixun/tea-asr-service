from __future__ import annotations

from pathlib import Path

import pytest

from tea_asr.api import app as app_module
from tea_asr.config import ServiceConfig
from tests.conftest import fake_class_names


def _class_map(path: Path) -> Path:
    rows = ["index,mid,display_name"]
    rows += [f'{i},/m/{i},"{name}"' for i, name in enumerate(fake_class_names())]
    path.write_text("\n".join(rows), encoding="utf-8")
    return path


def test_disabled_config_never_touches_the_asset(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode() -> None:
        raise AssertionError("must not look for the asset when disabled")

    monkeypatch.setattr(app_module, "locate_yamnet", explode)
    assert app_module._load_singing(ServiceConfig(singing_detection_enabled=False)) is None


def test_missing_asset_means_no_runtime_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing() -> None:
        raise FileNotFoundError("not prepared")

    monkeypatch.setattr(app_module, "locate_yamnet", missing)
    assert app_module._load_singing(ServiceConfig()) is None


def test_hash_mismatch_means_no_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    model = tmp_path / "yamnet.onnx"
    model.write_bytes(b"tampered")
    monkeypatch.setattr(
        app_module, "locate_yamnet", lambda: (model, _class_map(tmp_path / "c.csv"))
    )
    assert app_module._load_singing(ServiceConfig()) is None


def test_verified_asset_builds_a_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    model = tmp_path / "yamnet.onnx"
    model.write_bytes(b"model")
    classes = _class_map(tmp_path / "c.csv")
    monkeypatch.setattr(app_module, "locate_yamnet", lambda: (model, classes))
    monkeypatch.setattr(app_module, "verify_assets", lambda *_: True)

    class StubModel:
        def __init__(self, path: Path, *, expected_sha256: str | None = None) -> None:
            self.path = path

    monkeypatch.setattr(app_module, "YamnetModel", StubModel)
    runtime = app_module._load_singing(ServiceConfig())
    assert runtime is not None
    assert runtime.model.path == model  # type: ignore[attr-defined]
    runtime.close()


def test_class_map_hash_is_verified_too(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import hashlib

    from tea_asr import yamnet

    model = tmp_path / "yamnet.onnx"
    model.write_bytes(b"model")
    classes = _class_map(tmp_path / "c.csv")  # not the pinned class map
    monkeypatch.setattr(yamnet, "YAMNET_SHA256", hashlib.sha256(b"model").hexdigest())
    monkeypatch.setattr(app_module, "locate_yamnet", lambda: (model, classes))

    loaded: list[Path] = []

    class StubModel:
        def __init__(self, path: Path, *, expected_sha256: str | None = None) -> None:
            loaded.append(path)

    monkeypatch.setattr(app_module, "YamnetModel", StubModel)
    assert app_module._load_singing(ServiceConfig()) is None
    assert loaded == []  # an unverified asset is never handed to onnxruntime
