from unittest.mock import patch

import pytest

import core.cloud_budget as cloud_budget
from core.cloud_budget import CloudBudgetConfig, CloudBudgetGate, MultiProviderBudgetGate, ProviderBudgetConfig
from core.policy import CloudSpendPolicy


def _enabled_config(tmp_path, **overrides):
    values = {
        "enabled": True,
        "max_calls_per_turn": 1,
        "max_calls_per_session": 2,
        "max_calls_per_day": 3,
        "max_calls_per_month": 4,
        "max_daily_usd": 5.0,
        "max_monthly_usd": 50.0,
        "max_input_tokens_per_call": 16_000,
        "max_output_tokens_per_call": 2_048,
        "sqlite_path": str(tmp_path / "budget.db"),
    }
    values.update(overrides)
    return CloudBudgetConfig(**values)


@pytest.mark.asyncio
async def test_disabled_budget_gate_denies_cloud_spend():
    gate = CloudBudgetGate(CloudBudgetConfig(enabled=False))

    decision = await gate.reserve("session-1")
    availability = await gate.availability()

    assert decision.allowed is False
    assert decision.reason == "cloud_spillover_disabled"
    assert availability.allowed is False
    assert availability.reason == "cloud_spillover_disabled"


@pytest.mark.asyncio
async def test_budget_gate_availability_checks_caps_without_incrementing(tmp_path):
    gate = CloudBudgetGate(_enabled_config(tmp_path, max_calls_per_day=1))

    available = await gate.availability()
    first = await gate.reserve("session-1")
    after = await gate.availability()

    assert available.allowed is True
    assert available.reason == "within_budget"
    assert first.allowed is True
    assert after.allowed is False
    assert after.reason == "daily_call_cap_exhausted"


@pytest.mark.asyncio
async def test_budget_gate_enforces_session_cap(tmp_path):
    gate = CloudBudgetGate(_enabled_config(tmp_path, max_calls_per_session=1))

    first = await gate.reserve("session-1")
    second = await gate.reserve("session-1")

    assert first.allowed is True
    assert second.allowed is False
    assert second.reason == "session_call_cap_exhausted"


@pytest.mark.asyncio
async def test_budget_gate_enforces_daily_cap_across_sessions(tmp_path):
    gate = CloudBudgetGate(_enabled_config(tmp_path, max_calls_per_day=1))

    first = await gate.reserve("session-1")
    second = await gate.reserve("session-2")

    assert first.allowed is True
    assert second.allowed is False
    assert second.reason == "daily_call_cap_exhausted"


@pytest.mark.asyncio
async def test_budget_gate_enforces_daily_usd_cap(tmp_path):
    gate = CloudBudgetGate(_enabled_config(tmp_path, max_daily_usd=0.01))

    await gate.record_usage("session-1", "ClaudeProvider", "sonnet", 1000, 1000)
    decision = await gate.reserve("session-2", estimated_input_tokens=1000, requested_output_tokens=1000)

    assert decision.allowed is False
    assert decision.reason == "daily_usd_cap_exhausted"


@pytest.mark.asyncio
async def test_budget_gate_enforces_input_token_cap(tmp_path):
    gate = CloudBudgetGate(_enabled_config(tmp_path, max_input_tokens_per_call=10))

    decision = await gate.reserve("session-1", estimated_input_tokens=11, requested_output_tokens=10)

    assert decision.allowed is False
    assert decision.reason == "input_token_cap_exceeded"


@pytest.mark.asyncio
async def test_missing_usage_metadata_blocks_future_cloud_spend(tmp_path):
    gate = CloudBudgetGate(_enabled_config(tmp_path))

    result = await gate.record_usage("session-1", "ClaudeProvider", "sonnet", None, None)
    next_decision = await gate.reserve("session-2", estimated_input_tokens=1000, requested_output_tokens=1000)

    assert result.allowed is False
    assert result.reason == "missing_usage_metadata"
    assert next_decision.allowed is False
    assert next_decision.reason == "budget_accounting_blocked"


@pytest.mark.asyncio
async def test_invalid_negative_usage_metadata_blocks_future_cloud_spend(tmp_path):
    gate = CloudBudgetGate(_enabled_config(tmp_path))

    result = await gate.record_usage("session-1", "ClaudeProvider", "sonnet", -1, 10)
    next_decision = await gate.reserve("session-2", estimated_input_tokens=1000, requested_output_tokens=1000)

    assert result.allowed is False
    assert result.reason == "invalid_usage_metadata"
    assert next_decision.allowed is False
    assert next_decision.reason == "budget_accounting_blocked"


