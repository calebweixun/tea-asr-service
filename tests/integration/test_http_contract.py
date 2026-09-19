from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tea_asr.worker.supervisor import WorkerError
from tests.conftest import AUTH, FakeSupervisor, build_client

TRANSCRIBE = "/v1/transcriptions?sample_rate=16000&channels=1&format=pcm_s16le"


def test_health_does_not_require_auth(client: TestClient) -> None:
    with client as http:
        assert http.get("/healthz").json() == {"status": "ok"}


def test_readyz_reports_model_state() -> None:
    with build_client(FakeSupervisor(state="loading")) as http:
        response = http.get("/readyz")
        assert response.status_code == 503
        assert response.json() == {"status": "loading"}


def test_status_requires_auth(client: TestClient) -> None:
    with client as http:
        unauthorized = http.get("/v1/status")
        assert unauthorized.status_code == 401
        assert unauthorized.json()["error"]["code"] == "unauthenticated"
        body = http.get("/v1/status", headers=AUTH).json()
        assert body["model_state"] == "ready"
        assert body["queue"]["waiting_tasks"] == 0


def test_capabilities_do_not_claim_unverified_features(client: TestClient) -> None:
    with client as http:
        body = http.get("/v1/capabilities", headers=AUTH).json()
        assert body["protocol_version"] == "1.0"
        assert body["features"]["partial_transcripts"] is False
        assert body["features"]["durable_sessions"] is False
        assert body["profiles"] == ["utterance"]
        assert set(body["limits"]) == {
            "max_frame_pcm_bytes",
            "max_utterance_ms",
            "max_continuous_sessions",
            "max_total_connections",
        }


def test_capabilities_declare_1_1_only_when_preview_is_enabled(
    preview_client: TestClient,
) -> None:
    with preview_client as http:
        body = http.get("/v1/capabilities", headers=AUTH).json()
        assert body["protocol_version"] == "1.1"
        assert body["features"]["partial_transcripts"] is True


def test_transcription_contract(client: TestClient) -> None:
    with client as http:
        response = http.post(
            TRANSCRIBE, content=b"\0\0" * 1600, headers={**AUTH, "X-Request-ID": "r1"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["request_id"] == "r1"
        assert body["segments"][0]["end_sample"] == 1600
        assert body["segments"][0]["timestamp_quality"] == "segment"
        assert body["audio_ms"] == 100
        assert body["warnings"] == []
        assert body["queue_ms"] >= 0


def test_private_use_characters_are_filtered_by_default(supervisor: FakeSupervisor) -> None:
    # docs/benchmarks/pua-bf16-ab-report.md: PUA is inserted between correct
    # text by MLX 4bit quantization, not a real character; filtering it is
    # the default so clients do not have to do it themselves.
    supervisor.text = "測試文字"
    with build_client(supervisor) as http:
        body = http.post(TRANSCRIBE, content=b"\0\0" * 1600, headers=AUTH).json()
        assert body["warnings"] == ["private_use_characters"]
        assert body["text"] == "測試文字"
        assert body["raw_text"] == "測試文字"
        assert "" not in body["text"]


def test_private_use_filter_can_be_disabled(supervisor: FakeSupervisor) -> None:
    supervisor.text = "測試文字"
    with build_client(supervisor, filter_pua=False) as http:
        body = http.post(TRANSCRIBE, content=b"\0\0" * 1600, headers=AUTH).json()
        assert body["warnings"] == ["private_use_characters"]
        assert body["text"] == "測試文字"
        assert body["raw_text"] == body["text"]


def test_transcript_entirely_made_of_pua_is_reported_empty_not_no_speech(
    supervisor: FakeSupervisor,
) -> None:
    supervisor.text = ""
    with build_client(supervisor) as http:
        body = http.post(TRANSCRIBE, content=b"\0\0" * 1600, headers=AUTH).json()
        assert body["text"] == ""
        assert body["raw_text"] == ""
        assert body["segments"] == []
        assert body["warnings"] == ["private_use_characters", "empty_after_filter"]


def test_silence_returns_no_speech(supervisor: FakeSupervisor) -> None:
    supervisor.text = ""
    with build_client(supervisor) as http:
        body = http.post(TRANSCRIBE, content=b"\0\0" * 1600, headers=AUTH).json()
        assert body["text"] == ""
        assert body["segments"] == []
        assert body["warnings"] == ["no_speech"]


@pytest.mark.parametrize(
    ("query", "content", "status", "code"),
    [
        ("?sample_rate=48000&channels=1&format=pcm_s16le", b"\0\0", 422, "invalid_audio"),
        ("?sample_rate=16000&channels=2&format=pcm_s16le", b"\0\0", 422, "invalid_audio"),
        ("?sample_rate=16000&channels=1&format=wav", b"\0\0", 422, "invalid_audio"),
        ("?sample_rate=16000&channels=1&format=pcm_s16le", b"", 422, "invalid_audio"),
        ("?sample_rate=16000&channels=1&format=pcm_s16le", b"\0", 422, "invalid_audio"),
        (
            "?sample_rate=16000&channels=1&format=pcm_s16le",
            b"\0\0" * 480_001,
            413,
            "payload_too_large",
        ),
    ],
)
def test_rejected_requests_use_the_error_envelope(
    client: TestClient, query: str, content: bytes, status: int, code: str
) -> None:
    with client as http:
        response = http.post(f"/v1/transcriptions{query}", content=content, headers=AUTH)
        assert response.status_code == status
        error = response.json()["error"]
        assert error["code"] == code
        assert set(error) == {"code", "message", "retryable", "request_id"}


def test_missing_query_parameters_use_the_error_envelope(client: TestClient) -> None:
    with client as http:
        response = http.post("/v1/transcriptions", content=b"\0\0", headers=AUTH)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_audio"


def test_model_failure_is_not_masked_as_success(supervisor: FakeSupervisor) -> None:
    supervisor.failure = WorkerError("inference_failed", "worker exploded")
    with build_client(supervisor) as http:
        response = http.post(TRANSCRIBE, content=b"\0\0" * 1600, headers=AUTH)
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "inference_failed"


def test_requests_are_refused_while_the_model_is_not_ready() -> None:
    with build_client(FakeSupervisor(state="loading")) as http:
        response = http.post(TRANSCRIBE, content=b"\0\0" * 1600, headers=AUTH)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "model_loading"
