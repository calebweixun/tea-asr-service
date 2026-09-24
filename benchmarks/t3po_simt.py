"""Confucius4-T3PO 同步翻譯 spike：MLX 上的 READ/WRITE 迴圈（不改 production）。

協定照官方 repo（netease-youdao/Confucius4-T3PO，commit 4827f02b7b344d9ab5af95ad484fd64b6682cb85，
Apache-2.0）的 `inference/prompts.py`、`inference/translation.py`、`inference/latency.py` 移植：

- user message = 任務提示 + `<STREAMING_HISTORY>`（`src¦tgt§` 串接）+ `<CURRENT_INPUT>`。
- 模型輸出空字串／只有 EOS = WAIT（READ）；非空 = TRANS（WRITE），整個 buffer 與譯文 append 進 history。
- 三個檔位是對兩個停止 token（151643、151645）加 logit bias `-tau*scale`，
  非 native 再加 vLLM 語意的 repetition penalty 1.05（先 bias 後 penalty，對 prompt∪輸出 token）。
- force（buffer 到門檻或 flush）時第一個 token 禁止停止 token，等價於 vLLM `min_tokens=1`。

官方實作走 vLLM prefix cache 或 transformers 每次重算；這裡不引入 vLLM／PyTorch，
改用 mlx-lm 的 `KVCache` 自己做「最長共同 token 前綴」重用：保留上一步已算過的 KV，
trim 到與新 prompt 的共同前綴，只 prefill 剩下的 token。`reuse_kv=False` 每步清空，
用來實證重用的效果與正確性（同一份輸入，兩種模式的 greedy 輸出應一致）。

只依賴 mlx／mlx-lm（mlx-audio 0.4.5 的傳遞依賴，uv.lock 鎖 0.31.3），不改 pyproject.toml。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np

Direction = Literal["zh2en", "en2zh"]

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

STOP_TOKEN_IDS: tuple[int, ...] = (151643, 151645)
STOP_TOKEN_BIAS_SCALES: dict[int, float] = {151643: 1.0, 151645: 1.05}
REPETITION_PENALTY = 1.05


@dataclass(frozen=True)
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
    return str(text or "").replace("¦", "｜").replace("§", "；").strip()


_WAIT_ONLY = re.compile(r"^<?\s*WAIT\s*>?$", re.IGNORECASE)
_TRANS_ONLY = re.compile(r"^<?\s*TRANS\s*>?$", re.IGNORECASE)
_TRANS_PREFIX = re.compile(r"^(?:<\s*TRANS\s*>\s*|TRANS(?:\s*[:：]\s*|\s+))", re.IGNORECASE)


def parse_model_response(raw: object) -> tuple[str, str]:
    text = str(raw or "").replace("<|im_end|>", "").strip()
    if not text or _WAIT_ONLY.fullmatch(text) or _TRANS_ONLY.fullmatch(text):
        return "WAIT", ""
    text = sanitize_history(_TRANS_PREFIX.sub("", text, count=1))
    return ("TRANS", text) if text else ("WAIT", "")


# ---------------------------------------------------------------- generation


@dataclass
class CallStats:
    prompt_tokens: int
    reused_tokens: int
    prefill_tokens: int
    generated_tokens: int
    prefill_s: float
    first_token_s: float
    total_s: float
    force: bool
    mode: str


class MlxT3poGenerator:
    """Greedy decoder with longest-common-prefix KV reuse on one mlx-lm model."""

    def __init__(
        self,
        model_path: Path,
        *,
        reuse_kv: bool = True,
        max_new_tokens: int = 128,
        prefill_step: int = 512,
    ) -> None:
        import mlx.core as mx
        from mlx_lm import load
        from mlx_lm.models.cache import make_prompt_cache

        self._mx = mx
        self._make_cache = make_prompt_cache
        started = time.perf_counter()
        self.model, self.tokenizer = load(str(model_path))
        mx.eval(self.model.parameters())
        self.load_s = time.perf_counter() - started
        self.reuse_kv = reuse_kv
        self.max_new_tokens = max_new_tokens
        self.prefill_step = prefill_step
        self._cache = make_prompt_cache(self.model)
        self._cached: list[int] = []
        self.calls: list[CallStats] = []
        stop = np.zeros(self.model.args.vocab_size, dtype=bool)
        stop[list(STOP_TOKEN_IDS)] = True
        self._stop_mask = mx.array(stop)
        self._bias: dict[str, object] = {}

    def prompt_tokens(self, user_message: str) -> list[int]:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]
        text = self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        return list(self.tokenizer.encode(text, add_special_tokens=False))

    def reset(self) -> None:
        self._cache = self._make_cache(self.model)
        self._cached = []

    def _prefill(self, tokens: list[int]):
        mx = self._mx
        from mlx_lm.models.cache import trim_prompt_cache

        if not self.reuse_kv:
            self.reset()
        common = 0
        limit = min(len(self._cached), len(tokens) - 1)
        while common < limit and self._cached[common] == tokens[common]:
            common += 1
        drop = len(self._cached) - common
        if drop:
            trim_prompt_cache(self._cache, drop)
        self._cached = self._cached[:common]
        rest = tokens[common:]
        logits = None
        for start in range(0, len(rest), self.prefill_step):
            chunk = mx.array(rest[start : start + self.prefill_step])[None]
            logits = self.model(chunk, cache=self._cache)
            mx.eval([c.state for c in self._cache])
        self._cached.extend(rest)
        return logits[:, -1, :], common, len(rest)

    def _pick(self, logits, mode: LatencyMode, seen, *, block_stop: bool) -> int:
        mx = self._mx
        logits = logits.astype(mx.float32)
        if mode.tau != 0.0:
            bias = self._bias.get(mode.name)
            if bias is None:
                values = np.zeros(logits.shape[-1], dtype=np.float32)
                for token_id in STOP_TOKEN_IDS:
                    values[token_id] = -mode.tau * STOP_TOKEN_BIAS_SCALES[token_id]
                bias = self._bias[mode.name] = mx.array(values)
            logits = logits + bias
            penalized = mx.where(logits > 0, logits / REPETITION_PENALTY, logits * REPETITION_PENALTY)
            logits = mx.where(seen, penalized, logits)
        if block_stop:
            logits = mx.where(self._stop_mask, -mx.inf, logits)
        return int(mx.argmax(logits, axis=-1).item())

    def complete(self, user_message: str, *, force: bool, mode: LatencyMode) -> tuple[str, CallStats]:
        mx = self._mx
        started = time.perf_counter()
        tokens = self.prompt_tokens(user_message)
        logits, reused, prefilled = self._prefill(tokens)
        seen = None
        if mode.tau != 0.0:
            mask = np.zeros(logits.shape[-1], dtype=bool)
            mask[tokens] = True
            seen = mx.array(mask)
        mx.eval(logits)
        prefill_s = time.perf_counter() - started
        generated: list[int] = []
        first_token_s = 0.0
        while True:
            token = self._pick(logits, mode, seen, block_stop=force and not generated)
            if not generated:
                first_token_s = time.perf_counter() - started
            if token in STOP_TOKEN_IDS:
                break
            generated.append(token)
            if len(generated) >= self.max_new_tokens:
                break
            if seen is not None:
                seen = mx.where(mx.arange(seen.shape[0]) == token, True, seen)
            logits = self.model(mx.array([[token]]), cache=self._cache)[:, -1, :]
            self._cached.append(token)
        text = self.tokenizer.decode(generated, skip_special_tokens=True).strip()
        stats = CallStats(
            prompt_tokens=len(tokens),
            reused_tokens=reused,
            prefill_tokens=prefilled,
            generated_tokens=len(generated),
            prefill_s=prefill_s,
            first_token_s=first_token_s,
            total_s=time.perf_counter() - started,
            force=force,
            mode=mode.name,
        )
        self.calls.append(stats)
        return text, stats


# ---------------------------------------------------------------- policy engine

PUNCTUATION_END = frozenset({"。", "！", "？", "!", "?", "；", ";", "…", "～", "~"})
LATIN_TOKEN = re.compile(r"^[A-Za-z0-9]+(?:[._'’-][A-Za-z0-9]+)*$")


def split_source(text: str, direction: Direction) -> list[str]:
    if direction == "en2zh":
        return str(text or "").strip().split()
    tokens: list[str] = []
    value = str(text or "")
    index = 0
    while index < len(value):
        char = value[index]
        if char.isspace():
            end = index + 1
            while end < len(value) and value[end].isspace():
                end += 1
            tokens.append(value[index:end])
            index = end
            continue
        if char.isascii() and (char.isalnum() or char in "_'’"):
            end = index + 1
            while end < len(value) and value[end].isascii() and (
                value[end].isalnum() or value[end] in "_’'-"
            ):
                end += 1
            tokens.append(value[index:end])
            index = end
            continue
        tokens.append(char)
        index += 1
    return tokens


def join_source(tokens: list[str], direction: Direction) -> str:
    return "".join(tokens) if direction == "zh2en" else " ".join(tokens)


def source_units(tokens: list[str], direction: Direction) -> int:
    if direction == "zh2en":
        return sum(not token.isspace() for token in tokens)
    return len(tokens)


def join_translation_segments(parts: list[str], direction: Direction) -> str:
    values = [p.strip() for p in parts if p and p.strip()]
    if direction == "en2zh":
        return "".join(values)
    text = " ".join(values)
    text = re.sub(r"\s+([,.;:!?%])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Decision:
    action: Literal["WAIT", "TRANS", "SKIP"]
    source: str
    text: str
    forced: bool
    stats: CallStats | None


@dataclass
class SimtEngine:
    """官方 TranslationEngine 的同步版（預設值相同：20／200／history 30／不做標點強制）。"""

    generator: MlxT3poGenerator
    direction: Direction
    mode: LatencyMode
    force_break_threshold: int = 20
    max_buffer_units: int = 200
    history_window: int = 30
    #: 超過 history_window 時一次砍到剩幾對。官方做法是每次提交都滑動 1 對（=history_window），
    #: 但那會讓 history 開頭每步都變、KV 前綴重用失效；設小一點改成「偶爾整批砍」。
    history_keep: int | None = None
    buffer: list[str] = field(default_factory=list)
    history: list[tuple[str, str]] = field(default_factory=list)
    committed: list[str] = field(default_factory=list)

    def _history_text(self) -> str:
        pairs = self.history[-self.history_window :] if self.history_window else []
        return "".join(f"{src}¦{tgt}§" for src, tgt in pairs)

    def _translate(self, *, force: bool) -> Decision:
        source = join_source(self.buffer, self.direction)
        if not source.strip():
            return Decision("SKIP", "", "", force, None)
        raw, stats = self.generator.complete(
            build_user_message(self.direction, self._history_text(), source),
            force=force,
            mode=self.mode,
        )
        _, target = parse_model_response(raw)
        if not target:
            return Decision("WAIT", source, "", force, stats)
        self.history.append((sanitize_history(source), target))
        if len(self.history) > self.history_window:
            keep = self.history_window if self.history_keep is None else self.history_keep
            self.history = self.history[-keep:] if keep else []
        self.committed.append(target)
        self.buffer.clear()
        return Decision("TRANS", source, target, force, stats)

    def feed(self, text: str) -> Decision:
        incoming = [t for t in split_source(text, self.direction) if t]
        if not incoming:
            return Decision("SKIP", "", "", False, None)
        if (
            self.direction == "zh2en"
            and self.buffer
            and LATIN_TOKEN.match(self.buffer[-1] or "")
            and LATIN_TOKEN.match(incoming[0] or "")
        ):
            self.buffer[-1] += incoming.pop(0)
        self.buffer.extend(incoming)
        units = source_units(self.buffer, self.direction)
        if not units:
            return Decision("SKIP", "", "", False, None)
        if units >= self.max_buffer_units:
            return self._translate(force=True)
        if self.direction == "zh2en" and self.buffer and LATIN_TOKEN.match(self.buffer[-1] or ""):
            return Decision("SKIP", join_source(self.buffer, self.direction), "", False, None)
        if units >= self.force_break_threshold:
            return self._translate(force=True)
        return self._translate(force=False)

    def flush(self) -> Decision:
        if not self.buffer:
            return Decision("SKIP", "", "", True, None)
        return self._translate(force=True)

    @property
    def translation(self) -> str:
        return join_translation_segments(self.committed, self.direction)
