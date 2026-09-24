"""關卡 2 的 ASR 負載：用生產的 TeaMlxBackend 即時重播語料，量 ASR 自己的 RTF 與 final 延遲。

時間軸與 `t3po_replay.py` 相同（語料句子首尾相接）。行為模仿 continuous＋revisable session：

- 句尾（音訊時間到）做一次 final 辨識；final 優先。
- 句中每累積 0.8 秒做一次 preview 辨識（P2a 的 min_audio_ms／min_interval_ms=800），
  落後時只跑最新的一個 preview（P2a「最新待跑任務合併」），不排隊補跑舊的。

單獨跑一次當基準，再跟翻譯程序同時跑（`--start-at` 共同起跑），比較兩次的 RTF。

    uv run python benchmarks/t3po_asr_load.py --limit 100 --out benchmarks/results/t3po_asr_solo.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from quality_eval import decode_mp3, load_samples

from tea_asr.backend import TeaMlxBackend
from tea_asr.model_manager import locate_prepared_model
from tea_asr.model_spec import TEA_ASR_1_1_MLX_4BIT

PREVIEW_S = 0.8


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(len(ordered) * q))], 4)


def stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 4),
        "p50": pct(values, 0.5),
        "p95": pct(values, 0.95),
        "max": round(max(values), 4),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--start-at", type=float, default=None)
    parser.add_argument("--no-preview", action="store_true")
    parser.add_argument("--flat-out", action="store_true", help="不照時間軸，全部 final 背靠背跑")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    import mlx.core as mx

    samples = load_samples(args.limit)
    audios = [decode_mp3(s.audio) for s in samples]
    backend = TeaMlxBackend(locate_prepared_model(TEA_ASR_1_1_MLX_4BIT))
    backend.load()
    backend.transcribe(audios[0])  # 暖機
    mx.reset_peak_memory()

    starts, ends = [], []
    cursor = 0.0
    for audio in audios:
        starts.append(cursor)
        cursor += audio.size / 16_000
        ends.append(cursor)

    origin = args.start_at if args.start_at is not None else time.time() + 1.0
    while time.time() < origin:
        time.sleep(0.01)
    mono0 = time.perf_counter()

    def now() -> float:
        return time.perf_counter() - mono0

    calls: list[dict] = []

    def run(kind: str, index: int, audio) -> None:
        started = now()
        result = backend.transcribe(audio)
        finished = now()
        audio_s = audio.size / 16_000
        calls.append(
            {
                "kind": kind,
                "utt": index,
                "audio_s": round(audio_s, 3),
                "inference_s": round(finished - started, 4),
                "rtf": (finished - started) / audio_s,
                "final_latency_s": round(finished - ends[index], 4) if kind == "final" else None,
                "text": result.text if kind == "final" else None,
            }
        )

    if args.flat_out:
        for index, audio in enumerate(audios):
            run("final", index, audio)
    else:
        next_final = 0
        preview_done: dict[int, float] = {}
        while next_final < len(audios):
            t = now()
            if t >= ends[next_final]:
                run("final", next_final, audios[next_final])
                next_final += 1
                continue
            index = next_final
            elapsed = t - starts[index] if t >= starts[index] else 0.0
            last = preview_done.get(index, 0.0)
            if not args.no_preview and elapsed >= PREVIEW_S and elapsed - last >= PREVIEW_S:
                samples_n = int(elapsed * 16_000)
                preview_done[index] = elapsed
                run("preview", index, audios[index][:samples_n])
                continue
            time.sleep(0.005)

    finals = [c for c in calls if c["kind"] == "final"]
    previews = [c for c in calls if c["kind"] == "preview"]
    summary = {
        "utterances": len(audios),
        "stream_s": round(ends[-1], 2),
        "flat_out": args.flat_out,
        "preview": not args.no_preview,
        "final_rtf": stats([c["rtf"] for c in finals]),
        "final_inference_s": stats([c["inference_s"] for c in finals]),
        "final_latency_s": stats([c["final_latency_s"] for c in finals]),
        "preview_calls": len(previews),
        "preview_rtf": stats([c["rtf"] for c in previews]),
        "wall_s": round(now(), 2),
        "mlx_peak_memory_gib": round(mx.get_peak_memory() / 2**30, 3),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "calls": calls}, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
