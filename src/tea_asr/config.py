from __future__ import annotations

import math
import os
import secrets
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

from .model_spec import ASR_MODEL_DEFAULT, ASR_MODEL_VARIANTS


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
    def dictionaries_dir(self) -> Path:
        return self.support / "dictionaries"

    @property
    def lock_file(self) -> Path:
        return self.support / "service.lock"

    @property
    def log_file(self) -> Path:
        return self.logs / "service.log"

    @property
    def error_log_file(self) -> Path:
        """Longer-retention, warning/error-only sibling of `log_file`.

        See `ServiceConfig.log_error_backup_count` for why this exists as a
        separate rotation family instead of just widening `log_file`'s
        backup count.
        """

        return self.logs / "service.error.log"


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
    #: The one ASR model loaded by the service. Selection is explicit; a local
    #: model pin failure is surfaced and never falls back to another variant.
    asr_model: str = ASR_MODEL_DEFAULT
    #: `segment.audio_class` labels (docs/04). On since the held-out YAMNet
    #: evaluation in docs/benchmarks/singing-eval-report.md met its targets.
    #: It only takes effect when the pinned YAMNet asset is present (fetched by
    #: `tea-asr model-prepare`); without it the capability is simply not
    #: advertised. Transcription is never changed, only labelled.
    singing_detection_enabled: bool = True
    #: Insert missing `，。？、` into recognized text with the pinned CT-Transformer
    #: punctuation model (src/tea_asr/punctuation.py, docs/04). Insert-only;
    #: needs the optional asset from `tea-asr model-prepare`, and the
    #: capability is only advertised once it has loaded. Default decided by
    #: docs/benchmarks/punctuation-report.md.
    punctuation_restore_enabled: bool = False
    #: Partials get no inserted mark within this many characters of their end
    #: (finals are always fully punctuated).
    punctuation_tail_margin: int = 2
    #: Strip Unicode Private Use Area characters (BMP U+E000-U+F8FF and
    #: supplementary U+F0000-U+FFFFD/U+100000-U+10FFFD) from recognized text
    #: before it reaches the client. This is a stopgap for a
    #: defect in the deployed MLX 4bit quantization of the model, not a
    #: permanent feature — see `filter_private_use_characters` in
    #: `tea_asr/api/stream.py` for the measurements and the removal
    #: condition. Default on since docs/benchmarks/pua-bf16-ab-report.md.
    filter_pua: bool = True
    #: Maximum consecutive copies kept by the streaming repetition guard.
    #: One-character runs preserve conversational emphasis (default 3); units
    #: of 2–6 characters use the same default. Environment overrides:
    #: TEA_ASR_REPETITION_SINGLE_CHAR_LIMIT and
    #: TEA_ASR_REPETITION_MULTI_CHAR_LIMIT.
    repetition_single_char_limit: int = 3
    repetition_multi_char_limit: int = 3
    #: Numeral-bearing loops are trimmed to three copies after this threshold.
    #: A run of one decimal digit needs at least ten copies for number safety.
    #: Environment override: TEA_ASR_REPETITION_NUMERAL_LOOP_LIMIT.
    repetition_numeral_loop_limit: int = 6
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
    #: Allow authenticated dictionary edits from non-loopback clients.
    #: Disabled by default even when LAN serving is enabled.
    dictionary_remote_edit: bool = False
    #: Extra Host/Origin values to accept once `allow_lan` is on, beyond the
    #: private/Tailscale IP ranges `wire.is_trusted_lan_address` recognizes —
    #: e.g. a Tailscale MagicDNS name, which is a hostname rather than an IP
    #: and so cannot be judged by address range alone. Matched case-insensitively.
    extra_allowed_hosts: tuple[str, ...] = ()
    #: Minimum severity `tea_asr.logs.event()` actually writes to disk.
    #: `debug` is deliberately not the default: it is noisy enough to rotate
    #: `warning`/`error` entries out of the short-retention main log before
    #: anyone reads them, which defeats the point of the log page.
    log_level: str = "info"
    #: Per-file cap for both rotating log families below, in bytes. Same
    #: default as the size this module shipped with before per-level
    #: retention existed.
    log_max_bytes: int = 5 * 1024 * 1024
    #: Backups kept for `service.log` (every level at/above `log_level`).
    #: Small on purpose: this is the high-volume "what's happening right
    #: now" stream, not the archive.
    log_backup_count: int = 3
    #: Backups kept for `service.error.log` (warning/error only, written by
    #: `tea_asr.logs.setup_logging`'s second handler). Bigger than
    #: `log_backup_count` on purpose: failures are rare, so the same byte
    #: budget buys far more retention *in time* for exactly the entries a
    #: post-mortem needs, without the main log's routine traffic evicting
    #: them first. Still bounded — see `validate_log_retention_or_raise` —
    #: there is no "keep forever" setting.
    log_error_backup_count: int = 10
    #: Opt-in translation provider (docs/04「翻譯（opt-in）」). Off by default;
    #: when off nothing about ASR changes and no translation model is touched.
    #: It is a separate worker process with its own model (docs/06 #2), never
    #: a replacement for the ASR model.
    translation_enabled: bool = False
    #: Local MLX 4bit folder of Confucius4-T3PO (see models.lock.json). May sit
    #: on an external disk: a missing path is reported as a failed provider,
    #: never as a silently disabled one.
    translation_model_path: str = ""
    #: Refuse to keep the translation worker if the loaded model occupies more
    #: than this much Metal memory. Measured peak is ~8.5 GiB.
    translation_max_memory_gib: float = 12.0
    #: Enables validated context, server dictionaries, and deterministic
    #: replacements. Off by default; reviewed exact replacements are safe to
    #: enable independently of the experimental model prompt below.
    context_hints_enabled: bool = False
    #: Experimental prompt to the ASR model. Separate from deterministic
    #: replacements because the real-sermon smoke run did not show a benefit.
    context_prompt_enabled: bool = False
    #: Revisable-preview cadence (docs/07「輕量化與排程」). A preview may start
    #: once `preview_min_audio_ms` of new audio has arrived since the last
    #: published one, and no sooner than
    #: `max(preview_min_interval_ms, preview_load_factor × last preview decode
    #: time)` after the previous preview started. With factor k, one session's
    #: previews keep the single worker at most 1/k busy; 0 turns the load guard
    #: off. Defaults are measured, see docs/benchmarks/preview-cadence-report.md.
    #: Server-wide on purpose: a client cannot ask for a faster cadence and
    #: starve another session.
    preview_min_interval_ms: int = 300
    preview_min_audio_ms: int = 300
    preview_load_factor: float = 2.0
    #: Audio before a new final's segment start to include as left context.
    #: Kept off until the speaker/music gold evaluation meets its quality gate.
    carry_context_s: float = 0.0
    #: Maximum silence/gap after the previous final for its text/audio to be
    #: eligible as left context. This does not change segment sample clocks.
    carry_context_max_gap_s: float = 1.5
    #: Diagnostics only, off by default: keep the most recent
    #: `debug_capture_minutes` of every `/v1/stream` session's received 16 kHz
    #: PCM as rolling WAV files under `<logs>/captures/`, so the exact audio
    #: of a "no captions" moment can be replayed through
    #: `benchmarks/capture_event_trace.py`. It records whatever the user
    #: streams (docs/06 #7: ephemeral sessions do not persist audio unless
    #: this is explicitly turned on). Bounded: see `tea_asr.diagnostics`.
    debug_capture_audio: bool = False
    debug_capture_minutes: int = 10

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ServiceConfig:
        source = os.environ if env is None else env
        config = cls()
        return _apply_env(config, source)

    def __post_init__(self) -> None:
        if self.asr_model not in ASR_MODEL_VARIANTS:
            allowed = ", ".join(sorted(ASR_MODEL_VARIANTS))
            raise ValueError(f"asr_model must be one of: {allowed}")
        if min(
            self.repetition_single_char_limit,
            self.repetition_multi_char_limit,
            self.repetition_numeral_loop_limit,
        ) < 1:
            raise ValueError("repetition limits must be at least 1")
        if not 0 <= self.punctuation_tail_margin <= 20:
            raise ValueError("punctuation_tail_margin must be between 0 and 20")

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
    asr_model = get("TEA_ASR_MODEL")
    if asr_model is not None:
        config = replace(config, asr_model=asr_model)
    preview = get("TEA_ASR_REVISABLE_PREVIEW")
    if preview is not None:
        config = replace(config, revisable_preview=preview not in {"0", "false", "no"})
    elif get("TEA_ASR_EXPERIMENTAL_REVISABLE_PREVIEW") == "1":
        config = replace(config, revisable_preview=True)
    singing_detection = get("TEA_ASR_SINGING_DETECTION")
    if singing_detection is not None:
        config = replace(
            config,
            singing_detection_enabled=singing_detection not in {"0", "false", "no", ""},
        )
    punctuation = get("TEA_ASR_PUNCTUATION")
    if punctuation is not None:
        config = replace(
            config, punctuation_restore_enabled=punctuation not in {"0", "false", "no", ""}
        )
    if get("TEA_ASR_KEEP_WARM") == "1":
        config = replace(config, keep_warm=True)
    filter_pua = get("TEA_ASR_FILTER_PUA")
    if filter_pua is not None:
        config = replace(config, filter_pua=filter_pua not in {"0", "false", "no"})
    repetition_single_char_limit = get("TEA_ASR_REPETITION_SINGLE_CHAR_LIMIT")
    if repetition_single_char_limit is not None:
        config = replace(config, repetition_single_char_limit=int(repetition_single_char_limit))
    repetition_multi_char_limit = get("TEA_ASR_REPETITION_MULTI_CHAR_LIMIT")
    if repetition_multi_char_limit is not None:
        config = replace(config, repetition_multi_char_limit=int(repetition_multi_char_limit))
    repetition_numeral_loop_limit = get("TEA_ASR_REPETITION_NUMERAL_LOOP_LIMIT")
    if repetition_numeral_loop_limit is not None:
        config = replace(config, repetition_numeral_loop_limit=int(repetition_numeral_loop_limit))
    allow_lan = get("TEA_ASR_ALLOW_LAN")
    if allow_lan is not None:
        config = replace(config, allow_lan=allow_lan not in {"0", "false", "no", ""})
    dictionary_remote_edit = get("TEA_ASR_DICTIONARY_REMOTE_EDIT")
    if dictionary_remote_edit is not None:
        config = replace(
            config,
            dictionary_remote_edit=dictionary_remote_edit not in {"0", "false", "no", ""},
        )
    extra_hosts = get("TEA_ASR_EXTRA_ALLOWED_HOSTS")
    if extra_hosts is not None:
        config = replace(
            config,
            extra_allowed_hosts=tuple(
                host.strip() for host in extra_hosts.split(",") if host.strip()
            ),
        )
    log_level = get("TEA_ASR_LOG_LEVEL")
    if log_level is not None:
        config = replace(config, log_level=log_level.strip().lower())
    log_max_bytes = get("TEA_ASR_LOG_MAX_BYTES")
    if log_max_bytes is not None:
        config = replace(config, log_max_bytes=int(log_max_bytes))
    log_backup_count = get("TEA_ASR_LOG_BACKUP_COUNT")
    if log_backup_count is not None:
        config = replace(config, log_backup_count=int(log_backup_count))
    log_error_backup_count = get("TEA_ASR_LOG_ERROR_BACKUP_COUNT")
    if log_error_backup_count is not None:
        config = replace(config, log_error_backup_count=int(log_error_backup_count))
    translation = get("TEA_ASR_TRANSLATION")
    if translation is not None:
        config = replace(config, translation_enabled=translation not in {"0", "false", "no", ""})
    context_hints = get("TEA_ASR_CONTEXT_HINTS")
    if context_hints is not None:
        config = replace(config, context_hints_enabled=context_hints == "1")
    context_prompt = get("TEA_ASR_CONTEXT_PROMPT")
    if context_prompt is not None:
        config = replace(config, context_prompt_enabled=context_prompt == "1")
    translation_path = get("TEA_ASR_TRANSLATION_MODEL_PATH")
    if translation_path is not None:
        config = replace(config, translation_model_path=translation_path.strip())
    min_interval = get("TEA_ASR_PREVIEW_MIN_INTERVAL_MS")
    if min_interval is not None:
        config = replace(config, preview_min_interval_ms=int(min_interval))
    min_audio = get("TEA_ASR_PREVIEW_MIN_AUDIO_MS")
    if min_audio is not None:
        config = replace(config, preview_min_audio_ms=int(min_audio))
    load_factor = get("TEA_ASR_PREVIEW_LOAD_FACTOR")
    if load_factor is not None:
        config = replace(config, preview_load_factor=float(load_factor))
    carry_context = get("TEA_ASR_CARRY_CONTEXT_S")
    if carry_context is not None:
        config = replace(config, carry_context_s=float(carry_context))
    carry_max_gap = get("TEA_ASR_CARRY_CONTEXT_MAX_GAP_S")
    if carry_max_gap is not None:
        config = replace(config, carry_context_max_gap_s=float(carry_max_gap))
    capture = get("TEA_ASR_DEBUG_CAPTURE_AUDIO")
    if capture is not None:
        config = replace(config, debug_capture_audio=capture not in {"0", "false", "no", ""})
    capture_minutes = get("TEA_ASR_DEBUG_CAPTURE_MINUTES")
    if capture_minutes is not None:
        config = replace(config, debug_capture_minutes=int(capture_minutes))
    return config


