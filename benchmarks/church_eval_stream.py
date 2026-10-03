"""Streaming check on 20-minute windows of the church services.

Three steps; derived audio, traces and reports stay under the git-excluded work directory
(``TEA_SOAK_ROOT`` points ``soak_real_audio.py`` at ``<work>``):

    # 1. cut the windows (WAV + manifest) from the converted services
    python benchmarks/church_eval_stream.py windows

    # 2. against a server on port 8431 (isolated HOME, temporary token), stream every window
    #    once per end_silence value, the values of one window concurrently (two sessions)
    python benchmarks/church_eval_stream.py capture --token-file <token> \\
        --server-log <HOME>/Library/Logs/TEA\\ ASR/service.log --end-silence 600 870 \\
        --context-profile church

    # 3. score the traces against the SRT: concatenated CER, singing hides vs captions,
    #    latencies, and the OBS caption-replay metrics with the user's display settings
    python benchmarks/church_eval_stream.py score --end-silence 600 870 --out <metrics.json>

The reference for a window is the SRT cues starting inside it.  Segments are mapped to the
service timeline by their sample positions; those mostly inside an uncaptioned span are not
scored for CER (the user does not caption them) and are counted as *missed singing* when a
caption would have been shown.  Only metrics are printed or written.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import wave
from pathlib import Path
from typing import Any

SERVICE_ROOT = Path("/Users/c2leb/Codes/tea-asr-service")
WORK = SERVICE_ROOT / ".soak/church-eval/work"

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from benchmarks.cer_eval import normalize
from benchmarks.srt_gold import Cue, load_srt

SAMPLE_RATE = 16_000
SERVICES = ("20260704", "20260822", "20260912")
WINDOWS = {
    # name: (start_s, end_s, what it covers)
    "open": (180.0, 1380.0, "welcome prayer, captioned->singing->captioned boundaries, greeting"),
    "sermon": (3000.0, 4200.0, "sermon"),
    "close": (6300.0, 7500.0, "response prayer, closing, singing boundary"),
}
PLUGIN = Path("/Users/c2leb/Codes/obs-plugins/tea-live-subtitle")


def levenshtein(a: str, b: str) -> int:
    """Edit distance by Hyyrö's bit-parallel algorithm on Python integers (exact)."""
    if not a:
        return len(b)
    if not b:
        return len(a)
    m = len(a)
    mask = (1 << m) - 1
    high = 1 << (m - 1)
    peq: dict[str, int] = {}
    for index, char in enumerate(a):
        peq[char] = peq.get(char, 0) | (1 << index)
    pv, mv, score = mask, 0, m
    for char in b:
        eq = peq.get(char, 0)
        xv = eq | mv
        xh = ((((eq & pv) + pv) & mask) ^ pv) | eq
        ph = (mv | ~(xh | pv)) & mask
        mh = pv & xh
        if ph & high:
            score += 1
        elif mh & high:
            score -= 1
        ph = ((ph << 1) | 1) & mask
        mh = (mh << 1) & mask
        pv = (mh | ~(xv | ph)) & mask
        mv = ph & xv
    return score


def window_path(service: str, name: str) -> Path:
    return WORK / "windows" / f"{service}-{name}.wav"


def cut_windows(args: argparse.Namespace) -> int:
    for service in SERVICES:
        with wave.open(str(WORK / f"{service}.wav"), "rb") as source:
            for name, (start_s, end_s, note) in WINDOWS.items():
                out = window_path(service, name)
                out.parent.mkdir(parents=True, exist_ok=True)
                source.setpos(round(start_s * SAMPLE_RATE))
                frames = source.readframes(round((end_s - start_s) * SAMPLE_RATE))
                with wave.open(str(out), "wb") as sink:
                    sink.setnchannels(1)
                    sink.setsampwidth(2)
                    sink.setframerate(SAMPLE_RATE)
                    sink.writeframes(frames)
                manifest = {
                    "source_start_s": start_s,
                    "source_end_s": end_s,
                    "duration_s": len(frames) / 2 / SAMPLE_RATE,
                    "note": note,
                    "sections": [{"name": "all", "start_s": 0.0, "end_s": len(frames) / 2 / SAMPLE_RATE}],
                }
                out.with_suffix(".sections.json").write_text(json.dumps(manifest, indent=2) + "\n")
                print(f"{out.name}: {manifest['duration_s']:.0f}s")
    return 0


def trace_path(service: str, name: str, end_silence: int, tag: str) -> Path:
    return WORK / "traces" / f"{tag}-{service}-{name}-es{end_silence}.jsonl"


