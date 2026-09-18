from __future__ import annotations

import pytest

from tea_asr import cli


def test_probe_reports_an_unreachable_service_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "_token", lambda: "t" * 32)

    def refuse(*args: object, **kwargs: object) -> dict:
        raise OSError("Connection refused")

    monkeypatch.setattr(cli, "_request", refuse)
    result = cli._probe_service("http://127.0.0.1:1")
    assert result["reachable"] is False
    assert "Connection refused" in result["reason"]


def test_probe_without_a_token_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing() -> str:
        raise FileNotFoundError("no token")

    monkeypatch.setattr(cli, "_token", missing)
    result = cli._probe_service("http://127.0.0.1:8327")
    assert result["reachable"] is False
    assert "token" in result["reason"]


def test_probe_skips_inference_when_the_model_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "_token", lambda: "t" * 32)
    calls: list[str] = []

    def fake_request(url: str, *, data: bytes | None = None, headers: dict) -> dict:
        calls.append(url)
        if url.endswith("/v1/status"):
            return {"model_state": "idle_unloaded", "worker_generation": 2, "queue": {}}
        return {"protocol_version": "1.1", "profiles": ["utterance"], "features": {}}

    monkeypatch.setattr(cli, "_request", fake_request)
    result = cli._probe_service("http://127.0.0.1:8327")
    assert result["model_state"] == "idle_unloaded"
    # Probing would have woken the model just to answer a health question.
    assert result["one_second_probe"] == {}
    assert not any("transcriptions" in url for url in calls)
