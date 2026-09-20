from __future__ import annotations

import pytest
from starlette.websockets import WebSocketDisconnect

from tea_asr.rate_limit import AuthRateLimiter
from tests.conftest import AUTH, FakeSupervisor, build_client


def test_default_mode_still_rejects_a_lan_origin(supervisor: FakeSupervisor) -> None:
    """Acceptance #4: default WS behaviour is unchanged."""

    with (
        build_client(supervisor) as http,
        pytest.raises(WebSocketDisconnect) as caught,
        http.websocket_connect(
            "/v1/stream", headers={**AUTH, "Origin": "http://192.168.1.5"}
        ) as socket,
    ):
        socket.receive_json()
    assert caught.value.code == 1008


def test_lan_mode_accepts_a_trusted_lan_origin(supervisor: FakeSupervisor) -> None:
    with (
        build_client(supervisor, allow_lan=True) as http,
        http.websocket_connect(
            "/v1/stream", headers={**AUTH, "Origin": "http://192.168.1.5"}
        ) as socket,
    ):
        hello = socket.receive_json()
        assert hello["type"] == "hello"


def test_lan_mode_still_rejects_a_public_origin(supervisor: FakeSupervisor) -> None:
    with (
        build_client(supervisor, allow_lan=True) as http,
        pytest.raises(WebSocketDisconnect) as caught,
        http.websocket_connect(
            "/v1/stream", headers={**AUTH, "Origin": "http://evil.example.com"}
        ) as socket,
    ):
        socket.receive_json()
    assert caught.value.code == 1008


def test_lan_mode_marks_the_ws_accept_with_an_insecure_warning(
    supervisor: FakeSupervisor,
) -> None:
    with (
        build_client(supervisor, allow_lan=True) as http,
        http.websocket_connect("/v1/stream", headers=AUTH) as socket,
    ):
        socket.receive_json()
        headers = dict(socket.extra_headers or [])
        assert headers.get(b"x-tea-asr-security") == b"unencrypted-lan-mode"


def test_default_mode_does_not_mark_the_ws_accept(supervisor: FakeSupervisor) -> None:
    with (
        build_client(supervisor) as http,
        http.websocket_connect("/v1/stream", headers=AUTH) as socket,
    ):
        socket.receive_json()
        headers = dict(socket.extra_headers or [])
        assert b"x-tea-asr-security" not in headers


def test_repeated_ws_auth_failures_are_rate_limited(supervisor: FakeSupervisor) -> None:
    limiter = AuthRateLimiter(max_failures=2, window_s=60.0)
    with build_client(supervisor, rate_limiter=limiter) as http:
        for _ in range(2):
            with (
                pytest.raises(WebSocketDisconnect) as caught,
                http.websocket_connect(
                    "/v1/stream", headers={"Authorization": "Bearer wrong"}
                ) as socket,
            ):
                socket.receive_json()
            assert caught.value.code == 1008

        with (
            pytest.raises(WebSocketDisconnect) as caught,
            http.websocket_connect(
                "/v1/stream", headers={"Authorization": "Bearer wrong"}
            ) as socket,
        ):
            socket.receive_json()
        assert caught.value.code == 1013

        # Even the correct token is refused while the source is throttled.
        with (
            pytest.raises(WebSocketDisconnect) as caught,
            http.websocket_connect("/v1/stream", headers=AUTH) as socket,
        ):
            socket.receive_json()
        assert caught.value.code == 1013
