"""Punctuation density and stable-commit metrics for a captured stream trace.

Reads a trace written by `soak_real_audio.py capture` (JSONL of wire events with
`t_ms`) and prints: characters per mark, share of finals without a mark, the
longest unpunctuated run per final, how early `transcript.stable` committed
text, the soak analyzer's rewrite/latency numbers and the OBS caption-replay
counters (layout moves, duplicates). Text never leaves the machine; only counts
are printed.

    python benchmarks/punctuation_stream_metrics.py trace.jsonl \
        --server-log service.log --replay-binary .../caption-replay
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from soak_metrics import analyze_trace, parse_replay_output, read_jsonl

MARKS = set("，。？！、,.?!；;：:")
REPLAY_FLAGS = [
    "--fade-delay-ms", "3000", "--fade-ms", "200", "--max-rows", "3", "--max-lines", "3",
    "--width", "1800", "--punct", "comma", "--comma-min", "8", "--quiet",
]  # fmt: skip


def runs_without_marks(text: str) -> list[int]:
    runs: list[int] = []
    current = 0
    for char in text:
        if char in MARKS:
            runs.append(current)
            current = 0
        elif not char.isspace():
            current += 1
    runs.append(current)
    return runs


def density(texts: list[str]) -> dict[str, Any]:
    chars = sum(sum(1 for c in t if c not in MARKS and not c.isspace()) for t in texts)
    marks = sum(sum(1 for c in t if c in MARKS) for t in texts)
    closing = sum(1 for t in texts if t and t[-1] in MARKS)
    inner_marks = marks - closing
    without = sum(1 for t in texts if sum(1 for c in t[:-1] if c in MARKS) == 0)
    longest = sorted(max(runs_without_marks(t)) for t in texts)
    return {
        "finals": len(texts),
        "chars": chars,
        "marks": marks,
        "chars_per_mark": round(chars / max(marks, 1), 1),
        "chars_per_inner_mark": round(chars / max(inner_marks, 1), 1),
        "pct_without_inner_mark": round(100 * without / max(len(texts), 1), 1),
        "longest_run_median": statistics.median(longest) if longest else 0,
        "longest_run_p90": longest[int(0.9 * (len(longest) - 1))] if longest else 0,
    }


def stable_lead(rows: list[dict[str, Any]]) -> dict[str, float]:
    """How early committed text appeared relative to the segment's final."""

    finals: dict[str, tuple[float, str]] = {}
    stables: dict[str, list[tuple[float, str, str]]] = {}
    for row in rows:
        event = row.get("event", {})
        kind = event.get("type")
        if kind == "transcript.final":
            finals[event["segment_id"]] = (row["t_ms"] / 1000.0, event["text"])
        elif kind == "transcript.stable":
            stables.setdefault(event["segment_id"], []).append(
                (row["t_ms"] / 1000.0, event["text"], event["state"])
            )
    weighted = total = committed = final_chars = 0.0
    for segment, (final_time, final_text) in finals.items():
        previous = 0
        before_final = 0
        for when, text, state in stables.get(segment, []):
            if state == "open":
                weighted += max(0.0, final_time - when) * (len(text) - previous)
                total += len(text) - previous
                before_final = len(text)
            previous = len(text)
        committed += min(before_final, len(final_text))
        final_chars += len(final_text)
    return {
        "lead_s": round(weighted / max(total, 1), 3),
        "pre_final_share": round(committed / max(final_chars, 1), 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--server-log", type=Path)
    parser.add_argument("--replay-binary", type=Path)
    args = parser.parse_args()
    rows = read_jsonl(args.trace)
    finals = [
        row["event"]["text"]
        for row in rows
        if row.get("event", {}).get("type") == "transcript.final" and row["event"]["text"]
    ]
    analysis = analyze_trace(args.trace, None, args.server_log)["overall"]
    out: dict[str, Any] = {
        "density": density(finals),
        "restored_finals": sum(
            "punctuation_restored" in row.get("event", {}).get("warnings", [])
            for row in rows
            if row.get("event", {}).get("type") == "transcript.final"
        ),
        "stable": stable_lead(rows),
        "analysis": {
            key: analysis.get(key)
            for key in (
                "segments",
                "speech_to_first_partial_ms",
                "stable_gap_ms",
                "final_latency_ms",
                "partial_rewrite",
                "stable_closes",
                "worker_busy_fraction",
            )
        },
    }
    if args.replay_binary:
        result = subprocess.run(
            [str(args.replay_binary), str(args.trace), *REPLAY_FLAGS],
            check=True,
            capture_output=True,
            text=True,
            errors="replace",
        )
        replay, _closes = parse_replay_output(result.stdout, rows)
        out["replay"] = {
            key: replay.get(key)
            for key in (
                "layout_moves",
                "duplication_lines",
                "duplication_total_lines",
                "tail_retractions",
                "tail_rewrites",
                "row_limit_losses",
                "mid_speech_fade_outs",
                "largest_burst",
            )
        }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
