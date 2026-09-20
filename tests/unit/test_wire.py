from __future__ import annotations

import pytest
from pydantic import ValidationError

from tea_asr.wire import (
    ClientEnvelope,
    SessionStart,
    is_trusted_lan_address,
    make_host_allowlist,
    make_origin_allowlist,
    ws_event_schema,
)


def _parse(payload: dict[str, object]) -> object:
    return ClientEnvelope.model_validate({"event": payload}).event


def test_session_start_defaults_to_final_only() -> None:
    start = _parse(
        {
            "type": "session.start",
            "request_id": "s1",
            "profile": "utterance",
            "audio": {"sample_rate": 16_000, "channels": 1, "format": "pcm_s16le"},
        }
    )
    assert isinstance(start, SessionStart)
    assert start.transcript_mode == "final_only"
    assert start.durable is False


def test_unknown_client_field_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _parse(
            {
                "type": "session.start",
                "request_id": "s1",
                "profile": "utterance",
                "audio": {"sample_rate": 16_000, "channels": 1, "format": "pcm_s16le"},
                "hotwords": ["TEA-ASR"],
            }
        )


def test_unsupported_audio_format_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _parse(
            {
                "type": "session.start",
                "request_id": "s1",
                "profile": "utterance",
                "audio": {"sample_rate": 48_000, "channels": 1, "format": "pcm_s16le"},
            }
        )


def test_schema_covers_every_wire_event() -> None:
    models = ws_event_schema()["$defs"]["models"]
    for name in ("AudioAck", "SegmentSkipped", "SegmentError", "TranscriptPartial", "Pong"):
        assert name in models


# --- W9: LAN/Tailscale host & origin allowlists ------------------------------


@pytest.mark.parametrize(
    "address",
    ["192.168.1.5", "10.0.0.7", "172.20.3.4", "100.90.1.1", "127.0.0.1", "::1"],
)
def test_trusted_lan_addresses_are_recognized(address: str) -> None:
    assert is_trusted_lan_address(address) is True


@pytest.mark.parametrize("address", ["8.8.8.8", "1.2.3.4", "203.0.113.5", "localhost"])
def test_public_addresses_and_hostnames_are_not_trusted_lan_addresses(address: str) -> None:
    assert is_trusted_lan_address(address) is False


def test_host_allowlist_default_matches_the_historic_loopback_exact_match() -> None:
    is_allowed = make_host_allowlist(allow_lan=False)
    assert is_allowed("127.0.0.1") is True
    assert is_allowed("localhost") is True
    assert is_allowed("192.168.1.5") is False
    assert is_allowed("evil.example.com") is False
    assert is_allowed("0.0.0.0") is False


def test_host_allowlist_widens_to_trusted_lan_addresses_once_opted_in() -> None:
    is_allowed = make_host_allowlist(allow_lan=True)
    assert is_allowed("127.0.0.1") is True
    assert is_allowed("192.168.1.5") is True
    assert is_allowed("100.90.1.1") is True  # Tailscale CGNAT range
    # LAN mode is still a *narrower* allowlist than "anything", not "any Host":
    assert is_allowed("evil.example.com") is False
    assert is_allowed("8.8.8.8") is False


def test_host_allowlist_accepts_explicit_extra_hosts_only_when_opted_in() -> None:
    hostname = "mymac.tailnet.ts.net"
    closed = make_host_allowlist(allow_lan=False, extra_hosts=frozenset({hostname}))
    assert closed(hostname) is False
    opened = make_host_allowlist(allow_lan=True, extra_hosts=frozenset({hostname}))
    assert opened(hostname) is True
    assert opened(hostname.upper()) is True  # case-insensitive
    assert opened("other.tailnet.ts.net") is False


def test_origin_allowlist_default_matches_the_historic_exact_match() -> None:
    is_allowed = make_origin_allowlist(allow_lan=False)
    assert is_allowed("http://127.0.0.1") is True
    assert is_allowed("http://localhost") is True
    assert is_allowed("http://localhost:3000") is False  # exact match only, as before
    assert is_allowed("http://192.168.1.5") is False


def test_origin_allowlist_widens_to_trusted_lan_addresses_once_opted_in() -> None:
    is_allowed = make_origin_allowlist(allow_lan=True)
    assert is_allowed("http://192.168.1.5") is True
    assert is_allowed("http://192.168.1.5:5173") is True
    assert is_allowed("http://100.90.1.1") is True
    assert is_allowed("http://evil.example.com") is False


def test_origin_allowlist_rejects_https_even_in_lan_mode() -> None:
    """W9 ships without TLS; an `https://` Origin claiming to be this service is a lie."""

    is_allowed = make_origin_allowlist(allow_lan=True)
    assert is_allowed("https://192.168.1.5") is False
