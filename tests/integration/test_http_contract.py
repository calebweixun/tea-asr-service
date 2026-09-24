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


def test_status_under_a_fake_backend_does_not_claim_the_real_model(client: TestClient) -> None:
    """docs/06-handoff.md constraint 1: a fake backend must be explicitly
    opted into (it is here, via `create_app(supervisor=FakeSupervisor())` in
    `tests/conftest.py::build_client`) and must show up as such in
    `/v1/status` — it must not report the real model's repo id/revision,
    which would look like the real model had actually loaded."""

    with client as http:
        body = http.get("/v1/status", headers=AUTH).json()
        assert body["model"] != "Alkd/TEA-ASR-1.1-MLX-4bit"
        assert body["model_revision"] != "caee57a908b6d64be08a6462c7a21ececbd4d7cb"
        assert "fake" in body["model"].lower()


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


def test_all_private_use_ranges_are_filtered_and_raw_text_is_preserved(
    supervisor: FakeSupervisor,
) -> None:
    raw = "測試" + "".join(
        chr(codepoint) for codepoint in (0xF0000, 0xFFFFD, 0x100000, 0x10FFFD)
    ) + "文字"
    supervisor.text = raw
    with build_client(supervisor) as http:
        body = http.post(TRANSCRIBE, content=b"\0\0" * 1600, headers=AUTH).json()

    assert body["warnings"] == ["private_use_characters"]
    assert body["text"] == "測試文字"
    assert body["raw_text"] == raw


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


def test_extended_private_use_only_transcript_is_also_empty_after_filter(
    supervisor: FakeSupervisor,
) -> None:
    supervisor.text = "".join(chr(codepoint) for codepoint in (0xF0000, 0x100000))
    with build_client(supervisor) as http:
        body = http.post(TRANSCRIBE, content=b"\0\0" * 1600, headers=AUTH).json()

    assert body["text"] == ""
    assert body["raw_text"] == supervisor.text
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


# --- HTTP Host allowlist (docs/03-architecture.md) ---------------------------
#
# The service only ever binds 127.0.0.1, and the Host header is the DNS-
# rebinding guard for HTTP the way Origin is for WS: a page served from an
# attacker-controlled domain that resolves to 127.0.0.1 must not reach this
# API just because a browser sends a matching Host for that domain.


@pytest.mark.parametrize("host", ["evil.example.com", "127.0.0.1.evil.com", "0.0.0.0"])
def test_requests_with_a_disallowed_host_are_rejected(client: TestClient, host: str) -> None:
    with client as http:
        response = http.get("/healthz", headers={"Host": host})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden_origin"


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_requests_with_an_allowed_host_pass_through(client: TestClient, host: str) -> None:
    with client as http:
        response = http.get("/healthz", headers={"Host": host})
        assert response.status_code == 200


# --- Host header port-stripping (the `[::1]:8327` -> "[" bug) ----------------
#
# `HostValidationMiddleware` used to do `host.split(":")[0]`, which truncates
# a bracketed IPv6 Host down to just "[" instead of stripping the port. That
# rejected every IPv6 Host outright, including `::1` once LAN mode opts it
# in (see tests/integration/test_lan_mode.py for the LAN-mode acceptances).
# These cases are the ones a plain loopback (allow_lan=False) deployment must
# still get right: a `:port` suffix must be stripped, not treated as part of
# the hostname.


@pytest.mark.parametrize("host", ["127.0.0.1:8327", "localhost:8327"])
def test_requests_with_an_allowed_host_and_port_pass_through(
    client: TestClient, host: str
) -> None:
    with client as http:
        response = http.get("/healthz", headers={"Host": host})
        assert response.status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "[::1]:8327",  # bracketed IPv6 + port
        "[::1]",  # bracketed IPv6, no port
    ],
)
def test_ipv6_loopback_host_is_parsed_but_still_rejected_without_lan_mode(
    client: TestClient, host: str
) -> None:
    """`::1` is only on the allowlist once LAN mode opts it in (see
    tests/integration/test_lan_mode.py); this only asserts that the *parse*
    now correctly extracts `::1` (not the pre-fix `"["`) so it is evaluated
    against the allowlist as itself, still ending in the same 403 the
    allowlist has always given a not-yet-allowed host — never widened."""

    with client as http:
        response = http.get("/healthz", headers={"Host": host})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden_origin"


def test_malformed_bracketed_host_is_rejected_not_silently_repaired(
    client: TestClient,
) -> None:
    """A Host header with no closing bracket is not a valid bracketed IPv6
    literal; `parse_host_header` leaves it untouched rather than guessing,
    so it is rejected like any other unrecognized host."""

    with client as http:
        response = http.get("/healthz", headers={"Host": "[::1"})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden_origin"


def test_host_allowlist_also_covers_unauthenticated_health_endpoints(
    client: TestClient,
) -> None:
    """A disallowed Host must be rejected before auth is even considered."""

    with client as http:
        response = http.get("/v1/status", headers={"Host": "evil.example.com", **AUTH})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden_origin"
