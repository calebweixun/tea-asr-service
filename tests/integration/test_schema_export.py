from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tea_asr.cli import _export_schemas
from tea_asr.wire import ws_event_schema

API_DOCS = Path("docs/api")


def test_exported_schemas_match_the_code(tmp_path: Path) -> None:
    _export_schemas(tmp_path)
    for name in ("openapi.json", "ws-events.schema.json"):
        checked_in = (API_DOCS / name).read_text()
        assert checked_in == (tmp_path / name).read_text(), (
            f"docs/api/{name} is stale; run `uv run tea-asr export-schemas`"
        )


def test_openapi_documents_the_error_envelope() -> None:
    document = json.loads((API_DOCS / "openapi.json").read_text())
    assert "ErrorEnvelope" in document["components"]["schemas"]
    assert set(document["paths"]) == {
        "/healthz",
        "/readyz",
        "/v1/capabilities",
        "/v1/status",
        "/v1/logs",
        "/v1/transcriptions",
    }


@pytest.mark.parametrize(
    "event",
    [
        "AudioAck",
        "AudioCommitted",
        "SegmentQueued",
        "SegmentSkipped",
        "SegmentError",
        "TranscriptFinal",
        "TranscriptPartial",
        "FlowControl",
        "PreviewStatus",
        "SessionStopped",
        "SessionCancelled",
        "Pong",
        "ErrorEvent",
    ],
)
def test_every_documented_server_event_has_a_schema(event: str) -> None:
    assert event in ws_event_schema()["$defs"]["models"]


def test_v0_2_endpoints_are_absent_not_faked(client: TestClient) -> None:
    with client as http:
        assert http.post("/v1/jobs").status_code == 404
