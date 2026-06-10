"""Tests for core/rate_limiter.py token bucket rate limiting."""

from __future__ import annotations

import gestaltworkframe.core.rate_limiter as rate_limiter_module
from gestaltworkframe.core.rate_limiter import (
    RateLimitConfig,
    TokenBucketRateLimiter,
    get_key_store_rate_limiter,
)


def test_rate_limit_config_from_env_defaults(monkeypatch):
    monkeypatch.delenv("KEY_STORE_RATE_LIMIT_RPS", raising=False)
    monkeypatch.delenv("KEY_STORE_RATE_LIMIT_BURST", raising=False)
    config = RateLimitConfig.from_env("KEY_STORE")
    assert config.requests_per_second == 5.0
    assert config.burst == 10


def test_rate_limit_config_from_env_custom(monkeypatch):
    monkeypatch.setenv("KEY_STORE_RATE_LIMIT_RPS", "2.5")
    monkeypatch.setenv("KEY_STORE_RATE_LIMIT_BURST", "20")
    config = RateLimitConfig.from_env("KEY_STORE")
    assert config.requests_per_second == 2.5
    assert config.burst == 20


def test_rate_limit_config_from_env_invalid_values_fall_back(monkeypatch):
    monkeypatch.setenv("KEY_STORE_RATE_LIMIT_RPS", "not-a-number")
    monkeypatch.setenv("KEY_STORE_RATE_LIMIT_BURST", "also-bad")
    config = RateLimitConfig.from_env("KEY_STORE")
    assert config.requests_per_second == 5.0
    assert config.burst == 10


async def test_is_allowed_within_burst():
    limiter = TokenBucketRateLimiter(RateLimitConfig(requests_per_second=5.0, burst=10))
    allowed, meta = await limiter.is_allowed("client-1")
    assert allowed is True
    assert meta["allowed"] is True
    assert meta["tokens_remaining"] == 9
    assert "reset_after" in meta


async def test_is_allowed_blocks_when_cost_exceeds_tokens():
    limiter = TokenBucketRateLimiter(RateLimitConfig(requests_per_second=1.0, burst=1))
    allowed, _ = await limiter.is_allowed("client-2", cost=1)
    assert allowed is True

    blocked, meta = await limiter.is_allowed("client-2", cost=1)
    assert blocked is False
    assert meta["allowed"] is False
    assert meta["tokens_remaining"] == 0
    assert meta["retry_after"] > 0


async def test_cleanup_old_buckets_removes_only_stale_entries():
    limiter = TokenBucketRateLimiter()
    await limiter.is_allowed("stale-client")
    await limiter.is_allowed("fresh-client")

    limiter._buckets["stale-client"]["last_update"] -= 7200

    removed = await limiter.cleanup_old_buckets(max_age_seconds=3600)

    assert removed == 1
    assert "stale-client" not in limiter._buckets
    assert "fresh-client" in limiter._buckets


def test_get_status_unknown_client_is_inactive():
    limiter = TokenBucketRateLimiter(RateLimitConfig(burst=10))
    assert limiter.get_status("never-seen") == {"tokens": 10, "active": False}


async def test_get_status_known_client_is_active():
    limiter = TokenBucketRateLimiter(RateLimitConfig(burst=10))
    await limiter.is_allowed("known-client")

    status = limiter.get_status("known-client")

    assert status["active"] is True
    assert status["tokens"] == 9


def test_get_key_store_rate_limiter_returns_singleton(monkeypatch):
    monkeypatch.setattr(rate_limiter_module, "_key_store_rate_limiter", None)

    first = get_key_store_rate_limiter()
    second = get_key_store_rate_limiter()

    assert first is second
