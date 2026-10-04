"""Turn a human-corrected SRT into listening-kit style evaluation items.

The SRT is the reference.  Consecutive cues are merged into chunks of at most
``--max-chunk-s`` seconds, split wherever the gap between cues is at least
``--split-gap-s``.  Long stretches without any caption (``--uncaptioned-gap-s``,
default 20 s) are *not* speech references: the user only captions the parts that
need captions, so such gaps are mostly worship singing.  They are written to a
sidecar JSON instead of becoming items.

The answers file has the format ``benchmarks/cer_eval.py`` and
``benchmarks/gold_offline_eval.py`` read (``{"set", "items": [{id, start_s,
end_s, reference, music, unclear}]}``).  Nothing here touches the network; the
SRT text is private and the outputs belong under a git-excluded work directory.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SAMPLE_RATE = 16_000
DEFAULT_MAX_CHUNK_S = 15.0
DEFAULT_SPLIT_GAP_S = 0.6
DEFAULT_UNCAPTIONED_GAP_S = 20.0

_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


@dataclass(frozen=True)
class Cue:
    index: int
    start_s: float
    end_s: float
    text: str


def _seconds(hours: str, minutes: str, seconds: str, millis: str) -> float:
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis.ljust(3, "0")) / 1000


def parse_srt(text: str) -> list[Cue]:
    """Parse SRT text.  Tolerates a BOM, CRLF, multi-line cues and leading spaces.

    Cues with no text are dropped.  Cue lines are joined with no separator, except
    a single space between two ASCII alphanumerics.
    """
    normalized = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", normalized.strip("\n")):
        lines = [line for line in block.split("\n")]
        timing_at = next((i for i, line in enumerate(lines) if _TIME_RE.search(line)), None)
        if timing_at is None:
            continue
        match = _TIME_RE.search(lines[timing_at])
        assert match is not None
        start_s = _seconds(*match.group(1, 2, 3, 4))
        end_s = _seconds(*match.group(5, 6, 7, 8))
        index = len(cues) + 1
        if timing_at > 0 and lines[timing_at - 1].strip().isdigit():
            index = int(lines[timing_at - 1].strip())
        body = join_text(line.strip() for line in lines[timing_at + 1 :] if line.strip())
        if not body or end_s < start_s:
            continue
        cues.append(Cue(index=index, start_s=start_s, end_s=end_s, text=body))
    return cues


def join_text(parts: Any) -> str:
    """Concatenate caption pieces; add a space only between two ASCII alphanumerics."""
    joined = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if joined and joined[-1].isascii() and joined[-1].isalnum() and part[0].isascii() and part[0].isalnum():
            joined += " "
        joined += part
    return joined


def load_srt(path: Path) -> list[Cue]:
    return parse_srt(path.read_text(encoding="utf-8-sig"))


def shift_cues(cues: list[Cue], offset_s: float) -> list[Cue]:
    """Add ``offset_s`` to every cue time (positive = the SRT is early vs the audio)."""
    if offset_s == 0:
        return list(cues)
    shifted = [Cue(c.index, c.start_s + offset_s, c.end_s + offset_s, c.text) for c in cues]
    return [c for c in shifted if c.end_s > 0 and c.start_s >= 0]


def build_chunks(
    cues: list[Cue],
    *,
    max_chunk_s: float = DEFAULT_MAX_CHUNK_S,
    split_gap_s: float = DEFAULT_SPLIT_GAP_S,
) -> list[list[Cue]]:
    """Group consecutive cues; start a new chunk at a gap >= split_gap_s or when the
    chunk would exceed max_chunk_s.  A single cue longer than the limit stays alone."""
    chunks: list[list[Cue]] = []
    current: list[Cue] = []
    for cue in sorted(cues, key=lambda c: (c.start_s, c.end_s)):
        if current:
            gap = cue.start_s - current[-1].end_s
            span = max(cue.end_s, current[-1].end_s) - current[0].start_s
            if gap >= split_gap_s or span > max_chunk_s:
                chunks.append(current)
                current = []
        current.append(cue)
    if current:
        chunks.append(current)
    return chunks


def uncaptioned_spans(
    cues: list[Cue],
    *,
    min_gap_s: float = DEFAULT_UNCAPTIONED_GAP_S,
    audio_duration_s: float | None = None,
) -> list[dict[str, Any]]:
    """Gaps of at least min_gap_s between cues (and before the first / after the last
    cue when the audio duration is known)."""
    ordered = sorted(cues, key=lambda c: (c.start_s, c.end_s))
    spans: list[dict[str, Any]] = []
    previous_end = 0.0
    edge = "head"
    for cue in ordered:
        if cue.start_s - previous_end >= min_gap_s:
            spans.append(
                {"start_s": round(previous_end, 3), "end_s": round(cue.start_s, 3), "edge": edge}
            )
        previous_end = max(previous_end, cue.end_s)
        edge = "gap"
    if audio_duration_s is not None and audio_duration_s - previous_end >= min_gap_s:
        spans.append(
            {"start_s": round(previous_end, 3), "end_s": round(audio_duration_s, 3), "edge": "tail"}
        )
    for span in spans:
        span["duration_s"] = round(span["end_s"] - span["start_s"], 3)
    return spans


def build_answers(
    cues: list[Cue],
    set_name: str,
    *,
    max_chunk_s: float = DEFAULT_MAX_CHUNK_S,
    split_gap_s: float = DEFAULT_SPLIT_GAP_S,
    pad_s: float = 0.0,
    audio_duration_s: float | None = None,
) -> dict[str, Any]:
    """Build the answers document.  ``pad_s`` widens each item on both sides but never
    past the midpoint of the gap to its neighbour."""
    chunks = build_chunks(cues, max_chunk_s=max_chunk_s, split_gap_s=split_gap_s)
    bounds = [(chunk[0].start_s, max(c.end_s for c in chunk)) for chunk in chunks]
    items: list[dict[str, Any]] = []
    for position, (chunk, (start_s, end_s)) in enumerate(zip(chunks, bounds, strict=True)):
        lower = 0.0
        upper = audio_duration_s if audio_duration_s is not None else end_s + pad_s
        if position > 0:
            lower = (bounds[position - 1][1] + start_s) / 2
        if position + 1 < len(bounds):
            upper = min(upper, (end_s + bounds[position + 1][0]) / 2)
        padded_start = max(lower, start_s - pad_s)
        padded_end = min(upper, end_s + pad_s)
        if padded_end <= padded_start:
            padded_start, padded_end = start_s, end_s
        items.append(
            {
                "id": f"{set_name}-{position + 1:04d}",
                "set": set_name,
                "start_s": round(padded_start, 3),
                "end_s": round(padded_end, 3),
                "reference": join_text(c.text for c in chunk),
                "cue_first": chunk[0].index,
                "cue_last": chunk[-1].index,
                "music": False,
                "unclear": False,
            }
        )
    return {"set": set_name, "items": items}


def probe_duration_s(path: Path) -> float:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "csv=p=0", str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return float(out)


def convert_to_wav(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(source),
            "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(destination),
        ],
        check=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--srt", type=Path, required=True)
    parser.add_argument("--set-name", required=True, help="item id prefix and answers set name")
    parser.add_argument("--answers-out", type=Path, required=True)
    parser.add_argument("--uncaptioned-out", type=Path, required=True)
    parser.add_argument("--audio", type=Path, help="source audio; gives the duration for the tail gap")
    parser.add_argument("--wav-out", type=Path, help="also convert --audio to 16 kHz mono PCM16 here")
    parser.add_argument("--max-chunk-s", type=float, default=DEFAULT_MAX_CHUNK_S)
    parser.add_argument("--split-gap-s", type=float, default=DEFAULT_SPLIT_GAP_S)
    parser.add_argument("--uncaptioned-gap-s", type=float, default=DEFAULT_UNCAPTIONED_GAP_S)
    parser.add_argument("--pad-s", type=float, default=0.0)
    parser.add_argument(
        "--offset-s", type=float, default=0.0, help="constant shift added to every cue time"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.wav_out is not None and args.audio is None:
        print("error: --wav-out needs --audio", file=sys.stderr)
        return 2
    try:
        duration = probe_duration_s(args.audio) if args.audio is not None else None
        cues = shift_cues(load_srt(args.srt), args.offset_s)
        answers = build_answers(
            cues,
            args.set_name,
            max_chunk_s=args.max_chunk_s,
            split_gap_s=args.split_gap_s,
            pad_s=args.pad_s,
            audio_duration_s=duration,
        )
        spans = uncaptioned_spans(
            cues, min_gap_s=args.uncaptioned_gap_s, audio_duration_s=duration
        )
        for path, value in (
            (args.answers_out, answers),
            (
                args.uncaptioned_out,
                {
                    "set": args.set_name,
                    "audio_duration_s": duration,
                    "offset_s": args.offset_s,
                    "min_gap_s": args.uncaptioned_gap_s,
                    "spans": spans,
                },
            ),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if args.wav_out is not None:
            convert_to_wav(args.audio, args.wav_out)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    item_s = sum(i["end_s"] - i["start_s"] for i in answers["items"])
    print(
        f"{args.set_name}: {len(cues)} cues -> {len(answers['items'])} items "
        f"({item_s / 60:.1f} min referenced), {len(spans)} uncaptioned spans "
        f"({sum(s['duration_s'] for s in spans) / 60:.1f} min)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
