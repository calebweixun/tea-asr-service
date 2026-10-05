"""Append-only stable prefix for live subtitles (LocalAgreement-n).

`transcript.partial` replaces the whole segment text on every revision, so a
subtitle that renders partials jumps whenever the model changes its mind. This
module derives a second, append-only view of the same segment: the longest
prefix that the last ``agreement`` partials all agree on is *committed* and is
never changed afterwards. The method is LocalAgreement-n from Whisper-Streaming
(Macháček et al., 2023); measurements that justify ``agreement`` and the final
rules are in docs/benchmarks/stable-prefix-report.md.

Committed text is only ever cut at a position that is safe in *every*
hypothesis that voted for it:

- an extended grapheme cluster boundary (UAX #29), so an emoji ZWJ sequence, a
  flag, a keycap, or a base letter plus combining marks is never split. The
  check below is a deliberately conservative subset of UAX #29 written with the
  standard library only: it may refuse a cut UAX #29 allows (the text is then
  committed one partial later), but it never allows a cut UAX #29 forbids.
- not inside a run of narrow (non-CJK) letters or digits, so an English word or
  a number is committed whole, never ``iPh`` of ``iPhone`` or ``202`` of
  ``2026``.
- punctuation is ignored for agreement and emitted only when every voting
  hypothesis has the same mark sequence at that text position. This lets
  content pass a comma flip without committing a mark that may move.

The wire contract keeps docs/07's "full text, never offsets" rule: events carry
the whole committed prefix, and each new value starts with the previous one.
"""

from __future__ import annotations

import unicodedata
from collections import deque
from dataclasses import dataclass
from typing import Literal

#: Agreement levels a session may ask for; both were measured.
SUPPORTED_AGREEMENTS: tuple[int, ...] = (2, 3)
DEFAULT_AGREEMENT = 2

#: Upper bound on the alignment table built when a final diverges. A 30 s
#: utterance is ~200 characters, i.e. ~40k cells; anything past this falls back
#: to a length-based guess instead of spending event-loop time.
MAX_ALIGNMENT_CELLS = 250_000

ZWJ = "\u200d"

_PREPEND = frozenset(
    [
        *range(0x0600, 0x0606),
        0x06DD,
        0x070F,
        0x0890,
        0x0891,
        0x08E2,
        0x0D4E,
        0x110BD,
        0x110CD,
        0x111C2,
        0x111C3,
        0x1193F,
        0x11941,
        0x11A3A,
        *range(0x11A84, 0x11A8A),
        0x11D46,
        0x11F02,
    ]
)

#: Lo characters that UAX #29 classifies as SpacingMark.
_SPACING_LO = frozenset({0x0E33, 0x0EB3})

#: Word-internal joiners: never cut on either side of these between letters.
_WORD_JOINERS = frozenset("'’")


def _is_extend(char: str) -> bool:
    """Characters that attach to the preceding cluster (Extend/ZWJ/SpacingMark)."""

    cp = ord(char)
    if unicodedata.category(char) in {"Mn", "Me", "Mc"}:
        return True
    return (
        cp in (0x200C, 0x200D)
        or 0xFE00 <= cp <= 0xFE0F
        or 0xE0100 <= cp <= 0xE01EF
        or 0x1F3FB <= cp <= 0x1F3FF
        or 0xE0020 <= cp <= 0xE007F
        or 0xFF9E <= cp <= 0xFF9F
        or cp in _SPACING_LO
    )


def _is_regional_indicator(char: str) -> bool:
    return 0x1F1E6 <= ord(char) <= 0x1F1FF


def _hangul_kind(char: str) -> str | None:
    cp = ord(char)
    if 0x1100 <= cp <= 0x115F or 0xA960 <= cp <= 0xA97C:
        return "L"
    if 0x1160 <= cp <= 0x11A7 or 0xD7B0 <= cp <= 0xD7C6:
        return "V"
    if 0x11A8 <= cp <= 0x11FF or 0xD7CB <= cp <= 0xD7FB:
        return "T"
    if 0xAC00 <= cp <= 0xD7A3:
        return "LV"
    return None


def is_grapheme_boundary(text: str, index: int) -> bool:
    """True when cutting ``text`` at ``index`` cannot split a grapheme cluster.

    ``index`` 0 and ``len(text)`` are always boundaries. Conservative: see the
    module docstring.
    """

    if index <= 0 or index >= len(text):
        return True
    before, after = text[index - 1], text[index]
    if before == "\r" and after == "\n":
        return False
    if _is_extend(after) or before == ZWJ:
        return False
    if ord(before) in _PREPEND:
        return False
    if unicodedata.combining(before) == 9 and unicodedata.category(after) == "Lo":
        # GB9c: a virama joins the following consonant into one cluster.
        return False
    kind_after = _hangul_kind(after)
    if kind_after in {"V", "T"}:
        return False
    if _hangul_kind(before) == "L" and kind_after in {"L", "LV"}:
        return False
    if _is_regional_indicator(after) and _is_regional_indicator(before):
        run = 0
        position = index - 1
        while position >= 0 and _is_regional_indicator(text[position]):
            run += 1
            position -= 1
        if run % 2 == 1:
            return False
    return True


