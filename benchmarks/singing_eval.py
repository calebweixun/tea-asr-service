"""Evaluate the YAMNet singing detector offline against the private labelled set.

Nothing from the private data is written into the repository: the WAVs and
answer JSON stay in the read-only ``.soak`` directory and the derived caches
(YAMNet frame scores, VAD segments) go to a git-ignored ``.soak/singing-cache``.

Labels (all private, see docs/benchmarks/singing-eval-report.md for the method):

* ``music-0123``, ``music-0934``, ``music-1339``: worship singing. Positive
  ground truth is the answer item spans; gaps (instrumental, crowd) are skipped.
* ``music-8900``: a speaker over background music; spans are speech.
* ``church-30m-59m``: a 29 minute sermon; the whole file is speech.

Everything is replayed through the same library code the server runs
(``tea_asr.singing``), with real Silero VAD segmentation, so the numbers are
the pipeline's, not a proxy's.

Run::

    PYTHONPATH=src python benchmarks/singing_eval.py            # held-out tables
    PYTHONPATH=src python benchmarks/singing_eval.py --fit      # refit the scorer
    PYTHONPATH=src python benchmarks/singing_eval.py --full-wav .soak/audio/full-2h.wav

Set ``TEA_ASR_SINGING_DATA`` or ``--data-root`` if the private set lives
elsewhere, and ``TEA_ASR_MODELS_DIR`` for the VAD and YAMNet assets.
"""

from __future__ import annotations

import argparse
import dataclasses
import itertools
import json
import math
import os
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from tea_asr.segmenter import ContinuousSegmenter, SegmentClosed, SegmenterConfig, SpeechStarted
from tea_asr.singing import (
    DEFAULT_PARAMS,
    DEFAULT_SCORER,
    ClassGroups,
    FrameScorer,
    SegmentLabeler,
    SingingParams,
    SingingTracker,
    design_matrix,
    frame_features,
)
from tea_asr.vad import SileroVad, locate_vad
from tea_asr.yamnet import (
    HOP_SAMPLES,
    PATCH_SAMPLES,
    SAMPLE_RATE,
    YamnetModel,
    complete_frames,
    load_class_names,
    locate_yamnet,
)

FILES = (
    ("music-0123", 1),
    ("music-0934", 1),
    ("music-1339", 1),
    ("music-8900", 0),
    ("church-30m-59m", 0),
)
SPEECH_FILES = tuple(name for name, label in FILES if label == 0)
WINDOW_S = 1.5
WINDOW_HOP_S = 0.5
END_SILENCE_MS = 600  # the user's OBS setting in the soak traces

# Grid used for parameter selection; the selection runs on training files only.
GRID_ENTER_FRAMES = (4, 6, 8)
GRID_ENTER_THRESHOLD = (0.5, 0.7, 0.8, 0.9, 0.95, 0.98)
GRID_LOCAL_THRESHOLD = (0.3, 0.5, 0.7, 0.9)
#: Selection rule (see ``select_params``): zero hidden training speech segments,
#: then the most conservative setting within this much recall of the best.
TRAIN_FP_BUDGET = 0.0
RECALL_SLACK = 0.03


@dataclass(slots=True)
class FileData:
    file_id: str
    truth: int
    duration_s: float
    features: np.ndarray  # [frames, 3]
    spans: list[tuple[float, float]]
    segments: list[dict[str, Any]]  # VAD segments with a `truth` field
    yamnet_ms_per_s: float


# -- data ----------------------------------------------------------------------


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as source:
        if source.getframerate() != SAMPLE_RATE or source.getnchannels() != 1:
            raise ValueError(f"{path} must be 16 kHz mono")
        return np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")


