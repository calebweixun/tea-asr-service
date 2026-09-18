from __future__ import annotations

from pathlib import Path

import pytest

from tea_asr.config import AppPaths
from tea_asr.service import ServiceAlreadyRunningError, SingletonLock, agent_plist


def test_second_instance_is_refused_and_told_who_holds_the_lock(tmp_path: Path) -> None:
    first = SingletonLock(tmp_path / "service.lock")
    first.acquire(8765)
    second = SingletonLock(tmp_path / "service.lock")
    with pytest.raises(ServiceAlreadyRunningError, match="8765"):
        second.acquire(8770)
    first.release()


def test_lock_is_reusable_after_release(tmp_path: Path) -> None:
    lock = SingletonLock(tmp_path / "service.lock")
    lock.acquire(8765)
    lock.release()
    assert not (tmp_path / "service.lock").exists()
    again = SingletonLock(tmp_path / "service.lock")
    again.acquire(8765)
    again.release()


def test_agent_restarts_only_on_a_crash(tmp_path: Path) -> None:
    paths = AppPaths(support=tmp_path / "support", logs=tmp_path / "logs")
    plist = agent_plist(paths, port=8765, host="127.0.0.1")
    # A clean non-zero exit is how the service reports "already running"; if
    # launchd restarted that, it would spin.
    assert plist["KeepAlive"] == {"Crashed": True}
    assert plist["RunAtLoad"] is True
    program = plist["ProgramArguments"]
    assert isinstance(program, list)
    assert Path(str(program[0])).is_absolute(), "LaunchAgents do not inherit PATH"
    assert program[1] == "serve"