def _is_narrow_word_char(char: str) -> bool:
    return char.isalnum() and unicodedata.east_asian_width(char) not in {"W", "F"}


def _splits_word(text: str, index: int) -> bool:
    before, after = text[index - 1], text[index]
    if _is_narrow_word_char(before) and _is_narrow_word_char(after):
        return True
    if after in _WORD_JOINERS and _is_narrow_word_char(before):
        return True
    return (
        before in _WORD_JOINERS
        and _is_narrow_word_char(after)
        and index >= 2
        and _is_narrow_word_char(text[index - 2])
    )


def _is_soft(char: str) -> bool:
    """Punctuation or whitespace: never the last committed character."""

    return char.isspace() or unicodedata.category(char)[0] in {"P", "Z"}


def common_prefix_length(texts: list[str] | tuple[str, ...]) -> int:
    if not texts:
        return 0
    shortest = min(len(text) for text in texts)
    first = texts[0]
    for index in range(shortest):
        char = first[index]
        if any(text[index] != char for text in texts[1:]):
            return index
    return shortest


def is_safe_cut(hypotheses: list[str] | tuple[str, ...], index: int) -> bool:
    """A cut at ``index`` of the shared prefix is safe in every hypothesis."""

    if index <= 0:
        return True
    reference = hypotheses[0]
    if _is_soft(reference[index - 1]):
        return False
    continued = False
    for text in hypotheses:
        if index < len(text):
            continued = True
            if not is_grapheme_boundary(text, index) or _splits_word(text, index):
                return False
    # Every hypothesis ends exactly here: an English word or number at the very
    # end of the audio may still be growing, so wait for the next partial.
    return continued or not _is_narrow_word_char(reference[index - 1])


_CJK_DIGITS = dict(zip("〇零一二三四五六七八九", "00123456789", strict=True))


@dataclass(frozen=True, slots=True)
class _SurfacePart:
    start: int
    end: int
    text: str
    kind: Literal["content", "punctuation", "space"]
    key_position: int


@dataclass(frozen=True, slots=True)
class _ComparisonView:
    key: str
    parts: tuple[_SurfacePart, ...]
    # A key boundary maps to a surface offset only when it does not split a
    # grapheme cluster or a normalization expansion.
    boundaries: tuple[int | None, ...]
    punctuation: tuple[str, ...]


def _comparison_form(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    return unicodedata.normalize("NFKC", normalized.casefold())


def _is_space(char: str) -> bool:
    return char.isspace() or unicodedata.category(char).startswith("Z")


def _comparison_view(text: str) -> _ComparisonView:
    """Build a punctuation-free comparison key with offsets into ``text``."""

    clusters: list[tuple[int, int, str, str]] = []
    start = 0
    for end in range(1, len(text) + 1):
        if is_grapheme_boundary(text, end):
            surface = text[start:end]
            clusters.append((start, end, surface, _comparison_form(surface)))
            start = end

    key_parts: list[str] = []
    parts: list[_SurfacePart] = []
    boundaries: list[int | None] = [0]
    punctuation: list[list[str]] = [[]]

    for start, end, surface, normalized in clusters:
        position = len(key_parts)
        if normalized and all(_is_space(char) for char in normalized):
            parts.append(_SurfacePart(start, end, surface, "space", position))
            continue
        if normalized and all(unicodedata.category(char).startswith("P") for char in normalized):
            parts.append(
                _SurfacePart(start, end, surface, "punctuation", position)
            )
            punctuation[position].append(normalized)
            continue

        # Fold simple digit glyphs. Unit-based Chinese numbers (十、百、千…) are
        # deliberately left alone rather than parsed with ambiguous rules.
        fragment = "".join(
            _CJK_DIGITS.get(char, char)
            for char in normalized
            if not _is_space(char) and not unicodedata.category(char).startswith("P")
        )
        parts.append(_SurfacePart(start, end, surface, "content", position))
        if not fragment:
            continue
        key_parts.extend(fragment)
        boundaries.extend([None] * (len(fragment) - 1))
        boundaries.append(end)
        punctuation.extend([] for _ in fragment)

    return _ComparisonView(
        key="".join(key_parts),
        parts=tuple(parts),
        boundaries=tuple(boundaries),
        punctuation=tuple("".join(marks) for marks in punctuation),
    )


def comparison_key(text: str) -> str:
    """Case/width/spacing/punctuation-insensitive key used by stable tracking."""

    return _comparison_view(text).key


def _project_surface(
    view: _ComparisonView,
    votes: tuple[_ComparisonView, ...],
    start_key: int,
    end_key: int,
) -> str:
    """Project an agreed key range into the latest voted surface text."""

    start = view.boundaries[start_key]
    end = view.boundaries[end_key]
    if start is None or end is None:
        return ""
    projected: list[str] = []
    for part in view.parts:
        if part.end <= start or part.start >= end:
            continue
        if part.kind == "punctuation":
            position = part.key_position
            if not start_key <= position < end_key:
                continue
            signature = view.punctuation[position]
            if any(vote.punctuation[position] != signature for vote in votes):
                continue
        projected.append(part.text)
    return "".join(projected)


def stable_cut(hypotheses: list[str] | tuple[str, ...], floor: int = 0) -> int:
    """Longest safe committed length the hypotheses agree on, or ``floor``."""

    index = common_prefix_length(hypotheses)
    while index > floor and not is_safe_cut(hypotheses, index):
        index -= 1
    return max(index, floor)


def _alignment_end(committed: str, final: str) -> int:
    """Where in ``final`` the committed text most plausibly ends.

    Minimises the edit distance between ``committed`` and ``final[:j]``; ties go
    to the longest ``j`` so a substituted character is not repeated.
    """

    rows, cols = len(committed), len(final)
    if rows * cols > MAX_ALIGNMENT_CELLS:
        return min(rows, cols)
    previous = list(range(cols + 1))
    for i in range(1, rows + 1):
        current = [i] + [0] * cols
        a = committed[i - 1]
        for j in range(1, cols + 1):
            current[j] = min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (a != final[j - 1]),
            )
        previous = current
    best = min(previous)
    return max(j for j, cost in enumerate(previous) if cost == best)


