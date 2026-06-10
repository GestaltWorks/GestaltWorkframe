"""Simple in-memory rate limiter for API endpoints.

Tracks requests per client IP and enforces limits with configurable
windows. Used primarily for key store admin endpoints to prevent
brute-force attacks on encrypted key operations.

Environment:
  KEY_STORE_RATE_LIMIT_RPS    - Requests per second per IP (default: 5)
  KEY_STORE_RATE_LIMIT_BURST  - Burst allowance (default: 10)
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class RateLimitConfig:
    requests_per_second: float = 5.0
    burst: int = 10
    window_seconds: int = 60

    @classmethod
    def from_env(cls, prefix: str = "KEY_STORE") -> "RateLimitConfig":
        rps = 5.0
        burst = 10
        try:
            rps = float(os.getenv(f"{prefix}_RATE_LIMIT_RPS", "5.0"))
        except ValueError:
            pass
        try:
            burst = int(os.getenv(f"{prefix}_RATE_LIMIT_BURST", "10"))
        except ValueError:
            pass
        return cls(requests_per_second=rps, burst=burst)


class TokenBucketRateLimiter:
    """Token bucket rate limiter for per-client IP throttling."""

    def __init__(self, config: RateLimitConfig | None = None) -> None:
        self.config = config or RateLimitConfig()
        self._buckets: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def is_allowed(self, client_id: str, cost: int = 1) -> tuple[bool, dict[str, Any]]:
        """Check if request is allowed. Returns (allowed, metadata)."""
        now = time.monotonic()
        async with self._lock:
            bucket = self._buckets.get(client_id)
            if bucket is None:
                bucket = {"tokens": float(self.config.burst), "last_update": now}
                self._buckets[client_id] = bucket

            # Add tokens based on time passed
            elapsed = now - bucket["last_update"]
            tokens_to_add = elapsed * self.config.requests_per_second
            bucket["tokens"] = min(bucket["tokens"] + tokens_to_add, float(self.config.burst))
            bucket["last_update"] = now

            if bucket["tokens"] >= cost:
                bucket["tokens"] -= cost
                metadata = {
                    "allowed": True,
                    "tokens_remaining": int(bucket["tokens"]),
                    "reset_after": (self.config.burst - bucket["tokens"]) / self.config.requests_per_second,
                }
                return True, metadata
            else:
                wait_time = (cost - bucket["tokens"]) / self.config.requests_per_second
                metadata = {
                    "allowed": False,
                    "retry_after": wait_time,
                    "tokens_remaining": int(bucket["tokens"]),
                }
                return False, metadata

    async def cleanup_old_buckets(self, max_age_seconds: int = 3600) -> int:
        """Remove buckets older than max_age_seconds. Returns removed count."""
        now = time.monotonic()
        async with self._lock:
            to_remove = [
                client_id for client_id, bucket in self._buckets.items()
                if (now - bucket["last_update"]) > max_age_seconds
            ]
            for client_id in to_remove:
                del self._buckets[client_id]
            return len(to_remove)

    def get_status(self, client_id: str) -> dict[str, Any]:
        """Get current bucket status for a client."""
        bucket = self._buckets.get(client_id)
        if bucket is None:
            return {"tokens": self.config.burst, "active": False}
        now = time.monotonic()
        elapsed = now - bucket["last_update"]
        tokens = min(bucket["tokens"] + elapsed * self.config.requests_per_second, float(self.config.burst))
        return {"tokens": int(tokens), "active": True}


# Global rate limiter instance for key store endpoints
_key_store_rate_limiter: TokenBucketRateLimiter | None = None


def get_key_store_rate_limiter() -> TokenBucketRateLimiter:
    """Get the global key store rate limiter instance."""
    global _key_store_rate_limiter
    if _key_store_rate_limiter is None:
        _key_store_rate_limiter = TokenBucketRateLimiter(RateLimitConfig.from_env("KEY_STORE"))
    return _key_store_rate_limiter
