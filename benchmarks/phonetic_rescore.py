"""Offline experiment: phonetic candidate generation + ASR-model rescoring.

Like an input method: find spans of a hypothesis that sound like a known term (dictionary
``to`` values, hotwords, names), build candidate transcripts with the span replaced, and let
the ASR model itself pick, by teacher-forced log-likelihood against the SAME audio.

This module is a benchmark only; the live server never imports it.

Stages (see docs/09-testing-guide.md for the exact commands)::

    score     decode nothing; generate candidates for each item of a service and score every
              distinct transcript with the model (private cache, never committed)
    tune      cross-validate decision parameters on the cached scores
    time      time the full pipeline (generation + model scoring) per item
    report    write the metrics-only Markdown report

Pure pieces (pinyin matching, decision rule, metrics) have no MLX dependency and are
unit-tested; ``pypinyin`` is required (not a project dependency).
"""

# ruff: noqa: ISC004
from __future__ import annotations

import argparse
import itertools
import json
import re
import sys
import time
import tomllib
from collections import Counter
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from benchmarks.cer_eval import edit_counts, normalize_for_scoring

SAMPLE_RATE = 16_000
LANGUAGE = "Chinese"
HAN_RE = re.compile(r"[㐀-鿿]+")
GAP_COST = 0.8
WEIGHT_INITIAL, WEIGHT_FINAL, WEIGHT_TONE = 0.40, 0.45, 0.15
CANDIDATE_CAP = 8
GENERATION_COST_MAX = 0.35  # liberal; the decision rule thresholds tighter

# Initial confusions common in Taiwanese Mandarin (retroflex/dental, n/l, f/h, m/n, j/zh ...).
_INITIAL_PAIRS: dict[frozenset[str], float] = {
    frozenset(pair): cost
    for pairs, cost in (
        ((("zh", "z"), ("ch", "c"), ("sh", "s"), ("n", "l"), ("l", "r"), ("f", "h")), 0.30),
        ((("m", "n"), ("j", "zh"), ("q", "ch"), ("x", "sh"), ("j", "z"), ("q", "c"), ("x", "s")), 0.40),
        ((("w", ""), ("y", ""), ("w", "y")), 0.15),
        ((("b", "p"), ("d", "t"), ("g", "k"), ("r", "z"), ("zh", "ch"), ("z", "c")), 0.50),
    )
    for pair in pairs
}
_FINAL_PAIRS: dict[frozenset[str], float] = {
    frozenset(pair): 0.35
    for pair in (("e", "o"), ("uo", "o"), ("ei", "ie"), ("ai", "ei"), ("ou", "u"), ("ong", "eng"),
                 ("un", "uen"), ("in", "ing"), ("ian", "ie"), ("uan", "an"), ("ue", "ve"))
}


@dataclass(frozen=True, slots=True)
class Syllable:
    initial: str
    final: str
    tone: str


def _nucleus(final: str) -> str:
    stripped = final.rstrip("ng") if final.endswith(("n", "ng")) else final
    return stripped.lstrip("iuv") or stripped


def initial_cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    return _INITIAL_PAIRS.get(frozenset((a, b)), 1.0)


def final_cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    if a.removesuffix("g") == b.removesuffix("g") and a.endswith(("n", "ng")):
        return 0.30  # en/eng, an/ang, in/ing ...
    pair = _FINAL_PAIRS.get(frozenset((a, b)))
    if pair is not None:
        return pair
    if _nucleus(a) and _nucleus(a)[0] == _nucleus(b)[:1]:
        return 0.65
    return 1.0


def syllable_cost(a: Syllable, b: Syllable, *, tones: bool = True) -> float:
    cost = WEIGHT_INITIAL * initial_cost(a.initial, b.initial)
    cost += WEIGHT_FINAL * final_cost(a.final, b.final)
    if tones and a.tone != b.tone:
        cost += WEIGHT_TONE
    return cost


@lru_cache(maxsize=65_536)
def han_run_syllables(run: str) -> tuple[Syllable, ...]:
    """Pinyin (initial, final, tone) for a run of Han characters, one per character."""
    from pypinyin import Style, lazy_pinyin

    tone3 = lazy_pinyin(run, style=Style.TONE3, neutral_tone_with_five=True)
    initials = lazy_pinyin(run, style=Style.INITIALS, strict=False)
    finals = lazy_pinyin(run, style=Style.FINALS, strict=False)
    if not (len(tone3) == len(initials) == len(finals) == len(run)):
        raise ValueError(f"pypinyin returned an unexpected length for a Han run of {len(run)}")
    syllables = []
    for toned, initial, final in zip(tone3, initials, finals, strict=True):
        tone = toned[-1] if toned and toned[-1].isdigit() else "5"
        syllables.append(Syllable(initial, final, tone))
    return tuple(syllables)


@dataclass(frozen=True, slots=True)
class Term:
    text: str
    source: str  # "dict" | "hot" | "name"
    syllables: tuple[Syllable, ...]


def make_terms(entries: list[tuple[str, str]], *, min_len: int = 2) -> list[Term]:
    """Han-only terms of at least ``min_len`` characters; de-duplicated, first source wins."""
    seen: set[str] = set()
    terms: list[Term] = []
    for text, source in entries:
        text = text.strip()
        if text in seen or len(text) < min_len or not HAN_RE.fullmatch(text):
            continue
        seen.add(text)
        terms.append(Term(text, source, han_run_syllables(text)))
    return terms


@dataclass(frozen=True, slots=True)
class Candidate:
    start: int  # character offsets into the hypothesis string
    end: int
    term: str
    source: str
    cost: float  # normalised syllable edit cost with tones (0 = identical sound)
    cost_toneless: float
    text: str  # the whole candidate transcript

    def as_dict(self) -> dict[str, Any]:
        return {
            "s": self.start, "e": self.end, "term": self.term, "src": self.source,
            "cost": round(self.cost, 4), "cost_tl": round(self.cost_toneless, 4), "text": self.text,
        }


def _alignment_cost(
    term: tuple[Syllable, ...], window: tuple[Syllable, ...], *, tones: bool
) -> float:
    rows, cols = len(term), len(window)
    previous = [j * GAP_COST for j in range(cols + 1)]
    for i in range(1, rows + 1):
        current = [i * GAP_COST] + [0.0] * cols
        for j in range(1, cols + 1):
            current[j] = min(
                previous[j - 1] + syllable_cost(term[i - 1], window[j - 1], tones=tones),
                previous[j] + GAP_COST,
                current[j - 1] + GAP_COST,
            )
        previous = current
    return previous[cols]


def find_candidates(
    hypothesis: str,
    terms: list[Term],
    *,
    cost_max: float = GENERATION_COST_MAX,
    cap: int = CANDIDATE_CAP,
) -> list[Candidate]:
    """Windows of ``hypothesis`` whose syllables resemble a term (length within +-1).

    Approximate substring matching (free start) with syllable-level edit distance, per
    Han run so a window never spans punctuation or Latin text.  Windows that already spell
    the term are skipped.  Returns the best ``cap`` by tonal cost.
    """
    found: dict[tuple[int, int, str], Candidate] = {}
    for run_match in HAN_RE.finditer(hypothesis):
        run, base = run_match.group(), run_match.start()
        syllables = han_run_syllables(run)
        n = len(run)
        for term in terms:
            m = len(term.syllables)
            if n < m - 1:
                continue
            spelled = [(hit.start(), hit.end()) for hit in re.finditer(re.escape(term.text), run)]
            # D[i][j]: cost of aligning term[:i] to a window of run ending at j (free start).
            cost_row = [[0.0] * (n + 1)] + [[i * GAP_COST] + [0.0] * n for i in range(1, m + 1)]
            start_row = [list(range(n + 1))] + [[0] * (n + 1) for _ in range(m)]
            for i in range(1, m + 1):
                for j in range(1, n + 1):
                    options = (
                        (cost_row[i - 1][j - 1] + syllable_cost(term.syllables[i - 1], syllables[j - 1]),
                         start_row[i - 1][j - 1]),
                        (cost_row[i - 1][j] + GAP_COST, start_row[i - 1][j]),
                        (cost_row[i][j - 1] + GAP_COST, start_row[i][j - 1]),
                    )
                    cost_row[i][j], start_row[i][j] = min(options, key=lambda option: option[0])
                start_row[i][0] = 0
            for end in range(1, n + 1):
                cost = cost_row[m][end] / m
                if cost > cost_max:
                    continue
                start = start_row[m][end]
                width = end - start
                if abs(width - m) > 1 or width < 1 or any(
                    start < spelled_end and end > spelled_start for spelled_start, spelled_end in spelled
                ):
                    continue  # windows touching an existing spelling of the term are not candidates
                window = syllables[start:end]
                toneless = _alignment_cost(term.syllables, window, tones=False) / m
                key = (base + start, base + end, term.text)
                candidate_text = hypothesis[: base + start] + term.text + hypothesis[base + end :]
                if candidate_text == hypothesis:
                    continue
                candidate = Candidate(base + start, base + end, term.text, term.source, cost,
                                      toneless, candidate_text)
                if key not in found or found[key].cost > cost:
                    found[key] = candidate
    ordered = sorted(found.values(), key=lambda c: (c.cost, c.start, c.term))
    # Same window + same resulting text from different terms cannot happen; drop duplicate texts.
    unique: dict[str, Candidate] = {}
    for candidate in ordered:
        unique.setdefault(candidate.text, candidate)
    return list(unique.values())[:cap]


