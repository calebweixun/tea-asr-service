"""Score event traces for the preview-cadence study (docs/benchmarks/preview-cadence-report.md).

Inputs per run, all written by the capture harness:

- `<trace>.jsonl` + `<trace>.meta.json` from `capture_event_trace.py capture`
- the WAV's `speech.manifest.json` (clip sample ranges and reference text)
- optionally `<run>.timing.jsonl` from `serve_timed.py`: one row per worker
  call, `{kind, session, t0, t1, samples}` on the server's monotonic clock

Metrics (per trace):

a0. time to first text: speech.started -> the segment's first partial
a. tail latency: clip audio start -> first partial of the clip's segment whose
   normalized text contains the clip's first two reference characters
b. commit latency: clip audio end -> first `transcript.stable` of that segment
   containing the clip's last two reference characters; if no stable ever
   does (a misrecognized ending), the segment's `transcript.final` instead
c. characters appended per stable event and per growing partial
d. worker busy fraction (preview + final decode time / wall span) and preview
   decode time distribution, from the timing log
e. final latency: segment end_sample -> transcript.final. The segmenter's
   end_sample sits near the end of speech, so this includes waiting for the
   end silence; `e_queued_to_final_ms` (segment.queued -> final) is the part
   the preview cadence can actually delay
f. rewrites: partials that do not start with the stable text already
   committed for their segment, and diverged segments

A clip maps to the segment whose final covers the clip's midpoint; clips with
no such final (dropped by the model, or skipped) are counted, not scored.
Two characters rather than one, because a single Chinese character recurs too
often to identify a clip.

    python benchmarks/preview_cadence_eval.py --manifest speech.manifest.json \\
        --run baseline=traces/cadence-baseline.jsonl:traces/cadence-baseline.timing.jsonl \\
        --out traces/cadence-summary.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import unicodedata
from pathlib import Path
from typing import Any

SAMPLES_PER_MS = 16


def normalize(text: str) -> str:
    return "".join(
        char.lower() for char in text if unicodedata.category(char)[0] in {"L", "N"}
    )


def dist(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "median": None, "p95": None, "max": None}
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    return {
        "n": len(values),
        "median": round(statistics.median(values), 1),
        "p95": round(p95, 1),
        "max": round(max(values), 1),
    }


def load_trace(path: Path) -> tuple[list[tuple[float, dict[str, Any]]], dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
    return [(row["t_ms"], row["event"]) for row in rows], meta


def score_trace(
    path: Path, manifest: dict[str, Any], timing: list[dict[str, Any]] | None
) -> dict[str, Any]:
    events, meta = load_trace(path)
    t0 = float(meta["audio_t0_ms"])

    def audio_time(sample: int) -> float:
        return t0 + sample / SAMPLES_PER_MS

    started = next(e for _, e in events if e["type"] == "session.started")
    session_id = started["session_id"]
    finals = {e["segment_id"]: (t, e) for t, e in events if e["type"] == "transcript.final"}
    partials: dict[str, list[tuple[float, dict[str, Any]]]] = {}
    stables: dict[str, list[tuple[float, dict[str, Any]]]] = {}
    for t, e in events:
        if e["type"] == "transcript.partial":
            partials.setdefault(e["segment_id"], []).append((t, e))
        elif e["type"] == "transcript.stable":
            stables.setdefault(e["segment_id"], []).append((t, e))

    # Time to first text: speech.started -> the segment's first partial,
    # whatever it says. Recognition accuracy does not enter this one.
    spoken = {e["segment_id"]: t for t, e in events if e["type"] == "speech.started"}
    first_text = [
        rows[0][0] - spoken[sid] for sid, rows in partials.items() if sid in spoken
    ]

    tail: list[float] = []
    commit: list[float] = []
    commit_fallback = 0
    unmapped = 0
    tail_missing = 0
    for clip in manifest["clips"]:
        ref = normalize(clip["reference"])
        mid = (clip["start_sample"] + clip["end_sample"]) // 2
        segment_id = next(
            (
                sid
                for sid, (_, final) in finals.items()
                if final["start_sample"] <= mid < final["end_sample"]
            ),
            None,
        )
        if segment_id is None or len(ref) < 2:
            unmapped += 1
            continue
        head, end = ref[:2], ref[-2:]
        first = next(
            (
                t
                for t, e in partials.get(segment_id, [])
                if e["end_sample"] > clip["start_sample"] and head in normalize(e["text"])
            ),
            None,
        )
        if first is None:
            tail_missing += 1
        else:
            tail.append(first - audio_time(clip["start_sample"]))
        covered = next(
            (
                t
                for t, e in stables.get(segment_id, [])
                if e["end_sample"] >= clip["start_sample"] and end in normalize(e["text"])
            ),
            None,
        )
        if covered is None:
            commit_fallback += 1
            covered = finals[segment_id][0]
        commit.append(covered - audio_time(clip["end_sample"]))

    stable_append: list[float] = []
    for rows in stables.values():
        previous = ""
        for _, e in rows:
            stable_append.append(len(e["text"]) - len(previous))
            previous = e["text"]
    partial_growth: list[float] = []
    for rows in partials.values():
        previous = ""
        for _, e in rows:
            grown = len(e["text"]) - len(previous)
            if grown > 0:
                partial_growth.append(grown)
            previous = e["text"]

    final_latency = [t - audio_time(e["end_sample"]) for t, e in finals.values()]
    queued_at = {e["segment_id"]: t for t, e in events if e["type"] == "segment.queued"}
    queue_to_final = [t - queued_at[sid] for sid, (t, _) in finals.items() if sid in queued_at]

    committed: dict[str, str] = {}
    rewrites = 0
    n_partials = 0
    diverged: set[str] = set()
    for _, e in events:
        if e["type"] == "transcript.stable":
            committed[e["segment_id"]] = e["text"]
            if e["state"] == "diverged":
                diverged.add(e["segment_id"])
        elif e["type"] == "transcript.partial":
            n_partials += 1
            if not e["text"].startswith(committed.get(e["segment_id"], "")):
                rewrites += 1

    result: dict[str, Any] = {
        "trace": str(path),
        "session_id": session_id,
        "preview_policy": started.get("preview_policy"),
        "segments_final": len(finals),
        "clips_unmapped": unmapped,
        "a0_first_text_after_speech_started_ms": dist(first_text),
        "a_tail_latency_ms": {**dist(tail), "missing": tail_missing},
        "b_commit_latency_ms": {**dist(commit), "fell_back_to_final": commit_fallback},
        "c_stable_append_chars": dist(stable_append),
        "c_partial_growth_chars": dist(partial_growth),
        "stable_events": sum(len(rows) for rows in stables.values()),
        "e_final_latency_ms": dist(final_latency),
        "e_queued_to_final_ms": dist(queue_to_final),
        "f_partials": n_partials,
        "f_partials_rewriting_stable": rewrites,
        "f_rewrite_rate": round(rewrites / n_partials, 4) if n_partials else None,
        "f_diverged_segments": len(diverged),
    }
    if timing is not None:
        result["d_worker"] = worker_load(timing, session_id)
    return result


def worker_load(rows: list[dict[str, Any]], session_id: str | None) -> dict[str, Any]:
    """Busy fraction over the span from the first to the last worker call."""

    mine = [row for row in rows if session_id is None or row["session"] == session_id]
    if not mine:
        return {}
    span = max(row["t1"] for row in mine) - min(row["t0"] for row in mine)
    preview = [row["t1"] - row["t0"] for row in mine if row["kind"] == "preview"]
    final = [row["t1"] - row["t0"] for row in mine if row["kind"] != "preview"]
    return {
        "span_s": round(span, 1),
        "busy_fraction": round((sum(preview) + sum(final)) / span, 3),
        "preview_busy_fraction": round(sum(preview) / span, 3),
        "previews": len(preview),
        "finals": len(final),
        "preview_decode_ms": dist([value * 1000 for value in preview]),
        "final_decode_ms": dist([value * 1000 for value in final]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--run",
        action="append",
        required=True,
        help="name=trace.jsonl[,trace2.jsonl][:timing.jsonl]",
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    runs: dict[str, Any] = {}
    for spec in args.run:
        name, _, rest = spec.partition("=")
        traces, _, timing_path = rest.partition(":")
        timing = (
            [json.loads(line) for line in Path(timing_path).read_text().splitlines() if line]
            if timing_path
            else None
        )
        sessions = [score_trace(Path(p), manifest, timing) for p in traces.split(",")]
        runs[name] = {"sessions": sessions}
        if timing is not None:
            runs[name]["worker_all_sessions"] = worker_load(timing, None)
    text = json.dumps(runs, ensure_ascii=False, indent=2)
    if args.out is not None:
        args.out.write_text(text + "\n", encoding="utf-8")
    header = (
        "| run | a tail med/p95 | b commit med/p95 (final fallback) | c stable append "
        "med/p95/max | c partial growth med/p95/max | d busy (preview) | d preview decode "
        "med/p95/max | e final med/p95 | e queued->final med/p95 | f rewrites / partials "
        "| diverged |"
    )
    print(header)
    print("|" + "---|" * 11)
    for name, run in runs.items():
        for s in run["sessions"]:
            a, b = s["a_tail_latency_ms"], s["b_commit_latency_ms"]
            ca, cp = s["c_stable_append_chars"], s["c_partial_growth_chars"]
            e, d = s["e_final_latency_ms"], s.get("d_worker", {})
            q = s["e_queued_to_final_ms"]
            pd = d.get("preview_decode_ms", {})
            print(
                f"| {name} | {a['median']} / {a['p95']} | {b['median']} / {b['p95']}"
                f" ({b['fell_back_to_final']}) | {ca['median']} / {ca['p95']} / {ca['max']}"
                f" | {cp['median']} / {cp['p95']} / {cp['max']}"
                f" | {d.get('busy_fraction')} ({d.get('preview_busy_fraction')})"
                f" | {pd.get('median')} / {pd.get('p95')} / {pd.get('max')}"
                f" | {e['median']} / {e['p95']} | {q['median']} / {q['p95']}"
                f" | {s['f_partials_rewriting_stable']} / {s['f_partials']}"
                f" | {s['f_diverged_segments']} |"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
