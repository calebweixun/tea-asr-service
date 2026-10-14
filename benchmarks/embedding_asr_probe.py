#!/usr/bin/env python3
"""Offline audio/text embedding probe for the church evaluation corpus.

Private transcript strings are read only into memory. Cache and report files
contain embeddings, row indices, timings, and aggregate metrics only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import sys
import time
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import numpy as np

ROOT = Path(__file__).resolve().parents[1]

from benchmarks.cer_eval import normalize_for_scoring
from tea_asr.context import (
    ContextDictionaryStore,
    ReplacementRule,
    apply_replacements,
)

SERVICES = ("20260704", "20260822", "20260912")
DEFAULT_DATA_ROOT = Path(
    os.environ.get("EMBEDDING_PROBE_DATA_ROOT", ROOT / ".soak/church-eval/work")
).expanduser()
DEFAULT_MODEL_DIR = Path(
    os.environ.get("EMBEDDING_PROBE_MODEL_DIR", ROOT / "models/embeddinggemma-2")
).expanduser()
DEFAULT_DICTIONARY_DIR = Path.home() / "Library/Application Support/TEA ASR/dictionaries"
DEFAULT_OUTPUT_DIR = DEFAULT_DATA_ROOT / "embedding-probe"
TEXT_PROMPT_NAME = "SentenceSimilarity"
EMBEDDING_DIM = 768
TRUNCATED_DIM = 256
MODALITIES = ("audio", "reference", "hypothesis", "rule_to", "rule_from")


@dataclass(frozen=True)
class ProbeItem:
    item_id: str
    start_s: float
    end_s: float
    reference: str
    hypothesis: str

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_s - self.start_s)


@dataclass(frozen=True)
class RuleVariantPair:
    item_index: int
    rule_index: int
    from_text: str
    to_text: str


@dataclass(frozen=True)
class ServiceData:
    service: str
    items: tuple[ProbeItem, ...]
    wav_path: Path
    rules: tuple[ReplacementRule, ...]
    rule_pairs: tuple[RuleVariantPair, ...]
    source_signature: str


class Encoder(Protocol):
    def encode_texts(self, texts: Sequence[str]) -> np.ndarray: ...

    def encode_audio(self, samples: np.ndarray) -> np.ndarray: ...


def normalize_text(text: str) -> str:
    """Use CER's NFKC/punctuation normalization and God-pronoun folding."""
    return normalize_for_scoring(text, fold_pronouns=True)


def edit_distance(left: str, right: str) -> int:
    """Return character-level Levenshtein distance using O(min(n,m)) memory."""
    if len(left) < len(right):
        left, right = right, left
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_char in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def edit_distance_bucket(distance: int) -> str | None:
    if distance <= 0:
        return None
    if distance == 1:
        return "1_char"
    if distance <= 3:
        return "2_3_chars"
    return "4_plus_chars"


def normalize_embeddings(vectors: np.ndarray) -> np.ndarray:
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return np.divide(matrix, np.maximum(norms, 1e-12), out=np.zeros_like(matrix))


def cosine_matrix(queries: np.ndarray, candidates: np.ndarray) -> np.ndarray:
    return normalize_embeddings(queries) @ normalize_embeddings(candidates).T


def random_retrieval_baseline(
    candidate_count: int, relevant_counts: Sequence[int]
) -> dict[str, float | int | None]:
    """Expected retrieval metrics for uniformly random candidate ordering."""
    if candidate_count <= 0 or not relevant_counts:
        return {"candidate_count": candidate_count, "top1": None, "top5": None, "mrr": None}

    top1_values: list[float] = []
    top5_values: list[float] = []
    mrr_values: list[float] = []
    for raw_relevant in relevant_counts:
        relevant = min(max(int(raw_relevant), 0), candidate_count)
        if relevant == 0:
            top1_values.append(0.0)
            top5_values.append(0.0)
            mrr_values.append(0.0)
            continue
        top1_values.append(relevant / candidate_count)
        no_hit = 1.0
        cutoff = min(5, candidate_count)
        for position in range(cutoff):
            no_hit *= (candidate_count - relevant - position) / (candidate_count - position)
        top5_values.append(1.0 - no_hit)

        first_relevant = relevant / candidate_count
        no_relevant_before = (candidate_count - relevant) / candidate_count
        expected_reciprocal_rank = first_relevant
        for rank in range(2, candidate_count - relevant + 2):
            remaining_positions = candidate_count - rank + 1
            first_at_rank = no_relevant_before * relevant / remaining_positions
            expected_reciprocal_rank += first_at_rank / rank
            no_relevant_before *= (
                candidate_count - relevant - rank + 1
            ) / (candidate_count - rank + 1)
        mrr_values.append(expected_reciprocal_rank)

    return {
        "candidate_count": candidate_count,
        "top1": float(np.mean(top1_values)),
        "top5": float(np.mean(top5_values)),
        "mrr": float(np.mean(mrr_values)),
    }


