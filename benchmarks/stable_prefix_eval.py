"""Measure append-only stable prefixes (LocalAgreement-n) on the revisable replay.

Two stages, so the model runs once and every variant is analysed from the same
recognitions:

    # 1. recognize: the docs/07 replay (800 ms prefixes, then the whole) on the
    #    Taiwan-Tongues zh-TW test shard, single clips and 3-clip concatenations
    TEA_ASR_MODELS_DIR=... uv run python benchmarks/stable_prefix_eval.py collect \
        --out benchmarks/results/stable_replay.json
    # 2. analyse: flicker of today's partials, lock latency and divergence of
    #    the stable prefix for n=2/3, raw vs production cut rules
    uv run python benchmarks/stable_prefix_eval.py analyze \
        benchmarks/results/stable_replay.json

The replay is `p0_revisable.replay_prefixes`; the corpus loader and CER rules
are `quality_eval`'s; the stable prefix is the production
`tea_asr.stable.StablePrefixTracker`. PUA characters are filtered exactly as the
service does before any text is compared (`filter_pua` is on by default).

Timing model (documented in docs/benchmarks/stable-prefix-report.md): a
preview over audio [0, e) is published at ``e + inference``; the final at
``duration + 0.9 s endpoint silence + inference``. Queueing behind other
sessions is not modelled.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
import unicodedata
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

from quality_eval import (
    DATASET_ID,
    DATASET_REVISION,
    chinese_chars,
    edit_distance,
    load_samples,
    normalize,
)

from tea_asr.api.stream import filter_private_use_characters
from tea_asr.stable import StablePrefixTracker, common_prefix_length

STEP_SAMPLES = 12_800  # docs/07 / stream.py PREVIEW_MIN_AUDIO_SAMPLES
ENDPOINT_WAIT_S = 0.9  # stream.py REVISABLE_END_SILENCE_MS
GAP_SAMPLES = 4_800  # 300 ms between concatenated clips: below the 500 ms endpoint
MAX_JOINED_SAMPLES = 14 * 16_000  # continuous segment cap (12 s + 2 s grace)


def collect(args: argparse.Namespace) -> int:
    from p0_revisable import replay_prefixes
    from quality_eval import decode_mp3

    from tea_asr.backend import TeaMlxBackend
    from tea_asr.model_manager import locate_prepared_model
    from tea_asr.model_spec import TEA_ASR_1_1_MLX_4BIT

    samples = load_samples(args.limit)
    backend = TeaMlxBackend(locate_prepared_model(TEA_ASR_1_1_MLX_4BIT))
    backend.load()
    audio = [decode_mp3(sample.audio) for sample in samples]

    items: list[dict] = []
    started = time.perf_counter()

    def run(kind: str, keys: list[str], reference: str, pcm: np.ndarray) -> None:
        runs = replay_prefixes(backend, pcm, STEP_SAMPLES)
        items.append(
            {
                "kind": kind,
                "keys": keys,
                "reference": reference,
                "samples": int(pcm.size),
                "runs": [
                    {"end_sample": r["end_sample"], "text": r["text"], "infer_s": r["total_time_s"]}
                    for r in runs
                ],
            }
        )
        if len(items) % 100 == 0:
            print(f"  {len(items)} items, {time.perf_counter() - started:.0f}s", flush=True)

    for sample, pcm in zip(samples, audio, strict=True):
        run("single", [sample.key], sample.reference, pcm)
    gap = np.zeros(GAP_SAMPLES, dtype=np.float32)
    for start in range(0, len(samples) - 2, 3):
        group = range(start, start + 3)
        pcm = np.concatenate([part for i in group for part in (audio[i], gap)][:-1])
        if pcm.size > MAX_JOINED_SAMPLES:
            continue
        run(
            "joined3",
            [samples[i].key for i in group],
            "".join(samples[i].reference for i in group),
            pcm,
        )

    report = {
        "environment": {
            "machine": platform.machine(),
            "macos": platform.mac_ver()[0],
            "model": TEA_ASR_1_1_MLX_4BIT.repo_id,
            "revision": TEA_ASR_1_1_MLX_4BIT.revision,
            "dataset": DATASET_ID,
            "dataset_revision": DATASET_REVISION,
            "step_samples": STEP_SAMPLES,
            "gap_samples": GAP_SAMPLES,
            "wall_s": round(time.perf_counter() - started, 1),
        },
        "items": items,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False) + "\n")
    print(f"寫入 {args.out}（{len(items)} 筆）")
    backend.close()
    return 0


# -- analysis -----------------------------------------------------------------


def soft_stripped(text: str) -> str:
    """Drop punctuation and whitespace, to separate `。→，` flips from real rewrites."""

    return "".join(
        c for c in text if not (c.isspace() or unicodedata.category(c)[0] in {"P", "Z"})
    )


class RawAgreement:
    """LocalAgreement-n on code points with no cut rules, for comparison."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.history: list[str] = []
        self.text = ""

    def observe(self, hypothesis: str) -> None:
        self.history = [*self.history, hypothesis][-self.n :]
        if len(self.history) < self.n:
            return
        if any(not h.startswith(self.text) for h in self.history):
            return
        length = common_prefix_length(self.history)
        if length > len(self.text):
            self.text = self.history[0][:length]

    def finalize(self, final: str) -> tuple[str, str, int]:
        if final.startswith(self.text):
            return final, "final", 0
        shared = common_prefix_length((self.text, final))
        return self.text + final[len(self.text) :], "diverged", len(self.text) - shared


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


