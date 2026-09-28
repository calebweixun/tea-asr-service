"""Evaluate character error rates against corrected listening-kit answers."""

from __future__ import annotations

import argparse
import itertools
import json
import math
import random
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SAMPLE_RATE = 16_000


@dataclass(frozen=True)
class EditCounts:
    substitutions: int
    deletions: int
    insertions: int
    reference_chars: int
    hypothesis_chars: int

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions

    @property
    def cer(self) -> float | None:
        if self.reference_chars == 0:
            return None
        return self.errors / self.reference_chars

    def as_dict(self) -> dict[str, int | float | None]:
        return {
            "cer": self.cer,
            "substitutions": self.substitutions,
            "deletions": self.deletions,
            "insertions": self.insertions,
            "errors": self.errors,
            "reference_chars": self.reference_chars,
            "hypothesis_chars": self.hypothesis_chars,
        }


def normalize(text: str, *, converter: Any | None = None) -> str:
    """Fold fullwidth forms, remove punctuation/spacing, and lowercase Latin text."""
    normalized = unicodedata.normalize("NFKC", text)
    if converter is not None:
        normalized = converter.convert(normalized)
    chars: list[str] = []
    for char in normalized:
        category = unicodedata.category(char)
        if char.isspace() or category.startswith(("P", "Z", "C")):
            continue
        if "LATIN" in unicodedata.name(char, ""):
            char = char.lower()
        chars.append(char)
    return "".join(chars)


def edit_counts(reference: Sequence[str], hypothesis: Sequence[str]) -> EditCounts:
    """Return Levenshtein substitution, deletion, and insertion counts."""
    ref_len = len(reference)
    hyp_len = len(hypothesis)
    costs = [[0] * (hyp_len + 1) for _ in range(ref_len + 1)]
    directions = [[""] * (hyp_len + 1) for _ in range(ref_len + 1)]
    for row in range(1, ref_len + 1):
        costs[row][0] = row
        directions[row][0] = "D"
    for column in range(1, hyp_len + 1):
        costs[0][column] = column
        directions[0][column] = "I"

    for row in range(1, ref_len + 1):
        for column in range(1, hyp_len + 1):
            same = reference[row - 1] == hypothesis[column - 1]
            choices = (
                (costs[row - 1][column - 1] + (not same), "M" if same else "S"),
                (costs[row - 1][column] + 1, "D"),
                (costs[row][column - 1] + 1, "I"),
            )
            costs[row][column], directions[row][column] = min(
                choices, key=lambda choice: (choice[0], "MSDI".index(choice[1]))
            )

    substitutions = deletions = insertions = 0
    row, column = ref_len, hyp_len
    while row or column:
        direction = directions[row][column]
        if direction in ("M", "S"):
            substitutions += direction == "S"
            row -= 1
            column -= 1
        elif direction == "D":
            deletions += 1
            row -= 1
        else:
            insertions += 1
            column -= 1
    return EditCounts(
        substitutions=int(substitutions),
        deletions=deletions,
        insertions=insertions,
        reference_chars=ref_len,
        hypothesis_chars=hyp_len,
    )


def transcript_final_events(trace_path: Path) -> list[dict[str, Any]]:
    lines = trace_path.read_text(encoding="utf-8").splitlines()
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            if line_number == len(lines):
                break
            raise ValueError(f"Invalid JSON in {trace_path} at line {line_number}")
        event = row.get("event")
        if not isinstance(event, dict) or event.get("type") != "transcript.final":
            continue
        if not isinstance(event.get("text"), str):
            continue
        if not isinstance(event.get("start_sample"), (int, float)):
            continue
        if not isinstance(event.get("end_sample"), (int, float)):
            continue
        if event["end_sample"] > event["start_sample"]:
            events.append(event)
    return sorted(events, key=lambda event: (event["start_sample"], event["end_sample"]))


def join_event_text(events: Sequence[dict[str, Any]]) -> str:
    text = ""
    for event in events:
        part = str(event.get("text", "")).strip()
        if not part:
            continue
        if text and text[-1].isascii() and text[-1].isalnum() and part[0].isascii() and part[0].isalnum():
            text += " "
        text += part
    return text


def trace_to_item(
    events: Sequence[dict[str, Any]], start_s: float, end_s: float, sample_rate: int = SAMPLE_RATE
) -> str:
    """Join final events that overlap the item's half-open time interval."""
    start_sample = start_s * sample_rate
    end_sample = end_s * sample_rate
    matches = [
        event
        for event in events
        if event["start_sample"] < end_sample and event["end_sample"] > start_sample
    ]
    return join_event_text(matches)