def answer_spans(data_root: Path, file_id: str) -> list[tuple[float, float]]:
    answer_id = "speakers" if file_id.startswith("church") else file_id
    path = data_root / "gold" / "answers" / f"{answer_id}-answers.json"
    answers = json.loads(path.read_text(encoding="utf-8"))
    return [
        (float(i["start_s"]), float(i["end_s"]))
        for i in answers["items"]
        if float(i["end_s"]) > float(i["start_s"])
    ]


def yamnet_scores(model: YamnetModel, audio: np.ndarray, chunk_frames: int = 1200) -> np.ndarray:
    """Frame scores for a whole file, chunked on frame boundaries."""

    waveform = audio.astype(np.float32) / 32768.0
    total = complete_frames(waveform.size)
    parts = []
    index = 0
    while index < total:
        count = min(chunk_frames, total - index)
        start = index * HOP_SAMPLES
        parts.append(
            model.scores(waveform[start : start + PATCH_SAMPLES + HOP_SAMPLES * (count - 1)])
        )
        index += count
    return np.concatenate(parts)


def vad_segments(vad: SileroVad, audio: np.ndarray) -> list[dict[str, Any]]:
    segmenter = ContinuousSegmenter(vad, SegmenterConfig(end_silence_ms=END_SILENCE_MS))
    pcm = audio.tobytes()
    out: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for offset in range(0, len(pcm), 3200):
        for event in segmenter.push(pcm[offset : offset + 3200]):
            if isinstance(event, SpeechStarted):
                current = {"start": event.start_sample, "open": segmenter.next_sample}
            elif isinstance(event, SegmentClosed):
                current = current or {"start": event.start_sample, "open": event.start_sample}
                current.update(end=event.end_sample, close=segmenter.next_sample)
                out.append(current)
                current = None
    for event in segmenter.flush():
        if isinstance(event, SegmentClosed):
            current = current or {"start": event.start_sample, "open": event.start_sample}
            current.update(end=event.end_sample, close=segmenter.next_sample)
            out.append(current)
    return out


def load_file(
    data_root: Path,
    cache_dir: Path,
    file_id: str,
    truth: int,
    model: YamnetModel,
    vad: SileroVad,
    groups: ClassGroups,
    *,
    wav_path: Path | None = None,
    labelled: bool = True,
) -> FileData:
    wav = wav_path or data_root / "audio" / f"{file_id}.wav"
    cache = cache_dir / f"{file_id}.npz"
    stat = wav.stat()
    key = f"{stat.st_size}:{int(stat.st_mtime)}"
    scores = None
    segments: list[dict[str, Any]] = []
    ms_per_s = 0.0
    duration = 0.0
    if cache.is_file():
        loaded = np.load(cache, allow_pickle=False)
        if str(loaded["key"]) == key:
            scores = loaded["scores"]
            segments = json.loads(str(loaded["segments"]))
            ms_per_s = float(loaded["ms_per_s"])
            duration = float(loaded["duration"])
    if scores is None:
        audio = read_wav(wav)
        duration = audio.size / SAMPLE_RATE
        started = time.perf_counter()
        scores = yamnet_scores(model, audio)
        ms_per_s = (time.perf_counter() - started) / duration * 1000.0
        segments = vad_segments(vad, audio)
        cache_dir.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            cache,
            key=key,
            scores=scores,
            segments=json.dumps(segments),
            ms_per_s=ms_per_s,
            duration=duration,
        )
    spans: list[tuple[float, float]] = []
    labelled_segments: list[dict[str, Any]] = []
    if labelled:
        spans = (
            [(0.0, duration)] if file_id.startswith("church") else answer_spans(data_root, file_id)
        )
        for segment in segments:
            start_s, end_s = segment["start"] / SAMPLE_RATE, segment["end"] / SAMPLE_RATE
            overlap = max(
                (max(0.0, min(end_s, b) - max(start_s, a)) for a, b in spans), default=0.0
            )
            # Segments mostly outside any answer item (crowd, instrumental) are unlabelled.
            if overlap / max(end_s - start_s, 1e-9) >= 0.5:
                labelled_segments.append({**segment, "truth": truth})
    else:
        labelled_segments = [{**segment, "truth": -1} for segment in segments]
    return FileData(
        file_id, truth, duration, frame_features(scores, groups), spans, labelled_segments, ms_per_s
    )