# --------------------------------------------------------------------------------------
# Decision rule and application
# --------------------------------------------------------------------------------------

NORMS = ("sum", "tok", "char")


@dataclass(frozen=True, slots=True)
class Params:
    margin: float
    norm: str = "sum"
    cost_max: float = 0.30
    toneless: bool = False
    min_term_len: int = 2
    names: bool = False  # accept candidates whose term came from the "name" source


def _norm_score(record: list[float], text: str, norm: str) -> float:
    log_prob, tokens = record
    if norm == "sum":
        return log_prob
    if norm == "tok":
        return log_prob / max(tokens, 1)
    return log_prob / max(len(text), 1)


def accepted_candidates(
    base_text: str,
    candidates: list[dict[str, Any]],
    scores: dict[str, list[float]],
    params: Params,
) -> list[dict[str, Any]]:
    """Candidates whose score beats the original by the margin, non-overlapping, best first."""
    if base_text not in scores:
        return []
    original = _norm_score(scores[base_text], base_text, params.norm)
    scored: list[tuple[float, dict[str, Any]]] = []
    for candidate in candidates:
        cost = candidate["cost_tl"] if params.toneless else candidate["cost"]
        if cost > params.cost_max or len(candidate["term"]) < params.min_term_len:
            continue
        if candidate["src"] == "name" and not params.names:
            continue
        record = scores.get(candidate["text"])
        if record is None:
            continue
        gain = _norm_score(record, candidate["text"], params.norm) - original
        if gain > params.margin:
            scored.append((gain, candidate))
    scored.sort(key=lambda pair: -pair[0])
    chosen: list[dict[str, Any]] = []
    for gain, candidate in scored:
        if all(candidate["e"] <= other["s"] or candidate["s"] >= other["e"] for other in chosen):
            chosen.append({**candidate, "gain": gain})
    return chosen


def apply_accepted(base_text: str, accepted: list[dict[str, Any]]) -> str:
    text = base_text
    for candidate in sorted(accepted, key=lambda c: -c["s"]):
        text = text[: candidate["s"]] + candidate["term"] + text[candidate["e"] :]
    return text


# --------------------------------------------------------------------------------------
# Metrics helpers (no private text is ever written by these)
# --------------------------------------------------------------------------------------


def normalised_chars(text: str) -> tuple[str, list[int]]:
    """Scoring-normalised text (pronouns folded) and, per raw character, its normalised index."""
    kept: list[str] = []
    raw_to_norm: list[int] = []
    for char in text:
        piece = normalize_for_scoring(char, fold_pronouns=True)
        raw_to_norm.append(len(kept))
        kept.extend(piece)
    return "".join(kept), raw_to_norm


def matched_hypothesis_chars(reference: str, hypothesis: str) -> list[bool]:
    """For each hypothesis character, whether Levenshtein alignment matches it to the reference."""
    n, m = len(reference), len(hypothesis)
    cost = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        cost[i][0] = i
    for j in range(m + 1):
        cost[0][j] = j
    for i, j in itertools.product(range(1, n + 1), range(1, m + 1)):
        cost[i][j] = min(
            cost[i - 1][j - 1] + (reference[i - 1] != hypothesis[j - 1]),
            cost[i - 1][j] + 1,
            cost[i][j - 1] + 1,
        )
    matched = [False] * m
    i, j = n, m
    while i and j:
        if cost[i][j] == cost[i - 1][j - 1] + (reference[i - 1] != hypothesis[j - 1]):
            matched[j - 1] = reference[i - 1] == hypothesis[j - 1]
            i -= 1
            j -= 1
        elif cost[i][j] == cost[i - 1][j] + 1:
            i -= 1
        else:
            j -= 1
    return matched


def errors_of(reference: str, text: str) -> int:
    return edit_counts(
        normalize_for_scoring(reference, fold_pronouns=True),
        normalize_for_scoring(text, fold_pronouns=True),
    ).errors


def classify_change(reference: str, base_text: str, change: dict[str, Any]) -> str:
    """'false' if the replaced window was already correct, else 'good'/'neutral'/'worse'.

    'false' means the original window's characters all aligned to the reference and the
    replacement spells something else (a correct span was turned wrong).
    """
    ref_norm, _ = normalised_chars(reference)
    base_norm, raw_to_norm = normalised_chars(base_text)
    matched = matched_hypothesis_chars(ref_norm, base_norm)
    window = range(raw_to_norm[change["s"]], raw_to_norm[change["e"] - 1] + 1)
    replacement = normalise_term(change["term"])
    original = base_norm[window.start : window.stop]
    if replacement != original and all(matched[k] for k in window):
        return "false"
    after = apply_accepted(base_text, [change])
    delta = errors_of(reference, after) - errors_of(reference, base_text)
    return "good" if delta < 0 else "worse" if delta > 0 else "neutral"


def normalise_term(term: str) -> str:
    return normalized_text(term)


def normalized_text(text: str) -> str:
    return normalised_chars(text)[0]


def term_recall(
    references: list[str], outputs: list[str], terms: list[str]
) -> tuple[int, int]:
    """(correct, total) occurrences of target terms in the references found in the outputs."""
    correct = total = 0
    folded_terms = [t for t in dict.fromkeys(normalized_text(t) for t in terms) if t]
    for reference, output in zip(references, outputs, strict=True):
        ref_norm, out_norm = normalized_text(reference), normalized_text(output)
        for term in folded_terms:
            count = ref_norm.count(term)
            if count:
                total += count
                correct += min(count, out_norm.count(term))
    return correct, total


# --------------------------------------------------------------------------------------
# Term lists
# --------------------------------------------------------------------------------------

TITLE_RE = re.compile(r"([㐀-鿿]{2,3})(哥|姐|姊|牧師|執事|老師|弟兄)")
NAME_STOP = frozenset("這那哪每一二三四五六七八九十個的了是我你他她祢祂它們有在和跟與或")


def mine_names(references: list[str], *, min_count: int = 3) -> list[str]:
    """Names with a title (e.g. XX哥) frequent in training references only.

    The 2-3 characters before a title are counted; a 3-character form is dropped when its
    last two characters carry (almost) the same count, since the extra leading character is
    then ordinary context rather than part of the name.
    """
    counts: Counter[str] = Counter()
    for reference in references:
        text = normalize_for_scoring(reference)
        for title in ("哥", "姐", "姊", "牧師", "執事", "老師", "弟兄"):
            for match in re.finditer(r"(?=([\u3400-\u9fff]{2,3})" + title + ")", text):
                name = match.group(1)
                if any(char in NAME_STOP for char in name):
                    continue
                counts[name + title] += 1
                counts[name[-2:] + title] += 1 if len(name) == 3 else 0
    kept = {name for name, count in counts.items() if count >= min_count}
    for name in list(kept):
        title = next(t for t in ("牧師", "執事", "老師", "弟兄", "哥", "姐", "姊") if name.endswith(t))
        stem = name[: -len(title)]
        if len(stem) == 3 and counts[stem[-2:] + title] >= 0.9 * counts[name]:
            kept.discard(name)
    return sorted(kept, key=lambda name: -counts[name])