def load_reference(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise TypeError("reference JSON must be a kit answer object with an items array")
    seen: set[str] = set()
    for item in value["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise TypeError("each reference item must have a string id")
        if item["id"] in seen:
            raise ValueError(f"duplicate reference item id: {item['id']}")
        seen.add(item["id"])
        if not isinstance(item.get("start_s"), (int, float)) or not isinstance(
            item.get("end_s"), (int, float)
        ):
            raise TypeError(f"reference item {item['id']} needs numeric start_s and end_s")
        if item["end_s"] <= item["start_s"]:
            raise ValueError(f"reference item {item['id']} has an invalid time range")
        if not isinstance(item.get("reference"), str):
            raise TypeError(f"reference item {item['id']} needs a string reference")
    return value


def load_hypothesis(path: Path) -> tuple[str, dict[str, str] | list[dict[str, Any]]]:
    if path.suffix.lower() == ".json":
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or any(
            not isinstance(key, str) or not isinstance(text, str) for key, text in value.items()
        ):
            raise ValueError(f"hypothesis JSON must be an object mapping item id to text: {path}")
        return "json", value
    return "trace", transcript_final_events(path)


def summarize(counts: Sequence[EditCounts], item_count: int | None = None) -> dict[str, int | float | None]:
    aggregate = EditCounts(
        substitutions=sum(count.substitutions for count in counts),
        deletions=sum(count.deletions for count in counts),
        insertions=sum(count.insertions for count in counts),
        reference_chars=sum(count.reference_chars for count in counts),
        hypothesis_chars=sum(count.hypothesis_chars for count in counts),
    )
    result: dict[str, int | float | None] = aggregate.as_dict()
    result["items"] = len(counts) if item_count is None else item_count
    return result


def make_item_counts(
    reference: dict[str, Any],
    source: tuple[str, dict[str, str] | list[dict[str, Any]]],
    *,
    include_unclear: bool,
    converter: Any | None = None,
) -> tuple[list[dict[str, Any]], list[EditCounts], list[str], int]:
    source_kind, source_value = source
    included_rows: list[dict[str, Any]] = []
    counts: list[EditCounts] = []
    excluded_ids: list[str] = []
    missing = 0
    for item in reference["items"]:
        if bool(item.get("unclear", False)) and not include_unclear:
            excluded_ids.append(item["id"])
            continue
        if source_kind == "trace":
            hypothesis = trace_to_item(source_value, item["start_s"], item["end_s"])
        else:
            hypothesis = source_value.get(item["id"], "")
            missing += item["id"] not in source_value
        ref_text = normalize(item["reference"], converter=converter)
        hyp_text = normalize(hypothesis, converter=converter)
        count = edit_counts(ref_text, hyp_text)
        counts.append(count)
        included_rows.append(
            {
                "id": item["id"],
                "set": str(item.get("set", reference.get("set", "unknown"))),
                "music": bool(item.get("music", False)),
                "unclear": bool(item.get("unclear", False)),
                **count.as_dict(),
            }
        )
    return included_rows, counts, excluded_ids, missing


def summarize_breakdowns(rows: Sequence[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    by_set: dict[str, list[EditCounts]] = {}
    by_music: dict[str, list[EditCounts]] = {"true": [], "false": []}
    for row in rows:
        count = EditCounts(
            substitutions=row["substitutions"],
            deletions=row["deletions"],
            insertions=row["insertions"],
            reference_chars=row["reference_chars"],
            hypothesis_chars=row["hypothesis_chars"],
        )
        by_set.setdefault(row["set"], []).append(count)
        by_music["true" if row["music"] else "false"].append(count)
    sets = {name: summarize(counts) for name, counts in sorted(by_set.items())}
    music = {name: summarize(counts) for name, counts in by_music.items()}
    return sets, music


def quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def bootstrap_difference_ci(
    first: Sequence[EditCounts],
    second: Sequence[EditCounts],
    *,
    iterations: int = 10_000,
    seed: int = 1729,
) -> dict[str, float | int | None]:
    """Paired item bootstrap for first-system CER minus second-system CER."""
    if len(first) != len(second):
        raise ValueError("paired bootstrap requires one score from each system per item")
    if iterations < 1:
        raise ValueError("bootstrap iterations must be positive")
    observed_first = summarize(first)["cer"]
    observed_second = summarize(second)["cer"]
    observed = (
        float(observed_first) - float(observed_second)
        if observed_first is not None and observed_second is not None
        else None
    )
    if not first:
        return {"delta_cer": observed, "ci95_low": None, "ci95_high": None, "iterations": iterations}
    rng = random.Random(seed)
    samples: list[float] = []
    for _ in range(iterations):
        indices = [rng.randrange(len(first)) for _ in first]
        a = summarize([first[index] for index in indices])["cer"]
        b = summarize([second[index] for index in indices])["cer"]
        if a is not None and b is not None:
            samples.append(float(a) - float(b))
    return {
        "delta_cer": observed,
        "ci95_low": quantile(samples, 0.025),
        "ci95_high": quantile(samples, 0.975),
        "iterations": iterations,
    }


def evaluate(
    reference: dict[str, Any],
    hypothesis_paths: Sequence[Path],
    *,
    include_unclear: bool = False,
    bootstrap_iterations: int = 10_000,
    seed: int = 1729,
) -> dict[str, Any]:
    converter = None
    try:
        from opencc import OpenCC

        converter = OpenCC("s2t")
    except ImportError:
        pass

    sources: list[dict[str, Any]] = []
    source_counts: list[list[EditCounts]] = []
    for path in hypothesis_paths:
        loaded = load_hypothesis(path)
        rows, counts, excluded_ids, missing = make_item_counts(
            reference,
            loaded,
            include_unclear=include_unclear,
        )
        sets, music = summarize_breakdowns(rows)
        source_report: dict[str, Any] = {
            "name": path.name,
            "missing_hypothesis_items": missing,
            "excluded_unclear_items": len(excluded_ids),
            "excluded_unclear_ids": excluded_ids,
            "overall": summarize(counts),
            "per_set": sets,
            "breakdown_by_music": music,
            "per_item": rows,
        }
        if converter is not None:
            variant_rows, variant_counts, _, _ = make_item_counts(
                reference,
                loaded,
                include_unclear=include_unclear,
                converter=converter,
            )
            variant_sets, variant_music = summarize_breakdowns(variant_rows)
            source_report["simplified_to_traditional"] = {
                "overall": summarize(variant_counts),
                "per_set": variant_sets,
                "breakdown_by_music": variant_music,
                "per_item": variant_rows,
            }
        sources.append(source_report)
        source_counts.append(counts)

    comparisons: list[dict[str, Any]] = []
    for first_index, second_index in itertools.combinations(range(len(sources)), 2):
        comparison: dict[str, Any] = {
            "first": sources[first_index]["name"],
            "second": sources[second_index]["name"],
            "note": "delta_cer is first minus second; a negative value favors the first system",
            "normalization": bootstrap_difference_ci(
                source_counts[first_index],
                source_counts[second_index],
                iterations=bootstrap_iterations,
                seed=seed + first_index * 101 + second_index,
            ),
        }
        if converter is not None:
            first_variant = make_item_counts(
                reference,
                load_hypothesis(hypothesis_paths[first_index]),
                include_unclear=include_unclear,
                converter=converter,
            )[1]
            second_variant = make_item_counts(
                reference,
                load_hypothesis(hypothesis_paths[second_index]),
                include_unclear=include_unclear,
                converter=converter,
            )[1]
            comparison["simplified_to_traditional"] = bootstrap_difference_ci(
                first_variant,
                second_variant,
                iterations=bootstrap_iterations,
                seed=seed + first_index * 101 + second_index,
            )
        comparisons.append(comparison)

    return {
        "reference_set": reference.get("set", "unknown"),
        "unclear_true_excluded_by_default": not include_unclear,
        "included_items": sum(len(counts) for counts in source_counts[:1]),
        "excluded_unclear_items": sum(bool(item.get("unclear", False)) for item in reference["items"])
        if not include_unclear
        else 0,
        "normalization": "NFKC fullwidth fold; punctuation, spaces, and control characters removed; Latin lowercased",
        "simplified_to_traditional_available": converter is not None,
        "simplified_to_traditional_note": (
            "Reported only because OpenCC is installed in this environment."
            if converter is not None
            else "Not reported: OpenCC is not installed in this environment."
        ),
        "sources": sources,
        "pairwise_bootstrap_95_ci": comparisons,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path, help="downloaded listening-kit answers JSON")
    parser.add_argument(
        "hypotheses", type=Path, nargs="+", help="trace JSONL or JSON mapping item ids to text"
    )
    parser.add_argument("--include-unclear", action="store_true", help="score unclear=true items")
    parser.add_argument("--bootstrap", type=int, default=10_000, help="paired bootstrap iterations")
    parser.add_argument("--seed", type=int, default=1729)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        reference = load_reference(args.reference)
        report = evaluate(
            reference,
            args.hypotheses,
            include_unclear=args.include_unclear,
            bootstrap_iterations=args.bootstrap,
            seed=args.seed,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError, ImportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
