from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tea_asr.api.app import create_app
from tea_asr.config import ServiceConfig

AUTH = {"Authorization": "Bearer test-token"}
_DEFAULTS = ServiceConfig()


class FakeSupervisor:
    """Test double for the MLX worker.

    docs/06 forbids a production fallback to a fake backend, so this lives in
    tests only and is injected explicitly through `create_app`.
    """

    def __init__(self, *, text: str = "測試文字", state: str = "ready") -> None:
        self.state = state
        self.last_error: str | None = None
        self.load_ms = 10
        self.generation = 1
        self.text = text
        self.calls = 0
        self.system_prompts: list[str] = []
        self.failure: Exception | None = None
        self.delay_s = 0.0

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def transcribe(
        self,
        pcm: bytes,
        *,
        language: str = "Chinese",
        system_prompt: str | None = None,
    ) -> dict[str, Any]:
        self.calls += 1
        if system_prompt is not None:
            self.system_prompts.append(system_prompt)
        if self.delay_s:
            import asyncio

            await asyncio.sleep(self.delay_s)
        if self.failure is not None:
            raise self.failure
        return {
            "status": "ok",
            "text": self.text,
            "audio_samples": len(pcm) // 2,
            "model_input_samples": max(16_000, len(pcm) // 2),
            "total_time_s": 0.01,
            "prompt_tokens": 10,
            "generation_tokens": 3,
        }


class FakeVad:
    """Deterministic stand-in for Silero.

    Any window with real energy counts as speech, so segmentation tests do not
    depend on a downloaded ONNX asset or on model behaviour.
    """

    def probability(self, window: np.ndarray, session: Any) -> float:
        return 1.0 if float(np.abs(window).max()) > 0.05 else 0.0


class FakeYamnet:
    """Deterministic YAMNet stand-in: loud frames look like worship singing.

    A frame is "loud" when its patch has real energy (a tone); quiet frames
    read as speech. No ONNX file is needed, so CI never touches the asset.
    """

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls: list[int] = []
        self.fail = fail

    def scores(self, waveform: np.ndarray) -> np.ndarray:
        from tea_asr.yamnet import HOP_SAMPLES, NUM_CLASSES, PATCH_SAMPLES, complete_frames

        if self.fail is not None:
            raise self.fail
        count = complete_frames(waveform.size)
        self.calls.append(count)
        out = np.zeros((count, NUM_CLASSES), dtype=np.float32)
        for index in range(count):
            patch = waveform[index * HOP_SAMPLES : index * HOP_SAMPLES + PATCH_SAMPLES]
            if float(np.abs(patch).max()) > 0.05:
                out[index, FAKE_MUSIC] = 0.9
                out[index, FAKE_SINGING] = 0.1
            else:
                out[index, FAKE_SPEECH] = 0.95
        return out


FAKE_SPEECH, FAKE_MUSIC, FAKE_SINGING = 0, 132, 24


def fake_class_names() -> list[str]:
    from tea_asr.singing import MUSIC_CLASSES, SPEECH_CLASSES, VOCAL_CLASSES
    from tea_asr.yamnet import NUM_CLASSES

    names = [f"class-{index}" for index in range(NUM_CLASSES)]
    for offset, name in enumerate(SPEECH_CLASSES):
        names[FAKE_SPEECH + offset] = name
    names[FAKE_MUSIC] = MUSIC_CLASSES[0]
    for offset, name in enumerate(VOCAL_CLASSES):
        names[FAKE_SINGING + offset] = name
    return names


def fake_singing_runtime(model: Any | None = None) -> Any:
    from tea_asr.singing import ClassGroups
    from tea_asr.singing_session import SingingRuntime

    return SingingRuntime(model or FakeYamnet(), ClassGroups.from_names(fake_class_names()))


def build_client(
    supervisor: FakeSupervisor,
    *,
    revisable_preview: bool = False,
    filter_pua: bool = True,
    vad: Any = None,
    max_continuous_sessions: int = 1,
    max_total_connections: int = 4,
    allow_lan: bool = False,
    dictionary_remote_edit: bool = False,
    extra_allowed_hosts: tuple[str, ...] = (),
    rate_limiter: Any = None,
    paths: Any = None,
    log_backup_count: int = 3,
    preview_min_interval_ms: int = _DEFAULTS.preview_min_interval_ms,
    preview_min_audio_ms: int = _DEFAULTS.preview_min_audio_ms,
    preview_load_factor: float = _DEFAULTS.preview_load_factor,
    debug_capture_audio: bool = False,
    context_hints_enabled: bool = False,
    context_prompt_enabled: bool = False,
    singing_runtime: Any = None,
    punctuation_runtime: Any = None,
    punctuation_restore_enabled: bool = False,
    carry_context_s: float = _DEFAULTS.carry_context_s,
    carry_context_max_gap_s: float = _DEFAULTS.carry_context_max_gap_s,
) -> TestClient:
    app = create_app(
        Path("unused"),
        token="test-token",
        supervisor=supervisor,
        config=ServiceConfig(
            revisable_preview=revisable_preview,
            filter_pua=filter_pua,
            max_continuous_sessions=max_continuous_sessions,
            max_total_connections=max_total_connections,
            allow_lan=allow_lan,
            dictionary_remote_edit=dictionary_remote_edit,
            extra_allowed_hosts=extra_allowed_hosts,
            log_backup_count=log_backup_count,
            preview_min_interval_ms=preview_min_interval_ms,
            preview_min_audio_ms=preview_min_audio_ms,
            preview_load_factor=preview_load_factor,
            debug_capture_audio=debug_capture_audio,
            context_hints_enabled=context_hints_enabled,
            context_prompt_enabled=context_prompt_enabled,
            carry_context_s=carry_context_s,
            carry_context_max_gap_s=carry_context_max_gap_s,
            punctuation_restore_enabled=punctuation_restore_enabled,
        ),
        vad_model=vad,
        rate_limiter=rate_limiter,
        paths=paths,
        # Hermetic: never load the real YAMNet asset unless a test injects a runtime.
        singing_runtime=singing_runtime,
        punctuation_runtime=punctuation_runtime,
    )
    # The service only ever binds 127.0.0.1 (docs/03-architecture.md), and
    # HostValidationMiddleware enforces that Host allowlist on every HTTP
    # request; TestClient's default base_url ("http://testserver") would fail
    # it, so tests use a real allowed host instead of special-casing "testserver".
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def supervisor() -> FakeSupervisor:
    return FakeSupervisor()


@pytest.fixture
def client(supervisor: FakeSupervisor) -> TestClient:
    return build_client(supervisor)


@pytest.fixture
def continuous_client(supervisor: FakeSupervisor) -> TestClient:
    return build_client(supervisor, vad=FakeVad())


@pytest.fixture
def preview_client(supervisor: FakeSupervisor) -> TestClient:
    return build_client(supervisor, revisable_preview=True)
