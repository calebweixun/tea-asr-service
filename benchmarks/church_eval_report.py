"""Score the church offline matrix: per service and pooled CER with cluster-bootstrap CIs.

Inputs are ``<work>/answers/<service>-pad25.json`` and the hypotheses written by
``church_eval_matrix.py`` (``<work>/eval/<label>/<service>.json``).  Output is metrics
only (no transcript text): JSON plus a Markdown table.

CIs resample *clusters* (consecutive items of one service in the same 5-minute block),
not single items, because neighbouring chunks share speaker, topic and audio conditions.
Paired deltas reuse the same resampled clusters for both systems.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

import numpy as np

from benchmarks.cer_eval import edit_counts, normalize_for_scoring
from benchmarks.gold_offline_eval import count_duplicate_prefix_items

DEFAULT_WORK = Path("/Users/c2leb/Codes/tea-asr-service/.soak/church-eval/work")
SERVICES = ("20260704", "20260822", "20260912")
BLOCK_S = 300.0


def load_answers(work: Path, services: tuple[str, ...]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for service in services:
        document = json.loads((work / "answers" / f"{service}-pad25.json").read_text("utf-8"))
        for item in document["items"]:
            items.append({**item, "service": service})
    return items


def load_hypothesis(work: Path, label: str, services: tuple[str, ...]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for service in services:
        merged.update(json.loads((work / "eval" / label / f"{service}.json").read_text("utf-8")))
    return merged


def item_errors(
    items: list[dict[str, Any]], hypothesis: dict[str, str], *, fold: bool
) -> tuple[np.ndarray, np.ndarray]:
    errors = np.zeros(len(items))
    chars = np.zeros(len(items))
    for index, item in enumerate(items):
        reference = normalize_for_scoring(item["reference"], fold_pronouns=fold)
        text = normalize_for_scoring(hypothesis.get(item["id"], ""), fold_pronouns=fold)
        counts = edit_counts(reference, text)
        errors[index] = counts.errors
        chars[index] = counts.reference_chars
    return errors, chars


def cluster_ids(items: list[dict[str, Any]]) -> np.ndarray:
    keys: dict[tuple[str, int], int] = {}
    ids = np.zeros(len(items), dtype=int)
    for index, item in enumerate(items):
        key = (item["service"], int(item["start_s"] // BLOCK_S))
        ids[index] = keys.setdefault(key, len(keys))
    return ids


class Scorer:
    """Per-cluster sums so a bootstrap resample is two vector gathers."""

    def __init__(self, items: list[dict[str, Any]], iterations: int, seed: int) -> None:
        self.items = items
        self.cluster = cluster_ids(items)
        self.n_clusters = int(self.cluster.max()) + 1
        self.service_of_cluster = np.zeros(self.n_clusters, dtype=object)
        for index, item in enumerate(items):
            self.service_of_cluster[self.cluster[index]] = item["service"]
        rng = np.random.default_rng(seed)
        self.iterations = iterations
        # Resample clusters within each service, so per-service and pooled draws agree.
        self.draws: dict[str, np.ndarray] = {}
        by_service = {
            service: np.flatnonzero(self.service_of_cluster == service)
            for service in sorted({item["service"] for item in items})
        }
        pooled = np.zeros((iterations, self.n_clusters), dtype=np.int32)
        for service, clusters in by_service.items():
            picks = rng.integers(0, len(clusters), size=(iterations, len(clusters)))
            counts = np.zeros((iterations, self.n_clusters), dtype=np.int32)
            rows = np.arange(iterations)[:, None]
            np.add.at(counts, (rows, clusters[picks]), 1)
            self.draws[service] = counts
            pooled += counts
        self.draws["pooled"] = pooled

    def cluster_sums(self, errors: np.ndarray, chars: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        err = np.bincount(self.cluster, weights=errors, minlength=self.n_clusters)
        ref = np.bincount(self.cluster, weights=chars, minlength=self.n_clusters)
        return err, ref

    def scope_mask(self, scope: str) -> np.ndarray:
        if scope == "pooled":
            return np.ones(self.n_clusters, dtype=bool)
        return self.service_of_cluster == scope

    def cer(self, errors: np.ndarray, chars: np.ndarray, scope: str) -> dict[str, float | int]:
        err, ref = self.cluster_sums(errors, chars)
        mask = self.scope_mask(scope)
        point = err[mask].sum() / ref[mask].sum()
        draws = self.draws[scope]
        samples = (draws @ err) / (draws @ ref)
        return {
            "cer": float(point),
            "ci95_low": float(np.quantile(samples, 0.025)),
            "ci95_high": float(np.quantile(samples, 0.975)),
            "errors": int(err[mask].sum()),
            "reference_chars": int(ref[mask].sum()),
        }

    def delta(
        self, a: tuple[np.ndarray, np.ndarray], b: tuple[np.ndarray, np.ndarray], scope: str
    ) -> dict[str, float | bool]:
        """CER(a) minus CER(b); negative favours a."""
        ea, ra = self.cluster_sums(*a)
        eb, rb = self.cluster_sums(*b)
        mask = self.scope_mask(scope)
        point = ea[mask].sum() / ra[mask].sum() - eb[mask].sum() / rb[mask].sum()
        draws = self.draws[scope]
        samples = (draws @ ea) / (draws @ ra) - (draws @ eb) / (draws @ rb)
        low, high = float(np.quantile(samples, 0.025)), float(np.quantile(samples, 0.975))
        return {
            "delta_cer": float(point),
            "ci95_low": low,
            "ci95_high": high,
            "significant": bool(low > 0 or high < 0),
        }


def timing_summary(work: Path, label: str, services: tuple[str, ...]) -> dict[str, float]:
    decode = audio = attempts = items = fallback = used = 0.0
    for service in services:
        path = work / "eval" / label / f"{service}.timings.json"
        if not path.exists():
            continue
        for row in json.loads(path.read_text("utf-8")).values():
            decode += row["decode_s"]
            audio += row["audio_s"]
            segment_audio = row["segment_audio_s"]
            attempts += row["decode_attempts"]
            items += 1
            fallback += bool(row["carry_fallback"])
            used += bool(row["carry_used"])
            del segment_audio
    return {
        "decode_rtf": decode / audio if audio else float("nan"),
        "attempts_per_item": attempts / items if items else float("nan"),
        "carry_used_rate": used / items if items else 0.0,
        "carry_fallback_rate": fallback / items if items else 0.0,
    }


def replacement_effects(
    items: list[dict[str, Any]], hypothesis: dict[str, str], dictionary: Path
) -> list[dict[str, Any]]:
    """Per rule: hits in the un-replaced hypothesis and the net error change if only that rule
    were applied (negative = the rule helps).  Rules are identified by index, not text."""
    rules = tomllib.loads(dictionary.read_text("utf-8")).get("replacements", [])
    rows = []
    for number, rule in enumerate(rules, start=1):
        hits = items_hit = 0
        net = 0
        for item in items:
            text = hypothesis.get(item["id"], "")
            if rule["from"] not in text:
                continue
            hits += text.count(rule["from"])
            items_hit += 1
            reference = normalize_for_scoring(item["reference"])
            before = edit_counts(reference, normalize_for_scoring(text)).errors
            after = edit_counts(
                reference, normalize_for_scoring(text.replace(rule["from"], rule["to"]))
            ).errors
            net += after - before
        rows.append({"rule": number, "hits": hits, "items": items_hit, "net_error_change": net})
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("labels", nargs="+", help="variants to score, in table order")
    parser.add_argument("--work", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--baseline", help="label for paired deltas (default: first label)")
    parser.add_argument("--delta", action="append", default=[], metavar="A:B",
                        help="extra paired comparison, CER(A) minus CER(B)")
    parser.add_argument("--iterations", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=20261003)
    parser.add_argument("--dictionary", type=Path)
    parser.add_argument("--repl-base", metavar="LABEL",
                        help="un-replaced variant to measure each dictionary rule against")
    parser.add_argument("--out", type=Path, help="write the JSON report here")
    args = parser.parse_args(argv)

    items = load_answers(args.work, SERVICES)
    scorer = Scorer(items, args.iterations, args.seed)
    scopes = (*SERVICES, "pooled")
    report: dict[str, Any] = {
        "services": list(SERVICES),
        "items": {s: sum(i["service"] == s for i in items) for s in SERVICES},
        "bootstrap": {"iterations": args.iterations, "cluster": f"{int(BLOCK_S)} s blocks per service"},
        "variants": {},
        "deltas": {},
    }
    scores: dict[str, dict[bool, tuple[np.ndarray, np.ndarray]]] = {}
    hypotheses: dict[str, dict[str, str]] = {}
    for label in args.labels:
        hypotheses[label] = load_hypothesis(args.work, label, SERVICES)
        scores[label] = {}
        entry: dict[str, Any] = {"timing": timing_summary(args.work, label, SERVICES)}
        for fold in (False, True):
            errors, chars = item_errors(items, hypotheses[label], fold=fold)
            scores[label][fold] = (errors, chars)
            entry["fold_pronouns" if fold else "plain"] = {
                scope: scorer.cer(errors, chars, scope) for scope in scopes
            }
        answers = {"items": items}
        entry["duplicate_prefix_items"] = count_duplicate_prefix_items(answers, hypotheses[label])
        report["variants"][label] = entry
        print(f"scored {label}", file=sys.stderr)

    pairs = [(args.baseline or args.labels[0], label) for label in args.labels]
    pairs = [(a, b) for a, b in pairs if a != b]
    for spec in args.delta:
        first, second = spec.split(":")
        pairs.append((first, second))
    for first, second in pairs:
        # reported as second minus first: negative = `second` is better than `first`.
        key = f"{second} minus {first}"
        report["deltas"][key] = {}
        for fold in (False, True):
            name = "fold_pronouns" if fold else "plain"
            report["deltas"][key][name] = {
                scope: scorer.delta(scores[second][fold], scores[first][fold], scope)
                for scope in scopes
            }

    if args.dictionary and args.repl_base:
        report["replacement_rules"] = replacement_effects(
            items, load_hypothesis(args.work, args.repl_base, SERVICES), args.dictionary
        )

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(render_markdown(report))
    return 0


def render_markdown(report: dict[str, Any]) -> str:
    scopes = (*report["services"], "pooled")
    lines = []
    for kind, title in (("plain", "CER"), ("fold_pronouns", "CER, pronouns folded")):
        lines += [f"### {title} (%, 95% cluster-bootstrap CI)", "", "| variant | " + " | ".join(scopes) + " |",
                  "|---|" + "---:|" * len(scopes)]
        for label, entry in report["variants"].items():
            cells = []
            for scope in scopes:
                row = entry[kind][scope]
                cells.append(f"{row['cer'] * 100:.2f} [{row['ci95_low'] * 100:.2f}, {row['ci95_high'] * 100:.2f}]")
            lines.append(f"| {label} | " + " | ".join(cells) + " |")
        lines.append("")
    for kind, title in (("plain", "Paired CER delta"), ("fold_pronouns", "Paired CER delta, pronouns folded")):
        lines += [f"### {title} (percentage points; negative = better; * = CI excludes 0)", "",
                  "| comparison | " + " | ".join(scopes) + " |", "|---|" + "---:|" * len(scopes)]
        for key, entry in report["deltas"].items():
            cells = []
            for scope in scopes:
                row = entry[kind][scope]
                star = "*" if row["significant"] else ""
                cells.append(f"{row['delta_cer'] * 100:+.2f} [{row['ci95_low'] * 100:+.2f}, {row['ci95_high'] * 100:+.2f}]{star}")
            lines.append(f"| {key} | " + " | ".join(cells) + " |")
        lines.append("")
    lines += ["### Cost and carry behaviour", "", "| variant | decode RTF | decodes/item | carry used | carry fallback | duplicate-prefix items |",
              "|---|---:|---:|---:|---:|---:|"]
    for label, entry in report["variants"].items():
        t = entry["timing"]
        lines.append(f"| {label} | {t['decode_rtf']:.3f} | {t['attempts_per_item']:.2f} | {t['carry_used_rate']:.0%} | {t['carry_fallback_rate']:.0%} | {entry['duplicate_prefix_items']} |")
    if "replacement_rules" in report:
        lines += ["", "### Replacement rules (by dictionary order)", "", "| rule | hits | items | net error change |", "|---:|---:|---:|---:|"]
        for row in report["replacement_rules"]:
            lines.append(f"| {row['rule']} | {row['hits']} | {row['items']} | {row['net_error_change']:+d} |")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
