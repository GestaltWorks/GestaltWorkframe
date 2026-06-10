from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gestaltworkframe.api.main as api_main
from gestaltworkframe.core.policy import CloudSpendPolicy
from gestaltworkframe.core.router import LLMRouter


def _provider(model: str, healthy: bool = True):
    provider = MagicMock()
    provider.model = model
    provider.is_healthy = AsyncMock(return_value=healthy)
    provider.health_status = None
    provider.__class__.__name__ = "FakeProvider"
    return provider


def _request(router: LLMRouter):
    services = SimpleNamespace(llm_router=router)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(services=services)))


def _request_with_cloud_enabled(router: LLMRouter):
    policy = CloudSpendPolicy(low_cost_enabled=True, max_cloud_calls_per_turn=1)
    services = SimpleNamespace(
        llm_router=router,
        orchestrator=SimpleNamespace(cloud_policy=policy),
        cloud_budget=SimpleNamespace(
            config=SimpleNamespace(enabled=True, max_output_tokens_per_call=2048),
            availability=AsyncMock(return_value=SimpleNamespace(allowed=True, reason="within_budget")),
        ),
    )
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(services=services)))


def _request_with_cloud_budget_blocked(router: LLMRouter):
    policy = CloudSpendPolicy(low_cost_enabled=True, max_cloud_calls_per_turn=1)
    services = SimpleNamespace(
        llm_router=router,
        orchestrator=SimpleNamespace(cloud_policy=policy),
        cloud_budget=SimpleNamespace(
            config=SimpleNamespace(enabled=True, max_output_tokens_per_call=0),
            availability=AsyncMock(return_value=SimpleNamespace(allowed=False, reason="daily_cap_zero")),
        ),
    )
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(services=services)))


@pytest.mark.asyncio
async def test_provider_health_reports_degraded_when_local_unavailable():
    primary = _provider("local-model", healthy=False)
    router = LLMRouter(primary=primary, secondary=None, error_threshold=2)
    router._breaker_open = True
    router._error_count = 2

    result = await api_main.provider_health_check(_request(router))

    assert result["status"] == "degraded"
    assert result["local_model_available"] is False
    assert result["cloud_fallback_configured"] is False
    assert result["primary"]["configured"] is True
    assert result["primary"]["healthy"] is False
    assert result["secondary"]["configured"] is False
    assert "circuit_breaker" not in result
    assert "provider_type" not in result["primary"]
    assert "model" not in result["primary"]


@pytest.mark.asyncio
async def test_provider_health_reports_secondary_when_configured():
    primary = _provider("local-model", healthy=True)
    secondary = _provider("claude-haiku", healthy=True)
    router = LLMRouter(primary=primary, secondary=secondary, error_threshold=3)

    result = await api_main.provider_health_check(_request(router))

    assert result["status"] == "ok"
    assert result["local_model_available"] is True
    assert result["cloud_fallback_configured"] is True
    assert result["secondary"]["configured"] is True
    assert result["secondary"]["healthy"] is True
    assert result["models"][0]["callable"] is True
    assert all("available_models" not in model for model in result["models"])


@pytest.mark.asyncio
async def test_provider_health_uses_runtime_cloud_policy_without_leaking_details():
    primary = _provider("local-model", healthy=False)
    secondary = _provider("claude-haiku", healthy=True)
    router = LLMRouter(primary=primary, secondary=secondary, error_threshold=3)

    result = await api_main.provider_health_check(_request_with_cloud_enabled(router))

    assert result["status"] == "ok"
    assert result["primary"]["callable"] is False
    assert result["secondary"]["callable"] is True
    assert result["cloud_fallback_ready"] is True
    assert result["cloud_fallback_reason"] == "ready"
    assert "cloud_budget" not in result
    assert "allowed_response_policies" not in result["secondary"]


@pytest.mark.asyncio
async def test_provider_health_does_not_report_cloud_callable_when_budget_preflight_blocks():
    primary = _provider("local-model", healthy=False)
    secondary = _provider("claude-haiku", healthy=True)
    router = LLMRouter(primary=primary, secondary=secondary, error_threshold=3)

    result = await api_main.provider_health_check(_request_with_cloud_budget_blocked(router))

    assert result["status"] == "degraded"
    assert result["secondary"]["healthy"] is True
    assert result["secondary"]["callable"] is False
    assert result["cloud_fallback_ready"] is False
    assert result["cloud_fallback_reason"] == "budget_caps_unset"


@pytest.mark.asyncio
async def test_admin_policy_cloud_enable_defaults_missing_token_caps():
    route = SimpleNamespace(name="claude-haiku")
    router = SimpleNamespace(
        routes=[route],
        set_route_enabled=lambda name, enabled: None,
        set_routing_strategy=lambda strategy: None,
        runtime_manager=None,
    )
    budget = SimpleNamespace(
        config=SimpleNamespace(
            enabled=False,
            max_calls_per_turn=0,
            max_calls_per_session=0,
            max_calls_per_day=0,
            max_calls_per_month=0,
            max_daily_usd=0.0,
            max_monthly_usd=0.0,
            max_input_tokens_per_call=0,
            max_output_tokens_per_call=0,
        ),
        init=AsyncMock(),
    )
    services = SimpleNamespace(
        cloud_budget=budget,
        orchestrator=SimpleNamespace(cloud_policy=CloudSpendPolicy()),
        llm_router=router,
    )

    await api_main._apply_admin_policy(
        services,
        api_main.AdminPolicyPatch(cloud_spillover_enabled=True, low_cost_enabled=True, max_calls_per_turn=1),
    )

    assert budget.config.max_input_tokens_per_call == api_main.DEFAULT_CLOUD_INPUT_TOKEN_CAP
    assert budget.config.max_output_tokens_per_call == api_main.DEFAULT_CLOUD_OUTPUT_TOKEN_CAP