from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

#: docs/06 constraint 4: every layer needs a cap and a visible failure. This
#: one guards auth itself — without it, opening the service to a LAN turns a
#: 32-byte bearer token from "practically unguessable" into "brute-forceable
#: at whatever rate the network allows", since W9 deliberately does not add
#: TLS (see docs/06-handoff.md's LAN row). Failing fast with `rate_limited`
#: is the visible half of that cap.
DEFAULT_MAX_FAILURES = 10
DEFAULT_WINDOW_S = 60.0
#: Bounds memory: this is a single-user desktop service reachable from a LAN,
#: not a multi-tenant server, so a four-digit number of distinct recent
#: source addresses is already generous. Oldest entries are evicted first.
DEFAULT_MAX_TRACKED = 1024


@dataclass(slots=True)
class _Window:
    start: float
    failures: int


class AuthRateLimiter:
    """Per-source-address sliding window over failed auth attempts.

    Keyed by whatever the caller considers "one source" (HTTP client IP, WS
    client IP). Success clears the window immediately, so a legitimate client
    that mistypes a token a couple of times is never punished once it gets it
    right; only a sustained run of failures trips the limit.
    """

    def __init__(
        self,
        *,
        max_failures: int = DEFAULT_MAX_FAILURES,
        window_s: float = DEFAULT_WINDOW_S,
        max_tracked: int = DEFAULT_MAX_TRACKED,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_failures = max_failures
        self._window_s = window_s
        self._max_tracked = max_tracked
        self._clock = clock
        self._windows: OrderedDict[str, _Window] = OrderedDict()

    def is_blocked(self, key: str) -> bool:
        """True if `key` has exceeded the failure budget for the current window."""

        window = self._windows.get(key)
        if window is None:
            return False
        if self._clock() - window.start >= self._window_s:
            return False
        return window.failures >= self._max_failures

    def record_failure(self, key: str) -> None:
        now = self._clock()
        window = self._windows.get(key)
        if window is None or now - window.start >= self._window_s:
            window = _Window(start=now, failures=0)
        window.failures += 1
        self._windows[key] = window
        self._windows.move_to_end(key)
        while len(self._windows) > self._max_tracked:
            self._windows.popitem(last=False)

    def record_success(self, key: str) -> None:
        self._windows.pop(key, None)
