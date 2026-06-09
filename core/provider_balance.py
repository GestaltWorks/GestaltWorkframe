"""Provider credit-balance checkers.

Each checker polls the provider's own API to report available credit. Results
are cached in-process for CACHE_TTL_SECONDS (default 300) to avoid hammering
rate limits on every admin health poll.

`BalanceSnapshot` is the canonical response shape. All checkers return one.
`available=False` means the check could not complete (network error, missing
key, etc.); the `error` field describes why. `available=True` means the
response was decoded successfully; numeric fields may still be zero for
free-tier accounts that don't expose a limit.

Public surface:
  - BalanceSnapshot
  - OpenRouterBalanceChecker
  - LocalTrackingBalanceSummary  (not a network check; computes from local spend)
  - get_balance(provider_id, api_key, *, multi_gate) -> BalanceSnapshot
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS: float = 300.0
_OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/auth/key"


@dataclass
class BalanceSnapshot:
    provider_id: str
    available: bool
    source: str  # "live" | "cached" | "local_tracking" | "unavailable"
    # Credit fields -- zero when not applicable (e.g. free-tier with no limit).
    limit_usd: float = 0.0
    used_usd: float = 0.0
    remaining_usd: float = 0.0
    is_free_tier: bool = False
    checked_at: float = field(default_factory=time.time)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "available": self.available,
            "source": self.source,
            "limit_usd": round(self.limit_usd, 6),
            "used_usd": round(self.used_usd, 6),
            "remaining_usd": round(self.remaining_usd, 6),
            "is_free_tier": self.is_free_tier,
            "checked_at": self.checked_at,
            "error": self.error,
        }


class OpenRouterBalanceChecker:
    """Polls GET /api/v1/auth/key on OpenRouter and caches the result.

    Thread-safe via asyncio.Lock. If the call fails for any reason the
    last known snapshot (or an unavailable sentinel) is returned rather
    than propagating the exception.
    """

    def __init__(
        self,
        api_key: str,
        cache_ttl: float = CACHE_TTL_SECONDS,
        base_url: str = _OPENROUTER_KEY_URL,
    ) -> None:
        self._api_key = api_key
        self._cache_ttl = cache_ttl
        self._base_url = base_url
        self._lock = asyncio.Lock()
        self._cached: BalanceSnapshot | None = None

    async def get(self) -> BalanceSnapshot:
        async with self._lock:
            now = time.time()
            if self._cached is not None and (now - self._cached.checked_at) < self._cache_ttl:
                return BalanceSnapshot(
                    provider_id="openrouter",
                    available=self._cached.available,
                    source="cached",
                    limit_usd=self._cached.limit_usd,
                    used_usd=self._cached.used_usd,
                    remaining_usd=self._cached.remaining_usd,
                    is_free_tier=self._cached.is_free_tier,
                    checked_at=self._cached.checked_at,
                    error=self._cached.error,
                )
            snapshot = await self._fetch()
            self._cached = snapshot
            return snapshot

    def invalidate(self) -> None:
        """Force the next call to re-fetch (used in tests and after key changes)."""
        self._cached = None

    async def _fetch(self) -> BalanceSnapshot:
        if not self._api_key:
            return BalanceSnapshot(
                provider_id="openrouter",
                available=False,
                source="unavailable",
                error="missing_api_key",
            )
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(
                    self._base_url,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json().get("data", {})
            limit_usd = float(data.get("limit") or 0.0)
            used_usd = float(data.get("usage") or 0.0)
            is_free = bool(data.get("is_free_tier", False))
            # When limit is 0 the account has no hard cap (free tier or
            # unlimited plan); remaining is not meaningful in that case.
            remaining_usd = max(limit_usd - used_usd, 0.0) if limit_usd > 0 else 0.0
            return BalanceSnapshot(
                provider_id="openrouter",
                available=True,
                source="live",
                limit_usd=limit_usd,
                used_usd=used_usd,
                remaining_usd=remaining_usd,
                is_free_tier=is_free,
                checked_at=time.time(),
            )
        except Exception as exc:
            logger.warning("OpenRouter balance check failed: %s", exc)
            return BalanceSnapshot(
                provider_id="openrouter",
                available=False,
                source="unavailable",
                error=type(exc).__name__,
                checked_at=time.time(),
            )


def local_tracking_balance(
    provider_id: str,
    max_daily_usd: float,
    max_monthly_usd: float,
    day_used_usd: float,
    month_used_usd: float,
) -> BalanceSnapshot:
    """Compute an estimated-remaining snapshot from local spend tracking.

    No network call. Used for providers that don't expose a balance API
    (Anthropic, Google, OpenAI). The numbers are estimates: they reflect
    what the local gate has recorded, not what the provider has actually
    billed.
    """
    daily_remaining = max(max_daily_usd - day_used_usd, 0.0) if max_daily_usd > 0 else 0.0
    monthly_remaining = max(max_monthly_usd - month_used_usd, 0.0) if max_monthly_usd > 0 else 0.0
    # Report the more conservative of daily / monthly headroom.
    remaining = min(daily_remaining, monthly_remaining) if max_daily_usd > 0 and max_monthly_usd > 0 else max(daily_remaining, monthly_remaining)
    return BalanceSnapshot(
        provider_id=provider_id,
        available=True,
        source="local_tracking",
        limit_usd=max_monthly_usd,
        used_usd=month_used_usd,
        remaining_usd=remaining,
        checked_at=time.time(),
    )