def cer(pairs: list[tuple[str, str]]) -> float:
    errors = length = 0
    for reference, hypothesis in pairs:
        ref, hyp = chinese_chars(normalize(reference)), chinese_chars(normalize(hypothesis))
        errors += edit_distance(ref, hyp)
        length += len(ref)
    return errors / length if length else 0.0


def cer_delta_ci(
    subtitle: list[tuple[str, str]], final: list[tuple[str, str]], rounds: int = 2000
) -> list[float]:
    """Paired bootstrap 95% CI of subtitle CER minus final CER (seed 0)."""

    rows = []
    for (reference, hyp_a), (_, hyp_b) in zip(subtitle, final, strict=True):
        ref = chinese_chars(normalize(reference))
        rows.append(
            (
                edit_distance(ref, chinese_chars(normalize(hyp_a))),
                edit_distance(ref, chinese_chars(normalize(hyp_b))),
                len(ref),
            )
        )
    table = np.array(rows, dtype=float)
    rng = np.random.default_rng(0)
    deltas = []
    for _ in range(rounds):
        pick = table[rng.integers(0, len(table), len(table))]
        deltas.append((pick[:, 0].sum() - pick[:, 1].sum()) / pick[:, 2].sum())
    low, high = np.percentile(deltas, [2.5, 97.5])
    return [round(float(low), 4), round(float(high), 4)]


def flicker(items: list[dict], strip: bool) -> dict:
    transitions = flicks = final_transitions = final_flicks = 0
    rewritten_total = 0
    segments_with = 0
    for item in items:
        texts = [r["text"] for r in item["runs"]]
        if strip:
            texts = [soft_stripped(t) for t in texts]
        rewrote = False
        for index in range(1, len(texts)):
            previous, current = texts[index - 1], texts[index]
            if not previous:
                continue
            lost = len(previous) - common_prefix_length((previous, current))
            is_final = index == len(texts) - 1
            if is_final:
                final_transitions += 1
                final_flicks += lost > 0
            else:
                transitions += 1
                flicks += lost > 0
            rewritten_total += lost
            rewrote = rewrote or lost > 0
        segments_with += rewrote
    return {
        "partial_to_partial": transitions,
        "partial_to_partial_rewrite_rate": flicks / transitions if transitions else 0.0,
        "last_partial_to_final": final_transitions,
        "last_partial_to_final_rewrite_rate": final_flicks / final_transitions
        if final_transitions
        else 0.0,
        "all_transition_rewrite_rate": (flicks + final_flicks) / (transitions + final_transitions),
        "segments_with_rewrite_rate": segments_with / len(items),
        "rewritten_chars_per_segment": rewritten_total / len(items),
        "rewritten_chars_per_rewrite": rewritten_total / max(1, flicks + final_flicks),
    }


def first_seen(partials: list[tuple[float, str]], target: str, index: int) -> float | None:
    for at, text in partials:
        if common_prefix_length((text, target)) > index:
            return at
    return None