#: Bounds for the preview cadence (docs/06 #4: every knob has a ceiling). Below
#: 100 ms a preview would re-run on almost every 100 ms frame; above 5 s the
#: "preview" is slower than the final it is meant to precede.
PREVIEW_CADENCE_MS_RANGE = (100, 5000)
PREVIEW_LOAD_FACTOR_MAX = 10.0


def validate_preview_cadence_or_raise(config: ServiceConfig) -> None:
    """Refuse a preview cadence outside the range the server can honour."""

    low, high = PREVIEW_CADENCE_MS_RANGE
    for name in ("preview_min_interval_ms", "preview_min_audio_ms"):
        value = getattr(config, name)
        if not (low <= value <= high):
            raise RuntimeError(f"{name}={value} 超出範圍（必須介於 {low} 與 {high} ms 之間）。")
    if not (0 <= config.preview_load_factor <= PREVIEW_LOAD_FACTOR_MAX):
        raise RuntimeError(
            f"preview_load_factor={config.preview_load_factor} 超出範圍"
            f"（必須介於 0 與 {PREVIEW_LOAD_FACTOR_MAX:g} 之間，0 表示關閉負載保護）。"
        )


CARRY_CONTEXT_S_RANGE = (0.0, 5.0)
CARRY_CONTEXT_MAX_GAP_S_RANGE = (0.0, 30.0)