# -- scorer fit ------------------------------------------------------------------


def frame_labels(data: FileData) -> tuple[np.ndarray, np.ndarray]:
    """Frames whose centre lies in a labelled span, with the file's label."""

    centres = (np.arange(len(data.features)) * HOP_SAMPLES + PATCH_SAMPLES / 2) / SAMPLE_RATE
    inside = np.array([any(a <= c < b for a, b in data.spans) for c in centres])
    return inside, np.full(int(inside.sum()), data.truth, dtype=float)


def fit_scorer(files: list[FileData], l2: float = 1.0, iterations: int = 50) -> FrameScorer:
    """Class-balanced L2 logistic regression by Newton steps (NumPy only)."""

    rows, labels = [], []
    for data in files:
        inside, y = frame_labels(data)
        rows.append(design_matrix(data.features[inside]))
        labels.append(y)
    x, y = np.vstack(rows), np.concatenate(labels)
    weight = np.where(y == 1, 0.5 / max(y.sum(), 1), 0.5 / max((1 - y).sum(), 1))
    weight = weight / weight.mean()
    xb = np.hstack([x, np.ones((len(x), 1))])
    beta = np.zeros(4)
    penalty = np.eye(4) * l2
    penalty[3, 3] = 0.0
    for _ in range(iterations):
        p = 1.0 / (1.0 + np.exp(-np.clip(xb @ beta, -40, 40)))
        gradient = xb.T @ (weight * (p - y)) + penalty @ beta
        hessian = (xb * (weight * p * (1 - p))[:, None]).T @ xb + penalty + 1e-6 * np.eye(4)
        beta -= np.linalg.solve(hessian, gradient)
    return FrameScorer((float(beta[0]), float(beta[1]), float(beta[2])), float(beta[3]))


# -- replay ----------------------------------------------------------------------


@dataclass(slots=True)
class SegmentResult:
    early: str
    final: str
    truth: int
    start_s: float
    end_s: float
    decision_latency_s: float


def replay_segments(
    data: FileData, scorer: FrameScorer, params: SingingParams
) -> list[SegmentResult]:
    tracker = SingingTracker(scorer, params, history_frames=10**7)
    tracker.observe(data.features)
    results: list[SegmentResult] = []
    for segment in data.segments:
        labeler = SegmentLabeler(tracker, segment["start"])
        end = segment["end"]
        if end >= labeler.early_sample:
            labeler.update(labeler.early_sample)
            latency = params.early_decision_s
        else:
            labeler.update(end, closing=True)
            latency = (end - segment["start"]) / SAMPLE_RATE
        early = labeler.decision
        if early is None:
            raise AssertionError("frames missing for an early decision")
        early_class = early.audio_class
        if end >= labeler.revision_sample:
            labeler.update(labeler.revision_sample)
        else:
            labeler.update(end, closing=True)
        final = labeler.decision
        assert final is not None
        results.append(
            SegmentResult(
                early_class,
                final.audio_class,
                segment["truth"],
                segment["start"] / SAMPLE_RATE,
                end / SAMPLE_RATE,
                latency,
            )
        )
    return results


def replay_windows(
    data: FileData, scorer: FrameScorer, params: SingingParams
) -> list[tuple[int, str]]:
    """(truth, label) per 1.5 s window labelled by item-span midpoint."""

    tracker = SingingTracker(scorer, params, history_frames=10**7)
    tracker.observe(data.features)
    out: list[tuple[int, str]] = []
    width = round(WINDOW_S * SAMPLE_RATE)
    step = round(WINDOW_HOP_S * SAMPLE_RATE)
    total = round(data.duration_s * SAMPLE_RATE)
    for start in range(0, total - width + 1, step):
        centre = (start + width / 2) / SAMPLE_RATE
        if not any(a <= centre < b for a, b in data.spans):
            continue
        decision = tracker.evaluate(start + width)
        out.append((data.truth, decision.audio_class))
    return out


