"""Classify offline ASR edit errors by Mandarin phonetic similarity.

Run from the repository root with the environment that has ``pypinyin``:

    PYTHONPATH=src python -m benchmarks.error_phonetics

The default input and output paths point to the private eval-services workspace.
Unclear items are excluded like ``cer_eval``'s default; music items remain in
the score. Raw examples are written only to the private output directory.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

from pypinyin import Style, pinyin

from benchmarks.cer_eval import load_hypothesis, load_reference, normalize_for_scoring
from benchmarks.dict_mine import align_characters

DEFAULT_CACHE_DIR = Path(
    "/Users/c2leb/Codes/tea-asr-service/.soak/eval-services/work/cache"
)
DEFAULT_OUTPUT_DIR = (
    Path(__file__).resolve().parents[1]
    / ".soak/eval-services/work/error-phonetics"
)

PHONETIC_CLASSES = (
    "homophone_toneless",
    "near_phone",
    "non_phonetic",
    "non_han",
    "span_mismatch",
)
ERROR_OPERATIONS = {"S", "D", "I"}
INITIAL_CONFUSIONS = frozenset(
    frozenset(pair.split())
    for pair in ("zh z", "ch c", "sh s", "n l", "f h", "r l")
)
FINAL_CONFUSIONS = frozenset(
    frozenset(pair.split())
    for pair in (
        "en eng",
        "in ing",
        "an ang",
        "ian iang",
        "uan uang",
        "un ong",
        "e o",
    )
)
MANDARIN_INITIALS = (
    "zh",
    "ch",
    "sh",
    "b",
    "p",
    "m",
    "f",
    "d",
    "t",
    "n",
    "l",
    "g",
    "k",
    "h",
    "j",
    "q",
    "x",
    "r",
    "z",
    "c",
    "s",
    "y",
    "w",
)
CONTEXT_RADIUS = 8
SAMPLE_LIMIT = 10
TOP_PAIR_LIMIT = 15


@dataclass(frozen=True)
class RunClassification:
    name: str
    tone_identical_pairs: int
    pairs: tuple[tuple[str, str], ...]


@dataclass
class ItemAnalysis:
    reference_chars: int
    substitutions: int = 0
    deletions: int = 0
    insertions: int = 0
    class_counts: Counter[str] = field(default_factory=Counter)
    tone_identical_homophones: int = 0
    span_mismatch_homophone_spans: int = 0
    pair_counts: Counter[tuple[str, str, str]] = field(default_factory=Counter)
    examples: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: defaultdict(list)
    )

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions


@dataclass
class ReportSummary:
    service: str
    hypothesis_kind: str
    item_count: int = 0
    reference_chars: int = 0
    substitutions: int = 0
    deletions: int = 0
    insertions: int = 0
    class_counts: Counter[str] = field(default_factory=Counter)
    tone_identical_homophones: int = 0
    span_mismatch_homophone_spans: int = 0
    pair_counts: Counter[tuple[str, str, str]] = field(default_factory=Counter)
    examples: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: defaultdict(list)
    )

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions

    @property
    def cer(self) -> float | None:
        if not self.reference_chars:
            return None
        return self.errors / self.reference_chars

    def add_item(self, item: ItemAnalysis) -> None:
        self.item_count += 1
        self.reference_chars += item.reference_chars
        self.substitutions += item.substitutions
        self.deletions += item.deletions
        self.insertions += item.insertions
        self.class_counts.update(item.class_counts)
        self.tone_identical_homophones += item.tone_identical_homophones
        self.span_mismatch_homophone_spans += item.span_mismatch_homophone_spans
        self.pair_counts.update(item.pair_counts)
        for category, examples in item.examples.items():
            self.examples[category].extend(examples)

    def merge(self, other: ReportSummary) -> None:
        self.item_count += other.item_count
        self.reference_chars += other.reference_chars
        self.substitutions += other.substitutions
        self.deletions += other.deletions
        self.insertions += other.insertions
        self.class_counts.update(other.class_counts)
        self.tone_identical_homophones += other.tone_identical_homophones
        self.span_mismatch_homophone_spans += other.span_mismatch_homophone_spans
        self.pair_counts.update(other.pair_counts)
        for category, examples in other.examples.items():
            self.examples[category].extend(examples)


@lru_cache(maxsize=16384)
def is_han(character: str) -> bool:
    if len(character) != 1:
        return False
    name = unicodedata.name(character, "")
    return name.startswith(("CJK UNIFIED IDEOGRAPH-", "CJK COMPATIBILITY IDEOGRAPH-")) or (
        name == "IDEOGRAPHIC NUMBER ZERO"
    )


@lru_cache(maxsize=16384)
def normal_readings(character: str) -> frozenset[str]:
    if not is_han(character):
        return frozenset()
    values = pinyin(character, style=Style.NORMAL, heteronym=True, errors="ignore")
    if not values:
        return frozenset()
    return frozenset(value.lower() for value in values[0] if value)


@lru_cache(maxsize=16384)
def tone_readings(character: str) -> frozenset[str]:
    if not is_han(character):
        return frozenset()
    values = pinyin(
        character,
        style=Style.TONE3,
        heteronym=True,
        neutral_tone_with_five=True,
        errors="ignore",
    )
    if not values:
        return frozenset()
    return frozenset(value.lower() for value in values[0] if value)


def _split_syllable(syllable: str) -> tuple[str, str]:
    for initial in MANDARIN_INITIALS:
        if syllable.startswith(initial):
            return initial, syllable[len(initial) :]
    return "", syllable


def _pair_is_homophone(reference: str, hypothesis: str) -> bool:
    if reference == hypothesis:
        return True
    left = normal_readings(reference)
    return bool(left and left.intersection(normal_readings(hypothesis)))


def _pair_is_tonally_identical(reference: str, hypothesis: str) -> bool:
    if reference == hypothesis:
        return True
    left = tone_readings(reference)
    return bool(left and left.intersection(tone_readings(hypothesis)))


def _pair_is_near_phone(reference: str, hypothesis: str) -> bool:
    if reference == hypothesis:
        return True
    left_readings = normal_readings(reference)
    right_readings = normal_readings(hypothesis)
    for left in left_readings:
        left_initial, left_final = _split_syllable(left)
        for right in right_readings:
            right_initial, right_final = _split_syllable(right)
            if (left_initial, left_final) == (right_initial, right_final):
                return True
            if (
                left_final == right_final
                and frozenset((left_initial, right_initial)) in INITIAL_CONFUSIONS
            ):
                return True
            if (
                left_initial == right_initial
                and frozenset((left_final, right_final)) in FINAL_CONFUSIONS
            ):
                return True
    return False


def classify_substitution_run(
    reference_span: str, hypothesis_span: str
) -> RunClassification:
    """Classify one equal-length, match-delimited substitution run."""
    if not reference_span or len(reference_span) != len(hypothesis_span):
        raise ValueError("substitution runs must be non-empty and equal length")

    pairs = tuple(zip(reference_span, hypothesis_span, strict=True))
    if any(not is_han(left) or not is_han(right) for left, right in pairs):
        category = "non_han"
    elif all(_pair_is_homophone(left, right) for left, right in pairs):
        category = "homophone_toneless"
    elif all(_pair_is_near_phone(left, right) for left, right in pairs):
        category = "near_phone"
    else:
        category = "non_phonetic"

    tone_matches = 0
    if category == "homophone_toneless":
        tone_matches = sum(
            _pair_is_tonally_identical(left, right) for left, right in pairs
        )
    return RunClassification(category, tone_matches, pairs)


def _span_readings(span: str) -> tuple[tuple[str, ...], ...] | None:
    readings: list[tuple[str, ...]] = []
    for character in span:
        choices = tuple(sorted(normal_readings(character)))
        if not choices:
            return None
        readings.append(choices)
    return tuple(readings)


def concatenated_pinyin_matches(reference_span: str, hypothesis_span: str) -> bool:
    """Return whether any heteronym readings concatenate to the same string."""
    if not reference_span or not hypothesis_span:
        return False
    left = _span_readings(reference_span)
    right = _span_readings(hypothesis_span)
    if left is None or right is None:
        return False

    @cache
    def match(i: int, j: int, pending_side: int, pending: str) -> bool:
        if pending_side == 0:
            if i == len(left) or j == len(right):
                return i == len(left) and j == len(right)
            for left_syllable in left[i]:
                for right_syllable in right[j]:
                    if left_syllable == right_syllable:
                        if match(i + 1, j + 1, 0, ""):
                            return True
                    elif left_syllable.startswith(right_syllable):
                        if match(
                            i + 1,
                            j + 1,
                            1,
                            left_syllable[len(right_syllable) :],
                        ):
                            return True
                    elif right_syllable.startswith(left_syllable) and match(
                        i + 1,
                        j + 1,
                        2,
                        right_syllable[len(left_syllable) :],
                    ):
                        return True
            return False

        if pending_side == 1:
            if j == len(right):
                return False
            for right_syllable in right[j]:
                if pending == right_syllable:
                    if match(i, j + 1, 0, ""):
                        return True
                elif pending.startswith(right_syllable):
                    if match(i, j + 1, 1, pending[len(right_syllable) :]):
                        return True
                elif right_syllable.startswith(pending) and match(
                    i, j + 1, 2, right_syllable[len(pending) :]
                ):
                    return True
            return False

        if i == len(left):
            return False
        for left_syllable in left[i]:
            if pending == left_syllable:
                if match(i + 1, j, 0, ""):
                    return True
            elif pending.startswith(left_syllable):
                if match(i + 1, j, 2, pending[len(left_syllable) :]):
                    return True
            elif left_syllable.startswith(pending) and match(
                i + 1, j, 1, left_syllable[len(pending) :]
            ):
                return True
        return False

    return match(0, 0, 0, "")


def _marked_context(text: str, start: int, end: int, radius: int = CONTEXT_RADIUS) -> str:
    start = max(0, min(start, len(text)))
    end = max(start, min(end, len(text)))
    before = text[max(0, start - radius) : start]
    target = text[start:end] or "∅"
    after = text[end : min(len(text), end + radius)]
    return f"{before}⟦{target}⟧{after}"


def analyze_normalized_item(
    reference: str,
    hypothesis: str,
    *,
    item_id: str = "",
    service: str = "",
    hypothesis_kind: str = "",
) -> ItemAnalysis:
    """Align normalized texts and classify every edit operation exactly once."""
    result = ItemAnalysis(reference_chars=len(reference))
    steps = align_characters(reference, hypothesis)
    pending: list[Any] = []
    previous_reference_end = 0
    previous_hypothesis_end = 0

    def classify_pending() -> None:
        nonlocal pending
        if not pending:
            return

        reference_indices = [
            step.reference_index
            for step in pending
            if step.reference_index is not None
        ]
        hypothesis_indices = [
            step.hypothesis_index
            for step in pending
            if step.hypothesis_index is not None
        ]
        reference_start = min(reference_indices, default=previous_reference_end)
        hypothesis_start = min(hypothesis_indices, default=previous_hypothesis_end)
        reference_end = max(reference_indices, default=reference_start - 1) + 1
        hypothesis_end = max(hypothesis_indices, default=hypothesis_start - 1) + 1
        reference_span = "".join(reference[index] for index in reference_indices)
        hypothesis_span = "".join(hypothesis[index] for index in hypothesis_indices)
        operation_count = len(pending)
        operations = Counter(step.operation for step in pending)

        if len(reference_span) != len(hypothesis_span):
            category = "span_mismatch"
            result.class_counts[category] += operation_count
            concatenated_match = concatenated_pinyin_matches(
                reference_span, hypothesis_span
            )
            if concatenated_match:
                result.span_mismatch_homophone_spans += 1
        else:
            classification = classify_substitution_run(reference_span, hypothesis_span)
            category = classification.name
            result.class_counts[category] += operation_count
            if category == "homophone_toneless":
                result.tone_identical_homophones += (
                    classification.tone_identical_pairs
                )
            if category in {"homophone_toneless", "near_phone"}:
                for left, right in classification.pairs:
                    result.pair_counts[(category, left, right)] += 1
            concatenated_match = False

        result.examples[category].append(
            {
                "item_id": item_id,
                "service": service,
                "hypothesis": hypothesis_kind,
                "classification": category,
                "reference_span": reference_span,
                "hypothesis_span": hypothesis_span,
                "errors": operation_count,
                "substitutions": operations["S"],
                "deletions": operations["D"],
                "insertions": operations["I"],
                "span_pinyin_match": concatenated_match,
                "reference_context": _marked_context(
                    reference, reference_start, reference_end
                ),
                "hypothesis_context": _marked_context(
                    hypothesis, hypothesis_start, hypothesis_end
                ),
            }
        )
        pending = []

    for step in steps:
        if step.operation == "M":
            classify_pending()
            previous_reference_end = (step.reference_index or 0) + 1
            previous_hypothesis_end = (step.hypothesis_index or 0) + 1
        else:
            pending.append(step)
            if step.operation == "S":
                result.substitutions += 1
            elif step.operation == "D":
                result.deletions += 1
            elif step.operation == "I":
                result.insertions += 1
            else:
                raise ValueError(f"unexpected alignment operation: {step.operation}")
    classify_pending()

    if sum(result.class_counts.values()) != result.errors:
        raise AssertionError("phonetic classes must account for each edit operation")
    return result


def _safe_label(label: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", label).strip("._-")
    return cleaned or "service"


def _service_directories(cache_dir: Path) -> list[tuple[str, Path]]:
    answer_paths = sorted(cache_dir.glob("*/answers.json"))
    if not answer_paths:
        raise FileNotFoundError(f"no cache/<sha>/answers.json files under {cache_dir}")

    records: list[tuple[str, Path]] = []
    labels: Counter[str] = Counter()
    for answer_path in answer_paths:
        reference = load_reference(answer_path)
        preferred = reference.get("service", reference.get("set"))
        label = str(preferred) if preferred is not None else answer_path.parent.name
        labels[label] += 1
        records.append((label, answer_path.parent))

    return [
        (f"{label}-{folder.name[:8]}" if labels[label] > 1 else label, folder)
        for label, folder in records
    ]


def analyze_service(
    service: str, folder: Path, *, include_unclear: bool = False
) -> dict[str, ReportSummary]:
    reference = load_reference(folder / "answers.json")
    raw_kind, raw_hypotheses = load_hypothesis(folder / "hypotheses-raw.json")
    dictionary_kind, dictionary_hypotheses = load_hypothesis(
        folder / "hypotheses-dictionary.json"
    )
    if raw_kind != "json" or dictionary_kind != "json":
        raise TypeError("raw and dictionary hypotheses must be item-keyed JSON objects")

    summaries = {
        "raw": ReportSummary(service, "raw"),
        "dictionary": ReportSummary(service, "dictionary"),
    }
    hypotheses_by_kind = {
        "raw": raw_hypotheses,
        "dictionary": dictionary_hypotheses,
    }
    for item in reference["items"]:
        if bool(item.get("unclear", False)) and not include_unclear:
            continue
        item_id = item["id"]
        normalized_reference = normalize_for_scoring(
            item["reference"], fold_pronouns=True
        )
        for kind, hypotheses in hypotheses_by_kind.items():
            normalized_hypothesis = normalize_for_scoring(
                hypotheses.get(item_id, ""), fold_pronouns=True
            )
            result = analyze_normalized_item(
                normalized_reference,
                normalized_hypothesis,
                item_id=item_id,
                service=service,
                hypothesis_kind=kind,
            )
            summaries[kind].add_item(result)
    return summaries


def _validate_stored_cer(folder: Path, summaries: dict[str, ReportSummary]) -> int:
    result_path = folder / "result.json"
    if not result_path.exists():
        return 0
    result = json.loads(result_path.read_text(encoding="utf-8"))
    scores = result.get("scores", {})
    score_names = {"raw": "raw", "dictionary": "with_dictionary"}
    checked = 0
    for kind, summary in summaries.items():
        score = scores.get(score_names[kind], {})
        expected = score.get("folded_cer")
        if expected is None:
            continue
        if summary.reference_chars != score.get("reference_chars") or not math.isclose(
            summary.cer or 0.0, float(expected), rel_tol=0.0, abs_tol=1e-12
        ):
            raise ValueError(
                f"computed folded CER does not match stored score for {kind}"
            )
        checked += 1
    return checked


def _share(count: int, total: int) -> float:
    return count / total if total else 0.0


def _format_percent(value: float) -> str:
    return f"{100 * value:.2f}%"


def _print_summary(summary: ReportSummary) -> None:
    classes = " ".join(
        f"{category}={summary.class_counts[category]}"
        f"({_format_percent(_share(summary.class_counts[category], summary.errors))})"
        for category in PHONETIC_CLASSES
    )
    cer = "NA" if summary.cer is None else _format_percent(summary.cer)
    print(
        f"kind={summary.hypothesis_kind} service={summary.service} "
        f"items={summary.item_count} reference_chars={summary.reference_chars} "
        f"S={summary.substitutions} D={summary.deletions} I={summary.insertions} "
        f"errors={summary.errors} CER={cer} {classes} "
        f"homophone_tonal_identical={summary.tone_identical_homophones} "
        f"span_mismatch_homophone_spans={summary.span_mismatch_homophone_spans}"
    )


def _write_pair_report(summary: ReportSummary, path: Path) -> None:
    rows = sorted(
        summary.pair_counts.items(),
        key=lambda row: (-row[1], row[0][0], row[0][1], row[0][2]),
    )[:TOP_PAIR_LIMIT]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("rank", "class", "reference_char", "hypothesis_char", "count"))
        for rank, ((category, reference, hypothesis), count) in enumerate(rows, start=1):
            writer.writerow((rank, category, reference, hypothesis, count))


def _write_example_report(
    summary: ReportSummary, path: Path, *, seed: int
) -> None:
    rng = random.Random(seed)
    with path.open("w", encoding="utf-8") as handle:
        for category in PHONETIC_CLASSES:
            population = summary.examples.get(category, [])
            selected = rng.sample(population, min(SAMPLE_LIMIT, len(population)))
            for example in selected:
                handle.write(json.dumps(example, ensure_ascii=False) + "\n")


def write_private_reports(
    summaries: Iterable[ReportSummary], output_dir: Path, *, seed: int = 1729
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for summary in summaries:
        stem = f"{_safe_label(summary.service)}-{summary.hypothesis_kind}"
        _write_pair_report(summary, output_dir / f"{stem}-top-pairs.csv")
        _write_example_report(summary, output_dir / f"{stem}-examples.jsonl", seed=seed)
        written += 2
    return written


def run_analysis(cache_dir: Path, output_dir: Path, *, seed: int = 1729) -> dict[
    tuple[str, str], ReportSummary
]:
    summaries: dict[tuple[str, str], ReportSummary] = {}
    pooled = {
        kind: ReportSummary("pooled", kind) for kind in ("raw", "dictionary")
    }
    services = _service_directories(cache_dir)
    cer_validations = 0
    for service, folder in services:
        service_summaries = analyze_service(service, folder)
        cer_validations += _validate_stored_cer(folder, service_summaries)
        for kind, summary in service_summaries.items():
            summaries[(service, kind)] = summary
            pooled[kind].merge(summary)
    for kind, summary in pooled.items():
        summaries[("pooled", kind)] = summary

    print(f"stored_folded_CER_validation=PASS comparisons={cer_validations}")

    for kind in ("raw", "dictionary"):
        for service, _folder in services:
            _print_summary(summaries[(service, kind)])
        _print_summary(pooled[kind])

    dictionary = pooled["dictionary"]
    phonetic_errors = (
        dictionary.class_counts["homophone_toneless"]
        + dictionary.class_counts["near_phone"]
    )
    phonetic_share = _share(phonetic_errors, dictionary.errors)
    reduction = _share(phonetic_errors, dictionary.reference_chars)
    print(
        f"dictionary_phonetic_gate={'PASS' if phonetic_share >= 0.30 else 'FAIL'} "
        f"phonetic_errors={phonetic_errors} all_errors={dictionary.errors} "
        f"share={_format_percent(phonetic_share)} threshold=30.00%"
    )
    print(
        f"dictionary_upper_bound_CER_reduction={_format_percent(reduction)} "
        f"({phonetic_errors}/{dictionary.reference_chars} reference chars)"
    )

    report_count = write_private_reports(summaries.values(), output_dir, seed=seed)
    print(f"private_report_files_written={report_count}")
    return summaries


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--seed", type=int, default=1729)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_analysis(args.cache_dir, args.output_dir, seed=args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
