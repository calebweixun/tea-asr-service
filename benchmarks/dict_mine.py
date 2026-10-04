"""Mine reviewable replacement candidates from CER answer and hypothesis files."""

from __future__ import annotations

import argparse
import json
import tomllib
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

try:
    from benchmarks.cer_eval import load_reference, normalize
except ModuleNotFoundError:  # Running this file directly puts benchmarks on sys.path.
    from cer_eval import load_reference, normalize


MIN_SPAN_CHARS = 2
MAX_SPAN_CHARS = 8
MIN_COUNT = 2
MIN_SUPPORT = 2
MIN_PRECISION = 0.9
CONTEXT_CHARS = 10
_CJK_NUMERALS = frozenset("〇零一二三四五六七八九十百千萬億兩")

BUILTIN_STOPLIST = frozenset(
    normalize(word)
    for word in (
        "你們",
        "我們",
        "他們",
        "她們",
        "因為",
        "所以",
        "這個",
        "那個",
        "就是",
        "可以",
        "什麼",
        "為什麼",
        "一個",
        "沒有",
        "不是",
    )
)
AMEN_VARIANTS = ("阿門", "阿們")
STYLE_RULES = (
    ("你", "祢", "God pronoun"),
    ("他", "祂", "God pronoun"),
    ("阿門", "AMEN", "Amen spelling"),
    ("阿們", "AMEN", "Amen spelling"),
)


@dataclass(frozen=True, slots=True)
class NormalizedText:
    value: str
    source_spans: tuple[tuple[int, int], ...]

    def surface(self, text: str, start: int, end: int) -> str:
        """Return the original surface slice, including ignored marks between its chars."""
        raw_start, raw_end = self.raw_bounds(start, end)
        return text[raw_start:raw_end]

    def raw_bounds(self, start: int, end: int) -> tuple[int, int]:
        spans = self.source_spans[start:end]
        if not spans:
            return (0, 0)
        return min(span[0] for span in spans), max(span[1] for span in spans)


@dataclass(frozen=True, slots=True)
class AlignmentStep:
    operation: str
    reference_index: int | None
    hypothesis_index: int | None


@dataclass(slots=True)
class Observation:
    set_name: str
    item_id: str
    reference: str
    hypothesis: str
    normalized_reference: NormalizedText
    normalized_hypothesis: NormalizedText
    steps: list[AlignmentStep]
    style_spans: set[tuple[int, int]] = field(default_factory=set)

    @property
    def item_key(self) -> tuple[str, str]:
        return self.set_name, self.item_id

    @property
    def hypothesis_to_reference(self) -> list[int | None]:
        mapping: list[int | None] = [None] * len(self.normalized_hypothesis.value)
        for step in self.steps:
            if step.hypothesis_index is not None:
                mapping[step.hypothesis_index] = step.reference_index
        return mapping


@dataclass(frozen=True, slots=True)
class SubstitutionSpan:
    hypothesis_start: int
    hypothesis_end: int
    reference_start: int
    reference_end: int


@dataclass(slots=True)
class Candidate:
    from_: str
    to: str
    count: int
    support: int
    precision_proxy: float
    harm: int
    occurrences: int
    contexts: list[str]
    hotword_exempt: bool = False


def normalize_with_spans(text: str) -> NormalizedText:
    """Use cer_eval normalization and retain a map back to original surface characters."""
    normalized = normalize(text)
    pieces: list[str] = []
    spans: list[tuple[int, int]] = []
    for index, char in enumerate(text):
        piece = normalize(char)
        pieces.append(piece)
        spans.extend([(index, index + 1)] * len(piece))

    joined = "".join(pieces)
    if joined != normalized:
        # NFKC can compose across adjacent source characters. Reconcile that uncommon
        # case against the authoritative whole-string normalization.
        import difflib

        mapped: list[tuple[int, int] | None] = [None] * len(normalized)
        matcher = difflib.SequenceMatcher(a=joined, b=normalized, autojunk=False)
        for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
            if tag == "equal":
                mapped[new_start:new_end] = spans[old_start:old_end]
                continue
            old_spans = spans[old_start:old_end]
            if old_spans:
                source_span = (
                    min(span[0] for span in old_spans),
                    max(span[1] for span in old_spans),
                )
            elif spans:
                nearest = min(old_start, len(spans) - 1)
                source_span = spans[nearest]
            else:
                source_span = (0, 0)
            mapped[new_start:new_end] = [source_span] * (new_end - new_start)
        spans = [span if span is not None else (0, 0) for span in mapped]
    return NormalizedText(normalized, tuple(spans))


