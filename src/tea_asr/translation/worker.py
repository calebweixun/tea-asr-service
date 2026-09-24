"""Translation worker subprocess: one T3PO model, one active session.

A separate process from the ASR worker (docs/06 #2: the translation provider
never shares or replaces the ASR model). stdout carries only length-prefixed
JSON frames; any library print goes to stderr.

    python -m tea_asr.translation.worker --model-path /Volumes/.../t3po-mlx-4bit
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import time
from pathlib import Path
from typing import Any, BinaryIO

from tea_asr.translation.simt import LATENCY_MODES, VERIFIED_DIRECTIONS, SimtEngine
from tea_asr.worker.protocol import encode_message, read_exact

MAX_REQUEST_BYTES = 64 * 1024
#: Longest source increment accepted in one request (one ASR final is far shorter).
MAX_TEXT_CHARS = 2000
#: Freed Metal buffers MLX may keep around for reuse (docs/06 #4).
CACHE_LIMIT_BYTES = 512 * 1024 * 1024

_IPC_OUT = sys.stdout.buffer
_IPC_IN = sys.stdin.buffer


def _write(payload: dict[str, Any]) -> None:
    _IPC_OUT.write(encode_message(payload))
    _IPC_OUT.flush()


def _read(stream: BinaryIO) -> dict[str, Any]:
    size = struct.unpack(">I", read_exact(stream, 4))[0]
    if size > MAX_REQUEST_BYTES:
        raise ValueError("IPC request is too large")
    payload = json.loads(read_exact(stream, size))
    if not isinstance(payload, dict):
        raise TypeError("IPC request must be an object")
    return payload


def missing_model_reason(model_path: Path) -> str | None:
    """Explain why `model_path` cannot be loaded, or None when it looks usable."""

    if not model_path.exists():
        return (
            f"翻譯模型路徑不存在：{model_path}。若模型放在外接 SSD，請確認磁碟已連接並掛載。"
        )
    if not (model_path / "config.json").is_file():
        return f"翻譯模型資料夾缺少 config.json：{model_path}"
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--max-memory-gib", type=float, required=True)
    args = parser.parse_args()
    sys.stdout = sys.stderr

    reason = missing_model_reason(args.model_path)
    if reason is not None:
        _write({"status": "error", "code": "translation_unavailable", "message": reason})
        return 1
    try:
        import mlx.core as mx

        from tea_asr.translation.generator import MlxT3poGenerator, PromptTooLongError

        mx.set_cache_limit(CACHE_LIMIT_BYTES)
        started = time.perf_counter()
        generator = MlxT3poGenerator(args.model_path)
        load_ms = round((time.perf_counter() - started) * 1000)
        active = mx.get_active_memory()
        if active > args.max_memory_gib * 2**30:
            _write(
                {
                    "status": "error",
                    "code": "translation_unavailable",
                    "message": (
                        f"翻譯模型載入後佔用 {active / 2**30:.2f} GiB，超過上限 "
                        f"{args.max_memory_gib:g} GiB。"
                    ),
                }
            )
            return 1
    except Exception as exc:  # noqa: BLE001 - load failures cross the process boundary
        _write({"status": "error", "code": "translation_unavailable", "message": str(exc)})
        return 1
    _write({"status": "ready", "load_ms": load_ms, "active_memory_bytes": active})

    engine: SimtEngine | None = None
    while True:
        try:
            request = _read(_IPC_IN)
        except EOFError:
            break
        except Exception as exc:  # noqa: BLE001 - malformed IPC ends the worker
            _write({"status": "error", "code": "translation_failed", "message": str(exc)})
            break
        request_id = str(request.get("request_id", ""))
        op = request.get("op")
        try:
            if op == "start":
                direction = str(request["direction"])
                mode = LATENCY_MODES[str(request["latency_mode"])]
                if direction not in VERIFIED_DIRECTIONS:
                    raise ValueError(f"unsupported direction {direction}")
                generator.reset()
                engine = SimtEngine(generator, direction, mode)  # type: ignore[arg-type]
                _write({"status": "ok", "request_id": request_id})
                continue
            if op != "translate" or engine is None:
                raise ValueError(f"unexpected op {op!r}")
            text = str(request.get("text", ""))
            if len(text) > MAX_TEXT_CHARS:
                raise ValueError(f"text longer than {MAX_TEXT_CHARS} characters")
            force = bool(request.get("force", True))
            try:
                decision = engine.translate(text, force=force)
            except PromptTooLongError:
                # The bound in docs/06 #4 was hit: keep the source, drop context.
                engine.drop_history()
                generator.reset()
                decision = engine.translate(text, force=force)
            stats = generator.last
            _write(
                {
                    "status": "ok",
                    "request_id": request_id,
                    "action": decision.action,
                    "source": decision.source,
                    "text": decision.text,
                    "forced": decision.forced,
                    "prompt_tokens": stats.prompt_tokens if stats else 0,
                    "reused_tokens": stats.reused_tokens if stats else 0,
                    "generated_tokens": stats.generated_tokens if stats else 0,
                    "inference_ms": round(stats.total_s * 1000) if stats else 0,
                    "peak_memory_bytes": int(mx.get_peak_memory()),
                }
            )
        except Exception as exc:  # noqa: BLE001 - one failed call must not kill the worker
            _write(
                {
                    "status": "error",
                    "request_id": request_id,
                    "code": "translation_failed",
                    "message": str(exc),
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
