from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tea_asr import cli, model_manager
from tea_asr.config import AppPaths, ServiceConfig
from tea_asr.model_manager import ModelUnavailableError
from tea_asr.model_spec import (
    ASR_MODEL_4BIT,
    ASR_MODEL_8BIT,
    TEA_ASR_1_1_MLX_8BIT,
    ModelSpec,
    asr_model_spec,
)


def _local_spec(name: str, payload: bytes) -> ModelSpec:
    return ModelSpec(
        repo_id=f"local/{name}",
        revision="sha256:test",
        mlx_audio_version="test",
        variant=name,
        local_dir=name,
        files=(("weights.bin", hashlib.sha256(payload).hexdigest()),),
    )


def test_default_variant_and_exact_variant_selection() -> None:
    assert ServiceConfig().asr_model == ASR_MODEL_4BIT
    assert asr_model_spec(ASR_MODEL_8BIT) is TEA_ASR_1_1_MLX_8BIT
    with pytest.raises(ValueError, match="未知的 ASR model variant"):
        asr_model_spec("tea-1.1-mlx-16bit")


def test_variant_selection_reads_toml_then_environment(tmp_path: Path) -> None:
    paths = AppPaths(support=tmp_path / "support", logs=tmp_path / "logs")
    paths.support.mkdir()
    paths.config_file.write_text('[service]\nasr_model = "tea-1.1-mlx-8bit"\n')

    assert ServiceConfig.load(paths, env={}).asr_model == ASR_MODEL_8BIT
    assert ServiceConfig.load(paths, env={"TEA_ASR_MODEL": ASR_MODEL_4BIT}).asr_model == ASR_MODEL_4BIT
    assert ServiceConfig.load(env={"TEA_ASR_MODEL": ASR_MODEL_8BIT}).asr_model == ASR_MODEL_8BIT


def test_unknown_variant_is_rejected_from_config_and_environment(tmp_path: Path) -> None:
    paths = AppPaths(support=tmp_path / "support", logs=tmp_path / "logs")
    paths.support.mkdir()
    paths.config_file.write_text('[service]\nasr_model = "tea-1.1-mlx-16bit"\n')

    with pytest.raises(ValueError, match="asr_model must be one of"):
        ServiceConfig.load(paths, env={})
    with pytest.raises(ValueError, match="asr_model must be one of"):
        ServiceConfig.load(env={"TEA_ASR_MODEL": "tea-1.1-mlx-16bit"})


def test_local_model_verification_accepts_only_exact_pinned_file_set(tmp_path: Path) -> None:
    payload = b"pinned model bytes"
    spec = _local_spec("artifact", payload)
    model_path = tmp_path / spec.local_dir
    model_path.mkdir()
    (model_path / "weights.bin").write_bytes(payload)

    assert model_manager.verify_local_model(spec, tmp_path) == model_path

    (model_path / "weights.bin").write_bytes(b"mutated model bytes")
    with pytest.raises(ModelUnavailableError, match="sha256 mismatch: weights.bin") as caught:
        model_manager.verify_local_model(spec, tmp_path)
    assert caught.value.code == "model_unavailable"
    assert "benchmarks/convert_quant.py --bits 8" in str(caught.value)

    (model_path / "weights.bin").write_bytes(payload)
    (model_path / "unpinned.txt").write_text("not part of the pinned artifact")
    with pytest.raises(ModelUnavailableError, match="unpinned files: unpinned.txt"):
        model_manager.verify_local_model(spec, tmp_path)


def test_missing_or_mutated_local_model_never_uses_huggingface_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_download(**kwargs: object) -> Path:
        raise AssertionError(f"unexpected model download: {kwargs}")

    monkeypatch.setattr(model_manager, "snapshot_download", unexpected_download)
    with pytest.raises(ModelUnavailableError, match="directory is missing"):
        model_manager.locate_prepared_model(TEA_ASR_1_1_MLX_8BIT, tmp_path)

    model_path = tmp_path / TEA_ASR_1_1_MLX_8BIT.local_dir
    model_path.mkdir()
    for name, digest in TEA_ASR_1_1_MLX_8BIT.files:
        path = model_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"wrong bytes")
    with pytest.raises(ModelUnavailableError, match="sha256 mismatch"):
        model_manager.prepare_model(TEA_ASR_1_1_MLX_8BIT, tmp_path)


