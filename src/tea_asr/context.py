"""Recognition hints, server dictionaries, and deterministic replacements."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import tempfile
import threading
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
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


class DictionaryRevisionConflict(Exception):
    """An edit used a stale base revision."""

    def __init__(self, current_revision: str | None) -> None:
        super().__init__("dictionary revision changed")
        self.current_revision = current_revision


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


@dataclass(frozen=True, slots=True)
class DictionarySnapshot:
    dictionary: _Dictionary
    revision: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class DictionaryDeleteResult:
    revision: str
    updated_at: str
    hotwords_count: int | None
    replacements_count: int | None


DICTIONARY_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def is_valid_dictionary_name(name: str) -> bool:
    return DICTIONARY_NAME_PATTERN.fullmatch(name) is not None


def _updated_at(mtime_ns: int) -> str:
    timestamp = datetime.fromtimestamp(mtime_ns / 1_000_000_000, tz=UTC)
    return timestamp.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_dictionary(name: str, raw: bytes) -> _Dictionary:
    if len(raw) > MAX_CONTEXT_DICTIONARY_BYTES:
        raise InvalidDictionary(name, "file exceeds 1 MiB limit")
    try:
        document = ContextDictionaryFile.model_validate(
            tomllib.loads(raw.decode("utf-8"))
        )
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValidationError) as exc:
        raise InvalidDictionary(name) from exc
    return _Dictionary(
        name=name,
        domain=document.domain,
        hotwords=tuple(document.hotwords),
        replacements=tuple(
            ReplacementRule(rule.from_, rule.to) for rule in document.replacements
        ),
    )


class ContextDictionaryStore:
    """Read support dictionaries on demand and reuse them only while unchanged."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._cache: dict[str, tuple[tuple[int, int], _Dictionary]] = {}
        self._mutation_lock = threading.RLock()

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
        except OSError as exc:
            raise InvalidDictionary(name, "could not read dictionary") from exc
        dictionary = _parse_dictionary(name, raw)
        self._cache[name] = (signature, dictionary)
        return dictionary

    def snapshot(self, name: str) -> DictionarySnapshot:
        """Read and validate the current bytes, bypassing the session cache."""

        if not name or name in {".", ".."} or "/" in name or "\\" in name or "\0" in name:
            raise UnknownDictionaryProfile(name)
        path = self.directory / f"{name}.toml"
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError as exc:
            raise UnknownDictionaryProfile(name) from exc
        except OSError as exc:
            if path.is_symlink():
                raise InvalidDictionary(name, "symlink is not allowed") from exc
            raise InvalidDictionary(name, "could not read dictionary") from exc
        with os.fdopen(descriptor, "rb") as dictionary_file:
            file_stat = os.fstat(dictionary_file.fileno())
            if not stat.S_ISREG(file_stat.st_mode):
                raise UnknownDictionaryProfile(name)
            raw = dictionary_file.read(MAX_CONTEXT_DICTIONARY_BYTES + 1)
        dictionary = _parse_dictionary(name, raw)
        return DictionarySnapshot(
            dictionary=dictionary,
            revision=hashlib.sha256(raw).hexdigest(),
            updated_at=_updated_at(file_stat.st_mtime_ns),
        )

    def write(
        self,
        name: str,
        document: ContextDictionaryFile,
        *,
        base_revision: str | None,
        check_base_revision: bool,
    ) -> DictionarySnapshot:
        if not is_valid_dictionary_name(name):
            raise ValueError("invalid dictionary name")
        raw = _canonical_toml(document)
        if len(raw) > MAX_CONTEXT_DICTIONARY_BYTES:
            raise InvalidDictionary(name, "file exceeds 1 MiB limit")
        with self._mutation_lock:
            self._ensure_directory()
            path = self.directory / f"{name}.toml"
            exists = path.exists() or path.is_symlink()
            if path.is_symlink():
                raise InvalidDictionary(name, "symlink is not allowed")
            if exists and not path.is_file():
                raise InvalidDictionary(name, "dictionary path is not a regular file")
            current_revision = _sha256_file(path) if exists else None
            if check_base_revision and base_revision != current_revision:
                raise DictionaryRevisionConflict(current_revision)
            if exists:
                self._copy_to_history(name, path)
            self._atomic_write(path, raw)
            if exists:
                self._rotate_history(name)
            self._cache.pop(name, None)
            return self.snapshot(name)

    def delete(self, name: str) -> DictionaryDeleteResult:
        if not is_valid_dictionary_name(name):
            raise ValueError("invalid dictionary name")
        with self._mutation_lock:
            path = self.directory / f"{name}.toml"
            try:
                file_stat = path.lstat()
            except FileNotFoundError as exc:
                raise UnknownDictionaryProfile(name) from exc
            if stat.S_ISLNK(file_stat.st_mode):
                raise InvalidDictionary(name, "symlink is not allowed")
            if not stat.S_ISREG(file_stat.st_mode):
                raise UnknownDictionaryProfile(name)
            revision = _sha256_file(path)
            updated_at = _updated_at(file_stat.st_mtime_ns)
            try:
                snapshot = self.snapshot(name)
            except InvalidDictionary:
                hotwords_count = replacements_count = None
            else:
                hotwords_count = len(snapshot.dictionary.hotwords)
                replacements_count = len(snapshot.dictionary.replacements)
            history_path = self._history_path(name)
            os.replace(path, history_path)
            _fsync_directory(self.directory)
            _fsync_directory(history_path.parent)
            self._rotate_history(name)
            self._cache.pop(name, None)
            return DictionaryDeleteResult(
                revision=revision,
                updated_at=updated_at,
                hotwords_count=hotwords_count,
                replacements_count=replacements_count,
            )

    def _ensure_directory(self) -> None:
        if self.directory.is_symlink():
            raise OSError("dictionaries directory must not be a symlink")
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise OSError("dictionaries path is not a directory")

    def _history_directory(self) -> Path:
        history = self.directory / ".history"
        if history.is_symlink():
            raise OSError("dictionary history directory must not be a symlink")
        history.mkdir(mode=0o700, exist_ok=True)
        if history.is_symlink() or not history.is_dir():
            raise OSError("dictionary history path is not a directory")
        return history

    def _history_path(self, name: str) -> Path:
        history = self._history_directory()
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        candidate = history / f"{name}-{stamp}.toml"
        suffix = 1
        while candidate.exists():
            candidate = history / f"{name}-{stamp}-{suffix}.toml"
            suffix += 1
        return candidate

    def _copy_to_history(self, name: str, source: Path) -> None:
        destination = self._history_path(name)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{name}-", suffix=".tmp", dir=destination.parent
        )
        source_descriptor = -1
        try:
            source_descriptor = os.open(
                source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
            with os.fdopen(source_descriptor, "rb") as source_file, os.fdopen(
                descriptor, "wb"
            ) as target_file:
                while chunk := source_file.read(64 * 1024):
                    target_file.write(chunk)
                target_file.flush()
                os.fsync(target_file.fileno())
            os.replace(temporary_name, destination)
            _fsync_directory(destination.parent)
        except BaseException:
            if source_descriptor >= 0:
                try:
                    os.close(source_descriptor)
                except OSError:
                    pass
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

    def _atomic_write(self, path: Path, raw: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as temporary_file:
                temporary_file.write(raw)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_name, path)
            _fsync_directory(path.parent)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

    def _rotate_history(self, name: str) -> None:
        history = self.directory / ".history"
        entries = sorted(
            history.glob(f"{name}-*.toml"),
            key=lambda item: item.name,
            reverse=True,
        )
        for old in entries[30:]:
            old.unlink()
        if len(entries) > 30:
            _fsync_directory(history)

    def _invalid_summary(
        self,
        name: str,
        reason: str,
        *,
        revision: str | None,
        updated_at: str | None,
    ) -> dict[str, str | None]:
        event(logger, "context.dictionary_invalid", level="warning", name=name, reason=reason)
        return {
            "name": name,
            "error": reason,
            "revision": revision,
            "updated_at": updated_at,
        }

    def summaries(self) -> list[dict[str, str | int | None]]:
        if not self.directory.is_dir():
            return []
        summaries: list[dict[str, str | int | None]] = []
        for path in sorted(self.directory.glob("*.toml")):
            if path.is_symlink():
                revision, updated_at = _file_metadata(path)
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
                summaries.append(
                    self._invalid_summary(
                        path.stem, reason, revision=revision, updated_at=updated_at
                    )
                )
                continue
            if not path.is_file():
                continue
            try:
                snapshot = self.snapshot(path.stem)
            except UnknownDictionaryProfile:
                continue
            except InvalidDictionary as exc:
                revision, updated_at = _file_metadata(path)
                summaries.append(
                    self._invalid_summary(
                        path.stem, exc.reason, revision=revision, updated_at=updated_at
                    )
                )
                continue
            dictionary = snapshot.dictionary
            summaries.append(
                {
                    "name": dictionary.name,
                    "domain": dictionary.domain,
                    "hotwords_count": len(dictionary.hotwords),
                    "replacements_count": len(dictionary.replacements),
                    "revision": snapshot.revision,
                    "updated_at": snapshot.updated_at,
                }
            )
        return summaries


def _canonical_toml(document: ContextDictionaryFile) -> bytes:
    def quote(value: str) -> str:
        return json.dumps(value, ensure_ascii=False)

    lines = [
        "# TEA ASR dictionary, written by the server API.",
        f"domain = {quote(document.domain)}",
        "hotwords = [" + ", ".join(quote(word) for word in document.hotwords) + "]",
    ]
    for replacement in document.replacements:
        lines.extend(
            [
                "",
                "[[replacements]]",
                f"from = {quote(replacement.from_)}",
                f"to = {quote(replacement.to)}",
            ]
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise OSError("dictionary path is not a regular file")
        while chunk := file.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _file_metadata(path: Path) -> tuple[str | None, str | None]:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as file:
            file_stat = os.fstat(file.fileno())
            updated_at = _updated_at(file_stat.st_mtime_ns)
            if not stat.S_ISREG(file_stat.st_mode):
                return None, updated_at
            digest = hashlib.sha256()
            while chunk := file.read(64 * 1024):
                digest.update(chunk)
            return digest.hexdigest(), updated_at
    except OSError:
        try:
            file_stat = path.lstat()
        except OSError:
            return None, None
        return None, _updated_at(file_stat.st_mtime_ns)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
    text: str,
    rules: tuple[ReplacementRule, ...],
    *,
    counts: dict[tuple[str, str], int] | None = None,
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
                if counts is not None:
                    key = (rule.source, rule.target)
                    counts[key] = counts.get(key, 0) + 1
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
