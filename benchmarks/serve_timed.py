"""Run `tea-asr serve` with every ASR worker call timed (benchmark use only).

Wraps three methods without changing what they do:

- `StreamSession._run_preview` / `_transcribe_segment`: which session a call is for
- `Scheduler.transcribe`: the call's kind (`preview`, `realtime`, `interactive`)
- `WorkerSupervisor.transcribe`: wall time of the actual worker call (IPC included)

It works unchanged against older checkouts (e.g. `git archive c3829c9 src`), so
the baseline and a new build are timed the same way. Output is JSONL at
`$TEA_TIMING_LOG`, one row per worker call on the server's `time.monotonic()`:
`{"kind", "session", "t0", "t1", "samples"}`. `preview_cadence_eval.py` turns it
into busy fractions and decode-time distributions.

    HOME=<isolated home> HF_HUB_OFFLINE=1 TEA_ASR_MODELS_DIR=<models> \\
    PYTHONPATH=<checkout>/src TEA_TIMING_LOG=/tmp/run.timing.jsonl \\
        python benchmarks/serve_timed.py serve --port 8399
"""

from __future__ import annotations

import contextvars
import json
import os
import sys
import time
from typing import Any

import tea_asr
from tea_asr import scheduler as scheduler_module
from tea_asr.api import stream as stream_module
from tea_asr.cli import main
from tea_asr.worker import supervisor as supervisor_module

print(f"tea_asr from {tea_asr.__file__}", file=sys.stderr, flush=True)

LOG = open(os.environ["TEA_TIMING_LOG"], "a", buffering=1)  # noqa: SIM115 - lives as long as the server
KIND: contextvars.ContextVar[str] = contextvars.ContextVar("kind", default="?")
SESSION: contextvars.ContextVar[str] = contextvars.ContextVar("session", default="?")

_orig_preview = stream_module.StreamSession._run_preview
_orig_segment = stream_module.StreamSession._transcribe_segment
_orig_schedule = scheduler_module.Scheduler.transcribe
_orig_worker = supervisor_module.WorkerSupervisor.transcribe


async def _run_preview(self: Any, *args: Any, **kwargs: Any) -> Any:
    SESSION.set(self._state.session_id)
    return await _orig_preview(self, *args, **kwargs)


async def _transcribe_segment(self: Any, *args: Any, **kwargs: Any) -> Any:
    SESSION.set(self._state.session_id)
    return await _orig_segment(self, *args, **kwargs)


async def _schedule(self: Any, pcm: bytes, *args: Any, **kwargs: Any) -> Any:
    KIND.set(kwargs.get("kind", "interactive"))
    return await _orig_schedule(self, pcm, *args, **kwargs)


async def _worker(self: Any, pcm: bytes, *args: Any, **kwargs: Any) -> Any:
    t0 = time.monotonic()
    try:
        return await _orig_worker(self, pcm, *args, **kwargs)
    finally:
        row = {
            "kind": KIND.get(),
            "session": SESSION.get(),
            "t0": t0,
            "t1": time.monotonic(),
            "samples": len(pcm) // 2,
        }
        LOG.write(json.dumps(row) + "\n")


stream_module.StreamSession._run_preview = _run_preview  # type: ignore[method-assign]
stream_module.StreamSession._transcribe_segment = _transcribe_segment  # type: ignore[method-assign]
scheduler_module.Scheduler.transcribe = _schedule  # type: ignore[method-assign]
supervisor_module.WorkerSupervisor.transcribe = _worker  # type: ignore[method-assign]

if __name__ == "__main__":
    sys.argv = ["tea-asr", *sys.argv[1:]]
    raise SystemExit(main())
