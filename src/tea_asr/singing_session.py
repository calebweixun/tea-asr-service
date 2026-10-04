"""Per-stream glue: PCM in, YAMNet frames out, never blocking the event loop.

The model runs in a shared single-thread executor (onnxruntime releases the
GIL). Each stream session keeps its own audio backlog and `SingingTracker`.
One executor call is in flight per session; audio that arrives meanwhile just
queues and is scored in the next call, so a slow call delays labels but never
blocks audio intake (docs/06 constraint 3). The backlog is bounded: if the
executor falls more than `MAX_BACKLOG_SAMPLES` behind, the session stops
labelling and says so, instead of fabricating or silently skipping frames.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np

from .singing import (
    DEFAULT_PARAMS,
    DEFAULT_SCORER,
    ClassGroups,
    FrameScorer,
    SingingParams,
    SingingTracker,
    frame_features,
)
from .yamnet import HOP_SAMPLES, PATCH_SAMPLES, YamnetModel, complete_frames, pcm16_to_float

MAX_BACKLOG_SAMPLES = 30 * 16_000
MAX_FRAMES_PER_CALL = 16


@dataclass(slots=True)
class SingingRuntime:
    """Process-wide, shared by every stream session."""

    model: YamnetModel
    groups: ClassGroups
    scorer: FrameScorer = DEFAULT_SCORER
    params: SingingParams = DEFAULT_PARAMS
    executor: ThreadPoolExecutor | None = None

    def __post_init__(self) -> None:
        if self.executor is None:
            self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="yamnet")

    def close(self) -> None:
        if self.executor is not None:
            self.executor.shutdown(wait=False, cancel_futures=True)


class SessionSinging:
    """Frame producer and tracker owner for one stream session."""

    def __init__(
        self,
        runtime: SingingRuntime,
        *,
        on_frames: Callable[[], None],
        on_failure: Callable[[str], None],
    ) -> None:
        self._runtime = runtime
        self._on_frames = on_frames
        self._on_failure = on_failure
        self.tracker = SingingTracker(runtime.scorer, runtime.params)
        self._buffer = bytearray()
        self._buffer_start = 0
        self._end_sample = 0
        self._next_frame = 0
        self._task: asyncio.Task[None] | None = None
        self.failed: str | None = None

    def push(self, start_sample: int, pcm: bytes) -> None:
        """Add the next contiguous PCM; cheap, never runs the model inline."""

        if self.failed is not None:
            return
        if start_sample != self._end_sample:
            self._fail("sample_gap")
            return
        self._buffer.extend(pcm)
        self._end_sample += len(pcm) // 2
        if (self._end_sample - self._buffer_start) > MAX_BACKLOG_SAMPLES:
            self._fail("backlog_overflow")
            return
        self._dispatch()

    def _pending_frames(self) -> int:
        return complete_frames(self._end_sample) - self._next_frame

    def _dispatch(self) -> None:
        if self.failed is not None or (self._task is not None and not self._task.done()):
            return
        pending = self._pending_frames()
        if pending <= 0:
            return
        count = min(pending, MAX_FRAMES_PER_CALL)
        first = self._next_frame
        begin = first * HOP_SAMPLES - self._buffer_start
        length = PATCH_SAMPLES + HOP_SAMPLES * (count - 1)
        waveform = pcm16_to_float(self._buffer[begin * 2 : (begin + length) * 2])
        self._next_frame += count
        new_start = self._next_frame * HOP_SAMPLES
        del self._buffer[: (new_start - self._buffer_start) * 2]
        self._buffer_start = new_start
        self._task = asyncio.get_running_loop().create_task(self._compute(waveform, count))

    async def _compute(self, waveform: np.ndarray, count: int) -> None:
        loop = asyncio.get_running_loop()
        try:
            scores = await loop.run_in_executor(
                self._runtime.executor, self._runtime.model.scores, waveform
            )
            if scores.shape[0] != count:
                raise RuntimeError(f"expected {count} frames, got {scores.shape[0]}")
            self.tracker.observe(frame_features(scores, self._runtime.groups))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - labelling is best effort
            self._task = None
            self._fail(type(exc).__name__)
            return
        # Free the slot first, or `_dispatch` would see this very task as busy.
        self._task = None
        self._on_frames()
        self._dispatch()

    def _fail(self, reason: str) -> None:
        if self.failed is not None:
            return
        self.failed = reason
        self._buffer.clear()
        self._on_failure(reason)

    async def settle(self) -> None:
        """Wait until every complete frame received so far has been scored."""

        while self.failed is None:
            task = self._task
            if task is not None and not task.done():
                await asyncio.gather(task, return_exceptions=True)
                continue
            if self._pending_frames() > 0:
                self._dispatch()
                continue
            return

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