def load_dictionary(path: Path) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[str]]:
    """(replacements as (from, to), hotword/to term entries, hotwords) from a dictionary TOML."""
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    replacements = [(r["from"], r["to"]) for r in document.get("replacements", [])]
    hotwords = list(document.get("hotwords", []))
    entries = [(to, "dict") for _, to in replacements] + [(word, "hot") for word in hotwords]
    return replacements, entries, hotwords


def audio_duration_hours(items: list[dict[str, Any]]) -> float:
    return sum(item["end_s"] - item["start_s"] for item in items) / 3600.0


# --------------------------------------------------------------------------------------
# Teacher-forced scoring with the Qwen3-ASR model (mlx-audio 0.4.5)
# --------------------------------------------------------------------------------------


class TeacherForcedScorer:
    """log p(text | audio) with the same prompt ``generate()`` builds, by teacher forcing.

    mlx-audio 0.4.5 ``qwen3_asr.py`` lines used (venv copy):

    * ``_preprocess_audio`` 866-897: log-mel features and the audio-token count.
    * ``_build_prompt`` 911-941: the chat prompt; with ``language="Chinese"`` the assistant
      turn is pre-filled with ``language Chinese<asr_text>`` (``backend.py`` passes this).
    * ``get_audio_features`` 639 / ``_build_inputs_embeds`` 647-681: audio encoder and the
      merge of audio embeddings into the ``<|audio_pad|>`` slots (what ``stream_generate``
      does at 987-1000).
    * ``make_cache`` 763-767 and ``TextModel.__call__`` 600-618: KV-cached decoder; the
      ``lm_head`` is tied to ``embed_tokens`` (``as_linear``, 706-711 / 689-694).

    The prefix (audio + prompt) is prefilled once; all candidate transcripts are then run as
    ONE batch over a broadcast copy of that KV cache.  Each text is followed by ``<|im_end|>``
    (``generate()`` stops on it), so a candidate that is too long or short pays for it.
    """

    def __init__(self, model: Any) -> None:
        import mlx.core as mx

        self._mx = mx
        self.model = model
        self.tokenizer = model._tokenizer
        self.eos_id = int(self.tokenizer.convert_tokens_to_ids("<|im_end|>"))
        self._token_cache: dict[str, list[int]] = {}

    def tokens(self, text: str) -> list[int]:
        cached = self._token_cache.get(text)
        if cached is None:
            encoded = self.tokenizer.encode(text)
            cached = [int(t) for t in (encoded.tolist() if hasattr(encoded, "tolist") else encoded)]
            if cached and isinstance(cached[0], list):
                cached = cached[0]
            self._token_cache[text] = cached
        return cached

    def prefix(self, audio: Any) -> Any:
        """Encode ``audio`` and prefill the prompt.  Returns (cache, first-token log-probs)."""
        import numpy as np

        mx, model = self._mx, self.model
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if samples.size < SAMPLE_RATE:  # same minimum-length padding as split_audio_into_chunks
            samples = np.pad(samples, (0, SAMPLE_RATE - samples.size))
        features, mask, audio_tokens = model._preprocess_audio(samples)
        input_ids = model._build_prompt(audio_tokens, LANGUAGE, None)
        audio_features = model.get_audio_features(features, mask)
        embeds = model._build_inputs_embeds(input_ids, audio_features)
        cache = model.make_cache()
        hidden = model.model(inputs_embeds=embeds, cache=cache)[:, -1:, :]
        logits = self._logits(hidden)[0, 0].astype(mx.float32)
        first = logits - mx.logsumexp(logits)
        mx.eval(first, [c.state for c in cache])
        return cache, first

    def _logits(self, hidden: Any) -> Any:
        model = self.model
        if model.lm_head is not None:
            return model.lm_head(hidden)
        return model.model.embed_tokens.as_linear(hidden)

    def score(self, prefix: Any, texts: list[str]) -> list[tuple[float, int]]:
        """(sum of token log-probs incl. end token, token count) for each text."""
        import numpy as np
        from mlx_lm.models.cache import KVCache

        mx, model = self._mx, self.model
        cache, first = prefix
        targets = [self.tokens(text) + [self.eos_id] for text in texts]
        batch = len(targets)
        width = max(len(t) for t in targets)
        first_np = np.array(first)
        sums = np.array([first_np[t[0]] for t in targets], dtype=np.float64)
        if width > 1:
            padded = np.zeros((batch, width - 1), dtype=np.int32)
            for row, target in enumerate(targets):
                padded[row, : len(target) - 1] = target[:-1]
            tiled = []
            for layer_cache in cache:
                keys, values = layer_cache.state
                clone = KVCache()
                clone.state = (
                    mx.broadcast_to(keys, (batch, *keys.shape[1:])),
                    mx.broadcast_to(values, (batch, *values.shape[1:])),
                )
                tiled.append(clone)
            embeds = model.model.embed_tokens(mx.array(padded))
            hidden = model.model(inputs_embeds=embeds, cache=tiled)
            logits = self._logits(hidden).astype(mx.float32)
            gold = mx.array(np.array([t[1:] + [0] * (width - len(t)) for t in targets], dtype=np.int32))
            picked = mx.take_along_axis(logits, gold[..., None], axis=-1)[..., 0]
            log_probs = np.array(picked - mx.logsumexp(logits, axis=-1))
            for row, target in enumerate(targets):
                sums[row] += float(log_probs[row, : len(target) - 1].sum())
        return [(float(total), len(target)) for total, target in zip(sums, targets, strict=True)]


# --------------------------------------------------------------------------------------
# Stage 1: candidates + model scores (private cache, never committed)
# --------------------------------------------------------------------------------------


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def build_term_sets(
    dictionary: Path, hotwords_from: Path | None, name_references: list[Path]
) -> tuple[tuple[Any, ...], list[Term], list[Term]]:
    """(replacement rules, dictionary+hotword terms, name terms) for one evaluation run."""
    from tea_asr.context import ReplacementRule

    replacements, entries, _ = load_dictionary(dictionary)
    if hotwords_from is not None:
        entries += [(word, "hot") for word in load_dictionary(hotwords_from)[2]]
    references = [
        item["reference"]
        for path in name_references
        for item in _load_json(path)["items"]
        if not item.get("music") and not item.get("unclear")
    ]
    rules = tuple(ReplacementRule(source, target) for source, target in replacements)
    dict_terms = make_terms(entries)
    known = {term.text for term in dict_terms}
    names = [(name, "name") for name in mine_names(references) if name not in known]
    return rules, dict_terms, make_terms(names)


