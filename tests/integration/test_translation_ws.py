"""Opt-in translation over `/v1/stream` with a fake provider (no model).

What matters here is the contract, not translation quality: translation rides
on new `translation.*` events, never edits a `transcript.final`, never makes
ASR wait, and says so when it cannot run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tea_asr.api.app import create_app
from tea_asr.config import ServiceConfig, validate_translation_or_raise
from tea_asr.errors import ApiError
from tea_asr.translation.supervisor import TranslationSupervisor
from tests.conftest import AUTH, FakeSupervisor
from tests.integration.test_ws_contract import START, drain_until, send_audio

TRANSLATE = {"direction": "zh2en", "latency_mode": "native"}


class FakeTranslation:
    """Same surface as `TranslationSupervisor`, answers instantly or slowly."""

    model = "netease-youdao/Confucius4-T3PO"
    model_revision = "test-revision"

    def __init__(self, *, state: str = "ready", delay_s: float = 0.0) -> None:
        self.state = state
        self.last_error: str | None = None
        self.generation = 1
        self.task_timeout_s = 20.0
        self.delay_s = delay_s
        self.owner: object | None = None
        self.texts: list[str] = []
        self.sessions: list[tuple[str, str]] = []

    def start_in_background(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def availability_error(self) -> ApiError | None:
        if self.state != "ready":
            return ApiError(
                "translation_unavailable", self.last_error or self.state, retryable=False
            )
        if self.owner is not None:
            return ApiError("translation_unavailable", "busy", retryable=True)
        return None

    def try_acquire(self, owner: object) -> bool:
        if self.owner is not None and self.owner is not owner:
            return False
        self.owner = owner
        return True

    def release(self, owner: object) -> None:
        if self.owner is owner:
            self.owner = None

    async def start_session(self, direction: str, latency_mode: str) -> int:
        self.sessions.append((direction, latency_mode))
        return self.generation

    async def translate(self, text: str, *, force: bool) -> dict[str, Any]:
        self.texts.append(text)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        return {
            "action": "TRANS",
            "source": text,
            "text": f"EN({text})",
            "forced": force,
            "inference_ms": 5,
        }


def build(translation: Any, **config: Any) -> TestClient:
    app = create_app(
        Path("unused"),
        token="test-token",
        supervisor=FakeSupervisor(),
        config=ServiceConfig(revisable_preview=False, **config),
        vad_model=None,
        translation_provider=translation,
    )
    return TestClient(app, base_url="http://127.0.0.1")


def commit(socket: Any, request_id: str, first_seq: int) -> None:
    send_audio(socket, 10, first_seq=first_seq)
    socket.send_json(
        {"type": "audio.commit", "request_id": request_id, "through_seq": first_seq + 9}
    )


def test_finals_are_translated_as_new_events_and_left_untouched() -> None:
    translation = FakeTranslation()
    with build(translation) as http, http.websocket_connect("/v1/stream", headers=AUTH) as socket:
        socket.receive_json()
        socket.send_json({**START, "translation": TRANSLATE})
        started = socket.receive_json()
        assert started["type"] == "session.started"
        assert "translation" not in started  # the existing event is not extended
        announced = socket.receive_json()
        assert announced["type"] == "translation.started"
        assert announced["direction"] == "zh2en"
        assert announced["latency_mode"] == "native"
        assert announced["request_id"] == "start-1"
        commit(socket, "c1", 0)
        events = drain_until(socket, "translation.segment")
        commit(socket, "c2", 10)
        events += drain_until(socket, "translation.segment")
        socket.send_json({"type": "session.stop", "request_id": "s", "through_seq": 19})
        events += drain_until(socket, "session.stopped")

    finals = [e for e in events if e["type"] == "transcript.final"]
    pieces = [e for e in events if e["type"] == "translation.segment"]
    assert [f["text"] for f in finals] == ["測試文字", "測試文字"]
    assert [f["raw_text"] for f in finals] == ["測試文字", "測試文字"]
    assert [(f["start_sample"], f["end_sample"]) for f in finals] == [(0, 16000), (16000, 32000)]
    assert [p["source_segment_ids"] for p in pieces] == [[f["segment_id"]] for f in finals]
    assert [p["translation_index"] for p in pieces] == [0, 1]
    assert all(p["text"] == "EN(測試文字)" for p in pieces)
    # A translation never arrives before the final it translates.
    order = [e["type"] for e in events]
    assert order.index("transcript.final") < order.index("translation.segment")
    assert translation.sessions == [("zh2en", "native")]
    assert translation.owner is None  # released for the next session


def test_slow_translation_never_holds_up_asr_finals() -> None:
    translation = FakeTranslation(delay_s=0.5)
    with build(translation) as http, http.websocket_connect("/v1/stream", headers=AUTH) as socket:
        socket.receive_json()
        socket.send_json({**START, "translation": TRANSLATE})
        socket.receive_json()
        socket.receive_json()
        seen: list[dict[str, Any]] = []
        for index in range(3):
            commit(socket, f"c{index}", index * 10)
            seen += drain_until(socket, "transcript.final")
        # All three finals arrived while the first translation was still asleep.
        assert not any(e["type"] == "translation.segment" for e in seen)
        socket.send_json({"type": "session.stop", "request_id": "s", "through_seq": 29})
        rest = drain_until(socket, "session.stopped")
    pieces = [e for e in rest if e["type"] == "translation.segment"]
    covered = [segment for piece in pieces for segment in piece["source_segment_ids"]]
    finals = [e["segment_id"] for e in seen if e["type"] == "transcript.final"]
    assert covered == finals  # nothing lost, in order, possibly coalesced
    assert [e["type"] for e in rest][-1] == "session.stopped"


def test_translation_requested_but_disabled_is_refused() -> None:
    with build(None) as http, http.websocket_connect("/v1/stream", headers=AUTH) as socket:
        socket.receive_json()
        socket.send_json({**START, "translation": TRANSLATE})
        error = socket.receive_json()
    assert error["type"] == "error"
    assert error["code"] == "unsupported_option"


@pytest.mark.parametrize("direction", ["en2zh", "ja2zh"])
def test_unverified_direction_is_refused(direction: str) -> None:
    with build(FakeTranslation()) as http, http.websocket_connect(
        "/v1/stream", headers=AUTH
    ) as socket:
        socket.receive_json()
        socket.send_json({**START, "translation": {"direction": direction}})
        error = socket.receive_json()
    assert error["code"] == "unsupported_option"


def test_unplugged_model_disk_fails_loudly_and_asr_still_works(tmp_path: Path) -> None:
    missing = tmp_path / "DigiFusion" / "t3po-mlx-4bit"
    translation = TranslationSupervisor(missing, max_memory_gib=12)
    with build(translation) as http:
        for _ in range(100):
            if translation.state == "failed":
                break
            asyncio.run(asyncio.sleep(0.01))
        body = http.get("/v1/capabilities", headers=AUTH).json()
        assert body["features"]["translation"] is False
        assert body["translation"]["state"] == "failed"
        assert str(missing) in body["translation"]["last_error"]
        assert body["translation"]["directions"] == ["zh2en"]

        with pytest.raises(WebSocketDisconnect) as closed, http.websocket_connect(
            "/v1/stream", headers=AUTH
        ) as socket:
            socket.receive_json()
            socket.send_json({**START, "translation": TRANSLATE})
            error = socket.receive_json()
            assert error["code"] == "translation_unavailable"
            assert error["retryable"] is False
            assert "外接 SSD" in error["message"]
            socket.receive_json()
        assert closed.value.code == 4503

        # The same server keeps doing ASR for sessions that did not ask.
        with http.websocket_connect("/v1/stream", headers=AUTH) as socket:
            socket.receive_json()
            socket.send_json(START)
            socket.receive_json()
            commit(socket, "c1", 0)
            events = drain_until(socket, "transcript.final")
        assert events[-1]["text"] == "測試文字"


def test_only_one_session_translates_at_a_time() -> None:
    translation = FakeTranslation()
    with build(translation) as http, http.websocket_connect(
        "/v1/stream", headers=AUTH
    ) as first:
        first.receive_json()
        first.send_json({**START, "translation": TRANSLATE})
        first.receive_json()
        first.receive_json()
        with http.websocket_connect("/v1/stream", headers=AUTH) as second:
            second.receive_json()
            second.send_json({**START, "translation": TRANSLATE})
            error = second.receive_json()
        assert error["code"] == "translation_unavailable"
        assert error["retryable"] is True


def test_capabilities_describe_a_ready_provider() -> None:
    with build(FakeTranslation()) as http:
        body = http.get("/v1/capabilities", headers=AUTH).json()
    assert body["features"]["translation"] is True
    assert body["translation"] == {
        "state": "ready",
        "model": "netease-youdao/Confucius4-T3PO",
        "model_revision": "test-revision",
        "directions": ["zh2en"],
        "latency_modes": ["low", "native", "high"],
        "max_sessions": 1,
        "max_pending_segments": 16,
        "request_timeout_ms": 20000,
    }


def test_cancel_stops_translation_events() -> None:
    translation = FakeTranslation(delay_s=0.3)
    with build(translation) as http, http.websocket_connect("/v1/stream", headers=AUTH) as socket:
        socket.receive_json()
        socket.send_json({**START, "translation": TRANSLATE})
        socket.receive_json()
        socket.receive_json()
        commit(socket, "c1", 0)
        drain_until(socket, "transcript.final")
        socket.send_json({"type": "session.cancel", "request_id": "x"})
        events = drain_until(socket, "session.cancelled")
        with pytest.raises(WebSocketDisconnect):
            while True:
                events.append(socket.receive_json())
    assert not any(e["type"].startswith("translation.") for e in events)


def test_enabled_without_a_model_path_refuses_to_start() -> None:
    with pytest.raises(RuntimeError, match="translation_model_path"):
        validate_translation_or_raise(ServiceConfig(translation_enabled=True))
    with pytest.raises(RuntimeError, match="translation_model_path"):
        create_app(
            Path("unused"),
            token="t",
            supervisor=FakeSupervisor(),
            config=ServiceConfig(translation_enabled=True),
            vad_model=None,
        )


def test_env_switches_translation_on() -> None:
    config = ServiceConfig.from_env(
        {"TEA_ASR_TRANSLATION": "1", "TEA_ASR_TRANSLATION_MODEL_PATH": " /Volumes/X/t3po "}
    )
    assert config.translation_enabled is True
    assert config.translation_model_path == "/Volumes/X/t3po"
    assert ServiceConfig.from_env({}).translation_enabled is False
