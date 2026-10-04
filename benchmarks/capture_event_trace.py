"""Record every server event of a real-time continuous session, with receive times.

Built for diagnosing live-subtitle clients (OBS plugin): captions that vanish
and come back as one big chunk, text that seems to go backwards, and where
sentence breaks land for a given `segmentation.end_silence_ms`.

Three steps, each reproducible:

    # 1. Build a >=60 s speech WAV from the pinned Taiwan-Tongues zh-TW test
    #    shard (the corpus quality_eval.py already uses; cached, no download).
    #    Real read speech; the pauses *between* clips are inserted digital
    #    silence with known lengths, so each end_silence setting splits at a
    #    predictable subset of them.
    HF_HUB_OFFLINE=1 uv run python benchmarks/capture_event_trace.py build-wav \\
        --out /tmp/traces/speech.wav

    # 2. Against a server started on a separate port, e.g.
    #    HOME=/tmp/tea-home TEA_ASR_MODELS_DIR=<models> HF_HUB_OFFLINE=1 \\
    #        uv run tea-asr serve --port 8399
    #    stream it at 1x real time the way the OBS plugin does
    #    (continuous + revisable + stable.agreement=2):
    uv run python benchmarks/capture_event_trace.py capture --wav /tmp/traces/speech.wav \\
        --url ws://127.0.0.1:8399/v1/stream --token-file /tmp/tea-home/.../token \\
        --end-silence-ms 600 --out /tmp/traces/real-600.jsonl

    # 3. Summarise one or more traces.
    uv run python benchmarks/capture_event_trace.py summarize /tmp/traces/real-*.jsonl

Trace format: one JSON object per line, `{"t_ms": <float>, "event": <raw server
event>}`. `t_ms` is `time.monotonic()` in milliseconds at receive, relative to
the moment the connection opened. A sidecar `<trace>.meta.json` records the
request, the WAV, and `audio_t0_ms` (when sample 0 was sent), so sample
positions map onto the same clock: `audio_t0_ms + sample / 16`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import itertools
import json
import statistics
import struct
import sys
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SAMPLE_RATE = 16_000
FRAME_SAMPLES = 1_600  # 100 ms per binary frame
TEXT_EVENTS = {"transcript.partial", "transcript.stable", "transcript.final"}


# --- build-wav -----------------------------------------------------------------


def _trim(pcm: np.ndarray, margin_samples: int) -> np.ndarray:
    """Cut leading/trailing near-silence, keeping a margin so onsets survive."""

    window = 320  # 20 ms
    usable = len(pcm) - len(pcm) % window
    if usable <= 0:
        return pcm
    frames = pcm[:usable].reshape(-1, window)
    energy = np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1))
    peak = energy.max()
    if peak <= 0:
        return pcm
    voiced = np.flatnonzero(energy > peak * 0.05)
    start = max(0, voiced[0] * window - margin_samples)
    end = min(len(pcm), (voiced[-1] + 1) * window + margin_samples)
    return pcm[start:end]


def build_wav(args: argparse.Namespace) -> int:
    from quality_eval import DATASET_ID, DATASET_REVISION, decode_mp3, load_samples

    gaps_ms = [int(value) for value in args.gaps.split(",")]
    samples = load_samples(args.clips + args.skip)[args.skip :]
    pieces: list[np.ndarray] = [np.zeros(SAMPLE_RATE, dtype="<i2")]  # 1 s lead-in
    cursor = SAMPLE_RATE
    clips: list[dict[str, Any]] = []
    speech_samples = 0
    for index, sample in enumerate(samples):
        audio = decode_mp3(sample.audio)
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
        pcm = _trim(pcm, margin_samples=args.margin_ms * 16)
        pieces.append(pcm)
        clips.append(
            {
                "key": sample.key,
                "reference": sample.reference,
                "start_sample": cursor,
                "end_sample": cursor + len(pcm),
            }
        )
        cursor += len(pcm)
        speech_samples += len(pcm)
        gap = gaps_ms[index % len(gaps_ms)] if index < len(samples) - 1 else args.tail_ms
        clips[-1]["gap_after_ms"] = gap
        pieces.append(np.zeros(gap * 16, dtype="<i2"))
        cursor += gap * 16
    pcm = np.concatenate(pieces)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(args.out), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())
    manifest = {
        "dataset": f"{DATASET_ID}@{DATASET_REVISION}",
        "shard_skip": args.skip,
        "gaps_ms": gaps_ms,
        "margin_ms": args.margin_ms,
        "duration_s": round(len(pcm) / SAMPLE_RATE, 2),
        "clip_audio_s": round(speech_samples / SAMPLE_RATE, 2),
        "sha256": hashlib.sha256(pcm.tobytes()).hexdigest(),
        "clips": clips,
    }
    manifest_path = args.out.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(
        f"{args.out}: {manifest['duration_s']} s total, {manifest['clip_audio_s']} s of clips, "
        f"{len(clips)} clips; manifest {manifest_path}"
    )
    return 0


# --- capture -------------------------------------------------------------------


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (SAMPLE_RATE, 1, 2):
            raise ValueError("WAV 必須是 16 kHz mono PCM16")
        return wav.readframes(wav.getnframes())


async def capture(args: argparse.Namespace) -> int:
    from websockets.asyncio.client import connect

    pcm = read_wav(args.wav)
    token = getattr(args, "token", None)
    if token is None:
        token = args.token_file.read_text().strip()
    start: dict[str, Any] = {
        "type": "session.start",
        "request_id": "trace",
        "profile": "continuous",
        "audio": {"sample_rate": SAMPLE_RATE, "channels": 1, "format": "pcm_s16le"},
        "language": "Chinese",
        "durable": False,
        "transcript_mode": "revisable",
        "stable": {"agreement": args.agreement},
    }
    if args.end_silence_ms is not None:
        start["segmentation"] = {"end_silence_ms": args.end_silence_ms}
    if getattr(args, "context_profile", None):
        # What the OBS plugin sends for its "hints_profile" setting.
        start["context"] = {"profile": args.context_profile}

    args.out.parent.mkdir(parents=True, exist_ok=True)
    wall_t0_epoch_s = time.time()
    origin = time.monotonic()

    def now_ms() -> float:
        return round((time.monotonic() - origin) * 1000, 1)

    audio_t0_ms: float | None = None
    status = 0
    with args.out.open("w", encoding="utf-8") as sink:

        def record(event: dict[str, Any]) -> None:
            sink.write(json.dumps({"t_ms": now_ms(), "event": event}, ensure_ascii=False) + "\n")

        async with connect(
            args.url, additional_headers={"Authorization": f"Bearer {token}"}, max_queue=None
        ) as socket:
            record(json.loads(await socket.recv()))  # hello
            await socket.send(json.dumps(start))
            started = json.loads(await socket.recv())
            record(started)
            if started.get("type") != "session.started":
                print(json.dumps(started, ensure_ascii=False), file=sys.stderr)
                return 1
            window = started["send_until_sample"]
            done = asyncio.Event()

            async def receive() -> None:
                nonlocal window, status
                async for message in socket:
                    event = json.loads(message)
                    record(event)
                    kind = event["type"]
                    if kind == "flow.control":
                        window = max(window, event["send_until_sample"])
                    elif kind == "error":
                        status = 1
                    if kind in {"session.stopped", "session.cancelled", "error"}:
                        done.set()
                        return

            receiver = asyncio.create_task(receive())
            seq = 0
            sample = 0
            audio_t0_ms = now_ms()
            clock = time.monotonic()
            for offset in range(0, len(pcm), FRAME_SAMPLES * 2):
                if done.is_set():
                    break
                chunk = pcm[offset : offset + FRAME_SAMPLES * 2]
                end = sample + len(chunk) // 2
                while end > window and not done.is_set():
                    await asyncio.sleep(0.005)
                if done.is_set():
                    break
                await socket.send(struct.pack("<QQ", seq, sample) + chunk)
                seq += 1
                sample = end
                # Absolute schedule: a fixed per-frame sleep drifts.
                ahead = clock + sample / SAMPLE_RATE - time.monotonic()
                if ahead > 0:
                    await asyncio.sleep(ahead)
            with contextlib.suppress(Exception):
                await socket.send(
                    json.dumps(
                        {"type": "session.stop", "request_id": "trace-stop", "through_seq": seq - 1}
                    )
                )
            await asyncio.wait_for(receiver, timeout=120)

    meta = {
        "request": start,
        "url": args.url,
        "wav": str(args.wav),
        "wav_sha256": hashlib.sha256(pcm).hexdigest(),
        "wav_duration_s": round(len(pcm) / 2 / SAMPLE_RATE, 2),
        "frame_samples": FRAME_SAMPLES,
        "audio_t0_ms": audio_t0_ms,
        "wall_t0_epoch_s": wall_t0_epoch_s,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    args.out.with_suffix(".meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n"
    )
    print(f"{args.out}: done (status {status})")
    return status


# --- summarize -----------------------------------------------------------------


def _pct(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(len(ordered) * fraction))], 1)


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def summarize_trace(path: Path) -> dict[str, Any]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    events = [(row["t_ms"], row["event"]) for row in rows]
    started = next(event for _, event in events if event["type"] == "session.started")
    policy = started.get("preview_policy") or {}

    finals = [e for _, e in events if e["type"] == "transcript.final"]
    skipped = [e for _, e in events if e["type"] == "segment.skipped"]
    errors = [e for _, e in events if e["type"] in {"segment.error", "error"}]
    boundaries: dict[str, int] = {}
    for _, event in events:
        if event["type"] == "segment.queued":
            boundaries[event["boundary"]] = boundaries.get(event["boundary"], 0) + 1

    # Stable commit gaps within a segment (every transcript.stable, incl. closing).
    stable_times: dict[str, list[float]] = {}
    stable_states: dict[str, int] = {}
    unusual_states: list[dict[str, Any]] = []
    largest_append = 0
    last_stable_text: dict[str, str] = {}
    for t, event in events:
        if event["type"] != "transcript.stable":
            continue
        segment = event["segment_id"]
        stable_times.setdefault(segment, []).append(t)
        stable_states[event["state"]] = stable_states.get(event["state"], 0) + 1
        previous = last_stable_text.get(segment, "")
        largest_append = max(largest_append, len(event["text"]) - len(previous))
        last_stable_text[segment] = event["text"]
        if event["state"] not in {"open", "final"}:
            unusual_states.append(
                {
                    "segment_index": event["segment_index"],
                    "state": event["state"],
                    "diverged_chars": event.get("diverged_chars"),
                    "text": event["text"],
                }
            )
    stable_gaps = [
        later - earlier
        for times in stable_times.values()
        for earlier, later in itertools.pairwise(times)
    ]

    # Speech intervals on the receive clock: speech.started .. segment.queued.
    open_at: dict[str, float] = {}
    intervals: list[tuple[float, float]] = []
    first_stable_delay: list[float] = []
    for t, event in events:
        if event["type"] == "speech.started":
            open_at[event["segment_id"]] = t
        elif event["type"] == "segment.queued" and event["segment_id"] in open_at:
            intervals.append((open_at[event["segment_id"]], t))
    for segment, t0 in open_at.items():
        if segment in stable_times:
            first_stable_delay.append(stable_times[segment][0] - t0)

    def longest_quiet(kinds: set[str] | None) -> float:
        times = [t for t, e in events if kinds is None or e["type"] in kinds]
        longest = 0.0
        for begin, end in intervals:
            inside = [begin] + [t for t in times if begin < t < end] + [end]
            longest = max(longest, *(b - a for a, b in itertools.pairwise(inside)))
        return round(longest, 1)

    # Partials that do not extend the stable text already committed for their segment.
    committed: dict[str, str] = {}
    partials = 0
    regressions: list[dict[str, Any]] = []
    for t, event in events:
        kind = event["type"]
        if kind == "transcript.stable":
            committed[event["segment_id"]] = event["text"]
        elif kind == "transcript.partial":
            partials += 1
            shown = committed.get(event["segment_id"], "")
            if not event["text"].startswith(shown):
                regressions.append(
                    {
                        "t_ms": t,
                        "segment_index": event["segment_index"],
                        "revision": event["revision"],
                        "stable": shown,
                        "partial": event["text"],
                    }
                )

    return {
        "trace": str(path),
        "endpoint_silence_ms": policy.get("endpoint_silence_ms"),
        "segments_final": len(finals),
        "segments_skipped": len(skipped),
        "errors": len(errors),
        "boundaries": boundaries,
        "stable_gap_ms": {
            "n": len(stable_gaps),
            "median": _median(stable_gaps),
            "p95": _pct(stable_gaps, 0.95),
            "max": round(max(stable_gaps), 1) if stable_gaps else None,
        },
        "first_stable_after_speech_start_ms": {
            "median": _median(first_stable_delay),
            "p95": _pct(first_stable_delay, 0.95),
            "max": round(max(first_stable_delay), 1) if first_stable_delay else None,
        },
        "largest_single_stable_append_chars": largest_append,
        "longest_silence_during_speech_ms": {
            "any_event": longest_quiet(None),
            "text_event": longest_quiet(TEXT_EVENTS),
        },
        "partials": partials,
        "partials_not_extending_stable": len(regressions),
        "partial_regressions": regressions,
        "stable_states": stable_states,
        "stable_non_open_final": unusual_states,
        "finals": [
            {
                "segment_index": e["segment_index"],
                "start_s": round(e["start_sample"] / SAMPLE_RATE, 2),
                "end_s": round(e["end_sample"] / SAMPLE_RATE, 2),
                "text": e["text"],
            }
            for e in finals
        ],
    }


def summarize(args: argparse.Namespace) -> int:
    summaries = [summarize_trace(path) for path in args.traces]
    if args.out is not None:
        args.out.write_text(json.dumps(summaries, ensure_ascii=False, indent=2) + "\n")
    header = (
        "| end_silence | segments | stable gap median/p95 ms | longest quiet any/text ms "
        "| partials not extending stable | diverged/abandoned |"
    )
    print(header)
    print("|---|---|---|---|---|---|")
    for s in summaries:
        gap = s["stable_gap_ms"]
        quiet = s["longest_silence_during_speech_ms"]
        states = s["stable_states"]
        print(
            f"| {s['endpoint_silence_ms']} | {s['segments_final']} final"
            f" (+{s['segments_skipped']} skipped) | {gap['median']} / {gap['p95']}"
            f" | {quiet['any_event']} / {quiet['text_event']}"
            f" | {s['partials_not_extending_stable']} / {s['partials']}"
            f" | {states.get('diverged', 0)} / {states.get('abandoned', 0)} |"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build-wav")
    build.add_argument("--out", type=Path, required=True)
    build.add_argument("--clips", type=int, default=24)
    build.add_argument("--skip", type=int, default=0, help="skip this many shard samples")
    build.add_argument("--gaps", default="400,700,1000,1500", help="ms, cycled between clips")
    build.add_argument("--margin-ms", type=int, default=150)
    build.add_argument("--tail-ms", type=int, default=2500)

    cap = commands.add_parser("capture")
    cap.add_argument("--wav", type=Path, required=True)
    cap.add_argument("--url", default="ws://127.0.0.1:8399/v1/stream")
    token = cap.add_mutually_exclusive_group(required=True)
    token.add_argument("--token", help="bearer token; prefer --token-file to avoid shell history")
    token.add_argument("--token-file", type=Path)
    cap.add_argument("--end-silence-ms", type=int, default=None, help="omit to use the default")
    cap.add_argument("--agreement", type=int, default=2)
    cap.add_argument(
        "--context-profile",
        help="server dictionary name sent as session.start.context.profile (server needs hints on)",
    )
    cap.add_argument("--out", type=Path, required=True)

    summary = commands.add_parser("summarize")
    summary.add_argument("traces", type=Path, nargs="+")
    summary.add_argument("--out", type=Path, default=None)

    args = parser.parse_args()
    if args.command == "build-wav":
        return build_wav(args)
    if args.command == "capture":
        return asyncio.run(capture(args))
    return summarize(args)


if __name__ == "__main__":
    raise SystemExit(main())