def capture_all(args: argparse.Namespace) -> int:
    soak = [sys.executable, str(HERE / "soak_real_audio.py"), "capture"]
    env = {**os.environ, "TEA_SOAK_ROOT": str(WORK)}
    failures = 0
    for service in SERVICES:
        for name in WINDOWS:
            if args.windows and name not in args.windows:
                continue
            if args.services and service not in args.services:
                continue
            running: list[tuple[Path, subprocess.Popen[bytes]]] = []
            for end_silence in args.end_silence:
                out = trace_path(service, name, end_silence, args.tag)
                if out.exists():
                    print(f"skip {out.name} (exists)", file=sys.stderr)
                    continue
                command = [
                    *soak, "--wav", str(window_path(service, name)), "--url", args.url,
                    "--token-file", str(args.token_file), "--end-silence", str(end_silence),
                    "--manifest", str(window_path(service, name).with_suffix(".sections.json")),
                    "--out", str(out),
                ]
                if args.server_log:
                    command += ["--server-log", str(args.server_log)]
                if args.context_profile:
                    command += ["--context-profile", args.context_profile]
                print("capture", out.name, file=sys.stderr, flush=True)
                running.append((out, subprocess.Popen(command, env=env)))
            for out, process in running:
                if process.wait() != 0:
                    print(f"FAILED {out.name}", file=sys.stderr)
                    failures += 1
    return 1 if failures else 0


# --- scoring ----------------------------------------------------------------------


def overlap(a0: float, a1: float, spans: list[tuple[float, float]]) -> float:
    return sum(max(0.0, min(a1, b) - max(a0, a)) for a, b in spans)


def join_final_text(texts: list[str]) -> str:
    out = ""
    for text in texts:
        text = text.strip()
        if not text:
            continue
        if out and out[-1].isascii() and out[-1].isalnum() and text[0].isascii() and text[0].isalnum():
            out += " "
        out += text
    return out


def read_trace(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                break  # truncated last line
    return rows


def pct(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, max(0, int(len(ordered) * fraction + 0.5) - 1))], 1)


def score_trace(
    rows: list[dict[str, Any]],
    meta: dict[str, Any],
    cues: list[Cue],
    unc: list[tuple[float, float]],
    window: tuple[float, float],
) -> dict[str, Any]:
    start_s, end_s = window
    t0 = float(meta["audio_t0_ms"])
    ref_cues = [c for c in cues if start_s <= (c.start_s + c.end_s) / 2 < end_s]
    cue_spans = [(c.start_s, c.end_s) for c in ref_cues]
    segments: dict[str, dict[str, Any]] = {}
    for row in rows:
        event = row["event"]
        segment_id = event.get("segment_id")
        if not segment_id:
            continue
        seg = segments.setdefault(segment_id, {"partials": [], "stables": []})
        kind = event.get("type")
        t_ms = float(row["t_ms"])
        if kind == "speech.started":
            seg["start_sample"] = event.get("start_sample")
        elif kind == "segment.queued":
            seg["start_sample"] = event.get("start_sample", seg.get("start_sample"))
            seg["end_sample"] = event.get("end_sample")
            seg["queued_ms"] = t_ms
        elif kind == "transcript.partial":
            seg["partials"].append(t_ms)
        elif kind == "transcript.stable":
            seg["stables"].append((t_ms, event.get("end_sample")))
        elif kind == "segment.audio_class":
            seg.setdefault("classes", []).append(event.get("class"))
        elif kind == "transcript.final":
            seg["final_ms"] = t_ms
            seg["text"] = str(event.get("text", ""))
            seg["start_sample"] = event.get("start_sample", seg.get("start_sample"))
            seg["end_sample"] = event.get("end_sample", seg.get("end_sample"))
            if event.get("audio_class"):
                seg["final_class"] = event["audio_class"]
            seg["warnings"] = event.get("warnings") or []
    shown_all: list[str] = []
    shown_visible: list[str] = []
    stats = {
        "segments": 0, "speech_segments": 0, "uncaptioned_segments": 0,
        "false_hide_final": 0, "false_hide_ever": 0,
        "missed_singing_segments": 0, "missed_singing_chars": 0, "missed_singing_seconds": 0.0,
        "uncaptioned_hidden_segments": 0,
    }
    first_text_audio, stable_latency, final_after_close, final_after_end = [], [], [], []
    carry_stripped = carry_uncertain = 0
    for seg in sorted(segments.values(), key=lambda s: s.get("start_sample") or 0):
        if "final_ms" not in seg or seg.get("start_sample") is None or seg.get("end_sample") is None:
            continue
        s0 = start_s + seg["start_sample"] / SAMPLE_RATE
        s1 = start_s + seg["end_sample"] / SAMPLE_RATE
        if not (start_s <= (s0 + s1) / 2 < end_s):
            continue
        stats["segments"] += 1
        length = max(s1 - s0, 1e-9)
        if overlap(s0, s1, cue_spans) / length >= 0.5:
            kind = "speech"
        elif overlap(s0, s1, unc) / length >= 0.5:
            kind = "uncaptioned"
        else:
            kind = "other"
        classes = seg.get("classes", [])
        final_class = seg.get("final_class") or (classes[-1] if classes else "speech")
        ever_singing = "singing" in classes or final_class == "singing"
        text = seg["text"]
        warnings = {w if isinstance(w, str) else w.get("code", "") for w in seg.get("warnings", [])}
        carry_stripped += "carry_overlap_stripped" in warnings
        carry_uncertain += "carry_overlap_uncertain" in warnings
        # latencies (audio clock: sample n is sent at t0 + n/16 ms)
        end_t = t0 + seg["end_sample"] / 16
        start_t = t0 + seg["start_sample"] / 16
        if seg["partials"]:
            first_text_audio.append(seg["partials"][0] - start_t)
        for t_ms, end_sample in seg["stables"]:
            if end_sample is not None:
                stable_latency.append(t_ms - (t0 + end_sample / 16))
        final_after_end.append(seg["final_ms"] - end_t)
        if "queued_ms" in seg:
            final_after_close.append(seg["final_ms"] - seg["queued_ms"])
        if kind == "uncaptioned":
            stats["uncaptioned_segments"] += 1
            if final_class == "singing":
                stats["uncaptioned_hidden_segments"] += 1
            elif text.strip():
                stats["missed_singing_segments"] += 1
                stats["missed_singing_chars"] += len(normalize(text))
                stats["missed_singing_seconds"] += s1 - s0
            continue
        if kind == "speech":
            stats["speech_segments"] += 1
            stats["false_hide_final"] += final_class == "singing"
            stats["false_hide_ever"] += ever_singing
        shown_all.append(text)
        if final_class != "singing":
            shown_visible.append(text)
    reference = normalize(join_final_text([c.text for c in ref_cues]))
    result: dict[str, Any] = {
        "reference_chars": len(reference),
        "captioned_seconds": round(sum(b - a for a, b in cue_spans), 1),
        **{k: (round(v, 1) if isinstance(v, float) else v) for k, v in stats.items()},
    }
    for label, texts in (("asr", shown_all), ("displayed", shown_visible)):
        hypothesis = normalize(join_final_text(texts))
        errors = levenshtein(reference, hypothesis)
        result[f"cer_{label}"] = round(errors / max(len(reference), 1), 4)
        result[f"errors_{label}"] = errors
        result[f"hyp_chars_{label}"] = len(hypothesis)
    result["first_text_after_segment_start_ms"] = {
        "median": pct(first_text_audio, 0.5), "p95": pct(first_text_audio, 0.95), "n": len(first_text_audio),
    }
    result["stable_commit_latency_ms"] = {
        "median": pct(stable_latency, 0.5), "p95": pct(stable_latency, 0.95), "n": len(stable_latency),
    }
    result["final_after_close_ms"] = {
        "median": pct(final_after_close, 0.5), "p95": pct(final_after_close, 0.95), "n": len(final_after_close),
    }
    result["final_after_segment_end_ms"] = {
        "median": pct(final_after_end, 0.5), "p95": pct(final_after_end, 0.95), "n": len(final_after_end),
    }
    result["carry"] = {"stripped": carry_stripped, "uncertain": carry_uncertain}
    return result


