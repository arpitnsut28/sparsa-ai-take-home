"""
services/ratelimit.py — per-client token bucket for run submissions.

Starting a run costs real money (scrapes plus an LLM call), so ``POST /runs``
is the one endpoint worth protecting from a stuck retry loop or a hot page.

In-process and per-instance: behind N replicas the effective limit is N x the
configured rate. That is a deliberate floor, not the ceiling — the real limit
belongs at the gateway or in a shared store (see SIGNOFF.md). It is still worth
having, because it bounds what a single instance can be made to spend.
"""
from __future__ import annotations

import time

import config


class TokenBucket:
    __slots__ = ("tokens", "updated_at")

    def __init__(self, tokens: float, updated_at: float):
        self.tokens = tokens
        self.updated_at = updated_at


class RateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[str, TokenBucket] = {}

    def check(self, key: str) -> tuple[bool, float]:
        """Consume one token for ``key``.

        Returns (allowed, retry_after_seconds).
        """
        if not config.settings.rate_limit_enabled:
            return True, 0.0

        burst = max(1, config.settings.rate_limit_burst)
        refill_per_s = max(0.0001, config.settings.rate_limit_per_minute / 60.0)
        now = time.monotonic()

        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(tokens=float(burst), updated_at=now)
            self._buckets[key] = bucket

        elapsed = max(0.0, now - bucket.updated_at)
        bucket.tokens = min(float(burst), bucket.tokens + elapsed * refill_per_s)
        bucket.updated_at = now

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True, 0.0

        return False, round((1.0 - bucket.tokens) / refill_per_s, 2)

    def sweep(self, max_idle_s: float = 900.0) -> int:
        """Forget buckets that have been idle — otherwise this map is a slow leak
        keyed by client IP, which is exactly what an attacker would grow."""
        now = time.monotonic()
        stale = [k for k, b in self._buckets.items() if now - b.updated_at > max_idle_s]
        for key in stale:
            self._buckets.pop(key, None)
        return len(stale)

    def reset(self) -> None:
        self._buckets.clear()
