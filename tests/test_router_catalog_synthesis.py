"""The candidate set is the CATALOG, not llm/profiles.json.

`resolve_lane` ranks the whole live catalog, but the router used to keep only
the routes it already had configured, so it reordered a hand-typed list and
never selected from the catalog: a better or cheaper model could ship and never
be chosen. That is the stale-constant failure the doctrine exists to abolish,
relocated from a model id to a curated list.

These tests pin the fix and, just as importantly, pin the things the fix is not
allowed to break: a synthesized route is a third party on the public internet
and must clear every gate a configured route clears.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from gestaltworkframe.core.model_catalog import CatalogModel
from gestaltworkframe.core.model_resolver import Candidate, Resolution
from gestaltworkframe.core.router import (
    SYNTHESIZED_PREMIUM_COMPLETION_PRICE_USD_PER_MILLION,
    SYNTHESIZED_RESPONSE_POLICIES,
    SYNTHESIZED_ROUTE_PREFIX,
    LLMRouter,
    ProviderRoute,
)

TOOLS = ["tools", "tool_choice", "structured_outputs", "reasoning"]


def _entry(
    model_id: str,
    *,
    prompt: float,
    completion: float,
    intelligence: float = 50.0,
    agentic: float = 50.0,
    params: list[str] | None = None,
) -> dict:
    """One catalog entry. Prices are given per million for readability."""
    return {
        "id": model_id,
        "context_length": 1_000_000,
        "pricing": {
            "prompt": str(prompt / 1_000_000),
            "completion": str(completion / 1_000_000),
        },
        "supported_parameters": TOOLS if params is None else params,
        "benchmarks": {
            "artificial_analysis": {
                "intelligence_index": intelligence,
                "coding_index": intelligence + 15,
                "agentic_index": agentic,
            }
        },
    }


def _configured(
    name: str,
    model: str,
    *,
    priority: int = 99,
    task: str | None = "classification",
    cost_tier: str = "low_cost",
) -> ProviderRoute:
    """A route as provider_registry builds one from llm/profiles.json."""
    return ProviderRoute(
        name=name,
        provider=object(),  # never dispatched here; only selection is under test
        provider_type="openrouter",
        model=model,
        role="secondary",
        cost_tier=cost_tier,
        allowed_response_policies=["local_then_low_cost", "local_then_claude_if_high_value"],
        recommended_for=[task] if task else [],
        routing_priority=priority,
        capabilities=["chat", "tools", "rag_answering"],
        tool_calling_quality="strong",
        provider_budget_id="openrouter",
    )


@pytest.fixture()
def catalog(tmp_path: Path, monkeypatch):
    """Point the sync catalog reader at a cache this test controls."""

    def write(models: list[dict]) -> Path:
        path = tmp_path / "catalog.json"
        path.write_text(
            json.dumps({"fetched_at": time.time(), "catalog": {"data": models}}),
            encoding="utf-8",
        )
        monkeypatch.setenv("MODEL_CATALOG_CACHE_PATH", str(path))
        return path

    monkeypatch.setenv("MODEL_GATEWAY_PREFIX", "openrouter/")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    return write


def _order(router: LLMRouter, **kwargs) -> tuple[list[str], dict]:
    ordered, diagnostics = router._ordered_routes(
        kwargs.pop("force_secondary", False),
        kwargs.pop("cloud_allowed", True),
        kwargs.pop("response_policy", None),
        kwargs.pop("task", "classification"),
        kwargs.pop("context_cloud_eligible", True),
        **kwargs,
    )
    return [route.name for route in ordered], diagnostics


# ---- the defect this change exists to close ------------------------------

def test_a_catalog_model_that_beats_every_configured_profile_is_selected(catalog):
    """The whole point. profiles.json stops being the candidate set.

    `vendor/superior` is in nobody's profile file. It clears the lookup lane on
    the lane's own terms (higher intelligence, lower cost per turn at the lane's
    declared shape, under the lane's prompt-price ceiling) and must therefore
    win the turn.
    """
    catalog([
        _entry("vendor/configured", prompt=0.5, completion=2.0, intelligence=50),
        _entry("vendor/superior", prompt=0.2, completion=0.8, intelligence=60),
    ])
    router = LLMRouter(primary=None, routes=[_configured("configured", "vendor/configured")])

    names, diagnostics = _order(router)

    assert names[0] == f"{SYNTHESIZED_ROUTE_PREFIX}vendor/superior"
    assert diagnostics["capability_synthesized"] == ["catalog:vendor/superior"]
    assert "vendor/superior" in diagnostics["capability_choice"]


def test_without_the_synthesis_path_the_hand_typed_list_wins_again(catalog, monkeypatch):
    """The same setup with synthesis off, so the test above can genuinely fail.

    No OpenRouter key means no transport a catalog model is reachable through,
    so the router falls back to reordering what it was configured with -- which
    is exactly the defect, reproduced on demand.
    """
    catalog([
        _entry("vendor/configured", prompt=0.5, completion=2.0, intelligence=50),
        _entry("vendor/superior", prompt=0.2, completion=0.8, intelligence=60),
    ])
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    router = LLMRouter(primary=None, routes=[_configured("configured", "vendor/configured")])

    names, diagnostics = _order(router)

    assert names == ["configured"], "the better catalog model is unreachable and never chosen"
    assert diagnostics["capability_synthesized"] == []


def test_an_operator_pin_still_wins_over_a_synthesized_route(catalog):
    """profiles.json becomes overrides and pins, and a pin is honoured.

    Synthesizing over a configured profile for the same catalog id would
    silently discard the operator's params, task tags and price overrides.
    """
    catalog([_entry("vendor/superior", prompt=0.2, completion=0.8, intelligence=60)])
    router = LLMRouter(primary=None, routes=[_configured("pinned", "vendor/superior")])

    names, diagnostics = _order(router)

    assert names == ["pinned"]
    assert diagnostics["capability_synthesized"] == []


# ---- what a synthesized route is not allowed to do -----------------------

def test_a_synthesized_route_can_never_serve_a_local_only_turn(catalog):
    """The exact data-handling breach the previous pass closed.

    Free OpenRouter endpoints had been silently serving `local_only` turns. A
    route the router invents for itself serving one would reopen that wider,
    because nobody typed it into a file where a reviewer could see it.
    """
    catalog([_entry("vendor/superior", prompt=0.2, completion=0.8, intelligence=60)])
    router = LLMRouter(primary=None, routes=[_configured("configured", "vendor/configured")])

    names, diagnostics = _order(router, response_policy="local_only")

    assert not any(name.startswith(SYNTHESIZED_ROUTE_PREFIX) for name in names)
    synthesized_candidates = [
        candidate
        for candidate in diagnostics["candidates"]
        if candidate.get("catalog_derived")
    ]
    assert synthesized_candidates, "it must be considered and then refused, not merely absent"
    assert all(
        candidate["blocked_reason"] == "not_allowed_for_response_policy"
        for candidate in synthesized_candidates
    )


def test_local_only_is_absent_from_every_synthesized_policy_list():
    """Pinned as a property of the table, so a future edit has to argue with it."""
    for policies in SYNTHESIZED_RESPONSE_POLICIES.values():
        assert "local_only" not in policies


def test_the_local_only_strategy_also_excludes_synthesized_routes(catalog):
    catalog([_entry("vendor/superior", prompt=0.2, completion=0.8, intelligence=60)])
    router = LLMRouter(
        primary=None,
        routes=[_configured("configured", "vendor/configured")],
        routing_strategy="local_only",
    )

    names, _ = _order(router)

    assert not any(name.startswith(SYNTHESIZED_ROUTE_PREFIX) for name in names)


def test_a_context_marked_not_cloud_eligible_excludes_synthesized_routes(catalog):
    catalog([_entry("vendor/superior", prompt=0.2, completion=0.8, intelligence=60)])
    router = LLMRouter(primary=None, routes=[_configured("configured", "vendor/configured")])

    names, _ = _order(router, context_cloud_eligible=False)

    assert not any(name.startswith(SYNTHESIZED_ROUTE_PREFIX) for name in names)


def test_free_and_batch_ids_are_never_synthesized(catalog):
    """Unliftable, and cheaper than every alternative, which is the trap.

    `:free` trains on submitted prompts; `:batch` publishes its interactive
    sibling's indices, so it clears every floor, undercuts the real endpoint by
    half, and then fails a synchronous turn by arriving hours later.
    """
    catalog([
        _entry("vendor/superior:free", prompt=0.0, completion=0.0, intelligence=60),
        _entry("vendor/superior:batch", prompt=0.05, completion=0.2, intelligence=60),
        _entry("vendor/superior", prompt=0.2, completion=0.8, intelligence=60),
    ])
    router = LLMRouter(primary=None, routes=[_configured("configured", "vendor/configured")])

    names, _ = _order(router)

    assert f"{SYNTHESIZED_ROUTE_PREFIX}vendor/superior" in names
    assert not any(":free" in name or ":batch" in name for name in names)


def test_the_synthesis_guard_refuses_a_free_or_batch_candidate_on_its_own(catalog):
    """Belt and braces against the resolver, on the DISPATCH id.

    The id checked here is the string that would actually be sent, which is the
    only namespace worth guarding.
    """
    catalog([_entry("vendor/superior", prompt=0.2, completion=0.8)])
    router = LLMRouter(primary=None, routes=[])

    def candidate(model_id: str) -> Candidate:
        return Candidate(
            model=CatalogModel(
                id=model_id,
                context_length=1_000_000,
                prompt_price=0.2 / 1_000_000,
                completion_price=0.8 / 1_000_000,
                supported_parameters=frozenset(TOOLS),
                intelligence_index=60.0,
                agentic_index=50.0,
            ),
            lane="lookup",
            cost_per_turn_usd=0.001,
            margin=30.0,
        )

    resolution = Resolution(
        lane="lookup",
        candidates=(
            candidate("vendor/a:free"),
            candidate("vendor/b:batch"),
            candidate("openrouter/auto"),
            candidate("vendor/ok"),
        ),
        rejections=(),
    )

    synthesized = router._synthesize_cloud_routes(resolution, [], "classification")

    assert [route.model for route in synthesized] == ["vendor/ok"]


def test_no_openrouter_key_synthesizes_nothing(catalog, monkeypatch):
    catalog([_entry("vendor/superior", prompt=0.2, completion=0.8, intelligence=60)])
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    router = LLMRouter(primary=None, routes=[])

    names, diagnostics = _order(router)

    assert names == []
    assert diagnostics["capability_synthesized"] == []


def test_only_the_openrouter_transport_is_ever_synthesized(catalog):
    """Never a local route, never a direct-vendor route.

    A local model is one we host, so there is nothing to construct. A direct
    vendor alias is named by the gateway rather than derived, so it cannot be
    constructed. Only the aggregator is generic enough: the model string is the
    single per-model input.
    """
    catalog([_entry("vendor/superior", prompt=0.2, completion=0.8, intelligence=60)])
    router = LLMRouter(primary=None, routes=[_configured("configured", "vendor/configured")])

    _order(router)

    assert router._synthesized_routes, "the turn must actually have synthesized something"
    for route in router._synthesized_routes.values():
        assert route.is_cloud
        assert route.cost_tier != "local"
        assert route.provider_type == "openrouter"
        assert route.provider_budget_id == "openrouter"
        assert route.provider.base_url.startswith("https://openrouter.ai")
        # The dispatch id is the BARE catalog slug, which is how the OpenRouter
        # API keys models. MODEL_GATEWAY_PREFIX describes a broker in front of
        # the aggregator and does not apply to a route pointed straight at it.
        assert not route.model.startswith("openrouter/")


# ---- staying inside the existing controls ---------------------------------

def test_cost_tier_is_derived_from_the_catalog_price_not_invented(catalog):
    """Spend caps and reporting key off the bucket, so it has to be real."""
    catalog([
        _entry("vendor/metered", prompt=0.2, completion=0.8, intelligence=60),
        _entry("vendor/dear", prompt=5.0, completion=25.0, intelligence=61),
    ])
    router = LLMRouter(primary=None, routes=[_configured("configured", "vendor/configured")])

    # The guide lane, which declares no prompt-price ceiling, so the dear model
    # reaches the shortlist and can be classified at all.
    _order(router, task=None)

    tiers = {route.model: route.cost_tier for route in router._synthesized_routes.values()}
    assert tiers["vendor/metered"] == "low_cost"
    assert tiers["vendor/dear"] == "premium"

    prices = {
        route.model: (route.input_price_usd_per_million, route.output_price_usd_per_million)
        for route in router._synthesized_routes.values()
    }
    assert prices["vendor/dear"] == pytest.approx((5.0, 25.0))


def test_the_premium_boundary_is_greater_than_not_at_least(catalog):
    """claude-haiku-4.5 completes at exactly $5.00/M and is a low_cost profile.

    A boundary that classified it premium would put a route the operator calls
    cheap into the escalation bucket, and the two would then disagree about the
    same model.
    """
    catalog([
        _entry(
            "vendor/at-the-line",
            prompt=1.0,
            completion=SYNTHESIZED_PREMIUM_COMPLETION_PRICE_USD_PER_MILLION,
            intelligence=60,
        ),
    ])
    router = LLMRouter(primary=None, routes=[_configured("configured", "vendor/configured")])

    _order(router)

    assert router._synthesized_routes["vendor/at-the-line"].cost_tier == "low_cost"


def test_a_declared_lane_cost_ceiling_still_binds_a_synthesized_route(catalog):
    """The doctrine permits a declared ceiling in the resolution order.

    The lookup lane declares $1.00/M prompt. A catalog model above it never
    reaches the shortlist, so it can never be synthesized either -- synthesis
    reads the resolver's output rather than the raw catalog, which is what keeps
    the two from drifting apart.
    """
    catalog([
        _entry("vendor/over-the-ceiling", prompt=2.0, completion=0.1, intelligence=61),
        _entry("vendor/under-the-ceiling", prompt=0.2, completion=0.8, intelligence=60),
    ])
    router = LLMRouter(primary=None, routes=[_configured("configured", "vendor/configured")])

    names, _ = _order(router)

    assert f"{SYNTHESIZED_ROUTE_PREFIX}vendor/under-the-ceiling" in names
    assert not any("over-the-ceiling" in name for name in names)


def test_a_model_failing_the_lane_floor_is_never_synthesized(catalog):
    catalog([
        _entry("vendor/no-tools", prompt=0.01, completion=0.02, intelligence=60, params=["temperature"]),
        _entry("vendor/dim", prompt=0.01, completion=0.02, intelligence=5),
        _entry("vendor/fine", prompt=0.2, completion=0.8, intelligence=60),
    ])
    router = LLMRouter(primary=None, routes=[_configured("configured", "vendor/configured")])

    names, _ = _order(router)

    assert f"{SYNTHESIZED_ROUTE_PREFIX}vendor/fine" in names
    assert not any("no-tools" in name or "dim" in name for name in names)


def test_a_synthesized_route_is_visible_and_controllable_like_any_other(catalog):
    """A route the operator cannot see or disable is outside the controls."""
    catalog([_entry("vendor/superior", prompt=0.2, completion=0.8, intelligence=60)])
    router = LLMRouter(primary=None, routes=[_configured("configured", "vendor/configured")])

    names, diagnostics = _order(router)
    synthesized = f"{SYNTHESIZED_ROUTE_PREFIX}vendor/superior"
    assert synthesized in names
    assert synthesized in router.route_overrides()
    assert synthesized in router.circuit_breaker_status()["routes"]
    assert any(
        candidate["name"] == synthesized and candidate["catalog_derived"]
        for candidate in diagnostics["candidates"]
    )

    router.set_route_enabled(synthesized, False)
    names_after, _ = _order(router)
    assert synthesized not in names_after


def test_the_breaker_benches_a_synthesized_route_like_a_configured_one(catalog):
    """Availability is observed. One failure signal, not two."""
    catalog([
        _entry("vendor/superior", prompt=0.2, completion=0.8, intelligence=60),
        _entry("vendor/next-best", prompt=0.3, completion=1.0, intelligence=58),
    ])
    router = LLMRouter(primary=None, routes=[_configured("configured", "vendor/configured")])

    names, _ = _order(router)
    assert names[0] == f"{SYNTHESIZED_ROUTE_PREFIX}vendor/superior"

    router._route_breaker_open.add(f"{SYNTHESIZED_ROUTE_PREFIX}vendor/superior")
    names_after, _ = _order(router)

    assert names_after[0] == f"{SYNTHESIZED_ROUTE_PREFIX}vendor/next-best"


def test_a_synthesized_route_never_invents_a_cloud_task_match(catalog):
    """It inherits the cloud family's task fit; it does not create one.

    A synthesized route carries the task as its own recommendation only when a
    configured cloud route already claims it. Otherwise a turn tagged for a
    local profile would start escalating for no reason anybody asked for.
    """
    catalog([_entry("vendor/superior", prompt=0.2, completion=0.8, intelligence=60)])
    router = LLMRouter(
        primary=None,
        routes=[_configured("configured", "vendor/configured", task=None)],
    )

    _order(router, task="classification")

    synthesized = [
        candidate
        for candidate in router.route_diagnostics()["candidates"]
        if candidate.get("catalog_derived")
    ]
    assert synthesized and not any(candidate["recommended_match"] for candidate in synthesized)


@pytest.mark.asyncio
async def test_a_synthesized_route_is_closed_and_rekeyed_with_the_rest(catalog):
    """Its httpx client is built once and stays inside the lifecycle."""
    catalog([_entry("vendor/superior", prompt=0.2, completion=0.8, intelligence=60)])
    router = LLMRouter(primary=None, routes=[])

    _order(router)
    _order(router)
    route = router._synthesized_routes["vendor/superior"]
    first_client = route.provider.client

    assert len(router._synthesized_routes) == 1, "the provider is cached, not rebuilt per turn"

    assert await router.rotate_provider_key("openrouter", "rotated-key") == 1
    assert route.provider.client is not first_client
    assert route.provider.client.headers["Authorization"] == "Bearer rotated-key"

    await router.close()
    assert route.provider.client.is_closed


def test_a_disabled_configured_route_is_not_resurrected_by_synthesis(catalog):
    """The operator kill-switch survives synthesis.

    Downstream review finding (critical, EGI_bot#85): the pin set used to be
    computed from the gate-passed routes only, so admin-disabling a configured
    route removed its model from the dedupe set and synthesis re-created the
    identical model under a `catalog:` name no override covers."""
    catalog([_entry("vendor/superior", prompt=0.2, completion=0.8, intelligence=60)])
    router = LLMRouter(primary=None, routes=[_configured("pinned", "vendor/superior")])
    router.set_route_enabled("pinned", False)

    names, diagnostics = _order(router)

    assert names == [], "the disabled model must not serve under any name"
    assert diagnostics["capability_synthesized"] == []


def test_a_breakered_configured_route_benches_its_model_for_synthesis(catalog):
    """A failing upstream is not re-dispatched through a synthesized twin.

    Breaker-open configured routes are gate-blocked before capability
    ordering, so the benched sweep must cover the full fleet or the resolver
    ranks the same failing model straight back in."""
    catalog([
        _entry("vendor/flaky", prompt=0.1, completion=0.2, intelligence=60),
        _entry("vendor/steady", prompt=0.9, completion=3.0, intelligence=50),
    ])
    flaky = _configured("flaky", "vendor/flaky")
    router = LLMRouter(primary=None, routes=[flaky])
    router._route_breaker_open.add(router._route_key(flaky))

    names, _ = _order(router)

    assert not any("vendor/flaky" in name for name in names)
    assert any("vendor/steady" in name for name in names)
