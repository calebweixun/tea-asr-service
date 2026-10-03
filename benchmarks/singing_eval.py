"""Fit and evaluate the streaming singing detector without copying private data.

The WAVs and answer JSON remain in the user's read-only ``.soak`` directory.
Window labels come from item-span midpoints; the full church WAV is speech.
Cross-validation leaves out one complete WAV at a time. The model has eight
hand-inspectable audio features and is fitted with NumPy only.

Run with::

    PYTHONPATH=src python benchmarks/singing_eval.py

Set ``TEA_ASR_SINGING_DATA`` or pass ``--data-root`` to locate the private set.
"""

from __future__ import annotations

import argparse
import json
import os
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from tea_asr.singing import (
    FEATURE_NAMES,
    SAMPLE_RATE,
    StreamingSingingClassifier,
    extract_features,
)

FILES = (
    ("music-0123", "singing"),
    ("music-0934", "singing"),
    ("music-1339", "singing"),
    ("music-8900", "speech"),
    ("church-30m-59m", "speech"),
)
WINDOW_S = 1.5
WINDOW_HOP_S = 0.5
SEGMENT_S = 2.0
CHUNK_BYTES = round(0.2 * SAMPLE_RATE) * 2


@dataclass(frozen=True, slots=True)
class Sample:
    file_id: str
    label: int
    start_s: float
    duration_s: float
    features: np.ndarray


@dataclass(frozen=True, slots=True)
class Model:
    mean: np.ndarray
    scale: np.ndarray
    coefficients: np.ndarray
    intercept: float

    def predict(self, features: np.ndarray) -> np.ndarray:
        standardized = (features - self.mean) / self.scale
        logits = np.clip(standardized @ self.coefficients + self.intercept, -40.0, 40.0)
        return 1.0 / (1.0 + np.exp(-logits))


def _read_audio(data_root: Path, file_id: str) -> tuple[np.ndarray, list[tuple[float, float]]]:
    audio_path = data_root / "audio" / f"{file_id}.wav"
    answer_id = "speakers" if file_id == "church-30m-59m" else file_id
    answers_path = data_root / "gold" / "answers" / f"{answer_id}-answers.json"
    with wave.open(str(audio_path), "rb") as source:
        if source.getframerate() != SAMPLE_RATE or source.getnchannels() != 1:
            raise ValueError(f"{audio_path} must be 16 kHz mono")
        audio = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")
        duration_s = source.getnframes() / source.getframerate()
    answers = json.loads(answers_path.read_text(encoding="utf-8"))
    spans = [
        (float(item["start_s"]), float(item["end_s"]))
        for item in answers.get("items", [])
        if float(item["end_s"]) > float(item["start_s"])
    ]
    if file_id == "church-30m-59m":
        # Per task instruction the entire 29-minute source is speech, even
        # though human transcript spans are provided for only its first 6 min.
        spans = [(0.0, duration_s)]
    return audio, spans


def _span_label(center_s: float, spans: list[tuple[float, float]]) -> bool:
    return any(start <= center_s < end for start, end in spans)


def _extract_samples(
    file_id: str,
    label: str,
    audio_i16: np.ndarray,
    spans: list[tuple[float, float]],
    *,
    duration_s: float,
    hop_s: float,
) -> list[Sample]:
    width = round(duration_s * SAMPLE_RATE)
    step = max(1, round(hop_s * SAMPLE_RATE))
    output: list[Sample] = []
    for start in range(0, audio_i16.size - width + 1, step):
        center_s = (start + width / 2) / SAMPLE_RATE
        in_span = _span_label(center_s, spans)
        # In the worship/speech-over-music files, unlabeled instrumental or
        # transition time is excluded instead of being guessed as speech.
        if not in_span:
            continue
        clip = audio_i16[start : start + width].astype(np.float64) / 32768.0
        features = extract_features(clip).as_array()
        output.append(
            Sample(
                file_id=file_id,
                label=int(label == "singing"),
                start_s=start / SAMPLE_RATE,
                duration_s=duration_s,
                features=features,
            )
        )
    return output


def _fit_logistic(features: np.ndarray, labels: np.ndarray, file_ids: np.ndarray) -> Model:
    """Fit a small L2 logistic regression, weighting each file equally."""

    labels = labels.astype(np.float64)
    row_weights = np.zeros(labels.size, dtype=np.float64)
    for class_value in (0, 1):
        class_files = sorted(set(file_ids[labels == class_value]))
        if not class_files:
            continue
        for file_id in class_files:
            mask = (labels == class_value) & (file_ids == file_id)
            row_weights[mask] = 1.0 / (len(class_files) * int(mask.sum()))
        row_weights[labels == class_value] *= 0.5

    mean = np.sum(features * row_weights[:, None], axis=0) / max(float(row_weights.sum()), 1e-12)
    variance = np.sum(((features - mean) ** 2) * row_weights[:, None], axis=0)
    scale = np.sqrt(np.maximum(variance / max(float(row_weights.sum()), 1e-12), 1e-6))
    standardized = (features - mean) / scale
    design = np.column_stack((np.ones(labels.size), standardized))
    parameters = np.zeros(design.shape[1], dtype=np.float64)
    regularization = 0.08
    for _ in range(3_000):
        logits = np.clip(design @ parameters, -30.0, 30.0)
        probability = 1.0 / (1.0 + np.exp(-logits))
        gradient = design.T @ (row_weights * (probability - labels))
        gradient[1:] += regularization * parameters[1:]
        parameters -= 0.35 * gradient
    return Model(mean, scale, parameters[1:], float(parameters[0]))