@pytest.mark.asyncio
async def test_clear_accounting_block_unblocks_future_cloud_spend(tmp_path):
    """The admin recovery path: clear a stuck accounting_blocked flag."""
    gate = CloudBudgetGate(_enabled_config(tmp_path))

    # Trigger an accounting block by sending invalid usage metadata.
    await gate.record_usage("session-1", "ClaudeProvider", "sonnet", None, None)
    blocked = await gate.reserve("session-2", estimated_input_tokens=100, requested_output_tokens=100)
    assert blocked.reason == "budget_accounting_blocked"

    # Operator clears the block.
    cleared = await gate.clear_accounting_block()
    assert cleared.allowed is True
    assert cleared.reason == "accounting_block_cleared"

    # Future calls go through (subject to the normal caps).
    after = await gate.reserve("session-3", estimated_input_tokens=100, requested_output_tokens=100)
    assert after.allowed is True
    assert after.reason == "within_budget"

    snapshot = await gate.snapshot()
    assert snapshot["accounting_blocked"] is False
    assert snapshot["last_accounting_error"] == ""


@pytest.mark.asyncio
async def test_clear_accounting_block_noop_when_spillover_disabled():
    gate = CloudBudgetGate(CloudBudgetConfig(enabled=False))
    decision = await gate.clear_accounting_block()
    assert decision.allowed is False
    assert decision.reason == "cloud_spillover_disabled"


@pytest.mark.asyncio
async def test_negative_reserve_estimates_cannot_reduce_projected_spend(tmp_path):
    gate = CloudBudgetGate(_enabled_config(tmp_path, max_daily_usd=0.01, max_calls_per_day=10))
    await gate.record_usage("session-1", "ClaudeProvider", "sonnet", 1000, 1000)

    decision = await gate.reserve("session-2", estimated_input_tokens=-1_000_000, requested_output_tokens=-1_000_000)

    assert decision.allowed is False
    assert decision.reason == "daily_usd_cap_exhausted"


@pytest.mark.asyncio
async def test_enabled_budget_gate_persists_counts_across_instances(tmp_path):
    config = _enabled_config(tmp_path, max_calls_per_session=1)
    first_gate = CloudBudgetGate(config)
    second_gate = CloudBudgetGate(config)

    first = await first_gate.reserve("session-1")
    second = await second_gate.reserve("session-1")

    assert first.allowed is True
    assert second.allowed is False
    assert second.reason == "session_call_cap_exhausted"


@pytest.mark.asyncio
async def test_budget_gate_init_short_circuits_after_store_ready(tmp_path, monkeypatch):
    calls = []

    class FakeDb:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, *_args, **_kwargs):
            return None

        async def commit(self):
            return None

    def connect(path):
        calls.append(path)
        return FakeDb()

    monkeypatch.setattr(cloud_budget.aiosqlite, "connect", connect)
    gate = CloudBudgetGate(_enabled_config(tmp_path))

    await gate.init()
    await gate.init()

    assert calls == [gate.config.sqlite_path]


def test_cloud_spend_policy_requires_spillover_enablement():
    with patch.dict(
        "os.environ",
        {
            "ENABLE_CLAUDE_FALLBACK": "1",
            "ENABLE_CLOUD_SPILLOVER": "0",
            "CLOUD_SPILLOVER_MAX_CALLS_PER_TURN": "1",
            "CLOUD_SPILLOVER_MAX_CALLS_PER_SESSION": "1",
        },
        clear=False,
    ):
        policy = CloudSpendPolicy.from_env()

    assert policy.claude_enabled is False
    assert policy.max_cloud_calls_per_turn == 1


def test_cloud_spend_policy_enables_claude_only_with_spillover_and_caps(tmp_path):
    with patch.dict(
        "os.environ",
        {
            "ENABLE_CLAUDE_FALLBACK": "1",
            "ENABLE_CLOUD_SPILLOVER": "1",
            "CLOUD_SPILLOVER_MAX_CALLS_PER_TURN": "1",
            "CLOUD_SPILLOVER_MAX_CALLS_PER_SESSION": "2",
            "CLOUD_SPILLOVER_MAX_DAILY_USD": "5",
            "CLOUD_SPILLOVER_MAX_MONTHLY_USD": "50",
            "CLOUD_SPILLOVER_MAX_INPUT_TOKENS_PER_CALL": "16000",
            "CLOUD_SPILLOVER_MAX_OUTPUT_TOKENS_PER_CALL": "2048",
            "CLOUD_SPILLOVER_DB_PATH": str(tmp_path / "budget.db"),
        },
        clear=False,
    ):
        policy = CloudSpendPolicy.from_env()
        config = CloudBudgetConfig.from_env()

    assert policy.claude_enabled is True
    assert policy.max_cloud_calls_per_turn == 1
    assert config.enabled is True
    assert config.max_calls_per_session == 2
    assert config.max_daily_usd == 5
    assert config.max_monthly_usd == 50
    assert config.max_output_tokens_per_call == 2048
    assert config.sqlite_path.endswith("budget.db")