def run_score(args: argparse.Namespace) -> int:
    import numpy as np  # noqa: F401  (imported for the scorer)

    from benchmarks.gold_offline_eval import read_wav
    from tea_asr.backend import TeaMlxBackend
    from tea_asr.context import apply_replacements

    items = _load_json(args.answers)["items"]
    if args.limit:
        items = items[: args.limit]
    base_a: dict[str, str] = _load_json(args.hypotheses)
    rules, dict_terms, name_terms = build_term_sets(
        args.dictionary, args.hotwords_from, args.name_references
    )
    samples = read_wav(args.wav)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    done: set[str] = set()
    if args.out.exists():
        done = {json.loads(line)["id"] for line in args.out.read_text("utf-8").splitlines() if line.strip() and '"id"' in line}
    backend = TeaMlxBackend(args.model_path)
    backend.load()
    scorer = TeacherForcedScorer(backend._model)
    header = {
        "header": True, "terms": [t.text for t in dict_terms], "names": len(name_terms),
        "name_terms": [t.text for t in name_terms], "rules": len(rules),
    }
    mode = "a" if done else "w"
    with args.out.open(mode, encoding="utf-8") as sink:
        if not done:
            sink.write(json.dumps(header, ensure_ascii=False) + "\n")
        for count, item in enumerate(items):
            if item["id"] in done or item["id"] not in base_a:
                continue
            text_a = base_a[item["id"]]
            text_b, _ = apply_replacements(text_a, rules)
            t_gen = time.perf_counter()
            cands: dict[str, list[dict[str, Any]]] = {}
            for label, base in (("A", text_a), ("B", text_b)):
                if label == "B" and base == text_a:
                    cands["B"] = cands["A"]
                    continue
                found = find_candidates(base, dict_terms) + find_candidates(base, name_terms)
                cands[label] = [candidate.as_dict() for candidate in found]
            gen_ms = (time.perf_counter() - t_gen) * 1000
            texts = {text_a, text_b}
            for label_cands in cands.values():
                texts.update(c["text"] for c in label_cands)
            scores: dict[str, list[float]] = {}
            prefix_ms = score_ms = 0.0
            if any(cands.values()):
                start = round(item["start_s"] * SAMPLE_RATE)
                end = round(item["end_s"] * SAMPLE_RATE)
                t0 = time.perf_counter()
                prefix = scorer.prefix(samples[start:end])
                t1 = time.perf_counter()
                ordered = sorted(texts)
                for lo in range(0, len(ordered), args.max_batch):
                    chunk = ordered[lo : lo + args.max_batch]
                    for text, (log_prob, tokens) in zip(chunk, scorer.score(prefix, chunk), strict=True):
                        scores[text] = [round(log_prob, 4), tokens]
                t2 = time.perf_counter()
                prefix_ms, score_ms = (t1 - t0) * 1000, (t2 - t1) * 1000
            row = {
                "id": item["id"], "A": text_a, "B": text_b, "cands": cands, "scores": scores,
                "t": {"gen_ms": gen_ms, "prefix_ms": prefix_ms, "score_ms": score_ms, "texts": len(scores)},
            }
            sink.write(json.dumps(row, ensure_ascii=False) + "\n")
            sink.flush()
            if count % 50 == 0:
                print(f"{count}/{len(items)}", file=sys.stderr, flush=True)
    backend.close()
    return 0


# --------------------------------------------------------------------------------------
# Stage 2: cross-validated tuning and metrics over the cached scores
# --------------------------------------------------------------------------------------

SYSTEMS = ("A", "B", "C", "D")
GRID_MARGINS = {
    "sum": (0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0),
    "tok": (0.0, 0.02, 0.05, 0.1, 0.15, 0.2, 0.3),
    "char": (0.0, 0.02, 0.05, 0.1, 0.15, 0.2, 0.3),
}
FALSE_CHANGE_PENALTY = 4.0


def parameter_grid() -> list[Params]:
    grid = [
        Params(margin, norm, cost_max, toneless, min_len, names)
        for norm, margins in GRID_MARGINS.items()
        for margin in sorted(margins, reverse=True)
        for cost_max in (0.15, 0.2, 0.25, 0.3, 0.35)
        for toneless in (False, True)
        for min_len in (2, 3)
        for names in (False, True)
    ]
    return grid


@dataclass
class Run:
    """One evaluation set: its items plus the cached candidates and scores."""

    name: str
    items: list[dict[str, Any]]
    rows: list[dict[str, Any]]
    terms: list[str]
    hours: float

    @classmethod
    def load(cls, name: str, answers: Path, cache: Path) -> Run:
        lines = [json.loads(line) for line in cache.read_text("utf-8").splitlines() if line.strip()]
        header, rows = lines[0], {row["id"]: row for row in lines[1:]}
        items = [
            {**item, "service": name}
            for item in _load_json(answers)["items"]
            if item["id"] in rows and not item.get("unclear")
        ]
        return cls(name, items, [rows[item["id"]] for item in items], header["terms"],
                   audio_duration_hours(items))


class Evaluator:
    """Applies the decision rule to cached scores; memoises per-text error counts."""

    def __init__(self, runs: list[Run]) -> None:
        self.runs = {run.name: run for run in runs}
        self._errors: dict[tuple[str, int, str], int] = {}
        self._class: dict[tuple[str, int, str], str] = {}

    def errors(self, run: Run, index: int, text: str) -> int:
        key = (run.name, index, text)
        if key not in self._errors:
            self._errors[key] = errors_of(run.items[index]["reference"], text)
        return self._errors[key]

    def decide(self, run: Run, index: int, system: str, params: Params) -> list[dict[str, Any]]:
        row = run.rows[index]
        base_key = "A" if system == "D" else "B"
        return accepted_candidates(row[base_key], row["cands"][base_key], row["scores"], params)

    def texts(
        self, run: Run, system: str, params: Params | None
    ) -> tuple[list[str], list[tuple[int, dict[str, Any], str]]]:
        """Output text per item and the (item, change, classification) list for ``system``."""
        outputs: list[str] = []
        changes: list[tuple[int, dict[str, Any], str]] = []
        for index, row in enumerate(run.rows):
            if system == "A":
                outputs.append(row["A"])
            elif system == "B" or params is None:
                outputs.append(row["B"])
            else:
                base = row["A"] if system == "D" else row["B"]
                accepted = self.decide(run, index, system, params)
                outputs.append(apply_accepted(base, accepted))
                for change in accepted:
                    changes.append((index, change, self.classify(run, index, base, change)))
        return outputs, changes

    def classify(self, run: Run, index: int, base: str, change: dict[str, Any]) -> str:
        key = (run.name, index, change["text"])
        if key not in self._class:
            self._class[key] = classify_change(run.items[index]["reference"], base, change)
        return self._class[key]

    def objective(self, runs: list[Run], system: str, params: Params) -> tuple[float, int, int]:
        """(objective, false changes, net error change) pooled over ``runs`` for the grid search."""
        delta = false = 0
        for run in runs:
            for index, row in enumerate(run.rows):
                base = row["A"] if system == "D" else row["B"]
                if not row["cands"]["A" if system == "D" else "B"]:
                    continue
                accepted = self.decide(run, index, system, params)
                if not accepted:
                    continue
                after = apply_accepted(base, accepted)
                delta += self.errors(run, index, after) - self.errors(run, index, base)
                false += sum(self.classify(run, index, base, c) == "false" for c in accepted)
        return delta + FALSE_CHANGE_PENALTY * false, false, delta

    def tune(
        self, train: list[Run], system: str, norm: str | None = None
    ) -> tuple[Params, dict[str, Any]]:
        """Grid search; most conservative parameters win ties (grid order is margin-descending)."""
        hours = sum(run.hours for run in train)
        best_key: tuple[bool, float] | None = None
        best: tuple[Params, int, int, float] | None = None
        for params in parameter_grid():
            if norm is not None and params.norm != norm:
                continue
            objective, false, delta = self.objective(train, system, params)
            key = (false <= int(hours), -objective)
            if best_key is None or key > best_key:
                best_key, best = key, (params, false, delta, objective)
        assert best is not None and best_key is not None
        params, false, delta, objective = best
        return params, {"objective": objective, "false": false, "delta_errors": delta,
                        "feasible": best_key[0], "train_hours": round(hours, 3)}


def item_error_vectors(run: Run, outputs: list[str]) -> tuple[Any, Any]:
    import numpy as np

    errors = np.array([errors_of(item["reference"], text) for item, text in zip(run.items, outputs, strict=True)], dtype=float)
    chars = np.array([len(normalize_for_scoring(item["reference"], fold_pronouns=True)) for item in run.items], dtype=float)
    return errors, chars


def _quantile(values: list[float], q: float) -> float:
    import numpy as np

    return float(np.quantile(values, q)) if values else 0.0


