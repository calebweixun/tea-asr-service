from __future__ import annotations

import argparse
import sys
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np

from tea_asr.backend import TeaMlxBackend
from tea_asr.worker.protocol import encode_message, read_request

#: stdout belongs to the IPC protocol (docs/03). The real handle is taken once
#: and `sys.stdout` is pointed at stderr, so any third-party print or progress
#: bar lands in the log instead of corrupting a framed message.
_IPC_OUT = sys.stdout.buffer
_IPC_IN = sys.stdin.buffer


def _claim_stdout() -> None:
    sys.stdout = sys.stderr


def _write(payload: dict) -> None:
    _IPC_OUT.write(encode_message(payload))
    _IPC_OUT.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", type=Path, required=True)
    args = parser.parse_args()
    _claim_stdout()
    backend = TeaMlxBackend(args.model_path)
    try:
        started = time.perf_counter()
        backend.load()
        _write({"status": "ready", "load_ms": round((time.perf_counter() - started) * 1000)})
    except Exception as exc:  # noqa: BLE001 - process boundary must serialize failures
        _write({"status": "error", "code": "model_incompatible", "message": str(exc)})
        return 1

    while True:
        try:
            header, pcm = read_request(_IPC_IN)
        except EOFError:
            break
        except Exception as exc:  # noqa: BLE001 - malformed IPC must not escape the worker
            _write({"status": "error", "code": "invalid_ipc", "message": str(exc)})
            break
        request_id = str(header.get("request_id", ""))
        try:
            audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
            result = backend.transcribe(audio, language=str(header.get("language", "Chinese")))
            payload = asdict(result)
            payload.update({"status": "ok", "request_id": request_id})
            _write(payload)
        except Exception as exc:  # noqa: BLE001 - inference failures cross a process boundary
            _write(
                {
                    "status": "error",
                    "request_id": request_id,
                    "code": "inference_failed",
                    "message": str(exc),
                }
            )
    backend.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
