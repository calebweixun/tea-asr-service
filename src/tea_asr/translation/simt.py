"""Confucius4-T3PO streaming protocol and READ/WRITE policy, with no model code.

The prompt strings and response parsing are ported unchanged from the official
inference code (netease-youdao/Confucius4-T3PO, commit
4827f02b7b344d9ab5af95ad484fd64b6682cb85, Apache-2.0: `inference/prompts.py`,
`inference/translation.py`, `inference/latency.py`). They are part of the model
interface: rewording them changes the model's behaviour, so they live in one
place and tests pin them.

This module is import-safe without MLX. The generator that actually runs the
model lives in `tea_asr.translation.generator` and only ever runs inside the
translation worker subprocess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, Protocol

Direction = Literal["zh2en", "en2zh"]

#: Directions this service has actually exercised on the model (docs/06 #6).
#: en2zh runs, but its output is Simplified Chinese with mainland wording
#: (docs/benchmarks/t3po-eval-report.md), so it is not offered.
VERIFIED_DIRECTIONS: tuple[Direction, ...] = ("zh2en",)

SYSTEM_PROMPT = "You are a helpful assistant."

_PROMPT_TEMPLATE = """### Role
You are a professional {role} simultaneous interpreter for live streaming and ASR speech translation, with strict requirements for low latency, high coherence, and natural fluency.

### Context Format
- The conversation history is provided in <STREAMING_HISTORY>, structured as:
  source_text¦translated_text§source_text¦translated_text§...
- The last segment of <STREAMING_HISTORY> is the latest input awaiting translation.

### Input
- The latest chunk from a live ASR speech stream.
- ASR artifacts (fillers, stutters, repetitions) should be ignored.

### Rules
- Output nothing if the available context is still ambiguous.
- Otherwise, output the translation of what has become sufficiently clear.
  Do not assume linear or word-by-word correspondence — reorder and restructure
  as needed for a natural output.
- The new translation must read smoothly as a continuation of the preceding
  translated text.
- Output the translation directly, with no prefix, suffix, or extra markers."""

STREAMING_PROMPTS: dict[str, str] = {
    "zh2en": _PROMPT_TEMPLATE.format(role="Chinese-to-English"),
    "en2zh": _PROMPT_TEMPLATE.format(role="English-to-Chinese"),
}

#: Qwen pad/eos ids; either one ends a completion, and an immediate stop is WAIT.
STOP_TOKEN_IDS: tuple[int, ...] = (151643, 151645)
#: Published calibration for the latency modes (official `latency.py`).
STOP_TOKEN_BIAS_SCALES: dict[int, float] = {151643: 1.0, 151645: 1.05}
REPETITION_PENALTY = 1.05


@dataclass(frozen=True, slots=True)
class LatencyMode:
    name: str
    tau: float


LATENCY_MODES: dict[str, LatencyMode] = {
    "low": LatencyMode("low", 0.9375009536743164),
    "native": LatencyMode("native", 0.0),
    "high": LatencyMode("high", -0.39),
}


def build_user_message(direction: Direction, history: str, current_input: str) -> str:
    return (
        f"{STREAMING_PROMPTS[direction]}\n\n<STREAMING_HISTORY>\n{history}\n\n"
        f"<CURRENT_INPUT>\n{current_input}"
    )


def sanitize_history(text: object) -> str:
    """Keep generated text from corrupting the `¦`/`§` history framing."""

    return str(text or "").replace("¦", "｜").replace("§", "；").strip()


_WAIT_ONLY = re.compile(r"^<?\s*WAIT\s*>?$", re.IGNORECASE)
_TRANS_ONLY = re.compile(r"^<?\s*TRANS\s*>?$", re.IGNORECASE)
_TRANS_PREFIX = re.compile(r"^(?:<\s*TRANS\s*>\s*|TRANS(?:\s*[:：]\s*|\s+))", re.IGNORECASE)


def parse_model_response(raw: object) -> tuple[Literal["WAIT", "TRANS"], str]:
    text = str(raw or "").replace("<|im_end|>", "").strip()
    if not text or _WAIT_ONLY.fullmatch(text) or _TRANS_ONLY.fullmatch(text):
        return "WAIT", ""
    text = sanitize_history(_TRANS_PREFIX.sub("", text, count=1))
    return ("TRANS", text) if text else ("WAIT", "")


class Completion(Protocol):
    def __call__(self, user_message: str, *, force: bool, mode: LatencyMode) -> str: ...


@dataclass(slots=True)
class Decision:
    action: Literal["WAIT", "TRANS"]
    source: str
    text: str
    forced: bool


@dataclass
class SimtEngine:
    """Committed history plus the uncommitted source buffer for one session.

    `history_window`/`history_keep` bound the prompt (docs/06 #4). The official
    code slides the window by one pair per commit; that changes the start of the
    history on every step and throws away the reusable KV prefix, which measured
    ~2.7 s per call instead of ~0.2 s. Trimming to `history_keep` pairs only when
    the window overflows keeps the prefix stable between trims.
    """

    complete: Completion
    direction: Direction
    mode: LatencyMode
    history_window: int = 30
    history_keep: int = 10
    max_buffer_chars: int = 400
    buffer: str = ""
    history: list[tuple[str, str]] = field(default_factory=list)

    def history_text(self) -> str:
        return "".join(f"{src}¦{tgt}§" for src, tgt in self.history)

    def translate(self, text: str, *, force: bool) -> Decision:
        """Append a source increment and ask the model for a READ/WRITE decision.

        With `force` the model must write (the generator forbids an immediate
        stop), which is how a finished ASR segment is flushed. A committed
        translation is only ever appended to the history, never revised.
        """

        joiner = " " if self.direction == "en2zh" and self.buffer and text.strip() else ""
        # The buffer only changes once the model answered, so a failed call
        # (timeout, prompt bound) can be retried without duplicating text.
        source = f"{self.buffer}{joiner}{text.strip()}"
        if len(source) > self.max_buffer_chars:
            force = True
        if not source.strip():
            return Decision("WAIT", "", "", force)
        raw = self.complete(
            build_user_message(self.direction, self.history_text(), source),
            force=force,
            mode=self.mode,
        )
        action, target = parse_model_response(raw)
        if action == "WAIT":
            self.buffer = source
            return Decision("WAIT", source, "", force)
        self.history.append((sanitize_history(source), target))
        if len(self.history) > self.history_window:
            self.history = self.history[-self.history_keep :] if self.history_keep else []
        self.buffer = ""
        return Decision("TRANS", source, target, force)

    def drop_history(self) -> None:
        self.history.clear()
