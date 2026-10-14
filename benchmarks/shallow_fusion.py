"""Offline decode-time hotword fusion experiment for the 8-bit Qwen3-ASR model.

All transcripts and model caches belong under the ignored service ``.soak`` directory.
This benchmark never changes or imports into the live server path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from benchmarks import church_eval_report, gold_offline_eval

SERVICES = ("20260704", "20260822", "20260912")
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORK = Path(
    os.environ.get("SHALLOW_FUSION_WORK", REPO_ROOT / ".soak/church-eval/work")
)
DEFAULT_MODEL_PATH = Path(
    os.environ.get("SHALLOW_FUSION_MODEL", REPO_ROOT / "models/mlx-8bit-selfconv")
)
DEFAULT_LIVE_DICTIONARY = (
    Path.home() / "Library/Application Support/TEA ASR/dictionaries/church.toml"
)
BETA_GRID = (0.0, 0.5, 1.0, 2.0, 3.0, 5.0)
FALSE_CHANGE_PENALTY = 4
BOOTSTRAP_ITERATIONS = 5000
BOOTSTRAP_SEED = 20261003
CACHE_VERSION = 1
REPLACEMENT_CHARACTER = "\ufffd"


@dataclass
class TrieNode:
    children: dict[int, int] = field(default_factory=dict)
    terminal: bool = False


class TokenTrie:
    """Token-prefix trie with a reset-to-root transition on a broken prefix."""

    def __init__(self, sequences: list[list[int]] | tuple[tuple[int, ...], ...]) -> None:
        self.nodes = [TrieNode()]
        for sequence in sequences:
            if not sequence:
                continue
            node = 0
            for raw_token in sequence:
                token = int(raw_token)
                if token < 0:
                    raise ValueError("token IDs must be non-negative")
                child = self.nodes[node].children.get(token)
                if child is None:
                    child = len(self.nodes)
                    self.nodes[node].children[token] = child
                    self.nodes.append(TrieNode())
                node = child
            self.nodes[node].terminal = True
        self.start_ids = frozenset(self.nodes[0].children)

    def advance(self, state: int, credit: int, token: int) -> tuple[int, int]:
        """Advance one generated token; broken prefixes reset, then may restart."""
        token = int(token)
        child = self.nodes[state].children.get(token)
        if child is not None:
            next_credit = credit + 1 if state != 0 else 1
            if self.nodes[child].terminal:
                next_credit = 0
            return child, next_credit

        restart = self.nodes[0].children.get(token)
        if restart is None:
            return 0, 0
        return restart, 0 if self.nodes[restart].terminal else 1


def apply_vectorized_bias(
    logits: Any,
    continuation_mask: Any,
    restart_mask: Any,
    beta: float,
    partial_credit: int,
    *,
    array_module: Any = np,
) -> Any:
    """Apply trie reward and rewind with vector operations over the vocabulary."""
    if beta == 0:
        return logits
    rewound = logits - (beta * partial_credit)
    adjusted = array_module.where(continuation_mask, logits + beta, rewound)
    if partial_credit or restart_mask is not None:
        adjusted = array_module.where(
            restart_mask,
            logits + beta - (beta * partial_credit),
            adjusted,
        )
    return adjusted


class TrieLogitsProcessor:
    """Greedy-decoding logit bias with a rewind when an incomplete prefix breaks.

    The active partial prefix earns ``beta`` per matched token. If the next selected
    token leaves that prefix, its logit gets a penalty equal to the outstanding credit;
    the state then resets (and can begin a different term on that same token). A terminal
    term commits its credit, so a later mismatch cannot rewind a completed term.
    """

    def __init__(
        self,
        sequences: list[list[int]] | tuple[tuple[int, ...], ...],
        beta: float,
        *,
        array_module: Any = np,
        mask_cache: dict[tuple[int, int], tuple[Any, Any]] | None = None,
    ) -> None:
        if not math.isfinite(beta) or beta < 0:
            raise ValueError("beta must be finite and non-negative")
        self.trie = TokenTrie(sequences)
        self.beta = float(beta)
        self.array_module = array_module
        self.state = 0
        self.partial_credit = 0
        self._last_length: int | None = None
        self._mask_cache = mask_cache if mask_cache is not None else {}

    def reset(self) -> None:
        self.state = 0
        self.partial_credit = 0
        self._last_length = None

    def advance(self, token: int) -> None:
        self.state, self.partial_credit = self.trie.advance(
            self.state, self.partial_credit, token
        )

    @staticmethod
    def _length(tokens: Any) -> int:
        shape = getattr(tokens, "shape", None)
        return int(shape[-1]) if shape else len(tokens)

    @staticmethod
    def _token_at(tokens: Any, index: int) -> int:
        shape = getattr(tokens, "shape", None)
        value = tokens[index] if not shape or len(shape) == 1 else tokens[0, index]
        if hasattr(value, "item"):
            value = value.item()
        return int(value)

    def _observe_tokens(self, tokens: Any) -> None:
        length = self._length(tokens)
        shape = getattr(tokens, "shape", None)
        if shape is not None and len(shape) > 1 and int(shape[0]) != 1:
            raise ValueError("TrieLogitsProcessor supports batch_size=1 only")
        if self._last_length is None:
            # The first call is the final prompt token; generated tokens follow it.
            self._last_length = length
            return
        if length < self._last_length:
            self.state = 0
            self.partial_credit = 0
            self._last_length = length
            return
        for index in range(self._last_length, length):
            self.advance(self._token_at(tokens, index))
        self._last_length = length

    def _masks(self, state: int, vocab_size: int) -> tuple[Any, Any]:
        key = (state, vocab_size)
        cached = self._mask_cache.get(key)
        if cached is not None:
            return cached

        continuation_ids = self.trie.nodes[state].children
        restart_ids = self.trie.start_ids.difference(continuation_ids)
        all_ids = (*continuation_ids, *restart_ids)
        if any(token >= vocab_size for token in all_ids):
            raise ValueError("target tokenizer emitted a token outside the model vocabulary")
        continuation = np.zeros(vocab_size, dtype=bool)
        restart = np.zeros(vocab_size, dtype=bool)
        continuation[list(continuation_ids)] = True
        restart[list(restart_ids)] = True
        if self.array_module is not np:
            continuation = self.array_module.array(continuation)
            restart = self.array_module.array(restart)
        self._mask_cache[key] = (continuation, restart)
        return continuation, restart

    def __call__(self, tokens: Any, logits: Any) -> Any:
        if self.beta == 0:
            return logits
        self._observe_tokens(tokens)
        vocab_size = int(logits.shape[-1])
        continuation, restart = self._masks(self.state, vocab_size)
        return apply_vectorized_bias(
            logits,
            continuation,
            restart,
            self.beta,
            self.partial_credit,
            array_module=self.array_module,
        )


def tokenize_term_variants(
    tokenizer: Any, term: str, *, include_leading_space: bool = True
) -> list[list[int]]:
    """Encode the standalone and optional leading-space forms without special tokens."""
    variants = [term]
    if include_leading_space:
        variants.append(f" {term}")
    tokenizations: list[list[int]] = []
    for variant in variants:
        encoded = tokenizer.encode(variant, add_special_tokens=False)
        if hasattr(encoded, "tolist"):
            encoded = encoded.tolist()
        if encoded and isinstance(encoded[0], list):
            if len(encoded) != 1:
                raise ValueError("tokenizer returned multiple rows for a single term")
            encoded = encoded[0]
        token_ids = [int(token) for token in encoded]
        if token_ids and token_ids not in tokenizations:
            tokenizations.append(token_ids)
    return tokenizations


def _tokenizer_from_local_path(model_path: Path) -> Any:
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise RuntimeError("targets requires transformers in the coordinator venv") from exc
    return AutoTokenizer.from_pretrained(
        model_path,
        local_files_only=True,
        trust_remote_code=True,
    )


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_fold_terms(
    fold: str,
    *,
    work: Path,
    live_dictionary: Path,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    from benchmarks.phonetic_rescore import HAN_RE, load_dictionary, mine_names

    dictionary_path = work / "dict-cv" / fold / "fold-safe.toml"
    fold_replacements, _, _ = load_dictionary(dictionary_path)
    _, _, live_hotwords = load_dictionary(live_dictionary)

    base_terms: list[dict[str, str]] = []
    seen: set[str] = set()
    for text, source in [
        *((target, "dict") for _, target in fold_replacements),
        *((hotword, "hot") for hotword in live_hotwords),
    ]:
        text = text.strip()
        if len(text) < 2 or not HAN_RE.fullmatch(text) or text in seen:
            continue
        seen.add(text)
        base_terms.append({"text": text, "source": source})

    other_services = [service for service in SERVICES if service != fold]
    references = [
        str(item["reference"])
        for service in other_services
        for item in _read_json(work / "answers" / f"{service}-pad25.json")["items"]
        if not item.get("music") and not item.get("unclear")
    ]
    name_terms = []
    for name in mine_names(references):
        if len(name) < 2 or not HAN_RE.fullmatch(name) or name in seen:
            continue
        seen.add(name)
        name_terms.append({"text": name, "source": "name"})
    return base_terms, name_terms


def build_targets(
    fold: str,
    *,
    work: Path = DEFAULT_WORK,
    model_path: Path = DEFAULT_MODEL_PATH,
    live_dictionary: Path = DEFAULT_LIVE_DICTIONARY,
    include_leading_space: bool = True,
) -> Path:
    if fold not in SERVICES:
        raise ValueError(f"unknown held-out service: {fold}")
    base_terms, name_terms = _read_fold_terms(
        fold, work=work, live_dictionary=live_dictionary
    )
    tokenizer = _tokenizer_from_local_path(model_path)
    for group in (base_terms, name_terms):
        for entry in group:
            entry["tokenizations"] = tokenize_term_variants(
                tokenizer,
                entry["text"],
                include_leading_space=include_leading_space,
            )

    output = work / "shallow-fusion" / "targets" / f"{fold}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "format": CACHE_VERSION,
        "fold": fold,
        "tokenizer_path": str(model_path),
        "leading_space_variant": include_leading_space,
        "base_terms": base_terms,
        "name_terms": name_terms,
    }
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", "utf-8")
    temporary.replace(output)
    print(
        f"targets {fold}: {len(base_terms)} dictionary/hotword terms, "
        f"{len(name_terms)} held-out-safe names; wrote {output}",
        file=sys.stderr,
    )
    return output


def _load_targets(path: Path, include_names: bool) -> tuple[list[list[int]], list[str]]:
    document = _read_json(path)
    if document.get("format") != CACHE_VERSION:
        raise ValueError(f"unsupported target file format: {path}")
    entries = list(document["base_terms"])
    if include_names:
        entries.extend(document["name_terms"])
    sequences: list[list[int]] = []
    terms: list[str] = []
    for entry in entries:
        text = str(entry["text"])
        if text not in terms:
            terms.append(text)
        sequences.extend([list(map(int, ids)) for ids in entry["tokenizations"]])
    return sequences, terms


def _load_hypotheses(path: Path) -> dict[str, str]:
    document = _read_json(path)
    if not isinstance(document, dict) or any(not isinstance(v, str) for v in document.values()):
        raise TypeError(f"hypotheses must be an item-id to string mapping: {path}")
    return {item_id: _strip_replacement_character(text) for item_id, text in document.items()}


def _strip_replacement_character(text: str) -> str:
    return text.replace(REPLACEMENT_CHARACTER, "")


def _legacy_baseline_path(work: Path, service: str) -> Path:
    return work / "eval" / "m8-c3" / f"{service}.json"


def _beta0_hypotheses_path(work: Path, service: str) -> Path:
    return work / "shallow-fusion" / "beta0" / f"{service}.json"


def _baseline_path(work: Path, service: str) -> Path:
    beta0_path = _beta0_hypotheses_path(work, service)
    if beta0_path.is_file():
        return beta0_path
    print(
        f"WARNING: beta=0 hypotheses missing for {service}; "
        "falling back to eval/m8-c3",
        file=sys.stderr,
        flush=True,
    )
    return _legacy_baseline_path(work, service)


def _answers_path(work: Path, service: str) -> Path:
    return work / "answers" / f"{service}-pad25.json"


def _dictionary_path(work: Path, fold: str) -> Path:
    return work / "dict-cv" / fold / "fold-safe.toml"


def _apply_dictionary(
    hypotheses: dict[str, str], dictionary_path: Path
) -> dict[str, str]:
    from tea_asr.context import ContextDictionaryStore, apply_replacements

    dictionary = ContextDictionaryStore(dictionary_path.parent).load(dictionary_path.stem)
    return {
        item_id: apply_replacements(text, dictionary.replacements)[0]
        for item_id, text in hypotheses.items()
    }


def _fingerprint(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    stat = path.stat()
    if path.suffix.lower() == ".wav":
        return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return {"size": stat.st_size, "sha256": digest.hexdigest()}


def _cache_path(
    work: Path,
    *,
    fold: str,
    service: str,
    beta: float,
    include_names: bool,
    system: str,
    subset: str,
) -> Path:
    beta_label = format(beta, ".6g")
    return (
        work
        / "shallow-fusion"
        / "cache"
        / fold
        / service
        / f"beta-{beta_label}"
        / f"names-{int(include_names)}"
        / f"{system.lower()}-{subset}.jsonl"
    )


def _cache_metadata(
    *,
    work: Path,
    fold: str,
    service: str,
    beta: float,
    include_names: bool,
    system: str,
    subset: str,
    model_path: Path,
    selected_ids: set[str] | None,
) -> dict[str, Any]:
    target_path = work / "shallow-fusion" / "targets" / f"{fold}.json"
    dictionary_path = _dictionary_path(work, fold) if system == "FB" else None
    answers_path = _answers_path(work, service)
    # Full decodes do not depend on a baseline. Keep their metadata compatible
    # with existing caches; tune subsets are seeded from the selected baseline.
    baseline_path = (
        _baseline_path(work, service)
        if subset == "tune"
        else _legacy_baseline_path(work, service)
    )
    wav_path = work / f"{service}.wav"
    return {
        "kind": "metadata",
        "cache_version": CACHE_VERSION,
        "fold": fold,
        "service": service,
        "beta": beta,
        "include_names": include_names,
        "system": system,
        "subset": subset,
        "model_path": str(model_path),
        "answers": _fingerprint(answers_path),
        "wav": _fingerprint(wav_path),
        "baseline": _fingerprint(baseline_path),
        "targets": _fingerprint(target_path),
        "dictionary": _fingerprint(dictionary_path),
        "carry_s": 3.0,
        "carry_max_gap_s": 1.5,
        "pad25_answers": True,
        "selected_ids_sha256": (
            hashlib.sha256("\n".join(sorted(selected_ids)).encode()).hexdigest()
            if selected_ids is not None
            else None
        ),
    }


def _read_item_cache(
    path: Path, expected_metadata: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(
            json.dumps(expected_metadata, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return {}

    contents = path.read_text(encoding="utf-8")
    lines = contents.splitlines()
    if not lines:
        raise ValueError(f"empty cache file: {path}")
    metadata = json.loads(lines[0])
    if metadata != expected_metadata:
        raise ValueError(f"cache inputs/options changed; use a new work directory: {path}")
    records: dict[str, dict[str, Any]] = {}
    truncated_tail = False
    for index, line in enumerate(lines[1:], start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1:
                # Remove the torn tail before any resumed run appends new records.
                path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
                truncated_tail = True
                break
            raise ValueError(f"invalid cache record at line {index + 1}: {path}")
        if record.get("kind") != "item":
            raise ValueError(f"unexpected cache record at line {index + 1}: {path}")
        text = record.get("text")
        records[str(record["id"])] = {
            "text": _strip_replacement_character(text) if isinstance(text, str) else text,
            "timing": record.get("timing", {}),
        }
    if not truncated_tail and not contents.endswith("\n"):
        with path.open("a", encoding="utf-8") as output:
            output.write("\n")
    return records


def _append_item_cache(
    path: Path, item_id: str, text: str, timing: dict[str, Any]
) -> None:
    record = {
        "kind": "item",
        "id": item_id,
        "text": _strip_replacement_character(text),
        "timing": timing,
    }
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        output.flush()


def _phonetic_terms(entries: list[dict[str, Any]]) -> list[Any]:
    from benchmarks.phonetic_rescore import make_terms

    try:
        return make_terms([(str(entry["text"]), str(entry["source"])) for entry in entries])
    except ImportError as exc:
        raise RuntimeError(
            "tune subset selection needs the optional pypinyin package in the coordinator venv"
        ) from exc


def select_tune_items(
    service: str,
    fold: str,
    *,
    work: Path = DEFAULT_WORK,
    random_controls: int = 300,
    seed: int = 20261013,
) -> set[str]:
    """Target exact/near-pinyin items, then add a reproducible random control sample."""
    from benchmarks.phonetic_rescore import find_candidates

    if service not in SERVICES or fold not in SERVICES:
        raise ValueError("service and fold must be one of the three evaluation services")
    target_path = work / "shallow-fusion" / "targets" / f"{fold}.json"
    target_document = _read_json(target_path)
    entries = [*target_document["base_terms"], *target_document["name_terms"]]
    term_texts = [str(entry["text"]) for entry in entries]
    phonetic = _phonetic_terms(entries)
    answers = _read_json(_answers_path(work, service))["items"]
    baseline = _load_hypotheses(_baseline_path(work, service))

    def has_match(text: str) -> bool:
        if any(term in text for term in term_texts):
            return True
        return bool(find_candidates(text, phonetic, cap=1))

    selected = {
        item["id"]
        for item in answers
        if has_match(str(item.get("reference", "")))
        or has_match(baseline.get(item["id"], ""))
    }
    controls = [item["id"] for item in answers if item["id"] not in selected]
    rng = random.Random(seed + int(service) + int(fold))
    selected.update(rng.sample(controls, min(random_controls, len(controls))))
    return selected


class ShallowFusionBackend(gold_offline_eval.TeaMlxBackend):
    """Offline-only backend that injects a processor below Model.generate()."""

    def __init__(self, model_path: Path, beta: float, sequences: list[list[int]]) -> None:
        super().__init__(model_path)
        self.beta = beta
        self.sequences = sequences
        self._mx: Any | None = None
        self._make_sampler: Any | None = None
        self._qwen: Any | None = None
        self._mask_cache: dict[tuple[int, int], tuple[Any, Any]] = {}

    def load(self) -> None:
        super().load()
        import inspect

        import mlx.core as mx
        from mlx_audio.stt.models.qwen3_asr import qwen3_asr
        from mlx_lm.sample_utils import make_sampler

        if "logits_processors" not in inspect.signature(
            self._model._generate_single_chunk
        ).parameters:
            raise RuntimeError("mlx-audio Qwen3-ASR has no lower-level logits processor hook")
        if "logits_processors" not in inspect.signature(self._model.stream_generate).parameters:
            raise RuntimeError("mlx-audio Qwen3-ASR stream_generate hook changed")
        self._mx = mx
        self._make_sampler = make_sampler
        self._qwen = qwen3_asr

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        language: str = "Chinese",
        system_prompt: str | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        from tea_asr.backend import (
            MAX_GENERATION_TOKENS,
            Transcription,
            _bounded_system_prompt,
            max_tokens_for_samples,
        )

        if self._model is None or self._mx is None or self._make_sampler is None:
            raise RuntimeError("Model has not been loaded")
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if samples.size == 0:
            return Transcription("", 0, 0, 0.0, 0, 0)
        if not np.isfinite(samples).all():
            raise ValueError("Audio contains NaN or infinity")
        generation_limit = (
            max_tokens_for_samples(int(samples.size)) if max_tokens is None else max_tokens
        )
        if not 1 <= generation_limit <= MAX_GENERATION_TOKENS:
            raise ValueError(f"max_tokens must be between 1 and {MAX_GENERATION_TOKENS}")

        bounded_prompt = (
            _bounded_system_prompt(system_prompt, self._model._tokenizer)
            if system_prompt is not None
            else None
        )
        sampler = self._make_sampler(
            0.0,
            1.0,
            0.0,
            min_tokens_to_keep=1,
            top_k=0,
        )
        processor = (
            TrieLogitsProcessor(
                self.sequences,
                self.beta,
                array_module=self._mx,
                mask_cache=self._mask_cache,
            )
            if self.beta > 0
            else None
        )
        logits_processors = [processor] if processor is not None else None
        chunks = self._qwen.split_audio_into_chunks(
            samples,
            sr=self._model.sample_rate,
            chunk_duration=1200.0,
            min_chunk_duration=1.0,
        )
        remaining_tokens = generation_limit
        texts: list[str] = []
        prompt_tokens = 0
        generation_tokens = 0
        started = time.time()
        for chunk_audio, _ in chunks:
            if remaining_tokens <= 0:
                break
            text, prompt_count, generation_count = self._model._generate_single_chunk(
                chunk_audio,
                max_tokens=remaining_tokens,
                sampler=sampler,
                logits_processors=logits_processors,
                language=language,
                prefill_step_size=2048,
                verbose=False,
                system_prompt=bounded_prompt,
            )
            texts.append(text)
            prompt_tokens += prompt_count
            generation_tokens += generation_count
            remaining_tokens -= generation_count
            self._mx.clear_cache()
        total_time = time.time() - started
        return Transcription(
            text=_strip_replacement_character(" ".join(texts).strip()),
            audio_samples=int(samples.size),
            model_input_samples=max(int(samples.size), 16_000),
            total_time_s=float(total_time),
            prompt_tokens=int(prompt_tokens),
            generation_tokens=int(generation_tokens),
        )


def _decode_service(
    service: str,
    fold: str,
    beta: float,
    include_names: bool,
    system: str,
    subset: str,
    *,
    work: Path,
    model_path: Path,
    random_controls: int = 300,
    seed: int = 20261013,
    selected_ids: set[str] | None = None,
) -> tuple[dict[str, str], dict[str, dict[str, Any]], set[str] | None]:
    if service not in SERVICES or fold not in SERVICES:
        raise ValueError("service and fold must be one of the three evaluation services")
    if system not in ("F", "FB") or subset not in ("full", "tune"):
        raise ValueError("system must be F/FB and subset must be full/tune")
    if not math.isfinite(beta) or beta < 0:
        raise ValueError("beta must be finite and non-negative")

    target_path = work / "shallow-fusion" / "targets" / f"{fold}.json"
    sequences, _ = _load_targets(target_path, include_names)
    selected = selected_ids
    if subset == "tune" and selected is None:
        selected = select_tune_items(
            service,
            fold,
            work=work,
            random_controls=random_controls,
            seed=seed,
        )
    if subset == "full":
        selected = None

    answers_path = _answers_path(work, service)
    dictionary_path = _dictionary_path(work, fold)
    dictionary = dictionary_path if system == "FB" else None
    seed_hypotheses = None
    if selected is not None:
        baseline = _load_hypotheses(_baseline_path(work, service))
        seed_hypotheses = (
            _apply_dictionary(baseline, dictionary_path) if system == "FB" else baseline
        )
    cache_path = _cache_path(
        work,
        fold=fold,
        service=service,
        beta=beta,
        include_names=include_names,
        system=system,
        subset=subset,
    )
    metadata = _cache_metadata(
        work=work,
        fold=fold,
        service=service,
        beta=beta,
        include_names=include_names,
        system=system,
        subset=subset,
        model_path=model_path,
        selected_ids=selected,
    )
    cached = _read_item_cache(cache_path, metadata)
    items = gold_offline_eval.load_items(answers_path)
    expected = selected if selected is not None else {item["id"] for item in items}
    extra = set(cached).difference(expected)
    if extra:
        raise ValueError(f"cache has {len(extra)} item(s) outside this run")
    completed = len(set(cached).intersection(expected))
    print(
        f"decode {service} fold={fold} beta={beta:g} names={int(include_names)} "
        f"system={system} subset={subset}: resume {completed}/{len(expected)} items",
        file=sys.stderr,
        flush=True,
    )

    def persist(item_id: str, text: str, timing: dict[str, Any]) -> None:
        _append_item_cache(cache_path, item_id, _strip_replacement_character(text), timing)
        persist.completed += 1
        print(
            f"decode {service} fold={fold} beta={beta:g} {persist.completed}/{len(expected)}",
            file=sys.stderr,
            flush=True,
        )

    persist.completed = completed  # type: ignore[attr-defined]

    args = argparse.Namespace(
        answers=answers_path,
        wav=work / f"{service}.wav",
        model_path=model_path,
        dictionary=dictionary,
        prompt=False,
        mode="carry",
        pad_s=0.0,
        carry_s=3.0,
        carry_max_gap_s=1.5,
    )
    output, timings = gold_offline_eval.run_evaluation(
        args,
        backend_factory=lambda path: ShallowFusionBackend(path, beta, sequences),
        selected_ids=selected,
        seed_hypotheses=seed_hypotheses if selected is not None else None,
        cached_results=cached,
        item_callback=persist,
    )
    output = {
        item_id: _strip_replacement_character(text) for item_id, text in output.items()
    }
    if beta == 0 and system == "F" and subset == "full":
        baseline_path = _beta0_hypotheses_path(work, service)
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = baseline_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(baseline_path)

        legacy = _load_hypotheses(_legacy_baseline_path(work, service))
        differing_ids = sorted(
            item_id
            for item_id in set(output).union(legacy)
            if output.get(item_id) != legacy.get(item_id)
        )
        print(
            f"beta=0 vs eval/m8-c3: {len(differing_ids)} item(s) differ for {service}",
            file=sys.stderr,
            flush=True,
        )
        for item_id in differing_ids:
            print(item_id, file=sys.stderr, flush=True)
    return output, timings, selected


def _aligned_indices(reference: str, hypothesis: str) -> tuple[list[int | None], list[int | None]]:
    """Return hypothesis-to-reference and reference-to-hypothesis edit alignments."""
    n, m = len(reference), len(hypothesis)
    costs = [[0] * (m + 1) for _ in range(n + 1)]
    steps = [[""] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        costs[i][0] = i
        steps[i][0] = "delete"
    for j in range(1, m + 1):
        costs[0][j] = j
        steps[0][j] = "insert"
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if reference[i - 1] == hypothesis[j - 1]:
                costs[i][j] = costs[i - 1][j - 1]
                steps[i][j] = "equal"
                continue
            options = (
                (costs[i - 1][j - 1] + 1, 0, "replace"),
                (costs[i - 1][j] + 1, 1, "delete"),
                (costs[i][j - 1] + 1, 2, "insert"),
            )
            costs[i][j], _, steps[i][j] = min(options)

    hyp_to_ref: list[int | None] = [None] * m
    ref_to_hyp: list[int | None] = [None] * n
    i, j = n, m
    while i or j:
        step = steps[i][j]
        if step in ("equal", "replace"):
            hyp_to_ref[j - 1] = i - 1
            ref_to_hyp[i - 1] = j - 1
            i -= 1
            j -= 1
        elif step == "delete":
            i -= 1
        elif step == "insert":
            j -= 1
        else:
            raise AssertionError("edit alignment did not reach the origin")
    return hyp_to_ref, ref_to_hyp


def _occurrences(text: str, term: str) -> list[int]:
    starts: list[int] = []
    offset = 0
    while term and offset <= len(text) - len(term):
        hit = text.find(term, offset)
        if hit < 0:
            break
        starts.append(hit)
        offset = hit + 1
    return starts


def item_has_false_change(
    reference: str,
    baseline: str,
    fusion: str,
    target_terms: list[str],
) -> bool:
    """Item-level guard: new false target span or a correct baseline char made wrong."""
    from benchmarks.cer_eval import normalize_for_scoring

    reference = normalize_for_scoring(reference, fold_pronouns=True)
    baseline = normalize_for_scoring(baseline, fold_pronouns=True)
    fusion = normalize_for_scoring(fusion, fold_pronouns=True)
    b_to_ref, _ = _aligned_indices(reference, baseline)
    _, f_to_hyp = _aligned_indices(reference, fusion)

    for hyp_index, ref_index in enumerate(b_to_ref):
        if ref_index is None or baseline[hyp_index] != reference[ref_index]:
            continue
        fusion_index = f_to_hyp[ref_index]
        if fusion_index is None or fusion[fusion_index] != reference[ref_index]:
            return True

    for raw_term in target_terms:
        from_term = normalize_for_scoring(raw_term, fold_pronouns=True)
        if len(from_term) < 2:
            continue
        baseline_positions: set[tuple[int | None, ...]] = set()
        for start in _occurrences(baseline, from_term):
            baseline_positions.add(tuple(b_to_ref[start : start + len(from_term)]))
        for start in _occurrences(fusion, from_term):
            mapped = tuple(f_to_hyp[start : start + len(from_term)])
            contiguous = bool(mapped) and mapped[0] is not None and all(
                index is not None and index == mapped[0] + offset
                for offset, index in enumerate(mapped)
            )
            exists_at_reference = bool(
                contiguous
                and mapped[0] is not None
                and reference[mapped[0] : mapped[0] + len(from_term)] == from_term
            )
            if not exists_at_reference and mapped not in baseline_positions:
                return True
    return False


def _parse_betas(value: str) -> list[float]:
    try:
        betas = [float(part.strip()) for part in value.split(",") if part.strip()]
    except ValueError as exc:
        raise ValueError("betas must be comma-separated numbers") from exc
    if not betas or any(not math.isfinite(beta) or beta < 0 for beta in betas):
        raise ValueError("betas must contain finite non-negative values")
    return sorted(set(betas))


def tune(
    *,
    work: Path = DEFAULT_WORK,
    model_path: Path = DEFAULT_MODEL_PATH,
    betas: tuple[float, ...] = BETA_GRID,
    random_controls: int = 300,
    seed: int = 20261013,
) -> Path:
    results: dict[str, Any] = {
        "format": CACHE_VERSION,
        "objective": "pronoun-folded edit errors + 4 * item false changes",
        "false_change_penalty": FALSE_CHANGE_PENALTY,
        "beta_grid": list(betas),
        "folds": {},
    }
    for fold in SERVICES:
        target_path = work / "shallow-fusion" / "targets" / f"{fold}.json"
        target_document = _read_json(target_path)
        if target_document.get("format") != CACHE_VERSION:
            raise ValueError(f"run targets before tune; missing {target_path}")
        training_services = [service for service in SERVICES if service != fold]
        selected_by_service = {
            service: select_tune_items(
                service,
                fold,
                work=work,
                random_controls=random_controls,
                seed=seed,
            )
            for service in training_services
        }
        grid: list[dict[str, Any]] = []
        for beta in betas:
            for include_names in (False, True):
                term_texts = [
                    str(entry["text"])
                    for entry in target_document["base_terms"]
                    + (target_document["name_terms"] if include_names else [])
                ]
                total_errors = 0
                false_changes = 0
                sample_counts: dict[str, int] = {}
                for service in training_services:
                    hypotheses, _, selected = _decode_service(
                        service,
                        fold,
                        beta,
                        include_names,
                        "FB",
                        "tune",
                        work=work,
                        model_path=model_path,
                        random_controls=random_controls,
                        seed=seed,
                        selected_ids=selected_by_service[service],
                    )
                    if selected is None:
                        raise AssertionError("tune decode did not return its item subset")
                    items = [
                        {**item, "service": service}
                        for item in gold_offline_eval.load_items(_answers_path(work, service))
                        if item["id"] in selected
                    ]
                    baseline = _apply_dictionary(
                        _load_hypotheses(_baseline_path(work, service)),
                        _dictionary_path(work, fold),
                    )
                    errors, _ = church_eval_report.item_errors(items, hypotheses, fold=True)
                    total_errors += int(errors.sum())
                    false_changes += sum(
                        item_has_false_change(
                            str(item.get("reference", "")),
                            baseline.get(item["id"], ""),
                            hypotheses.get(item["id"], ""),
                            term_texts,
                        )
                        for item in items
                    )
                    sample_counts[service] = len(items)
                grid.append(
                    {
                        "beta": beta,
                        "include_names": include_names,
                        "errors": total_errors,
                        "false_changes": false_changes,
                        "objective": total_errors + FALSE_CHANGE_PENALTY * false_changes,
                        "training_services": training_services,
                        "sample_counts": sample_counts,
                    }
                )
                print(
                    f"tune fold={fold} beta={beta:g} names={int(include_names)} "
                    f"errors={total_errors} false={false_changes}",
                    file=sys.stderr,
                    flush=True,
                )
        choice = min(
            grid,
            key=lambda row: (
                row["objective"],
                row["false_changes"],
                row["beta"],
                row["include_names"],
            ),
        )
        results["folds"][fold] = {
            "choice": choice,
            "grid": grid,
            "training_services": training_services,
        }

    output = work / "shallow-fusion" / "tune" / "results.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    print(f"tuning results: {output}", file=sys.stderr)
    return output


def _load_full_cache(
    *,
    work: Path,
    fold: str,
    service: str,
    beta: float,
    include_names: bool,
    system: str,
    model_path: Path,
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    path = _cache_path(
        work,
        fold=fold,
        service=service,
        beta=beta,
        include_names=include_names,
        system=system,
        subset="full",
    )
    metadata = _cache_metadata(
        work=work,
        fold=fold,
        service=service,
        beta=beta,
        include_names=include_names,
        system=system,
        subset="full",
        model_path=model_path,
        selected_ids=None,
    )
    records = _read_item_cache(path, metadata)
    expected_ids = {
        item["id"] for item in gold_offline_eval.load_items(_answers_path(work, service))
    }
    if set(records) != expected_ids:
        raise ValueError(
            f"incomplete full decode cache for {service}/{fold}/{system}: "
            f"{len(records)} of {len(expected_ids)} items"
        )
    hypotheses: dict[str, str] = {}
    timings: dict[str, dict[str, Any]] = {}
    for item_id, record in records.items():
        if not isinstance(record.get("text"), str) or not isinstance(record.get("timing"), dict):
            raise TypeError(f"malformed full decode cache for item {item_id}")
        hypotheses[item_id] = record["text"]
        timings[item_id] = record["timing"]
    return hypotheses, timings


def _format_cer(metric: dict[str, Any]) -> str:
    return (
        f"{100 * metric['cer']:.2f} "
        f"[{100 * metric['ci95_low']:.2f}, {100 * metric['ci95_high']:.2f}]"
    )


def _format_delta(metric: dict[str, Any]) -> str:
    return (
        f"{100 * metric['delta_cer']:+.3f} "
        f"[{100 * metric['ci95_low']:+.3f}, {100 * metric['ci95_high']:+.3f}]"
    )


def report(
    *,
    work: Path = DEFAULT_WORK,
    results_path: Path | None = None,
    markdown_path: Path | None = None,
    model_path: Path = DEFAULT_MODEL_PATH,
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
) -> Path:
    results_path = results_path or work / "shallow-fusion" / "tune" / "results.json"
    markdown_path = markdown_path or (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "benchmarks"
        / "shallow-fusion-2026-10.md"
    )
    tuning = _read_json(results_path)
    if tuning.get("format") != CACHE_VERSION:
        raise ValueError(f"unsupported tuning results: {results_path}")

    items = church_eval_report.load_answers(work, SERVICES)
    hypotheses: dict[str, dict[str, str]] = {name: {} for name in ("A", "B", "F", "FB")}
    choices: dict[str, dict[str, Any]] = {}
    false_changes_by_service: dict[str, int] = {}
    latency_by_service: dict[str, list[float]] = {}
    for service in SERVICES:
        choice = tuning["folds"][service]["choice"]
        choices[service] = choice
        baseline = _load_hypotheses(_baseline_path(work, service))
        hypotheses["A"].update(baseline)
        hypotheses["B"].update(_apply_dictionary(baseline, _dictionary_path(work, service)))

        fused, _ = _load_full_cache(
            work=work,
            fold=service,
            service=service,
            beta=float(choice["beta"]),
            include_names=bool(choice["include_names"]),
            system="F",
            model_path=model_path,
        )
        fused_replaced, fused_timing = _load_full_cache(
            work=work,
            fold=service,
            service=service,
            beta=float(choice["beta"]),
            include_names=bool(choice["include_names"]),
            system="FB",
            model_path=model_path,
        )
        zero_timing = _load_full_cache(
            work=work,
            fold=service,
            service=service,
            beta=0.0,
            include_names=False,
            system="F",
            model_path=model_path,
        )[1]
        hypotheses["F"].update(fused)
        hypotheses["FB"].update(fused_replaced)

        target_document = _read_json(
            work / "shallow-fusion" / "targets" / f"{service}.json"
        )
        target_entries = list(target_document["base_terms"])
        if choice["include_names"]:
            target_entries.extend(target_document["name_terms"])
        target_terms = [str(entry["text"]) for entry in target_entries]
        service_items = _read_json(_answers_path(work, service))["items"]
        false_changes_by_service[service] = sum(
            item_has_false_change(
                str(item.get("reference", "")),
                hypotheses["B"].get(item["id"], ""),
                fused_replaced.get(item["id"], ""),
                target_terms,
            )
            for item in service_items
        )
        latency_by_service[service] = [
            (
                float(fused_timing[item["id"]]["decode_s"])
                - float(zero_timing[item["id"]]["decode_s"])
            )
            * 1000
            for item in service_items
        ]

    scores: dict[str, tuple[np.ndarray, np.ndarray]] = {
        system: church_eval_report.item_errors(items, hypotheses[system], fold=True)
        for system in ("A", "B", "F", "FB")
    }
    scorer = church_eval_report.Scorer(items, iterations, seed)
    scopes = (*SERVICES, "pooled")
    cer = {
        system: {scope: scorer.cer(*scores[system], scope) for scope in scopes}
        for system in ("A", "B", "F", "FB")
    }
    deltas = {
        scope: scorer.delta(scores["FB"], scores["B"], scope) for scope in scopes
    }
    audio_hours = (
        sum(float(item["end_s"]) - float(item["start_s"]) for item in items) / 3600
    )
    false_changes = sum(false_changes_by_service.values())
    false_per_hour = false_changes / audio_hours if audio_hours else math.inf
    all_latency = [value for service in SERVICES for value in latency_by_service[service]]
    p50_ms = float(np.quantile(all_latency, 0.50))
    p95_ms = float(np.quantile(all_latency, 0.95))
    criteria = {
        "pooled CI upper below zero": deltas["pooled"]["ci95_high"] < 0,
        "false changes at most one per hour": false_per_hour <= 1.0,
        "p95 added time at most 50 ms": p95_ms <= 50.0,
    }
    verdict = "GO" if all(criteria.values()) else "NO-GO"

    scope_header = " | ".join(SERVICES) + " | pooled"
    lines = [
        "# Hotword shallow fusion (offline, 2026-10)",
        "",
        (
            "Metrics only: no transcript, caption, audio text, or target term is included. "
            "Commands are in [docs/09](../09-testing-guide.md#hotword-shallow-fusion-logit-bias-offline-experiment-2026-10)."
        ),
        "",
        "## Verdict",
        "",
        f"**{verdict}** for live integration.",
        "",
        "| criterion | result | met |",
        "|---|---|---|",
        f"| FB − B pooled CER CI upper < 0 | {_format_delta(deltas['pooled'])} pp | {'yes' if criteria['pooled CI upper below zero'] else 'no'} |",
        f"| false changes ≤ 1 / audio hour | {false_changes} / {audio_hours:.2f} h = {false_per_hour:.2f}/h | {'yes' if criteria['false changes at most one per hour'] else 'no'} |",
        f"| p95 added decode time ≤ 50 ms / final | {p95_ms:.1f} ms (p50 {p50_ms:.1f} ms) | {'yes' if criteria['p95 added time at most 50 ms'] else 'no'} |",
        "",
        "## Cross-validation choices",
        "",
        "| held-out service | β | names | training errors | false changes | objective |",
        "|---|---:|---|---:|---:|---:|",
    ]
    for service in SERVICES:
        choice = choices[service]
        lines.append(
            f"| {service} | {choice['beta']:g} | "
            f"{'on' if choice['include_names'] else 'off'} | {choice['errors']} | "
            f"{choice['false_changes']} | {choice['objective']} |"
        )
    lines += [
        "",
        "## Pronoun-folded CER (%, 5-minute-cluster bootstrap 95% CI)",
        "",
        f"| system | {scope_header} |",
        "|---|" + "---:|" * (len(SERVICES) + 1),
    ]
    system_names = {
        "A": "A current-code β=0 decode",
        "B": "B = A + fold dictionary replacements",
        "F": "F(β) decode-time fusion",
        "FB": "FB(β) fusion + dictionary replacements",
    }
    for system in ("A", "B", "F", "FB"):
        values = " | ".join(_format_cer(cer[system][scope]) for scope in scopes)
        lines.append(f"| {system_names[system]} | {values} |")
    lines += [
        "",
        "## Paired FB − B CER difference (percentage points; negative favours FB)",
        "",
        "| " + " | ".join(SERVICES) + " | pooled |",
        "|---|" + "---:|" * (len(SERVICES) + 1),
        "| FB − B | " + " | ".join(_format_delta(deltas[s]) for s in (*SERVICES, "pooled")) + " |",
        "",
        "## False changes and decode time",
        "",
        "| held-out service | false changes | added decode p50 (ms) | added decode p95 (ms) |",
        "|---|---:|---:|---:|",
    ]
    for service in SERVICES:
        values = latency_by_service[service]
        lines.append(
            f"| {service} | {false_changes_by_service[service]} | "
            f"{np.quantile(values, 0.50):.1f} | {np.quantile(values, 0.95):.1f} |"
        )
    lines += [
        "",
        "## Method",
        "",
        (
            "Targets are the held-out fold dictionary `to` values plus the 13 read-only live hotwords; "
            "optional title names are mined only from the other two services. Terms are Han-only and "
            "at least two characters. Tokenization uses the local 8-bit model tokenizer, with standalone "
            "and leading-space variants."
        ),
        "",
        (
            "The processor boosts valid trie continuations by β. If a partial match is abandoned, the "
            "breaking transition pays back the accumulated partial reward; completed terms keep their "
            "reward. `Model.generate()` drops unknown kwargs in mlx-audio 0.4.5, so this offline benchmark "
            "calls Qwen3-ASR `_generate_single_chunk()` with the existing `stream_generate()` processor hook. "
            "No live server code changed."
        ),
        "",
        (
            "A is the current-code β=0 full decode; B applies the held-out fold dictionary replacements "
            "to A. If the β=0 output is missing, the benchmark warns and falls back to `eval/m8-c3`. "
            "The β=0 diagnostic reports how many outputs differ from m8-c3 and prints only their item ids. "
            "U+FFFD is removed from every decoded hypothesis. Audio uses the pad25 answer spans, 3-second "
            "carry with the matrix's 1.5-second gap limit, shared overlap stripping, PUA filtering and repetition trim. "
            "CER follows `church_eval_report.py` with pronoun folding and paired 5-minute-cluster bootstrap "
            f"({iterations} draws, seed {seed}). False changes are counted per item; audio hours are the "
            "sum of scored answer spans. Added time is paired per-final `decode_s` versus β=0."
        ),
        "",
    ]
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"{verdict}: wrote aggregate-only report {markdown_path}")
    return markdown_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    target_parser = commands.add_parser("targets", help="build held-out fold target tries")
    target_parser.add_argument("--fold", choices=SERVICES)
    target_parser.add_argument("--work", type=Path, default=DEFAULT_WORK)
    target_parser.add_argument("--model-path", type=Path, default=DEFAULT_MODEL_PATH)
    target_parser.add_argument("--live-dictionary", type=Path, default=DEFAULT_LIVE_DICTIONARY)
    target_parser.add_argument("--no-leading-space-variant", action="store_true")

    decode_parser = commands.add_parser(
        "decode", help="decode one service/config with resumable per-item caches"
    )
    decode_parser.add_argument("--service", choices=SERVICES, required=True)
    decode_parser.add_argument("--fold", choices=SERVICES, required=True)
    decode_parser.add_argument("--beta", type=float)
    decode_parser.add_argument("--tuned", type=Path)
    decode_parser.add_argument("--include-names", action="store_true", default=None)
    decode_parser.add_argument("--system", choices=("F", "FB"), default="FB")
    decode_parser.add_argument("--subset", choices=("full", "tune"), default="full")
    decode_parser.add_argument("--random-controls", type=int, default=300)
    decode_parser.add_argument("--seed", type=int, default=20261013)
    decode_parser.add_argument("--work", type=Path, default=DEFAULT_WORK)
    decode_parser.add_argument("--model-path", type=Path, default=DEFAULT_MODEL_PATH)

    tune_parser = commands.add_parser("tune", help="cross-validate beta and name inclusion")
    tune_parser.add_argument("--work", type=Path, default=DEFAULT_WORK)
    tune_parser.add_argument("--model-path", type=Path, default=DEFAULT_MODEL_PATH)
    tune_parser.add_argument(
        "--betas", default=",".join(format(beta, "g") for beta in BETA_GRID)
    )
    tune_parser.add_argument("--random-controls", type=int, default=300)
    tune_parser.add_argument("--seed", type=int, default=20261013)

    report_parser = commands.add_parser("report", help="write aggregate-only CV metrics")
    report_parser.add_argument("--work", type=Path, default=DEFAULT_WORK)
    report_parser.add_argument("--results", type=Path)
    report_parser.add_argument("--markdown", type=Path)
    report_parser.add_argument("--model-path", type=Path, default=DEFAULT_MODEL_PATH)
    report_parser.add_argument("--iterations", type=int, default=BOOTSTRAP_ITERATIONS)
    report_parser.add_argument("--seed", type=int, default=BOOTSTRAP_SEED)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "targets":
            folds = [args.fold] if args.fold else list(SERVICES)
            for fold in folds:
                build_targets(
                    fold,
                    work=args.work,
                    model_path=args.model_path,
                    live_dictionary=args.live_dictionary,
                    include_leading_space=not args.no_leading_space_variant,
                )
        elif args.command == "decode":
            include_names = bool(args.include_names)
            beta = args.beta
            if args.tuned is not None:
                if beta is not None or args.include_names is not None:
                    parser.error("--tuned cannot be combined with --beta or --include-names")
                choice = _read_json(args.tuned)["folds"][args.fold]["choice"]
                beta = float(choice["beta"])
                include_names = bool(choice["include_names"])
            if beta is None:
                parser.error("decode requires --beta or --tuned")
            if args.random_controls < 0:
                parser.error("--random-controls must be non-negative")
            _decode_service(
                args.service,
                args.fold,
                beta,
                include_names,
                args.system,
                args.subset,
                work=args.work,
                model_path=args.model_path,
                random_controls=args.random_controls,
                seed=args.seed,
            )
        elif args.command == "tune":
            if args.random_controls < 0:
                parser.error("--random-controls must be non-negative")
            result = tune(
                work=args.work,
                model_path=args.model_path,
                betas=tuple(_parse_betas(args.betas)),
                random_controls=args.random_controls,
                seed=args.seed,
            )
            print(result)
        elif args.command == "report":
            if args.iterations <= 0:
                parser.error("--iterations must be positive")
            report(
                work=args.work,
                results_path=args.results,
                markdown_path=args.markdown,
                model_path=args.model_path,
                iterations=args.iterations,
                seed=args.seed,
            )
        return 0
    except (FileNotFoundError, ImportError, RuntimeError, TypeError, ValueError) as exc:
        print(f"shallow-fusion: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