def confusion(pairs: list[tuple[int, str]]) -> tuple[int, int, int, int]:
    """(TP, FP, FN, TN) with singing as the positive class."""

    tp = sum(1 for t, p in pairs if t == 1 and p == "singing")
    fp = sum(1 for t, p in pairs if t == 0 and p == "singing")
    fn = sum(1 for t, p in pairs if t == 1 and p != "singing")
    tn = sum(1 for t, p in pairs if t == 0 and p != "singing")
    return tp, fp, fn, tn


def rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else float("nan")


# -- selection and cross-validation --------------------------------------------------


def candidate_params() -> list[SingingParams]:
    return [
        dataclasses.replace(
            DEFAULT_PARAMS, enter_frames=frames, enter_threshold=threshold, local_threshold=local
        )
        for frames, threshold, local in itertools.product(
            GRID_ENTER_FRAMES, GRID_ENTER_THRESHOLD, GRID_LOCAL_THRESHOLD
        )
    ]


def segment_stats(results: list[SegmentResult]) -> dict[str, float]:
    sing = [r for r in results if r.truth == 1]
    speech = [r for r in results if r.truth == 0]
    return {
        "recall_early": rate(sum(r.early == "singing" for r in sing), len(sing)),
        "recall_final": rate(sum(r.final == "singing" for r in sing), len(sing)),
        "fp_early": rate(sum(r.early == "singing" for r in speech), len(speech)),
        "fp_final": rate(sum(r.final == "singing" for r in speech), len(speech)),
        "fp_ever": rate(sum("singing" in (r.early, r.final) for r in speech), len(speech)),
        "n_sing": len(sing),
        "n_speech": len(speech),
    }


def select_params(train: list[FileData], scorer: FrameScorer) -> SingingParams:
    """Precision-first selection on training files only.

    Keep the settings that hide no training speech segment (early or final);
    among them accept anything within ``RECALL_SLACK`` of the best training
    recall and take the most conservative one. If nothing is clean, minimise
    the false calls instead.
    """

    scored: list[tuple[SingingParams, float, float]] = []
    for params in candidate_params():
        results = [r for data in train for r in replay_segments(data, scorer, params)]
        stats = segment_stats(results)
        recall = (stats["recall_early"] + stats["recall_final"]) / 2.0
        scored.append((params, stats["fp_ever"], recall))

    def conservativeness(params: SingingParams) -> float:
        return params.enter_threshold * params.enter_frames + params.local_threshold

    clean = [item for item in scored if item[1] <= TRAIN_FP_BUDGET]
    if not clean:
        return min(scored, key=lambda item: (item[1], -item[2]))[0]
    best_recall = max(item[2] for item in clean)
    near_best = [item for item in clean if item[2] >= best_recall - RECALL_SLACK]
    return max(near_best, key=lambda item: conservativeness(item[0]))[0]


@dataclass(slots=True)
class FoldResult:
    held_out: str
    params: SingingParams
    scorer: FrameScorer
    segments: list[SegmentResult]
    windows: list[tuple[int, str]]


def leave_one_file_out(files: list[FileData]) -> list[FoldResult]:
    folds: list[FoldResult] = []
    for held in files:
        train = [f for f in files if f.file_id != held.file_id]
        scorer = fit_scorer(train)
        params = select_params(train, scorer)
        folds.append(
            FoldResult(
                held.file_id,
                params,
                scorer,
                replay_segments(held, scorer, params),
                replay_windows(held, scorer, params),
            )
        )
    return folds