def validate_carry_context_or_raise(config: ServiceConfig) -> None:
    """Refuse carry-over windows outside the memory and timing bounds."""

    for name, value, limits in (
        ("carry_context_s", config.carry_context_s, CARRY_CONTEXT_S_RANGE),
        (
            "carry_context_max_gap_s",
            config.carry_context_max_gap_s,
            CARRY_CONTEXT_MAX_GAP_S_RANGE,
        ),
    ):
        low, high = limits
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not low <= value <= high
        ):
            raise RuntimeError(
                f"{name}={value!r} 超出範圍（必須介於 {low:g} 與 {high:g} 秒之間）。"
            )


#: Ceiling on `debug_capture_minutes` (docs/06 #4). An hour of 16 kHz PCM is
#: ~115 MB per session.
DEBUG_CAPTURE_MINUTES_MAX = 60


def validate_debug_capture_or_raise(config: ServiceConfig) -> None:
    """Refuse a capture window the rolling recorder cannot bound."""

    if not (1 <= config.debug_capture_minutes <= DEBUG_CAPTURE_MINUTES_MAX):
        raise RuntimeError(
            f"debug_capture_minutes={config.debug_capture_minutes} 超出範圍"
            f"（必須介於 1 與 {DEBUG_CAPTURE_MINUTES_MAX} 分鐘之間）。"
        )