def summarize(
    evaluator: Evaluator,
    entries: list[tuple[Run, Params, Params]],
    *,
    iterations: int = 5000,
) -> dict[str, Any]:
    """Metrics for systems A-D over held-out runs (each with its own tuned C/D parameters)."""
    from benchmarks.church_eval_report import Scorer

    items: list[dict[str, Any]] = []
    outputs: dict[str, list[str]] = {system: [] for system in SYSTEMS}
    changes: dict[str, list[tuple[Run, int, dict[str, Any], str]]] = {"C": [], "D": []}
    offset = 0
    for run, params_c, params_d in entries:
        items.extend(run.items)
        for system, params in (("A", None), ("B", None), ("C", params_c), ("D", params_d)):
            texts, found = evaluator.texts(run, system, params)
            outputs[system].extend(texts)
            if system in changes:
                changes[system].extend((run, index, change, label) for index, change, label in found)
        offset += len(run.items)
    scorer = Scorer(items, iterations, 20261005)
    vectors = {}
    for system in SYSTEMS:
        errors, chars = [], []
        for run, start in _spans(entries):
            part_e, part_c = item_error_vectors(run, outputs[system][start : start + len(run.items)])
            errors.append(part_e)
            chars.append(part_c)
        import numpy as np

        vectors[system] = (np.concatenate(errors), np.concatenate(chars))
    scopes = ["pooled"] + [run.name for run, _, _ in entries]
    result: dict[str, Any] = {"cer": {}, "delta": {}, "changes": {}, "recall": {}, "candidates": {}}
    for system in SYSTEMS:
        result["cer"][system] = {scope: scorer.cer(*vectors[system], scope) for scope in scopes}
    for first, second in (("C", "B"), ("D", "B"), ("D", "A"), ("B", "A"), ("C", "A")):
        result["delta"][f"{first}-{second}"] = {
            scope: scorer.delta(vectors[first], vectors[second], scope) for scope in scopes
        }
    hours = sum(run.hours for run, _, _ in entries)
    result["hours"] = hours
    for system in ("C", "D"):
        labels = Counter(label for _, _, _, label in changes[system])
        result["changes"][system] = {
            "total": sum(labels.values()), **{k: labels.get(k, 0) for k in ("good", "neutral", "worse", "false")},
            "false_per_hour": labels.get("false", 0) / hours,
            "examples": [
                {"run": run.name, "item": index, "id": run.items[index]["id"], "cost": change["cost"],
                 "gain": round(change["gain"], 3), "src": change["src"]}
                for run, index, change, label in changes[system] if label == "false"
            ][:10],
        }
    for system in SYSTEMS:
        for threshold in (2, 3):
            correct = total = 0
            start = 0
            for run, _, _ in entries:
                terms = [t for t in run.terms if len(t) >= threshold]
                c, t = term_recall([i["reference"] for i in run.items],
                                   outputs[system][start : start + len(run.items)], terms)
                correct, total, start = correct + c, total + t, start + len(run.items)
            result["recall"].setdefault(system, {})[f"len>={threshold}"] = {
                "correct": correct, "total": total, "recall": correct / total if total else None,
            }
    n_items = len(items)
    for system, key in (("C", "B"), ("D", "A")):
        generated = sum(len(row["cands"][key]) for run, _, _ in entries for row in run.rows)
        passing = 0
        for run, params_c, params_d in entries:
            params = params_c if system == "C" else params_d
            for row in run.rows:
                passing += sum(
                    1 for c in row["cands"][key]
                    if (c["cost_tl"] if params.toneless else c["cost"]) <= params.cost_max
                    and len(c["term"]) >= params.min_term_len and (params.names or c["src"] != "name")
                )
        result["candidates"][system] = {
            "generated_per_segment": generated / n_items,
            "passing_filter_per_segment": passing / n_items,
            "accepted_per_segment": len(changes[system]) / n_items,
            "segments_with_candidates": sum(1 for run, _, _ in entries for row in run.rows if row["cands"][key]) / n_items,
        }
    return result


def _spans(entries: list[tuple[Run, Params, Params]]) -> list[tuple[Run, int]]:
    spans, start = [], 0
    for run, _, _ in entries:
        spans.append((run, start))
        start += len(run.items)
    return spans


def evaluator_for(runs: list[Run]) -> Evaluator:
    return Evaluator(runs)


def norm_ablation(evaluator: Evaluator, runs: list[Run]) -> dict[str, Any]:
    """Cross-validated result per normalisation (grid restricted to that norm)."""
    out: dict[str, Any] = {}
    for norm in NORMS:
        entries = []
        for held in runs:
            train = [run for run in runs if run is not held]
            entries.append((held, evaluator.tune(train, "C", norm)[0], evaluator.tune(train, "D", norm)[0]))
        summary = summarize(evaluator, entries, iterations=1000)
        out[norm] = {
            "C-B": summary["delta"]["C-B"]["pooled"], "D-A": summary["delta"]["D-A"]["pooled"],
            "changes": summary["changes"],
        }
    return out


def oracle_ceiling(evaluator: Evaluator, runs: list[Run]) -> dict[str, Any]:
    """Best possible error reduction if the scorer always picked the best generated candidates."""
    out: dict[str, Any] = {}
    for system, key in (("C", "B"), ("D", "A")):
        saved = base = 0
        for run in runs:
            for index, row in enumerate(run.rows):
                before = evaluator.errors(run, index, row[key])
                base += before
                gains = [before - evaluator.errors(run, index, c["text"]) for c in row["cands"][key]]
                saved += max([g for g in gains if g > 0], default=0)
        out[system] = {"base_errors": base, "max_errors_fixable_one_candidate_per_item": saved}
    return out


def margin_sweep(evaluator: Evaluator, runs: list[Run], final_c: Params, final_d: Params) -> dict[str, Any]:
    """Pooled change counts per margin with the final other parameters (in-sample, for shape only)."""
    out: dict[str, Any] = {}
    for system, base in (("C", final_c), ("D", final_d)):
        rows = []
        for margin in (0.0, 0.5, 1.0, 2.0, 3.0, 4.0, 6.0):
            params = Params(margin, base.norm, base.cost_max, base.toneless, base.min_term_len, base.names)
            total = Counter()
            delta = 0
            for run in runs:
                outputs, changes = evaluator.texts(run, system, params)
                total.update(label for _, _, label in changes)
                delta += sum(evaluator.errors(run, i, t) for i, t in enumerate(outputs)) - sum(
                    evaluator.errors(run, i, row["A" if system == "D" else "B"]) for i, row in enumerate(run.rows))
            rows.append({"margin": margin, "changes": sum(total.values()), "false": total["false"], "good": total["good"], "delta_errors": delta})
        out[system] = rows
    return out


def run_tune(args: argparse.Namespace) -> int:
    runs = [Run.load(name, args.work / "answers" / f"{name}-pad25.json", args.work / "rescore" / f"{name}.jsonl")
            for name in args.services]
    evaluator = Evaluator(runs)
    tuned: dict[str, Any] = {"folds": {}}
    entries: list[tuple[Run, Params, Params]] = []
    for held in runs:
        train = [run for run in runs if run is not held]
        params_c, info_c = evaluator.tune(train, "C")
        params_d, info_d = evaluator.tune(train, "D")
        tuned["folds"][held.name] = {"C": asdict(params_c) | {"train": info_c},
                                     "D": asdict(params_d) | {"train": info_d}}
        entries.append((held, params_c, params_d))
        print(f"fold {held.name}: C {params_c} D {params_d}", file=sys.stderr, flush=True)
    final_c, info_c = evaluator.tune(runs, "C")
    final_d, info_d = evaluator.tune(runs, "D")
    tuned["final"] = {"C": asdict(final_c) | {"train": info_c}, "D": asdict(final_d) | {"train": info_d}}
    results: dict[str, Any] = {"cv": summarize(evaluator, entries), "tuned": tuned}
    if args.gold_answers and (args.work / "rescore" / "gold.jsonl").exists():
        gold = Run.load("gold", args.gold_answers, args.work / "rescore" / "gold.jsonl")
        evaluator = Evaluator([*runs, gold])
        results["gold"] = summarize(evaluator, [(gold, final_c, final_d)], iterations=2000)
        results["gold"]["sweep"] = {
            f"margin={m}": summarize(evaluator, [(gold, Params(m, final_c.norm, final_c.cost_max, final_c.toneless, final_c.min_term_len, final_c.names),
                                                   Params(m, final_d.norm, final_d.cost_max, final_d.toneless, final_d.min_term_len, final_d.names))], iterations=200)["cer"]["C"]["pooled"]["errors"]
            for m in (0.0, final_c.margin)
        }
    results["norm_ablation"] = norm_ablation(evaluator_for(runs), runs)
    results["oracle"] = oracle_ceiling(evaluator_for(runs), runs)
    results["margin_sweep"] = margin_sweep(evaluator_for(runs), runs, final_c, final_d)
    args.results.parent.mkdir(parents=True, exist_ok=True)
    args.results.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


