from __future__ import annotations

import json
from pathlib import Path

from benchmarks.soak_metrics import (
    analyze_trace,
    duplicate_segments_from_trace,
    has_new_repeated_run,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "soak"


def test_analyze_trace_summarises_timing_errors_sections_and_logs() -> None:
    result = analyze_trace(
        FIXTURES / "synthetic.jsonl",
        FIXTURES / "sections.json",
        FIXTURES / "synthetic.server.jsonl",
    )

    first = result["sections"][0]["metrics"]
    assert first["segments"] == {"count": 1, "queued": 1, "final": 1, "skipped": 0, "errors": 0}
    assert first["segment_duration_s"]["median"] == 2.0
    assert first["speech_to_first_partial_ms"]["median"] == 400.0
    assert first["stable_gap_ms"]["median"] == 700.5
    assert first["final_latency_ms"]["p95"] == 200.0
    assert first["longest_speech_no_text_ms"] == 699.0
    assert first["partial_rewrite"] == {"rewrites": 1, "transitions": 1, "rate": 1.0}
    assert first["stable_closes"] == {"diverged": 1, "abandoned": 0}
    assert first["worker_busy_fraction"] == 0.25
    assert first["warnings_by_message"] == {"stream.audio_quiet": 1}
    assert result["overall"]["errors_by_code"] == {"invalid_ipc": 1}
    assert result["sections"][1]["metrics"]["errors_by_code"] == {}


def test_duplication_detector_uses_normalised_non_overlapping_runs() -> None:
    assert has_new_repeated_run("中華民國，中華民國", "中華民國")
    assert not has_new_repeated_run("中華民國，中華民國", "中華民國中華民國")
    assert has_new_repeated_run("Fullwidth ＡＢＣＤ ＡＢＣＤ", "abcd")

    rows = [
        {"event": {"type": "transcript.partial", "segment_id": "a", "text": "中華民國中華民國"}},
        {"event": {"type": "transcript.final", "segment_id": "a", "text": "中華民國台灣"}},
        {"event": {"type": "transcript.partial", "segment_id": "b", "text": "中華民國中華民國"}},
        {"event": {"type": "transcript.final", "segment_id": "b", "text": "中華民國中華民國"}},
    ]
    assert duplicate_segments_from_trace(rows) == ["a"]


def test_jsonl_fixture_has_no_external_audio_fields() -> None:
    rows = [json.loads(line) for line in (FIXTURES / "synthetic.jsonl").read_text().splitlines()]
    assert rows[0]["event"]["type"] == "hello"
