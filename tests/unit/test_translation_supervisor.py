from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tea_asr.errors import ApiError
from tea_asr.translation.supervisor import TranslationSupervisor

FAKE_WORKER = "tests.fake_translation_worker"


def model_dir(tmp_path: Path) -> Path:
    path = tmp_path / "t3po-mlx-4bit"
    path.mkdir()
    (path / "config.json").write_text("{}")
    return path


def supervisor(path: Path, **kwargs: float) -> TranslationSupervisor:
    return TranslationSupervisor(
        path, max_memory_gib=12, worker_module=FAKE_WORKER, **kwargs  # type: ignore[arg-type]
    )


def test_missing_model_path_fails_loudly_and_names_the_disk(tmp_path: Path) -> None:
    missing = tmp_path / "unplugged" / "t3po-mlx-4bit"
    translation = supervisor(missing)

    async def scenario() -> ApiError:
        with pytest.raises(ApiError) as caught:
            await translation.start()
        return caught.value

    error = asyncio.run(scenario())
    assert error.code == "translation_unavailable"
    assert error.retryable is False
    assert str(missing) in error.message
    assert "外接 SSD" in error.message
    assert translation.state == "failed"
    assert translation.last_error == error.message
    assert translation.process is None
    unavailable = translation.availability_error()
    assert unavailable is not None and unavailable.retryable is False


def test_folder_without_config_is_not_mistaken_for_a_model(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    translation = supervisor(empty)
    with pytest.raises(ApiError):
        asyncio.run(translation.start())
    assert "config.json" in (translation.last_error or "")


def test_worker_load_failure_is_reported_not_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_T3PO", "refuse")
    translation = supervisor(model_dir(tmp_path))
    with pytest.raises(ApiError):
        asyncio.run(translation.start())
    assert translation.state == "failed"
    assert translation.last_error == "記憶體超過上限"


def test_ready_worker_translates_and_admits_one_session(tmp_path: Path) -> None:
    translation = supervisor(model_dir(tmp_path))

    async def scenario() -> dict:
        await translation.start()
        await translation.start_session("zh2en", "native")
        result = await translation.translate("你好", force=True)
        await translation.close()
        return result

    result = asyncio.run(scenario())
    assert result["text"] == "EN(你好)"
    first, second = object(), object()
    translation.state = "ready"
    assert translation.try_acquire(first)
    assert translation.availability_error() is not None
    assert not translation.try_acquire(second)
    translation.release(first)
    assert translation.availability_error() is None
    assert translation.try_acquire(second)


def test_hung_worker_times_out_and_is_restarted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_T3PO", "hang")
    monkeypatch.setattr("tea_asr.translation.supervisor.RESTART_BACKOFF_S", (0.01, 0.01, 0.01))
    translation = supervisor(model_dir(tmp_path), task_timeout_s=0.3)

    async def scenario() -> tuple[ApiError, int]:
        await translation.start()
        await translation.start_session("zh2en", "native")
        with pytest.raises(ApiError) as caught:
            await translation.translate("你好", force=True)
        for _ in range(200):
            if translation.state == "ready":
                break
            await asyncio.sleep(0.02)
        generation = translation.generation
        await translation.close()
        return caught.value, generation

    error, generation = asyncio.run(scenario())
    assert error.code == "translation_timeout"
    assert error.retryable is True
    assert generation == 2


def test_crashed_worker_is_reported_as_failed_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_T3PO", "crash")
    monkeypatch.setattr("tea_asr.translation.supervisor.RESTART_BACKOFF_S", (0.01, 0.01, 0.01))
    translation = supervisor(model_dir(tmp_path))

    async def scenario() -> ApiError:
        await translation.start()
        await translation.start_session("zh2en", "native")
        with pytest.raises(ApiError) as caught:
            await translation.translate("你好", force=True)
        await translation.close()
        return caught.value

    assert asyncio.run(scenario()).code == "translation_failed"


def test_missing_disk_during_restart_stops_retrying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_T3PO", "crash")
    monkeypatch.setattr("tea_asr.translation.supervisor.RESTART_BACKOFF_S", (0.01, 0.01, 0.01))
    path = model_dir(tmp_path)
    translation = supervisor(path)

    async def scenario() -> None:
        await translation.start()
        await translation.start_session("zh2en", "native")
        (path / "config.json").unlink()
        path.rmdir()  # the SSD went away
        with pytest.raises(ApiError):
            await translation.translate("你好", force=True)
        for _ in range(200):
            if translation.state == "failed":
                break
            await asyncio.sleep(0.02)
        await translation.close()

    asyncio.run(scenario())
    assert translation.state == "failed"
    assert "外接 SSD" in (translation.last_error or "")