def align_characters(reference: str, hypothesis: str) -> list[AlignmentStep]:
    """Return a deterministic Levenshtein alignment matching cer_eval edit priorities."""
    ref_len = len(reference)
    hyp_len = len(hypothesis)
    costs = [[0] * (hyp_len + 1) for _ in range(ref_len + 1)]
    directions = [[""] * (hyp_len + 1) for _ in range(ref_len + 1)]
    for row in range(1, ref_len + 1):
        costs[row][0] = row
        directions[row][0] = "D"
    for column in range(1, hyp_len + 1):
        costs[0][column] = column
        directions[0][column] = "I"

    for row in range(1, ref_len + 1):
        for column in range(1, hyp_len + 1):
            same = reference[row - 1] == hypothesis[column - 1]
            choices = (
                (costs[row - 1][column - 1] + (not same), "M" if same else "S"),
                (costs[row - 1][column] + 1, "D"),
                (costs[row][column - 1] + 1, "I"),
            )
            costs[row][column], directions[row][column] = min(
                choices, key=lambda choice: (choice[0], "MSDI".index(choice[1]))
            )

    steps: list[AlignmentStep] = []
    row, column = ref_len, hyp_len
    while row or column:
        direction = directions[row][column]
        if direction in ("M", "S"):
            steps.append(AlignmentStep(direction, row - 1, column - 1))
            row -= 1
            column -= 1
        elif direction == "D":
            steps.append(AlignmentStep(direction, row - 1, None))
            row -= 1
        else:
            steps.append(AlignmentStep(direction, None, column - 1))
            column -= 1
    steps.reverse()
    return steps


def _overlapping_occurrences(text: str, pattern: str) -> list[int]:
    if not pattern:
        return []
    matches: list[int] = []
    start = 0
    while True:
        index = text.find(pattern, start)
        if index < 0:
            return matches
        matches.append(index)
        start = index + 1


def _is_cjk(char: str) -> bool:
    name = unicodedata.name(char, "")
    return "CJK" in name or "IDEOGRAPH" in name


def _is_latin_word_char(char: str) -> bool:
    name = unicodedata.name(char, "")
    return char == "_" or ("LATIN" in name and char.isalpha()) or char.isdigit()


def _normalized_chars_are_adjacent(value: NormalizedText, left: int, right: int) -> bool:
    left_span = value.source_spans[left]
    right_span = value.source_spans[right]
    return left_span[1] == right_span[0] or left_span == right_span


def _step_indices(
    steps: list[AlignmentStep], start: int, end: int
) -> tuple[int, int, int, int]:
    selected = steps[start:end]
    hypothesis_indices = [step.hypothesis_index for step in selected]
    reference_indices = [step.reference_index for step in selected]
    h_values = [index for index in hypothesis_indices if index is not None]
    r_values = [index for index in reference_indices if index is not None]
    return min(h_values), max(h_values) + 1, min(r_values), max(r_values) + 1


