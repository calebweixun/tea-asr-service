"""對著麥克風說話，即時看到辨識結果。

需要 ffmpeg（`brew install ffmpeg`）。ffmpeg 負責擷取與重採樣到
16 kHz mono PCM16，本腳本只做 WebSocket 協定的部分。

    uv run python examples/mic_stream.py --list-devices
    uv run python examples/mic_stream.py --device 2

預設是 final_only：說完按 Enter（或 Ctrl-C）才會送出並定稿。
服務若啟用了實驗性的 P2a 預覽，加 --revisable 就能看到邊說邊修訂：

    TEA_ASR_EXPERIMENTAL_REVISABLE_PREVIEW=1 uv run tea-asr serve
    uv run python examples/mic_stream.py --device 2 --revisable
"""

from __future__ import annotations

import argparse
import asyncio
import json
import struct
import subprocess
import sys

from websockets.asyncio.client import connect

from tea_asr.config import AppPaths

#: docs/04-api.md caps one binary frame at 6,400 PCM bytes; 100 ms is 3,200.
FRAME_BYTES = 3200
MAX_UTTERANCE_BYTES = 960_000


def list_devices() -> int:
    subprocess.run(
        ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        check=False,
    )
    return 0


def start_ffmpeg(device: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            "ffmpeg",
            "-loglevel", "error",
            "-f", "avfoundation",
            "-i", f":{device}",
            "-ac", "1",
            "-ar", "16000",
            "-f", "s16le",
            "-",
        ],
        stdout=subprocess.PIPE,
        stderr=None,
    )


def render(event: dict) -> None:
    kind = event.get("type")
    if kind == "transcript.partial":
        sys.stdout.write(f"\r\033[K… {event['text']}")
        sys.stdout.flush()
    elif kind == "transcript.final":
        warn = " ".join(event.get("warnings", []))
        suffix = f"   [{warn}]" if warn else ""
        sys.stdout.write(f"\r\033[K✓ {event['text']}{suffix}\n")
        sys.stdout.flush()
    elif kind == "segment.skipped":
        sys.stdout.write("\r\033[K（沒有偵測到語音）\n")
    elif kind in {"segment.error", "error"}:
        sys.stdout.write(f"\r\033[K✗ {event.get('code')}: {event.get('message', '')}\n")


async def run(device: str, url: str, transcript_mode: str) -> int:
    token_file = AppPaths.macos_default().token_file
    if not token_file.exists():
        print(f"找不到 token，請先啟動服務：{token_file}", file=sys.stderr)
        return 2
    token = token_file.read_text().strip()

    ffmpeg = start_ffmpeg(device)
    assert ffmpeg.stdout is not None
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()

    async with connect(url, additional_headers={"Authorization": f"Bearer {token}"}) as socket:
        hello = json.loads(await socket.recv())
        await socket.send(
            json.dumps(
                {
                    "type": "session.start",
                    "request_id": "mic-start",
                    "profile": "utterance",
                    "audio": {"sample_rate": 16_000, "channels": 1, "format": "pcm_s16le"},
                    "language": "Chinese",
                    "durable": False,
                    "transcript_mode": transcript_mode,
                }
            )
        )
        started = json.loads(await socket.recv())
        if started.get("type") != "session.started":
            print(json.dumps(started, ensure_ascii=False, indent=2), file=sys.stderr)
            ffmpeg.terminate()
            return 1
        print(
            f"協定 {hello['protocol_version']}、模式 {started['transcript_mode']}。"
            "開始說話，按 Enter 結束。\n"
        )

        async def receive() -> None:
            async for message in socket:
                event = json.loads(message)
                render(event)
                if event["type"] == "session.stopped":
                    return

        async def wait_for_enter() -> None:
            await loop.run_in_executor(None, sys.stdin.readline)
            stopping.set()

        receiver = asyncio.create_task(receive())
        enter = asyncio.create_task(wait_for_enter())

        seq = 0
        sample = 0
        sent_bytes = 0
        try:
            while not stopping.is_set():
                chunk = await loop.run_in_executor(None, ffmpeg.stdout.read, FRAME_BYTES)
                if not chunk:
                    break
                if sent_bytes + len(chunk) > MAX_UTTERANCE_BYTES:
                    print("\n達到 30 秒單段上限，先定稿。", file=sys.stderr)
                    break
                await socket.send(struct.pack("<QQ", seq, sample) + chunk)
                seq += 1
                sample += len(chunk) // 2
                sent_bytes += len(chunk)
        finally:
            ffmpeg.terminate()
            enter.cancel()

        await socket.send(
            json.dumps(
                {
                    "type": "session.stop",
                    "request_id": "mic-stop",
                    "through_seq": seq - 1 if seq else None,
                }
            )
        )
        await receiver
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="0", help="avfoundation 音訊裝置編號")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--url", default="ws://127.0.0.1:8765/v1/stream")
    parser.add_argument("--revisable", action="store_true", help="要求 P2a 串流預覽")
    args = parser.parse_args()
    if args.list_devices:
        return list_devices()
    mode = "revisable" if args.revisable else "final_only"
    try:
        return asyncio.run(run(args.device, args.url, mode))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