def tradeoff_curve(files: list[FileData], thresholds: tuple[float, ...]) -> list[dict[str, float]]:
    """Pooled held-out numbers as the enter threshold varies (other knobs default)."""

    scorers = {
        held.file_id: fit_scorer([f for f in files if f.file_id != held.file_id]) for held in files
    }
    rows = []
    for threshold in thresholds:
        params = dataclasses.replace(DEFAULT_PARAMS, enter_threshold=threshold)
        segments: list[SegmentResult] = []
        windows: list[tuple[int, str]] = []
        for held in files:
            segments += replay_segments(held, scorers[held.file_id], params)
            windows += replay_windows(held, scorers[held.file_id], params)
        stats = segment_stats(segments)
        tp, fp, fn, tn = confusion(windows)
        rows.append(
            {
                "threshold": threshold,
                "seg_recall_early": stats["recall_early"],
                "seg_recall_final": stats["recall_final"],
                "seg_fp_early": stats["fp_early"],
                "seg_fp_final": stats["fp_final"],
                "win_recall": rate(tp, tp + fn),
                "win_fpr": rate(fp, fp + tn),
            }
        )
    return rows


# -- cost and false calls -------------------------------------------------------------


def streaming_cost(model: YamnetModel, audio: np.ndarray) -> dict[str, float]:
    """Cost of the server's call pattern: one new frame per 0.48 s of audio."""

    waveform = audio.astype(np.float32) / 32768.0
    calls = min(400, complete_frames(waveform.size))
    times = []
    for index in range(calls):
        start = index * HOP_SAMPLES
        started = time.perf_counter()
        model.scores(waveform[start : start + PATCH_SAMPLES])
        times.append(time.perf_counter() - started)
    ms = np.asarray(times) * 1000.0
    return {
        "calls": float(calls),
        "ms_per_call_median": float(np.median(ms)),
        "ms_per_call_p95": float(np.percentile(ms, 95)),
        "ms_per_audio_second": float(ms.mean() / (HOP_SAMPLES / SAMPLE_RATE)),
    }


def false_calls(results: list[SegmentResult]) -> list[tuple[float, float, str, str]]:
    return [
        (r.start_s, r.end_s, r.early, r.final)
        for r in results
        if r.truth == 0 and "singing" in (r.early, r.final)
    ]


# -- output ----------------------------------------------------------------------------


def pct(value: float) -> str:
    return "n/a" if math.isnan(value) else f"{value * 100:.1f}%"


