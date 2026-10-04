"""Mine replacement candidates from the church services and cross-validate the gain.

Wraps ``benchmarks/dict_mine.py``.  Mining runs on *un-replaced* hypotheses (a variant label
without ``-repl``), so a candidate can never be learned from text a rule already rewrote.

* ``cv``: for each service in turn, mine on the other two, build ``current church.toml +
  safe candidates`` and score it on the held-out service next to the current dictionary
  alone and next to no dictionary.  Pooled over the three held-out folds with cluster
  bootstrap CIs.  The gain is therefore never measured on the data a rule was mined from.
* ``final``: mine on all three services and write the merged dictionary plus the private
  review table (they contain transcript snippets; keep them under the git-excluded work dir).

    python benchmarks/church_eval_dict.py cv    --base-label m4-base --current <church.toml>
    python benchmarks/church_eval_dict.py final --base-label m4-base --current <church.toml> \\
        --out-dir <recommended>
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

from benchmarks import dict_mine
from benchmarks.church_eval_report import (
    DEFAULT_WORK,
    SERVICES,
    Scorer,
    item_errors,
    load_answers,
    load_hypothesis,
)
from tea_asr.context import ReplacementRule, apply_replacements


def rules_from_toml(text: str) -> tuple[ReplacementRule, ...]:
    document = tomllib.loads(text)
    return tuple(ReplacementRule(r["from"], r["to"]) for r in document.get("replacements", []))


def merge_dictionary(current_text: str, candidates_text: str) -> tuple[str, list[dict[str, str]], list[dict[str, str]]]:
    """Append candidates whose ``from`` is not in the current dictionary.  Returns the merged
    text, the added rules and the skipped ones (same ``from`` already present)."""
    existing = {r.source: r.target for r in rules_from_toml(current_text)}
    added: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    block = ["", "# --- mined candidates (church-eval 2026-10): review before use ---"]
    for rule in rules_from_toml(candidates_text):
        entry = {"from": rule.source, "to": rule.target}
        if rule.source in existing:
            skipped.append({**entry, "existing_to": existing[rule.source]})
            continue
        existing[rule.source] = rule.target
        added.append(entry)
        block += [
            "",
            "[[replacements]]",
            f"from = {json.dumps(rule.source, ensure_ascii=False)}",
            f"to = {json.dumps(rule.target, ensure_ascii=False)}",
        ]
    merged = current_text.rstrip("\n") + ("\n" + "\n".join(block) + "\n" if added else "\n")
    return merged, added, skipped


def mine(
    services: tuple[str, ...], base_label: str, work: Path, out_dir: Path, current: Path, tag: str
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    candidates = out_dir / f"{tag}-safe.toml"
    review = out_dir / f"{tag}-review.md"
    argv: list[str] = []
    for service in services:
        argv += [
            "--answers", str(work / "answers" / f"{service}-pad25.json"),
            "--hypothesis", str(work / "eval" / base_label / f"{service}.json"),
        ]
    argv += ["--candidates-toml", str(candidates), "--review-md", str(review),
             "--merge-with", str(current)]
    if dict_mine.main(argv):
        raise RuntimeError("dict_mine failed")
    return candidates, review


def apply_to(hypothesis: dict[str, str], rules: tuple[ReplacementRule, ...]) -> dict[str, str]:
    return {key: apply_replacements(text, rules)[0] for key, text in hypothesis.items()}


def cross_validate(args: argparse.Namespace) -> dict[str, Any]:
    work: Path = args.work
    current_text = args.current.read_text("utf-8")
    current_rules = rules_from_toml(current_text)
    items = load_answers(work, SERVICES)
    base = load_hypothesis(work, args.base_label, SERVICES)
    hypotheses: dict[str, dict[str, str]] = {"none": {}, "current": {}, "merged": {}}
    folds: dict[str, Any] = {}
    for held_out in SERVICES:
        train = tuple(s for s in SERVICES if s != held_out)
        candidates, _ = mine(train, args.base_label, work, work / "dict-cv" / held_out, args.current, "fold")
        merged_text, added, skipped = merge_dictionary(current_text, candidates.read_text("utf-8"))
        merged_rules = rules_from_toml(merged_text)
        test_ids = {i["id"] for i in items if i["service"] == held_out}
        subset = {k: v for k, v in base.items() if k in test_ids}
        hypotheses["none"].update(subset)
        hypotheses["current"].update(apply_to(subset, current_rules))
        hypotheses["merged"].update(apply_to(subset, merged_rules))
        folds[held_out] = {"trained_on": list(train), "added_rules": len(added), "skipped_existing": len(skipped)}
    scorer = Scorer(items, args.iterations, args.seed)
    scores = {k: item_errors(items, h, fold=args.fold_pronouns) for k, h in hypotheses.items()}
    scopes = (*SERVICES, "pooled")
    report: dict[str, Any] = {
        "base_label": args.base_label,
        "fold_pronouns": args.fold_pronouns,
        "folds": folds,
        "cer": {k: {s: scorer.cer(*scores[k], s) for s in scopes} for k in scores},
        "deltas": {
            f"{a} minus {b}": {s: scorer.delta(scores[a], scores[b], s) for s in scopes}
            for a, b in (("current", "none"), ("merged", "current"), ("merged", "none"))
        },
    }
    return report


def final(args: argparse.Namespace) -> dict[str, Any]:
    current_text = args.current.read_text("utf-8")
    candidates, review = mine(SERVICES, args.base_label, args.work, args.work / "dict-final", args.current, "all")
    merged, added, skipped = merge_dictionary(current_text, candidates.read_text("utf-8"))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "church-candidates.toml").write_text(merged, encoding="utf-8")
    (args.out_dir / "candidates-review.md").write_text(review.read_text("utf-8"), encoding="utf-8")
    return {"added_rules": len(added), "skipped_existing": len(skipped),
            "current_rules": len(rules_from_toml(current_text))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("command", choices=("cv", "final"))
    parser.add_argument("--work", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--base-label", required=True, help="un-replaced hypothesis variant")
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--iterations", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=20261003)
    parser.add_argument("--fold-pronouns", action="store_true")
    parser.add_argument("--out", type=Path, help="write the JSON metrics here")
    args = parser.parse_args(argv)
    if args.command == "final" and args.out_dir is None:
        parser.error("final needs --out-dir")
    report = cross_validate(args) if args.command == "cv" else final(args)
    text = json.dumps(report, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
