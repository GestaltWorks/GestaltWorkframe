"""
Integration test: local provider failure causes failover to Claude.

Marked `integration` — requires ANTHROPIC_API_KEY in env.
Run with: uv run pytest -m integration tests/test_failover_integration.py -v
"""
import os
import pytest
from core.cloud_budget import CloudBudgetConfig, CloudBudgetGate
from core.providers import LocalProvider, ClaudeProvider
from core.router import LLMRouter

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


@pytest.mark.integration
@pytest.mark.skipif(not ANTHROPIC_API_KEY, reason="ANTHROPIC_API_KEY not set")
async def test_local_failure_falls_back_to_claude_and_returns_real_response(tmp_path):
    """Primary at a dead port fails; router delivers a real Claude response."""
    primary = LocalProvider(base_url="http://localhost:1/v1", model="dead")
    secondary = ClaudeProvider(
        api_key=ANTHROPIC_API_KEY,
        model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
    )
    budget = CloudBudgetGate(
        CloudBudgetConfig(
            enabled=True,
            max_calls_per_turn=1,
            max_calls_per_session=1,
            max_calls_per_day=1,
            max_calls_per_month=1,
            max_daily_usd=5,
            max_monthly_usd=50,
            max_input_tokens_per_call=16_000,
            max_output_tokens_per_call=2_048,
            sqlite_path=str(tmp_path / "budget.db"),
        )
    )
    router = LLMRouter(primary=primary, secondary=secondary, error_threshold=1, cloud_budget=budget)

    messages = [{"role": "user", "content": "Reply with exactly: FAILOVER_OK"}]
    result = await router.chat(messages, cloud_allowed=True)

    # Result is an Anthropic message object with content blocks
    text = next(
        (block.text for block in result.content if block.type == "text"), ""
    )
    assert "FAILOVER_OK" in text


@pytest.mark.integration
@pytest.mark.skipif(not ANTHROPIC_API_KEY, reason="ANTHROPIC_API_KEY not set")
async def test_local_failure_with_cloud_disabled_does_not_hit_claude():
    """Primary fails but cloud_allowed=False — must return degraded response, never Claude."""
    primary = LocalProvider(base_url="http://localhost:1/v1", model="dead")
    secondary = ClaudeProvider(
        api_key=ANTHROPIC_API_KEY,
        model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
    )
    router = LLMRouter(primary=primary, secondary=secondary, error_threshold=1)

    messages = [{"role": "user", "content": "hello"}]
    result = await router.chat(messages, cloud_allowed=False)

    assert isinstance(result, dict)
    assert "unavailable" in result["content"].lower()
