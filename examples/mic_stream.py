"""對著麥克風說話，即時看到辨識結果。

需要 ffmpeg（`brew install ffmpeg`）。ffmpeg 負責擷取與重採樣到
16 kHz mono PCM16，本腳本只做 WebSocket 協定的部分。

    uv run python examples/mic_stream.py --list-devices
    uv run python examples/mic_stream.py --device 2

預設是 continuous：server 用 VAD 自己斷句，你講一句停一下就會出一段，
不用按任何鍵。按 Enter 結束整個 session。

    uv run python examples/mic_stream.py --device 2 --utterance

改用 utterance profile 時，要自己按 Enter 標記「這段講完了」。

服務若啟用了實驗性的 P2a 預覽，加 --revisable 就能看到邊說邊修訂：

    TEA_ASR_EXPERIMENTAL_REVISABLE_PREVIEW=1 uv run tea-asr serve
    uv run python examples/mic_stream.py --device 2 --revisable
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import signal
import struct
import subprocess
import sys
import threading

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


def start_ffmpeg(device: str, *, show_errors: bool = False) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-f", "avfoundation",
            "-i", f":{device}",
            "-ac", "1",
            "-ar", "16000",
            "-f", "s16le",
            "-",
        ],
        stdout=subprocess.PIPE,
        # Terminating ffmpeg mid-write makes it complain about the broken pipe,
        # which is expected on shutdown and only confuses the reader.
        stderr=None if show_errors else subprocess.DEVNULL,
    )


def pump_audio(
    ffmpeg: subprocess.Popen[bytes],
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue[bytes | None],
) -> None:
    """Read the capture pipe on a daemon thread.

    A blocking read on the default executor keeps the interpreter alive at exit,
    which is why Ctrl-C used to need a second press.
    """

    def reader() -> None:
        stream = ffmpeg.stdout
        assert stream is not None
        try:
            while True:
                chunk = stream.read(FRAME_BYTES)
                loop.call_soon_threadsafe(queue.put_nowait, chunk or None)
                if not chunk:
                    return
        except (ValueError, OSError):
            loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=reader, name="mic-reader", daemon=True).start()


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


async def run(
    device: str,
    url: str,
    profile: str,
    transcript_mode: str,
    *,
    show_ffmpeg_errors: bool = False,
) -> int:
    token_file = AppPaths.macos_default().token_file
    if not token_file.exists():
        print(f"找不到 token，請先啟動服務：{token_file}", file=sys.stderr)
        return 2
    token = token_file.read_text().strip()

    ffmpeg = start_ffmpeg(device, show_errors=show_ffmpeg_errors)
    assert ffmpeg.stdout is not None
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    audio: asyncio.Queue[bytes | None] = asyncio.Queue()
    pump_audio(ffmpeg, loop, audio)

    # Ctrl-C ends the session the same way Enter does, so the last segment
    # still gets a final instead of being dropped on the floor.
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stopping.set)

    async with connect(url, additional_headers={"Authorization": f"Bearer {token}"}) as socket:
        hello = json.loads(await socket.recv())
        await socket.send(
            json.dumps(
                {
                    "type": "session.start",
                    "request_id": "mic-start",
                    "profile": profile,
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
        how = (
            "server 會用 VAD 自動斷句"
            if profile == "continuous"
            else "按 Enter 送出這一段"
        )
        print(
            f"協定 {hello['protocol_version']}、profile {started['profile']}、"
            f"模式 {started['transcript_mode']}。{how}；按 Enter 結束。\n"
        )

        async def receive() -> None:
            async for message in socket:
                event = json.loads(message)
                render(event)
                if event["type"] == "session.stopped":
                    return

        def watch_stdin() -> None:
            # EOF (piped or /dev/null stdin) is not a request to stop; only a
            # real line is, otherwise the session would end before it began.
            if sys.stdin.readline():
                loop.call_soon_threadsafe(stopping.set)

        threading.Thread(target=watch_stdin, name="enter-watch", daemon=True).start()

        receiver = asyncio.create_task(receive())
        stop_wait = asyncio.create_task(stopping.wait())

        seq = 0
        sample = 0
        sent_bytes = 0
        try:
            while True:
                nxt = asyncio.create_task(audio.get())
                done, _ = await asyncio.wait(
                    {nxt, stop_wait}, return_when=asyncio.FIRST_COMPLETED
                )
                if nxt not in done:
                    nxt.cancel()
                    break
                chunk = nxt.result()
                if not chunk:
                    break
                if profile != "continuous" and sent_bytes + len(chunk) > MAX_UTTERANCE_BYTES:
                    print("\n達到 30 秒單段上限，先定稿。", file=sys.stderr)
                    break
                await socket.send(struct.pack("<QQ", seq, sample) + chunk)
                seq += 1
                sample += len(chunk) // 2
                sent_bytes += len(chunk)
        finally:
            stop_wait.cancel()
            ffmpeg.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                ffmpeg.wait(timeout=2)
            print()

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
    parser.add_argument(
        "--utterance",
        action="store_true",
        help="改用 utterance profile，由你自己標記段落結束",
    )
    parser.add_argument(
        "--debug-ffmpeg", action="store_true", help="顯示 ffmpeg 的 stderr"
    )
    args = parser.parse_args()
    if args.list_devices:
        return list_devices()
    mode = "revisable" if args.revisable else "final_only"
    profile = "utterance" if args.utterance else "continuous"
    try:
        return asyncio.run(
            run(
                args.device,
                args.url,
                profile,
                mode,
                show_ffmpeg_errors=args.debug_ffmpeg,
            )
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
