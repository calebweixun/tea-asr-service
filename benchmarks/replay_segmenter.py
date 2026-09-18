"""對已錄下的 WAV 重跑切段（可選擇同時跑辨識），用來調 VAD 參數。

    uv run python benchmarks/replay_segmenter.py take.wav
    uv run python benchmarks/replay_segmenter.py take.wav --transcribe
    uv run python benchmarks/replay_segmenter.py take.wav --pre-roll-ms 400 --end-silence-ms 700

錄音由 `examples/mic_stream.py --save-wav` 產生，內容就是當時送給 server 的
PCM，所以這裡印出的 sample 範圍與當時的事件可以直接對照。
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tea_asr.segmenter import (
    ContinuousSegmenter,
    SegmentClosed,
    SegmenterConfig,
    SpeechStarted,
)
from tea_asr.vad import SileroVad, locate_vad

FRAME_BYTES = 3200


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16_000, 1, 2):
            raise ValueError("WAV 必須是 16 kHz mono PCM16")
        return wav.readframes(wav.getnframes())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wav", type=Path)
    parser.add_argument("--transcribe", action="store_true", help="同時跑真模型辨識")
    parser.add_argument("--pre-roll-ms", type=int, default=None)
    parser.add_argument("--end-silence-ms", type=int, default=None)
    parser.add_argument("--min-speech-ms", type=int, default=None)
    parser.add_argument("--max-segment-ms", type=int, default=None)
    parser.add_argument("--tail-ms", type=int, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    overrides = {
        name: value
        for name, value in (
            ("pre_roll_ms", args.pre_roll_ms),
            ("end_silence_ms", args.end_silence_ms),
            ("min_speech_ms", args.min_speech_ms),
            ("max_segment_ms", args.max_segment_ms),
            ("tail_ms", args.tail_ms),
        )
        if value is not None
    }
    config = replace(SegmenterConfig(), **overrides)
    print(f"config: {config}")

    pcm = read_wav(args.wav)
    segmenter = ContinuousSegmenter(SileroVad(locate_vad()), config)
    events: list[object] = []
    for offset in range(0, len(pcm), FRAME_BYTES):
        events.extend(segmenter.push(pcm[offset : offset + FRAME_BYTES]))
    events.extend(segmenter.flush())

    backend = None
    if args.transcribe:
        from tea_asr.backend import TeaMlxBackend
        from tea_asr.model_manager import locate_prepared_model
        from tea_asr.model_spec import TEA_ASR_1_1_MLX_4BIT

        backend = TeaMlxBackend(locate_prepared_model(TEA_ASR_1_1_MLX_4BIT))
        backend.load()

    rows = []
    index = 0
    for item in events:
        if isinstance(item, SpeechStarted):
            continue
        assert isinstance(item, SegmentClosed)
        text = ""
        if backend is not None:
            audio = np.frombuffer(item.pcm, dtype="<i2").astype(np.float32) / 32768.0
            text = backend.transcribe(audio).text
        start_s = item.start_sample / 16_000
        end_s = item.end_sample / 16_000
        print(
            f"  [{index:2d}] {start_s:6.2f}-{end_s:6.2f}s "
            f"len={end_s - start_s:5.2f}s {item.boundary:12s} {text}"
        )
        rows.append(
            {
                "index": index,
                "start_sample": item.start_sample,
                "end_sample": item.end_sample,
                "boundary": item.boundary,
                "text": text,
            }
        )
        index += 1

    print(f"\n共 {len(rows)} 段")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps({"config": asdict(config), "segments": rows}, ensure_ascii=False, indent=2)
            + "\n"
        )
        print(f"寫入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