def _latin_boundary_extension(
    steps: list[AlignmentStep],
    start: int,
    end: int,
    normalized_reference: NormalizedText,
    normalized_hypothesis: NormalizedText,
) -> tuple[int, int]:
    hyp = normalized_hypothesis.value
    ref = normalized_reference.value
    while start > 0 and steps[start - 1].operation == "M":
        step = steps[start - 1]
        assert step.hypothesis_index is not None and step.reference_index is not None
        h_index = step.hypothesis_index
        r_index = step.reference_index
        current_h = steps[start].hypothesis_index
        current_r = steps[start].reference_index
        assert current_h is not None and current_r is not None
        if not (
            _is_latin_word_char(hyp[h_index])
            and _is_latin_word_char(ref[r_index])
            and _normalized_chars_are_adjacent(normalized_hypothesis, h_index, current_h)
            and _normalized_chars_are_adjacent(normalized_reference, r_index, current_r)
        ):
            break
        start -= 1
    while end < len(steps) and steps[end].operation == "M":
        step = steps[end]
        assert step.hypothesis_index is not None and step.reference_index is not None
        previous = steps[end - 1]
        assert previous.hypothesis_index is not None and previous.reference_index is not None
        h_index = step.hypothesis_index
        r_index = step.reference_index
        if not (
            _is_latin_word_char(hyp[h_index])
            and _is_latin_word_char(ref[r_index])
            and _normalized_chars_are_adjacent(
                normalized_hypothesis, previous.hypothesis_index, h_index
            )
            and _normalized_chars_are_adjacent(
                normalized_reference, previous.reference_index, r_index
            )
        ):
            break
        end += 1
    return start, end


def _cjk_boundary_extension(
    steps: list[AlignmentStep],
    start: int,
    end: int,
    normalized_hypothesis: NormalizedText,
) -> tuple[int, int]:
    """Grow Han spans to a locally unique substring, bounded by 8 characters."""
    hyp = normalized_hypothesis.value
    while True:
        h_start, h_end, _, _ = _step_indices(steps, start, end)
        source = hyp[h_start:h_end]
        occurrences = len(_overlapping_occurrences(hyp, source))
        if len(source) >= MIN_SPAN_CHARS and occurrences <= 1:
            return start, end

        choices: list[tuple[int, int, int, int]] = []
        if start > 0 and steps[start - 1].operation == "M":
            left = steps[start - 1]
            current = steps[start]
            assert left.hypothesis_index is not None and current.hypothesis_index is not None
            if (
                _is_cjk(hyp[left.hypothesis_index])
                and _normalized_chars_are_adjacent(
                    normalized_hypothesis, left.hypothesis_index, current.hypothesis_index
                )
                and len(source) < MAX_SPAN_CHARS
            ):
                new_source = hyp[left.hypothesis_index:h_end]
                choices.append(
                    (
                        len(_overlapping_occurrences(hyp, new_source)),
                        len(new_source),
                        0,
                        start - 1,
                    )
                )
        if end < len(steps) and steps[end].operation == "M":
            right = steps[end]
            current = steps[end - 1]
            assert right.hypothesis_index is not None and current.hypothesis_index is not None
            if (
                _is_cjk(hyp[right.hypothesis_index])
                and _normalized_chars_are_adjacent(
                    normalized_hypothesis, current.hypothesis_index, right.hypothesis_index
                )
                and len(source) < MAX_SPAN_CHARS
            ):
                new_source = hyp[h_start : right.hypothesis_index + 1]
                choices.append(
                    (
                        len(_overlapping_occurrences(hyp, new_source)),
                        len(new_source),
                        1,
                        end,
                    )
                )
        if not choices:
            return start, end
        _, _, side, step_index = min(choices)
        if side == 0:
            start = step_index
        else:
            end = step_index + 1


