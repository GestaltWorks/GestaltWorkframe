"""The `:free` exclusion, pinned at every layer it can be broken at.

This is the defect these tests exist to stop coming back, stated plainly:
`llm/profiles.json` shipped four routes whose model id ended in `:free`, all
`deployment_status: active`, all `enabled_by_default: true`, all carrying
`api_key_env: OPENROUTER_API_KEY`, so they were live third-party network calls
carrying real user prompts. They were classified `cost_tier: "free"`, and
`ProviderRoute.is_cloud` read `cost_tier in {"low_cost", "premium"}`, so they
counted as NOT cloud: `cloud_allowed` was never checked, `response_policy` was
never checked, and the USD caps never applied. A turn declared
`response_policy="local_only"` could be served over the public internet by an
endpoint that generally trains on submitted prompts. A policy named local_only
sending prompts off-box is a promise the code did not keep.

Per docs/standards/model-routing-policy.md the exclusion is a DATA-HANDLING
rule and is UNLIFTABLE: no operator setting, env var or per-deployment config
key may re-enable it, because no setting makes a training corpus forget.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gestaltworkframe.core.model_profile import ProfileStore
from gestaltworkframe.core.router import (
    ROUTING_STRATEGIES,
    LLMRouter,
    ProviderRoute,
    is_free_tier_model,
)

ROOT = Path(__file__).resolve().parent.parent
PROFILES = ROOT / "llm" / "profiles.json"

POLICIES = (None, "local_only", "local_then_low_cost", "local_then_claude_if_high_value")


def _provider():
    class _P:
        model = "x"

    return _P()


def _route(model: str, *, cost_tier: str) -> ProviderRoute:
    return ProviderRoute(
        name="candidate",
        provider=_provider(),
        provider_type="openai_compatible",
        model=model,
        role="primary",
        cost_tier=cost_tier,
        allowed_response_policies=list(POLICIES[1:]),
        capabilities=["chat", "tools"],
        tool_calling_quality="strong",
        routing_priority=99,
    )


def test_no_shipped_profile_names_a_free_tier_endpoint():
    profiles = json.loads(PROFILES.read_text(encoding="utf-8"))["profiles"]
    offenders = {
        name: record["model"]
        for name, record in profiles.items()
        if is_free_tier_model(record["model"])
    }
    assert offenders == {}, f"llm/profiles.json ships `:free` routes: {offenders}"


def test_no_shipped_profile_names_a_router_pseudo_model():
    """`openrouter/auto` delegates the routing decision away from the lane.

    The profile that carried it was internally inconsistent as well: its model
    was `openrouter/auto` while its own description named
    `openrouter/owl-alpha:free`, a different model, and a `:free` one.
    """
    profiles = json.loads(PROFILES.read_text(encoding="utf-8"))["profiles"]
    offenders = {
        name: record["model"]
        for name, record in profiles.items()
        if record["model"].lower().split(":")[0]
        in {"openrouter/auto", "openrouter/auto-beta", "openrouter/free"}
    }
    assert offenders == {}, f"llm/profiles.json ships router pseudo-models: {offenders}"


def test_the_shipped_profile_store_produces_no_free_tier_route():
    """Belt and braces: read it the way the application reads it."""
    store = ProfileStore(PROFILES)
    for profile in store.profiles():
        assert not is_free_tier_model(profile.model), profile.name


@pytest.mark.parametrize("strategy", sorted(ROUTING_STRATEGIES))
@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("cloud_allowed", [True, False])
def test_a_free_tier_route_is_unselectable_under_every_strategy_and_policy(
    strategy, policy, cloud_allowed, monkeypatch
):
    """No code path, under any configuration, may select a `:free` endpoint."""
    monkeypatch.delenv("ENABLE_CAPABILITY_ROUTING", raising=False)
    route = _route("openai/gpt-oss-20b:free", cost_tier="free")
    router = LLMRouter(primary=None, routes=[route], routing_strategy=strategy)
    # Even an operator explicitly switching the route on must not lift it.
    router.set_route_enabled(route.name, True)

    ordered, diagnostics = router._ordered_routes(
        force_secondary=False,
        cloud_allowed=cloud_allowed,
        response_policy=policy,
        task=None,
        publish=False,
    )

    assert ordered == []
    assert diagnostics["candidates"][0]["blocked_reason"] == "free_tier_excluded"
    assert router._route_allowed(route, cloud_allowed, policy) is False


def test_the_same_route_on_a_paid_model_is_selectable():
    """Proves the test above is not passing for some unrelated reason."""
    route = _route("openai/gpt-oss-20b", cost_tier="low_cost")
    router = LLMRouter(primary=None, routes=[route], routing_strategy="best_value")

    ordered, _ = router._ordered_routes(
        force_secondary=False,
        cloud_allowed=True,
        response_policy="local_then_low_cost",
        task=None,
        publish=False,
    )

    assert [r.name for r in ordered] == ["candidate"]


def test_a_free_tier_route_is_classified_as_cloud_not_as_local():
    """The classification bug on its own, independent of the id check.

    `is_cloud` answers "does the prompt leave this box", which is a
    data-handling question, not a billing one. Anything that is not local
    inference on our own hardware is cloud. Billing exposure is `is_metered`.
    """
    free = _route("vendor/model:free", cost_tier="free")
    local = _route("local-model", cost_tier="local")

    assert free.is_cloud is True
    assert free.is_metered is False
    assert local.is_cloud is False


@pytest.mark.parametrize(
    "model_id,expected",
    [
        ("openai/gpt-oss-20b:free", True),
        ("nvidia/llama-3.1-nemotron-ultra-253b-v1:free", True),
        ("openrouter/openai/gpt-oss-120b:free", True),
        ("vendor/model:free-tier", True),
        ("vendor/model", False),
        ("vendor/freedom-model", False),
        ("vendor/model:batch", False),
        ("", False),
    ],
)
def test_free_tier_id_matching_is_on_the_suffix_not_a_substring(model_id, expected):
    assert is_free_tier_model(model_id) is expected