def build_report(files: list[FileData], model: YamnetModel, audio_probe: np.ndarray) -> str:
    lines: list[str] = []
    out = lines.append
    folds = leave_one_file_out(files)
    by_file = {f.file_id: f for f in files}

    out("### Held-out (leave-one-file-out) per-file confusion matrices\n")
    out("Each row refits the scorer and re-selects the knobs on the other four files only. ")
    out("Matrices are (TP, FP, FN, TN) with singing positive.\n")
    out(
        "| Held-out file | Knobs: enter frames / threshold / local | Segments, early | "
        "Segments, final | 1.5 s windows |"
    )
    out("|---|---|---:|---:|---:|")
    for fold in folds:
        truth = by_file[fold.held_out].truth
        early = confusion([(r.truth, r.early) for r in fold.segments])
        final = confusion([(r.truth, r.final) for r in fold.segments])
        window = confusion(fold.windows)
        p = fold.params
        out(
            f"| `{fold.held_out}` ({'singing' if truth else 'speech'}) | "
            f"{p.enter_frames} / {p.enter_threshold} / {p.local_threshold} | "
            f"{early} | {final} | {window} |"
        )

    pooled_segments = [r for fold in folds for r in fold.segments]
    pooled_windows = [w for fold in folds for w in fold.windows]
    stats = segment_stats(pooled_segments)
    tp, fp, fn, tn = confusion(pooled_windows)
    speech_windows = [w for w in pooled_windows if w[0] == 0]
    out("\n### Pooled held-out summary\n")
    out("| Metric | Value | Target |")
    out("|---|---:|---|")
    out(
        f"| Singing segments caught, early (1.5 s) | {pct(stats['recall_early'])} "
        f"({int(stats['n_sing'])} segments) | >= 80% |"
    )
    out(f"| Singing segments caught, final | {pct(stats['recall_final'])} | >= 80% |")
    out(
        f"| Speech segments hidden, early | {pct(stats['fp_early'])} "
        f"({int(stats['n_speech'])} segments) | < 1% |"
    )
    out(f"| Speech segments hidden, final | {pct(stats['fp_final'])} | < 1% |")
    out(f"| Singing windows caught | {pct(rate(tp, tp + fn))} ({tp + fn} windows) | >= 80% |")
    out(
        f"| Speech windows called singing | {pct(rate(fp, fp + tn))} "
        f"({len(speech_windows)} windows) | < 1% |"
    )
    for name in SPEECH_FILES:
        pairs = [w for fold in folds if fold.held_out == name for w in fold.windows]
        _, f_p, _, t_n = confusion(pairs)
        out(f"| ... `{name}` speech windows called singing | {pct(rate(f_p, f_p + t_n))} | < 1% |")

    out("\n### Trade-off curve (pooled held-out, scorer refit per fold, other knobs at defaults)\n")
    out(
        "| Enter threshold | Seg recall early | Seg recall final | Seg FP early | "
        "Seg FP final | Window recall | Window FPR |"
    )
    out("|---:|---:|---:|---:|---:|---:|---:|")
    for row in tradeoff_curve(files, (0.5, 0.7, 0.8, 0.9, 0.95, 0.98, 0.99)):
        out(
            f"| {row['threshold']} | {pct(row['seg_recall_early'])} | "
            f"{pct(row['seg_recall_final'])} | {pct(row['seg_fp_early'])} | "
            f"{pct(row['seg_fp_final'])} | {pct(row['win_recall'])} | {pct(row['win_fpr'])} |"
        )

    church = by_file["church-30m-59m"]
    church_fold = next(f for f in folds if f.held_out == "church-30m-59m")
    wrong = false_calls(church_fold.segments)
    seconds = sum(b - a for a, b, _, _ in wrong)
    out(
        f"\n### Full {church.duration_s / 60:.1f} minute sermon (held out)\n\n"
        f"{len(church.segments)} VAD segments; {len(wrong)} called singing at any time "
        f"({seconds:.1f} s of audio). Calls: "
        + (", ".join(f"{a:.1f}-{b:.1f} s ({e}->{f})" for a, b, e, f in wrong) or "none")
        + "."
    )

    latencies = np.asarray([r.decision_latency_s for r in pooled_segments])
    out("\n### Decision latency\n")
    out(
        f"First label {np.median(latencies):.2f} s after the segment's first sample (median; "
        f"p95 {np.percentile(latencies, 95):.2f} s, max {latencies.max():.2f} s) over "
        f"{len(latencies)} segments; segments shorter than 1.5 s are decided at close. The "
        "evidence is the YAMNet frames completed by then (the newest ends up to 0.48 s before "
        "the decision point) plus the session history."
    )

    out("\n### CPU cost (onnxruntime CPU, 1 intra-op thread)\n")
    out("| File | Audio | Batch ms per audio-second |")
    out("|---|---:|---:|")
    for data in files:
        out(f"| `{data.file_id}` | {data.duration_s:.0f} s | {data.yamnet_ms_per_s:.2f} |")
    cost = streaming_cost(model, audio_probe)
    out(
        f"\nServer call pattern (one 0.975 s patch per call): median "
        f"{cost['ms_per_call_median']:.2f} ms, p95 {cost['ms_per_call_p95']:.2f} ms per call, "
        f"{cost['ms_per_audio_second']:.1f} ms of CPU per second of audio over "
        f"{int(cost['calls'])} calls."
    )

    final_scorer = fit_scorer(files)
    final_params = select_params(files, final_scorer)
    out("\n### Deployed setting (fit on all labelled files, in-sample)\n")
    out(
        "- Scorer coefficients (speech, music, vocal): "
        f"{tuple(round(c, 4) for c in final_scorer.coefficients)}, "
        f"intercept {final_scorer.intercept:.4f}"
    )
    out(f"- Knobs selected on all data: {final_params}")
    results = [r for data in files for r in replay_segments(data, final_scorer, final_params)]
    stats_all = segment_stats(results)
    out(
        f"- In-sample segments: singing caught {pct(stats_all['recall_early'])} early / "
        f"{pct(stats_all['recall_final'])} final, speech hidden {pct(stats_all['fp_early'])} early / "
        f"{pct(stats_all['fp_final'])} final (optimistic by construction; use the held-out rows)."
    )
    return "\n".join(lines)


