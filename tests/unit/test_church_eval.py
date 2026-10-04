"""Synthetic fixtures for the church evaluation helpers (no private text or audio)."""

import json
import random
from pathlib import Path

import numpy as np
import pytest

from benchmarks.cer_eval import edit_counts
from benchmarks.church_eval_matrix import decode_command, parse_label
from benchmarks.church_eval_report import Scorer
from benchmarks.church_eval_singing import merge, overlap
from benchmarks.church_eval_stream import levenshtein, score_trace
from benchmarks.srt_gold import Cue


def test_levenshtein_matches_the_reference_dp() -> None:
    rng = random.Random(7)
    for _ in range(300):
        a = "".join(rng.choice("abc你好") for _ in range(rng.randint(0, 30)))
        b = "".join(rng.choice("abc你好") for _ in range(rng.randint(0, 30)))
        assert levenshtein(a, b) == edit_counts(a, b).errors


def test_parse_label_variants() -> None:
    assert parse_label("m4-base") == {
        "bits": "4", "carry_s": None, "gap_s": None, "repl": False, "prompt": False,
    }
    spec = parse_label("m8-c3g5-repl-prompt")
    assert spec == {"bits": "8", "carry_s": 3.0, "gap_s": 5.0, "repl": True, "prompt": True}
    assert parse_label("m4-c2")["gap_s"] == 1.5
    with pytest.raises(ValueError):
        parse_label("m5-base")


def test_decode_command_adds_carry_and_dictionary_flags(tmp_path: Path) -> None:
    command = decode_command("m4-c3-repl", "svc", tmp_path, tmp_path / "church.toml")
    assert "--carry-s" in command and command[command.index("--carry-s") + 1] == "3.0"
    assert "--dictionary" in command and "--prompt" not in command
    with pytest.raises(ValueError):
        decode_command("m4-c3-repl", "svc", tmp_path, None)


def test_overlap_and_merge() -> None:
    assert overlap(0, 10, [(2, 4), (8, 12)]) == 4
    assert merge([(5, 6), (0, 2), (1, 3)]) == [(0, 3), (5, 6)]


def test_cluster_bootstrap_delta_detects_a_consistent_improvement() -> None:
    items = [
        {"service": "a", "start_s": 300.0 * k, "id": f"a{k}"} for k in range(40)
    ] + [{"service": "b", "start_s": 300.0 * k, "id": f"b{k}"} for k in range(40)]
    scorer = Scorer(items, iterations=400, seed=1)
    chars = np.full(len(items), 100.0)
    worse = (np.full(len(items), 10.0), chars)
    better = (np.full(len(items), 5.0), chars)
    delta = scorer.delta(better, worse, "pooled")
    assert delta["delta_cer"] == pytest.approx(-0.05)
    assert delta["significant"] is True
    cer = scorer.cer(*worse, "a")
    assert cer["cer"] == pytest.approx(0.1)
    assert cer["ci95_low"] <= 0.1 <= cer["ci95_high"]


def _event(t_ms: float, **event: object) -> dict:
    return {"t_ms": t_ms, "event": event}


def test_score_trace_cer_hides_and_missed_singing() -> None:
    cues = [Cue(1, 10.0, 12.0, "你好"), Cue(2, 12.0, 14.0, "世界")]
    unc = [(100.0, 160.0)]
    rows = [
        _event(0, type="hello"),
        # speech segment 0: captioned, correct
        _event(1000, type="segment.queued", segment_id="s0", start_sample=10 * 16000, end_sample=14 * 16000),
        _event(1500, type="transcript.final", segment_id="s0", start_sample=10 * 16000,
               end_sample=14 * 16000, text="你好世界", audio_class="speech"),
        # a captioned segment wrongly hidden as singing
        _event(2000, type="segment.audio_class", segment_id="s1", **{"class": "singing"}),
        _event(2100, type="segment.queued", segment_id="s1", start_sample=12 * 16000, end_sample=14 * 16000),
        _event(2200, type="transcript.final", segment_id="s1", start_sample=12 * 16000,
               end_sample=14 * 16000, text="世界", audio_class="singing"),
        # captions shown in an uncaptioned span
        _event(3000, type="segment.queued", segment_id="s2", start_sample=110 * 16000, end_sample=120 * 16000),
        _event(3100, type="transcript.final", segment_id="s2", start_sample=110 * 16000,
               end_sample=120 * 16000, text="啦啦啦", audio_class="speech"),
    ]
    meta = {"audio_t0_ms": 0.0}
    # window starts at 0 s so sample positions equal the timeline
    result = score_trace(rows, meta, cues, unc, (0.0, 200.0))
    assert result["speech_segments"] == 2
    assert result["false_hide_final"] == 1 and result["false_hide_ever"] == 1
    assert result["missed_singing_segments"] == 1 and result["missed_singing_chars"] == 3
    # ASR text includes the duplicate "世界" from s1; displayed text drops it
    assert result["cer_displayed"] == 0.0
    assert result["cer_asr"] == pytest.approx(0.5)


def test_answers_roundtrip_is_plain_json(tmp_path: Path) -> None:
    path = tmp_path / "a.json"
    path.write_text(json.dumps({"set": "x", "items": []}))
    assert json.loads(path.read_text())["items"] == []
