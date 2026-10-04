from __future__ import annotations

import json
from pathlib import Path

from benchmarks.soak_metrics import (
    analyze_trace,
    duplicate_segments_from_trace,
    hard_failures,
    has_new_repeated_run,
    parse_replay_output,
    soft_failures,
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
    assert not has_new_repeated_run("ABCD ABCD", "abce,abce")

    rows = [
        {"event": {"type": "transcript.partial", "segment_id": "a", "text": "中華民國中華民國"}},
        {"event": {"type": "transcript.final", "segment_id": "a", "text": "中華民國台灣"}},
        {"event": {"type": "transcript.partial", "segment_id": "b", "text": "中華民國中華民國"}},
        {"event": {"type": "transcript.final", "segment_id": "b", "text": "中華民國中華民國"}},
    ]
    assert duplicate_segments_from_trace(rows) == ["a"]


def test_replay_duplication_lines_are_classified_with_fuzzy_segment_text() -> None:
    rows = [
        {"event": {"type": "transcript.partial", "segment_id": "plugin", "text": "one line"}},
        {"event": {"type": "transcript.final", "segment_id": "plugin", "text": "final1"}},
        {"event": {"type": "transcript.partial", "segment_id": "model", "text": "ABCDABCD"}},
        {"event": {"type": "transcript.final", "segment_id": "model", "text": "final2"}},
        {"event": {"type": "transcript.partial", "segment_id": "speech", "text": "final3"}},
        {"event": {"type": "transcript.final", "segment_id": "speech", "text": "abceabce"}},
    ]
    output = (
        "# duplication: lines that showed a repeated 4+ character run their final does not have=3\n"
        "#   dup: ABCDABCD  (final: final1)\n"
        "#   dup: ABCDABCD  (final: final2)\n"
        "#   dup: ＡＢＣＤ，ＡＢＣＤ  (final: abceabce)\n"
    )

    metrics, _ = parse_replay_output(output, rows)

    assert metrics["duplication_total_lines"] == 3
    assert metrics["duplication_lines"] == 1
    assert metrics["duplication_model_lines"] == 1
    assert metrics["duplication_speech_lines"] == 1
    assert metrics["duplication_unmapped_lines"] == 0
    analysis = {
        "overall": {
            "stable_gap_ms": {"p95": 0},
            "final_latency_ms": {"p95": 0},
        },
        "sections": [{"name": "all", "metrics": {}}],
    }
    replay = {
        "overall": metrics,
        "sections": [{"name": "all", **metrics}],
    }
    assert "overall: duplication_lines=1" in hard_failures(analysis, replay)
    replay["overall"]["duplication_lines"] = 0
    replay["sections"][0]["duplication_lines"] = 0
    assert hard_failures(analysis, replay) == []
    soft = soft_failures(analysis, replay)
    assert "overall: model_origin_duplication_lines=1 (soft)" in soft
    assert "overall: speech_origin_duplication_lines=1 (soft)" in soft


def test_unmapped_replay_dup_line_stays_a_hard_plugin_failure() -> None:
    output = (
        "# duplication: lines that showed a repeated 4+ character run their final does not have=1\n"
        "#   dup: ABCDABCD  (final: final1)\n"
    )

    metrics, _ = parse_replay_output(output, [])

    assert metrics["duplication_lines"] == 1
    assert metrics["duplication_unmapped_lines"] == 1


def test_jsonl_fixture_has_no_external_audio_fields() -> None:
    rows = [json.loads(line) for line in (FIXTURES / "synthetic.jsonl").read_text().splitlines()]
    assert rows[0]["event"]["type"] == "hello"