# ---------------------------------------------------------------------------
# ProviderBudgetConfig tests
# ---------------------------------------------------------------------------

def test_provider_budget_config_from_env_defaults_disabled():
    with patch.dict("os.environ", {}, clear=False):
        cfg = ProviderBudgetConfig.from_env("openrouter")
    assert cfg.provider_id == "openrouter"
    assert cfg.enabled is False
    assert cfg.max_daily_usd == 0.0
    assert cfg.max_monthly_usd == 0.0


def test_provider_budget_config_from_env_reads_vars():
    with patch.dict(
        "os.environ",
        {
            "OPENROUTER_BUDGET_ENABLED": "true",
            "OPENROUTER_BUDGET_MAX_DAILY_USD": "5.0",
            "OPENROUTER_BUDGET_MAX_MONTHLY_USD": "50.0",
        },
        clear=False,
    ):
        cfg = ProviderBudgetConfig.from_env("openrouter")
    assert cfg.enabled is True
    assert cfg.max_daily_usd == 5.0
    assert cfg.max_monthly_usd == 50.0


# ---------------------------------------------------------------------------
# MultiProviderBudgetGate tests
# ---------------------------------------------------------------------------

def _multi_gate(tmp_path, **provider_overrides):
    global_config = _enabled_config(tmp_path, max_daily_usd=100.0, max_monthly_usd=1000.0)
    global_gate = CloudBudgetGate(global_config)
    provider_configs = {
        "openrouter": ProviderBudgetConfig(
            provider_id="openrouter",
            enabled=True,
            max_daily_usd=provider_overrides.get("or_daily", 5.0),
            max_monthly_usd=provider_overrides.get("or_monthly", 50.0),
        ),
        "anthropic": ProviderBudgetConfig(
            provider_id="anthropic",
            enabled=True,
            max_daily_usd=provider_overrides.get("ant_daily", 2.0),
            max_monthly_usd=provider_overrides.get("ant_monthly", 20.0),
        ),
    }
    return MultiProviderBudgetGate(global_gate=global_gate, provider_configs=provider_configs)


@pytest.mark.asyncio
async def test_multi_gate_gate_for_returns_provider_gate(tmp_path):
    mg = _multi_gate(tmp_path)
    or_gate = mg.gate_for("openrouter")
    ant_gate = mg.gate_for("anthropic")
    default_gate = mg.gate_for("google")

    assert or_gate is not mg.global_gate
    assert ant_gate is not mg.global_gate
    assert default_gate is mg.global_gate


@pytest.mark.asyncio
async def test_multi_gate_reserve_allows_within_provider_cap(tmp_path):
    mg = _multi_gate(tmp_path, or_daily=5.0)
    decision = await mg.reserve("session-1", provider_id="openrouter")
    assert decision.allowed is True


@pytest.mark.asyncio
async def test_multi_gate_provider_usd_cap_blocks_independently(tmp_path):
    mg = _multi_gate(tmp_path, or_daily=0.001)
    await mg.record_usage(
        "session-1", "OpenAICompatibleProvider", "openrouter/auto",
        5000, 5000, 0.001, 0.001, provider_id="openrouter",
    )
    decision = await mg.reserve(
        "session-2",
        estimated_input_tokens=1000,
        requested_output_tokens=500,
        provider_id="openrouter",
    )
    assert decision.allowed is False
    assert "usd_cap_exhausted" in decision.reason


@pytest.mark.asyncio
async def test_multi_gate_anthropic_cap_does_not_block_openrouter(tmp_path):
    mg = _multi_gate(tmp_path, ant_daily=0.001, or_daily=5.0)
    await mg.record_usage(
        "session-1", "ClaudeProvider", "claude-sonnet",
        5000, 5000, 0.001, 0.001, provider_id="anthropic",
    )
    ant_decision = await mg.reserve(
        "session-2", estimated_input_tokens=100, requested_output_tokens=100, provider_id="anthropic"
    )
    or_decision = await mg.reserve(
        "session-3", estimated_input_tokens=100, requested_output_tokens=100, provider_id="openrouter"
    )
    assert ant_decision.allowed is False
    assert or_decision.allowed is True


