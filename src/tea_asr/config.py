from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AppPaths:
    support: Path
    logs: Path

    @classmethod
    def macos_default(cls) -> AppPaths:
        home = Path.home()
        return cls(
            support=home / "Library" / "Application Support" / "TEA ASR",
            logs=home / "Library" / "Logs" / "TEA ASR",
        )

    @property
    def token_file(self) -> Path:
        return self.support / "token"


def load_or_create_token(paths: AppPaths | None = None) -> str:
    paths = paths or AppPaths.macos_default()
    paths.support.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(paths.support, 0o700)
    except OSError:
        pass
    if paths.token_file.exists():
        token = paths.token_file.read_text().strip()
        if len(token) < 32:
            raise RuntimeError(f"Token file is invalid: {paths.token_file}")
        return token
    token = secrets.token_urlsafe(32)
    fd = os.open(paths.token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(token + "\n")
    return token


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    """Runtime switches that change what the service is allowed to claim.

    `revisable_preview` stays off until the P2a acceptance in docs/07 passes.
    While it is off the server answers `unsupported_option` instead of quietly
    downgrading, and capabilities keeps `partial_transcripts=false`.
    """

    revisable_preview: bool = False
    max_total_connections: int = 4

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ServiceConfig:
        source = os.environ if env is None else env
        return cls(
            revisable_preview=source.get("TEA_ASR_EXPERIMENTAL_REVISABLE_PREVIEW") == "1",
        )

    @property
    def protocol_version(self) -> str:
        return "1.1" if self.revisable_preview else "1.0"
