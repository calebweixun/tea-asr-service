"""PUA A/B 第二輪：對本機自轉的量化模型（8bit / 4bit-selfconv）跑同一批語料。

跟 pua_ab_mlx_pass.py 的差異只有：model_path 直接指到本機資料夾（不是
透過 model_spec.py 的 HF repo id 找 snapshot），因為這些模型是
benchmarks/convert_quant.py 本機轉出來的，不在 HuggingFace 上。

用法（專案 .venv）：

    uv run python benchmarks/pua_ab_local_pass.py \
        --model-path models/mlx-8bit-selfconv \
        --pass-name mlx-8bit-selfconv \
        --limit 30 \
        --out benchmarks/results/pua_ab_8bit_30.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quality_eval import DATASET_ID, DATASET_REVISION, decode_mp3, load_samples

from tea_asr.backend import TeaMlxBackend

# 跟 pua_ab_mlx_pass.py / pua_ab_bf16_pass.py 用同一個範圍（BMP 私用區），
# 才能跟既有兩組數字直接比較。
PUA = re.compile(r"[-]")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--pass-name", type=str, required=True)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    samples = load_samples(args.limit)
    print(f"樣本 {len(samples)} 筆，語料 {DATASET_ID}@{DATASET_REVISION[:12]}")
    print(f"模型: {args.model_path}")

    backend = TeaMlxBackend(args.model_path)
    started_load = time.perf_counter()
    backend.load()
    load_s = time.perf_counter() - started_load

    rows = []
    for index, sample in enumerate(samples, start=1):
        audio = decode_mp3(sample.audio)
        started = time.perf_counter()
        result = backend.transcribe(audio)
        elapsed = time.perf_counter() - started
        hypothesis = result.text
        pua_chars = PUA.findall(hypothesis)
        rows.append(
            {
                "key": sample.key,
                "reference": sample.reference,
                "hypothesis": hypothesis,
                "pua_count": len(pua_chars),
                "pua_codepoints": [f"U+{ord(c):04X}" for c in pua_chars],
                "inference_s": round(elapsed, 3),
            }
        )
        print(f"  {index}/{len(samples)}: pua={len(pua_chars)} t={elapsed:.2f}s")

    summary = {
        "pass": args.pass_name,
        "model": str(args.model_path),
        "model_revision": "local-selfconv",
        "dataset": DATASET_ID,
        "dataset_revision": DATASET_REVISION,
        "samples": len(samples),
        "sentences_with_pua": sum(1 for r in rows if r["pua_count"] > 0),
        "pua_occurrences": sum(r["pua_count"] for r in rows),
        "load_s": round(load_s, 3),
        "mean_inference_s": round(sum(r["inference_s"] for r in rows) / len(rows), 3) if rows else 0.0,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"寫入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
