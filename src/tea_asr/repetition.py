"""Trim unusually long, immediately repeated text units from ASR output."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

_CJK_NUMERALS = frozenset("〇零一二三四五六七八九十百千萬億兩")
_TOKEN_JOINERS = frozenset("_.-@/:#")
_NUMERAL_LOOP_RETAINED_COPIES = 3
_SINGLE_DIGIT_LOOP_MINIMUM = 10


@dataclass(frozen=True, slots=True)
class TrimmedRepetition:
    """Counts for one repeated run removed from a hypothesis."""

    unit: str
    unit_length: int
    removed_chars: int


def _is_repeat_separator(char: str) -> bool:
    return char.isspace() or unicodedata.category(char).startswith("P")


def _repeat_ends(text: str, start: int, unit: str) -> list[int]:
    """Return the end offset after each copy in a consecutive repeated run."""

    ends = [start + len(unit)]
    cursor = ends[0]
    while cursor < len(text):
        while cursor < len(text) and _is_repeat_separator(text[cursor]):
            cursor += 1
        if not text.startswith(unit, cursor):
            break
        cursor += len(unit)
        ends.append(cursor)
    return ends


def _is_ascii_letter(char: str) -> bool:
    return char.isascii() and char.isalpha()


def _is_numeral(char: str) -> bool:
    return unicodedata.category(char) == "Nd" or char in _CJK_NUMERALS


def _can_trim_repeat_run(text: str, start: int, end: int, unit: str) -> bool:
    """Keep numeral runs embedded in longer numbers and text tokens intact."""

    if start > 0 and _is_numeral(text[start - 1]):
        return False
    if end < len(text) and _is_numeral(text[end]):
        return False

    if all(_is_ascii_letter(char) for char in unit):
        # Letters glued to other letters or to an identifier/URL connector
        # (`user_aaaa_id`, `www.aaaa.com`) belong to a token, not a loop.
        if start > 0 and (_is_ascii_letter(text[start - 1]) or text[start - 1] in _TOKEN_JOINERS):
            return False
        if end < len(text) and (_is_ascii_letter(text[end]) or text[end] in _TOKEN_JOINERS):
            return False
    return True


def trim_repetitions(
    text: str,
    *,
    single_char_limit: int = 3,
    multi_char_limit: int = 3,
    numeral_loop_limit: int = 6,
) -> tuple[str, tuple[TrimmedRepetition, ...]]:
    """Trim long repeated units while preserving ordinary numbers.

    Punctuation and whitespace between copies do not break a run. The
    separators between retained copies stay in the output; separators leading
    into discarded copies are removed with those copies. Numeral-bearing units
    are kept through ``numeral_loop_limit`` copies and trimmed to three copies
    after that; a single decimal digit needs at least ten copies to qualify.
    """

    if min(single_char_limit, multi_char_limit, numeral_loop_limit) < 1:
        raise ValueError("repetition limits must be at least 1")

    output: list[str] = []
    trims: list[TrimmedRepetition] = []
    index = 0
    while index < len(text):
        match: tuple[int, list[int], int] | None = None
        preserved_end: int | None = None
        for unit_length in range(1, min(6, len(text) - index) + 1):
            unit = text[index : index + unit_length]
            if any(_is_repeat_separator(char) for char in unit):
                continue
            ends = _repeat_ends(text, index, unit)
            is_numeral_unit = any(_is_numeral(char) for char in unit)
            if is_numeral_unit:
                threshold = numeral_loop_limit
                if unit_length == 1 and unicodedata.category(unit) == "Nd":
                    threshold = max(threshold, _SINGLE_DIGIT_LOOP_MINIMUM - 1)
                limit = _NUMERAL_LOOP_RETAINED_COPIES
            else:
                threshold = single_char_limit if unit_length == 1 else multi_char_limit
                limit = threshold
            if len(ends) > max(threshold, limit):
                if not _can_trim_repeat_run(text, index, ends[-1], unit):
                    preserved_end = ends[-1]
                    break
                match = unit_length, ends, limit
                break

        if preserved_end is not None:
            output.append(text[index:preserved_end])
            index = preserved_end
            continue

        if match is None:
            output.append(text[index])
            index += 1
            continue

        unit_length, ends, limit = match
        retained_end = ends[limit - 1]
        run_end = ends[-1]
        output.append(text[index:retained_end])
        trims.append(
            TrimmedRepetition(
                unit=unit,
                unit_length=unit_length,
                removed_chars=run_end - retained_end,
            )
        )
        index = run_end

    return "".join(output), tuple(trims)
