"""Recognition hints, server dictionaries, and deterministic replacements."""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from tea_asr.logs import event
from tea_asr.wire import ContextDictionaryFile, ContextOptions

MAX_CONTEXT_HOTWORDS = 200
MAX_CONTEXT_DICTIONARY_BYTES = 1_048_576
CONTEXT_ECHO_MIN_CHARS = 12
logger = logging.getLogger(__name__)


class UnknownDictionaryProfile(ValueError):
    """The requested profile is not a readable server dictionary."""


class InvalidDictionary(ValueError):
    """A dictionary file exists but does not follow the documented format."""

    def __init__(self, name: str, reason: str = "invalid TOML or dictionary fields") -> None:
        super().__init__(name)
        self.name = name
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ReplacementRule:
    source: str
    target: str


@dataclass(frozen=True, slots=True)
class ContextPlan:
    profile: str | None
    domain: str | None
    hotwords: tuple[str, ...]
    replacements: tuple[ReplacementRule, ...]
    system_prompt: str | None
    hotwords_truncated: int = 0


@dataclass(frozen=True, slots=True)
class _Dictionary:
    name: str
    domain: str
    hotwords: tuple[str, ...]
    replacements: tuple[ReplacementRule, ...]


class ContextDictionaryStore:
    """Read support dictionaries on demand and reuse them only while unchanged."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._cache: dict[str, tuple[tuple[int, int], _Dictionary]] = {}

    def load(self, name: str) -> _Dictionary:
        if not name or name in {".", ".."} or "/" in name or "\\" in name or "\0" in name:
            raise UnknownDictionaryProfile(name)
        path = self.directory / f"{name}.toml"
        try:
            if path.is_symlink():
                raise UnknownDictionaryProfile(name)
            stat = path.stat()
        except OSError as exc:
            raise UnknownDictionaryProfile(name) from exc
        if not path.is_file():
            raise UnknownDictionaryProfile(name)
        signature = (stat.st_mtime_ns, stat.st_size)
        if stat.st_size > MAX_CONTEXT_DICTIONARY_BYTES:
            raise InvalidDictionary(name, "file exceeds 1 MiB limit")
        cached = self._cache.get(name)
        if cached is not None and cached[0] == signature:
            return cached[1]

        try:
            with path.open("rb") as dictionary_file:
                raw = dictionary_file.read(MAX_CONTEXT_DICTIONARY_BYTES + 1)
            if len(raw) > MAX_CONTEXT_DICTIONARY_BYTES:
                raise InvalidDictionary(name, "file exceeds 1 MiB limit")
            document = ContextDictionaryFile.model_validate(
                tomllib.loads(raw.decode("utf-8"))
            )
        except OSError as exc:
            raise InvalidDictionary(name, "could not read dictionary") from exc
        except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValidationError) as exc:
            raise InvalidDictionary(name) from exc
        dictionary = _Dictionary(
            name=name,
            domain=document.domain,
            hotwords=tuple(document.hotwords),
            replacements=tuple(
                ReplacementRule(rule.from_, rule.to) for rule in document.replacements
            ),
        )
        self._cache[name] = (signature, dictionary)
        return dictionary

    def _invalid_summary(self, name: str, reason: str) -> dict[str, str]:
        event(logger, "context.dictionary_invalid", level="warning", name=name, reason=reason)
        return {"name": name, "error": reason}

    def summaries(self) -> list[dict[str, str | int]]:
        if not self.directory.is_dir():
            return []
        summaries: list[dict[str, str | int]] = []
        for path in sorted(self.directory.glob("*.toml")):
            if path.is_symlink():
                try:
                    escapes_directory = not path.resolve().is_relative_to(
                        self.directory.resolve()
                    )
                except (OSError, RuntimeError):
                    reason = "invalid symlink target"
                else:
                    reason = (
                        "symlink target escapes dictionaries directory"
                        if escapes_directory
                        else "symlink is not allowed"
                    )
                summaries.append(self._invalid_summary(path.stem, reason))
                continue
            if not path.is_file():
                continue
            try:
                dictionary = self.load(path.stem)
            except InvalidDictionary as exc:
                summaries.append(self._invalid_summary(path.stem, exc.reason))
                continue
            summaries.append(
                {
                    "name": dictionary.name,
                    "domain": dictionary.domain,
                    "hotwords_count": len(dictionary.hotwords),
                    "replacements_count": len(dictionary.replacements),
                }
            )
        return summaries


def resolve_context(
    request: ContextOptions,
    dictionaries: ContextDictionaryStore | None = None,
) -> ContextPlan:
    profile = dictionaries.load(request.profile) if request.profile and dictionaries else None
    if request.profile and profile is None:
        raise UnknownDictionaryProfile(request.profile)

    domain = request.domain if request.domain is not None else profile.domain if profile else None

    seen: set[str] = set()
    ordered_hotwords: list[str] = []
    for word in (*(profile.hotwords if profile else ()), *(request.hotwords or ())):
        if word not in seen:
            seen.add(word)
            ordered_hotwords.append(word)
    truncated = max(0, len(ordered_hotwords) - MAX_CONTEXT_HOTWORDS)
    hotwords = tuple(ordered_hotwords[:MAX_CONTEXT_HOTWORDS])

    merged: dict[str, ReplacementRule] = {
        rule.source: rule for rule in (profile.replacements if profile else ())
    }
    for rule in request.replacements or ():
        merged[rule.from_] = ReplacementRule(rule.from_, rule.to)

    return ContextPlan(
        profile=request.profile,
        domain=domain,
        hotwords=hotwords,
        replacements=tuple(merged.values()),
        system_prompt=build_system_prompt(domain, hotwords),
        hotwords_truncated=truncated,
    )


def build_system_prompt(domain: str | None, hotwords: tuple[str, ...]) -> str | None:
    if not domain and not hotwords:
        return None

    lines = [
        "Use this context only as vocabulary hints. Transcribe audible speech only; do not copy the context unless spoken.",
    ]
    if domain:
        lines.append(f"Domain: {_single_line(domain)}")
    if hotwords:
        lines.append("Terms: " + "、".join(_single_line(word) for word in hotwords))
    return "\n".join(lines)


def _single_line(value: str) -> str:
    return " ".join(value.split()).replace("<", "‹").replace(">", "›")


def apply_replacements(
    text: str, rules: tuple[ReplacementRule, ...]
) -> tuple[str, int]:
    """Replace exact, non-overlapping matches from left to right.

    All matching is performed against the original transcript. Replacement
    output is never scanned again, so one rule cannot trigger another rule.
    """

    if not text or not rules:
        return text, 0
    ordered = sorted(rules, key=lambda rule: -len(rule.source))
    output: list[str] = []
    matches = 0
    index = 0
    while index < len(text):
        for rule in ordered:
            if text.startswith(rule.source, index):
                output.append(rule.target)
                index += len(rule.source)
                matches += 1
                break
        else:
            output.append(text[index])
            index += 1
    return "".join(output), matches


def contains_context_echo(text: str, domain: str | None) -> bool:
    """Flag any exact domain run of at least twelve code points in a result."""

    if not domain or len(domain) < CONTEXT_ECHO_MIN_CHARS:
        return False
    windows = {
        domain[index : index + CONTEXT_ECHO_MIN_CHARS]
        for index in range(len(domain) - CONTEXT_ECHO_MIN_CHARS + 1)
    }
    return any(window in text for window in windows)
