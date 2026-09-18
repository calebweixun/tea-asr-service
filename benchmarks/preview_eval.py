"""P2a 驗收量測：串流預覽的延遲、修訂行為與對 final 的影響。

    uv run tea-asr serve
    uv run python benchmarks/preview_eval.py --wav take.wav

依 docs/07 量測：
- 首次可見延遲：片段開始說話到第一個 partial 出現。
- 修訂次數與文字抖動。
- 錯改對／對改錯：以同段的 final 為基準，看每次修訂讓前綴更接近還是更遠離定稿。
- final 品質：與 final-only 模式跑同一段音訊比較，確認開了預覽不會讓定稿變差。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import struct
import sys
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

from websockets.asyncio.client import connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tea_asr.config import AppPaths

FRAME_BYTES = 3200


@dataclass
class Segment:
    index: int
    start_sample: int = 0
    first_partial_at: float | None = None
    spoke_at: float | None = None
    partials: list[tuple[float, str]] = field(default_factory=list)
    final: str | None = None
    final_at: float | None = None
    final_end_sample: int = 0


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16_000, 1, 2):
            raise ValueError("WAV 必須是 16 kHz mono PCM16")
        return wav.readframes(wav.getnframes())


async def run_session(pcm: bytes, url: str, revisable: bool) -> tuple[list[Segment], str]:
    token = AppPaths.macos_default().token_file.read_text().strip()
    segments: dict[int, Segment] = {}
    protocol = "?"

    async with connect(url, additional_headers={"Authorization": f"Bearer {token}"}) as socket:
        hello = json.loads(await socket.recv())
        protocol = hello["protocol_version"]
        await socket.send(
            json.dumps(
                {
                    "type": "session.start",
                    "request_id": "preview-eval",
                    "profile": "continuous",
                    "audio": {"sample_rate": 16_000, "channels": 1, "format": "pcm_s16le"},
                    "language": "Chinese",
                    "durable": False,
                    "transcript_mode": "revisable" if revisable else "final_only",
                }
            )
        )
        started = json.loads(await socket.recv())
        if started.get("type") != "session.started":
            raise RuntimeError(json.dumps(started, ensure_ascii=False))
        if revisable and started.get("transcript_mode") != "revisable":
            raise RuntimeError("服務未啟用串流預覽")
        window = started["send_until_sample"]
        origin = time.monotonic()

        def segment(index: int) -> Segment:
            return segments.setdefault(index, Segment(index=index))

        async def receive() -> None:
            nonlocal window
            async for message in socket:
                event = json.loads(message)
                kind = event["type"]
                now = time.monotonic()
                if kind == "flow.control":
                    window = max(window, event["send_until_sample"])
                elif kind == "speech.started":
                    item = segment(event["segment_index"])
                    item.start_sample = event["start_sample"]
                    item.spoke_at = origin + event["start_sample"] / 16_000
                elif kind == "transcript.partial":
                    item = segment(event["segment_index"])
                    if item.first_partial_at is None:
                        item.first_partial_at = now
                    item.partials.append((now, event["text"]))
                elif kind == "transcript.final":
                    item = segment(event["segment_index"])
                    item.final = event["text"]
                    item.final_at = now
                    item.final_end_sample = event["end_sample"]
                elif kind == "session.stopped":
                    return

        receiver = asyncio.create_task(receive())
        seq = 0
        sample = 0
        for offset in range(0, len(pcm), FRAME_BYTES):
            chunk = pcm[offset : offset + FRAME_BYTES]
            end = sample + len(chunk) // 2
            while end > window:
                await asyncio.sleep(0.02)
            await socket.send(struct.pack("<QQ", seq, sample) + chunk)
            seq += 1
            sample = end
            ahead = (origin + sample / 16_000) - time.monotonic()
            if ahead > 0:
                await asyncio.sleep(ahead)
        await socket.send(
            json.dumps(
                {"type": "session.stop", "request_id": "preview-stop", "through_seq": seq - 1}
            )
        )
        await asyncio.wait_for(receiver, timeout=120)

    return [segments[key] for key in sorted(segments)], protocol


def prefix_accuracy(text: str, final: str) -> float:
    """How much of a partial survives into the final, as a fraction of itself."""

    if not text:
        return 1.0
    matched = 0
    for a, b in zip(text, final, strict=False):
        if a != b:
            break
        matched += 1
    return matched / len(text)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", type=Path, required=True)
    parser.add_argument("--url", default="ws://127.0.0.1:8327/v1/stream")
    parser.add_argument("--out", type=Path, default=Path("benchmarks/results/preview.json"))
    args = parser.parse_args()

    pcm = read_wav(args.wav)
    print("=== revisable ===")
    revisable, protocol = asyncio.run(run_session(pcm, args.url, revisable=True))
    print(f"protocol_version={protocol}")
    print("\n=== final_only 對照 ===")
    baseline, _ = asyncio.run(run_session(pcm, args.url, revisable=False))

    first_latency: list[float] = []
    revisions: list[int] = []
    improved = 0
    regressed = 0
    unchanged = 0
    rows = []
    for item in revisable:
        if item.final is None:
            continue
        latency = None
        if item.first_partial_at is not None and item.spoke_at is not None:
            latency = item.first_partial_at - item.spoke_at
            first_latency.append(latency)
        revisions.append(len(item.partials))

        previous = None
        for _, text in item.partials:
            score = prefix_accuracy(text, item.final)
            if previous is not None:
                if score > previous + 1e-9:
                    improved += 1
                elif score < previous - 1e-9:
                    regressed += 1
                else:
                    unchanged += 1
            previous = score

        rows.append(
            {
                "index": item.index,
                "first_partial_latency_s": round(latency, 3) if latency else None,
                "revisions": len(item.partials),
                "partials": [text for _, text in item.partials],
                "final": item.final,
            }
        )
        print(
            f"  [{item.index:2d}] 首次可見 "
            f"{format(latency, '.2f') if latency else '  -  '}s  "
            f"修訂 {len(item.partials)}  {item.final}"
        )

    finals_match = [a.final for a in revisable] == [b.final for b in baseline]
    summary = {
        "protocol_version": protocol,
        "segments": len(rows),
        "first_partial_latency_p50_s": round(statistics.median(first_latency), 3)
        if first_latency
        else None,
        "first_partial_latency_p95_s": round(sorted(first_latency)[int(len(first_latency) * 0.95)], 3)
        if len(first_latency) > 1
        else None,
        "revisions_median": statistics.median(revisions) if revisions else None,
        "revision_improved": improved,
        "revision_regressed": regressed,
        "revision_unchanged": unchanged,
        "finals_identical_to_final_only": finals_match,
        "baseline_finals": [item.final for item in baseline],
    }
    print("\n" + json.dumps(summary, ensure_ascii=False, indent=2))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"summary": summary, "segments": rows}, ensure_ascii=False, indent=2) + "\n"
    )
    print(f"寫入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
