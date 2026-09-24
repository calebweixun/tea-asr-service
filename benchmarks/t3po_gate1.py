"""關卡 1：T3PO MLX 4bit 載得起來、翻得出來嗎？

- 載入時間、MLX 峰值記憶體、進程 max RSS。
- prefill／decode tokens/sec（冷 cache、單一長 prompt）。
- 20 句質性抽樣：Taiwan-Tongues zh-TW test shard 的轉錄文字（`quality_eval.py` 明細的
  `reference` 欄），取第 0、10、…、190 句。每句用全新 session，照串流協定每次送 2 個字、
  句尾 flush（native 檔位）；另外記一次「整句一次 force」的譯文當對照。
- en→zh 回譯：把上面 20 句英文譯文當英文原文，每次送 2 個詞、句尾 flush。repo 內沒有英文
  語料，這只用來看輸出字形（簡／繁）與是否產出中文，**不是品質評估**。

    uv run python benchmarks/t3po_gate1.py --model /Volumes/DigiFusion/tea-asr-models/t3po-mlx-4bit \\
        --hyp benchmarks/results/t3po_asr_hyp_200.json --out benchmarks/results/t3po_gate1.json
"""

from __future__ import annotations

import argparse
import json
import resource
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from t3po_simt import (
    LATENCY_MODES,
    MlxT3poGenerator,
    SimtEngine,
    build_user_message,
    parse_model_response,
)


def stream_translate(generator, direction, text, step):
    engine = SimtEngine(generator, direction, LATENCY_MODES["native"])
    if direction == "zh2en":
        pieces = [text[i : i + step] for i in range(0, len(text), step)]
    else:
        words = text.split()
        pieces = [" ".join(words[i : i + step]) + " " for i in range(0, len(words), step)]
    trace = []
    for piece in pieces:
        decision = engine.feed(piece)
        trace.append({"in": piece, "action": decision.action, "out": decision.text})
    decision = engine.flush()
    trace.append({"in": "<flush>", "action": decision.action, "out": decision.text})
    return engine.translation, trace


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--hyp", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    import mlx.core as mx

    rows = json.loads(args.hyp.read_text())["samples"]
    picks = [rows[i] for i in range(0, 200, 10)]

    generator = MlxT3poGenerator(args.model)
    load_s = generator.load_s
    after_load_active = mx.get_active_memory()
    generator.complete("暖機", force=True, mode=LATENCY_MODES["native"])

    # 吞吐：冷 cache、長 prompt、強制產生到 max_new_tokens 或 EOS。
    paragraph = "".join(r["reference"] + "。" for r in rows[:60])
    generator.reset()
    raw, cold = generator.complete(
        build_user_message("zh2en", "", paragraph), force=True, mode=LATENCY_MODES["native"]
    )
    throughput = {
        "prompt_tokens": cold.prompt_tokens,
        "prefill_tokens_per_s": round(cold.prefill_tokens / cold.prefill_s, 1),
        "generated_tokens": cold.generated_tokens,
        "decode_tokens_per_s": round(
            (cold.generated_tokens - 1) / (cold.total_s - cold.first_token_s), 2
        ),
        "sample_output_head": parse_model_response(raw)[1][:200],
    }

    samples = []
    for row in picks:
        source = row["reference"]
        generator.reset()
        started = time.perf_counter()
        zh2en, trace = stream_translate(generator, "zh2en", source, 2)
        stream_s = time.perf_counter() - started
        generator.reset()
        raw, _ = generator.complete(
            build_user_message("zh2en", "", source), force=True, mode=LATENCY_MODES["native"]
        )
        whole = parse_model_response(raw)[1]
        generator.reset()
        back, back_trace = stream_translate(generator, "en2zh", zh2en, 2)
        samples.append(
            {
                "key": row["key"],
                "source": source,
                "zh2en_streaming": zh2en,
                "zh2en_whole_sentence": whole,
                "zh2en_trace": trace,
                "zh2en_stream_s": round(stream_s, 3),
                "en2zh_backtranslation": back,
                "en2zh_trace": back_trace,
            }
        )
        print(f"{source}\n  → {zh2en}\n  ⇒ {whole}\n  ↩ {back}", flush=True)

    summary = {
        "model": str(args.model),
        "load_s": round(load_s, 2),
        "mlx_active_after_load_gib": round(after_load_active / 2**30, 3),
        "mlx_peak_memory_gib": round(mx.get_peak_memory() / 2**30, 3),
        "max_rss_gib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**30, 3),
        "throughput": throughput,
        "samples": len(samples),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"summary": summary, "samples": samples}, ensure_ascii=False, indent=2) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