def extract_substitution_spans(
    normalized_reference: NormalizedText, normalized_hypothesis: NormalizedText
) -> list[SubstitutionSpan]:
    """Find 2–8 character substitution spans and extend Latin/CJK boundaries."""
    steps = align_characters(normalized_reference.value, normalized_hypothesis.value)
    spans: list[SubstitutionSpan] = []
    position = 0
    while position < len(steps):
        if steps[position].operation != "S":
            position += 1
            continue
        start = position
        while position < len(steps) and steps[position].operation == "S":
            position += 1
        end = position
        hyp_start, hyp_end, ref_start, ref_end = _step_indices(steps, start, end)
        source = normalized_hypothesis.value[hyp_start:hyp_end]
        target = normalized_reference.value[ref_start:ref_end]
        if any(_is_latin_word_char(char) for char in source + target):
            start, end = _latin_boundary_extension(
                steps, start, end, normalized_reference, normalized_hypothesis
            )
        elif any(_is_cjk(char) for char in source):
            start, end = _cjk_boundary_extension(
                steps, start, end, normalized_hypothesis
            )
        hyp_start, hyp_end, ref_start, ref_end = _step_indices(steps, start, end)
        source = normalized_hypothesis.value[hyp_start:hyp_end]
        target = normalized_reference.value[ref_start:ref_end]
        if (
            MIN_SPAN_CHARS <= len(source) <= MAX_SPAN_CHARS
            and MIN_SPAN_CHARS <= len(target) <= MAX_SPAN_CHARS
        ):
            spans.append(SubstitutionSpan(hyp_start, hyp_end, ref_start, ref_end))
    return spans


def _mapped_reference_range(
    observation: Observation, hypothesis_start: int, hypothesis_end: int
) -> tuple[int, int] | None:
    mapping = observation.hypothesis_to_reference
    indices = mapping[hypothesis_start:hypothesis_end]
    if not indices or any(index is None for index in indices):
        return None
    reference_indices = [index for index in indices if index is not None]
    if reference_indices != list(
        range(reference_indices[0], reference_indices[0] + len(reference_indices))
    ):
        return None
    return reference_indices[0], reference_indices[-1] + 1


def _reference_target_near(
    observation: Observation, hypothesis_start: int, hypothesis_end: int, target: str
) -> tuple[int, int] | None:
    mapped = _mapped_reference_range(observation, hypothesis_start, hypothesis_end)
    if mapped is None:
        mapping = observation.hypothesis_to_reference
        indices = [
            index
            for index in mapping[hypothesis_start:hypothesis_end]
            if index is not None
        ]
        if not indices:
            return None
        anchor_start, anchor_end = min(indices), max(indices) + 1
    else:
        anchor_start, anchor_end = mapped
    ref = observation.normalized_reference.value
    for match_start in _overlapping_occurrences(ref, target):
        match_end = match_start + len(target)
        if match_end >= anchor_start - 4 and match_start <= anchor_end + 4:
            return match_start, match_end
    return None


def detect_style_rules(observation: Observation) -> dict[str, int]:
    """Count canonical style substitutions and mark them away from dictionary mining."""
    hyp = observation.normalized_hypothesis.value
    ref = observation.normalized_reference.value
    mapping = observation.hypothesis_to_reference
    counts = {f"{source}→{target}": 0 for source, target, _ in STYLE_RULES}

    for source, target in (("你", "祢"), ("他", "祂")):
        for start in _overlapping_occurrences(hyp, source):
            end = start + len(source)
            mapped_indices = [
                index for index in mapping[start:end] if index is not None
            ]
            if not mapped_indices:
                continue
            ref_start, ref_end = min(mapped_indices), max(mapped_indices) + 1
            if ref[ref_start:ref_end] != target:
                continue
            counts[f"{source}→{target}"] += 1
            observation.style_spans.add((start, end))

    for source in AMEN_VARIANTS:
        for start in _overlapping_occurrences(hyp, source):
            end = start + len(source)
            if _reference_target_near(observation, start, end, "amen") is not None:
                counts[f"{source}→AMEN"] += 1
                observation.style_spans.add((start, end))
    return counts


