"""送一段 16 kHz mono PCM 到 HTTP 短音訊端點。

用法：
    uv run python examples/transcribe_pcm.py path/to/audio.wav
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import wave
from pathlib import Path

from tea_asr.config import AppPaths
from tea_asr.wire import MAX_UTTERANCE_PCM_BYTES

ENDPOINT = "/v1/transcriptions?sample_rate=16000&channels=1&format=pcm_s16le"


def read_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16_000, 1, 2):
            raise ValueError("WAV 必須是 16 kHz mono PCM16")
        return wav.readframes(wav.getnframes())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wav", type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:8327")
    parser.add_argument("--request-id", default="example-1")
    args = parser.parse_args()

    pcm = read_pcm(args.wav)
    if len(pcm) > MAX_UTTERANCE_PCM_BYTES:
        print("這個端點只接受最多 30 秒；長檔案要等 P4 的 batch job。", file=sys.stderr)
        return 2

    token = AppPaths.macos_default().token_file.read_text().strip()
    request = urllib.request.Request(
        args.url + ENDPOINT,
        data=pcm,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream",
            "X-Request-ID": args.request_id,
        },
    )
    try:
        with urllib.request.urlopen(request) as response:  # fixed localhost URL
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read())
    print(json.dumps(body, ensure_ascii=False, indent=2))
    return 1 if "error" in body else 0


if __name__ == "__main__":
    raise SystemExit(main())
