from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient

from tea_asr.rate_limit import AuthRateLimiter
from tests.conftest import AUTH, FakeSupervisor, build_client


@contextmanager
def _capture_records(logger_name: str) -> Iterator[list[logging.LogRecord]]:
    """Capture records on a specific logger directly.

    Not `caplog`: `tea_asr.logs.setup_logging` sets `propagate = False` on the
    `tea_asr` logger (by design — it owns its own rotating-file handler,
    docs/03), and once any earlier test in the same process has called it,
    `caplog`'s root-logger handler stops seeing `tea_asr.*` records for the
    rest of the session. Attaching a handler directly to the logger sidesteps
    that entirely.
    """

    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger(logger_name)
    handler = _Collector(level=logging.INFO)
    logger.addHandler(handler)
    original_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)


def test_lan_mode_logs_an_insecure_warning_on_startup(supervisor: FakeSupervisor) -> None:
    with (
        _capture_records("tea_asr.api") as records,
        build_client(supervisor, allow_lan=True) as http,
    ):
        http.get("/healthz")
    assert any(record.getMessage() == "service.insecure_lan_bind" for record in records)


def test_default_mode_does_not_log_the_insecure_warning(supervisor: FakeSupervisor) -> None:
    with _capture_records("tea_asr.api") as records, build_client(supervisor) as http:
        http.get("/healthz")
    assert not any(record.getMessage() == "service.insecure_lan_bind" for record in records)


def test_default_mode_still_rejects_a_private_lan_host(supervisor: FakeSupervisor) -> None:
    """Acceptance #4: default behaviour is unchanged — LAN IPs are not special."""

    with build_client(supervisor) as http:
        response = http.get("/healthz", headers={"Host": "192.168.1.5"})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden_origin"


def test_lan_mode_accepts_a_trusted_lan_host(supervisor: FakeSupervisor) -> None:
    with build_client(supervisor, allow_lan=True) as http:
        response = http.get("/healthz", headers={"Host": "192.168.1.5"})
        assert response.status_code == 200


def test_lan_mode_still_rejects_a_public_host(supervisor: FakeSupervisor) -> None:
    """Opting into LAN mode must not turn into "accept any Host"."""

    with build_client(supervisor, allow_lan=True) as http:
        response = http.get("/healthz", headers={"Host": "evil.example.com"})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden_origin"


def test_lan_mode_accepts_an_explicitly_configured_extra_host(supervisor: FakeSupervisor) -> None:
    with build_client(
        supervisor, allow_lan=True, extra_allowed_hosts=("mymac.tailnet.ts.net",)
    ) as http:
        response = http.get("/healthz", headers={"Host": "mymac.tailnet.ts.net"})
        assert response.status_code == 200


def test_lan_mode_stamps_responses_with_an_insecure_warning_header(
    supervisor: FakeSupervisor,
) -> None:
    with build_client(supervisor, allow_lan=True) as http:
        response = http.get("/v1/status", headers=AUTH)
        assert response.status_code == 200
        assert response.headers["x-tea-asr-security"] == "unencrypted-lan-mode"
        assert "cleartext" in response.headers["x-tea-asr-security-notice"]


def test_default_mode_does_not_stamp_the_insecure_warning_header(client: TestClient) -> None:
    with client as http:
        response = http.get("/v1/status", headers=AUTH)
        assert response.status_code == 200
        assert "x-tea-asr-security" not in response.headers


def test_repeated_auth_failures_are_rate_limited(supervisor: FakeSupervisor) -> None:
    limiter = AuthRateLimiter(max_failures=3, window_s=60.0)
    with build_client(supervisor, rate_limiter=limiter) as http:
        for _ in range(3):
            failed = http.get("/v1/status", headers={"Authorization": "Bearer wrong"})
            assert failed.status_code == 401

        limited = http.get("/v1/status", headers={"Authorization": "Bearer wrong"})
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "rate_limited"

        # Even the *correct* token is refused while the source is throttled —
        # otherwise the limiter would not actually stop a guesser who
        # eventually stumbles onto the right value.
        still_limited = http.get("/v1/status", headers=AUTH)
        assert still_limited.status_code == 429


def test_a_correct_request_clears_the_failure_count(supervisor: FakeSupervisor) -> None:
    limiter = AuthRateLimiter(max_failures=2, window_s=60.0)
    with build_client(supervisor, rate_limiter=limiter) as http:
        failed = http.get("/v1/status", headers={"Authorization": "Bearer wrong"})
        assert failed.status_code == 401
        ok = http.get("/v1/status", headers=AUTH)
        assert ok.status_code == 200
        # Budget should be back at 2 failures, not 1, since success reset it.
        failed_again = http.get("/v1/status", headers={"Authorization": "Bearer wrong"})
        assert failed_again.status_code == 401
        still_ok = http.get("/v1/status", headers=AUTH)
        assert still_ok.status_code == 200