def _context_for(
    observation: Observation,
    hypothesis_start: int,
    hypothesis_end: int,
    source: str,
    target: str,
    reference_start: int,
    reference_end: int,
) -> str:
    raw_hyp_start, raw_hyp_end = observation.normalized_hypothesis.raw_bounds(
        hypothesis_start, hypothesis_end
    )
    before = observation.hypothesis[max(0, raw_hyp_start - CONTEXT_CHARS) : raw_hyp_start]
    after = observation.hypothesis[raw_hyp_end : raw_hyp_end + CONTEXT_CHARS]
    display_source = observation.normalized_hypothesis.surface(
        observation.hypothesis, hypothesis_start, hypothesis_end
    )
    display_target = observation.normalized_reference.surface(
        observation.reference, reference_start, reference_end
    )
    return f"{before}⟦{display_source or source}→{display_target or target}⟧{after}"


def _find_occurrences(text: str, pattern: str):
    for start in _overlapping_occurrences(text, pattern):
        yield start, start + len(pattern)


def mine_candidates(
    observations: list[Observation],
    *,
    hotwords: set[str] | None = None,
) -> tuple[list[Candidate], dict[str, int]]:
    hotword_norms = {normalize(word) for word in (hotwords or set())}
    observed_pairs: set[tuple[str, str]] = set()
    style_counts: Counter[str] = Counter(
        {f"{source}→{target}": 0 for source, target, _ in STYLE_RULES}
    )

    for observation in observations:
        style_counts.update(detect_style_rules(observation))
        for span in extract_substitution_spans(
            observation.normalized_reference, observation.normalized_hypothesis
        ):
            if any(
                span.hypothesis_start < style_end
                and span.hypothesis_end > style_start
                for style_start, style_end in observation.style_spans
            ):
                continue
            source = observation.normalized_hypothesis.value[
                span.hypothesis_start : span.hypothesis_end
            ]
            target = observation.normalized_reference.value[
                span.reference_start : span.reference_end
            ]
            if source and target and source != target:
                observed_pairs.add((source, target))

    candidates: list[Candidate] = []
    for normalized_source, normalized_target in sorted(observed_pairs):
        occurrences = target_count = harm = 0
        supporting_items: set[tuple[str, str]] = set()
        surface_pairs: Counter[tuple[str, str]] = Counter()
        contexts: list[str] = []
        for observation in observations:
            hyp = observation.normalized_hypothesis.value
            ref = observation.normalized_reference.value
            mapping = observation.hypothesis_to_reference
            for start, end in _find_occurrences(hyp, normalized_source):
                occurrences += 1
                indices = mapping[start:end]
                if not indices or any(index is None for index in indices):
                    continue
                ref_indices = [index for index in indices if index is not None]
                if ref_indices != list(
                    range(ref_indices[0], ref_indices[0] + len(ref_indices))
                ):
                    continue
                ref_start, ref_end = ref_indices[0], ref_indices[-1] + 1
                reference_surface = ref[ref_start:ref_end]
                if reference_surface == normalized_source:
                    harm += 1
                if reference_surface != normalized_target:
                    continue
                target_count += 1
                supporting_items.add(observation.item_key)
                display_source = observation.normalized_hypothesis.surface(
                    observation.hypothesis, start, end
                )
                display_target = observation.normalized_reference.surface(
                    observation.reference, ref_start, ref_end
                )
                surface_pairs[(display_source, display_target)] += 1
                context = _context_for(
                    observation,
                    start,
                    end,
                    normalized_source,
                    normalized_target,
                    ref_start,
                    ref_end,
                )
                if context not in contexts and len(contexts) < 3:
                    contexts.append(context)

        if not surface_pairs:
            source_surface, target_surface = normalized_source, normalized_target
        else:
            (source_surface, target_surface), _ = min(
                surface_pairs.items(), key=lambda pair: (-pair[1], pair[0])
            )
        precision = target_count / occurrences if occurrences else 0.0
        candidates.append(
            Candidate(
                from_=source_surface,
                to=target_surface,
                count=target_count,
                support=len(supporting_items),
                precision_proxy=precision,
                harm=harm,
                occurrences=occurrences,
                contexts=contexts,
                hotword_exempt=normalized_target in hotword_norms,
            )
        )
    return candidates, dict(style_counts)


