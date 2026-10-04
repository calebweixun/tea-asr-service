"""Estimate a constant SRT-vs-audio offset by decoding shifted item spans.

For a sample of items, decode the audio at ``start + delta`` for each candidate
``delta`` and score it against the item reference.  The delta with the lowest pooled
CER is the offset to *add* to the SRT times.  Only metrics are written.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from benchmarks.cer_eval import edit_counts, normalize
from benchmarks.gold_offline_eval import SAMPLE_RATE, load_items, read_wav
from tea_asr.backend import TeaMlxBackend


def pick_items(items: list[dict], count: int, min_s: float) -> list[dict]:
    usable = [i for i in items if i["end_s"] - i["start_s"] >= min_s and len(i["reference"]) >= 8]
    if len(usable) <= count:
        return usable
    step = len(usable) / count
    return [usable[int(k * step)] for k in range(count)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--answers", type=Path, required=True)
    parser.add_argument("--wav", type=Path, required=True)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=40)
    parser.add_argument("--min-s", type=float, default=4.0)
    parser.add_argument(
        "--deltas", type=float, nargs="+",
        default=[-4, -3, -2, -1.5, -1, -0.5, 0, 0.5, 1, 1.5, 2, 3, 4],
    )
    args = parser.parse_args(argv)
    samples = read_wav(args.wav)
    items = pick_items(load_items(args.answers), args.count, args.min_s)
    backend = TeaMlxBackend(args.model_path)
    backend.load()
    errors = {d: 0 for d in args.deltas}
    refs = 0
    per_item: list[dict] = []
    try:
        for item in items:
            ref = normalize(item["reference"])
            refs += len(ref)
            row = {"id": item["id"], "cer_by_delta": {}}
            for delta in args.deltas:
                start = round((item["start_s"] + delta) * SAMPLE_RATE)
                end = round((item["end_s"] + delta) * SAMPLE_RATE)
                start = max(0, start)
                end = min(samples.size, end)
                if end - start < SAMPLE_RATE // 2:
                    text = ""
                else:
                    text = str(backend.transcribe(np.ascontiguousarray(samples[start:end])).text)
                counts = edit_counts(ref, normalize(text))
                errors[delta] += counts.errors
                row["cer_by_delta"][str(delta)] = round(counts.errors / max(1, len(ref)), 4)
            per_item.append(row)
    finally:
        backend.close()
    pooled = {str(d): round(errors[d] / max(1, refs), 4) for d in args.deltas}
    best = min(pooled, key=lambda key: pooled[key])
    report = {"items": len(items), "pooled_cer_by_delta": pooled, "best_delta_s": float(best),
              "per_item": per_item}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"{args.answers.name}: best delta {best}s; pooled {pooled}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