def validate_translation_or_raise(config: ServiceConfig) -> None:
    """Refuse to start with translation switched on but nowhere to load it from.

    A path that is set but missing (unplugged SSD) is *not* a start-up error:
    ASR must still come up, and the provider reports itself as failed with the
    reason in capabilities and on every session.start that asks for it.
    """

    if not config.translation_enabled:
        return
    if not config.translation_model_path.strip():
        raise RuntimeError(
            "translation_enabled=true 但沒有設定 translation_model_path"
            "（config.toml 的 [service] 區塊或 TEA_ASR_TRANSLATION_MODEL_PATH）。"
        )
    if not (0 < config.translation_max_memory_gib <= 40):
        raise RuntimeError(
            f"translation_max_memory_gib={config.translation_max_memory_gib} 超出範圍（0–40 GiB）。"
        )


#: Log levels `tea_asr.logs.event()` and `ServiceConfig.log_level` accept.
LOG_LEVELS: tuple[str, ...] = ("debug", "info", "warning", "error")
#: Hard ceilings on log retention config, independent of anything a
#: config.toml or env var says — docs/06-handoff.md #4: every bounded
#: resource needs an upper bound that isn't just "whatever the operator
#: typed", so a typo (or a deliberately hostile config file) can't turn
#: "keep some logs" into "keep logs forever" / "fill the disk".
_MAX_LOG_MAX_BYTES = 50 * 1024 * 1024
_MAX_LOG_BACKUP_COUNT = 20
_MAX_LOG_ERROR_BACKUP_COUNT = 50


