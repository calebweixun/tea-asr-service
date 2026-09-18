from __future__ import annotations

import errno
import fcntl
import json
import os
import plistlib
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import AppPaths

LAUNCH_AGENT_LABEL = "com.tea-asr.service"


class ServiceAlreadyRunningError(RuntimeError):
    pass


@dataclass(slots=True)
class SingletonLock:
    """One service instance per machine (docs/06 constraint 2).

    The lock is an advisory flock on a file holding the owner's PID and port.
    A manual start and a LaunchAgent start therefore cannot end up with two
    models resident; the second one says who already holds the port.
    """

    path: Path
    _handle: object | None = None

    def acquire(self, port: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = self.path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.seek(0)
            owner = handle.read().strip()
            handle.close()
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            raise ServiceAlreadyRunningError(
                f"服務已在執行中：{owner or self.path}"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": os.getpid(), "port": port}) + "\n")
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
        finally:
            handle.close()  # type: ignore[attr-defined]
            self.path.unlink(missing_ok=True)


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, port)) == 0


def launch_agent_path(paths: AppPaths | None = None) -> Path:
    del paths
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def _executable() -> Path:
    """Absolute path to the installed CLI.

    LaunchAgents do not inherit a shell PATH, so the plist must name the real
    binary (docs/03).
    """

    candidate = Path(sys.argv[0]).resolve()
    if candidate.name == "tea-asr" and candidate.is_file():
        return candidate
    guess = Path(sys.executable).parent / "tea-asr"
    if guess.is_file():
        return guess
    raise RuntimeError(
        "找不到 tea-asr 執行檔的絕對路徑；請用安裝後的 tea-asr 指令執行 service install。"
    )


def agent_plist(paths: AppPaths, port: int, host: str) -> dict[str, object]:
    paths.logs.mkdir(parents=True, exist_ok=True)
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            str(_executable()),
            "serve",
            "--host",
            host,
            "--port",
            str(port),
        ],
        "RunAtLoad": True,
        # Restart only on an abnormal termination. A clean non-zero exit is how
        # the service says "another instance already holds the lock"; restarting
        # that would be a loop, and a deliberate shutdown must stay down.
        "KeepAlive": {"Crashed": True},
        "ThrottleInterval": 10,
        "ProcessType": "Interactive",
        "StandardOutPath": str(paths.logs / "launchd.out.log"),
        "StandardErrorPath": str(paths.logs / "launchd.err.log"),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *args], capture_output=True, text=True, check=False
    )


def install_agent(paths: AppPaths, *, host: str, port: int) -> Path:
    """Create and load the LaunchAgent. Idempotent."""

    target = launch_agent_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = agent_plist(paths, port=port, host=host)
    target.write_bytes(plistlib.dumps(payload))
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{LAUNCH_AGENT_LABEL}")
    result = _launchctl("bootstrap", domain, str(target))
    if result.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap 失敗：{result.stderr.strip()}")
    return target


def uninstall_agent() -> bool:
    """Unload and remove the agent. Returns False when nothing was installed."""

    target = launch_agent_path()
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{LAUNCH_AGENT_LABEL}")
    if not target.exists():
        return False
    target.unlink()
    return True


def agent_status(paths: AppPaths) -> dict[str, object]:
    target = launch_agent_path()
    listed = _launchctl("print", f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}")
    lock = paths.lock_file
    owner: dict[str, object] | None = None
    if lock.exists():
        try:
            owner = json.loads(lock.read_text() or "{}")
        except json.JSONDecodeError:
            owner = None
    return {
        "agent_installed": target.exists(),
        "agent_plist": str(target),
        "agent_loaded": listed.returncode == 0,
        "lock_owner": owner,
    }