# --------------------------------------------------------------------------------------
# Stage 3: end-to-end pipeline run with timing (what a live integration would do)
# --------------------------------------------------------------------------------------


def rescore_text(
    scorer: TeacherForcedScorer,
    audio: Any,
    base_text: str,
    dict_terms: list[Term],
    name_terms: list[Term],
    params: Params,
) -> tuple[str, list[dict[str, Any]], dict[str, float]]:
    """Rescore one final: candidates -> model scores -> accepted replacements.

    Returns (text, accepted replacements, timings in ms).  Nothing is scored when no candidate
    passes the phonetic filter, so those finals only pay ``gen_ms``.
    """
    t0 = time.perf_counter()
    found = find_candidates(base_text, dict_terms)
    if params.names:
        found += find_candidates(base_text, name_terms)
    candidates = [
        candidate.as_dict() for candidate in found
        if (candidate.cost_toneless if params.toneless else candidate.cost) <= params.cost_max
        and len(candidate.term) >= params.min_term_len
    ]
    timing = {"gen_ms": (time.perf_counter() - t0) * 1000, "prefix_ms": 0.0, "score_ms": 0.0,
              "candidates": float(len(candidates))}
    if not candidates:
        return base_text, [], timing
    t1 = time.perf_counter()
    prefix = scorer.prefix(audio)
    t2 = time.perf_counter()
    texts = [base_text] + [c["text"] for c in candidates]
    scores = {text: [log_prob, tokens] for text, (log_prob, tokens) in zip(texts, scorer.score(prefix, texts), strict=True)}
    timing["prefix_ms"] = (t2 - t1) * 1000
    timing["score_ms"] = (time.perf_counter() - t2) * 1000
    accepted = accepted_candidates(base_text, candidates, scores, params)
    return apply_accepted(base_text, accepted), accepted, timing


def run_time(args: argparse.Namespace) -> int:
    from benchmarks.gold_offline_eval import read_wav
    from tea_asr.backend import TeaMlxBackend

    tuned = _load_json(args.tuned)["tuned"]
    chosen = tuned["folds"][args.fold] if args.fold else tuned["final"]
    params = {
        system: Params(**{k: v for k, v in chosen[system].items() if k != "train"}) for system in ("C", "D")
    }
    lines = [json.loads(line) for line in args.cache.read_text("utf-8").splitlines() if line.strip()]
    header, rows = lines[0], lines[1:]
    dict_terms = make_terms([(t, "dict") for t in header["terms"]])
    name_terms = make_terms([(t, "name") for t in header.get("name_terms", [])])
    by_id = {item["id"]: item for item in _load_json(args.answers)["items"]}
    samples = read_wav(args.wav)
    backend = TeaMlxBackend(args.model_path)
    backend.load()
    scorer = TeacherForcedScorer(backend._model)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    warm = by_id[rows[0]["id"]]
    scorer.prefix(samples[round(warm["start_s"] * SAMPLE_RATE): round(warm["end_s"] * SAMPLE_RATE)])
    with args.out.open("w", encoding="utf-8") as sink:
        for count, row in enumerate(rows):
            item = by_id.get(row["id"])
            if item is None or item.get("unclear"):
                continue
            audio = samples[round(item["start_s"] * SAMPLE_RATE): round(item["end_s"] * SAMPLE_RATE)]
            record: dict[str, Any] = {"id": row["id"], "dur_s": item["end_s"] - item["start_s"]}
            for system, base in (("C", row["B"]), ("D", row["A"])):
                text, accepted, timing = rescore_text(scorer, audio, base, dict_terms, name_terms, params[system])
                record[system] = {
                    "text": text, "applied": len(accepted), **timing,
                    "changes": [{k: change[k] for k in ("s", "e", "term", "cost", "src", "gain")} for change in accepted],
                }
            sink.write(json.dumps(record, ensure_ascii=False) + "\n")
            sink.flush()
            if count % 100 == 0:
                print(f"{count}/{len(rows)}", file=sys.stderr, flush=True)
    backend.close()
    return 0


# --------------------------------------------------------------------------------------
# Report: end-to-end metrics and the metrics-only Markdown file
# --------------------------------------------------------------------------------------


def e2e_metrics(
    runs: list[Run], e2e: dict[str, list[dict[str, Any]]], *, iterations: int = 5000
) -> dict[str, Any]:
    """CER, change classes and per-final timings from `time` outputs (one record list per run)."""
    import numpy as np

    from benchmarks.church_eval_report import Scorer

    items = [item for run in runs for item in run.items]
    scorer = Scorer(items, iterations, 20261005)
    outputs: dict[str, list[str]] = {system: [] for system in SYSTEMS}
    labels: dict[str, Counter[str]] = {"C": Counter(), "D": Counter()}
    timing: dict[str, dict[str, list[float]]] = {s: {"total": [], "marginal": [], "scored_total": [], "scored_marginal": [], "candidates": []} for s in ("C", "D")}
    for run in runs:
        records = {record["id"]: record for record in e2e[run.name]}
        for index, (item, row) in enumerate(zip(run.items, run.rows, strict=True)):
            outputs["A"].append(row["A"])
            outputs["B"].append(row["B"])
            record = records.get(item["id"])
            for system, base in (("C", row["B"]), ("D", row["A"])):
                entry = record[system] if record else {"text": base, "changes": [], "gen_ms": 0.0, "prefix_ms": 0.0, "score_ms": 0.0, "candidates": 0.0}
                outputs[system].append(entry["text"])
                for change in entry["changes"]:
                    full = {**change, "text": apply_accepted(base, [change])}
                    labels[system][classify_change(item["reference"], base, full)] += 1
                total = entry["gen_ms"] + entry["prefix_ms"] + entry["score_ms"]
                marginal = entry["gen_ms"] + entry["score_ms"]
                timing[system]["total"].append(total)
                timing[system]["marginal"].append(marginal)
                timing[system]["candidates"].append(entry["candidates"])
                if entry["candidates"]:
                    timing[system]["scored_total"].append(total)
                    timing[system]["scored_marginal"].append(marginal)
    vectors = {}
    for system in SYSTEMS:
        errors, chars, start = [], [], 0
        for run in runs:
            part_e, part_c = item_error_vectors(run, outputs[system][start : start + len(run.items)])
            errors.append(part_e)
            chars.append(part_c)
            start += len(run.items)
        vectors[system] = (np.concatenate(errors), np.concatenate(chars))
    hours = sum(run.hours for run in runs)
    scopes = ["pooled"] + [run.name for run in runs]
    result: dict[str, Any] = {
        "cer": {s: {scope: scorer.cer(*vectors[s], scope) for scope in scopes} for s in SYSTEMS},
        "delta": {
            f"{a}-{b}": {scope: scorer.delta(vectors[a], vectors[b], scope) for scope in scopes}
            for a, b in (("C", "B"), ("D", "B"), ("D", "A"), ("B", "A"), ("C", "A"))
        },
        "hours": hours, "changes": {}, "timing": {},
    }
    for system in ("C", "D"):
        total = sum(labels[system].values())
        result["changes"][system] = {
            "total": total, **{k: labels[system].get(k, 0) for k in ("good", "neutral", "worse", "false")},
            "false_per_hour": labels[system].get("false", 0) / hours,
        }
        values = timing[system]
        result["timing"][system] = {
            "finals": len(values["total"]),
            "finals_scored": len(values["scored_total"]),
            "candidates_per_final": float(np.mean(values["candidates"])),
            "all_finals_ms": {"median": _quantile(values["total"], 0.5), "p95": _quantile(values["total"], 0.95), "p99": _quantile(values["total"], 0.99), "max": max(values["total"])},
            "scored_finals_ms": {"median": _quantile(values["scored_total"], 0.5), "p95": _quantile(values["scored_total"], 0.95), "max": max(values["scored_total"], default=0.0)},
            "marginal_all_finals_ms": {"median": _quantile(values["marginal"], 0.5), "p95": _quantile(values["marginal"], 0.95)},
            "marginal_scored_finals_ms": {"median": _quantile(values["scored_marginal"], 0.5), "p95": _quantile(values["scored_marginal"], 0.95)},
            "added_seconds_per_audio_hour": sum(values["total"]) / 1000 / hours,
        }
    return result