def source_length_is_allowed(source: str) -> bool:
    normalized = normalize(source)
    cjk_count = sum(_is_cjk(char) for char in normalized)
    if cjk_count >= MIN_SPAN_CHARS:
        return True
    latin_word = all(
        ("LATIN" in unicodedata.name(char, "") and char.isalpha())
        for char in normalized
    )
    return (
        len(normalized) >= MIN_SPAN_CHARS
        and latin_word
    )


def _is_decimal_digit(char: str) -> bool:
    return unicodedata.category(char) == "Nd"


def _numeric_ratio(text: str) -> float:
    if not text:
        return 0.0
    numeric_chars = sum(
        _is_decimal_digit(char) or char in _CJK_NUMERALS for char in text
    )
    return numeric_chars / len(text)


def is_number_rule(source: str, target: str) -> bool:
    """Identify number formatting by target digits or paired numeric spans.

    A digit in the target is always a style-only signal. Otherwise both sides
    must contain numerals/digits in at least half of their characters.
    """

    return any(_is_decimal_digit(char) for char in target) or (
        _numeric_ratio(source) >= 0.5 and _numeric_ratio(target) >= 0.5
    )


def _is_number_formatting_style(source: str, target: str) -> bool:
    """Keep formatting choices and number-shaped source runs out of replacements."""

    has_source_digit = any(_is_decimal_digit(char) for char in source)
    mostly_cjk_source = sum(char in _CJK_NUMERALS for char in source) * 2 > len(source)
    mostly_cjk_target = sum(char in _CJK_NUMERALS for char in target) * 2 > len(target)
    return (
        has_source_digit
        or is_number_rule(source, target)
        or mostly_cjk_source
        or mostly_cjk_target
    )


def filter_candidates(
    candidates: list[Candidate],
    *,
    stoplist: set[str] | None = None,
) -> tuple[list[Candidate], list[Candidate]]:
    blocked = BUILTIN_STOPLIST | {normalize(word) for word in (stoplist or set())}
    safe: list[Candidate] = []
    review: list[Candidate] = []
    for candidate in candidates:
        if _is_number_formatting_style(candidate.from_, candidate.to):
            continue
        normalized_source = normalize(candidate.from_)
        if not source_length_is_allowed(candidate.from_):
            continue
        if normalized_source in blocked:
            continue
        if candidate.harm != 0 or candidate.precision_proxy < MIN_PRECISION:
            continue
        frequent = candidate.count >= MIN_COUNT and candidate.support >= MIN_SUPPORT
        if frequent or candidate.hotword_exempt:
            safe.append(candidate)
        else:
            review.append(candidate)

    sort_key = lambda item: (
        -item.count,
        -item.support,
        -item.precision_proxy,
        item.from_,
        item.to,
    )
    return sorted(safe, key=sort_key), sorted(review, key=sort_key)


def load_wordlist(path: Path | None, *, keys: tuple[str, ...]) -> set[str]:
    if path is None:
        return set()
    text = path.read_text(encoding="utf-8")
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        document = {}
    for key in keys:
        words = document.get(key)
        if isinstance(words, list):
            return {word.strip() for word in words if isinstance(word, str) and word.strip()}
    return {
        line.split("#", 1)[0].strip()
        for line in text.splitlines()
        if line.split("#", 1)[0].strip()
    }


def read_hypothesis(path: Path) -> dict[str, str]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or any(
        not isinstance(item_id, str) or not isinstance(text, str)
        for item_id, text in value.items()
    ):
        raise TypeError(f"{path}: hypothesis JSON must map string ids to text")
    return value