def test_eight_bit_lock_records_local_pins_provenance_and_versions() -> None:
    lock = json.loads(Path("models.lock.json").read_text(encoding="utf-8"))
    pin = lock["asr"]["variants"][ASR_MODEL_8BIT]
    expected_files = dict(TEA_ASR_1_1_MLX_8BIT.files)
    assert pin["files"] == expected_files
    assert pin["revision"] == TEA_ASR_1_1_MLX_8BIT.revision
    manifest = json.dumps(expected_files, sort_keys=True, separators=(",", ":")).encode()
    assert pin["revision"] == f"sha256:{hashlib.sha256(manifest).hexdigest()}"
    assert pin["source"]["repo_id"] == "JacobLinCool/TEA-ASR-1.1"
    assert pin["source"]["revision"] == TEA_ASR_1_1_MLX_8BIT.source_revision
    assert pin["conversion"]["command"] == (
        "uv run python benchmarks/convert_quant.py --bits 8 --out models/mlx-8bit-selfconv"
    )
    assert pin["mlx"] == TEA_ASR_1_1_MLX_8BIT.mlx_version
    assert pin["mlx_audio"] == TEA_ASR_1_1_MLX_8BIT.mlx_audio_version
    assert pin["mlx_lm"] == TEA_ASR_1_1_MLX_8BIT.mlx_lm_version


def test_doctor_explains_missing_selected_eight_bit_model(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli.ServiceConfig,
        "load",
        lambda *args, **kwargs: ServiceConfig(asr_model=ASR_MODEL_8BIT),
    )

    def missing_model(spec: ModelSpec) -> Path:
        raise ModelUnavailableError(f"local {spec.variant} directory is missing")

    def no_asset() -> Path:
        raise FileNotFoundError("not installed")

    monkeypatch.setattr(cli, "locate_prepared_model", missing_model)
    monkeypatch.setattr(cli, "locate_vad", no_asset)
    monkeypatch.setattr(cli, "locate_yamnet", lambda: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(cli, "_probe_service", lambda _url: {"reachable": False})

    assert cli._doctor("http://127.0.0.1:8327") == 1
    report = json.loads(capsys.readouterr().out)
    assert report["model_variant"] == ASR_MODEL_8BIT
    assert report["model_prepared"] is False
    assert report["model_error"].startswith("model_unavailable:")
    assert "tea-1.1-mlx-8bit" in report["model_error"]


def test_serve_logs_model_unavailable_without_starting_a_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = AppPaths(support=tmp_path / "support", logs=tmp_path / "logs")
    monkeypatch.setattr(AppPaths, "macos_default", classmethod(lambda _cls: paths))
    monkeypatch.setattr(
        cli.ServiceConfig,
        "load",
        lambda *args, **kwargs: ServiceConfig(asr_model=ASR_MODEL_8BIT),
    )

    def unavailable(_spec: ModelSpec) -> Path:
        raise ModelUnavailableError("local 8-bit artifact pin mismatch")

    monkeypatch.setattr(cli, "locate_prepared_model", unavailable)
    monkeypatch.setattr("sys.argv", ["tea-asr", "serve"])

    assert cli.main() == 2
    assert "model_unavailable" in capsys.readouterr().err
    log_line = paths.error_log_file.read_text(encoding="utf-8").splitlines()[-1]
    logged = json.loads(log_line)
    assert logged["message"] == "model.unavailable"
    assert logged["error_code"] == "model_unavailable"
    assert logged["model_variant"] == ASR_MODEL_8BIT
