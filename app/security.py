from __future__ import annotations

import time


class LoginGate:
    def __init__(self, max_fails: int = 8, window_seconds: int = 60):
        self.max_fails = max_fails
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = {}

    def allow(self, ip: str, now: float | None = None) -> bool:
        stamp = time.time() if now is None else float(now)
        hits = [item for item in self._hits.get(ip, []) if stamp - item < self.window_seconds]
        self._hits[ip] = hits
        return len(hits) < self.max_fails

    def fail(self, ip: str, now: float | None = None) -> None:
        stamp = time.time() if now is None else float(now)
        self._hits.setdefault(ip, []).append(stamp)

    def success(self, ip: str) -> None:
        self._hits.pop(ip, None)
