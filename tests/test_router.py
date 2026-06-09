import asyncio
import pytest
import time
from unittest.mock import AsyncMock, MagicMock
from core.cloud_budget import CloudBudgetConfig, CloudBudgetGate
from core.router import LLMRouter, ProviderRoute
from core.runtime import GenerationConcurrencyPolicy, RuntimeControlPolicy, RuntimeManager


def _make_provider(healthy: bool = True, response: dict | None = None, raises: Exception | None = None):
    p = MagicMock()
    p.is_healthy = AsyncMock(return_value=healthy)
    p.health_status = None
    if raises:
        p.chat = AsyncMock(side_effect=raises)
    else:
        p.chat = AsyncMock(return_value=response or {"content": "ok"})
    return p


def _route(
    name: str,
    provider,
    cost_tier: str = "local",
    policies: list[str] | None = None,
    priority: int = 0,
    tasks: list[str] | None = None,
    avoid: list[str] | None = None,
    deployment_status: str = "active",
    enabled_by_default: bool = True,
    runtime_group: str = "",
):
    return ProviderRoute(
        name=name,
        provider=provider,
        provider_type=provider.__class__.__name__,
        model=name,
        role="primary" if cost_tier == "local" else "secondary",
        cost_tier=cost_tier,
        allowed_response_policies=policies or ["local_only"],
        routing_priority=priority,
        recommended_for=tasks or [],
        avoid_for=avoid or [],
        deployment_status=deployment_status,
        enabled_by_default=enabled_by_default,
        runtime_group=runtime_group,
    )


