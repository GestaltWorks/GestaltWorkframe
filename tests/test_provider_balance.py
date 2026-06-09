"""Tests for core/provider_balance.py.

All network calls are mocked. Tests cover:
- Successful OpenRouter balance fetch
- Error handling (HTTP error, network failure, missing key)
- In-process cache: second call within TTL returns cached result
- Cache invalidation
- local_tracking_balance helper
- BalanceSnapshot.to_dict shape
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.provider_balance import (
    BalanceSnapshot,
    OpenRouterBalanceChecker,
    local_tracking_balance,
)


# ---------------------------------------------------------------------------
# BalanceSnapshot
# ---------------------------------------------------------------------------

def test_balance_snapshot_to_dict_shape():
    snap = BalanceSnapshot(
        provider_id="openrouter",
        available=True,
        source="live",
        limit_usd=10.0,
        used_usd=2.5,
        remaining_usd=7.5,
        is_free_tier=False,
        checked_at=1_700_000_000.0,
    )
    d = snap.to_dict()
    assert d["provider_id"] == "openrouter"
    assert d["available"] is True
    assert d["source"] == "live"
    assert d["limit_usd"] == 10.0
    assert d["used_usd"] == 2.5
    assert d["remaining_usd"] == 7.5
    assert d["is_free_tier"] is False
    assert d["error"] == ""


def test_balance_snapshot_unavailable_has_error():
    snap = BalanceSnapshot(
        provider_id="openrouter",
        available=False,
        source="unavailable",
        error="ConnectError",
    )
    d = snap.to_dict()
    assert d["available"] is False
    assert d["error"] == "ConnectError"


# ---------------------------------------------------------------------------
# OpenRouterBalanceChecker - successful fetch
# ---------------------------------------------------------------------------

def _mock_response(data: dict) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"data": data})
    return resp


@pytest.mark.asyncio
async def test_openrouter_checker_parses_balance_fields():
    checker = OpenRouterBalanceChecker(api_key="sk-test", cache_ttl=300)
    mock_resp = _mock_response({"limit": 10.0, "usage": 2.5, "is_free_tier": False})

    with patch("core.provider_balance.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        snap = await checker.get()

    assert snap.available is True
    assert snap.source == "live"
    assert snap.provider_id == "openrouter"
    assert snap.limit_usd == 10.0
    assert snap.used_usd == 2.5
    assert snap.remaining_usd == 7.5
    assert snap.is_free_tier is False
    assert snap.error == ""


@pytest.mark.asyncio
async def test_openrouter_checker_free_tier_remaining_is_zero():
    """Free-tier accounts have limit=0; remaining should not go negative."""
    checker = OpenRouterBalanceChecker(api_key="sk-test", cache_ttl=300)
    mock_resp = _mock_response({"limit": 0, "usage": 0.5, "is_free_tier": True})

    with patch("core.provider_balance.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        snap = await checker.get()

    assert snap.is_free_tier is True
    assert snap.remaining_usd == 0.0
    assert snap.available is True


# ---------------------------------------------------------------------------
# OpenRouterBalanceChecker - error paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_openrouter_checker_missing_key_returns_unavailable():
    checker = OpenRouterBalanceChecker(api_key="", cache_ttl=300)
    snap = await checker.get()
    assert snap.available is False
    assert snap.source == "unavailable"
    assert snap.error == "missing_api_key"


@pytest.mark.asyncio
async def test_openrouter_checker_network_error_returns_unavailable():
    checker = OpenRouterBalanceChecker(api_key="sk-test", cache_ttl=300)

    with patch("core.provider_balance.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("connection refused"))
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        snap = await checker.get()

    assert snap.available is False
    assert snap.source == "unavailable"
    assert snap.error == "Exception"


@pytest.mark.asyncio
async def test_openrouter_checker_http_error_returns_unavailable():
    checker = OpenRouterBalanceChecker(api_key="sk-test", cache_ttl=300)

    resp = MagicMock()
    resp.raise_for_status = MagicMock(side_effect=Exception("401 Unauthorized"))

    with patch("core.provider_balance.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=resp)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        snap = await checker.get()

    assert snap.available is False
    assert snap.source == "unavailable"


# ---------------------------------------------------------------------------
# OpenRouterBalanceChecker - caching
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_openrouter_checker_caches_result_within_ttl():
    checker = OpenRouterBalanceChecker(api_key="sk-test", cache_ttl=300)
    mock_resp = _mock_response({"limit": 10.0, "usage": 1.0, "is_free_tier": False})
    call_count = 0

    async def _get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return mock_resp

    with patch("core.provider_balance.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = _get
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        first = await checker.get()
        second = await checker.get()

    assert call_count == 1
    assert first.source == "live"
    assert second.source == "cached"
    assert second.remaining_usd == first.remaining_usd


@pytest.mark.asyncio
async def test_openrouter_checker_refetches_after_ttl_expires():
    checker = OpenRouterBalanceChecker(api_key="sk-test", cache_ttl=0.01)
    mock_resp = _mock_response({"limit": 10.0, "usage": 1.0, "is_free_tier": False})
    call_count = 0

    async def _get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return mock_resp

    with patch("core.provider_balance.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = _get
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await checker.get()
        # Manually expire the cache by backdating the snapshot timestamp.
        checker._cached.checked_at = time.time() - 1.0
        await checker.get()

    assert call_count == 2


@pytest.mark.asyncio
async def test_openrouter_checker_invalidate_forces_refetch():
    checker = OpenRouterBalanceChecker(api_key="sk-test", cache_ttl=300)
    mock_resp = _mock_response({"limit": 5.0, "usage": 0.0, "is_free_tier": False})
    call_count = 0

    async def _get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return mock_resp

    with patch("core.provider_balance.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = _get
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await checker.get()
        checker.invalidate()
        second = await checker.get()

    assert call_count == 2
    assert second.source == "live"


# ---------------------------------------------------------------------------
# local_tracking_balance
# ---------------------------------------------------------------------------

def test_local_tracking_balance_computes_remaining():
    snap = local_tracking_balance(
        provider_id="anthropic",
        max_daily_usd=2.0,
        max_monthly_usd=20.0,
        day_used_usd=0.5,
        month_used_usd=3.0,
    )
    assert snap.provider_id == "anthropic"
    assert snap.available is True
    assert snap.source == "local_tracking"
    # daily remaining = 1.5, monthly remaining = 17.0; min wins
    assert snap.remaining_usd == 1.5


def test_local_tracking_balance_remaining_never_negative():
    snap = local_tracking_balance(
        provider_id="google",
        max_daily_usd=1.0,
        max_monthly_usd=10.0,
        day_used_usd=5.0,
        month_used_usd=5.0,
    )
    assert snap.remaining_usd == 0.0


def test_local_tracking_balance_no_cap_returns_zero():
    snap = local_tracking_balance(
        provider_id="openai",
        max_daily_usd=0.0,
        max_monthly_usd=0.0,
        day_used_usd=0.0,
        month_used_usd=0.0,
    )
    assert snap.remaining_usd == 0.0
    assert snap.available is True