def build_full_recording_report(files: list[FileData], full: FileData) -> str:
    scorer = fit_scorer(files)
    params = select_params(files, scorer)
    tracker = SingingTracker(scorer, params, history_frames=10**7)
    tracker.observe(full.features)
    states = np.array([tracker.state_after(i + 1) for i in range(tracker.frames_seen)])
    ends = (np.arange(tracker.frames_seen) * HOP_SAMPLES + PATCH_SAMPLES) / SAMPLE_RATE
    changes = np.flatnonzero(np.diff(states.astype(int)))
    stamps = ", ".join(
        f"{ends[i + 1] / 60:.2f} {'on' if states[i + 1] else 'off'}" for i in changes
    )
    results = replay_segments(full, scorer, params)
    singing = sum(r.final == "singing" for r in results)
    return (
        "### Unlabelled full recording (state timeline, no accuracy claim)\n\n"
        f"{full.duration_s / 60:.1f} minutes, {len(full.segments)} VAD segments, {singing} "
        f"called singing at the end. Singing state changes (minute, new state): {stamps}."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path(os.environ.get("TEA_ASR_SINGING_DATA", ".soak")),
        help="private .soak directory holding audio/ and gold/answers/",
    )
    parser.add_argument("--cache-dir", type=Path, default=Path(".soak/singing-cache"))
    parser.add_argument("--yamnet-models-dir", type=Path, help="models cache holding YAMNet")
    parser.add_argument("--vad-models-dir", type=Path, help="models cache holding Silero VAD")
    parser.add_argument("--fit", action="store_true", help="print the scorer fitted on all files")
    parser.add_argument("--full-wav", type=Path, help="unlabelled recording for a state timeline")
    parser.add_argument("--markdown", type=Path, help="also write the tables to this file")
    args = parser.parse_args()

    model_path, class_map_path = locate_yamnet(args.yamnet_models_dir)
    model = YamnetModel(model_path)
    groups = ClassGroups.from_names(load_class_names(class_map_path))
    vad = SileroVad(locate_vad(args.vad_models_dir))

    files = [
        load_file(args.data_root, args.cache_dir, name, truth, model, vad, groups)
        for name, truth in FILES
    ]
    if args.fit:
        scorer = fit_scorer(files)
        rounded = tuple(round(c, 4) for c in scorer.coefficients)
        print(f"FrameScorer(coefficients={rounded}, intercept={scorer.intercept:.4f})")
        print(f"(shipped: {DEFAULT_SCORER})")
        return
    probe = read_wav(args.data_root / "audio" / "church-30m-59m.wav")[: 60 * SAMPLE_RATE]
    report = build_report(files, model, probe)
    if args.full_wav:
        full = load_file(
            args.data_root,
            args.cache_dir,
            "full-recording",
            -1,
            model,
            vad,
            groups,
            wav_path=args.full_wav,
            labelled=False,
        )
        report += "\n\n" + build_full_recording_report(files, full)
    print(report)
    if args.markdown:
        args.markdown.write_text(report + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