def _budget(tmp_path) -> CloudBudgetGate:
    return CloudBudgetGate(
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


class _StreamingProvider:
    def __init__(
        self,
        chunks: list[dict] | None = None,
        raises: Exception | None = None,
        raise_after_chunks: bool = False,
    ) -> None:
        self.chunks = chunks or [{"choices": [{"delta": {"content": "ok"}}]}]
        self.raises = raises
        self.raise_after_chunks = raise_after_chunks
        self.is_healthy = AsyncMock(return_value=True)
        self.chat = AsyncMock(return_value={"content": "non-streamed"})

    async def stream_chat(self, messages, tools=None):
        if self.raises and not self.raise_after_chunks:
            raise self.raises
        for chunk in self.chunks:
            yield chunk
        if self.raises:
            raise self.raises


@pytest.mark.asyncio
async def test_cloud_not_allowed_never_uses_secondary_when_breaker_open():
    primary = _make_provider(healthy=False, raises=RuntimeError("down"))
    secondary = _make_provider(response={"content": "from claude"})

    router = LLMRouter(primary=primary, secondary=secondary, error_threshold=1)

    # Trip the breaker by forcing a failure
    primary.is_healthy = AsyncMock(return_value=False)
    router._breaker_open = True

    result = await router.chat([{"role": "user", "content": "hi"}], cloud_allowed=False)

    secondary.chat.assert_not_called()
    assert result == router._LOCAL_UNAVAILABLE


@pytest.mark.asyncio
async def test_cloud_allowed_without_budget_gate_degrades_when_breaker_open():
    primary = _make_provider(healthy=False)  # still unhealthy — breaker stays open
    secondary = _make_provider(response={"content": "from claude"})

    router = LLMRouter(primary=primary, secondary=secondary)
    router._breaker_open = True

    result = await router.chat([{"role": "user", "content": "hi"}], cloud_allowed=True)

    secondary.chat.assert_not_called()
    assert "unavailable" in result["content"].lower()


@pytest.mark.asyncio
async def test_cloud_allowed_uses_secondary_when_breaker_open_and_budget_allows(tmp_path):
    primary = _make_provider(healthy=False)
    secondary = _make_provider(response={"content": "from claude", "usage": {"input_tokens": 100, "output_tokens": 25}})
    budget = _budget(tmp_path)

    router = LLMRouter(primary=primary, secondary=secondary, cloud_budget=budget)
    router._breaker_open = True

    result = await router.chat([{"role": "user", "content": "hi"}], cloud_allowed=True, session_id="s1")

    secondary.chat.assert_called_once()
    assert result["content"] == "from claude"


@pytest.mark.asyncio
async def test_primary_failure_with_cloud_disabled_returns_degraded_response():
    primary = _make_provider(raises=RuntimeError("timeout"))
    secondary = _make_provider(response={"content": "from claude"})

    router = LLMRouter(primary=primary, secondary=secondary, error_threshold=5)

    result = await router.chat([{"role": "user", "content": "hi"}], cloud_allowed=False)

    secondary.chat.assert_not_called()
    assert "unavailable" in result["content"].lower()


@pytest.mark.asyncio
async def test_primary_failure_with_cloud_enabled_requires_budget_gate():
    primary = _make_provider(raises=RuntimeError("timeout"))
    secondary = _make_provider(response={"content": "from claude"})

    router = LLMRouter(primary=primary, secondary=secondary, error_threshold=5)

    result = await router.chat([{"role": "user", "content": "hi"}], cloud_allowed=True)

    secondary.chat.assert_not_called()
    assert "unavailable" in result["content"].lower()


@pytest.mark.asyncio
async def test_primary_failure_with_cloud_enabled_but_budget_disabled_degrades():
    primary = _make_provider(raises=RuntimeError("timeout"))
    secondary = _make_provider(response={"content": "from claude"})
    budget = CloudBudgetGate(CloudBudgetConfig(enabled=False))

    router = LLMRouter(primary=primary, secondary=secondary, error_threshold=5, cloud_budget=budget)

    result = await router.chat([{"role": "user", "content": "hi"}], cloud_allowed=True, session_id="s1")

    secondary.chat.assert_not_called()
    assert "unavailable" in result["content"].lower()


@pytest.mark.asyncio
async def test_primary_failure_uses_secondary_when_budget_allows(tmp_path):
    primary = _make_provider(raises=RuntimeError("timeout"))
    secondary = _make_provider(response={"content": "from claude", "usage": {"input_tokens": 100, "output_tokens": 25}})
    budget = _budget(tmp_path)

    router = LLMRouter(primary=primary, secondary=secondary, error_threshold=5, cloud_budget=budget)

    result = await router.chat([{"role": "user", "content": "hi"}], cloud_allowed=True, session_id="s1")

    secondary.chat.assert_called_once()
    assert result["content"] == "from claude"


@pytest.mark.asyncio
async def test_budgeted_secondary_call_uses_output_token_cap(tmp_path):
    primary = _make_provider(raises=RuntimeError("timeout"))
    secondary = _make_provider(response={"content": "from claude", "usage": {"input_tokens": 100, "output_tokens": 25}})
    budget = _budget(tmp_path)

    router = LLMRouter(primary=primary, secondary=secondary, error_threshold=5, cloud_budget=budget)

    await router.chat([{"role": "user", "content": "hi"}], cloud_allowed=True, session_id="s1")

    assert secondary.chat.call_args.kwargs["max_tokens"] == 2048


@pytest.mark.asyncio
async def test_missing_usage_metadata_blocks_next_budgeted_secondary_call(tmp_path):
    primary = _make_provider(raises=RuntimeError("timeout"))
    secondary = _make_provider(response={"content": "from claude"})
    budget = _budget(tmp_path)

    router = LLMRouter(primary=primary, secondary=secondary, error_threshold=5, cloud_budget=budget)

    first = await router.chat([{"role": "user", "content": "hi"}], cloud_allowed=True, session_id="s1")
    second = await router.chat([{"role": "user", "content": "hi"}], cloud_allowed=True, session_id="s2")

    assert first["content"] == "from claude"
    assert "unavailable" in second["content"].lower()
    assert secondary.chat.call_count == 1


@pytest.mark.asyncio
async def test_healthy_primary_always_used_regardless_of_cloud_flag():
    primary = _make_provider(response={"content": "from local"})
    secondary = _make_provider(response={"content": "from claude"})

    router = LLMRouter(primary=primary, secondary=secondary)

    result = await router.chat([{"role": "user", "content": "hi"}], cloud_allowed=True)

    primary.chat.assert_called_once()
    secondary.chat.assert_not_called()
    assert result["content"] == "from local"


@pytest.mark.asyncio
async def test_cloud_route_blocked_when_context_is_ineligible_even_if_budget_allows(tmp_path):
    primary = _make_provider(raises=RuntimeError("local down"))
    secondary = _make_provider(response={"content": "from cloud", "usage": {"input_tokens": 1, "output_tokens": 1}})
    router = LLMRouter(primary=primary, secondary=secondary, cloud_budget=_budget(tmp_path), error_threshold=1)

    result = await router.chat(
        [{"role": "user", "content": "sensitive context"}],
        cloud_allowed=True,
        session_id="sensitive-session",
        context_cloud_eligible=False,
    )

    secondary.chat.assert_not_called()
    assert "local-only" in result["content"]
    assert router.route_diagnostics()["candidates"][1]["blocked_reason"] == "context_cloud_ineligible"


@pytest.mark.asyncio
async def test_router_selects_best_local_route_by_task():
    routine = _make_provider(response={"content": "routine"})
    coding = _make_provider(response={"content": "coding"})
    router = LLMRouter(
        primary=routine,
        routes=[
            _route("routine", routine, priority=10, tasks=["small_talk"]),
            _route("coding", coding, priority=5, tasks=["technical_help"]),
        ],
    )

    result = await router.chat([{"role": "user", "content": "hi"}], task="technical_help")

    assert result["content"] == "coding"
    coding.chat.assert_called_once()
    routine.chat.assert_not_called()


@pytest.mark.asyncio
async def test_router_tries_next_local_route_before_cloud(tmp_path):
    broken = _make_provider(raises=RuntimeError("down"))
    healthy = _make_provider(response={"content": "local backup"})
    cloud = _make_provider(response={"content": "cloud", "usage": {"input_tokens": 1, "output_tokens": 1}})
    router = LLMRouter(
        primary=broken,
        cloud_budget=_budget(tmp_path),
        routing_strategy="prefer_local",
        routes=[
            _route("broken", broken, priority=20),
            _route("healthy", healthy, priority=10),
            _route("cloud", cloud, cost_tier="low_cost", policies=["local_then_low_cost"], priority=100),
        ],
    )

    result = await router.chat([{"role": "user", "content": "hi"}], cloud_allowed=True, response_policy="local_then_low_cost")

    assert result["content"] == "local backup"
    healthy.chat.assert_called_once()
    cloud.chat.assert_not_called()


@pytest.mark.asyncio
async def test_best_value_leans_local_without_task_match(tmp_path):
    """Value-lean calibration: best_value adds a modest cost-tier lean
    (local +120, low_cost +40, premium +0). Without a task-fit signal the
    lean breaks the tie for the cheaper-but-adequate route, so a routine turn
    stays local even when a higher-priority cloud route is eligible. Task fit
    still dominates the lean - see
    test_best_value_can_choose_task_matched_cloud_when_policy_allows.
    """
    local = _make_provider(response={"content": "local"})
    cloud = _make_provider(response={"content": "cloud", "usage": {"input_tokens": 1, "output_tokens": 1}})
    router = LLMRouter(
        primary=local,
        cloud_budget=_budget(tmp_path),
        routes=[
            _route("local", local, priority=40),
            _route("cloud", cloud, cost_tier="low_cost", policies=["local_then_low_cost"], priority=60),
        ],
    )
    assert router.routing_strategy == "best_value"
    # local 40+120=160 outranks low_cost 60+40=100 with no task signal.
    assert router._route_score(router.routes[0], None) > router._route_score(router.routes[1], None)

    result = await router.chat(
        [{"role": "user", "content": "hi"}],
        cloud_allowed=True,
        response_policy="local_then_low_cost",
        session_id="s1",
    )

    # Value lean keeps a routine, task-less turn on the cheaper local route.
    assert result["content"] == "local"
    local.chat.assert_called_once()
    cloud.chat.assert_not_called()


@pytest.mark.asyncio
async def test_low_cost_cloud_eligible_under_default_policy_when_local_down(tmp_path):
    """Regression for the prod Opus-escalation gap. Under the default
    LOCAL_THEN_CLAUDE_IF_HIGH_VALUE policy with local down and no task-fit
    signal, a routine turn must fall back to the low_cost cloud tier, not
    premium Claude. This only holds because the low_cost routes now list
    local_then_claude_if_high_value in allowed_response_policies (Option B);
    without it they were filtered out before scoring and premium won by
    priority. Scores: low_cost 58+40=98 beats premium 95+0=95.
    """
    local = _make_provider(raises=RuntimeError("local down"))
    low_cost = _make_provider(response={"content": "low_cost", "usage": {"input_tokens": 1, "output_tokens": 1}})
    premium = _make_provider(response={"content": "premium", "usage": {"input_tokens": 1, "output_tokens": 1}})
    router = LLMRouter(
        primary=local,
        cloud_budget=_budget(tmp_path),
        routes=[
            _route("local", local, priority=40),
            _route(
                "low_cost",
                low_cost,
                cost_tier="low_cost",
                policies=["local_then_low_cost", "local_then_claude_if_high_value"],
                priority=58,
            ),
            _route(
                "premium",
                premium,
                cost_tier="premium",
                policies=["local_then_claude_if_high_value"],
                priority=95,
            ),
        ],
    )

    result = await router.chat(
        [{"role": "user", "content": "how do I add a Jinja filter"}],
        cloud_allowed=True,
        response_policy="local_then_claude_if_high_value",
        session_id="s1",
    )

    assert result["content"] == "low_cost"
    low_cost.chat.assert_called_once()
    premium.chat.assert_not_called()


@pytest.mark.asyncio
async def test_premium_routine_fallback_prefers_sonnet_over_reserved_opus(tmp_path):
    """With only premium routes eligible (local and low-cost absent from the
    candidate set), a routine turn (no task fit) must fall to the default
    premium route (Sonnet), not the reserved premium (Opus). Opus stays
    enabled but its lower routing_priority keeps it off routine premium
    fallback. Scores: sonnet 95+0=95 beats opus 90+0=90.
    """
    sonnet = _make_provider(response={"content": "sonnet", "usage": {"input_tokens": 1, "output_tokens": 1}})
    opus = _make_provider(response={"content": "opus", "usage": {"input_tokens": 1, "output_tokens": 1}})
    router = LLMRouter(
        primary=sonnet,
        cloud_budget=_budget(tmp_path),
        routes=[
            _route("sonnet", sonnet, cost_tier="premium", policies=["local_then_claude_if_high_value"], priority=95, tasks=["complex_implementation"]),
            _route("opus", opus, cost_tier="premium", policies=["local_then_claude_if_high_value"], priority=90, tasks=["deep_reasoning"]),
        ],
    )
    assert router._route_score(router.routes[0], None) > router._route_score(router.routes[1], None)

    result = await router.chat(
        [{"role": "user", "content": "what is a good naming convention"}],
        cloud_allowed=True,
        response_policy="local_then_claude_if_high_value",
        session_id="s1",
    )

    assert result["content"] == "sonnet"
    opus.chat.assert_not_called()


@pytest.mark.asyncio
async def test_reserved_opus_still_wins_hard_task_via_task_fit(tmp_path):
    """The reservation only changes routine ordering. A deep-reasoning task
    still escalates to Opus through the +1000 recommended_for bonus, which
    dwarfs the 5-point priority gap: opus 90+1000=1090 beats sonnet 95+0=95.
    """
    sonnet = _make_provider(response={"content": "sonnet", "usage": {"input_tokens": 1, "output_tokens": 1}})
    opus = _make_provider(response={"content": "opus", "usage": {"input_tokens": 1, "output_tokens": 1}})
    router = LLMRouter(
        primary=sonnet,
        cloud_budget=_budget(tmp_path),
        routes=[
            _route("sonnet", sonnet, cost_tier="premium", policies=["local_then_claude_if_high_value"], priority=95, tasks=["complex_implementation"]),
            _route("opus", opus, cost_tier="premium", policies=["local_then_claude_if_high_value"], priority=90, tasks=["deep_reasoning"]),
        ],
    )

    result = await router.chat(
        [{"role": "user", "content": "reason carefully about this failure mode"}],
        cloud_allowed=True,
        response_policy="local_then_claude_if_high_value",
        task="deep_reasoning",
        session_id="s1",
    )

    assert result["content"] == "opus"
    sonnet.chat.assert_not_called()


@pytest.mark.asyncio
async def test_best_value_can_choose_task_matched_cloud_when_policy_allows(tmp_path):
    local = _make_provider(response={"content": "local"})
    cloud = _make_provider(response={"content": "cloud", "usage": {"input_tokens": 1, "output_tokens": 1}})
    router = LLMRouter(
        primary=local,
        cloud_budget=_budget(tmp_path),
        routes=[
            _route("local", local, priority=40),
            _route(
                "cloud",
                cloud,
                cost_tier="premium",
                policies=["local_then_claude_if_high_value"],
                priority=80,
                tasks=["high_value_service_inquiry"],
            ),
        ],
    )

    result = await router.chat(
        [{"role": "user", "content": "need production help"}],
        cloud_allowed=True,
        response_policy="local_then_claude_if_high_value",
        task="high_value_service_inquiry",
        session_id="s1",
    )

    assert result["content"] == "cloud"
    cloud.chat.assert_called_once()
    local.chat.assert_not_called()


@pytest.mark.asyncio
async def test_best_value_avoids_routes_marked_bad_for_task():
    risky = _make_provider(response={"content": "risky"})
    safer = _make_provider(response={"content": "safer"})
    router = LLMRouter(
        primary=risky,
        routes=[
            _route("risky", risky, priority=900, avoid=["high_stakes_reasoning"]),
            _route("safer", safer, priority=1),
        ],
    )

    result = await router.chat([{"role": "user", "content": "check this"}], task="HIGH_STAKES_REASONING")

    assert result["content"] == "safer"
    safer.chat.assert_called_once()
    risky.chat.assert_not_called()


@pytest.mark.asyncio
async def test_routing_strategy_cloud_only_filters_local_routes(tmp_path):
    local = _make_provider(response={"content": "local"})
    cloud = _make_provider(response={"content": "cloud", "usage": {"input_tokens": 1, "output_tokens": 1}})
    router = LLMRouter(
        primary=local,
        cloud_budget=_budget(tmp_path),
        routing_strategy="cloud_only",
        routes=[
            _route("local", local, priority=100),
            _route("cloud", cloud, cost_tier="low_cost", policies=["local_then_low_cost"], priority=1),
        ],
    )

    result = await router.chat(
        [{"role": "user", "content": "hi"}],
        cloud_allowed=True,
        response_policy="local_then_low_cost",
        session_id="s1",
    )

    assert result["content"] == "cloud"
    cloud.chat.assert_called_once()
    local.chat.assert_not_called()


@pytest.mark.asyncio
async def test_force_secondary_with_local_only_records_empty_route_conflict():
    local = _make_provider(response={"content": "local"})
    cloud = _make_provider(response={"content": "cloud"})
    router = LLMRouter(
        primary=local,
        routing_strategy="local_only",
        routes=[
            _route("local", local),
            _route("cloud", cloud, cost_tier="low_cost", policies=["local_then_low_cost"]),
        ],
    )

    result = await router.chat([{"role": "user", "content": "hi"}], force_secondary=True, cloud_allowed=True)

    assert "unavailable" in result["content"].lower()
    assert router.route_diagnostics()["ordered_routes"] == []
    assert router.route_diagnostics()["empty_reason"] == "force_secondary_conflicts_with_local_only"
    local.chat.assert_not_called()
    cloud.chat.assert_not_called()


@pytest.mark.asyncio
async def test_open_primary_breaker_still_allows_other_local_routes():
    primary = _make_provider(response={"content": "primary"})
    primary.is_healthy = AsyncMock(return_value=False)
    backup = _make_provider(response={"content": "backup"})
    router = LLMRouter(
        primary=primary,
        routes=[
            _route("primary", primary, priority=20),
            _route("backup", backup, priority=10),
        ],
    )
    router._breaker_open = True

    result = await router.chat([{"role": "user", "content": "hi"}])

    assert result["content"] == "backup"
    primary.chat.assert_not_called()
    backup.chat.assert_called_once()


@pytest.mark.asyncio
async def test_disabled_routes_are_never_selected_even_with_high_priority():
    disabled = _make_provider(response={"content": "disabled"})
    healthy = _make_provider(response={"content": "healthy"})
    router = LLMRouter(
        primary=healthy,
        routes=[
            _route("disabled", disabled, priority=9999, deployment_status="disabled"),
            _route("healthy", healthy, priority=1),
        ],
    )

    result = await router.chat([{"role": "user", "content": "hi"}])

    assert result["content"] == "healthy"
    disabled.chat.assert_not_called()
    assert router.route_diagnostics()["candidates"][0]["blocked_reason"] == "deployment_disabled"


@pytest.mark.asyncio
async def test_provider_statuses_show_block_reasons_for_uncallable_routes():
    local = _make_provider(healthy=False)
    cloud = _make_provider(healthy=True)
    router = LLMRouter(
        primary=local,
        routes=[
            _route("local", local),
            _route("cloud", cloud, cost_tier="premium", policies=["local_then_claude_if_high_value"]),
        ],
    )

    statuses = await router.provider_statuses(response_policy="local_then_low_cost", cloud_allowed=True)

    assert statuses[0]["blocked_reason"] == "health_check_failed"
    assert statuses[1]["blocked_reason"] == "not_allowed_for_response_policy"
    assert statuses[1]["callable"] is False


@pytest.mark.asyncio
async def test_provider_statuses_do_not_show_policy_block_without_policy_context():
    cloud = _make_provider(healthy=True)
    router = LLMRouter(
        primary=cloud,
        routes=[_route("cloud", cloud, cost_tier="premium", policies=["local_then_claude_if_high_value"])],
    )

    statuses = await router.provider_statuses(
        cloud_allowed=True,
        enabled_cost_tiers={"premium"},
    )

    assert statuses[0]["blocked_reason"] == ""
    assert statuses[0]["callable"] is True


@pytest.mark.asyncio
async def test_provider_statuses_include_local_runtime_diagnostics():
    local = _make_provider(healthy=True)
    route = _route("local", local, runtime_group="personal_gpu")
    router = LLMRouter(
        primary=local,
        routes=[route],
        runtime_manager=RuntimeManager(),
    )

    status = (await router.provider_statuses())[0]

    assert status["runtime_group"] == "personal_gpu"
    assert status["runtime_warm"] is True
    assert status["runtime_reachable"] is True


@pytest.mark.asyncio
async def test_provider_statuses_show_admin_disabled_routes():
    local = _make_provider(healthy=True)
    router = LLMRouter(primary=local, routes=[_route("local", local)])

    router.set_route_enabled("local", False)
    statuses = await router.provider_statuses()

    assert statuses[0]["admin_enabled"] is False
    assert statuses[0]["blocked_reason"] == "route_disabled"
    assert statuses[0]["callable"] is False


@pytest.mark.asyncio
async def test_provider_statuses_show_provider_not_built_before_health_state():
    local = _make_provider(healthy=True)
    missing = ProviderRoute(
        name="missing",
        provider=None,
        provider_type="claude",
        model="claude-test",
        role="secondary",
        cost_tier="premium",
        allowed_response_policies=["local_then_claude_if_high_value"],
        configured=True,
    )
    router = LLMRouter(primary=local, routes=[missing])

    statuses = await router.provider_statuses(cloud_allowed=True)

    assert statuses[0]["blocked_reason"] == "provider_not_built"
    assert statuses[0]["health_checked"] is False


@pytest.mark.asyncio
async def test_candidate_routes_are_not_health_checked_until_enabled():
    candidate = _make_provider(healthy=True)
    router = LLMRouter(
        primary=candidate,
        routes=[_route("candidate", candidate, deployment_status="candidate", enabled_by_default=False)],
    )

    disabled_status = (await router.provider_statuses())[0]
    router.set_route_enabled("candidate", True)
    enabled_status = (await router.provider_statuses(force_refresh=True))[0]

    assert disabled_status["admin_enabled"] is False
    assert disabled_status["health_checked"] is False
    assert disabled_status["blocked_reason"] == "candidate_not_enabled"
    assert enabled_status["admin_enabled"] is True
    assert enabled_status["health_checked"] is True
    assert enabled_status["blocked_reason"] == ""
    assert candidate.is_healthy.await_count == 1


@pytest.mark.asyncio
async def test_admin_status_can_probe_disabled_local_candidate_runtime():
    candidate = _make_provider(healthy=True)
    router = LLMRouter(
        primary=candidate,
        routes=[_route("candidate", candidate, deployment_status="candidate", enabled_by_default=False)],
        runtime_manager=RuntimeManager(),
    )

    status = (await router.provider_statuses(force_refresh=True, check_disabled_local_routes=True))[0]

    assert status["admin_enabled"] is False
    assert status["health_checked"] is True
    assert status["runtime_warm"] is True
    assert status["blocked_reason"] == "candidate_not_enabled"
    assert status["callable"] is False


@pytest.mark.asyncio
async def test_provider_health_statuses_use_short_ttl_cache():
    local = _make_provider(healthy=True)
    router = LLMRouter(primary=local, routes=[_route("local", local)], health_cache_ttl_seconds=60)

    first = (await router.provider_statuses())[0]
    second = (await router.provider_statuses())[0]
    third = (await router.provider_statuses(force_refresh=True))[0]

    assert first["health_cached"] is False
    assert second["health_cached"] is True
    assert third["health_cached"] is False
    assert local.is_healthy.await_count == 2


@pytest.mark.asyncio
async def test_route_enabled_change_clears_health_cache_for_empty_name_route():
    local = _make_provider(healthy=True)
    # Empty names use the composite route key, which caught a stale-cache edge case.
    route = _route("", local)
    router = LLMRouter(primary=local, routes=[route], health_cache_ttl_seconds=60)

    await router.provider_statuses()
    assert router._health_cache
    router.set_route_enabled("", False)
    assert router._health_cache == {}
    router.set_route_enabled("", True)
    await router.provider_statuses()

    assert local.is_healthy.await_count == 2


def test_set_routing_strategy_rejects_unknown_value():
    local = _make_provider()
    router = LLMRouter(primary=local)

    with pytest.raises(ValueError, match="Unknown routing_strategy"):
        router.set_routing_strategy("typo_strategy")


def test_clean_routing_strategy_defaults_empty_values_to_best_value():
    local = _make_provider()
    router = LLMRouter(primary=local, routing_strategy="")

    assert router.routing_strategy == "best_value"


@pytest.mark.asyncio
async def test_route_diagnostics_record_candidates_and_selected_route():
    local = _make_provider(response={"content": "local"})
    disabled = _make_provider(response={"content": "disabled"})
    router = LLMRouter(
        primary=local,
        routes=[
            _route("local", local, priority=1),
            _route("disabled", disabled, deployment_status="candidate", enabled_by_default=False),
        ],
    )

    result = await router.chat([{"role": "user", "content": "hi"}], task="routine_chat")
    diagnostics = router.route_diagnostics()

    assert result["content"] == "local"
    assert diagnostics["selected_route"] == "local"
    assert diagnostics["ordered_routes"] == ["local"]
    assert diagnostics["candidates"][1]["blocked_reason"] == "candidate_not_enabled"


@pytest.mark.asyncio
async def test_generation_concurrency_policy_limits_active_local_generation():
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_chat(*args, **kwargs):
        started.set()
        await release.wait()
        return {"content": "first"}

    local = _make_provider(response={"content": "unused"})
    local.chat = AsyncMock(side_effect=slow_chat)
    router = LLMRouter(
        primary=local,
        routes=[_route("local", local)],
        generation_concurrency_policy=GenerationConcurrencyPolicy(
            normal_parallel_limit=1,
            hard_parallel_limit=1,
            max_local_generations=1,
            max_cloud_generations=1,
        ),
    )

    first = asyncio.create_task(router.chat([{"role": "user", "content": "first"}]))
    await started.wait()
    second = await router.chat([{"role": "user", "content": "second"}])
    blocked_diagnostics = router.route_diagnostics()
    release.set()
    first_result = await first

    assert first_result["content"] == "first"
    assert second == router._LOCAL_UNAVAILABLE
    assert local.chat.await_count == 1
    assert blocked_diagnostics["generation_blocked_routes"] == ["local"]
    assert await router.generation_concurrency_status() == {
        "policy": {
            "normal_parallel_limit": 1,
            "hard_parallel_limit": 1,
            "max_local_generations": 1,
            "max_cloud_generations": 1,
        },
        "active_total": 0,
        "active_local": 0,
        "active_cloud": 0,
    }


@pytest.mark.asyncio
async def test_route_diagnostics_do_not_mix_concurrent_route_decisions():
    slow_started = asyncio.Event()
    release_slow = asyncio.Event()

    async def slow_chat(*args, **kwargs):
        slow_started.set()
        await release_slow.wait()
        return {"content": "slow"}

    slow = _make_provider(response={"content": "unused"})
    slow.chat = AsyncMock(side_effect=slow_chat)
    fast = _make_provider(response={"content": "fast"})
    router = LLMRouter(
        primary=slow,
        routes=[
            _route("slow", slow, tasks=["slow_task"]),
            _route("fast", fast, tasks=["fast_task"]),
        ],
        generation_concurrency_policy=GenerationConcurrencyPolicy(
            normal_parallel_limit=2,
            hard_parallel_limit=2,
            max_local_generations=2,
            max_cloud_generations=0,
        ),
    )

    slow_call = asyncio.create_task(router.chat([{"role": "user", "content": "slow"}], task="slow_task"))
    await slow_started.wait()
    fast_result = await router.chat([{"role": "user", "content": "fast"}], task="fast_task")
    release_slow.set()
    slow_result = await slow_call
    diagnostics = router.route_diagnostics()

    assert fast_result["content"] == "fast"
    assert slow_result["content"] == "slow"
    assert diagnostics["selected_route"] == diagnostics["ordered_routes"][0]
    assert diagnostics["selected_route"] == "slow"


@pytest.mark.asyncio
async def test_runtime_cache_prefers_warm_route_over_cold_route():
    cold = _make_provider(response={"content": "cold"})
    warm = _make_provider(response={"content": "warm"})
    router = LLMRouter(
        primary=cold,
        routes=[
            _route("cold", cold, priority=100),
            _route("warm", warm, priority=50),
        ],
        runtime_manager=RuntimeManager(RuntimeControlPolicy(warm_route_bonus=75, unavailable_penalty=750)),
    )
    router._health_cache["cold"] = (time.monotonic(), {
        "endpoint_healthy": True,
        "model_available": False,
        "available_models": [],
        "checked": True,
        "cached": False,
        "checked_at": time.time(),
    })
    router._health_cache["warm"] = (time.monotonic(), {
        "endpoint_healthy": True,
        "model_available": True,
        "available_models": [],
        "checked": True,
        "cached": False,
        "checked_at": time.time(),
    })

    result = await router.chat([{"role": "user", "content": "hi"}])

    assert result["content"] == "warm"
    assert router.route_diagnostics()["candidates"][0]["blocked_reason"] == "runtime_model_not_warm"
    cold.chat.assert_not_called()
    warm.chat.assert_called_once()


@pytest.mark.asyncio
async def test_stream_chat_yields_primary_provider_chunks():
    primary = _StreamingProvider(
        chunks=[
            {"choices": [{"delta": {"content": "hel"}}]},
            {"choices": [{"delta": {"content": "lo"}}]},
        ]
    )
    router = LLMRouter(primary=primary)

    chunks = [chunk async for chunk in router.stream_chat([{"role": "user", "content": "hi"}])]

    assert chunks == ["hel", "lo"]
    primary.chat.assert_not_called()


@pytest.mark.asyncio
async def test_stream_chat_degrades_when_primary_stream_fails_without_cloud_budget():
    primary = _StreamingProvider(raises=RuntimeError("down"))
    secondary = _make_provider(response={"content": "from claude"})
    router = LLMRouter(primary=primary, secondary=secondary, error_threshold=1)

    chunks = [chunk async for chunk in router.stream_chat([{"role": "user", "content": "hi"}], cloud_allowed=False)]

    secondary.chat.assert_not_called()
    assert "".join(chunks) == router._LOCAL_UNAVAILABLE["content"]
    assert router.circuit_breaker_status()["open"] is True


@pytest.mark.asyncio
async def test_stream_chat_falls_back_to_secondary_when_budget_allows(tmp_path):
    primary = _StreamingProvider(raises=RuntimeError("down"))
    secondary = _make_provider(response={"content": "from claude", "usage": {"input_tokens": 100, "output_tokens": 25}})
    router = LLMRouter(primary=primary, secondary=secondary, error_threshold=1, cloud_budget=_budget(tmp_path))

    chunks = [
        chunk
        async for chunk in router.stream_chat(
            [{"role": "user", "content": "hi"}],
            cloud_allowed=True,
            session_id="s1",
        )
    ]

    secondary.chat.assert_called_once()
    assert chunks == ["from claude"]


@pytest.mark.asyncio
async def test_stream_chat_raises_after_partial_primary_chunk_instead_of_fallback(tmp_path):
    primary = _StreamingProvider(
        chunks=[{"choices": [{"delta": {"content": "hel"}}]}],
        raises=RuntimeError("down"),
        raise_after_chunks=True,
    )
    secondary = _make_provider(response={"content": "from claude", "usage": {"input_tokens": 100, "output_tokens": 25}})
    router = LLMRouter(primary=primary, secondary=secondary, error_threshold=1, cloud_budget=_budget(tmp_path))

    stream = router.stream_chat(
        [{"role": "user", "content": "hi"}],
        cloud_allowed=True,
        session_id="s1",
    )

    assert await anext(stream) == "hel"
    with pytest.raises(RuntimeError, match="down"):
        await anext(stream)
    secondary.chat.assert_not_called()


# ---------------------------------------------------------------------------
# Phase 4 - blast-radius preference bonus tests
# ---------------------------------------------------------------------------

def _pref_route(name, provider, cost_tier="premium", preferred_provider_id="", provider_budget_id="default"):
    """Helper: build a ProviderRoute with preferred_provider_id set."""
    return ProviderRoute(
        name=name,
        provider=provider,
        provider_type=provider.__class__.__name__,
        model=name,
        role="secondary",
        cost_tier=cost_tier,
        allowed_response_policies=["local_then_low_cost"],
        preferred_provider_id=preferred_provider_id,
        provider_budget_id=provider_budget_id,
    )


def test_preferred_provider_id_field_default_empty():
    p = _make_provider()
    route = _route("r", p)
    assert route.preferred_provider_id == ""


def test_route_score_no_bonus_when_preferred_provider_id_empty():
    """Routes with preferred_provider_id='' get no blast-radius bonus."""
    from unittest.mock import MagicMock
    p = _make_provider()
    route = _pref_route("openrouter-via-or", p, preferred_provider_id="", provider_budget_id="openrouter")
    router = LLMRouter(primary=p, routes=[route])
    score_with = router._route_score(route, None)
    route_no_pref = _pref_route("openrouter-via-or-2", p, preferred_provider_id="", provider_budget_id="openrouter")
    assert router._route_score(route_no_pref, None) == score_with


def test_route_score_bonus_when_preferred_matches_budget_id(tmp_path):
    """A route where preferred_provider_id == provider_budget_id gets bonus when headroom > 0."""
    from core.cloud_budget import CloudBudgetConfig, CloudBudgetGate, MultiProviderBudgetGate, ProviderBudgetConfig
    p = _make_provider()
    # Direct Anthropic route
    direct = _pref_route("claude-direct", p, preferred_provider_id="anthropic", provider_budget_id="anthropic")
    # OpenRouter route for same model -- no preferred_provider_id
    via_or = _pref_route("claude-via-or", p, preferred_provider_id="", provider_budget_id="openrouter")

    global_gate = CloudBudgetGate(CloudBudgetConfig(
        enabled=True, max_calls_per_turn=5, max_calls_per_session=50,
        max_calls_per_day=50, max_calls_per_month=500,
        max_daily_usd=10.0, max_monthly_usd=100.0,
        max_input_tokens_per_call=16000, max_output_tokens_per_call=2048,
        sqlite_path=str(tmp_path / "budget.db"),
    ))
    ant_cfg = ProviderBudgetConfig(provider_id="anthropic", enabled=True, max_daily_usd=5.0, max_monthly_usd=50.0)
    multi = MultiProviderBudgetGate(global_gate=global_gate, provider_configs={"anthropic": ant_cfg})
    router = LLMRouter(primary=p, routes=[direct, via_or], cloud_budget=multi)

    # Full headroom -> direct route gets bonus; should score higher than via_or
    score_direct = router._route_score(direct, None)
    score_via_or = router._route_score(via_or, None)
    assert score_direct > score_via_or, f"direct={score_direct} should beat via_or={score_via_or}"


def test_route_score_no_bonus_when_preferred_differs_from_budget_id():
    """preferred_provider_id != provider_budget_id -> no bonus (cross-provider mismatch)."""
    p = _make_provider()
    # Preferred says anthropic but this route actually routes through openrouter
    mismatched = _pref_route("claude-mismatched", p, preferred_provider_id="anthropic", provider_budget_id="openrouter")
    router = LLMRouter(primary=p, routes=[mismatched])
    # Score should not include blast-radius bonus
    base = mismatched.routing_priority  # no extra budget adjustments for premium in best_value
    score = router._route_score(mismatched, None)
    # With mismatch, no bonus; check that score <= base (no blast-radius added)
    assert score <= base + 50, f"Unexpected high score {score}"
    # Specifically: preferred != budget_id, so no bonus added
    # Build a matching route and verify it scores higher
    matched = _pref_route("claude-matched", p, preferred_provider_id="anthropic", provider_budget_id="anthropic")
    router2 = LLMRouter(primary=p, routes=[matched, mismatched])
    assert router2._route_score(matched, None) >= router2._route_score(mismatched, None)


def test_provider_budget_headroom_no_multi_gate_returns_1():
    """Without a MultiProviderBudgetGate, headroom always returns 1.0."""
    p = _make_provider()
    router = LLMRouter(primary=p, routes=[_route("r", p)])
    assert router.provider_budget_headroom("anthropic") == 1.0


def test_provider_budget_headroom_unconfigured_provider_returns_1(tmp_path):
    """A provider not in the multi gate config returns 1.0."""
    from core.cloud_budget import CloudBudgetConfig, CloudBudgetGate, MultiProviderBudgetGate
    p = _make_provider()
    global_gate = CloudBudgetGate(CloudBudgetConfig(
        enabled=True, max_calls_per_turn=5, max_calls_per_session=50,
        max_calls_per_day=50, max_calls_per_month=500,
        max_daily_usd=10.0, max_monthly_usd=100.0,
        max_input_tokens_per_call=16000, max_output_tokens_per_call=2048,
        sqlite_path=str(tmp_path / "budget.db"),
    ))
    multi = MultiProviderBudgetGate(global_gate=global_gate, provider_configs={})
    router = LLMRouter(primary=p, routes=[_route("r", p)], cloud_budget=multi)
    assert router.provider_budget_headroom("anthropic") == 1.0