@pytest.mark.asyncio
async def test_multi_gate_global_cap_blocks_all_providers(tmp_path):
    mg = _multi_gate(tmp_path)
    # _enabled_config sets max_calls_per_day=3; exhaust it.
    await mg.reserve("s1", provider_id="openrouter")
    await mg.reserve("s2", provider_id="anthropic")
    await mg.reserve("s3", provider_id="google")
    decision = await mg.reserve("s4", provider_id="openrouter")
    assert decision.allowed is False
    assert decision.reason == "daily_call_cap_exhausted"


@pytest.mark.asyncio
async def test_multi_gate_snapshot_includes_provider_section(tmp_path):
    mg = _multi_gate(tmp_path)
    snap = await mg.snapshot()
    assert "providers" in snap
    assert "openrouter" in snap["providers"]
    assert "anthropic" in snap["providers"]
    or_entry = snap["providers"]["openrouter"]
    assert or_entry["enabled"] is True
    assert or_entry["max_daily_usd"] == 5.0


@pytest.mark.asyncio
async def test_multi_gate_config_property_exposes_global_config(tmp_path):
    mg = _multi_gate(tmp_path)
    assert mg.config is mg.global_gate.config


@pytest.mark.asyncio
async def test_multi_gate_from_env_builds_enabled_provider_buckets(tmp_path):
    with patch.dict(
        "os.environ",
        {
            "OPENROUTER_BUDGET_ENABLED": "true",
            "OPENROUTER_BUDGET_MAX_DAILY_USD": "5",
            "OPENROUTER_BUDGET_MAX_MONTHLY_USD": "50",
            "ANTHROPIC_BUDGET_ENABLED": "true",
            "ANTHROPIC_BUDGET_MAX_DAILY_USD": "2",
            "ANTHROPIC_BUDGET_MAX_MONTHLY_USD": "20",
        },
        clear=False,
    ):
        global_gate = CloudBudgetGate(_enabled_config(tmp_path))
        mg = MultiProviderBudgetGate.from_env(global_gate)

    assert "openrouter" in mg._gates
    assert "anthropic" in mg._gates
    assert mg._gates["openrouter"].config.max_daily_usd == 5.0
    assert mg._gates["anthropic"].config.max_daily_usd == 2.0
    # google/openai not enabled -> no dedicated gates
    assert "google" not in mg._gates
    assert "openai" not in mg._gates
@pytest.mark.asyncio
async def test_multi_gate_provider_config_and_gate_accessors(tmp_path):
    """Public accessor methods expose provider config and gate without touching privates."""
    mg = _multi_gate(tmp_path)
    cfg = mg.provider_config("openrouter")
    assert cfg is not None
    assert cfg.max_daily_usd == 5.0

    gate = mg.provider_gate("openrouter")
    assert gate is not None

    # Unknown provider returns None
    assert mg.provider_config("unknown_provider") is None
    assert mg.provider_gate("unknown_provider") is None


@pytest.mark.asyncio
async def test_refresh_headroom_cache_populates_cache(tmp_path):
    """refresh_headroom_cache() fills _headroom_cache; headroom() reads it."""
    mg = _multi_gate(tmp_path)
    # Before refresh, cache is empty; headroom() defaults to 1.0
    assert mg.headroom("openrouter") == 1.0

    await mg.refresh_headroom_cache()
    # No spend recorded yet -> headroom should be 1.0 (full)
    assert mg.headroom("openrouter") == pytest.approx(1.0)
    # Provider with no gate returns 1.0 from cache
    assert mg.headroom("google") == 1.0


