from __future__ import annotations

import argparse
import json
import platform
import resource
import time
from dataclasses import asdict
from pathlib import Path

from tea_asr.audio import load_pcm16_wav
from tea_asr.backend import TeaMlxBackend
from tea_asr.model_manager import prepare_model
from tea_asr.model_spec import TEA_ASR_1_1_MLX_4BIT


def has_private_use(text: str) -> bool:
    return any(0xE000 <= ord(char) <= 0xF8FF for char in text)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wav", type=Path)
    parser.add_argument("--step-ms", type=int, default=800)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    audio = load_pcm16_wav(args.wav)
    model_path = prepare_model(TEA_ASR_1_1_MLX_4BIT)
    started = time.perf_counter()
    backend = TeaMlxBackend(model_path)
    backend.load()
    load_s = time.perf_counter() - started

    step = args.step_ms * 16
    runs = []
    for end in range(step, len(audio), step):
        result = backend.transcribe(audio[:end])
        row = asdict(result)
        row["end_sample"] = end
        row["rtf"] = result.total_time_s / (end / 16_000)
        row["contains_private_use"] = has_private_use(result.text)
        runs.append(row)

    final = backend.transcribe(audio)
    final_row = asdict(final)
    final_row["end_sample"] = len(audio)
    final_row["rtf"] = final.total_time_s / (len(audio) / 16_000)
    final_row["contains_private_use"] = has_private_use(final.text)
    runs.append(final_row)

    report = {
        "environment": {
            "machine": platform.machine(),
            "macos": platform.mac_ver()[0],
            "python": platform.python_version(),
            "model": TEA_ASR_1_1_MLX_4BIT.repo_id,
            "revision": TEA_ASR_1_1_MLX_4BIT.revision,
            "mlx_audio": TEA_ASR_1_1_MLX_4BIT.mlx_audio_version,
        },
        "input": {"path": str(args.wav), "samples": len(audio), "duration_s": len(audio) / 16_000},
        "load_s": load_s,
        "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "total_inference_s": sum(run["total_time_s"] for run in runs),
        "runs": runs,
    }
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n")
    print(encoded)
    backend.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
