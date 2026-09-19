"""PUA A/B 第一步：用目前生產用的 MLX 4bit 模型跑同一批語料。

用法（在專案的 .venv 底下跑，需要 mlx_audio）：

    uv run python benchmarks/pua_ab_mlx_pass.py --limit 10 --out benchmarks/results/pua_ab_mlx.json

輸出的 JSON 只含每筆的 key/reference/hypothesis 與私用區統計，供
benchmarks/pua_ab_compare.py 與 BF16 那一輪的輸出比較。
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

from quality_eval import DATASET_ID, DATASET_REVISION, decode_mp3, load_samples  # noqa: E402

from tea_asr.backend import TeaMlxBackend  # noqa: E402
from tea_asr.model_manager import locate_prepared_model  # noqa: E402
from tea_asr.model_spec import TEA_ASR_1_1_MLX_4BIT  # noqa: E402

PUA = re.compile(r"[-]")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--out", type=Path, default=Path("benchmarks/results/pua_ab_mlx.json"))
    args = parser.parse_args()

    samples = load_samples(args.limit)
    print(f"樣本 {len(samples)} 筆，語料 {DATASET_ID}@{DATASET_REVISION[:12]}")

    backend = TeaMlxBackend(locate_prepared_model(TEA_ASR_1_1_MLX_4BIT))
    backend.load()

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
        print(f"  {index}/{len(samples)}: pua={len(pua_chars)}")

    summary = {
        "pass": "mlx-4bit",
        "model": TEA_ASR_1_1_MLX_4BIT.repo_id,
        "model_revision": TEA_ASR_1_1_MLX_4BIT.revision,
        "dataset": DATASET_ID,
        "dataset_revision": DATASET_REVISION,
        "samples": len(samples),
        "sentences_with_pua": sum(1 for r in rows if r["pua_count"] > 0),
        "pua_occurrences": sum(r["pua_count"] for r in rows),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"寫入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
