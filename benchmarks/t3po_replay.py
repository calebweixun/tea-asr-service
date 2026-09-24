"""關卡 2：用 ASR 的真實輸出節奏重播，量 T3PO 串流翻譯跟不跟得上。

輸入是 `quality_eval.py` 的逐句明細（TEA-ASR-1.1-MLX-4bit 對 Taiwan-Tongues zh-TW test 的
真實辨識結果、每句音訊長度與 ASR 推論時間）。句子依序首尾相接成一條時間軸，然後依
`--cadence` 產生到達事件：

- `increments`：每句的辨識文字切成約每 0.5 秒一塊，在該句音訊時間內均勻到達，句尾送 flush。
  這是官方設計的上游節奏（串流 ASR 的 append-only 增量＋句尾 reset）；假設上游 0 延遲。
- `finals`：整句在「句尾＋該句實測 ASR 推論時間」一次到達並 flush。這是本服務目前唯一
  不可變的 ASR 輸出（transcript.final；partial 會被修訂，不能餵給 append-only 的翻譯）。

翻譯端是單一 worker：閒下來時把所有已到達的增量合併成一次 feed（佇列有上限、不會堆積），
遇到 flush 就把文字併入 buffer 後直接 force 翻譯，不先做一次多餘的 probe。

`--clock virtual` 用虛擬時鐘（閒置時直接跳到下一個到達時間，服務時間是真實量到的），
`--clock wall` 用牆鐘即時重播，給 GPU 爭用實驗與 ASR 負載程序同時跑。

    uv run python benchmarks/t3po_replay.py --model /Volumes/DigiFusion/tea-asr-models/t3po-mlx-4bit \\
        --hyp benchmarks/results/t3po_asr_hyp_200.json --mode native --cadence increments
"""

from __future__ import annotations

import argparse
import json
import math
import re
import resource
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from t3po_simt import LATENCY_MODES, MlxT3poGenerator, SimtEngine, source_units, split_source

PRIVATE_USE = re.compile("[-\U000f0000-\U000ffffd\U00100000-\U0010fffd]")


@dataclass
class Arrival:
    t: float
    utt: int
    text: str
    flush: bool


def load_utterances(path: Path, limit: int) -> list[dict]:
    rows = json.loads(path.read_text())["samples"][:limit]
    for row in rows:
        row["text"] = PRIVATE_USE.sub("", row["hypothesis"]).strip()
    return rows


def build_arrivals(rows: list[dict], cadence: str, speed: float, chunk_s: float) -> list[Arrival]:
    arrivals: list[Arrival] = []
    start = 0.0
    for index, row in enumerate(rows):
        duration = row["audio_s"] / speed
        end = start + duration
        text = row["text"]
        if cadence == "finals":
            at = end + row["inference_s"]
            arrivals.append(Arrival(at, index, text, True))
        else:
            pieces = max(1, min(len(text), math.ceil(duration / chunk_s)))
            bounds = [round(len(text) * (j + 1) / pieces) for j in range(pieces)]
            previous = 0
            for j, bound in enumerate(bounds):
                arrivals.append(Arrival(start + duration * (j + 1) / pieces, index, text[previous:bound], False))
                previous = bound
            arrivals[-1].flush = True
        start = end
    return arrivals


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