def replay_metrics(trace: Path) -> dict[str, Any]:
    # soak_real_audio fixes its SOAK_ROOT at import, so point it at the work dir first.
    os.environ.setdefault("TEA_SOAK_ROOT", str(WORK))
    import soak_real_audio

    binary = WORK / "build" / "caption-replay"
    metrics, _ = soak_real_audio.run_replay(trace, None, PLUGIN, WORK / "build", binary=binary, only_overall=True)
    return metrics["overall"]


def score_all(args: argparse.Namespace) -> int:
    report: dict[str, Any] = {"windows": {k: v[:2] for k, v in WINDOWS.items()}, "runs": {}}
    for service in SERVICES:
        cues = load_srt(SERVICE_ROOT / ".soak/church-eval" / f"{service}.srt")
        unc_doc = json.loads((WORK / "answers" / f"{service}-uncaptioned.json").read_text("utf-8"))
        unc = [(s["start_s"], s["end_s"]) for s in unc_doc["spans"]]
        for name, (start_s, end_s, _) in WINDOWS.items():
            for end_silence in args.end_silence:
                path = trace_path(service, name, end_silence, args.tag)
                if not path.exists():
                    continue
                meta = json.loads(path.with_suffix(".meta.json").read_text("utf-8"))
                result = score_trace(read_trace(path), meta, cues, unc, (start_s, end_s))
                if not args.no_replay:
                    result["replay"] = replay_metrics(path)
                report["runs"][f"{service}/{name}/es{end_silence}"] = result
                print(f"scored {path.name}", file=sys.stderr, flush=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("windows").set_defaults(handler=cut_windows)
    cap = commands.add_parser("capture")
    cap.add_argument("--url", default="ws://127.0.0.1:8431/v1/stream")
    cap.add_argument("--token-file", type=Path, required=True)
    cap.add_argument("--server-log", type=Path)
    cap.add_argument("--end-silence", type=int, nargs="+", default=[600, 870])
    cap.add_argument("--context-profile")
    cap.add_argument("--windows", nargs="*", choices=list(WINDOWS))
    cap.add_argument("--services", nargs="*", choices=list(SERVICES))
    cap.add_argument("--tag", default="stream")
    cap.set_defaults(handler=capture_all)
    score = commands.add_parser("score")
    score.add_argument("--end-silence", type=int, nargs="+", default=[600, 870])
    score.add_argument("--tag", default="stream")
    score.add_argument("--no-replay", action="store_true")
    score.add_argument("--out", type=Path)
    score.set_defaults(handler=score_all)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
