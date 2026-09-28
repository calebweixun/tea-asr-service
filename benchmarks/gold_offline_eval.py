"""Decode listening-kit spans with the server backend, without streaming."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from tea_asr.api.stream import filter_private_use_characters
from tea_asr.backend import TeaMlxBackend
from tea_asr.context import (
    ContextDictionaryStore,
    apply_replacements,
    build_system_prompt,
)
from tea_asr.repetition import trim_repetitions

SAMPLE_RATE = 16_000


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--answers", type=Path, required=True, help="listening-kit answers JSON")
    parser.add_argument("--wav", type=Path, required=True, help="matching 16 kHz mono PCM16 WAV")
    parser.add_argument("--model-path", type=Path, required=True, help="prepared MLX snapshot path")
    parser.add_argument("--output", type=Path, help="write {item_id: text} JSON here")
    parser.add_argument("--timings", type=Path, help="write per-item decode timings JSON here")
    parser.add_argument(
        "--mode", choices=("segment", "span"), default="segment", help="decode exact or padded spans"
    )
    parser.add_argument(
        "--pad-s", type=float, default=0.0, help="extra seconds on both sides in span mode"
    )
    parser.add_argument("--dictionary", type=Path, help="context dictionary TOML")
    parser.add_argument(
        "--prompt", action="store_true", help="send dictionary domain and hotwords to the model"
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.pad_s < 0 or not math.isfinite(args.pad_s):
        parser.error("--pad-s must be finite and non-negative")
    if args.mode != "span" and args.pad_s:
        parser.error("--pad-s can only be used with --mode span")
    if args.prompt and args.dictionary is None:
        parser.error("--prompt requires --dictionary")
    if args.dictionary is not None and args.dictionary.suffix != ".toml":
        parser.error("--dictionary must be a .toml file")
    return args


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as audio_file:
        if (
            audio_file.getnchannels() != 1
            or audio_file.getsampwidth() != 2
            or audio_file.getframerate() != SAMPLE_RATE
        ):
            raise ValueError("WAV must be mono 16 kHz signed 16-bit PCM")
        raw_samples = audio_file.readframes(audio_file.getnframes())
    if not raw_samples:
        raise ValueError("WAV contains no samples")
    return np.frombuffer(raw_samples, dtype="<i2").astype(np.float32) / 32768.0


def load_items(path: Path) -> list[dict[str, Any]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("items"), list):
        raise TypeError("answers JSON must be a listening-kit object with an items list")
    items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(document["items"]):
        if not isinstance(item, dict):
            raise TypeError(f"answers item {index} must be an object")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise ValueError(f"answers item {index} must have a non-empty string id")
        if item_id in seen_ids:
            raise ValueError(f"duplicate item id in answers: {item_id}")
        seen_ids.add(item_id)
        start_s = item.get("start_s")
        end_s = item.get("end_s")
        if (
            isinstance(start_s, bool)
            or isinstance(end_s, bool)
            or not isinstance(start_s, (int, float))
            or not isinstance(end_s, (int, float))
            or not math.isfinite(start_s)
            or not math.isfinite(end_s)
            or start_s < 0
            or end_s <= start_s
        ):
            raise ValueError(f"answers item {item_id} has an invalid start_s/end_s span")
        items.append(item)
    return items


def _item_audio(
    samples: np.ndarray, item: dict[str, Any], *, mode: str, pad_s: float
) -> np.ndarray:
    start = round(float(item["start_s"]) * SAMPLE_RATE)
    end = round(float(item["end_s"]) * SAMPLE_RATE)
    if end > samples.size:
        raise ValueError(f"answers item {item['id']} ends beyond the WAV duration")
    if mode == "span":
        padding = round(pad_s * SAMPLE_RATE)
        start = max(0, start - padding)
        end = min(samples.size, end + padding)
    if end <= start:
        raise ValueError(f"answers item {item['id']} selects no audio samples")
    return samples[start:end]


def run_evaluation(
    args: argparse.Namespace,
    *,
    backend_factory: Callable[[Path], Any] | None = None,
) -> tuple[dict[str, str], dict[str, dict[str, float]]]:
    items = load_items(args.answers)
    samples = read_wav(args.wav)
    dictionary = None
    if args.dictionary is not None:
        dictionary = ContextDictionaryStore(args.dictionary.parent).load(args.dictionary.stem)
    system_prompt = (
        build_system_prompt(dictionary.domain, dictionary.hotwords)
        if dictionary is not None and args.prompt
        else None
    )
    replacement_rules = dictionary.replacements if dictionary is not None else ()

    backend_class = backend_factory or TeaMlxBackend
    backend = backend_class(args.model_path)
    output: dict[str, str] = {}
    timings: dict[str, dict[str, float]] = {}
    try:
        backend.load()
        for item in items:
            audio = _item_audio(samples, item, mode=args.mode, pad_s=args.pad_s)
            started = time.perf_counter()
            transcription = backend.transcribe(audio, system_prompt=system_prompt)
            decode_s = time.perf_counter() - started

            text = filter_private_use_characters(str(transcription.text))
            text, _ = trim_repetitions(text)
            text, _ = apply_replacements(text, replacement_rules)
            item_id = item["id"]
            output[item_id] = text
            timings[item_id] = {
                "decode_s": round(decode_s, 6),
                "audio_s": round(audio.size / SAMPLE_RATE, 6),
            }
            model_time = getattr(transcription, "total_time_s", None)
            if isinstance(model_time, (int, float)) and math.isfinite(model_time):
                timings[item_id]["model_time_s"] = round(float(model_time), 6)
    finally:
        backend.close()
    return output, timings


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _default_timings_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}.timings.json")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        output, timings = run_evaluation(args)
        if args.output is None:
            print(json.dumps(output, ensure_ascii=False, indent=2))
        else:
            _write_json(args.output, output)
        timings_path = args.timings or (
            _default_timings_path(args.output) if args.output is not None else None
        )
        if timings_path is not None:
            _write_json(timings_path, timings)
    except (OSError, TypeError, ValueError, KeyError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for item_id, timing in timings.items():
        print(
            f"decode-time {item_id}: {timing['decode_s']:.3f}s "
            f"({timing['audio_s']:.3f}s audio)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
