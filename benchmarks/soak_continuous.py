"""連續 continuous session 壓力測試：檢查 backlog、RAM 與 final lag 是否持續成長。

    uv run python benchmarks/soak_continuous.py --minutes 60 --wav take.wav

以實際時間送音訊，循環播放同一段錄音。每分鐘記錄一次 RSS、佇列深度與
「最後一個 frame 送出到收到對應 final」的延遲。docs/05 要求一小時 backlog
不得持續成長。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path

from websockets.asyncio.client import connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tea_asr.config import AppPaths

FRAME_BYTES = 3200


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return round(ordered[index], 3)


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16_000, 1, 2):
            raise ValueError("WAV 必須是 16 kHz mono PCM16")
        return wav.readframes(wav.getnframes())


def rss_mb() -> dict[str, float]:
    out = subprocess.run(
        ["ps", "-Ao", "rss,command"], capture_output=True, text=True, check=False
    ).stdout
    totals: dict[str, float] = {}
    for line in out.splitlines():
        if ("tea_asr" in line or "tea-asr serve" in line) and "grep" not in line:
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            key = "worker" if "worker.entry" in parts[1] else "service"
            totals[key] = totals.get(key, 0.0) + int(parts[0]) / 1024
    return totals


async def run(wav: Path, minutes: int, url: str, out: Path) -> int:
    pcm = read_wav(wav)
    token = AppPaths.macos_default().token_file.read_text().strip()
    deadline = time.monotonic() + minutes * 60
    samples: list[dict[str, object]] = []
    finals = 0
    errors = 0
    lag_samples: list[float] = []
    #: Wall clock of sample 0, so a segment's end_sample maps to the moment the
    #: speaker stopped. That is what docs/05 means by end-of-speech-to-final.
    clock_origin = 0.0
    finalized_samples = 0

    async with connect(
        url, additional_headers={"Authorization": f"Bearer {token}"}, max_queue=256
    ) as socket:
        hello = json.loads(await socket.recv())
        await socket.send(
            json.dumps(
                {
                    "type": "session.start",
                    "request_id": "soak",
                    "profile": "continuous",
                    "audio": {"sample_rate": 16_000, "channels": 1, "format": "pcm_s16le"},
                    "language": "Chinese",
                    "durable": False,
                }
            )
        )
        started = json.loads(await socket.recv())
        if started.get("type") != "session.started":
            print(json.dumps(started, ensure_ascii=False))
            return 1
        window = started["send_until_sample"]
        print(f"hello={hello['protocol_version']} 開始 {minutes} 分鐘壓力測試")

        stop = asyncio.Event()

        def nonlocal_finalized(end_sample: int) -> None:
            nonlocal finalized_samples
            finalized_samples = max(finalized_samples, end_sample)

        async def receive() -> None:
            nonlocal finals, errors, window
            async for message in socket:
                event = json.loads(message)
                kind = event["type"]
                if kind == "flow.control":
                    window = max(window, event["send_until_sample"])
                elif kind == "transcript.final":
                    nonlocal_finalized(event["end_sample"])
                    finals += 1
                    spoken_at = clock_origin + event["end_sample"] / 16_000
                    lag_samples.append(time.monotonic() - spoken_at)
                elif kind in {"segment.error", "error"}:
                    errors += 1
                    print(f"  ! {event.get('code')}: {event.get('message','')}")
                    if kind == "error":
                        stop.set()
                        return

        receiver = asyncio.create_task(receive())
        seq = 0
        sample = 0
        clock_origin = time.monotonic()
        next_report = time.monotonic() + 60
        try:
            while time.monotonic() < deadline and not stop.is_set():
                for offset in range(0, len(pcm), FRAME_BYTES):
                    if time.monotonic() >= deadline or stop.is_set():
                        break
                    chunk = pcm[offset : offset + FRAME_BYTES]
                    end = sample + len(chunk) // 2
                    while end > window and not stop.is_set():
                        await asyncio.sleep(0.02)
                    if stop.is_set():
                        break
                    await socket.send(struct.pack("<QQ", seq, sample) + chunk)
                    seq += 1
                    sample = end
                    # Pace against an absolute schedule. Sleeping a fixed amount
                    # per frame accumulates drift, which would show up as
                    # latency that the service never actually caused.
                    ahead = (clock_origin + sample / 16_000) - time.monotonic()
                    if ahead > 0:
                        await asyncio.sleep(ahead)

                    if time.monotonic() >= next_report:
                        next_report += 60
                        row = {
                            "minute": len(samples) + 1,
                            "audio_s": round(sample / 16_000, 1),
                            "finals": finals,
                            "errors": errors,
                            # Audio that has been sent but has no final yet.
                            # docs/05: this must not keep growing over an hour.
                            "backlog_s": round((sample - finalized_samples) / 16_000, 2),
                            "lag_p50_s": _percentile(lag_samples, 0.5),
                            "lag_p95_s": _percentile(lag_samples, 0.95),
                            "rss_mb": {k: round(v, 1) for k, v in rss_mb().items()},
                        }
                        samples.append(row)
                        print(f"  {json.dumps(row, ensure_ascii=False)}")
                        lag_samples.clear()
        finally:
            with __import__("contextlib").suppress(Exception):
                await socket.send(
                    json.dumps(
                        {"type": "session.stop", "request_id": "soak-stop",
                         "through_seq": seq - 1 if seq else None}
                    )
                )
                await asyncio.wait_for(receiver, timeout=60)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"minutes": minutes, "finals": finals, "errors": errors, "reports": samples},
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    print(f"\n共 {finals} 段定稿、{errors} 個錯誤。寫入 {out}")
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", type=Path, required=True)
    parser.add_argument("--minutes", type=int, default=60)
    parser.add_argument("--url", default="ws://127.0.0.1:8327/v1/stream")
    parser.add_argument("--out", type=Path, default=Path("benchmarks/results/soak.json"))
    args = parser.parse_args()
    return asyncio.run(run(args.wav, args.minutes, args.url, args.out))


if __name__ == "__main__":
    raise SystemExit(main())
