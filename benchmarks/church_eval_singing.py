"""Singing detection over full church services, with the captions as ground truth.

* VAD segments that are mostly covered by caption cues are SPEECH (reliable: a human
  corrected them).  Any singing call on those is a false call, and the number that matters
  is false calls per captioned hour.
* Long stretches without captions (``church_eval_gold``'s "uncaptioned" spans) are only
  *likely* singing: the user captions only what needs captions.  They give a recall-like
  coverage number that must be read with that caveat.

The same library code as the server (``tea_asr.singing``) runs on cached YAMNet frame
scores and Silero VAD segments, so labels equal the live pipeline's.  Only metrics are
printed; ``--list-spans`` and ``--sample`` print private timestamps for local review.

    TEA_ASR_MODELS_DIR=<models> HF_HUB_OFFLINE=1 PYTHONPATH=src:. \\
        python benchmarks/church_eval_singing.py --out <work>/singing/metrics.json
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np

from benchmarks import singing_eval
from tea_asr.singing import (
    DEFAULT_PARAMS,
    DEFAULT_SCORER,
    ClassGroups,
    SegmentLabeler,
    SingingParams,
    SingingTracker,
    frame_features,
)
from tea_asr.vad import SileroVad, locate_vad
from tea_asr.yamnet import (
    HOP_SAMPLES,
    PATCH_SAMPLES,
    SAMPLE_RATE,
    YamnetModel,
    load_class_names,
    locate_yamnet,
)

DEFAULT_WORK = Path("/Users/c2leb/Codes/tea-asr-service/.soak/church-eval/work")
SERVICES = ("20260704", "20260822", "20260912")
#: Mean YAMNet speech feature at or above which an uncaptioned segment counts as talking.
SPEECH_LIKE_SCORE = 0.5


def overlap(a0: float, a1: float, spans: list[tuple[float, float]]) -> float:
    return sum(max(0.0, min(a1, b) - max(a0, a)) for a, b in spans)


def merge(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


class Service:
    def __init__(self, name: str, work: Path, model: YamnetModel, vad: SileroVad,
                 groups: ClassGroups, end_silence_ms: int, cache: Path) -> None:
        self.name = name
        self.work = work
        raw = json.loads((work / "answers" / f"{name}-raw.json").read_text("utf-8"))
        self.speech = merge([(i["start_s"], i["end_s"]) for i in raw["items"]])
        sidecar = json.loads((work / "answers" / f"{name}-uncaptioned.json").read_text("utf-8"))
        self.unc = [(s["start_s"], s["end_s"]) for s in sidecar["spans"]]
        self.duration_s = float(sidecar["audio_duration_s"])
        self.end_silence_ms = end_silence_ms
        audio_path = work / f"{name}.wav"
        key = f"{audio_path.stat().st_size}"
        cache.mkdir(parents=True, exist_ok=True)
        scores_path = cache / f"{name}-scores.npz"
        if scores_path.exists() and str(np.load(scores_path)["key"]) == key:
            self.scores = np.load(scores_path)["scores"]
            audio = None
        else:
            audio = singing_eval.read_wav(audio_path)
            started = time.perf_counter()
            self.scores = singing_eval.yamnet_scores(model, audio)
            print(f"{name}: yamnet {time.perf_counter() - started:.0f}s", file=sys.stderr)
            np.savez_compressed(scores_path, key=key, scores=self.scores)
        seg_path = cache / f"{name}-vad{end_silence_ms}.json"
        if seg_path.exists():
            self.segments = json.loads(seg_path.read_text("utf-8"))
        else:
            if audio is None:
                audio = singing_eval.read_wav(audio_path)
            singing_eval.END_SILENCE_MS = end_silence_ms
            self.segments = singing_eval.vad_segments(vad, audio)
            seg_path.write_text(json.dumps(self.segments), encoding="utf-8")
        self.features = frame_features(self.scores, groups)
        self.frame_centre_s = (np.arange(len(self.features)) * HOP_SAMPLES + PATCH_SAMPLES / 2) / SAMPLE_RATE
        self.frame_end_s = (np.arange(len(self.features)) * HOP_SAMPLES + PATCH_SAMPLES) / SAMPLE_RATE
        in_speech = np.zeros(len(self.features), dtype=bool)
        in_unc = np.zeros(len(self.features), dtype=bool)
        for a, b in self.speech:
            in_speech |= (self.frame_centre_s >= a) & (self.frame_centre_s < b)
        for a, b in self.unc:
            in_unc |= (self.frame_centre_s >= a) & (self.frame_centre_s < b)
        self.frame_speech, self.frame_unc = in_speech, in_unc
        # Segment truth.
        self.kinds: list[str] = []
        for segment in self.segments:
            s0, s1 = segment["start"] / SAMPLE_RATE, segment["end"] / SAMPLE_RATE
            length = max(s1 - s0, 1e-9)
            if overlap(s0, s1, self.speech) / length >= 0.5:
                self.kinds.append("speech")
            elif overlap(s0, s1, self.unc) / length >= 0.5:
                # Uncaptioned is only "likely singing": a segment whose mean YAMNet speech
                # score is high is a person talking (video, MC) and not a miss.
                first = int(segment["start"] / HOP_SAMPLES)
                last = max(first + 1, int(segment["end"] / HOP_SAMPLES))
                speechy = float(self.features[first:last, 0].mean()) >= SPEECH_LIKE_SCORE
                self.kinds.append("uncaptioned_speechlike" if speechy else "uncaptioned")
            else:
                self.kinds.append("other")

    @property
    def captioned_hours(self) -> float:
        return (self.duration_s - sum(b - a for a, b in self.unc)) / 3600.0

    def labels(self, scorer: Any, params: SingingParams) -> list[tuple[str, str]]:
        """(early, final) class per VAD segment, from the shipped labeler."""
        return self._labels(self._tracker(scorer, params), params)

    def _tracker(self, scorer: Any, params: SingingParams) -> SingingTracker:
        tracker = SingingTracker(scorer, params, history_frames=10**7)
        tracker.observe(self.features)
        return tracker

    def _labels(self, tracker: SingingTracker, params: SingingParams) -> list[tuple[str, str]]:
        labels: list[tuple[str, str]] = []
        for segment in self.segments:
            labeler = SegmentLabeler(tracker, segment["start"])
            end = segment["end"]
            if end >= labeler.early_sample:
                labeler.update(labeler.early_sample)
            else:
                labeler.update(end, closing=True)
            early = labeler.decision
            assert early is not None
            if end >= labeler.revision_sample:
                labeler.update(labeler.revision_sample)
            else:
                labeler.update(end, closing=True)
            final = labeler.decision
            assert final is not None
            labels.append((early.audio_class, final.audio_class))
        return labels

    def evaluate(self, scorer: Any, params: SingingParams) -> dict[str, Any]:
        tracker = self._tracker(scorer, params)
        states = np.array([tracker.state_after(i + 1) for i in range(tracker.frames_seen)])
        labels = self._labels(tracker, params)

        def count(kind: str) -> dict[str, Any]:
            rows = [(lab, seg) for lab, seg, k in zip(labels, self.segments, self.kinds, strict=True) if k == kind]
            seconds = sum((seg["end"] - seg["start"]) / SAMPLE_RATE for _, seg in rows)
            sing_early = [seg for lab, seg in rows if lab[0] == "singing"]
            sing_final = [seg for lab, seg in rows if lab[1] == "singing"]
            ever = [seg for lab, seg in rows if "singing" in lab]
            return {
                "segments": len(rows),
                "seconds": round(seconds, 1),
                "singing_early": len(sing_early),
                "singing_final": len(sing_final),
                "singing_ever": len(ever),
                "singing_final_seconds": round(
                    sum((s["end"] - s["start"]) / SAMPLE_RATE for s in sing_final), 1
                ),
                "ever_spans": [
                    (round(s["start"] / SAMPLE_RATE, 1), round(s["end"] / SAMPLE_RATE, 1))
                    for s in ever
                ],
            }

        speech, unc, other = count("speech"), count("uncaptioned"), count("other")
        unc_speechlike = count("uncaptioned_speechlike")
        unc_frames = self.frame_unc
        return {
            "speech": speech,
            "uncaptioned": unc,
            "other": other,
            "uncaptioned_speechlike": unc_speechlike,
            "state_on_in_speech_frames": int((states & self.frame_speech).sum()),
            "speech_frames": int(self.frame_speech.sum()),
            "state_on_in_uncaptioned_frames": int((states & unc_frames).sum()),
            "uncaptioned_frames": int(unc_frames.sum()),
            "states": states,
        }


def metrics_row(per_service: dict[str, tuple[Service, dict[str, Any]]]) -> dict[str, Any]:
    hours = sum(svc.captioned_hours for svc, _ in per_service.values())
    speech_segments = sum(r["speech"]["segments"] for _, r in per_service.values())
    false_ever = sum(r["speech"]["singing_ever"] for _, r in per_service.values())
    false_early = sum(r["speech"]["singing_early"] for _, r in per_service.values())
    false_final = sum(r["speech"]["singing_final"] for _, r in per_service.values())
    unc_segments = sum(r["uncaptioned"]["segments"] for _, r in per_service.values())
    unc_final = sum(r["uncaptioned"]["singing_final"] for _, r in per_service.values())
    unc_final_s = sum(r["uncaptioned"]["singing_final_seconds"] for _, r in per_service.values())
    unc_s = sum(r["uncaptioned"]["seconds"] for _, r in per_service.values())
    speechlike = sum(r["uncaptioned_speechlike"]["segments"] for _, r in per_service.values())
    unc_frames = sum(r["uncaptioned_frames"] for _, r in per_service.values())
    unc_on = sum(r["state_on_in_uncaptioned_frames"] for _, r in per_service.values())
    sp_frames = sum(r["speech_frames"] for _, r in per_service.values())
    sp_on = sum(r["state_on_in_speech_frames"] for _, r in per_service.values())
    return {
        "captioned_hours": round(hours, 3),
        "speech_segments": speech_segments,
        "false_singing_ever": false_ever,
        "false_singing_early": false_early,
        "false_singing_final": false_final,
        "false_per_captioned_hour": round(false_ever / hours, 3),
        "speech_frames_state_on_fraction": round(sp_on / max(sp_frames, 1), 5),
        "uncaptioned_segments": unc_segments,
        "uncaptioned_segments_singing_final_fraction": round(unc_final / max(unc_segments, 1), 3),
        "uncaptioned_seconds_singing_final_fraction": round(unc_final_s / max(unc_s, 1e-9), 3),
        "uncaptioned_speechlike_segments_excluded": speechlike,
        "uncaptioned_time_state_on_fraction": round(unc_on / max(unc_frames, 1), 3),
    }


def judge_false_calls(services: list[Service], model_path: Path, srt_root: Path) -> dict[str, Any]:
    """What hiding a falsely-called segment costs: decode it and score it against its cues.

    A false call over music that the model cannot hear through (CER near or above 100%) hides
    only hallucinated text; one with a low CER hides a real caption.
    """
    from benchmarks.cer_eval import edit_counts, normalize
    from benchmarks.gold_offline_eval import read_wav as read_float_wav
    from benchmarks.srt_gold import load_srt
    from tea_asr.backend import TeaMlxBackend

    backend = TeaMlxBackend(model_path)
    backend.load()
    rows: list[dict[str, Any]] = []
    try:
        for service in services:
            cues = load_srt(srt_root / f"{service.name}.srt")
            samples = read_float_wav(service.work / f"{service.name}.wav")
            labels = service.labels(DEFAULT_SCORER, DEFAULT_PARAMS)
            for segment, kind, label in zip(service.segments, service.kinds, labels, strict=True):
                if kind != "speech" or "singing" not in label:
                    continue
                start_s, end_s = segment["start"] / SAMPLE_RATE, segment["end"] / SAMPLE_RATE
                reference = normalize(
                    "".join(c.text for c in cues if start_s <= (c.start_s + c.end_s) / 2 < end_s)
                )
                text = str(backend.transcribe(np.ascontiguousarray(samples[segment["start"] : segment["end"]])).text)
                hypothesis = normalize(text)
                errors = edit_counts(reference, hypothesis).errors if reference else len(hypothesis)
                rows.append(
                    {
                        "service": service.name,
                        "seconds": round(end_s - start_s, 1),
                        "reference_chars": len(reference),
                        "hypothesis_chars": len(hypothesis),
                        "cer": round(errors / max(len(reference), 1), 3),
                        "correct_chars_lost": max(0, len(reference) - errors),
                        "early": label[0],
                        "final": label[1],
                    }
                )
    finally:
        backend.close()
    cers = sorted(row["cer"] for row in rows)
    return {
        "segments": len(rows),
        "median_cer": cers[len(cers) // 2] if cers else None,
        "segments_cer_over_50pct": sum(c > 0.5 for c in cers),
        "reference_chars": sum(r["reference_chars"] for r in rows),
        "hypothesis_chars": sum(r["hypothesis_chars"] for r in rows),
        "correct_chars_lost": sum(r["correct_chars_lost"] for r in rows),
        "rows": rows,
    }


def load_services(work: Path, end_silence_ms: int, cache: Path) -> list[Service]:
    model_path, class_map = locate_yamnet()
    model = YamnetModel(model_path)
    groups = ClassGroups.from_names(load_class_names(class_map))
    vad = SileroVad(locate_vad())
    return [Service(name, work, model, vad, groups, end_silence_ms, cache) for name in SERVICES]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--work", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--end-silence-ms", type=int, nargs="+", default=[600, 870])
    parser.add_argument("--out", type=Path)
    parser.add_argument("--sweep", action="store_true", help="grid search for zero false calls")
    parser.add_argument("--judge-model-path", type=Path,
                        help="ASR snapshot: decode every false-called segment and score it vs its cues")
    parser.add_argument("--srt-root", type=Path, default=DEFAULT_WORK.parent)
    parser.add_argument("--list-spans", action="store_true", help="print false-call timestamps (private)")
    args = parser.parse_args(argv)
    cache = args.work / "singing-cache"
    report: dict[str, Any] = {"defaults": {"params": DEFAULT_PARAMS.__dict__ if hasattr(DEFAULT_PARAMS, "__dict__") else str(DEFAULT_PARAMS)}, "runs": {}}
    for end_silence in args.end_silence_ms:
        services = load_services(args.work, end_silence, cache)
        results = {s.name: (s, s.evaluate(DEFAULT_SCORER, DEFAULT_PARAMS)) for s in services}
        run: dict[str, Any] = {"pooled": metrics_row(results), "per_service": {}}
        for name, (svc, result) in results.items():
            run["per_service"][name] = {
                "captioned_hours": round(svc.captioned_hours, 3),
                "speech": {k: v for k, v in result["speech"].items() if k != "ever_spans"},
                "uncaptioned": {k: v for k, v in result["uncaptioned"].items() if k != "ever_spans"},
                "other": {k: v for k, v in result["other"].items() if k != "ever_spans"},
                "uncaptioned_speechlike": {
                    k: v for k, v in result["uncaptioned_speechlike"].items() if k != "ever_spans"
                },
            }
            if args.list_spans:
                print(name, end_silence, "false spans (s):", result["speech"]["ever_spans"], file=sys.stderr)
        # Margin: the strongest enter-window mean seen in captioned speech frames.
        margins = {}
        for name, (svc, _) in results.items():
            probabilities = DEFAULT_SCORER.score(svc.features)
            window = DEFAULT_PARAMS.enter_frames
            kernel = np.ones(window) / window
            means = np.convolve(probabilities, kernel, mode="valid")
            ok = svc.frame_speech[window - 1 :]
            margins[name] = round(float(means[ok].max()), 4)
        run["max_speech_enter_window_mean"] = margins
        if args.judge_model_path:
            run["false_call_cost"] = judge_false_calls(services, args.judge_model_path, args.srt_root)
        report["runs"][str(end_silence)] = run
        if args.sweep:
            report["runs"][str(end_silence)]["sweep"] = sweep(services)
        print(json.dumps({end_silence: run["pooled"]}), file=sys.stderr)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    return 0


def sweep(services: list[Service]) -> list[dict[str, Any]]:
    rows = []
    for enter_frames, enter_threshold, local_threshold, exit_threshold in itertools.product(
        (4, 8, 12, 16, 24),
        (0.98, 0.99, 0.995, 0.998, 0.999, 0.9995),
        (0.5, 0.95, 0.99),
        (0.4, 0.6, 0.8),
    ):
        params = replace(
            DEFAULT_PARAMS,
            enter_frames=enter_frames,
            enter_threshold=enter_threshold,
            local_threshold=local_threshold,
            exit_threshold=exit_threshold,
        )
        results = {s.name: (s, s.evaluate(DEFAULT_SCORER, params)) for s in services}
        row = metrics_row(results)
        row["params"] = {
            "enter_frames": enter_frames,
            "enter_threshold": enter_threshold,
            "local_threshold": local_threshold,
            "exit_threshold": exit_threshold,
        }
        rows.append(row)
    rows.sort(
        key=lambda r: (
            r["false_singing_ever"],
            -r["uncaptioned_segments_singing_final_fraction"],
            -r["uncaptioned_time_state_on_fraction"],
        )
    )
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
