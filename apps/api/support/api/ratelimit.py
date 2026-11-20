"""In-process rate limiting and concurrency control.

Deliberately in-process, not Redis. The deployment runs a single replica by
design (see build plan D3), so a shared store would add a dependency and a
failure mode without changing behaviour. Documented as a trade-off: scaling out
would require moving these counters to Redis, which is exactly what the
original plan's Redis component was for.

Fails *open* on its own errors. A limiter bug must not take the demo offline;
the cost ceiling is enforced separately by the per-turn tool budget and the
provider quota.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field


@dataclass
class _Window:
    """Sliding window of request timestamps."""

    hits: deque[float] = field(default_factory=deque)

    def allow(self, *, limit: int, window_s: float, now: float) -> tuple[bool, float]:
        cutoff = now - window_s
        while self.hits and self.hits[0] < cutoff:
            self.hits.popleft()
        if len(self.hits) >= limit:
            retry_after = max(0.0, self.hits[0] + window_s - now)
            return False, retry_after
        self.hits.append(now)
        return True, 0.0


class RateLimiter:
    def __init__(
        self,
        *,
        per_session_per_minute: int,
        per_ip_per_minute: int,
        max_concurrent: int,
    ) -> None:
        self._per_session = per_session_per_minute
        self._per_ip = per_ip_per_minute
        self._max_concurrent = max_concurrent
        self._sessions: dict[str, _Window] = defaultdict(_Window)
        self._ips: dict[str, _Window] = defaultdict(_Window)
        self._in_flight = 0

    def check(self, *, session_id: str, client_ip: str) -> tuple[bool, str, float]:
        """Returns (allowed, reason, retry_after_s)."""
        now = time.monotonic()

        if self._in_flight >= self._max_concurrent:
            return False, "concurrency_limit", 2.0

        ok, retry = self._sessions[session_id].allow(
            limit=self._per_session, window_s=60.0, now=now
        )
        if not ok:
            return False, "session_rate_limit", retry

        ok, retry = self._ips[client_ip].allow(limit=self._per_ip, window_s=60.0, now=now)
        if not ok:
            return False, "ip_rate_limit", retry

        return True, "", 0.0

    def acquire(self) -> None:
        self._in_flight += 1

    def release(self) -> None:
        self._in_flight = max(0, self._in_flight - 1)

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def prune(self, *, older_than_s: float = 300.0) -> None:
        """Drop idle windows so the dicts cannot grow without bound."""
        now = time.monotonic()
        for store in (self._sessions, self._ips):
            stale = [
                key
                for key, window in store.items()
                if not window.hits or window.hits[-1] < now - older_than_s
            ]
            for key in stale:
                del store[key]
