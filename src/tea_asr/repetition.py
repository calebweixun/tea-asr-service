"""Trim unusually long, immediately repeated text units from ASR output."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

_CJK_NUMERALS = frozenset("〇零一二三四五六七八九十百千萬億")
_TOKEN_JOINERS = frozenset("_.-@/:#")


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


def _can_trim_repeat_run(text: str, start: int, end: int, unit: str) -> bool:
    """Keep digit-bearing runs and text tokens intact."""

    if any(unicodedata.category(char) == "Nd" or char in _CJK_NUMERALS for char in unit):
        return False
    if start > 0 and unicodedata.category(text[start - 1]) == "Nd":
        return False
    if end < len(text) and unicodedata.category(text[end]) == "Nd":
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
) -> tuple[str, tuple[TrimmedRepetition, ...]]:
    """Keep only the configured number of consecutive copies of 1–6 chars.

    Punctuation and whitespace between copies do not break a run. The
    separators between retained copies stay in the output; separators leading
    into discarded copies are removed with those copies.
    """

    if single_char_limit < 1 or multi_char_limit < 1:
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
            limit = single_char_limit if unit_length == 1 else multi_char_limit
            if len(ends) > limit:
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
