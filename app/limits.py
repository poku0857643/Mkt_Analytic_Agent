"""Per-user request rate limit and daily BigQuery scan budget.

Both are kept in memory, so they apply per server instance and reset on restart.
That is enough to stop runaway loops and accidental hammering; for hard limits
across instances, back them with a shared store (e.g. Redis or Firestore).
"""
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Callable


class RateLimiter:
    """At most `limit` requests per user in any rolling `window` seconds."""

    def __init__(self, limit: int, window: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.window = window
        self.clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, user: str) -> float | None:
        """Record a request. Returns None if allowed, else seconds until retry."""
        now = self.clock()
        with self._lock:
            hits = self._hits[user]
            while hits and hits[0] <= now - self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return max(hits[0] + self.window - now, 0.0)
            hits.append(now)
            return None


def _utc_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class DailyScanBudget:
    """Bytes of BigQuery data each user may scan per UTC day."""

    def __init__(self, limit_bytes: int, today: Callable[[], str] = _utc_day):
        self.limit_bytes = limit_bytes
        self.today = today
        self._used: dict[tuple[str, str], int] = defaultdict(int)
        self._lock = threading.Lock()

    def remaining(self, user: str) -> int:
        with self._lock:
            return max(self.limit_bytes - self._used[(user, self.today())], 0)

    def add(self, user: str, bytes_scanned: int) -> None:
        with self._lock:
            self._used[(user, self.today())] += bytes_scanned
