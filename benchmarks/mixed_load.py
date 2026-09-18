"""混合負載：預覽在忙碌時必須降級，不能擋住收音與正式排程。

    uv run tea-asr serve
    uv run python benchmarks/mixed_load.py --wav take.wav

一邊跑 revisable 的 continuous session，一邊每隔幾秒打一次 HTTP 短音訊辨識。
docs/05 要求「preview超載不阻塞收音與正式排程」，所以檢查的是：
final 有沒有照常產生、HTTP 有沒有得到結果或誠實的 429，而不是預覽跑得多漂亮。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import struct
import sys
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

from websockets.asyncio.client import connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tea_asr.config import AppPaths

FRAME_BYTES = 3200


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16_000, 1, 2):
            raise ValueError("WAV 必須是 16 kHz mono PCM16")
        return wav.readframes(wav.getnframes())


def http_transcribe(base: str, token: str, pcm: bytes) -> tuple[int, float]:
    request = urllib.request.Request(
        f"{base}/v1/transcriptions?sample_rate=16000&channels=1&format=pcm_s16le",
        data=pcm,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream",
        },
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # fixed localhost URL
            response.read()
            return response.status, time.monotonic() - started
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code, time.monotonic() - started


async def run(wav: Path, ws_url: str, http_base: str, out: Path) -> int:
    pcm = read_wav(wav)
    token = AppPaths.macos_default().token_file.read_text().strip()
    probe = pcm[: 5 * 16_000 * 2]

    finals: list[str] = []
    previews: list[str] = []
    paused: list[str] = []
    errors: list[str] = []
    http_results: list[tuple[int, float]] = []

    async with connect(ws_url, additional_headers={"Authorization": f"Bearer {token}"}) as socket:
        hello = json.loads(await socket.recv())
        await socket.send(
            json.dumps(
                {
                    "type": "session.start",
                    "request_id": "mixed",
                    "profile": "continuous",
                    "audio": {"sample_rate": 16_000, "channels": 1, "format": "pcm_s16le"},
                    "language": "Chinese",
                    "durable": False,
                    "transcript_mode": "revisable",
                }
            )
        )
        started = json.loads(await socket.recv())
        if started.get("transcript_mode") != "revisable":
            print("服務未啟用串流預覽", file=sys.stderr)
            return 2
        window = started["send_until_sample"]
        origin = time.monotonic()
        print(f"protocol={hello['protocol_version']} 開始混合負載")

        async def receive() -> None:
            nonlocal window
            async for message in socket:
                event = json.loads(message)
                kind = event["type"]
                if kind == "flow.control":
                    window = max(window, event["send_until_sample"])
                elif kind == "transcript.partial":
                    previews.append(event["text"])
                elif kind == "transcript.final":
                    finals.append(event["text"])
                elif kind == "preview.status":
                    paused.append(event["reason"])
                elif kind in {"segment.error", "error"}:
                    errors.append(f"{kind}:{event.get('code')}")
                    if kind == "error":
                        return
                elif kind == "session.stopped":
                    return

        async def hammer() -> None:
            loop = asyncio.get_running_loop()
            while True:
                await asyncio.sleep(3)
                status, elapsed = await loop.run_in_executor(
                    None, http_transcribe, http_base, token, probe
                )
                http_results.append((status, elapsed))
                print(f"  HTTP {status} {elapsed:.2f}s")

        receiver = asyncio.create_task(receive())
        load = asyncio.create_task(hammer())
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
        load.cancel()
        await socket.send(
            json.dumps({"type": "session.stop", "request_id": "mixed-stop", "through_seq": seq - 1})
        )
        await asyncio.wait_for(receiver, timeout=120)

    ok_http = sum(1 for status, _ in http_results if status == 200)
    busy_http = sum(1 for status, _ in http_results if status == 429)
    summary = {
        "finals": len(finals),
        "previews": len(previews),
        "preview_paused_reasons": sorted(set(paused)),
        "http_requests": len(http_results),
        "http_ok": ok_http,
        "http_queue_full": busy_http,
        "http_other": len(http_results) - ok_http - busy_http,
        "http_slowest_s": round(max((e for _, e in http_results), default=0), 2),
        "errors": errors,
    }
    print("\n" + json.dumps(summary, ensure_ascii=False, indent=2))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "finals": finals}, ensure_ascii=False, indent=2) + "\n")
    print(f"寫入 {out}")
    return 1 if errors or summary["http_other"] else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", type=Path, required=True)
    parser.add_argument("--ws", default="ws://127.0.0.1:8327/v1/stream")
    parser.add_argument("--http", default="http://127.0.0.1:8327")
    parser.add_argument("--out", type=Path, default=Path("benchmarks/results/mixed-load.json"))
    args = parser.parse_args()
    return asyncio.run(run(args.wav, args.ws, args.http, args.out))


if __name__ == "__main__":
    raise SystemExit(main())