@pytest.mark.asyncio
async def test_provider_budgets_patch_mutates_caps(tmp_path):
    """provider_budgets in AdminPolicyPatch mutates in-memory ProviderBudgetConfig caps."""
    from unittest.mock import AsyncMock, MagicMock
    from api.admin import AdminPolicyPatch, _apply_admin_policy
    from core.cloud_budget import CloudBudgetConfig, CloudBudgetGate, MultiProviderBudgetGate, ProviderBudgetConfig
    from core.router import LLMRouter, ProviderRoute
    from core.orchestrator import Orchestrator
    from core.policy import CloudSpendPolicy
    from core.chat_orchestrator import ChatTurnOrchestrator
    from api.services import AppServices, ChatMetrics

    db_path = str(tmp_path / "test.db")
    mock_provider = MagicMock()
    mock_provider.is_healthy = AsyncMock(return_value=False)
    mock_provider.health_status = None
    mock_provider.close = AsyncMock()
    mock_provider.model = "stub"
    mock_provider.profile_name = "stub"
    mock_provider.provider_role = "primary"
    mock_provider.cost_tier = "local"
    mock_provider.allowed_response_policies = ["local_only"]
    mock_provider.capabilities = []
    mock_provider.tool_calling_quality = "none"

    global_gate = CloudBudgetGate(CloudBudgetConfig(enabled=False, sqlite_path=db_path))
    provider_configs = {
        "openrouter": ProviderBudgetConfig(
            provider_id="openrouter", enabled=True, max_daily_usd=5.0, max_monthly_usd=50.0
        ),
    }
    multi_gate = MultiProviderBudgetGate(global_gate, provider_configs)
    route = ProviderRoute(
        name="stub", provider=mock_provider, provider_type="LocalProvider",
        model="stub", role="primary", cost_tier="local",
        allowed_response_policies=["local_only"],
    )
    router = LLMRouter(primary=mock_provider, routes=[route], cloud_budget=multi_gate)
    orchestrator = Orchestrator(CloudSpendPolicy())
    chat_orc = MagicMock(spec=ChatTurnOrchestrator)
    services = AppServices(
        local_provider=mock_provider,
        secondary_provider=None,
        cloud_budget=multi_gate,
        llm_router=router,
        orchestrator=orchestrator,
        chat_turns=chat_orc,
        balance_checker=None,
        key_store=None,
        chat_metrics=ChatMetrics(),
    )

    assert multi_gate.provider_config("openrouter").max_daily_usd == 5.0
    patch_obj = AdminPolicyPatch(provider_budgets={"openrouter": {"max_daily_usd": 12.5, "max_monthly_usd": 100.0}})
    await _apply_admin_policy(services, patch_obj)

    assert multi_gate.provider_config("openrouter").max_daily_usd == pytest.approx(12.5)
    assert multi_gate.provider_config("openrouter").max_monthly_usd == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_provider_budgets_patch_rejects_both_zero_caps(tmp_path):
    """_apply_admin_policy raises HTTPException when both caps are zeroed on an enabled provider."""
    from unittest.mock import AsyncMock, MagicMock
    from fastapi import HTTPException
    from api.admin import AdminPolicyPatch, _apply_admin_policy
    from core.cloud_budget import CloudBudgetConfig, CloudBudgetGate, MultiProviderBudgetGate, ProviderBudgetConfig
    from core.router import LLMRouter, ProviderRoute
    from core.orchestrator import Orchestrator
    from core.policy import CloudSpendPolicy
    from core.chat_orchestrator import ChatTurnOrchestrator
    from api.services import AppServices, ChatMetrics

    db_path = str(tmp_path / "test.db")
    mock_provider = MagicMock()
    mock_provider.is_healthy = AsyncMock(return_value=False)
    mock_provider.health_status = None
    mock_provider.close = AsyncMock()
    mock_provider.model = "stub"
    mock_provider.profile_name = "stub"
    mock_provider.provider_role = "primary"
    mock_provider.cost_tier = "local"
    mock_provider.allowed_response_policies = ["local_only"]
    mock_provider.capabilities = []
    mock_provider.tool_calling_quality = "none"

    global_gate = CloudBudgetGate(CloudBudgetConfig(enabled=False, sqlite_path=db_path))
    provider_configs = {
        "openrouter": ProviderBudgetConfig(
            provider_id="openrouter", enabled=True, max_daily_usd=5.0, max_monthly_usd=50.0
        ),
    }
    multi_gate = MultiProviderBudgetGate(global_gate, provider_configs)
    route = ProviderRoute(
        name="stub", provider=mock_provider, provider_type="LocalProvider",
        model="stub", role="primary", cost_tier="local",
        allowed_response_policies=["local_only"],
    )
    router = LLMRouter(primary=mock_provider, routes=[route], cloud_budget=multi_gate)
    orchestrator = Orchestrator(CloudSpendPolicy())
    chat_orc = MagicMock(spec=ChatTurnOrchestrator)
    services = AppServices(
        local_provider=mock_provider,
        secondary_provider=None,
        cloud_budget=multi_gate,
        llm_router=router,
        orchestrator=orchestrator,
        chat_turns=chat_orc,
        balance_checker=None,
        key_store=None,
        chat_metrics=ChatMetrics(),
    )

    patch_obj = AdminPolicyPatch(provider_budgets={"openrouter": {"max_daily_usd": 0.0, "max_monthly_usd": 0.0}})
    with pytest.raises(HTTPException) as exc_info:
        await _apply_admin_policy(services, patch_obj)
    assert exc_info.value.status_code == 400
    assert "cap" in str(exc_info.value.detail).lower() or "daily" in str(exc_info.value.detail).lower()
