"""Punctuation restoration on ONNX Runtime (CPU, 1 thread), insert-only.

The model is FunASR's CT-Transformer (`iic/punc_ct-transformer_zh-cn-common-
vocab272727-pytorch`, Apache-2.0 per its ModelScope card) exported to ONNX and
int8-quantised by sherpa-onnx (release `punctuation-models`, file
`sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8`). The
tokenizer vocabulary and the punctuation table are embedded in the ONNX
metadata (`tokens`, `punctuations`, `unk_symbol`), so one file is the whole
asset. sherpa-onnx's Python package is *not* used; this module reimplements
its tokenizer and decoder against onnxruntime directly. References (read
2026-10-05, k2-fsa/sherpa-onnx master):

* `sherpa-onnx/csrc/offline-punctuation-ct-transformer-impl.h` - windowed
  decoding (20-token windows that grow until a sentence end, forced split at
  200 tokens, argmax per token).
* `sherpa-onnx/csrc/offline-ct-transformer-model.cc` - metadata keys, input
  names (`inputs` int32 [1, T], `text_lengths` int32 [1]) and the output
  `logits` float [1, T, 6] over `<unk>|_|，|。|？|、`.
* `sherpa-onnx/csrc/text-utils.cc` `SplitUtf8` / `MergeCharactersIntoWords` and
  the model's `test.py` - CJK by character, runs of ASCII letters/digits as
  one lower-cased word.

Policy (docs/04): marks are only ever *inserted*. Existing punctuation, every
non-punctuation character and the order of the text are preserved, which the
tests assert and `benchmarks/punctuation_eval.py` re-checks on real data.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tarfile
import tempfile
import time
import unicodedata
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .model_spec import default_model_cache

PUNCTUATION_ARCHIVE_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/punctuation-models/"
    "sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8.tar.bz2"
)
PUNCTUATION_ARCHIVE_SHA256 = "c0d5aa5f8eeb686032345e180bedf39319dc2e0556781c6264bcadba8328a6e1"
PUNCTUATION_ARCHIVE_SIZE = 64_717_756
PUNCTUATION_ARCHIVE_MEMBER = (
    "sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8/model.int8.onnx"
)
PUNCTUATION_MODEL_FILENAME = "model.int8.onnx"
PUNCTUATION_MODEL_SHA256 = "65a3fb9f5ad7bfb96bf69e0dc4481df97f6ee60513c1d94ce981ba6effd524b1"
PUNCTUATION_MODEL_SIZE = 75_519_198
PUNCTUATION_SUBDIR = "punctuation"

SEGMENT_SIZE = 20
MAX_WINDOW = 200
MIN_TEXT_CHARS = 6
#: A partial's last characters are the least certain (the model has no right
#: context and the ASR may still rewrite them), so marks closer than this to
#: the end of a partial are not inserted. Finals use 0. Measured in
#: docs/benchmarks/punctuation-report.md.
DEFAULT_PARTIAL_TAIL_MARGIN = 2
#: Marks the model may produce; `_` (no mark) and `<unk>` never insert.
INSERTABLE = frozenset("，。？、")
#: Keep inserted marks at least this many tokens from any other mark.
DEFAULT_MIN_GAP = 3


class PunctuationUnavailableError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def punctuation_model_path(cache_dir: Path | None = None) -> Path:
    return (cache_dir or default_model_cache()) / PUNCTUATION_SUBDIR / PUNCTUATION_MODEL_FILENAME


def verify_asset(path: Path) -> bool:
    return path.is_file() and sha256(path) == PUNCTUATION_MODEL_SHA256


def locate_punctuation(cache_dir: Path | None = None) -> Path:
    path = punctuation_model_path(cache_dir)
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def prepare_punctuation(cache_dir: Path | None = None) -> Path:
    """Download and unpack the pinned model (about 65 MB). Explicit only.

    The archive hash is checked before anything is extracted and the extracted
    file is checked again, so a truncated or substituted download is refused.
    """

    target = punctuation_model_path(cache_dir)
    if verify_asset(target):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as scratch:
        archive = Path(scratch) / "model.tar.bz2"
        digest = hashlib.sha256()
        received = 0
        request = urllib.request.Request(
            PUNCTUATION_ARCHIVE_URL, headers={"User-Agent": "tea-asr-model-prepare"}
        )
        with urllib.request.urlopen(request, timeout=60) as response, archive.open("wb") as out:
            for block in iter(lambda: response.read(1024 * 256), b""):
                received += len(block)
                if received > PUNCTUATION_ARCHIVE_SIZE:
                    raise PunctuationUnavailableError("punctuation archive is larger than pinned")
                digest.update(block)
                out.write(block)
        if digest.hexdigest() != PUNCTUATION_ARCHIVE_SHA256:
            raise PunctuationUnavailableError("punctuation archive hash does not match the lock")
        extracted = Path(scratch) / PUNCTUATION_MODEL_FILENAME
        with tarfile.open(archive, "r:bz2") as tar:
            member = tar.getmember(PUNCTUATION_ARCHIVE_MEMBER)
            source = tar.extractfile(member)
            if source is None or member.size != PUNCTUATION_MODEL_SIZE:
                raise PunctuationUnavailableError("unexpected punctuation archive layout")
            with source, extracted.open("wb") as out:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    out.write(block)
        if sha256(extracted) != PUNCTUATION_MODEL_SHA256:
            raise PunctuationUnavailableError("punctuation model hash does not match the lock")
        os.replace(extracted, target)
    return target


# -- tokenizer ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Token:
    text: str  # lower-cased token as looked up in the vocabulary
    start: int  # span in the original string
    end: int


def _is_ascii_word_char(char: str) -> bool:
    return char < "\x80" and (char.isalnum() or char == "'")


def _is_mark(char: str) -> bool:
    return unicodedata.category(char)[0] in {"P", "S"} and not _is_ascii_word_char(char)


def tokenize(text: str) -> list[Token]:
    """CJK (and other non-ASCII letters) per character, ASCII runs per word.

    Whitespace and punctuation separate tokens and are not tokens themselves.
    Spans index the original string so callers can insert marks in place.
    """

    tokens: list[Token] = []
    index = 0
    size = len(text)
    while index < size:
        char = text[index]
        if _is_ascii_word_char(char):
            start = index
            while index < size and _is_ascii_word_char(text[index]):
                index += 1
            tokens.append(Token(text[start:index].lower(), start, index))
            continue
        if char.isspace() or _is_mark(char) or not char.isalnum():
            index += 1
            continue
        tokens.append(Token(char.lower(), index, index + 1))
        index += 1
    return tokens


def cjk_ratio(tokens: list[Token]) -> float:
    if not tokens:
        return 0.0
    cjk = sum(1 for token in tokens if token.text >= "\x80")
    return cjk / len(tokens)


# -- model --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PunctuationResult:
    text: str
    #: ``(index in text, mark)`` for each inserted mark, ascending.
    inserted: tuple[tuple[int, str], ...] = ()
    skipped: str | None = None
    elapsed_ms: float = 0.0

    @property
    def changed(self) -> bool:
        return bool(self.inserted)


@dataclass(slots=True)
class PunctuationStats:
    calls: int = 0
    inserted_marks: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    recent_ms: deque[float] = field(default_factory=lambda: deque(maxlen=1024))

    def record(self, elapsed_ms: float, inserted: int) -> None:
        self.calls += 1
        self.inserted_marks += inserted
        self.total_ms += elapsed_ms
        self.max_ms = max(self.max_ms, elapsed_ms)
        self.recent_ms.append(elapsed_ms)

    def snapshot(self) -> dict[str, float | int]:
        ordered = sorted(self.recent_ms)

        def pick(q: float) -> float:
            return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 2) if ordered else 0.0

        return {
            "calls": self.calls,
            "inserted_marks": self.inserted_marks,
            "mean_ms": round(self.total_ms / self.calls, 2) if self.calls else 0.0,
            "p50_ms": pick(0.5),
            "p95_ms": pick(0.95),
            "max_ms": round(self.max_ms, 2),
        }


class PunctuationModel:
    """One shared ONNX session; ``InferenceSession.run`` is thread-safe."""

    def __init__(
        self,
        model_path: Path | None = None,
        *,
        expected_sha256: str | None = PUNCTUATION_MODEL_SHA256,
        session: Any | None = None,
    ) -> None:
        if session is None:
            if model_path is None or not model_path.is_file():
                raise PunctuationUnavailableError(f"punctuation asset is missing: {model_path}")
            if expected_sha256 and sha256(model_path) != expected_sha256:
                raise PunctuationUnavailableError(
                    f"punctuation asset hash does not match the lock: {model_path}"
                )
            try:
                import onnxruntime
            except ImportError as exc:  # pragma: no cover - dependency is declared
                raise PunctuationUnavailableError("onnxruntime is not installed") from exc
            options = onnxruntime.SessionOptions()
            options.inter_op_num_threads = 1
            options.intra_op_num_threads = 1
            options.log_severity_level = 3
            session = onnxruntime.InferenceSession(
                str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
            )
        self._session: Any = session
        meta = session.get_modelmeta().custom_metadata_map
        try:
            tokens = meta["tokens"].split("|")
            self._marks: list[str] = meta["punctuations"].split("|")
            unk = meta["unk_symbol"]
            declared = int(meta["vocab_size"])
        except (KeyError, ValueError) as exc:
            raise PunctuationUnavailableError("punctuation metadata changed; refusing to guess") from exc
        if len(tokens) != declared or unk not in tokens:
            raise PunctuationUnavailableError("punctuation vocabulary is inconsistent")
        self._token_id = {token: index for index, token in enumerate(tokens)}
        self._unk_id = self._token_id[unk]
        try:
            self._none_id = self._marks.index("_")
            self._dot_id = self._marks.index("。")
            self._comma_id = self._marks.index("，")
            self._quest_id = self._marks.index("？")
        except ValueError as exc:
            raise PunctuationUnavailableError("punctuation table changed; refusing to guess") from exc
        input_names = [item.name for item in session.get_inputs()]
        if input_names != ["inputs", "text_lengths"]:
            raise PunctuationUnavailableError(f"unexpected punctuation inputs {input_names}")
        self.stats = PunctuationStats()

    # -- inference

    def _forward(self, ids: list[int]) -> list[int]:
        feed = {
            "inputs": np.asarray([ids], dtype=np.int32),
            "text_lengths": np.asarray([len(ids)], dtype=np.int32),
        }
        logits = np.asarray(self._session.run(None, feed)[0])
        if logits.ndim != 3 or logits.shape[1] != len(ids) or logits.shape[2] != len(self._marks):
            raise PunctuationUnavailableError(f"unexpected punctuation output {logits.shape}")
        return [int(value) for value in logits[0].argmax(axis=-1)]

    def predict(self, ids: list[int]) -> list[int]:
        """Mark id per token, following sherpa-onnx's windowed decoder.

        Windows of 20 tokens; the decisions up to the last sentence end of a
        window are kept and the next window starts right after it, so a window
        grows until the model finds a sentence end (a comma is promoted to one
        at 200 tokens). Tokens the reference would drop after the last sentence
        end are decoded with one extra pass so nothing is left unmarked.
        """

        count = len(ids)
        if count == 0:
            return []
        marks: list[int] = []
        last = -1
        segments = (count + SEGMENT_SIZE - 1) // SEGMENT_SIZE
        for i in range(segments):
            start = i * SEGMENT_SIZE
            end = min(start + SEGMENT_SIZE, count)
            if last != -1:
                start = last
            window = self._forward(ids[start:end])
            dot_index = -1
            comma_index = -1
            for m in range(len(window) - 2, 0, -1):
                if window[m] in (self._dot_id, self._quest_id):
                    dot_index = m
                    break
                if comma_index == -1 and window[m] == self._comma_id:
                    comma_index = m
            if dot_index == -1 and len(window) >= MAX_WINDOW and comma_index != -1:
                dot_index = comma_index
                window[dot_index] = self._dot_id
            if dot_index == -1:
                if last == -1:
                    last = start
                if i == segments - 1:
                    dot_index = len(window) - 1
            else:
                last = start + dot_index + 1
            if dot_index != -1:
                marks.extend(window[: dot_index + 1])
        if len(marks) < count:
            marks.extend(self._forward(ids[len(marks) :]))
        return marks[:count]

    # -- policy

    def restore(
        self,
        text: str,
        *,
        tail_margin: int = 0,
        terminal: bool = False,
        min_chars: int = MIN_TEXT_CHARS,
        min_cjk_ratio: float = 0.5,
        min_gap: int = DEFAULT_MIN_GAP,
    ) -> PunctuationResult:
        """Insert missing marks into ``text``; never edit anything else.

        ``tail_margin``: no mark is inserted when fewer than this many
        characters of text follow it (partials). ``terminal``: the text is a
        complete sentence (a final), so one that does not already end in a mark
        gets a closing one (the model's own ``？``, otherwise ``。``).
        ``min_gap``: an inserted mark keeps at least this many tokens from the
        nearest existing or inserted mark, so the model's guess never chops
        the ASR's own clause into one-word fragments.
        """

        started = time.perf_counter()
        skipped: str | None = None
        tokens: list[Token] = []
        if len(text.strip()) < min_chars:
            skipped = "short"
        else:
            tokens = tokenize(text)
            if len(tokens) < 2:
                skipped = "short"
            elif cjk_ratio(tokens) < min_cjk_ratio:
                skipped = "english"
        if skipped is not None:
            return PunctuationResult(text, skipped=skipped, elapsed_ms=_ms_since(started))
        ids = [self._token_id.get(token.text, self._unk_id) for token in tokens]
        predicted = self.predict(ids)

        has_mark = [
            any(
                _is_mark(char)
                for char in text[
                    token.end : tokens[position + 1].start
                    if position + 1 < len(tokens)
                    else len(text)
                ]
            )
            for position, token in enumerate(tokens)
        ]
        next_mark = [len(tokens) + min_gap] * len(tokens)
        upcoming = len(tokens) + min_gap
        for position in range(len(tokens) - 1, -1, -1):
            next_mark[position] = upcoming
            if has_mark[position]:
                upcoming = position
        insertions: list[tuple[int, str]] = []  # (index in original text, mark)
        remaining = sum(token.end - token.start for token in tokens)
        previous_mark = -min_gap - 1
        for position, token in enumerate(tokens):
            remaining -= token.end - token.start
            if has_mark[position]:
                previous_mark = position
                continue
            mark_id = predicted[position]
            mark = self._marks[mark_id] if mark_id < len(self._marks) else "_"
            is_last = position == len(tokens) - 1
            if is_last:
                if not terminal:
                    continue
                mark = mark if mark in ("。", "？") else "。"
            elif mark not in INSERTABLE or remaining < tail_margin or (
                position - previous_mark < min_gap
                or next_mark[position] - position < min_gap
            ):
                continue
            previous_mark = position
            insertions.append((token.end, mark))

        pieces: list[str] = []
        inserted: list[tuple[int, str]] = []
        cursor = 0
        for added, (index, mark) in enumerate(insertions):
            pieces.append(text[cursor:index])
            pieces.append(mark)
            inserted.append((index + added, mark))
            cursor = index
        pieces.append(text[cursor:])
        elapsed = _ms_since(started)
        self.stats.record(elapsed, len(inserted))
        return PunctuationResult("".join(pieces), tuple(inserted), None, elapsed)


def _is_soft(char: str) -> bool:
    return char.isspace() or unicodedata.category(char)[0] in {"P", "S"}


def hold_back_tail(text: str, count: int) -> str:
    """``text`` without its last ``count`` non-mark characters.

    Stable-prefix tracking sees this instead of the full partial when
    punctuation is on: every mark left of the cut was decided with at least
    ``count`` characters of right context (see ``restore(tail_margin=)``), so a
    mark can no longer appear *inside* text the tracker has already committed.
    Marks directly after the cut character are kept. The partial event itself
    always carries the full text.
    """

    if count <= 0:
        return text
    plain = [index for index, char in enumerate(text) if not _is_soft(char)]
    if len(plain) <= count:
        return ""
    cut = plain[len(plain) - count - 1] + 1
    while cut < len(text) and _is_soft(text[cut]) and not text[cut].isspace():
        cut += 1
    return text[:cut]


def _ms_since(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


@dataclass(slots=True)
class PunctuationRuntime:
    """Process-wide holder; inference runs on its own single worker thread."""

    model: PunctuationModel
    tail_margin: int = DEFAULT_PARTIAL_TAIL_MARGIN
    executor: ThreadPoolExecutor | None = None

    def __post_init__(self) -> None:
        if self.executor is None:
            self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="punct")

    def close(self) -> None:
        if self.executor is not None:
            self.executor.shutdown(wait=False, cancel_futures=True)

    async def restore(self, text: str, *, final: bool) -> PunctuationResult:
        """Run the model off the event loop (docs/06 constraint 3)."""

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self.executor,
            lambda: self.model.restore(
                text, tail_margin=0 if final else self.tail_margin, terminal=final
            ),
        )