def collect_observations(
    answer_paths: list[Path],
    hypothesis_paths: list[Path],
    *,
    include_unclear: bool = False,
) -> tuple[list[Observation], int]:
    hypotheses = [(path, read_hypothesis(path)) for path in hypothesis_paths]
    observations: list[Observation] = []
    seen_items: set[tuple[str, str]] = set()
    missing_hypotheses = 0
    for answer_path in answer_paths:
        answer = load_reference(answer_path)
        for item in answer["items"]:
            if bool(item.get("unclear", False)) and not include_unclear:
                continue
            set_name = str(item.get("set", answer.get("set", "unknown")))
            item_id = item["id"]
            item_key = (set_name, item_id)
            if item_key in seen_items:
                raise ValueError(f"duplicate answer item: {set_name}/{item_id}")
            seen_items.add(item_key)
            reference = item["reference"]
            for _, hypothesis_map in hypotheses:
                if item_id not in hypothesis_map:
                    missing_hypotheses += 1
                hypothesis = hypothesis_map.get(item_id, "")
                normalized_reference = normalize_with_spans(reference)
                normalized_hypothesis = normalize_with_spans(hypothesis)
                observations.append(
                    Observation(
                        set_name=set_name,
                        item_id=item_id,
                        reference=reference,
                        hypothesis=hypothesis,
                        normalized_reference=normalized_reference,
                        normalized_hypothesis=normalized_hypothesis,
                        steps=align_characters(
                            normalized_reference.value, normalized_hypothesis.value
                        ),
                    )
                )
    return observations, missing_hypotheses


def render_candidates_toml(candidates: list[Candidate]) -> str:
    lines = [
        "# Mined suggestions. Review each replacement before using it.",
        'domain = "Mined replacement candidates; review before use."',
        "hotwords = []",
    ]
    for candidate in candidates:
        lines.extend(
            [
                "",
                (
                    f"# count={candidate.count} support={candidate.support} "
                    f"precision_proxy={candidate.precision_proxy:.3f} harm={candidate.harm}"
                ),
                "[[replacements]]",
                f"from = {json.dumps(candidate.from_, ensure_ascii=False)}",
                f"to = {json.dumps(candidate.to, ensure_ascii=False)}",
            ]
        )
    return "\n".join(lines) + "\n"


def inspect_merge(
    candidates: list[Candidate], existing_path: Path | None
) -> tuple[list[Candidate], list[tuple[Candidate, str]]]:
    if existing_path is None:
        return [], []
    document = tomllib.loads(existing_path.read_text(encoding="utf-8"))
    existing: dict[str, set[str]] = defaultdict(set)
    for replacement in document.get("replacements", []):
        source = replacement.get("from")
        target = replacement.get("to")
        if isinstance(source, str) and isinstance(target, str):
            existing[source].add(target)
    present: list[Candidate] = []
    conflicts: list[tuple[Candidate, str]] = []
    for candidate in candidates:
        targets = existing.get(candidate.from_, set())
        if candidate.to in targets:
            present.append(candidate)
        elif targets:
            conflicts.extend((candidate, target) for target in sorted(targets))
    return present, conflicts


