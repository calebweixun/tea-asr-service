"""Score punctuation insertion against corrected references.

Hypotheses (ASR text, usually already carrying some marks) are aligned to the
user's corrected references by their non-punctuation characters. A "break" is
a `，。？！、` (or ASCII equivalent) after aligned character k. The script
compares the breaks of the unmodified hypothesis with the breaks after
`PunctuationModel.restore`, and checks the insert-only guarantee: removing
punctuation from both gives identical text, so CER (computed as in cer_eval) is
unchanged.

    python benchmarks/punctuation_eval.py --model <model.int8.onnx> \
        --answers speakers-answers.json --hyp 01-4bit-segment.json
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from cer_eval import edit_counts, normalize_for_scoring

from tea_asr.punctuation import PunctuationModel

BREAKS = set("，。？！、,.?!；;：:")


def breaks_of(text: str) -> tuple[list[str], set[int]]:
    """Normalised characters and the set of "break after character k" (1-based k)."""

    chars: list[str] = []
    positions: set[int] = set()
    for char in text:
        if char in BREAKS:
            if chars:
                positions.add(len(chars))
            continue
        normalised = normalize_for_scoring(char, fold_pronouns=True)
        chars.extend(normalised)
    positions.discard(len(chars))  # a closing mark carries no layout information
    return chars, positions


def align(reference: list[str], hypothesis: list[str]) -> dict[int, int]:
    """Map hypothesis char index to reference char index for equal characters."""

    matcher = difflib.SequenceMatcher(a=hypothesis, b=reference, autojunk=False)
    mapping: dict[int, int] = {}
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            mapping[block.a + offset] = block.b + offset
    return mapping


def score(reference: str, hypothesis: str) -> dict[str, int]:
    ref_chars, ref_breaks = breaks_of(reference)
    hyp_chars, hyp_breaks = breaks_of(hypothesis)
    mapping = align(ref_chars, hyp_chars)
    # A break after hyp char k-1 maps to a reference break after the matched char.
    mapped: set[int] = set()
    unmapped = 0
    for position in hyp_breaks:
        target = mapping.get(position - 1)
        if target is None:
            unmapped += 1
        else:
            mapped.add(target + 1)
    ref_covered = {k for k in ref_breaks if (k - 1) in set(mapping.values())}
    exact = len(mapped & ref_breaks)
    near = len({m for m in mapped if {m - 1, m, m + 1} & ref_breaks})
    ref_near = len({r for r in ref_covered if {r - 1, r, r + 1} & mapped})
    return {
        "hyp_marks": len(mapped),
        "tp": exact,
        "tp_near": near,
        "ref_marks": len(ref_covered),
        "recall_hits": len(mapped & ref_covered),
        "recall_near": ref_near,
        "unmapped": unmapped,
        "chars": len(ref_chars),
    }


def aggregate(rows: list[dict[str, int]]) -> dict[str, float]:
    total = {key: sum(row[key] for row in rows) for key in rows[0]}
    precision = total["tp"] / max(total["hyp_marks"], 1)
    recall = total["recall_hits"] / max(total["ref_marks"], 1)
    near_p = total["tp_near"] / max(total["hyp_marks"], 1)
    near_r = total["recall_near"] / max(total["ref_marks"], 1)
    return {
        "marks": total["hyp_marks"],
        "chars_per_mark": round(total["chars"] / max(total["hyp_marks"], 1), 1),
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(2 * precision * recall / max(precision + recall, 1e-9), 3),
        "precision_pm1": round(near_p, 3),
        "recall_pm1": round(near_r, 3),
    }


def run(
    model: PunctuationModel, answers: dict[str, Any], hyps: dict[str, str], **options: Any
) -> dict[str, Any]:
    base_rows: list[dict[str, int]] = []
    new_rows: list[dict[str, int]] = []
    inserted_tp = inserted_total = 0
    cer_before = cer_after = 0
    ref_chars_total = 0
    text_identical = True
    ms: list[float] = []
    for item in answers["items"]:
        if item.get("music") or item["id"] not in hyps:
            continue
        reference, hypothesis = item["reference"], hyps[item["id"]]
        result = model.restore(hypothesis, terminal=True, **options)
        ms.append(result.elapsed_ms)
        # Insert-only invariant: dropping the new marks gives the original back.
        stripped = result.text
        for index, _ in reversed(result.inserted):
            stripped = stripped[:index] + stripped[index + 1 :]
        text_identical &= stripped == hypothesis
        base_rows.append(score(reference, hypothesis))
        new_rows.append(score(reference, result.text))
        # Precision of the *inserted* marks alone.
        ref_chars, ref_breaks = breaks_of(reference)
        hyp_chars, _ = breaks_of(hypothesis)
        mapping = align(ref_chars, hyp_chars)
        count = 0
        for index, _mark in result.inserted:
            before = result.text[:index]
            position = len(breaks_of(before)[0])
            target = mapping.get(position - 1)
            if position == len(hyp_chars):
                continue  # closing mark: no layout value, not scored
            inserted_total += 1
            if target is not None and (target + 1) in ref_breaks:
                inserted_tp += 1
            count += 1
        ref_norm = normalize_for_scoring(reference, fold_pronouns=True)
        ref_chars_total += len(ref_norm)
        cer_before += edit_counts(
            ref_norm, normalize_for_scoring(hypothesis, fold_pronouns=True)
        ).errors
        cer_after += edit_counts(
            ref_norm, normalize_for_scoring(result.text, fold_pronouns=True)
        ).errors
    ms.sort()
    return {
        "items": len(base_rows),
        "baseline": aggregate(base_rows),
        "restored": aggregate(new_rows),
        "inserted_marks_scored": inserted_total,
        "inserted_precision": round(inserted_tp / max(inserted_total, 1), 3),
        "insert_only_text_identical": text_identical,
        "cer_before": round(cer_before / ref_chars_total, 5),
        "cer_after": round(cer_after / ref_chars_total, 5),
        "ms_p50": round(ms[len(ms) // 2], 2),
        "ms_max": round(ms[-1], 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--answers", type=Path, required=True)
    parser.add_argument("--hyp", type=Path, required=True)
    parser.add_argument("--min-gap", type=int, nargs="*", default=[3])
    args = parser.parse_args()
    model = PunctuationModel(args.model)
    answers = json.loads(args.answers.read_text(encoding="utf-8"))
    hyps = json.loads(args.hyp.read_text(encoding="utf-8"))
    out = {gap: run(model, answers, hyps, min_gap=gap) for gap in args.min_gap}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