def run_report(args: argparse.Namespace) -> int:
    runs = [Run.load(name, args.work / "answers" / f"{name}-pad25.json", args.work / "rescore" / f"{name}.jsonl")
            for name in args.services]
    e2e = {name: [json.loads(line) for line in (args.work / "rescore" / f"{name}.e2e.jsonl").read_text("utf-8").splitlines() if line.strip()]
           for name in args.services}
    results = _load_json(args.results)
    results["e2e"] = e2e_metrics(runs, e2e)
    if args.gold_answers and (args.work / "rescore" / "gold.e2e.jsonl").exists():
        gold = Run.load("gold", args.gold_answers, args.work / "rescore" / "gold.jsonl")
        gold_e2e = {"gold": [json.loads(line) for line in (args.work / "rescore" / "gold.e2e.jsonl").read_text("utf-8").splitlines() if line.strip()]}
        results["gold_e2e"] = e2e_metrics([gold], gold_e2e, iterations=2000)
    args.results.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.markdown:
        import inspect

        refs = {}
        for name in ("prefix", "score"):
            lines, first = inspect.getsourcelines(getattr(TeacherForcedScorer, name))
            refs[name] = f"`benchmarks/phonetic_rescore.py:{first}-{first + len(lines) - 1}`"
        refs["scorer"] = f"`TeacherForcedScorer.prefix` {refs['prefix']} and `.score` {refs['score']}"
        args.markdown.write_text(render_markdown(results, line_refs=refs), encoding="utf-8")
    return 0


def _pct(value: float) -> str:
    return f"{100 * value:.2f}"


def _cer_cell(entry: dict[str, Any]) -> str:
    return f"{_pct(entry['cer'])} [{_pct(entry['ci95_low'])}, {_pct(entry['ci95_high'])}]"


def _delta_cell(entry: dict[str, Any]) -> str:
    star = "*" if entry["significant"] else ""
    low, high = 100 * entry["ci95_low"], 100 * entry["ci95_high"]
    return f"{100 * entry['delta_cer']:.3f} [{low:.3f}, {high:.3f}]{star}"


def _cer_tables(block: dict[str, Any], scopes: list[str]) -> list[str]:
    lines = ["| system | " + " | ".join(scopes) + " |", "|---|" + "---:|" * len(scopes)]
    names = {"A": "A baseline 8-bit + carry", "B": "B = A + dictionary replacements",
             "C": "C = B + rescoring (dictionary terms)", "D": "D = A + rescoring, terms only (no rules)"}
    for system in SYSTEMS:
        lines.append(f"| {names[system]} | " + " | ".join(_cer_cell(block["cer"][system][s]) for s in scopes) + " |")
    lines += ["", "Paired CER difference, percentage points (negative favours the first system; * = CI excludes 0):", "",
              "| pair | " + " | ".join(scopes) + " |", "|---|" + "---:|" * len(scopes)]
    for pair in ("C-B", "D-B", "D-A", "B-A"):
        lines.append(f"| {pair} | " + " | ".join(_delta_cell(block["delta"][pair][s]) for s in scopes) + " |")
    return lines


