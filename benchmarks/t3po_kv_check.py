"""關卡 2：KV cache 跨步驟重用的實證（速度與正確性）。

同一串輸入（前 N 句 ASR 辨識文字、每 2 字一塊、句尾 flush，不做時間合併），跑兩遍：
`reuse_kv=True`（最長共同前綴重用）與 `reuse_kv=False`（每步從頭 prefill）。
比較每一步的 READ/WRITE 決策與譯文是否逐步相同，以及每步延遲與 prefill token 數。
另跑一遍官方逐對滑動的 history（keep=None）量「窗口滿了之後」重用率掉多少。

    uv run python benchmarks/t3po_kv_check.py --model /Volumes/DigiFusion/tea-asr-models/t3po-mlx-4bit \\
        --hyp benchmarks/results/t3po_asr_hyp_200.json --out benchmarks/results/t3po_kv_check.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from t3po_replay import load_utterances
from t3po_simt import LATENCY_MODES, MlxT3poGenerator, SimtEngine


def run(generator, rows, *, reuse: bool, keep: int | None, window: int) -> dict:
    generator.reuse_kv = reuse
    generator.reset()
    generator.calls.clear()
    engine = SimtEngine(
        generator, "zh2en", LATENCY_MODES["native"], history_window=window, history_keep=keep
    )
    steps = []
    started = time.perf_counter()
    for row in rows:
        text = row["text"]
        for i in range(0, len(text), 2):
            d = engine.feed(text[i : i + 2])
            if d.stats:
                steps.append((d.action, d.text, d.stats))
        d = engine.flush()
        if d.stats:
            steps.append((d.action, d.text, d.stats))
    wall = time.perf_counter() - started
    lat = [s[2].total_s for s in steps]
    return {
        "reuse_kv": reuse,
        "history_window": window,
        "history_keep": keep,
        "calls": len(steps),
        "wall_s": round(wall, 2),
        "call_mean_s": round(statistics.fmean(lat), 4),
        "call_p95_s": round(sorted(lat)[int(len(lat) * 0.95)], 4),
        "prefill_tokens_mean": round(statistics.fmean(s[2].prefill_tokens for s in steps), 1),
        "prompt_tokens_mean": round(statistics.fmean(s[2].prompt_tokens for s in steps), 1),
        "reused_fraction": round(
            sum(s[2].reused_tokens for s in steps) / sum(s[2].prompt_tokens for s in steps), 4
        ),
        "steps": [(a, t) for a, t, _ in steps],
        "translation": engine.translation,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--hyp", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    rows = load_utterances(args.hyp, args.limit)
    generator = MlxT3poGenerator(args.model)
    generator.complete("暖機", force=True, mode=LATENCY_MODES["native"])
    reuse = run(generator, rows, reuse=True, keep=10, window=30)
    fresh = run(generator, rows, reuse=False, keep=10, window=30)
    sliding = run(generator, rows, reuse=True, keep=None, window=30)
    same_steps = sum(a == b for a, b in zip(reuse["steps"], fresh["steps"]))
    first_diff = next(
        (i for i, (a, b) in enumerate(zip(reuse["steps"], fresh["steps"])) if a != b), None
    )
    report = {
        "utterances": len(rows),
        "reuse_vs_fresh": {
            "steps_reuse": len(reuse["steps"]),
            "steps_fresh": len(fresh["steps"]),
            "identical_steps": same_steps,
            "first_divergent_step": first_diff,
            "identical_translation": reuse["translation"] == fresh["translation"],
            "divergence": (
                {"reuse": reuse["steps"][first_diff], "fresh": fresh["steps"][first_diff]}
                if first_diff is not None
                else None
            ),
        },
        "runs": [
            {k: v for k, v in r.items() if k not in {"steps", "translation"}}
            for r in (reuse, fresh, sliding)
        ],
        "translations": {
            "reuse": reuse["translation"],
            "fresh": fresh["translation"],
            "sliding": sliding["translation"],
        },
    }
    print(json.dumps({k: v for k, v in report.items() if k != "translations"}, ensure_ascii=False, indent=2))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
