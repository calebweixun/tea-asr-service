"""Stand-in for `tea_asr.worker.entry` that loads no model.

Speaks the real framed IPC (`tea_asr.worker.protocol`) so `WorkerSupervisor`
can be tested for what only a real pipe shows: a response left unread by a
cancelled caller, a response carrying the wrong ID, a restart. Every answer
echoes the request's sample count in `text`, so a test can tell which request
a response belongs to.

Behaviour is chosen with environment variables:

- `FAKE_ASR_DELAY_S` — seconds each "inference" takes (default 0).
- `FAKE_ASR_READ_DELAY_S` — seconds to wait before reading each request, so a
  large request fills the pipe and the supervisor's write blocks (default 0).
- `FAKE_ASR_MISMATCH_ONCE=<path>` — if `<path>` does not exist yet, create it
  and answer the first request with a wrong `request_id`. So only the first
  worker process misbehaves, and the one restarted after it is healthy.
- `FAKE_ASR_LOOP_TO_CAP=1` — return repeated text and a token count at the
  request's `max_tokens`, like a decoder that keeps repeating until capped.
"""

from __future__ import annotations

import os
import sys
import time

from tea_asr.worker.protocol import encode_message, read_request


def _write(payload: dict) -> None:
    sys.stdout.buffer.write(encode_message(payload))
    sys.stdout.buffer.flush()


def main() -> int:
    mismatch_marker = os.environ.get("FAKE_ASR_MISMATCH_ONCE")
    mismatch = bool(mismatch_marker) and not os.path.exists(mismatch_marker)
    if mismatch:
        open(mismatch_marker, "w").close()
    delay = float(os.environ.get("FAKE_ASR_DELAY_S", "0"))
    read_delay = float(os.environ.get("FAKE_ASR_READ_DELAY_S", "0"))
    _write({"status": "ready", "load_ms": 1})
    stdin = sys.stdin.buffer
    answered = 0
    while True:
        if read_delay:
            time.sleep(read_delay)
        try:
            header, pcm = read_request(stdin)
        except EOFError:
            return 0
        if os.environ.get("FAKE_ASR_HANG") == "1":
            while True:
                time.sleep(1)
        if delay:
            time.sleep(delay)
        samples = len(pcm) // 2
        request_id = str(header["request_id"])
        max_tokens = int(header.get("max_tokens", 512))
        if mismatch and answered == 0:
            request_id = "not-" + request_id
        answered += 1
        loop_to_cap = os.environ.get("FAKE_ASR_LOOP_TO_CAP") == "1"
        _write(
            {
                "status": "ok",
                "request_id": request_id,
                "text": "repeat" * max_tokens if loop_to_cap else f"samples={samples}",
                "audio_samples": samples,
                "model_input_samples": max(16_000, samples),
                "total_time_s": delay,
                "prompt_tokens": 10,
                "generation_tokens": max_tokens if loop_to_cap else 3,
            }
        )


if __name__ == "__main__":
    raise SystemExit(main())
