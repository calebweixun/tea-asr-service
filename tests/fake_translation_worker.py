"""Stand-in for `tea_asr.translation.worker` that loads no model.

Speaks the same framed-JSON IPC so `TranslationSupervisor` can be tested for
start-up, timeouts, crashes and restarts. Behaviour is chosen with
`FAKE_T3PO`: `ok` (default), `hang` (never answers translate), `crash` (exits
on the first translate), `refuse` (reports a load failure).
"""

from __future__ import annotations

import os
import sys
import time

from tea_asr.translation.worker import _read, _write, missing_model_reason


def main() -> int:
    mode = os.environ.get("FAKE_T3PO", "ok")
    path = sys.argv[sys.argv.index("--model-path") + 1]
    from pathlib import Path

    reason = missing_model_reason(Path(path))
    if reason is not None:
        _write({"status": "error", "code": "translation_unavailable", "message": reason})
        return 1
    if mode == "refuse":
        _write({"status": "error", "code": "translation_unavailable", "message": "記憶體超過上限"})
        return 1
    _write({"status": "ready", "load_ms": 1, "active_memory_bytes": 0})
    stdin = sys.stdin.buffer
    while True:
        try:
            request = _read(stdin)
        except EOFError:
            return 0
        if request["op"] == "start":
            _write({"status": "ok", "request_id": request["request_id"]})
            continue
        if mode == "hang":
            time.sleep(60)
        if mode == "crash":
            return 3
        _write(
            {
                "status": "ok",
                "request_id": request["request_id"],
                "action": "TRANS",
                "source": request["text"],
                "text": f"EN({request['text']})",
                "forced": True,
                "inference_ms": 1,
            }
        )


if __name__ == "__main__":
    raise SystemExit(main())
