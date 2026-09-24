"""Greedy MLX decoding for Confucius4-T3PO with longest-common-prefix KV reuse.

Runs only inside the translation worker subprocess (`tea_asr.translation.worker`).
mlx-lm is the version uv.lock resolves for mlx-audio (0.31.3); no vLLM,
PyTorch or CUDA code path exists here.

The KV cache holds exactly the tokens already fed to the model. Each call keeps
the part of it that matches the new prompt, trims the rest and prefills only
the new tail — measured ~0.2 s per call instead of ~3.5 s for a full prefill
(docs/benchmarks/t3po-eval-report.md).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .simt import (
    REPETITION_PENALTY,
    STOP_TOKEN_BIAS_SCALES,
    STOP_TOKEN_IDS,
    SYSTEM_PROMPT,
    LatencyMode,
)

#: docs/06 #4: the prompt (and therefore the KV cache) has a hard ceiling. With
#: history_window=30 the measured prompt stayed under 800 tokens.
MAX_PROMPT_TOKENS = 4096
MAX_NEW_TOKENS = 128
PREFILL_STEP = 512


class PromptTooLongError(ValueError):
    pass


@dataclass(slots=True)
class CallStats:
    prompt_tokens: int
    reused_tokens: int
    generated_tokens: int
    total_s: float


class MlxT3poGenerator:
    def __init__(self, model_path: Path) -> None:
        import mlx.core as mx
        from mlx_lm import load
        from mlx_lm.models.cache import make_prompt_cache

        self._mx = mx
        self._make_cache = make_prompt_cache
        self.model, self.tokenizer = load(str(model_path))
        mx.eval(self.model.parameters())
        self._cache: list[Any] = make_prompt_cache(self.model)
        self._cached: list[int] = []
        vocab = int(self.model.args.vocab_size)
        stop = np.zeros(vocab, dtype=bool)
        stop[list(STOP_TOKEN_IDS)] = True
        self._stop_mask = mx.array(stop)
        self._bias: dict[str, Any] = {}
        self._vocab = vocab
        self.last: CallStats | None = None

    def reset(self) -> None:
        self._cache = self._make_cache(self.model)
        self._cached = []

    def _tokens(self, user_message: str) -> list[int]:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]
        text = self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        return list(self.tokenizer.encode(text, add_special_tokens=False))

    def _prefill(self, tokens: list[int]) -> tuple[Any, int]:
        from mlx_lm.models.cache import trim_prompt_cache

        mx = self._mx
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
        for start in range(0, len(rest), PREFILL_STEP):
            chunk = mx.array(rest[start : start + PREFILL_STEP])[None]
            logits = self.model(chunk, cache=self._cache)
            mx.eval([c.state for c in self._cache])
        self._cached.extend(rest)
        assert logits is not None
        return logits[:, -1, :], common

    def _pick(self, logits: Any, mode: LatencyMode, seen: Any, *, block_stop: bool) -> int:
        mx = self._mx
        logits = logits.astype(mx.float32)
        if mode.tau != 0.0:
            bias = self._bias.get(mode.name)
            if bias is None:
                values = np.zeros(self._vocab, dtype=np.float32)
                for token_id in STOP_TOKEN_IDS:
                    values[token_id] = -mode.tau * STOP_TOKEN_BIAS_SCALES[token_id]
                bias = self._bias[mode.name] = mx.array(values)
            logits = logits + bias
            penalized = mx.where(
                logits > 0, logits / REPETITION_PENALTY, logits * REPETITION_PENALTY
            )
            logits = mx.where(seen, penalized, logits)
        if block_stop:
            logits = mx.where(self._stop_mask, -mx.inf, logits)
        return int(mx.argmax(logits, axis=-1).item())

    def __call__(self, user_message: str, *, force: bool, mode: LatencyMode) -> str:
        mx = self._mx
        started = time.perf_counter()
        tokens = self._tokens(user_message)
        if len(tokens) > MAX_PROMPT_TOKENS:
            raise PromptTooLongError(f"prompt has {len(tokens)} tokens (> {MAX_PROMPT_TOKENS})")
        logits, reused = self._prefill(tokens)
        seen = None
        if mode.tau != 0.0:
            mask = np.zeros(self._vocab, dtype=bool)
            mask[tokens] = True
            seen = mx.array(mask)
        generated: list[int] = []
        while True:
            token = self._pick(logits, mode, seen, block_stop=force and not generated)
            if token in STOP_TOKEN_IDS:
                break
            generated.append(token)
            if len(generated) >= MAX_NEW_TOKENS:
                break
            if seen is not None:
                seen = mx.where(mx.arange(self._vocab) == token, True, seen)
            logits = self.model(mx.array([[token]]), cache=self._cache)[:, -1, :]
            self._cached.append(token)
        self.last = CallStats(
            prompt_tokens=len(tokens),
            reused_tokens=reused,
            generated_tokens=len(generated),
            total_s=time.perf_counter() - started,
        )
        return self.tokenizer.decode(generated, skip_special_tokens=True).strip()