StableState = Literal["open", "final", "diverged", "abandoned"]


@dataclass(frozen=True, slots=True)
class StableUpdate:
    text: str
    state: StableState
    #: Committed comparison-key characters the final does not share from the
    #: start (0 unless ``state == "diverged"``).
    diverged_chars: int = 0


class StablePrefixTracker:
    """Committed, append-only text for one segment.

    Feed it every published partial with :meth:`observe`, then exactly one of
    :meth:`finalize` (a `transcript.final` arrived) or :meth:`abandon` (the
    segment ended without one). ``text`` never shrinks and every returned
    update's text starts with the previous one.
    """

    def __init__(self, agreement: int = DEFAULT_AGREEMENT) -> None:
        if agreement < 2:
            raise ValueError("agreement must be at least 2")
        self.agreement = agreement
        self._history: deque[str] = deque(maxlen=agreement)
        self.text = ""
        self.closed = False

    def observe(self, hypothesis: str) -> StableUpdate | None:
        """Record one partial; return an update only when the prefix grew."""

        if self.closed:
            return None
        self._history.append(hypothesis)
        if len(self._history) < self.agreement:
            return None
        hypotheses = tuple(self._history)
        views = tuple(_comparison_view(text) for text in hypotheses)
        floor = comparison_key(self.text)
        if any(not view.key.startswith(floor) for view in views):
            # The model moved away from what is already on screen. Nothing can
            # be retracted, so wait (the final decides how the line ends).
            return None
        common = common_prefix_length(tuple(view.key for view in views))
        for length in range(common, len(floor), -1):
            offsets = tuple(view.boundaries[length] for view in views)
            if any(
                offset is None or not is_safe_cut((hypothesis,), offset)
                for hypothesis, offset in zip(hypotheses, offsets, strict=True)
            ):
                continue
            addition = _project_surface(views[-1], views, len(floor), length)
            if not addition:
                return None
            self.text += addition
            return StableUpdate(self.text, "open")
        return None

    def finalize(self, final: str) -> StableUpdate:
        """Close the segment with its `transcript.final` text.

        A final whose normalized key extends the committed key is reconciled
        without a divergence, even if its case, width, spacing, or punctuation
        differs. The visible committed surface is retained and the final suffix
        is appended after the equivalent key boundary. A genuinely different
        key keeps the existing append-only aligned-tail behavior.
        """

        self.closed = True
        committed = self.text
        committed_key = comparison_key(committed)
        final_view = _comparison_view(final)
        if final_view.key.startswith(committed_key):
            end = final_view.boundaries[len(committed_key)]
            if end is not None:
                self.text = committed + final[end:]
                return StableUpdate(self.text, "final")

        shared = common_prefix_length((committed_key, final_view.key))
        end_key = _alignment_end(committed_key, final_view.key)
        while end_key < len(final_view.key) and final_view.boundaries[end_key] is None:
            end_key += 1
        end = final_view.boundaries[end_key]
        if end is None:
            end = len(final)
        self.text = committed + final[end:]
        return StableUpdate(
            self.text, "diverged", diverged_chars=len(committed_key) - shared
        )

    def abandon(self) -> StableUpdate:
        """Close the segment without a final (skipped or failed)."""

        self.closed = True
        return StableUpdate(self.text, "abandoned")
