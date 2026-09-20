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


def _atomic_write_token_file(path: Path, content: str) -> None:
    """Replace the token file's content without ever leaving it world-readable.

    Writes to a sibling temp file first and `os.replace`s it into place, so a
    reader (or a crash) never observes a partially written token. The temp
    file is created with the final 0600 mode directly, not chmod'd after the
    fact, so there is no window where the token is readable by anyone else.
    """

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    os.replace(tmp_path, path)


def rotate_token(paths: AppPaths | None = None) -> str:
    """Issue a fresh bearer token, invalidating whatever token was live before.

    There is no grace period: the old token stops working the moment this
    returns, because a leaked-token scenario (the actual reason to rotate) is
    made worse, not better, by a window where both work. A server process
    that reads the token through `TokenAuthenticator` (rather than a token
    string captured once at startup) picks up the new value on its next
    request without a restart.
    """

    paths = paths or AppPaths.macos_default()
    token = secrets.token_urlsafe(32)
    _atomic_write_token_file(paths.token_file, token + "\n")
    return token


def revoke_token(paths: AppPaths | None = None) -> None:
    """Disable the bearer token entirely, with no replacement.

    Writes an empty (but still 0600) token file. `TokenAuthenticator` treats
    an empty file as "no token can possibly match", so every request is
    rejected until an operator runs `rotate_token`/`tea-asr token rotate`.
    This is deliberately harsher than deleting the file: a missing file could
    be confused with "never configured"; an explicitly empty one is
    unambiguous evidence that the token was revoked on purpose.
    """

    paths = paths or AppPaths.macos_default()
    _atomic_write_token_file(paths.token_file, "")


class TokenAuthenticator:
    """Bearer-token check that follows the token file, not a value fixed at boot.

    `load_or_create_token` (and the historic `create_app(token=...)` path)
    reads the token once when the process starts, so rotating or revoking the
    token requires restarting the service for the change to take effect. This
    class instead re-`stat`s the token file on every check and only re-reads
    its content when the mtime moved, so `tea-asr token rotate/revoke` (which
    writes through `rotate_token`/`revoke_token` above) takes effect on the
    already-running service's very next request — at the cost of one `stat()`
    call per auth check, which is negligible next to an ASR inference.

    No expiration/scope is implemented: this is a single-user desktop service
    with one bearer token guarding every route, not a multi-tenant API, so
    per-token scope has no scope to divide and a fixed expiry would only add
    an operational trap (a client silently starts failing mid-session with
    nothing the user did wrong) without a corresponding attacker it stops —
    rotation and revocation already cover "the token leaked" and "I want to
    cut this client off now".
    """

    def __init__(self, paths: AppPaths | None = None) -> None:
        self._paths = paths or AppPaths.macos_default()
        self._mtime_ns: int | None = None
        self._token: str | None = None
        self._load(create_if_missing=True)

    def _load(self, *, create_if_missing: bool) -> None:
        try:
            mtime_ns = self._paths.token_file.stat().st_mtime_ns
        except FileNotFoundError:
            if not create_if_missing:
                self._mtime_ns = None
                self._token = None
                return
            self._token = load_or_create_token(self._paths)
            self._mtime_ns = self._paths.token_file.stat().st_mtime_ns
            return
        if mtime_ns == self._mtime_ns:
            return
        content = self._paths.token_file.read_text().strip()
        self._mtime_ns = mtime_ns
        self._token = content or None  # empty file == revoked

    def matches(self, presented: str) -> bool:
        self._load(create_if_missing=False)
        if self._token is None:
            return False
        return secrets.compare_digest(presented, self._token)


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
    #: W9: binding anywhere other than loopback must be a deliberate,
    #: explicit choice (docs/06-handoff.md's LAN row: "不得只改bind至0.0.0.0").
    #: `validate_bind_or_raise` refuses to start a non-loopback bind when this
    #: is False, regardless of what `host` says, so a config-file typo or a
    #: stray `--host` flag can never silently open the service to the
    #: network. Default False keeps the historic loopback-only behaviour.
    allow_lan: bool = False
    #: Extra Host/Origin values to accept once `allow_lan` is on, beyond the
    #: private/Tailscale IP ranges `wire.is_trusted_lan_address` recognizes —
    #: e.g. a Tailscale MagicDNS name, which is a hostname rather than an IP
    #: and so cannot be judged by address range alone. Matched case-insensitively.
    extra_allowed_hosts: tuple[str, ...] = ()

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
            # TOML arrays decode as `list`; normalize to the declared `tuple`
            # so callers always see the same type regardless of source.
            config = replace(config, extra_allowed_hosts=tuple(config.extra_allowed_hosts))
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
    allow_lan = get("TEA_ASR_ALLOW_LAN")
    if allow_lan is not None:
        config = replace(config, allow_lan=allow_lan not in {"0", "false", "no", ""})
    extra_hosts = get("TEA_ASR_EXTRA_ALLOWED_HOSTS")
    if extra_hosts is not None:
        config = replace(
            config,
            extra_allowed_hosts=tuple(
                host.strip() for host in extra_hosts.split(",") if host.strip()
            ),
        )
    return config


def validate_bind_or_raise(host: str, *, allow_lan: bool) -> None:
    """Refuse a non-loopback bind unless LAN mode was explicitly opted into.

    This is the single choke point every entry point that can start the
    server (`tea-asr serve`, `tea-asr service install`) must call with the
    address it is actually about to bind — not just `ServiceConfig.host`,
    since a `--host` flag can override that. docs/06-handoff.md is explicit
    that "只改bind至0.0.0.0" is not an acceptable way to open LAN access; this
    makes that a hard runtime error instead of a code-review expectation.
    """

    if allow_lan:
        return
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(
            f"host={host} 不是本機位址，開放前必須明確設定 allow_lan=true"
            "（config.toml 的 [service] 區塊、TEA_ASR_ALLOW_LAN=1，或 "
            "`tea-asr serve --allow-lan`）。這個服務未加 TLS："
            "只應在受信任的 LAN／Tailscale 網路下開放，token 會以明文傳輸。"
        )
