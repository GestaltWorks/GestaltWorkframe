"""Capability ordering inside the router.

The resolver decides which cloud model a turn should prefer; these tests pin
how that decision reaches route selection, and — more importantly — that it
cannot take the service down when it is wrong.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gestaltworkframe.core.model_catalog import CatalogModel
from gestaltworkframe.core.router import (
    CAPABILITY_ROUTING_ENV,
    DEFAULT_LANE,
    TASK_LANES,
    LLMRouter,
    ProviderRoute,
)


def _route(name: str, model: str, *, is_cloud: bool = True, priority: int = 0) -> ProviderRoute:
    # is_cloud is derived from cost_tier, not passed.
    return ProviderRoute(
        name=name,
        provider=None,
        provider_type="openai_compatible",
        model=model,
        role="secondary",
        cost_tier="low_cost" if is_cloud else "local",
        allowed_response_policies=["local_then_low_cost"],
        routing_priority=priority,
    )


def _catalog_cache(tmp_path: Path, models: list[dict]) -> Path:
    """Write a catalog cache the sync reader will accept as fresh."""
    import time

    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps({"fetched_at": time.time(), "catalog": {"data": models}}),
        encoding="utf-8",
    )
    return path


def _entry(model_id: str, *, prompt: float, completion: float, intelligence: float = 50.0) -> dict:
    return {
        "id": model_id,
        "context_length": 1_000_000,
        "pricing": {"prompt": str(prompt / 1_000_000), "completion": str(completion / 1_000_000)},
        "supported_parameters": ["tools", "tool_choice", "structured_outputs", "reasoning"],
        "benchmarks": {
            "artificial_analysis": {
                "intelligence_index": intelligence,
                "coding_index": intelligence + 15,
                "agentic_index": intelligence - 5,
            }
        },
    }


@pytest.fixture()
def router() -> LLMRouter:
    return LLMRouter(primary=None)


def test_capability_routing_is_the_default_path(router, monkeypatch):
    """Correct behaviour that ships disabled is not behaviour, it is a comment.

    This flag shipped off and was set in no .env, no .env.example, no compose
    file and no deployment bundle, which made the resolver dead code and left
    the live ordering to the sum of hand-typed routing_priority integers in
    llm/profiles.json -- a shortlist ranked by a stored human preference order.
    """
    monkeypatch.delenv(CAPABILITY_ROUTING_ENV, raising=False)
    assert router._capability_routing_enabled() is True


def test_the_flag_survives_as_an_escape_hatch_to_the_legacy_ordering(router, monkeypatch):
    """One variable back to priority ordering, for a mis-tuned lane, no redeploy."""
    for raw in ("0", "false", "no", "off"):
        monkeypatch.setenv(CAPABILITY_ROUTING_ENV, raw)
        assert router._capability_routing_enabled() is False, raw
    for raw in ("1", "true", "yes", "on"):
        monkeypatch.setenv(CAPABILITY_ROUTING_ENV, raw)
        assert router._capability_routing_enabled() is True, raw
    # Nonsense is not a silent "off".
    monkeypatch.setenv(CAPABILITY_ROUTING_ENV, "maybe")
    assert router._capability_routing_enabled() is True


def test_gateway_prefix_is_stripped_to_reach_the_catalog_id(router, monkeypatch):
    """Transport mapping is configuration, not judgement."""
    monkeypatch.setenv("MODEL_GATEWAY_PREFIX", "openrouter/")
    assert router._catalog_id("openrouter/anthropic/claude-haiku-4.5") == "anthropic/claude-haiku-4.5"
    assert router._catalog_id("anthropic/claude-haiku-4.5") == "anthropic/claude-haiku-4.5"


def test_cloud_routes_are_ordered_by_the_lane_not_by_priority(router, monkeypatch, tmp_path):
    """The whole point: a hand-typed priority integer stops deciding."""
    cache = _catalog_cache(
        tmp_path,
        [
            _entry("vendor/cheap", prompt=0.1, completion=0.4, intelligence=45),
            _entry("vendor/pricey", prompt=0.9, completion=40.0, intelligence=46),
        ],
    )
    monkeypatch.setenv("MODEL_CATALOG_CACHE_PATH", str(cache))
    monkeypatch.setenv("MODEL_GATEWAY_PREFIX", "openrouter/")

    # Both clear the lane's $1/M prompt ceiling, so ranking decides: the
    # "pricey" one is cheap to prompt and expensive to answer, which is exactly
    # the trap that ranking on the sticker input price falls into.
    routes = [
        _route("pricey", "openrouter/vendor/pricey", priority=99),
        _route("cheap", "openrouter/vendor/cheap", priority=1),
    ]
    diagnostics: dict = {}

    ordered = router._capability_order(routes, "classification", diagnostics)

    assert [r.name for r in ordered] == ["cheap", "pricey"]
    assert diagnostics["capability_lane"] == "lookup"
    assert "vendor/cheap" in diagnostics["capability_choice"]


def test_a_model_that_fails_the_lane_is_demoted_not_dropped(router, monkeypatch, tmp_path):
    """A configured route that misses the lane shortlist stays reachable, last.

    Regression (EGI 2026-08-05 outage): known-but-unranked routes were dropped
    entirely, so a lane-shortlist miss took every aggregator route offline. A
    lane floor may demote a configured route to last resort; it must never
    remove it."""
    cache = _catalog_cache(
        tmp_path,
        [
            _entry("vendor/good", prompt=0.1, completion=0.4, intelligence=55),
            {  # no tool support: broken for an agentic turn, however cheap
                "id": "vendor/no-tools",
                "context_length": 1_000_000,
                "pricing": {"prompt": "0.0000001", "completion": "0.0000002"},
                "supported_parameters": ["temperature"],
                "benchmarks": {"artificial_analysis": {"intelligence_index": 55}},
            },
        ],
    )
    monkeypatch.setenv("MODEL_CATALOG_CACHE_PATH", str(cache))
    monkeypatch.setenv("MODEL_GATEWAY_PREFIX", "openrouter/")

    routes = [_route("nt", "openrouter/vendor/no-tools"), _route("good", "openrouter/vendor/good")]
    diagnostics: dict = {}

    ordered = router._capability_order(routes, "classification", diagnostics)

    assert [r.name for r in ordered] == ["good", "nt"]
    assert any("missing required parameter" in line for line in diagnostics["capability_rejected"])


def test_routes_the_catalog_does_not_list_are_kept_last_not_dropped(router, monkeypatch, tmp_path):
    """An unlisted model is unranked, not disqualified.

    Self-hosted and private routes never appear in a public catalog; dropping
    them would take a working route offline the first time it is enabled.
    """
    cache = _catalog_cache(tmp_path, [_entry("vendor/known", prompt=1.0, completion=4.0, intelligence=50)])
    monkeypatch.setenv("MODEL_CATALOG_CACHE_PATH", str(cache))
    monkeypatch.setenv("MODEL_GATEWAY_PREFIX", "openrouter/")

    routes = [_route("private", "internal/private-model"), _route("known", "openrouter/vendor/known")]
    ordered = router._capability_order(routes, "classification", {})

    assert [r.name for r in ordered] == ["known", "private"]


def test_benched_routes_are_demoted_using_the_routers_own_breaker(router, monkeypatch, tmp_path):
    """Availability is observed. Reuse the breaker, do not invent a second signal.

    A benched route loses its lane rank and sorts last: still reachable as the
    final fallback rather than vanishing, per the demote-not-drop rule."""
    cache = _catalog_cache(
        tmp_path,
        [
            _entry("vendor/flaky", prompt=0.1, completion=0.2, intelligence=50),
            _entry("vendor/steady", prompt=0.9, completion=3.0, intelligence=50),
        ],
    )
    monkeypatch.setenv("MODEL_CATALOG_CACHE_PATH", str(cache))
    monkeypatch.setenv("MODEL_GATEWAY_PREFIX", "openrouter/")

    flaky = _route("flaky", "openrouter/vendor/flaky")
    steady = _route("steady", "openrouter/vendor/steady")
    router._route_breaker_open.add(router._route_key(flaky))

    ordered = router._capability_order([flaky, steady], "classification", {})

    assert [r.name for r in ordered] == ["steady", "flaky"]


def test_a_lane_that_clears_nothing_falls_back_to_the_given_order(router, monkeypatch, tmp_path):
    """A mis-tuned floor must degrade ordering, never the service.

    This is the failure mode that shipped once already: a floor above the
    index ceiling matched nothing at all.
    """
    cache = _catalog_cache(tmp_path, [_entry("vendor/ordinary", prompt=1.0, completion=4.0, intelligence=5)])
    monkeypatch.setenv("MODEL_CATALOG_CACHE_PATH", str(cache))
    monkeypatch.setenv("MODEL_GATEWAY_PREFIX", "openrouter/")

    routes = [_route("a", "openrouter/vendor/ordinary"), _route("b", "openrouter/vendor/ordinary")]
    diagnostics: dict = {}

    ordered = router._capability_order(routes, "code_review", diagnostics)

    assert ordered == routes, "every route must survive a lane that clears nothing"
    assert "no model cleared" in diagnostics["capability_choice"]


def test_empty_cloud_list_is_returned_untouched(router):
    assert router._capability_order([], "classification", {}) == []


def test_task_lane_mapping_covers_the_expensive_tasks():
    """Review-grade work must not silently ride the cheap lane."""
    for task in ("code_review", "critical_code_review", "security_review"):
        assert TASK_LANES[task] == "review"
    assert TASK_LANES.get("unmapped-task", DEFAULT_LANE) == "guide"


def test_both_transports_of_one_model_resolve_to_the_same_catalog_entry(router, monkeypatch, tmp_path):
    """OpenRouter primary, Anthropic backup: two routes, one model.

    If the direct alias did not resolve to the same catalog id it would never
    inherit the lane ranking, so the backup could never be chosen and the
    aggregator would be a single point of failure.
    """
    bundle = tmp_path / "brand"
    bundle.mkdir()
    (bundle / "transports.yaml").write_text(
        "direct:\n  anthropic/claude-haiku-4.5: claude-haiku-4-5\n", encoding="utf-8"
    )
    monkeypatch.setenv("MODEL_GATEWAY_PREFIX", "openrouter/")
    monkeypatch.setattr(type(router), "_deployment_bundle_dir", staticmethod(lambda: bundle))

    assert router._catalog_id("openrouter/anthropic/claude-haiku-4.5") == "anthropic/claude-haiku-4.5"
    assert router._catalog_id("claude-haiku-4-5") == "anthropic/claude-haiku-4.5"


def test_aggregator_is_tried_before_the_direct_backup(router, monkeypatch, tmp_path):
    """Doctrine order: OpenRouter first, direct provider as failover."""
    bundle = tmp_path / "brand"
    bundle.mkdir()
    (bundle / "transports.yaml").write_text(
        "direct:\n  vendor/model: vendor-direct\n", encoding="utf-8"
    )
    cache = _catalog_cache(tmp_path, [_entry("vendor/model", prompt=0.5, completion=2.0, intelligence=50)])
    monkeypatch.setenv("MODEL_CATALOG_CACHE_PATH", str(cache))
    monkeypatch.setenv("MODEL_GATEWAY_PREFIX", "openrouter/")
    monkeypatch.setattr(type(router), "_deployment_bundle_dir", staticmethod(lambda: bundle))

    # Deliberately listed backup-first to prove the order comes from the
    # transport map and not from however the routes happen to be configured.
    routes = [_route("direct", "vendor-direct"), _route("via-or", "openrouter/vendor/model")]

    ordered = router._capability_order(routes, "classification", {})

    assert [r.name for r in ordered] == ["via-or", "direct"]