def summarize(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 4),
        "p50": round(percentile(values, 0.5), 4),
        "p95": round(percentile(values, 0.95), 4),
        "max": round(max(values), 4),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--hyp", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--mode", choices=sorted(LATENCY_MODES), default="native")
    parser.add_argument("--cadence", choices=["increments", "finals"], default="increments")
    parser.add_argument("--speed", type=float, default=1.0, help="時間軸壓縮倍率（>1 = 語速更快）")
    parser.add_argument("--chunk-s", type=float, default=0.5)
    parser.add_argument("--clock", choices=["virtual", "wall"], default="virtual")
    parser.add_argument("--start-at", type=float, default=None, help="wall 模式：共同起跑的 epoch 秒")
    parser.add_argument("--no-reuse", action="store_true", help="每步清空 KV cache（對照組）")
    parser.add_argument("--history-window", type=int, default=30)
    parser.add_argument("--history-keep", type=int, default=None, help="超過窗口時砍到剩幾對；省略=官方逐對滑動")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    import mlx.core as mx

    rows = load_utterances(args.hyp, args.limit)
    arrivals = build_arrivals(rows, args.cadence, args.speed, args.chunk_s)
    generator = MlxT3poGenerator(args.model, reuse_kv=not args.no_reuse)
    engine = SimtEngine(
        generator,
        "zh2en",
        LATENCY_MODES[args.mode],
        history_window=args.history_window,
        history_keep=args.history_keep,
    )
    # 暖機：讓 Metal kernel 編譯與第一次 prefill 不算進量測。
    generator.complete("暖機", force=True, mode=LATENCY_MODES[args.mode])
    generator.reset()
    generator.calls.clear()
    mx.reset_peak_memory()

    if args.clock == "wall":
        origin = args.start_at if args.start_at is not None else time.time() + 1.0
        while time.time() < origin:
            time.sleep(0.01)
        mono0 = time.perf_counter()

        def now() -> float:
            return time.perf_counter() - mono0

        def wait_until(t: float) -> None:
            while (remaining := t - now()) > 0:
                time.sleep(min(0.01, remaining))
    else:
        clock = [0.0]

        def now() -> float:
            return clock[0]

        def wait_until(t: float) -> None:
            clock[0] = max(clock[0], t)

    def run_call(fn):
        started = time.perf_counter()
        decision = fn()
        elapsed = time.perf_counter() - started
        if args.clock == "virtual":
            clock[0] += elapsed
        return decision, elapsed

    # 每個到達的字元在哪個時間被提交進譯文（commit），用來算端到端延遲。
    pending_chars: list[tuple[float, int]] = []  # (arrival t, utt)
    char_commit_lag: list[float] = []
    utt_end = {}
    t_cursor = 0.0
    for index, row in enumerate(rows):
        t_cursor += row["audio_s"] / args.speed
        utt_end[index] = t_cursor
    utt_done: dict[int, float] = {}
    decisions: list[dict] = []
    translations_seen: list[str] = []
    append_only_violations = 0
    backlog_max = 0
    busy = 0.0
    cursor = 0
    while cursor < len(arrivals):
        if arrivals[cursor].t > now():
            wait_until(arrivals[cursor].t)
        ready = [a for a in arrivals[cursor:] if a.t <= now()]
        backlog_max = max(backlog_max, len(ready))
        cursor += len(ready)
        text = "".join(a.text for a in ready)
        flush = any(a.flush for a in ready)
        for a in ready:
            pending_chars.extend((a.t, a.utt) for _ in a.text)
        if flush:
            # 句尾：文字直接併入 buffer 後 force，不先做一次 probe。
            engine.buffer.extend(t for t in split_source(text, "zh2en") if t)
            decision, elapsed = run_call(engine.flush)
        else:
            decision, elapsed = run_call(lambda text=text: engine.feed(text))
        busy += elapsed
        if decision.stats is not None:
            decisions.append(
                {
                    "t": round(now(), 3),
                    "action": decision.action,
                    "forced": decision.forced,
                    "source_units": source_units(split_source(decision.source, "zh2en"), "zh2en"),
                    "text": decision.text,
                    **asdict(decision.stats),
                }
            )
        if decision.action == "TRANS":
            finished = now()
            for arrived, utt in pending_chars:
                char_commit_lag.append(finished - arrived)
                utt_done[utt] = finished
            pending_chars.clear()
            current = engine.translation
            if translations_seen and not current.startswith(translations_seen[-1].rstrip()):
                append_only_violations += 1
            translations_seen.append(current)
        elif decision.action == "SKIP" and not engine.buffer:
            pending_chars.clear()

    stream_s = utt_end[len(rows) - 1]
    source_chars = sum(len(r["text"]) for r in rows)
    calls = [d for d in decisions]
    waits = [d for d in calls if d["action"] == "WAIT"]
    trans = [d for d in calls if d["action"] == "TRANS"]
    utt_lag = [utt_done[i] - utt_end[i] for i in utt_done]
    decode_rates = [
        (d["generated_tokens"] - 1) / (d["total_s"] - d["first_token_s"])
        for d in trans
        if d["generated_tokens"] > 1 and d["total_s"] > d["first_token_s"]
    ]
    summary = {
        "model": str(args.model),
        "mode": args.mode,
        "cadence": args.cadence,
        "speed": args.speed,
        "clock": args.clock,
        "reuse_kv": not args.no_reuse,
        "history_window": args.history_window,
        "history_keep": args.history_keep,
        "utterances": len(rows),
        "stream_s": round(stream_s, 2),
        "source_chars": source_chars,
        "source_chars_per_s_arrival": round(source_chars / stream_s, 3),
        "calls": len(calls),
        "wait_calls": len(waits),
        "trans_calls": len(trans),
        "forced_calls": sum(d["forced"] for d in calls),
        "decision_latency_s": summarize([d["first_token_s"] for d in calls]),
        "call_latency_s": summarize([d["total_s"] for d in calls]),
        "wait_call_latency_s": summarize([d["total_s"] for d in waits]),
        "trans_call_latency_s": summarize([d["total_s"] for d in trans]),
        "char_commit_lag_s": summarize(char_commit_lag),
        "utterance_translation_lag_s": summarize(utt_lag),
        "translator_busy_s": round(busy, 2),
        "utilization": round(busy / stream_s, 4),
        "chars_per_busy_s": round(source_chars / busy, 2) if busy else None,
        "backlog_max_arrivals": backlog_max,
        "prompt_tokens": summarize([d["prompt_tokens"] for d in calls]),
        "prefill_tokens": summarize([d["prefill_tokens"] for d in calls]),
        "reused_fraction": round(
            sum(d["reused_tokens"] for d in calls) / max(1, sum(d["prompt_tokens"] for d in calls)), 4
        ),
        "decode_tokens_per_s": summarize(decode_rates),
        "append_only_violations": append_only_violations,
        "mlx_peak_memory_gib": round(mx.get_peak_memory() / 2**30, 3),
        "mlx_active_memory_gib": round(mx.get_active_memory() / 2**30, 3),
        "max_rss_gib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**30, 3),
        "load_s": round(generator.load_s, 2),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {"summary": summary, "decisions": decisions, "translation": engine.translation},
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