def agreement(items: list[dict], n: int, rules: str) -> dict:
    lat_all: list[float] = []
    lat_pre: list[float] = []
    lat_final_only: list[float] = []
    chars_pre = chars_final = chars_unseen = 0
    committed_segments = diverged_segments = content_diverged = 0
    diverged_pairs: list[tuple[str, str]] = []
    diverged_final_pairs: list[tuple[str, str]] = []
    diverged_chars: list[int] = []
    committed_chars_total = 0
    subtitle_pairs: list[tuple[str, str]] = []
    final_pairs: list[tuple[str, str]] = []
    coverage: list[float] = []
    first_stable_delay: list[float] = []

    for item in items:
        runs = item["runs"]
        partials = [
            (r["end_sample"] / 16_000 + r["infer_s"], r["text"]) for r in runs[:-1]
        ]
        final = runs[-1]["text"]
        final_at = item["samples"] / 16_000 + ENDPOINT_WAIT_S + runs[-1]["infer_s"]
        tracker = StablePrefixTracker(n) if rules == "safe" else RawAgreement(n)
        commit_at: list[float] = []
        for at, text in partials:
            before = len(tracker.text)
            tracker.observe(text)
            commit_at.extend([at] * (len(tracker.text) - before))
        committed = tracker.text
        if rules == "safe":
            outcome = tracker.finalize(final)
            terminal, state, wrong = outcome.text, outcome.state, outcome.diverged_chars
        else:
            terminal, state, wrong = tracker.finalize(final)

        if committed:
            committed_segments += 1
            committed_chars_total += len(committed)
            if partials and partials[0][1]:
                seen = first_seen(partials, committed, 0)
                if seen is not None:
                    first_stable_delay.append(commit_at[0] - seen)
        if state == "diverged":
            diverged_segments += 1
            diverged_chars.append(wrong)
            # Punctuation-only: the final merely inserted/changed a 、，《 etc.
            content_diverged += not soft_stripped(final).startswith(soft_stripped(committed))
            diverged_pairs.append((item["reference"], terminal))
            diverged_final_pairs.append((item["reference"], final))
        if final:
            coverage.append(min(len(committed), len(final)) / len(final))

        for index in range(len(committed)):
            seen = first_seen(partials, committed, index)
            if seen is None:
                continue
            lat_pre.append(commit_at[index] - seen)
            lat_all.append(commit_at[index] - seen)
            lat_final_only.append(final_at - seen)
            chars_pre += 1
        if state == "final":
            for index in range(len(committed), len(final)):
                seen = first_seen(partials, final, index)
                if seen is None:
                    chars_unseen += 1
                    continue
                lat_all.append(final_at - seen)
                lat_final_only.append(final_at - seen)
                chars_final += 1
        subtitle_pairs.append((item["reference"], terminal))
        final_pairs.append((item["reference"], final))

    total = len(items)
    return {
        "segments": total,
        "segments_with_commit_before_final": committed_segments,
        "diverged_segments": diverged_segments,
        "divergence_rate_all": diverged_segments / total,
        "divergence_rate_committed": diverged_segments / committed_segments
        if committed_segments
        else 0.0,
        "content_diverged_segments": content_diverged,
        "content_divergence_rate_all": content_diverged / total,
        "diverged_subtitle_cer": cer(diverged_pairs),
        "diverged_final_cer": cer(diverged_final_pairs),
        "diverged_chars_mean": statistics.fmean(diverged_chars) if diverged_chars else 0.0,
        "diverged_chars_max": max(diverged_chars, default=0),
        "wrong_committed_char_rate": sum(diverged_chars) / committed_chars_total
        if committed_chars_total
        else 0.0,
        "lock_latency_mean_s": statistics.fmean(lat_all) if lat_all else None,
        "lock_latency_p50_s": quantile(lat_all, 0.5),
        "lock_latency_p95_s": quantile(lat_all, 0.95),
        "lock_latency_pre_final_mean_s": statistics.fmean(lat_pre) if lat_pre else None,
        "lock_latency_pre_final_p95_s": quantile(lat_pre, 0.95),
        "final_only_latency_mean_s": statistics.fmean(lat_final_only) if lat_final_only else None,
        "final_only_latency_p95_s": quantile(lat_final_only, 0.95),
        "chars_committed_before_final": chars_pre,
        "chars_committed_at_final": chars_final,
        "chars_never_in_a_partial": chars_unseen,
        "share_committed_before_final": chars_pre / max(1, chars_pre + chars_final),
        "coverage_at_final_mean": statistics.fmean(coverage) if coverage else 0.0,
        "first_stable_delay_mean_s": statistics.fmean(first_stable_delay)
        if first_stable_delay
        else None,
        "first_stable_delay_p95_s": quantile(first_stable_delay, 0.95),
        "subtitle_cer": cer(subtitle_pairs),
        "final_cer": cer(final_pairs),
        "subtitle_minus_final_cer_ci95": cer_delta_ci(subtitle_pairs, final_pairs),
    }


def filtered(items: list[dict]) -> list[dict]:
    for item in items:
        for run in item["runs"]:
            run["text"] = filter_private_use_characters(run["text"])
    return items


def analyze(args: argparse.Namespace) -> int:
    report = json.loads(args.replay.read_text())
    items = filtered(report["items"])
    result: dict = {"environment": report["environment"], "corpora": {}}
    for kind in ("single", "joined3"):
        subset = [item for item in items if item["kind"] == kind]
        if not subset:
            continue
        audio_s = sum(item["samples"] for item in subset) / 16_000
        previews = sum(len(item["runs"]) - 1 for item in subset)
        result["corpora"][kind] = {
            "items": len(subset),
            "audio_min": round(audio_s / 60, 1),
            "mean_s": round(audio_s / len(subset), 2),
            "previews": previews,
            "flicker": flicker(subset, strip=False),
            "flicker_ignoring_punctuation": flicker(subset, strip=True),
            "agreement": {
                f"n{n}_{rules}": agreement(subset, n, rules)
                for n in (2, 3)
                for rules in ("raw", "safe")
            },
        }
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    print(encoded)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded + "\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("collect")
    run.add_argument("--limit", type=int, default=1317)
    run.add_argument("--out", type=Path, default=Path("benchmarks/results/stable_replay.json"))
    summary = commands.add_parser("analyze")
    summary.add_argument("replay", type=Path)
    summary.add_argument("--out", type=Path)
    args = parser.parse_args()
    return collect(args) if args.command == "collect" else analyze(args)


if __name__ == "__main__":
    raise SystemExit(main())
