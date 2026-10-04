"""Offline comparison of one WAV with and without a recognition dictionary.

The WAV must be mono, 16 kHz, signed 16-bit PCM. The model snapshot and
dictionary are loaded from local paths; this script never downloads assets.
"""

from __future__ import annotations

import argparse
import wave
from pathlib import Path

import numpy as np

from tea_asr.backend import TeaMlxBackend
from tea_asr.context import ContextDictionaryStore, apply_replacements, resolve_context
from tea_asr.wire import ContextOptions


def _read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as audio_file:
        if (
            audio_file.getnchannels() != 1
            or audio_file.getsampwidth() != 2
            or audio_file.getframerate() != 16_000
        ):
            raise ValueError("WAV must be mono 16 kHz signed 16-bit PCM")
        samples = audio_file.readframes(audio_file.getnframes())
    if not samples:
        raise ValueError("WAV contains no samples")
    return np.frombuffer(samples, dtype="<i2").astype(np.float32) / 32768.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--wav", type=Path, required=True)
    parser.add_argument("--dictionary", type=Path, required=True)
    parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="skip the experimental model prompt and show deterministic replacements only",
    )
    args = parser.parse_args()

    audio = _read_wav(args.wav)
    dictionary = args.dictionary.expanduser().resolve()
    plan = resolve_context(
        ContextOptions(profile=dictionary.stem),
        ContextDictionaryStore(dictionary.parent),
    )

    backend = TeaMlxBackend(args.model_path.expanduser())
    try:
        backend.load()
        plain = backend.transcribe(audio)
        prompted = not args.no_prompt and plan.system_prompt is not None
        contextual = (
            backend.transcribe(audio, system_prompt=plan.system_prompt) if prompted else plain
        )
    finally:
        backend.close()

    print(f"without_context.text: {plain.text}")
    print(f"without_context.prompt_tokens: {plain.prompt_tokens}")
    replaced, replacement_count = apply_replacements(contextual.text, plan.replacements)
    print(f"with_context.prompt_applied: {prompted}")
    print(f"with_context.raw_text: {contextual.text}")
    print(f"with_context.text: {replaced}")
    print(f"with_context.replacements_applied: {replacement_count}")
    print(f"with_context.prompt_tokens: {contextual.prompt_tokens}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
