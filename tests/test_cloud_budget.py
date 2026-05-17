from unittest.mock import patch

import pytest

import core.cloud_budget as cloud_budget
from core.cloud_budget import CloudBudgetConfig, CloudBudgetGate
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
    assert after.reason == "daily_cap_exhausted"


@pytest.mark.asyncio
async def test_budget_gate_enforces_session_cap(tmp_path):
    gate = CloudBudgetGate(_enabled_config(tmp_path, max_calls_per_session=1))

    first = await gate.reserve("session-1")
    second = await gate.reserve("session-1")

    assert first.allowed is True
    assert second.allowed is False
    assert second.reason == "session_cap_exhausted"


@pytest.mark.asyncio
async def test_budget_gate_enforces_daily_cap_across_sessions(tmp_path):
    gate = CloudBudgetGate(_enabled_config(tmp_path, max_calls_per_day=1))

    first = await gate.reserve("session-1")
    second = await gate.reserve("session-2")

    assert first.allowed is True
    assert second.allowed is False
    assert second.reason == "daily_cap_exhausted"


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
    assert second.reason == "session_cap_exhausted"


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