def _matrix(labels: np.ndarray, scores: np.ndarray, threshold: float) -> tuple[int, int, int, int]:
    predicted = scores >= threshold
    actual = labels.astype(bool)
    return (
        int(np.sum(predicted & actual)),
        int(np.sum(predicted & ~actual)),
        int(np.sum(~predicted & actual)),
        int(np.sum(~predicted & ~actual)),
    )


def _metrics(matrix: tuple[int, int, int, int]) -> tuple[float, float, float]:
    tp, fp, fn, tn = matrix
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    fpr = fp / (fp + tn) if fp + tn else 0.0
    return precision, recall, fpr


def _threshold_choice(
    labels: np.ndarray,
    scores: np.ndarray,
    file_ids: np.ndarray,
) -> tuple[float, bool, list[tuple[float, float, float, float]]]:
    candidate_thresholds = sorted({0.5, 0.75, 0.85, 0.9, 0.95, 0.98, 0.99, 0.995, *scores.tolist()})
    curve: list[tuple[float, float, float, float]] = []
    speech_files = sorted(set(file_ids[labels == 0]))
    singing_mask = labels == 1
    best: tuple[float, float, float] | None = None
    chosen = 0.995
    targets_met = False
    for threshold in candidate_thresholds:
        predicted = scores >= threshold
        class_precision, _class_recall, _ = _metrics(_matrix(labels, scores, threshold))
        speech_fprs = []
        for file_id in speech_files:
            mask = (file_ids == file_id) & (labels == 0)
            _, _, fpr = _metrics(_matrix(labels[mask], scores[mask], threshold))
            speech_fprs.append(fpr)
        worst_fpr = max(speech_fprs, default=0.0)
        song_recall = (
            float(np.mean(predicted[singing_mask])) if np.any(singing_mask) else 0.0
        )
        curve.append((float(threshold), class_precision, song_recall, worst_fpr))
        eligible = worst_fpr < 0.01 and song_recall >= 0.80
        rank = (class_precision, song_recall, -threshold)
        if eligible and (best is None or rank > best):
            best = rank
            chosen = float(threshold)
            targets_met = True
    if best is None:
        # If the requested operating point is unattainable, minimize speech
        # false positives first, then maximize singing recall.
        chosen = min(curve, key=lambda row: (row[3], -row[2], -row[1]))[0]
    return chosen, targets_met, curve


