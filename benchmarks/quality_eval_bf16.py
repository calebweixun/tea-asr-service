"""MER 量測：BF16 上游模型（full 或 mini）版本，跟 quality_eval.py 同一套指標與正規化規則。

跟 quality_eval.py 的差異只在推論後端：這支腳本用 qwen_asr.Qwen3ASRModel（torch），
quality_eval.py 用 TeaMlxBackend（MLX）。統計邏輯（edit_distance/normalize/
tokenize_mixed/PRIVATE_USE 判定）直接從 quality_eval.py import，避免另兜一套
可能跟既有數字對不齊的計算方式。

刻意不依賴專案 .venv（那個環境沒有 torch），比照 pua_ab_bf16_pass.py 的方式，
在獨立 venv 執行：

    uv venv /tmp/bf16-env --python 3.12
    uv pip install --python /tmp/bf16-env/bin/python torch transformers accelerate \
        soundfile librosa huggingface_hub qwen-asr
    /tmp/bf16-env/bin/python benchmarks/quality_eval_bf16.py --limit 30 \
        --repo JacobLinCool/TEA-ASR-1.1-mini --revision <rev> \
        --model-label mini-bf16 \
        --out benchmarks/results/quality_mini_bf16_30.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from quality_eval import (
    DATASET_ID,
    DATASET_REVISION,
    PRIVATE_USE,
    Report,
    chinese_chars,
    edit_distance,
    english_words,
    load_samples,
    normalize,
    tokenize_mixed,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = REPO_ROOT / "models"


def decode_mp3_to_wav_array(data: bytes):
    import subprocess

    import numpy as np

    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
        input=data,
        capture_output=True,
        check=True,
    )
    return np.frombuffer(result.stdout, dtype="<i2").astype("float32") / 32768.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--out", type=Path, default=Path("benchmarks/results/quality_bf16.json"))
    parser.add_argument("--repo", type=str, required=True)
    parser.add_argument("--revision", type=str, required=True)
    parser.add_argument("--model-label", type=str, required=True)
    args = parser.parse_args()

    import os

    os.environ.setdefault("HF_HOME", str(MODELS_DIR / "hf-home"))

    from huggingface_hub import snapshot_download

    print(f"下載/定位模型 {args.repo}@{args.revision[:12]} -> {MODELS_DIR}")
    snapshot_path = snapshot_download(repo_id=args.repo, revision=args.revision, cache_dir=MODELS_DIR)

    from qwen_asr import Qwen3ASRModel

    model = Qwen3ASRModel.from_pretrained(snapshot_path)

    samples = load_samples(args.limit)
    print(f"樣本 {len(samples)} 筆，語料 {DATASET_ID}@{DATASET_REVISION[:12]}")

    report = Report()
    rtfs: list[float] = []
    leaks = 0
    rows: list[dict[str, object]] = []
    for index, sample in enumerate(samples, start=1):
        audio = decode_mp3_to_wav_array(sample.audio)
        started = time.perf_counter()
        result = model.transcribe(audio=(audio, 16_000), language="Chinese")[0]
        elapsed = time.perf_counter() - started
        duration = audio.size / 16_000
        rtfs.append(elapsed / duration if duration else 0.0)

        hypothesis = str(result.text).strip()
        if PRIVATE_USE.search(hypothesis):
            leaks += 1

        raw_ref, raw_hyp = tokenize_mixed(sample.reference), tokenize_mixed(hypothesis)
        report.raw_mer.add(edit_distance(raw_ref, raw_hyp), len(raw_ref))

        norm_reference, norm_hypothesis = normalize(sample.reference), normalize(hypothesis)
        norm_ref, norm_hyp = tokenize_mixed(norm_reference), tokenize_mixed(norm_hypothesis)
        report.norm_mer.add(edit_distance(norm_ref, norm_hyp), len(norm_ref))

        ref_chars, hyp_chars = chinese_chars(norm_reference), chinese_chars(norm_hypothesis)
        report.cer.add(edit_distance(ref_chars, hyp_chars), len(ref_chars))

        ref_words, hyp_words = english_words(norm_reference), english_words(norm_hypothesis)
        if ref_words:
            report.wer.add(edit_distance(ref_words, hyp_words), len(ref_words))

        rows.append(
            {
                "key": sample.key,
                "reference": sample.reference,
                "hypothesis": hypothesis,
                "private_use": bool(PRIVATE_USE.search(hypothesis)),
                "audio_s": round(duration, 3),
                "inference_s": round(elapsed, 3),
            }
        )
        if index % 10 == 0:
            print(f"  {index}/{len(samples)} …")

    summary = {
        "dataset": DATASET_ID,
        "dataset_revision": DATASET_REVISION,
        "model": args.model_label,
        "model_repo": args.repo,
        "model_revision": args.revision,
        "samples": len(samples),
        "raw_mer": round(report.raw_mer.rate, 4),
        "normalized_mer": round(report.norm_mer.rate, 4),
        "cer": round(report.cer.rate, 4),
        "wer": round(report.wer.rate, 4) if report.wer.length else None,
        "english_reference_tokens": report.wer.length,
        "private_use_rate": round(leaks / len(samples), 4) if samples else 0.0,
        "rtf_p50": round(statistics.median(rtfs), 4) if rtfs else None,
        "rtf_p95": round(sorted(rtfs)[int(len(rtfs) * 0.95)], 4) if len(rtfs) > 1 else None,
        "mean_inference_s": round(sum(r["inference_s"] for r in rows) / len(rows), 3) if rows else 0.0,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "samples": rows}, ensure_ascii=False, indent=2) + "\n")
    print(f"明細寫入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
