"""Tests for the public health endpoint helpers.

The /health/providers wiring needs the full app/services graph; these cover the
pure and semi-pure helpers directly: status shaping, cloud-status
classification, the public cloud-control gate, and block-reason redaction.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gestaltworkframe.api import health


async def test_health_check_liveness():
    assert await health.health_check() == {"status": "ok"}


# --- _provider_status -------------------------------------------------------

async def test_provider_status_none_is_unconfigured():
    assert await health._provider_status("primary", None) == {
        "role": "primary",
        "configured": False,
        "healthy": False,
    }


async def test_provider_status_reports_healthy_provider():
    provider = SimpleNamespace(
        is_healthy=_ok_healthy,
        model="local-model",
        profile_name="env-local",
        provider_role="primary",
        cost_tier="local",
        allowed_response_policies=["local_only"],
    )
    status = await health._provider_status("primary", provider)
    assert status["configured"] is True
    assert status["healthy"] is True
    assert status["model"] == "local-model"
    assert status["provider_type"] == "SimpleNamespace"


async def test_provider_status_swallows_health_errors():
    provider = SimpleNamespace(is_healthy=_raise_healthy, model="m")
    status = await health._provider_status("secondary", provider)
    assert status["configured"] is True
    assert status["healthy"] is False


async def _ok_healthy():
    return True


async def _raise_healthy():
    raise RuntimeError("provider down")


# --- _is_cloud_status / _public_provider_group ------------------------------

def test_is_cloud_status_by_cost_tier():
    assert health._is_cloud_status({"cost_tier": "premium"}) is True
    assert health._is_cloud_status({"cost_tier": "low_cost"}) is True
    assert health._is_cloud_status({"cost_tier": "local"}) is False
    assert health._is_cloud_status({}) is False


def test_public_provider_group_aggregates_any():
    group = health._public_provider_group(
        "primary",
        [{"configured": False, "healthy": False, "callable": False},
         {"configured": True, "healthy": False, "callable": True}],
    )
    assert group == {"role": "primary", "configured": True, "healthy": False, "callable": True}


def test_public_provider_group_empty_is_all_false():
    assert health._public_provider_group("secondary", []) == {
        "role": "secondary",
        "configured": False,
        "healthy": False,
        "callable": False,
    }


# --- _public_cloud_block_reason ---------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("daily_zero", "budget_caps_unset"),
        ("budget_store_unavailable", "budget_store_unavailable"),
        ("budget_accounting_blocked", "budget_accounting_blocked"),
        ("monthly_exhausted", "budget_exhausted"),
        ("per_turn_exceeded", "request_exceeds_budget_caps"),
        ("something_else", "cloud_budget_blocked"),
    ],
)
def test_public_cloud_block_reason(raw, expected):
    assert health._public_cloud_block_reason(raw) == expected


# --- _public_cloud_health_controls ------------------------------------------

async def test_cloud_controls_not_configured_without_budget_or_policy():
    services = SimpleNamespace(cloud_budget=None, orchestrator=None)
    allowed, tiers, reason = await health._public_cloud_health_controls(services)
    assert allowed is False and tiers == set() and reason == "not_configured"


async def test_cloud_controls_policy_disabled_when_no_tiers():
    budget = SimpleNamespace(enabled=True, max_output_tokens_per_call=100)
    policy = SimpleNamespace(max_cloud_calls_per_turn=0, low_cost_enabled=False, claude_enabled=False)
    services = SimpleNamespace(
        cloud_budget=SimpleNamespace(config=budget),
        orchestrator=SimpleNamespace(cloud_policy=policy),
    )
    allowed, tiers, reason = await health._public_cloud_health_controls(services)
    assert allowed is False and reason == "policy_disabled"


async def test_cloud_controls_ready_when_budget_allows():
    budget = SimpleNamespace(enabled=True, max_output_tokens_per_call=100)
    policy = SimpleNamespace(max_cloud_calls_per_turn=2, low_cost_enabled=True, claude_enabled=True)

    async def availability(*, requested_output_tokens):
        return SimpleNamespace(allowed=True, reason="ok")

    services = SimpleNamespace(
        cloud_budget=SimpleNamespace(config=budget, availability=availability),
        orchestrator=SimpleNamespace(cloud_policy=policy),
    )
    allowed, tiers, reason = await health._public_cloud_health_controls(services)
    assert allowed is True
    assert tiers == {"low_cost", "premium"}
    assert reason == "ready"


async def test_cloud_controls_blocks_when_budget_denies():
    budget = SimpleNamespace(enabled=True, max_output_tokens_per_call=100)
    policy = SimpleNamespace(max_cloud_calls_per_turn=2, low_cost_enabled=True, claude_enabled=False)

    async def availability(*, requested_output_tokens):
        return SimpleNamespace(allowed=False, reason="monthly_exhausted")

    services = SimpleNamespace(
        cloud_budget=SimpleNamespace(config=budget, availability=availability),
        orchestrator=SimpleNamespace(cloud_policy=policy),
    )
    allowed, tiers, reason = await health._public_cloud_health_controls(services)
    assert allowed is False
    assert tiers == {"low_cost"}
    assert reason == "budget_exhausted"