def _simulate_latency(pcm: bytes, threshold: float) -> float:
    classifier = StreamingSingingClassifier(0, threshold=threshold)
    first_decision_s: float | None = None
    for start_byte in range(0, len(pcm), CHUNK_BYTES):
        chunk = pcm[start_byte : start_byte + CHUNK_BYTES]
        classifier.append(chunk, start_byte // 2)
        if classifier.decision is not None:
            first_decision_s = classifier.elapsed_s
            break
    if first_decision_s is None:
        classifier.finish()
        first_decision_s = classifier.elapsed_s
    return first_decision_s


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path(os.environ.get("TEA_ASR_SINGING_DATA", str(Path.home() / "Codes/tea-asr-service/.soak"))),
    )
    args = parser.parse_args()

    window_samples: list[Sample] = []
    segment_samples: list[Sample] = []
    loaded: dict[str, tuple[np.ndarray, list[tuple[float, float]], str]] = {}
    for file_id, label in FILES:
        audio, spans = _read_audio(args.data_root, file_id)
        loaded[file_id] = (audio, spans, label)
        window_samples.extend(
            _extract_samples(
                file_id, label, audio, spans, duration_s=WINDOW_S, hop_s=WINDOW_HOP_S
            )
        )
        segment_samples.extend(
            _extract_samples(
                file_id, label, audio, spans, duration_s=SEGMENT_S, hop_s=SEGMENT_S
            )
        )

    x = np.stack([sample.features for sample in window_samples])
    y = np.asarray([sample.label for sample in window_samples], dtype=np.int64)
    files = np.asarray([sample.file_id for sample in window_samples])
    oof_scores = np.zeros(y.size, dtype=np.float64)
    oof_segment_scores = np.zeros(len(segment_samples), dtype=np.float64)
    segment_labels = np.asarray([sample.label for sample in segment_samples], dtype=np.int64)
    segment_files = np.asarray([sample.file_id for sample in segment_samples])

    print("LOFO window-level confusion (TP FP FN TN), 1.5 s windows / 0.5 s hop")
    for held_out in sorted(set(files)):
        train = files != held_out
        test = ~train
        model = _fit_logistic(x[train], y[train], files[train])
        oof_scores[test] = model.predict(x[test])
        segment_mask = segment_files == held_out
        if np.any(segment_mask):
            segment_x = np.stack([sample.features for sample in segment_samples if sample.file_id == held_out])
            oof_segment_scores[segment_mask] = model.predict(segment_x)
        matrix = _matrix(y[test], oof_scores[test], 0.9)
        precision, recall, fpr = _metrics(matrix)
        print(
            f"  {held_out}: {matrix}  precision={precision:.3f} recall={recall:.3f} FPR={fpr:.3%}"
        )

    threshold, targets_met, curve = _threshold_choice(y, oof_scores, files)
    print("\nLOFO trade-off curve (threshold, singing precision, singing recall, worst speech FPR)")
    displayed = {0.5, 0.75, 0.85, 0.9, 0.95, 0.98, 0.99, 0.995, threshold}
    for row in curve:
        if row[0] in displayed:
            print(f"  {row[0]:.4f}  {row[1]:.3f}  {row[2]:.3f}  {row[3]:.3%}")
    print(f"Chosen threshold={threshold:.6f}; targets_met={targets_met}")

    print("\nLOFO window-level confusion at chosen threshold (TP FP FN TN)")
    for file_id in sorted(set(files)):
        mask = files == file_id
        matrix = _matrix(y[mask], oof_scores[mask], threshold)
        precision, recall, fpr = _metrics(matrix)
        print(f"  {file_id}: {matrix}  P={precision:.3f} R={recall:.3f} FPR={fpr:.3%}")

    print("\nLOFO segment-level confusion (TP FP FN TN), 2 s non-overlapping clips")
    for file_id in sorted(set(segment_files)):
        mask = segment_files == file_id
        matrix = _matrix(segment_labels[mask], oof_segment_scores[mask], threshold)
        precision, recall, fpr = _metrics(matrix)
        print(f"  {file_id}: {matrix}  P={precision:.3f} R={recall:.3f} FPR={fpr:.3%}")

    print("\nPer-file latency to first class decision, 200 ms streaming updates")
    for file_id, (audio, spans, label) in loaded.items():
        durations: list[float] = []
        if file_id == "church-30m-59m":
            for start in range(0, audio.size, round(5.0 * SAMPLE_RATE)):
                end = min(audio.size, start + round(5.0 * SAMPLE_RATE))
                clip = audio[start:end].astype("<i2", copy=False).tobytes()
                durations.append(_simulate_latency(clip, threshold))
        else:
            for start_s, end_s in spans:
                start = max(0, round(start_s * SAMPLE_RATE))
                end = min(audio.size, round(end_s * SAMPLE_RATE))
                if end > start:
                    durations.append(
                        _simulate_latency(audio[start:end].astype("<i2", copy=False).tobytes(), threshold)
                    )
        if durations:
            print(
                f"  {file_id}: n={len(durations)} median={np.median(durations):.2f}s "
                f"p95={np.percentile(durations, 95):.2f}s"
            )

    church_mask = (files == "church-30m-59m") & (y == 0)
    church_positive = oof_scores[church_mask] >= threshold
    church_count = int(np.sum(church_positive))
    # Treat positives separated by more than one window as separate calls.
    # Report both the call count and total hidden-window time.
    church_starts = np.asarray(
        [sample.start_s for sample in window_samples if sample.file_id == "church-30m-59m"]
    )
    positive_starts = church_starts[church_positive]
    calls = 0
    previous: float | None = None
    for start_s in positive_starts:
        if previous is None or start_s - previous > WINDOW_S:
            calls += 1
        previous = start_s
    print(
        f"\nChurch sermon LOFO false singing: {calls} contiguous calls, "
        f"{church_count} positive overlapping windows (~{church_count * WINDOW_HOP_S:.1f} window-seconds) "
        f"in 29 minutes"
    )

    final_model = _fit_logistic(x, y, files)
    print("\nFit constants for src/tea_asr/singing.py")
    for name, value in (
        ("MODEL_MEAN", final_model.mean),
        ("MODEL_SCALE", final_model.scale),
        ("MODEL_COEFFICIENTS", final_model.coefficients),
    ):
        print(f"{name} = np.asarray({np.round(value, 8).tolist()!r})")
    print(f"MODEL_INTERCEPT = {final_model.intercept:.8f}")
    print("FEATURE_NAMES =", ", ".join(FEATURE_NAMES))
    print("SUMMARY targets_met=", targets_met, "threshold=", threshold)


if __name__ == "__main__":
    main()
