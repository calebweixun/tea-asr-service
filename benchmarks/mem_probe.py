"""量測單一模型載入 + 推論的 peak RSS 用的小腳本，配合 /usr/bin/time -l 使用。

    /usr/bin/time -l uv run python benchmarks/mem_probe.py --model-path models/mlx-8bit-selfconv
    /usr/bin/time -l uv run python benchmarks/mem_probe.py --model-spec alkd-4bit
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quality_eval import decode_mp3, load_samples

from tea_asr.backend import TeaMlxBackend
from tea_asr.model_manager import locate_prepared_model
from tea_asr.model_spec import TEA_ASR_1_1_MLX_4BIT


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", type=Path, default=None)
    parser.add_argument("--model-spec", choices=["alkd-4bit"], default=None)
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args()

    if args.model_spec == "alkd-4bit":
        model_path = locate_prepared_model(TEA_ASR_1_1_MLX_4BIT)
    elif args.model_path is not None:
        model_path = args.model_path
    else:
        raise SystemExit("需要 --model-path 或 --model-spec")

    samples = load_samples(args.repeat)
    backend = TeaMlxBackend(model_path)
    t0 = time.perf_counter()
    backend.load()
    load_s = time.perf_counter() - t0

    times = []
    for sample in samples:
        audio = decode_mp3(sample.audio)
        t1 = time.perf_counter()
        backend.transcribe(audio)
        times.append(time.perf_counter() - t1)

    print(f"model_path={model_path}")
    print(f"load_s={load_s:.3f}")
    print(f"mean_inference_s={sum(times)/len(times):.3f}")
    print(f"per_sample_s={[round(t,3) for t in times]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