def retrieval_metrics(
    scores: np.ndarray, relevant_candidates: Sequence[Sequence[int] | int]
) -> dict[str, Any]:
    """Compute top-1, top-5 and MRR; duplicate references are all relevant."""
    matrix = np.asarray(scores, dtype=np.float64)
    if matrix.ndim != 2 or matrix.shape[0] != len(relevant_candidates):
        raise ValueError("scores must be a 2D matrix with one row per query")
    candidate_count = matrix.shape[1]
    ranks: list[int] = []
    relevant_counts: list[int] = []
    for row, target in zip(matrix, relevant_candidates, strict=True):
        targets = {int(target)} if isinstance(target, (int, np.integer)) else {int(i) for i in target}
        targets = {index for index in targets if 0 <= index < candidate_count}
        if not targets:
            raise ValueError("every query must have at least one relevant candidate")
        order = np.argsort(-row, kind="stable")
        positions = np.empty(candidate_count, dtype=np.int64)
        positions[order] = np.arange(1, candidate_count + 1)
        ranks.append(int(min(positions[list(targets)])))
        relevant_counts.append(len(targets))

    count = len(ranks)
    if count == 0:
        return {
            "query_count": 0,
            "candidate_count": candidate_count,
            "top1_count": 0,
            "top5_count": 0,
            "reciprocal_rank_sum": 0.0,
            "top1": None,
            "top5": None,
            "mrr": None,
            "random_baseline": random_retrieval_baseline(candidate_count, []),
        }
    top1_count = sum(rank <= 1 for rank in ranks)
    top5_count = sum(rank <= min(5, candidate_count) for rank in ranks)
    reciprocal_rank_sum = float(sum(1.0 / rank for rank in ranks))
    return {
        "query_count": count,
        "candidate_count": candidate_count,
        "top1_count": top1_count,
        "top5_count": top5_count,
        "reciprocal_rank_sum": reciprocal_rank_sum,
        "top1": top1_count / count,
        "top5": top5_count / count,
        "mrr": reciprocal_rank_sum / count,
        "random_baseline": random_retrieval_baseline(candidate_count, relevant_counts),
    }


def pairwise_accuracy(
    audio_embeddings: np.ndarray,
    preferred_embeddings: np.ndarray,
    alternative_embeddings: np.ndarray,
) -> dict[str, Any]:
    """Measure strict cosine wins; exact ties remain in the denominator."""
    audio = np.asarray(audio_embeddings, dtype=np.float32)
    preferred = np.asarray(preferred_embeddings, dtype=np.float32)
    alternative = np.asarray(alternative_embeddings, dtype=np.float32)
    if audio.ndim != 2 or preferred.shape != audio.shape or alternative.shape != audio.shape:
        raise ValueError("pairwise embedding matrices must have the same 2D shape")
    valid = (
        np.isfinite(audio).all(axis=1)
        & np.isfinite(preferred).all(axis=1)
        & np.isfinite(alternative).all(axis=1)
    )
    if not np.any(valid):
        return {"count": 0, "wins": 0, "ties": 0, "accuracy": None, "mean_margin": None}
    audio_norm = normalize_embeddings(audio[valid])
    preferred_norm = normalize_embeddings(preferred[valid])
    alternative_norm = normalize_embeddings(alternative[valid])
    preferred_scores = np.einsum("ij,ij->i", audio_norm, preferred_norm)
    alternative_scores = np.einsum("ij,ij->i", audio_norm, alternative_norm)
    wins = preferred_scores > alternative_scores
    ties = preferred_scores == alternative_scores
    count = int(valid.sum())
    return {
        "count": count,
        "wins": int(wins.sum()),
        "ties": int(ties.sum()),
        "accuracy": float(wins.mean()),
        "mean_margin": float(np.mean(preferred_scores - alternative_scores)),
    }