def _markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _render_candidate_table(candidates: list[Candidate]) -> list[str]:
    lines = [
        "| From | To | Count | Support | Precision proxy | Harm | Contexts |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for candidate in candidates:
        contexts = "<br>".join(_markdown_cell(context) for context in candidate.contexts)
        lines.append(
            f"| {_markdown_cell(candidate.from_)} | {_markdown_cell(candidate.to)} "
            f"| {candidate.count} | {candidate.support} "
            f"| {candidate.precision_proxy:.3f} | {candidate.harm} | {contexts} |"
        )
    if not candidates:
        lines.append("| — | — | 0 | 0 | — | 0 | — |")
    return lines


def render_review_markdown(
    safe: list[Candidate],
    review: list[Candidate],
    style_counts: dict[str, int],
    *,
    answer_paths: list[Path],
    hypothesis_paths: list[Path],
    number_formatting: list[Candidate] | None = None,
    present: list[Candidate] | None = None,
    conflicts: list[tuple[Candidate, str]] | None = None,
) -> str:
    lines = [
        "# Replacement dictionary candidates",
        "",
        "Review all suggestions before adding them to a production dictionary.",
        "",
        f"Answers: {', '.join(str(path) for path in answer_paths)}",
        f"Hypotheses: {', '.join(str(path) for path in hypothesis_paths)}",
        "",
        "## Style conventions (reported separately; not dictionary candidates)",
        "",
        "| Convention | Count |",
        "| --- | ---: |",
    ]
    for source, target, kind in STYLE_RULES:
        label = f"{source}→{target}"
        if kind == "God pronoun":
            label += " (context-dependent God-pronoun convention)"
        lines.append(f"| {label} | {style_counts.get(f'{source}→{target}', 0)} |")
    lines.extend(
        [
            "",
            "## Number formatting (style)",
            "",
            "These pairs may differ only in how a number is written; they are never replacement suggestions.",
            "",
        ]
    )
    lines.extend(_render_candidate_table(number_formatting or []))
    lines.extend(["", "## Safe to auto-suggest by the configured filters", ""])
    lines.extend(_render_candidate_table(safe))
    lines.extend(
        [
            "",
            "## Review: only count or distinct-item support is below threshold",
            "",
        ]
    )
    lines.extend(_render_candidate_table(review))
    if present is not None or conflicts is not None:
        lines.extend(["", "## Existing dictionary comparison", ""])
        lines.append("Already present:")
        if present:
            lines.extend(
                f"- {candidate.from_} → {candidate.to}" for candidate in present
            )
        else:
            lines.append("- None")
        lines.append("")
        lines.append("Conflicts (same from, different to):")
        if conflicts:
            lines.extend(
                f"- {candidate.from_} → {candidate.to} conflicts with {target}"
                for candidate, target in conflicts
            )
        else:
            lines.append("- None")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--answers",
        type=Path,
        action="append",
        required=True,
        help="answer-kit JSON; repeat to aggregate sets",
    )
    parser.add_argument(
        "--hypothesis",
        type=Path,
        action="append",
        required=True,
        help="hypothesis id-to-text JSON; repeat to aggregate runs",
    )
    parser.add_argument("--candidates-toml", type=Path, required=True)
    parser.add_argument("--review-md", type=Path, required=True)
    parser.add_argument("--hotwords-file", type=Path)
    parser.add_argument("--stoplist-file", type=Path)
    parser.add_argument("--merge-with", type=Path)
    parser.add_argument(
        "--include-unclear",
        action="store_true",
        help="include items marked unclear (default follows cer_eval and skips them)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    observations, missing = collect_observations(
        args.answers, args.hypothesis, include_unclear=args.include_unclear
    )
    hotwords = load_wordlist(args.hotwords_file, keys=("hotwords", "words"))
    stoplist = load_wordlist(args.stoplist_file, keys=("stoplist", "words"))
    mined, style_counts = mine_candidates(observations, hotwords=hotwords)
    number_formatting = [
        candidate
        for candidate in mined
        if _is_number_formatting_style(candidate.from_, candidate.to)
    ]
    safe, review = filter_candidates(mined, stoplist=stoplist)
    present, conflicts = inspect_merge(safe + review, args.merge_with)

    args.candidates_toml.parent.mkdir(parents=True, exist_ok=True)
    args.review_md.parent.mkdir(parents=True, exist_ok=True)
    args.candidates_toml.write_text(render_candidates_toml(safe), encoding="utf-8")
    args.review_md.write_text(
        render_review_markdown(
            safe,
            review,
            style_counts,
            answer_paths=args.answers,
            hypothesis_paths=args.hypothesis,
            number_formatting=number_formatting,
            present=present if args.merge_with else None,
            conflicts=conflicts if args.merge_with else None,
        ),
        encoding="utf-8",
    )
    print(
        f"safe={len(safe)} review={len(review)} already_present={len(present)} "
        f"conflicts={len(conflicts)} missing_hypothesis_items={missing}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