def render_markdown(results: dict[str, Any], *, line_refs: dict[str, str]) -> str:
    cv, e2e = results["cv"], results["e2e"]
    scopes = ["pooled", "20260704", "20260822", "20260912"]
    c_ci = e2e["delta"]["C-B"]["pooled"]
    timing_c = e2e["timing"]["C"]
    go = {
        "CER improvement CI below 0 (C vs B, end-to-end)": c_ci["ci95_high"] < 0,
        "false changes <= 1 per hour (C)": e2e["changes"]["C"]["false_per_hour"] <= 1.0,
        "p95 added time <= 300 ms per final (C, standalone)": timing_c["all_finals_ms"]["p95"] <= 300.0,
    }
    go_ci, go_false, go_time = go.values()
    verdict = "GO" if all(go.values()) else "NO-GO as built"
    out = [
        "# Phonetic candidates + ASR-model rescoring (offline, 2026-10)", "",
        "Metrics only: no transcript, caption or audio text is in this file. Commands: "
        "[docs/09](../09-testing-guide.md) (section on phonetic candidates + model rescoring). Code: "
        "`benchmarks/phonetic_rescore.py` (benchmark only; the live server never imports it).", "",
        "## Verdict", "", f"**{verdict}** for live integration.", "", "| criterion | result | met |", "|---|---|---|",
        f"| CER improvement CI below 0 (C vs B) | {_delta_cell(c_ci)} pp | {'yes' if go_ci else 'no'} |",
        f"| false changes <= 1 per audio hour (C) | {e2e['changes']['C']['false']} in {e2e['hours']:.2f} h "
        f"= {e2e['changes']['C']['false_per_hour']:.2f}/h | {'yes' if go_false else 'no'} |",
        f"| p95 added time <= 300 ms per final (C) | {timing_c['all_finals_ms']['p95']:.0f} ms standalone "
        f"({timing_c['marginal_all_finals_ms']['p95']:.0f} ms if the audio/prompt prefill were shared with the decode) "
        f"| {'yes' if go_time else 'no'} |", "",
        "## Reading", "",
        "* The scorer is precise: with the tuned margin, C made "
        f"{e2e['changes']['C']['total']} replacements in {e2e['hours']:.1f} h, {e2e['changes']['C']['good']} of them good, "
        f"{e2e['changes']['C']['false']} false. But the dictionary rules in B already take the easy wins, so on top of B the "
        "gain is a handful of characters (see the delta table); that is statistically below zero but far from useful.",
        "* D (terms only, no mishearing rules) recovers only a small part of what the rules give: compare D-A with B-A. "
        "Rescoring does not replace the long rule list; it finds some extra cases the rules miss.",
        "* Ceiling: even a perfect picker over the generated candidates could fix only the 'Ceiling' count below, "
        "so the phonetic candidate generator, not the scorer, bounds the gain.",
        "* Cost: about one extra encode + prefill + batch per final that has a candidate; the p95 target fails "
        "standalone and sits at the limit even if the decode's prefill were shared.", "",
        "## What was built", "",
        "Targets: dictionary `to` values (Han, >= 2 chars) plus the 13 live hotwords, plus optional names with titles "
        "mined from the training services' references only. Candidates: pinyin (TONE3) syllable edit distance with "
        "initial/final confusion costs (zh/z, ch/c, sh/s, n/l, f/h, m/n, j/zh, en/eng, an/ang, in/ing, w/y/zero ...), "
        "windows of length |term| +-1 inside one Han run, best 8 per text by tonal cost (toneless cost also stored). "
        "Scorer: the 8-bit Qwen3-ASR model itself, teacher-forced log p(text | audio) on the item audio, "
        "audio-encoder and prompt prefill once, all candidates in one batch over a broadcast KV cache, "
        "`<|im_end|>` included. Decision: replace when score(candidate) - score(original) > margin; several "
        "non-overlapping windows may be replaced. Scoring implementation: " + line_refs["scorer"] + ".", "",
        "Cross-validation: for each held-out service the dictionary is `church.toml` (13 rules) + rules mined from the "
        "other two services (`dict-cv/<held>/fold-safe.toml`), hotwords are the live 13, names come from the other two "
        "services' references, and the decision parameters (norm, margin, phonetic cost limit, tone use, minimum term "
        "length, names on/off) are grid-searched on the other two services (objective: errors + 4 x false changes; "
        "feasible only if false changes <= training hours). The live 116-rule dictionary was mined from all three "
        "services, so it is not used for the CV numbers (it would leak); it is used for the separate hand-corrected "
        "set. Hypotheses are `eval/m8-c3` (8-bit, carry 3 s); scoring uses the item audio span without the carry "
        "audio; CER is pronoun-folded; CIs are the 5-minute-cluster bootstrap of `church_eval_report.py`.", "",
        f"Held-out audio: {cv['hours']:.2f} h, {len(scopes) - 1} services pooled.", "",
        "## CER, held-out services (end-to-end pipeline run, per-fold parameters)", ""]
    out += _cer_tables(e2e, scopes)
    out += ["", "Same comparison computed from the stage-1 cache (batch scores of all candidates together; "
            "shows the batch-composition noise is harmless):", ""]
    out += _cer_tables(cv, scopes)
    out += ["", "## Changes made, target-term recall, candidates", "",
            "| system | replacements | good | neutral | worse | false (correct -> wrong) | false / hour |", "|---|---:|---:|---:|---:|---:|---:|"]
    for system in ("C", "D"):
        c = e2e["changes"][system]
        out.append(f"| {system} | {c['total']} | {c['good']} | {c['neutral']} | {c['worse']} | {c['false']} | {c['false_per_hour']:.2f} |")
    out += ["", "| system | recall, terms >= 3 chars | recall, all terms >= 2 chars |", "|---|---:|---:|"]
    for system in SYSTEMS:
        r3, r2 = cv["recall"][system]["len>=3"], cv["recall"][system]["len>=2"]
        out.append(f"| {system} | {r3['correct']}/{r3['total']} = {_pct(r3['recall'])}% | {r2['correct']}/{r2['total']} = {_pct(r2['recall'])}% |")
    out += ["", "| system | candidates generated / segment | passing phonetic filter / segment | accepted / segment | segments with >= 1 candidate |", "|---|---:|---:|---:|---:|"]
    for system in ("C", "D"):
        c = cv["candidates"][system]
        out.append(f"| {system} | {c['generated_per_segment']:.2f} | {c['passing_filter_per_segment']:.2f} | {c['accepted_per_segment']:.4f} | {_pct(c['segments_with_candidates'])}% |")
    oracle = results["oracle"]
    out += ["", "Ceiling: if the scorer always picked the best generated candidate, errors fixable "
            f"in C {oracle['C']['max_errors_fixable_one_candidate_per_item']} of {oracle['C']['base_errors']}; "
            f"in D {oracle['D']['max_errors_fixable_one_candidate_per_item']} of {oracle['D']['base_errors']} (one candidate per item).", "",
            "## Added time per final (8-bit model, this Mac, nothing else decoding)", "",
            "| system | finals | scored (>= 1 candidate) | candidates / final | median ms (all) | p95 ms (all) | p99 ms (all) | median ms (scored) | p95 ms (scored) | p95 ms (all) if prefill shared | added s per audio hour |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for system in ("C", "D"):
        t = e2e["timing"][system]
        out.append(f"| {system} | {t['finals']} | {t['finals_scored']} | {t['candidates_per_final']:.2f} | {t['all_finals_ms']['median']:.0f} | {t['all_finals_ms']['p95']:.0f} | {t['all_finals_ms']['p99']:.0f} | {t['scored_finals_ms']['median']:.0f} | {t['scored_finals_ms']['p95']:.0f} | {t['marginal_all_finals_ms']['p95']:.0f} | {t['added_seconds_per_audio_hour']:.0f} |")
    out += ["", "Standalone = candidate generation + audio encode + prompt prefill + batched candidate scoring. "
            "'Prefill shared' drops the encode+prefill, assuming a live integration reuses the decode's audio "
            "embeddings and prompt cache (not implemented or measured).", "",
            "## Normalisation, margin and parameters", "", "Cross-validated pooled result per normalisation of the score difference (sum = log-likelihood ratio; tok = per token; char = per character):", "",
            "| norm | C-B pp [CI] | D-A pp [CI] | C replacements (false) | D replacements (false) |", "|---|---:|---:|---:|---:|"]
    for norm, v in results["norm_ablation"].items():
        out.append(f"| {norm} | {_delta_cell(v['C-B'])} | {_delta_cell(v['D-A'])} | {v['changes']['C']['total']} ({v['changes']['C']['false']}) | {v['changes']['D']['total']} ({v['changes']['D']['false']}) |")
    out += ["", "Margin sweep with the final other parameters, pooled over the three services (in-sample, shows the trade-off only):", "",
            "| system | margin | replacements | good | false | net error change |", "|---|---:|---:|---:|---:|---:|"]
    for system, rows in results["margin_sweep"].items():
        for row in rows:
            out.append(f"| {system} | {row['margin']} | {row['changes']} | {row['good']} | {row['false']} | {row['delta_errors']} |")
    out += ["", "Tuned parameters (norm, margin, phonetic cost limit, toneless, min term length, names):", "",
            "| fold | C | D |", "|---|---|---|"]
    def fmt(p: dict[str, Any]) -> str:
        return f"{p['norm']}, {p['margin']}, {p['cost_max']}, {p['toneless']}, {p['min_term_len']}, {p['names']}"
    for fold, v in results["tuned"]["folds"].items():
        out.append(f"| held out {fold} | {fmt(v['C'])} | {fmt(v['D'])} |")
    out.append(f"| final (all three) | {fmt(results['tuned']['final']['C'])} | {fmt(results['tuned']['final']['D'])} |")
    if "gold_e2e" in results:
        g = results["gold_e2e"]
        out += ["", "## Hand-corrected set (speakers-answers.json, church-30m-59m.wav)", "",
                f"{g['hours'] * 60:.1f} audio minutes of corrected items; live 116-rule dictionary; parameters = final (tuned on the three services). "
                "Independence of this audio from the three services was not checked, and 40 items are too few for a significance claim.", ""]
        out += _cer_tables(g, ["gold"])
        out += ["", f"Replacements: C {g['changes']['C']['total']} (false {g['changes']['C']['false']}), D {g['changes']['D']['total']} (false {g['changes']['D']['false']})."]
    return "\n".join(out) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    score = sub.add_parser("score", help="generate candidates and score them with the model")
    score.add_argument("--answers", type=Path, required=True)
    score.add_argument("--wav", type=Path, required=True)
    score.add_argument("--hypotheses", type=Path, required=True, help="{item_id: text} JSON (system A)")
    score.add_argument("--dictionary", type=Path, required=True, help="replacements + hotwords TOML")
    score.add_argument("--hotwords-from", type=Path, help="extra hotwords TOML")
    score.add_argument("--name-references", type=Path, nargs="*", default=[],
                       help="answers JSON files (training only) to mine names with titles from")
    score.add_argument("--model-path", type=Path, required=True)
    score.add_argument("--out", type=Path, required=True)
    score.add_argument("--limit", type=int, default=0)
    score.add_argument("--max-batch", type=int, default=12)
    tune = sub.add_parser("tune", help="cross-validate decision parameters; write metrics JSON")
    tune.add_argument("--work", type=Path, required=True, help="church-eval work dir (answers/, rescore/)")
    tune.add_argument("--services", nargs="+", default=["20260704", "20260822", "20260912"])
    tune.add_argument("--gold-answers", type=Path)
    tune.add_argument("--results", type=Path, required=True)
    timing = sub.add_parser("time", help="end-to-end pipeline run with per-final timings")
    timing.add_argument("--answers", type=Path, required=True)
    timing.add_argument("--wav", type=Path, required=True)
    timing.add_argument("--cache", type=Path, required=True, help="stage-1 cache (for texts and terms)")
    timing.add_argument("--tuned", type=Path, required=True, help="results JSON from `tune`")
    timing.add_argument("--fold", help="held-out service whose fold parameters to use (default: final)")
    timing.add_argument("--model-path", type=Path, required=True)
    timing.add_argument("--out", type=Path, required=True)
    report = sub.add_parser("report", help="add end-to-end metrics (from `time` outputs) to the results JSON")
    report.add_argument("--work", type=Path, required=True)
    report.add_argument("--services", nargs="+", default=["20260704", "20260822", "20260912"])
    report.add_argument("--gold-answers", type=Path)
    report.add_argument("--results", type=Path, required=True)
    report.add_argument("--markdown", type=Path, help="write the metrics-only Markdown report here")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "score":
        return run_score(args)
    if args.command == "tune":
        return run_tune(args)
    if args.command == "time":
        return run_time(args)
    if args.command == "report":
        return run_report(args)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