def build_rule_variant_pairs(
    items: Sequence[ProbeItem], rules: Sequence[ReplacementRule]
) -> list[RuleVariantPair]:
    """Build full-context `to`/`from` text pairs where a rule fixes the term."""
    pairs: list[RuleVariantPair] = []
    for item_index, item in enumerate(items):
        if not item.reference or not item.hypothesis:
            continue
        normalized_reference = normalize_text(item.reference)
        normalized_hypothesis = normalize_text(item.hypothesis)
        if normalized_reference == normalized_hypothesis:
            continue
        original_distance = edit_distance(normalized_reference, normalized_hypothesis)
        for rule_index, rule in enumerate(rules):
            if not rule.source or not rule.target or rule.target not in item.reference:
                continue
            corrected, matches = apply_replacements(item.hypothesis, (rule,))
            if matches == 0:
                continue
            corrected_distance = edit_distance(
                normalized_reference, normalize_text(corrected)
            )
            if corrected_distance >= original_distance:
                continue
            pairs.append(
                RuleVariantPair(
                    item_index=item_index,
                    rule_index=rule_index,
                    from_text=item.hypothesis,
                    to_text=corrected,
                )
            )
    return pairs


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not read benchmark input: {path.name}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_fingerprint(path: Path, *, hash_contents: bool) -> dict[str, Any]:
    stat = path.stat()
    fingerprint: dict[str, Any] = {"name": path.name, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    if hash_contents:
        fingerprint["sha256"] = _sha256(path)
    return fingerprint


def _flagged(item: dict[str, Any], name: str) -> bool:
    if bool(item.get(name)):
        return True
    flags = item.get("flags")
    if isinstance(flags, (list, tuple, set)):
        return name in flags
    if isinstance(flags, dict):
        return bool(flags.get(name))
    return False


def load_service_data(
    data_root: Path,
    service: str,
    dictionary_dir: Path = DEFAULT_DICTIONARY_DIR,
    model_dir: Path = DEFAULT_MODEL_DIR,
    device: str = "cpu",
) -> ServiceData:
    if service not in SERVICES:
        raise ValueError(f"unknown service: {service}")
    answers_path = data_root / "answers" / f"{service}-pad25.json"
    hypotheses_path = data_root / "eval" / "m8-c3" / f"{service}.json"
    wav_path = data_root / f"{service}.wav"
    dictionary_path = dictionary_dir / "church.toml"
    answers_document = _read_json(answers_path)
    answer_rows = answers_document if isinstance(answers_document, list) else answers_document.get("items", [])
    hypotheses = _read_json(hypotheses_path)
    if not isinstance(answer_rows, list) or not isinstance(hypotheses, dict):
        raise TypeError(f"unexpected benchmark input structure for {service}")

    rows: list[ProbeItem] = []
    for record in answer_rows:
        if not isinstance(record, dict) or _flagged(record, "unclear") or _flagged(record, "music"):
            continue
        try:
            item_id = str(record["id"])
            start_s = float(record["start_s"])
            end_s = float(record["end_s"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"invalid timing row in {answers_path.name}") from exc
        reference = record.get("reference")
        hypothesis = hypotheses.get(item_id, "")
        rows.append(
            ProbeItem(
                item_id=item_id,
                start_s=start_s,
                end_s=end_s,
                reference=reference if isinstance(reference, str) else "",
                hypothesis=hypothesis if isinstance(hypothesis, str) else "",
            )
        )

    dictionary = ContextDictionaryStore(dictionary_dir).load("church")
    rules = tuple(dictionary.replacements)
    rule_pairs = tuple(build_rule_variant_pairs(rows, rules))
    signature_input = {
        "service": service,
        "answers": _file_fingerprint(answers_path, hash_contents=True),
        "hypotheses": _file_fingerprint(hypotheses_path, hash_contents=True),
        "wav": _file_fingerprint(wav_path, hash_contents=False),
        "dictionary": _file_fingerprint(dictionary_path, hash_contents=True),
        "model": _file_fingerprint(model_dir / "model.safetensors", hash_contents=False),
        "prompt": TEXT_PROMPT_NAME,
        "dims": [EMBEDDING_DIM, TRUNCATED_DIM],
        "device": device,
        "probe_pipeline_version": 1,
        "row_count": len(rows),
        "rule_pair_count": len(rule_pairs),
    }
    signature = hashlib.sha256(
        json.dumps(signature_input, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ServiceData(
        service=service,
        items=tuple(rows),
        wav_path=wav_path,
        rules=rules,
        rule_pairs=tuple(rule_pairs),
        source_signature=signature,
    )


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _cache_paths(cache_dir: Path, modality: str) -> tuple[Path, Path, Path]:
    return (
        cache_dir / f"{modality}-{EMBEDDING_DIM}.npy",
        cache_dir / f"{modality}-{TRUNCATED_DIM}.npy",
        cache_dir / f"{modality}.progress.json",
    )


def _load_progress(
    data: ServiceData,
    cache_dir: Path,
    modality: str,
    row_count: int,
) -> dict[str, Any] | None:
    path768, path256, progress_path = _cache_paths(cache_dir, modality)
    try:
        progress = _read_json(progress_path)
        if (
            progress.get("source_signature") != data.source_signature
            or progress.get("row_count") != row_count
            or not path768.is_file()
            or not path256.is_file()
        ):
            return None
        full = np.load(path768, mmap_mode="r")
        short = np.load(path256, mmap_mode="r")
        if full.shape != (row_count, EMBEDDING_DIM) or short.shape != (row_count, TRUNCATED_DIM):
            return None
        return progress
    except (OSError, ValueError, AttributeError, RuntimeError, json.JSONDecodeError):
        return None


def _new_cache(
    data: ServiceData,
    cache_dir: Path,
    modality: str,
    row_count: int,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path768, path256, progress_path = _cache_paths(cache_dir, modality)
    progress = _load_progress(data, cache_dir, modality, row_count)
    if progress is None:
        for path in (path768, path256, progress_path):
            path.unlink(missing_ok=True)
        full = np.lib.format.open_memmap(
            path768, mode="w+", dtype=np.float32, shape=(row_count, EMBEDDING_DIM)
        )
        short = np.lib.format.open_memmap(
            path256, mode="w+", dtype=np.float32, shape=(row_count, TRUNCATED_DIM)
        )
        full[:] = np.nan
        short[:] = np.nan
        progress = {
            "source_signature": data.source_signature,
            "row_count": row_count,
            "completed_rows": 0,
            "elapsed_seconds": 0.0,
            "timing_ms": [],
            "device": None,
            "dtype": None,
            "model_rss_mb": None,
        }
        full.flush()
        short.flush()
        _atomic_json(progress_path, progress)
        return full, short, progress
    return (
        np.lib.format.open_memmap(path768, mode="r+"),
        np.lib.format.open_memmap(path256, mode="r+"),
        progress,
    )


def _row_count(data: ServiceData, modality: str) -> int:
    return len(data.rule_pairs) if modality in {"rule_to", "rule_from"} else len(data.items)


def _text_rows(data: ServiceData, modality: str) -> list[str]:
    if modality == "reference":
        return [item.reference for item in data.items]
    if modality == "hypothesis":
        return [item.hypothesis for item in data.items]
    if modality == "rule_to":
        return [pair.to_text for pair in data.rule_pairs]
    if modality == "rule_from":
        return [pair.from_text for pair in data.rule_pairs]
    raise ValueError(f"not a text modality: {modality}")


def _current_rss_bytes() -> int:
    try:
        import psutil  # type: ignore[import-not-found]

        return int(psutil.Process().memory_info().rss)
    except ImportError:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform == "darwin" else peak * 1024)


class TransformersEncoder:
    def __init__(
        self,
        model: Any,
        tokenizer: Any,
        audio_feature_extractor: Any,
        torch: Any,
        device: str,
        dtype: Any,
        text_prompt: str,
    ) -> None:
        self.model = model
        self.tokenizer = tokenizer
        self.audio_feature_extractor = audio_feature_extractor
        self.torch = torch
        self.device = device
        self.dtype = dtype
        self.text_prompt = text_prompt
        self.audio_token = tokenizer.audio_token
        self.boa_token = tokenizer.boa_token
        self.eoa_token = tokenizer.eoa_token

    def _encode(self, texts: Sequence[str], audio_features: Any = None) -> np.ndarray:
        tokenized = self.tokenizer(
            list(texts),
            padding=True,
            truncation=True,
            return_tensors="pt",
        )
        model_inputs = {
            name: value.to(self.device) if hasattr(value, "to") else value
            for name, value in tokenized.items()
        }
        if audio_features is not None:
            model_inputs.update(
                {
                    name: value.to(
                        device=self.device,
                        dtype=self.dtype if name == "input_features" else None,
                    )
                    if name == "input_features"
                    else value.to(self.device)
                    for name, value in audio_features.items()
                }
            )
        with self.torch.inference_mode():
            outputs = self.model(**model_inputs, return_dict=True)
            hidden = outputs.last_hidden_state.float()
            attention_mask = model_inputs["attention_mask"].unsqueeze(-1).to(hidden.dtype)
            pooled = (hidden * attention_mask).sum(dim=1) / attention_mask.sum(dim=1).clamp_min(1.0)
            vectors = self.torch.nn.functional.normalize(pooled, p=2, dim=1)
        return vectors.cpu().numpy().astype(np.float32, copy=False)

    def encode_texts(self, texts: Sequence[str]) -> np.ndarray:
        return self._encode([self.text_prompt + text for text in texts])

    def encode_audio(self, samples: np.ndarray) -> np.ndarray:
        audio_features = self.audio_feature_extractor(
            [samples],
            sampling_rate=16000,
            return_tensors="pt",
        )
        mask = audio_features["input_features_mask"][0].cpu().numpy().astype(bool)
        for _ in range(2):
            output_length = (len(mask) + 2 - 3) // 2 + 1
            mask = mask[::2][:output_length]
        audio_token_count = int(mask.sum())
        if audio_token_count <= 0:
            raise RuntimeError("audio feature extractor returned no valid audio tokens")
        audio_prompt = f"{self.boa_token}{self.audio_token * audio_token_count}{self.eoa_token}"
        return self._encode([audio_prompt], audio_features=audio_features)


def load_encoder(model_dir: Path, device: str) -> tuple[Encoder, str, float]:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import torch

    if device == "mps" and not torch.backends.mps.is_available():
        raise RuntimeError("MPS is not available in this Python environment")
    dtype = torch.float32
    dtype_name = "float32"
    if device == "mps":
        try:
            probe = torch.ones((2, 2), dtype=torch.bfloat16, device="mps")
            _ = probe @ probe
        except (RuntimeError, TypeError):
            dtype = torch.float32
        else:
            dtype = torch.bfloat16
            dtype_name = "bfloat16"

    from transformers import AutoConfig, AutoFeatureExtractor, AutoModel, AutoTokenizer

    config = AutoConfig.from_pretrained(
        model_dir,
        local_files_only=True,
        vision_config=None,
    )
    tokenizer = AutoTokenizer.from_pretrained(model_dir, local_files_only=True)
    audio_feature_extractor = AutoFeatureExtractor.from_pretrained(
        model_dir,
        local_files_only=True,
    )
    sentence_transformer_config = _read_json(model_dir / "config_sentence_transformers.json")
    text_prompt = sentence_transformer_config.get("prompts", {}).get(TEXT_PROMPT_NAME)
    if not isinstance(text_prompt, str):
        raise KeyError(f"model checkpoint is missing the {TEXT_PROMPT_NAME} prompt")

    rss_before = _current_rss_bytes()
    model = AutoModel.from_pretrained(
        model_dir,
        config=config,
        local_files_only=True,
        torch_dtype=dtype,
    )
    model.to(device)
    model.eval()
    rss_after = _current_rss_bytes()
    model_rss_mb = max(0.0, (rss_after - rss_before) / (1024 * 1024))
    return (
        TransformersEncoder(
            model,
            tokenizer,
            audio_feature_extractor,
            torch,
            device,
            dtype,
            text_prompt,
        ),
        dtype_name,
        model_rss_mb,
    )


def _store_batch(
    full_cache: np.ndarray,
    short_cache: np.ndarray,
    row_indices: Sequence[int],
    vectors: np.ndarray,
) -> None:
    normalized_full = normalize_embeddings(vectors)
    normalized_short = normalize_embeddings(normalized_full[:, :TRUNCATED_DIM])
    full_cache[list(row_indices)] = normalized_full
    short_cache[list(row_indices)] = normalized_short


def _checkpoint(
    cache_dir: Path,
    modality: str,
    progress: dict[str, Any],
    full_cache: np.ndarray,
    short_cache: np.ndarray,
) -> None:
    full_cache.flush()  # type: ignore[attr-defined]
    short_cache.flush()  # type: ignore[attr-defined]
    _, _, progress_path = _cache_paths(cache_dir, modality)
    _atomic_json(progress_path, progress)


def embed_modality(
    data: ServiceData,
    cache_dir: Path,
    modality: str,
    encoder: Encoder,
    device: str,
    dtype: str,
    model_rss_mb: float,
    *,
    end_row: int,
    batch_size: int,
) -> dict[str, Any]:
    count = _row_count(data, modality)
    full_cache, short_cache, progress = _new_cache(data, cache_dir, modality, count)
    progress["device"] = device
    progress["dtype"] = dtype
    progress["model_rss_mb"] = model_rss_mb
    start_row = int(progress["completed_rows"])
    end_row = min(count, max(start_row, end_row))

    if modality == "audio":
        with wave.open(str(data.wav_path), "rb") as wav:
            if (
                wav.getnchannels() != 1
                or wav.getframerate() != 16000
                or wav.getsampwidth() != 2
            ):
                raise RuntimeError(f"expected 16 kHz mono PCM16 audio: {data.wav_path.name}")
            frame_count = wav.getnframes()
            for index in range(start_row, end_row):
                item = data.items[index]
                start_frame = max(0, min(frame_count, round(item.start_s * 16000)))
                end_frame = max(start_frame, min(frame_count, round(item.end_s * 16000)))
                wav.setpos(start_frame)
                pcm = wav.readframes(end_frame - start_frame)
                samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
                started = time.perf_counter()
                vector = encoder.encode_audio(samples)
                elapsed = time.perf_counter() - started
                vector = np.asarray(vector, dtype=np.float32).reshape(1, -1)
                if vector.shape[1] != EMBEDDING_DIM:
                    raise RuntimeError("audio encoder returned an unexpected dimension")
                _store_batch(full_cache, short_cache, [index], vector)
                progress["timing_ms"].append(elapsed * 1000.0)
                progress["elapsed_seconds"] += elapsed
                progress["completed_rows"] = index + 1
                _checkpoint(cache_dir, modality, progress, full_cache, short_cache)
                if (index + 1) % 10 == 0 or index + 1 == end_row:
                    print(f"{data.service} audio: {index + 1}/{count} items")
    else:
        values = _text_rows(data, modality)
        for batch_start in range(start_row, end_row, batch_size):
            batch_end = min(batch_start + batch_size, end_row)
            indices = [index for index in range(batch_start, batch_end) if values[index].strip()]
            if indices:
                started = time.perf_counter()
                vectors = np.asarray(
                    encoder.encode_texts([values[index] for index in indices]),
                    dtype=np.float32,
                )
                elapsed = time.perf_counter() - started
                if vectors.ndim == 1:
                    vectors = vectors.reshape(1, -1)
                if vectors.shape != (len(indices), EMBEDDING_DIM):
                    raise RuntimeError("text encoder returned an unexpected dimension")
                _store_batch(full_cache, short_cache, indices, vectors)
                progress["timing_ms"].extend([elapsed * 1000.0 / len(indices)] * len(indices))
                progress["elapsed_seconds"] += elapsed
            progress["completed_rows"] = batch_end
            _checkpoint(cache_dir, modality, progress, full_cache, short_cache)
            print(f"{data.service} {modality}: {batch_end}/{count} rows")

    timings = [float(value) for value in progress["timing_ms"]]
    return {
        "modality": modality,
        "count": len(timings),
        "mean_ms_per_item": float(np.mean(timings)) if timings else None,
        "p50_ms_per_item": float(np.percentile(timings, 50)) if timings else None,
        "p95_ms_per_item": float(np.percentile(timings, 95)) if timings else None,
        "elapsed_seconds": float(progress["elapsed_seconds"]),
        "completed_rows": int(progress["completed_rows"]),
    }


def _requested_row_end(data: ServiceData, modality: str, item_end: int) -> int:
    if modality not in {"rule_to", "rule_from"}:
        return min(item_end, len(data.items))
    return sum(pair.item_index < item_end for pair in data.rule_pairs)


def _cached_end(data: ServiceData, cache_dir: Path, modality: str) -> int:
    progress = _load_progress(data, cache_dir, modality, _row_count(data, modality))
    return int(progress["completed_rows"]) if progress else 0


def run_embed(args: argparse.Namespace) -> None:
    data = load_service_data(
        args.data_root,
        args.service,
        args.dictionary_dir,
        args.model_dir,
        args.device,
    )
    item_end = min(len(data.items), args.limit) if args.limit is not None else len(data.items)
    cache_dir = args.output_dir / "cache" / args.service
    requested = tuple(MODALITIES if "all" in args.modalities else args.modalities)
    if "all" in args.modalities and len(args.modalities) > 1:
        raise ValueError("--modalities all cannot be combined with individual names")
    for modality in requested:
        if _load_progress(data, cache_dir, modality, _row_count(data, modality)) is None:
            _new_cache(data, cache_dir, modality, _row_count(data, modality))
    pending = [
        modality
        for modality in requested
        if _cached_end(data, cache_dir, modality)
        < _requested_row_end(data, modality, item_end)
    ]
    if not pending:
        print(f"{args.service}: requested rows already cached")
        return

    encoder, dtype, model_rss_mb = load_encoder(args.model_dir, args.device)
    print(
        f"{args.service}: loaded local model ({args.device}, {dtype}); "
        f"model RSS increase {model_rss_mb:.1f} MB"
    )
    timing_rows: dict[str, Any] = {}
    for modality in pending:
        timing_rows[modality] = embed_modality(
            data,
            cache_dir,
            modality,
            encoder,
            args.device,
            dtype,
            model_rss_mb,
            end_row=_requested_row_end(data, modality, item_end),
            batch_size=args.batch_size,
        )
    timing_path = args.output_dir / "timings" / f"{args.service}.json"
    existing: dict[str, Any] = {}
    if timing_path.is_file():
        try:
            existing = _read_json(timing_path)
        except RuntimeError:
            existing = {}
    modalities = existing.get("modalities", {})
    modalities.update(timing_rows)
    _atomic_json(
        timing_path,
        {
            "service": args.service,
            "source_signature": data.source_signature,
            "device": args.device,
            "dtype": dtype,
            "model_rss_mb": model_rss_mb,
            "item_count": len(data.items),
            "rule_pair_count": len(data.rule_pairs),
            "modalities": modalities,
        },
    )
    print(f"{args.service}: embedding cache updated; no transcript strings were written")


def _load_embeddings(
    cache_dir: Path, modality: str, dimension: int, row_count: int
) -> np.ndarray:
    path = cache_dir / f"{modality}-{dimension}.npy"
    if not path.is_file():
        raise RuntimeError(f"missing embedding cache: {path.name}")
    matrix = np.load(path, mmap_mode="r")
    if matrix.shape != (row_count, dimension):
        raise RuntimeError(f"wrong embedding cache shape: {path.name}")
    return matrix


def _with_random_baseline(metrics: dict[str, Any]) -> dict[str, Any]:
    baseline = metrics["random_baseline"]
    return {
        "query_count": metrics["query_count"],
        "candidate_count": metrics["candidate_count"],
        "top1": metrics["top1"],
        "top5": metrics["top5"],
        "mrr": metrics["mrr"],
        "top1_count": metrics["top1_count"],
        "top5_count": metrics["top5_count"],
        "reciprocal_rank_sum": metrics["reciprocal_rank_sum"],
        "random_baseline": baseline,
    }


def _evaluate_retrieval(
    audio: np.ndarray,
    reference: np.ndarray,
    items: Sequence[ProbeItem],
    item_indices: Sequence[int],
) -> dict[str, Any]:
    candidate_indices = [
        index
        for index in item_indices
        if normalize_text(items[index].reference) and np.isfinite(reference[index]).all()
    ]
    query_indices = [
        index
        for index in item_indices
        if index in candidate_indices and np.isfinite(audio[index]).all()
    ]
    normalized_to_candidates: dict[str, list[int]] = {}
    for candidate_position, item_index in enumerate(candidate_indices):
        normalized_to_candidates.setdefault(normalize_text(items[item_index].reference), []).append(
            candidate_position
        )
    targets = [
        normalized_to_candidates[normalize_text(items[index].reference)] for index in query_indices
    ]
    if query_indices and candidate_indices:
        scores = cosine_matrix(audio[query_indices], reference[candidate_indices])
    else:
        scores = np.empty((len(query_indices), len(candidate_indices)), dtype=np.float32)
    overall = retrieval_metrics(scores, targets)
    result: dict[str, Any] = {"overall": _with_random_baseline(overall)}
    for key, predicate in (
        ("short_lt_4s", lambda item: item.duration_s < 4.0),
        ("long_gte_4s", lambda item: item.duration_s >= 4.0),
    ):
        selected = [index for index in query_indices if predicate(items[index])]
        selected_targets = [
            normalized_to_candidates[normalize_text(items[index].reference)] for index in selected
        ]
        subset_scores = (
            cosine_matrix(audio[selected], reference[candidate_indices])
            if selected and candidate_indices
            else np.empty((len(selected), len(candidate_indices)), dtype=np.float32)
        )
        result[key] = _with_random_baseline(retrieval_metrics(subset_scores, selected_targets))
    return result


def run_evaluate(args: argparse.Namespace) -> None:
    data = load_service_data(
        args.data_root,
        args.service,
        args.dictionary_dir,
        args.model_dir,
        args.device,
    )
    item_end = min(len(data.items), args.limit) if args.limit is not None else len(data.items)
    item_indices = list(range(item_end))
    cache_dir = args.output_dir / "cache" / args.service
    for modality in ("audio", "reference", "hypothesis", "rule_to", "rule_from"):
        requested_rows = _requested_row_end(data, modality, item_end)
        if _cached_end(data, cache_dir, modality) < requested_rows:
            raise RuntimeError(f"{modality} embeddings are incomplete; run embed first")

    audio_full = _load_embeddings(cache_dir, "audio", EMBEDDING_DIM, len(data.items))
    reference_full = _load_embeddings(cache_dir, "reference", EMBEDDING_DIM, len(data.items))
    hypothesis_full = _load_embeddings(cache_dir, "hypothesis", EMBEDDING_DIM, len(data.items))
    e1: dict[str, Any] = {}
    e2: dict[str, Any] = {}
    changed_items: list[int] = []
    edit_distances: dict[int, int] = {}
    buckets: dict[str, list[int]] = {"1_char": [], "2_3_chars": [], "4_plus_chars": []}
    for index in item_indices:
        item = data.items[index]
        ref_norm = normalize_text(item.reference)
        hyp_norm = normalize_text(item.hypothesis)
        if not ref_norm or not hyp_norm or ref_norm == hyp_norm:
            continue
        distance = edit_distance(ref_norm, hyp_norm)
        bucket = edit_distance_bucket(distance)
        if bucket is None:
            continue
        changed_items.append(index)
        edit_distances[index] = distance
        buckets[bucket].append(index)

    for dimension in (EMBEDDING_DIM, TRUNCATED_DIM):
        audio = audio_full if dimension == EMBEDDING_DIM else _load_embeddings(
            cache_dir, "audio", dimension, len(data.items)
        )
        reference = reference_full if dimension == EMBEDDING_DIM else _load_embeddings(
            cache_dir, "reference", dimension, len(data.items)
        )
        hypothesis = hypothesis_full if dimension == EMBEDDING_DIM else _load_embeddings(
            cache_dir, "hypothesis", dimension, len(data.items)
        )
        e1[str(dimension)] = _evaluate_retrieval(audio, reference, data.items, item_indices)
        valid_pair_indices = [
            index
            for index in changed_items
            if np.isfinite(audio[index]).all()
            and np.isfinite(reference[index]).all()
            and np.isfinite(hypothesis[index]).all()
        ]
        e2_dim: dict[str, Any] = {
            "overall": pairwise_accuracy(
                audio[valid_pair_indices],
                reference[valid_pair_indices],
                hypothesis[valid_pair_indices],
            )
        }
        for bucket, indices in buckets.items():
            valid_indices = [index for index in indices if index in valid_pair_indices]
            e2_dim[bucket] = pairwise_accuracy(
                audio[valid_indices], reference[valid_indices], hypothesis[valid_indices]
            )

        selected_rule_pairs = [
            (row, pair)
            for row, pair in enumerate(data.rule_pairs)
            if pair.item_index < item_end
        ]
        rule_to = _load_embeddings(cache_dir, "rule_to", dimension, len(data.rule_pairs))
        rule_from = _load_embeddings(cache_dir, "rule_from", dimension, len(data.rule_pairs))
        selected_rule_rows = [
            row
            for row, pair in selected_rule_pairs
            if np.isfinite(audio[pair.item_index]).all()
            and np.isfinite(rule_to[row]).all()
            and np.isfinite(rule_from[row]).all()
        ]
        selected_rule_items = [data.rule_pairs[row].item_index for row in selected_rule_rows]
        e2_dim["dictionary_rule_variants"] = {
            **pairwise_accuracy(
                audio[selected_rule_items], rule_to[selected_rule_rows], rule_from[selected_rule_rows]
            ),
            "item_count": len(set(selected_rule_items)),
        }
        e2[str(dimension)] = e2_dim

    timing_path = args.output_dir / "timings" / f"{args.service}.json"
    timing_payload = _read_json(timing_path) if timing_path.is_file() else {}
    payload = {
        "service": args.service,
        "source_signature": data.source_signature,
        "device": timing_payload.get("device", args.device),
        "dtype": timing_payload.get("dtype"),
        "item_count": item_end,
        "full_service_usable_items": len(data.items),
        "dictionary_rule_count": len(data.rules),
        "dictionary_rule_pair_count": sum(pair.item_index < item_end for pair in data.rule_pairs),
        "changed_hypothesis_count": len(changed_items),
        "edit_distance_bucket_counts": {key: len(value) for key, value in buckets.items()},
        "e1_retrieval": e1,
        "e2_pairwise": e2,
        "e3_timing": {
            "model_rss_mb": timing_payload.get("model_rss_mb"),
            "modalities": timing_payload.get("modalities", {}),
        },
    }
    results_path = args.output_dir / "results" / f"{args.service}.json"
    _atomic_json(results_path, payload)
    print(
        f"{args.service}: evaluated {item_end} rows; "
        f"E2 mismatches={len(changed_items)}, dictionary-rule pairs="
        f"{payload['dictionary_rule_pair_count']}"
    )
    print(f"metrics: {results_path}")


def _aggregate_retrieval(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    rows = [row for row in rows if int(row["query_count"]) > 0]
    query_count = sum(int(row["query_count"]) for row in rows)
    candidate_counts = [int(row["candidate_count"]) for row in rows]
    if not query_count:
        return {
            "query_count": 0,
            "top1": None,
            "top5": None,
            "mrr": None,
            "random_baseline": {"top1": None, "top5": None, "mrr": None},
        }
    return {
        "query_count": query_count,
        "candidate_count_range": [min(candidate_counts), max(candidate_counts)],
        "top1": sum(int(row["top1_count"]) for row in rows) / query_count,
        "top5": sum(int(row["top5_count"]) for row in rows) / query_count,
        "mrr": sum(float(row["reciprocal_rank_sum"]) for row in rows) / query_count,
        "random_baseline": {
            key: sum(
                float(row["random_baseline"][key]) * int(row["query_count"]) for row in rows
            )
            / query_count
            for key in ("top1", "top5", "mrr")
        },
    }


def _aggregate_pairwise(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    rows = [row for row in rows if int(row["count"]) > 0]
    count = sum(int(row["count"]) for row in rows)
    if not count:
        return {"count": 0, "wins": 0, "ties": 0, "accuracy": None, "mean_margin": None}
    return {
        "count": count,
        "wins": sum(int(row["wins"]) for row in rows),
        "ties": sum(int(row["ties"]) for row in rows),
        "accuracy": sum(int(row["wins"]) for row in rows) / count,
        "mean_margin": sum(float(row["mean_margin"]) * int(row["count"]) for row in rows) / count,
    }


def aggregate_results(payloads: Sequence[dict[str, Any]]) -> dict[str, Any]:
    aggregate: dict[str, Any] = {
        "service_count": len(payloads),
        "item_count": sum(int(payload["item_count"]) for payload in payloads),
        "e1_retrieval": {},
        "e2_pairwise": {},
    }
    for dimension in (str(EMBEDDING_DIM), str(TRUNCATED_DIM)):
        aggregate["e1_retrieval"][dimension] = {}
        for split in ("overall", "short_lt_4s", "long_gte_4s"):
            aggregate["e1_retrieval"][dimension][split] = _aggregate_retrieval(
                [payload["e1_retrieval"][dimension][split] for payload in payloads]
            )
        aggregate["e2_pairwise"][dimension] = {}
        for bucket in (
            "overall",
            "1_char",
            "2_3_chars",
            "4_plus_chars",
            "dictionary_rule_variants",
        ):
            aggregate["e2_pairwise"][dimension][bucket] = _aggregate_pairwise(
                [payload["e2_pairwise"][dimension][bucket] for payload in payloads]
            )
    return aggregate


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def _number(value: float | None, places: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, int):
        return str(value)
    return f"{value:.{places}f}"


def render_report(payloads: Sequence[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    aggregate = aggregate_results(payloads)
    lines = [
        "# EmbeddingGemma 2 audio↔text probe",
        "",
        f"Generated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "EmbeddingGemma 2 returns vectors; it does not generate a transcript. This report tests closed-set retrieval and similarity-based error discrimination.",
        "The local Transformers checkpoint was loaded offline with the vision encoder disabled. Text was passed as displayed with the model's `SentenceSimilarity` prompt; audio was extracted at 16 kHz mono and wrapped in its model audio tokens. The shared transformer's last hidden states were mean-pooled by attention mask and L2-normalized, matching the checkpoint's SentenceTransformers pooling modules. CPU uses float32 and MPS uses bfloat16 when supported, otherwise float32. No fp16 was used.",
        "",
        "## Dataset coverage",
        "",
        "| Service | Usable items | Mismatching hypotheses | Dictionary-rule pairs | Device | Model RSS increase (MB) |",
        "|---|---:|---:|---:|---|---:|",
    ]
    for payload in payloads:
        lines.append(
            "| {service} | {items} | {mismatches} | {rules} | {device} | {rss} |".format(
                service=payload["service"],
                items=payload["item_count"],
                mismatches=payload["changed_hypothesis_count"],
                rules=payload["dictionary_rule_pair_count"],
                device=payload.get("device", "unknown"),
                rss=_number(payload.get("e3_timing", {}).get("model_rss_mb"), 1),
            )
        )

    lines.extend(
        [
            "",
            "## E1 · Closed-set retrieval",
            "",
            "Each audio item ranks the usable human references from the same service. The random baseline accounts for duplicate normalized references.",
            "",
            "| Dim | Split | Queries | Candidates | Top-1 | Random | Top-5 | Random | MRR | Random |",
            "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for dimension in (str(EMBEDDING_DIM), str(TRUNCATED_DIM)):
        for split in ("overall", "short_lt_4s", "long_gte_4s"):
            row = aggregate["e1_retrieval"][dimension][split]
            baseline = row["random_baseline"]
            lines.append(
                f"| {dimension} | {split} | {row['query_count']} | "
                f"{row.get('candidate_count_range', ['n/a', 'n/a'])} | "
                f"{_percent(row['top1'])} | {_percent(baseline['top1'])} | "
                f"{_percent(row['top5'])} | {_percent(baseline['top5'])} | "
                f"{_number(row['mrr'])} | {_number(baseline['mrr'])} |"
            )

    lines.extend(
        [
            "",
            "## E2 · Pairwise discrimination",
            "",
            "Accuracy is the strict fraction where cosine(audio, preferred text) > cosine(audio, alternative text); ties count as misses.",
            "",
            "| Dim | Mismatch group | Pairs | Wins | Ties | Accuracy |",
            "|---:|---|---:|---:|---:|---:|",
        ]
    )
    for dimension in (str(EMBEDDING_DIM), str(TRUNCATED_DIM)):
        for bucket in (
            "overall",
            "1_char",
            "2_3_chars",
            "4_plus_chars",
            "dictionary_rule_variants",
        ):
            row = aggregate["e2_pairwise"][dimension][bucket]
            lines.append(
                f"| {dimension} | {bucket} | {row['count']} | {row['wins']} | "
                f"{row['ties']} | {_percent(row['accuracy'])} |"
            )

    lines.extend(
        [
            "",
            "## E3 · Cost",
            "",
            "| Service | Device | Audio mean (ms/item) | Audio p95 (ms/item) | Text mean (ms/item) | Model RSS increase (MB) |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for payload in payloads:
        modalities = payload.get("e3_timing", {}).get("modalities", {})
        audio = modalities.get("audio", {})
        text_rows = [modalities.get(name, {}) for name in ("reference", "hypothesis", "rule_to", "rule_from")]
        text_count = sum(int(row.get("count", 0)) for row in text_rows)
        text_elapsed = sum(float(row.get("elapsed_seconds", 0.0)) for row in text_rows)
        text_ms = text_elapsed * 1000.0 / text_count if text_count else None
        lines.append(
            f"| {payload['service']} | {payload.get('device', 'unknown')} | "
            f"{_number(audio.get('mean_ms_per_item'), 1)} | "
            f"{_number(audio.get('p95_ms_per_item'), 1)} | {_number(text_ms, 1)} | "
            f"{_number(payload.get('e3_timing', {}).get('model_rss_mb'), 1)} |"
        )

    lines.extend(
        [
            "",
            "## Verdict",
            "",
            "Pending the complete MPS run. Treat this model as an embedding and retrieval component, not a transcript generator. Consider live rescoring only if the full-service 1–3 character and dictionary-rule pairwise accuracy is materially above chance with adequate sample counts.",
            "",
            "## What this can and cannot do for live subtitles",
            "",
            "- **Cannot replace ASR:** an embedding maps audio to a fixed vector and cannot emit open-vocabulary words or a transcript.",
            "- **Possible closed-set use:** detect a known scripture reading or hymn by ranking a curated reference list. Follow-up: evaluate held-out services; require top-1 ≥80% and at least 5× the random top-1 baseline before a live canary.",
            "- **Possible error gate:** compare an ASR candidate with a reference-like correction. Follow-up: require ≥70% pairwise accuracy on 1–3 character and dictionary-rule pairs, with at least 50 pairs per group, before testing any live rescore path.",
            "- **Possible audio classifier:** classify speech, singing, and silence from audio vectors. Follow-up: label a held-out set and require balanced accuracy ≥85% plus per-class recall ≥75%.",
            "- **Otherwise:** no live use; keep the model offline for this service.",
            "",
            "All outputs are aggregate metrics. No reference, hypothesis, item ID, or audio content is included in this report.",
            "",
        ]
    )
    return aggregate, "\n".join(lines)


def run_report(args: argparse.Namespace) -> None:
    payloads: list[dict[str, Any]] = []
    for service in args.services:
        path = args.output_dir / "results" / f"{service}.json"
        if not path.is_file():
            raise RuntimeError(f"missing metrics for service {service}; run evaluate first")
        payloads.append(_read_json(path))
    aggregate, report = render_report(payloads)
    _atomic_json(args.output_dir / "summary.json", aggregate)
    report_path = args.output_dir / "report.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"aggregate report: {report_path}")


def _default_output_dir() -> Path:
    data_root = Path(
        os.environ.get("EMBEDDING_PROBE_DATA_ROOT", ROOT / ".soak/church-eval/work")
    ).expanduser()
    return Path(os.environ.get("EMBEDDING_PROBE_OUTPUT", str(data_root / "embedding-probe"))).expanduser()


def _add_shared_data_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--dictionary-dir", type=Path, default=DEFAULT_DICTIONARY_DIR)
    parser.add_argument("--output-dir", type=Path, default=_default_output_dir())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    embed = commands.add_parser("embed", help="create or resume local .npy embedding caches")
    _add_shared_data_args(embed)
    embed.add_argument("--service", choices=SERVICES, required=True)
    embed.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    embed.add_argument("--device", choices=("cpu", "mps"), default="cpu")
    embed.add_argument("--limit", type=int)
    embed.add_argument("--batch-size", type=int, default=16)
    embed.add_argument(
        "--modalities",
        nargs="+",
        choices=("all", *MODALITIES),
        default=("all",),
    )
    embed.set_defaults(func=run_embed)

    evaluate = commands.add_parser("evaluate", help="calculate E1/E2 from cached vectors")
    _add_shared_data_args(evaluate)
    evaluate.add_argument("--service", choices=SERVICES, required=True)
    evaluate.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    evaluate.add_argument("--device", choices=("cpu", "mps"), default="cpu")
    evaluate.add_argument("--limit", type=int)
    evaluate.set_defaults(func=run_evaluate)

    report = commands.add_parser("report", help="aggregate service metrics into Markdown and JSON")
    report.add_argument("--output-dir", type=Path, default=_default_output_dir())
    report.add_argument("--services", nargs="+", choices=SERVICES, default=SERVICES)
    report.set_defaults(func=run_report)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "limit", None) is not None and args.limit <= 0:
        parser.error("--limit must be greater than zero")
    if getattr(args, "batch_size", 1) <= 0:
        parser.error("--batch-size must be greater than zero")
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
