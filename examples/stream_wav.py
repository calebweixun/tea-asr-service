from __future__ import annotations

import argparse
import asyncio
import json
import struct
import wave
from pathlib import Path

from websockets.asyncio.client import connect

from tea_asr.config import AppPaths


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16_000, 1, 2):
            raise ValueError("WAV must be 16 kHz mono PCM16")
        return wav.readframes(wav.getnframes())


async def run(path: Path, url: str, realtime: bool, transcript_mode: str) -> None:
    token = AppPaths.macos_default().token_file.read_text().strip()
    pcm = read_wav(path)
    async with connect(url, additional_headers={"Authorization": f"Bearer {token}"}) as socket:
        print(json.dumps(json.loads(await socket.recv()), ensure_ascii=False))
        await socket.send(
            json.dumps(
                {
                    "type": "session.start",
                    "request_id": "example-start",
                    "profile": "utterance",
                    "audio": {"sample_rate": 16_000, "channels": 1, "format": "pcm_s16le"},
                    "language": "Chinese",
                    "durable": False,
                    "transcript_mode": transcript_mode,
                }
            )
        )
        print(json.dumps(json.loads(await socket.recv()), ensure_ascii=False))

        async def receive() -> None:
            async for message in socket:
                event = json.loads(message)
                print(json.dumps(event, ensure_ascii=False))
                if event["type"] == "session.stopped":
                    return

        receiver = asyncio.create_task(receive())
        frame_bytes = 3200  # 100 ms at 16 kHz mono PCM16; the wire caps a frame at 6,400
        seq = 0
        start_sample = 0
        for offset in range(0, len(pcm), frame_bytes):
            chunk = pcm[offset : offset + frame_bytes]
            await socket.send(struct.pack("<QQ", seq, start_sample) + chunk)
            seq += 1
            start_sample += len(chunk) // 2
            if realtime:
                await asyncio.sleep(len(chunk) / 2 / 16_000)
        await socket.send(
            json.dumps(
                {"type": "session.stop", "request_id": "example-stop", "through_seq": seq - 1}
            )
        )
        await receiver


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wav", type=Path)
    parser.add_argument("--url", default="ws://127.0.0.1:8765/v1/stream")
    parser.add_argument("--no-realtime", action="store_true")
    parser.add_argument(
        "--revisable",
        action="store_true",
        help="要求串流預覽；只有在服務啟用實驗性 P2a 預覽時才會被接受",
    )
    args = parser.parse_args()
    mode = "revisable" if args.revisable else "final_only"
    asyncio.run(run(args.wav, args.url, not args.no_realtime, mode))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
