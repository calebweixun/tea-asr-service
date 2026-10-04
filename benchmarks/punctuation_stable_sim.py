"""Replay a saved trace's partials through the stable-prefix tracker with and
without punctuation restoration, to pick the partial tail margin.

For each segment the recorded partials are fed in order to
`StablePrefixTracker` (as `stream.py` does, after restoration), then the final
closes it. Reported per configuration:

* ``lead_s``: mean seconds a committed character was on screen before the final
  (higher = commits earlier);
* ``pre_final``: share of final characters already committed before the final;
* ``diverged``: segments whose committed text the final did not extend;
* ``blocked``: partials that arrived while the last hypotheses no longer
  started with the committed text (the tracker had to wait).

    python benchmarks/punctuation_stable_sim.py --model <model.onnx> --trace trace.jsonl
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from tea_asr.punctuation import PunctuationModel
from tea_asr.punctuation import hold_back_tail as hold_back
from tea_asr.stable import StablePrefixTracker


def load_segments(trace: Path) -> list[dict[str, Any]]:
    segments: dict[str, dict[str, Any]] = defaultdict(lambda: {"partials": [], "final": None})
    order: list[str] = []
    for line in trace.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        event = row["event"]
        kind = event.get("type")
        if kind not in {"transcript.partial", "transcript.final"}:
            continue
        key = event["segment_id"]
        if key not in segments:
            order.append(key)
        if kind == "transcript.partial":
            segments[key]["partials"].append((row["t_ms"] / 1000.0, event["text"]))
        else:
            segments[key]["final"] = (row["t_ms"] / 1000.0, event["text"], event.get("raw_text"))
    return [segments[key] for key in order if segments[key]["final"]]


def simulate(
    segments: list[dict[str, Any]],
    model: PunctuationModel | None,
    margin: int,
    agreement: int,
    holdback: int = 0,
) -> dict[str, Any]:
    lead_weighted = 0.0
    lead_chars = 0
    pre_final = final_chars = 0
    diverged = 0
    blocked = 0
    for segment in segments:
        tracker = StablePrefixTracker(agreement)
        commits: list[tuple[float, int]] = []
        previous = 0
        for when, text in segment["partials"]:
            if model is not None:
                text = model.restore(text, tail_margin=margin).text
            text = hold_back(text, holdback)
            if tracker.text and tracker._history and not text.startswith(tracker.text):
                blocked += 1
            update = tracker.observe(text)
            if update is not None:
                commits.append((when, len(update.text) - previous))
                previous = len(update.text)
        final_time, final_text, _raw = segment["final"]
        if model is not None:
            final_text = model.restore(final_text, terminal=True).text
        committed_before = len(tracker.text)
        update = tracker.finalize(final_text)
        diverged += update.state == "diverged"
        final_chars += len(final_text)
        pre_final += min(committed_before, len(final_text))
        for when, grown in commits:
            lead_weighted += max(0.0, final_time - when) * grown
            lead_chars += grown
    return {
        "segments": len(segments),
        "lead_s": round(lead_weighted / max(lead_chars, 1), 3),
        "pre_final": round(pre_final / max(final_chars, 1), 3),
        "diverged": diverged,
        "blocked": blocked,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--margins", type=int, nargs="*", default=[0, 2, 4, 6, 8])
    parser.add_argument("--agreement", type=int, default=2)
    args = parser.parse_args()
    model = PunctuationModel(args.model)
    segments = load_segments(args.trace)
    out = {"off": simulate(segments, None, 0, args.agreement)}
    for margin in args.margins:
        out[f"margin_{margin}"] = simulate(segments, model, margin, args.agreement)
        out[f"margin_{margin}_holdback_{margin}"] = simulate(
            segments, model, margin, args.agreement, holdback=margin
        )
    out["off_holdback_4"] = simulate(segments, None, 0, args.agreement, holdback=4)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
