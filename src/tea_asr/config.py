from __future__ import annotations

import os
import secrets
import tomllib
from dataclasses import dataclass, replace
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

    @property
    def config_file(self) -> Path:
        return self.support / "config.toml"

    @property
    def lock_file(self) -> Path:
        return self.support / "service.lock"

    @property
    def log_file(self) -> Path:
        return self.logs / "service.log"


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

    `revisable_preview` is on since the P2a acceptance measurements in
    docs/benchmarks/p2a-preview-report.md. Turning it off makes the server
    answer `unsupported_option` instead of quietly downgrading, and capabilities
    then keeps `partial_transcripts=false`.
    """

    revisable_preview: bool = True
    #: Strip Unicode Private Use Area characters (BMP U+E000-U+F8FF and
    #: supplementary U+F0000-U+FFFFD/U+100000-U+10FFFD) from recognized text
    #: before it reaches the client. This is a stopgap for a
    #: defect in the deployed MLX 4bit quantization of the model, not a
    #: permanent feature — see `filter_private_use_characters` in
    #: `tea_asr/api/stream.py` for the measurements and the removal
    #: condition. Default on since docs/benchmarks/pua-bf16-ab-report.md.
    filter_pua: bool = True
    max_total_connections: int = 4
    #: Concurrent `continuous` profile sessions the single MLX worker admits
    #: before `/v1/stream` rejects an additional session.start with
    #: `concurrent_session_limit`. Measured, not guessed — see
    #: docs/benchmarks/concurrency-report.md for the methodology and the
    #: reasoning behind this default. Correctness held up to 4 (the most
    #: tested): no queue backlog growth, no dropped/garbled segments, and
    #: HTTP interactive requests were never starved. The default ships at 2
    #: — below the tested ceiling — because median end-to-end latency grows
    #: roughly linearly with session count (~0.4s at 1, ~0.7s at 2, ~1.1s at
    #: 4) and this is a single-user local desktop service, not a multi-tenant
    #: server; raise it in config.toml if a deployment genuinely needs more,
    #: with the tested ceiling of 4 as the known-safe upper bound.
    max_continuous_sessions: int = 2
    #: Stop the worker after this long with no work, freeing Metal memory.
    #: 0 disables unloading.
    idle_unload_s: int = 15 * 60
    keep_warm: bool = False
    host: str = "127.0.0.1"
    port: int = 8327

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ServiceConfig:
        source = os.environ if env is None else env
        config = cls()
        return _apply_env(config, source)

    @classmethod
    def load(
        cls, paths: AppPaths | None = None, env: dict[str, str] | None = None
    ) -> ServiceConfig:
        """File first, then environment, so a shell override always wins."""

        paths = paths or AppPaths.macos_default()
        config = cls()
        if paths.config_file.exists():
            try:
                data = tomllib.loads(paths.config_file.read_text())
            except (OSError, tomllib.TOMLDecodeError) as exc:
                raise RuntimeError(f"設定檔無法解析：{paths.config_file}: {exc}") from exc
            service = data.get("service", {})
            unknown = set(service) - {field for field in cls.__dataclass_fields__}
            if unknown:
                raise RuntimeError(
                    f"設定檔有不認得的欄位：{', '.join(sorted(unknown))}"
                )
            config = replace(config, **service)
        source = os.environ if env is None else env
        return _apply_env(config, source)

    @property
    def unload_after_s(self) -> int:
        return 0 if self.keep_warm else self.idle_unload_s

    @property
    def protocol_version(self) -> str:
        return "1.1" if self.revisable_preview else "1.0"


def _apply_env(config: ServiceConfig, source: object) -> ServiceConfig:
    """Environment overrides the file, so a shell flag always wins."""

    get = source.get  # type: ignore[attr-defined]
    preview = get("TEA_ASR_REVISABLE_PREVIEW")
    if preview is not None:
        config = replace(config, revisable_preview=preview not in {"0", "false", "no"})
    elif get("TEA_ASR_EXPERIMENTAL_REVISABLE_PREVIEW") == "1":
        config = replace(config, revisable_preview=True)
    if get("TEA_ASR_KEEP_WARM") == "1":
        config = replace(config, keep_warm=True)
    filter_pua = get("TEA_ASR_FILTER_PUA")
    if filter_pua is not None:
        config = replace(config, filter_pua=filter_pua not in {"0", "false", "no"})
    return config
