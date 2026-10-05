"""Replay saved partial/final event sequences through legacy and current trackers.

The tool prints aggregate measurements only. It does not print transcript text
or write the private trace contents to disk.

Example (paths are supplied by the operator):

    PYTHONPATH=src python benchmarks/stable_trace_replay.py \\
      --trace <punctuation-off.jsonl> --trace <short-off.jsonl> \\
      --punctuation-on <punctuation-on.jsonl>
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Literal

from tea_asr.punctuation import DEFAULT_PARTIAL_TAIL_MARGIN, hold_back_tail
from tea_asr.stable import (
    StablePrefixTracker,
    StableUpdate,
    _alignment_end,
    _comparison_view,
    common_prefix_length,
    comparison_key,
    is_grapheme_boundary,
    is_safe_cut,
    stable_cut,
)


@dataclass(frozen=True, slots=True)
class ReplayEvent:
    kind: Literal["partial", "final"]
    time_s: float
    text: str


@dataclass(slots=True)
class ReplaySegment:
    partials: list[ReplayEvent] = field(default_factory=list)
    final: ReplayEvent | None = None


class LegacyStablePrefixTracker:
    """Pre-change surface-text tracker retained as a replay reference."""

    def __init__(self, agreement: int = 2) -> None:
        self.agreement = agreement
        self._history: deque[str] = deque(maxlen=agreement)
        self.text = ""
        self.closed = False

    def observe(self, hypothesis: str) -> StableUpdate | None:
        if self.closed:
            return None
        self._history.append(hypothesis)
        if len(self._history) < self.agreement:
            return None
        hypotheses = tuple(self._history)
        floor = len(self.text)
        if any(not text.startswith(self.text) for text in hypotheses):
            return None
        length = stable_cut(hypotheses, floor)
        if length <= floor:
            return None
        self.text = hypotheses[0][:length]
        return StableUpdate(self.text, "open")

    def finalize(self, final: str) -> StableUpdate:
        self.closed = True
        committed = self.text
        if final.startswith(committed):
            self.text = final
            return StableUpdate(final, "final")
        shared = common_prefix_length((committed, final))
        end = _alignment_end(committed, final)
        while end < len(final) and not is_grapheme_boundary(final, end):
            end += 1
        self.text = committed + final[end:]
        return StableUpdate(self.text, "diverged", diverged_chars=len(committed) - shared)


def load_segments(path: Path) -> list[ReplaySegment]:
    segments: dict[str, ReplaySegment] = {}
    order: list[str] = []
    with path.open(encoding="utf-8") as source:
        for line in source:
            row = json.loads(line)
            event = row.get("event", {})
            kind = event.get("type")
            if kind not in {"transcript.partial", "transcript.final"}:
                continue
            segment_id = str(event["segment_id"])
            if segment_id not in segments:
                order.append(segment_id)
                segments[segment_id] = ReplaySegment()
            replay_event = ReplayEvent(
                "partial" if kind == "transcript.partial" else "final",
                float(row["t_ms"]) / 1000.0,
                str(event.get("text", "")),
            )
            segment = segments[segment_id]
            if replay_event.kind == "partial":
                segment.partials.append(replay_event)
            else:
                segment.final = replay_event
    return [segments[key] for key in order if segments[key].final is not None]


def strip_punctuation(text: str) -> str:
    return "".join(
        char for char in text if not unicodedata.category(char).startswith("P")
    )


def _prepare_partial(text: str, *, punctuation_on: bool, strip: bool) -> str:
    if strip:
        text = strip_punctuation(text)
    if punctuation_on:
        # Match stream.py: restored partials keep the punctuation model's
        # right-context margin before stable-prefix tracking.
        text = hold_back_tail(text, DEFAULT_PARTIAL_TAIL_MARGIN)
    return text


def _punctuation_count(text: str) -> int:
    return sum(unicodedata.category(char).startswith("P") for char in text)


def _percentiles(values: list[float]) -> tuple[float, float, float]:
    if not values:
        return 0.0, 0.0, 0.0
    ordered = sorted(values)
    p90 = ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)]
    return statistics.median(ordered), p90, ordered[-1]


def _head_escape_candidate(
    recent: list[str], committed: str, *, max_head: int = 2, min_tail: int = 2
) -> tuple[str, int] | None:
    if len(recent) < 3:
        return None
    keys = [comparison_key(text) for text in recent]
    base = comparison_key(committed)
    if any(not key.startswith(base) for key in keys):
        return None
    for head_length in range(1, max_head + 1):
        head_start = len(base)
        heads = [key[head_start : head_start + head_length] for key in keys]
        tails = [key[head_start + head_length :] for key in keys]
        if not heads[0] or len(set(heads)) == 1:
            continue
        tail_length = common_prefix_length(tuple(tails))
        if tail_length < min_tail:
            continue
        candidate_length = head_start + head_length + tail_length
        for text in recent:
            view = _comparison_view(text)
            offset = view.boundaries[candidate_length]
            if offset is None or not is_safe_cut((text,), offset):
                break
        else:
            return keys[-1][:candidate_length], candidate_length
    return None


def _head_escape_metrics(
    segment: ReplaySegment,
    *,
    punctuation_on: bool,
    strip: bool,
    timeout_s: float = 3.0,
    agreement: int = 2,
    history_size: int = 3,
) -> tuple[int, int, int, list[float]]:
    if segment.final is None or not segment.partials:
        return 0, 0, 0, []
    tracker = StablePrefixTracker(agreement)
    recent: deque[str] = deque(maxlen=history_size)
    growths: list[tuple[float, str]] = []
    last_growth = segment.partials[0].time_s
    escape: tuple[float, str, int, str] | None = None
    for event in segment.partials:
        text = _prepare_partial(
            event.text, punctuation_on=punctuation_on, strip=strip
        )
        recent.append(text)
        update = tracker.observe(text)
        if update is not None:
            last_growth = event.time_s
            growths.append((event.time_s, comparison_key(update.text)))
            continue
        if escape is not None:
            continue
        if event.time_s - last_growth < timeout_s:
            continue
        candidate = _head_escape_candidate(list(recent), tracker.text)
        if candidate is not None:
            escape = (
                event.time_s,
                candidate[0],
                candidate[1],
                comparison_key(tracker.text),
            )
    if escape is None:
        return 0, 0, 0, []

    trigger, candidate_key, _candidate_length, base_key = escape
    final_key = comparison_key(segment.final.text)
    added_key = candidate_key[len(base_key) :]
    if final_key.startswith(base_key):
        contradicted = len(added_key) - common_prefix_length(
            (added_key, final_key[len(base_key) :])
        )
    else:
        contradicted = len(added_key)
    natural_time = segment.final.time_s
    for when, stable_key in growths:
        if when >= trigger and stable_key.startswith(candidate_key):
            natural_time = when
            break
    saved = [max(0.0, natural_time - trigger)]
    return 1, len(added_key), contradicted, saved


def replay(
    segments: list[ReplaySegment],
    *,
    tracker_kind: Literal["before", "after"],
    punctuation_on: bool,
    strip: bool = False,
) -> dict[str, float | int]:
    jumps: list[float] = []
    stalls: list[float] = []
    diverged_chars = 0
    punctuation_marks = 0
    terminal_marks = 0
    escape_segments = escape_key_chars = escape_diverged_chars = 0
    escape_saved_s: list[float] = []

    for segment in segments:
        if segment.final is None:
            continue
        tracker = (
            LegacyStablePrefixTracker()
            if tracker_kind == "before"
            else StablePrefixTracker()
        )
        first_partial = segment.partials[0].time_s if segment.partials else segment.final.time_s
        growth_times: list[float] = []
        last_open = ""
        for event in segment.partials:
            text = _prepare_partial(
                event.text, punctuation_on=punctuation_on, strip=strip
            )
            update = tracker.observe(text)
            if update is not None and len(update.text) > len(last_open):
                growth_times.append(event.time_s)
                last_open = update.text

        final_text = strip_punctuation(segment.final.text) if strip else segment.final.text
        open_text = tracker.text
        closing = tracker.finalize(final_text)
        jumps.append(float(len(final_text) - len(open_text)))
        diverged_chars += closing.diverged_chars
        punctuation_marks += _punctuation_count(open_text)
        terminal_marks += bool(
            open_text and unicodedata.category(open_text[-1]).startswith("P")
        )
        points = [first_partial, *growth_times, segment.final.time_s]
        stalls.append(max((end - start for start, end in pairwise(points)), default=0.0))

        if tracker_kind == "after":
            eligible, committed, contradicted, saved = _head_escape_metrics(
                segment, punctuation_on=punctuation_on, strip=strip
            )
            escape_segments += eligible
            escape_key_chars += committed
            escape_diverged_chars += contradicted
            escape_saved_s.extend(saved)

    jump_p50, jump_p90, jump_max = _percentiles(jumps)
    stall_p50, stall_p90, stall_max = _percentiles(stalls)
    escape_p50, _, _ = _percentiles(escape_saved_s)
    return {
        "segments": len(jumps),
        "jump_p50": round(jump_p50, 1),
        "jump_p90": round(jump_p90, 1),
        "jump_max": round(jump_max, 1),
        "jump_ge20": sum(value >= 20 for value in jumps),
        "stall_p50_s": round(stall_p50, 2),
        "stall_p90_s": round(stall_p90, 2),
        "stall_max_s": round(stall_max, 2),
        "diverged_chars": diverged_chars,
        "punctuation_marks": punctuation_marks,
        "terminal_punctuation_segments": terminal_marks,
        "head_escape_segments": escape_segments,
        "head_escape_key_chars": escape_key_chars,
        "head_escape_diverged_chars": escape_diverged_chars,
        "head_escape_saved_p50_s": round(escape_p50, 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", action="append", type=Path, default=[])
    parser.add_argument("--punctuation-on", action="append", type=Path, default=[])
    args = parser.parse_args()
    if not args.trace and not args.punctuation_on:
        parser.error("provide at least one --trace or --punctuation-on path")

    traces = [(path, False) for path in args.trace] + [
        (path, True) for path in args.punctuation_on
    ]
    print(
        "trace,variant,segments,jump_p50,jump_p90,jump_max,jump_ge20,"
        "stall_p50_s,stall_p90_s,stall_max_s,diverged_chars,punctuation_marks,"
        "terminal_punctuation_segments"
    )
    for path, punctuation_on in traces:
        variants = [("raw", False)]
        if not punctuation_on:
            variants.append(("punctuation_stripped", True))
        for variant, strip in variants:
            segments = load_segments(path)
            for tracker_kind in ("before", "after"):
                result = replay(
                    segments,
                    tracker_kind=tracker_kind,
                    punctuation_on=punctuation_on,
                    strip=strip,
                )
                print(
                    ",".join(
                        str(value)
                        for value in (
                            path.name,
                            f"{variant}_{tracker_kind}",
                            result["segments"],
                            result["jump_p50"],
                            result["jump_p90"],
                            result["jump_max"],
                            result["jump_ge20"],
                            result["stall_p50_s"],
                            result["stall_p90_s"],
                            result["stall_max_s"],
                            result["diverged_chars"],
                            result["punctuation_marks"],
                            result["terminal_punctuation_segments"],
                        )
                    )
                )
                if tracker_kind == "after":
                    print(
                        "head_escape,"
                        f"{path.name},"
                        f"{variant},"
                        f"segments={result['head_escape_segments']},"
                        f"key_chars={result['head_escape_key_chars']},"
                        f"diverged_chars={result['head_escape_diverged_chars']},"
                        f"saved_p50_s={result['head_escape_saved_p50_s']}"
                    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