def validate_log_retention_or_raise(config: ServiceConfig) -> None:
    """Refuse a log retention config that is invalid or effectively unbounded.

    Called from `tea_asr.logs.setup_logging` before any handler is created,
    so a bad `[service]` block in config.toml fails loudly at startup instead
    of silently filling the disk or dropping levels nobody asked to drop.
    """

    if config.log_level not in LOG_LEVELS:
        raise RuntimeError(
            f"log_level={config.log_level!r} 不是合法等級，必須是 {', '.join(LOG_LEVELS)} 之一。"
        )
    if not (0 < config.log_max_bytes <= _MAX_LOG_MAX_BYTES):
        raise RuntimeError(
            f"log_max_bytes={config.log_max_bytes} 超出範圍"
            f"（必須介於 1 與 {_MAX_LOG_MAX_BYTES} bytes 之間）。"
        )
    if not (0 <= config.log_backup_count <= _MAX_LOG_BACKUP_COUNT):
        raise RuntimeError(
            f"log_backup_count={config.log_backup_count} 超出範圍"
            f"（必須介於 0 與 {_MAX_LOG_BACKUP_COUNT} 之間）。"
        )
    if not (0 <= config.log_error_backup_count <= _MAX_LOG_ERROR_BACKUP_COUNT):
        raise RuntimeError(
            f"log_error_backup_count={config.log_error_backup_count} 超出範圍"
            f"（必須介於 0 與 {_MAX_LOG_ERROR_BACKUP_COUNT} 之間）。"
        )


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
