"""PUA A/B 第二步：用上游未量化的 BF16 checkpoint 跑同一批語料。

這支腳本刻意不依賴專案的 .venv（那個環境沒有 torch）。改在獨立的
venv 裡跑，例如：

    uv venv /tmp/bf16-env --python 3.12
    uv pip install --python /tmp/bf16-env/bin/python torch transformers accelerate \
        soundfile librosa huggingface_hub qwen-asr
    /tmp/bf16-env/bin/python benchmarks/pua_ab_bf16_pass.py --limit 10 \
        --out benchmarks/results/pua_ab_bf16.json

模型權重下載到專案的 models/ 目錄（gitignored），不進 ~/Library/Caches。
語料跟 MLX 那一輪用同一個 dataset/shard/revision，並列在 quality_eval.py。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

DATASET_ID = "adi-gov-tw/Taiwan-Tongues-ASR-CE-dataset-zhtw"
DATASET_REVISION = "ff0e8047bdce71c881c6d26eec7b2bbab6381ac1"
DATASET_SHARD = "test/test-000000.tar"

UPSTREAM_REPO = "JacobLinCool/TEA-ASR-1.1"
UPSTREAM_REVISION = "bda08df76d4fd6b487b4a1dd7f0bddf8541696f8"  # main HEAD at A/B time

PUA = re.compile(r"[-]")

REPO_ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = REPO_ROOT / "models"


@dataclass(slots=True)
class Sample:
    key: str
    audio: bytes
    reference: str


def load_samples(limit: int) -> list[Sample]:
    from huggingface_hub import hf_hub_download

    shard = hf_hub_download(
        DATASET_ID, DATASET_SHARD, repo_type="dataset", revision=DATASET_REVISION
    )
    samples: list[Sample] = []
    pending: dict[str, dict[str, bytes]] = {}
    with tarfile.open(shard) as archive:
        for member in archive:
            if not member.isfile():
                continue
            key, _, suffix = member.name.partition(".")
            if suffix not in {"mp3", "txt"}:
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            pending.setdefault(key, {})[suffix] = handle.read()
            entry = pending[key]
            if {"mp3", "txt"} <= entry.keys():
                samples.append(
                    Sample(key=key, audio=entry["mp3"], reference=entry["txt"].decode("utf-8").strip())
                )
                del pending[key]
                if len(samples) >= limit:
                    break
    return samples


def decode_mp3_to_wav_array(data: bytes):
    import io
    import subprocess

    import numpy as np

    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
        input=data,
        capture_output=True,
        check=True,
    )
    return np.frombuffer(result.stdout, dtype="<i2").astype(np.float32) / 32768.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--out", type=Path, default=Path("benchmarks/results/pua_ab_bf16.json"))
    parser.add_argument(
        "--repo",
        type=str,
        default=UPSTREAM_REPO,
        help="上游 BF16 repo id，預設 TEA-ASR-1.1 full；也可指定 mini 等其他變體",
    )
    parser.add_argument(
        "--revision",
        type=str,
        default=UPSTREAM_REVISION,
        help="對應 --repo 的 revision，預設值只適用於 full 版本",
    )
    parser.add_argument(
        "--pass-name",
        type=str,
        default="bf16-upstream",
        help="寫入 summary 的 pass 名稱，跑非 full 模型時建議改名以免跟既有結果混淆",
    )
    args = parser.parse_args()

    upstream_repo = args.repo
    upstream_revision = args.revision

    import os

    os.environ.setdefault("HF_HOME", str(MODELS_DIR / "hf-home"))

    from huggingface_hub import snapshot_download

    print(f"下載/定位上游模型 {upstream_repo}@{upstream_revision[:12]} -> {MODELS_DIR}")
    snapshot_path = snapshot_download(
        repo_id=upstream_repo,
        revision=upstream_revision,
        cache_dir=MODELS_DIR,
    )
    print(f"模型快照：{snapshot_path}")

    from qwen_asr import Qwen3ASRModel

    model = Qwen3ASRModel.from_pretrained(snapshot_path)

    samples = load_samples(args.limit)
    print(f"樣本 {len(samples)} 筆，語料 {DATASET_ID}@{DATASET_REVISION[:12]}")

    rows = []
    for index, sample in enumerate(samples, start=1):
        audio = decode_mp3_to_wav_array(sample.audio)
        started = time.perf_counter()
        result = model.transcribe(audio=(audio, 16_000), language="Chinese")[0]
        elapsed = time.perf_counter() - started
        hypothesis = str(result.text).strip()
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
        "pass": args.pass_name,
        "model": upstream_repo,
        "model_revision": upstream_revision,
        "dataset": DATASET_ID,
        "dataset_revision": DATASET_REVISION,
        "samples": len(samples),
        "sentences_with_pua": sum(1 for r in rows if r["pua_count"] > 0),
        "pua_occurrences": sum(r["pua_count"] for r in rows),
        "mean_inference_s": round(sum(r["inference_s"] for r in rows) / len(rows), 3) if rows else 0.0,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"寫入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
