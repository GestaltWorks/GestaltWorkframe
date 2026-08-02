import copy
import logging
import math
import time
import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any

import os
from pathlib import Path

from gestalt_llm_contract import env as llm_env

from gestaltworkframe.core.cloud_budget import CloudBudgetGate, MultiProviderBudgetGate
from gestaltworkframe.core.model_catalog import CatalogModel, load_cached_catalog_sync
from gestaltworkframe.core.model_lanes import Lane, load_lanes
from gestaltworkframe.core.model_profile import GenerationParams
from gestaltworkframe.core.model_resolver import Resolution, resolve_lane
from gestaltworkframe.core.model_transport import load_transport_map
from gestaltworkframe.core.providers import LLMProvider, OpenAICompatibleProvider
from gestaltworkframe.core.runtime import GenerationConcurrencyPolicy, RuntimeManager

logger = logging.getLogger(__name__)

Message = dict[str, Any]
ToolSpec = dict[str, Any]

ROUTING_STRATEGIES = {"best_value", "prefer_local", "prefer_cloud_quality", "local_only", "cloud_only"}
# best_value ranks every eligible route by task fit plus a modest value lean
# toward cheaper tiers: the best-fitting model wins, but local/low-cost routes
# take ties and near-ties when they are adequate. Task fit dominates (+1000 on
# recommended_for, -2000 on avoid_for), so a premium-only task match
# (complex_implementation, code_review, high_value_service_inquiry, ...) still
# outranks the local lean and genuinely hard turns escalate. The lean only
# orders routes; cloud spend safety is enforced separately by the
# CloudSpendPolicy USD/call caps.
#
# prefer_local and prefer_cloud_quality apply a stronger within-family lean for
# operators who want an explicit tilt; local_only and cloud_only filter route
# families entirely.
# Capability-based routing (docs/standards/model-routing-policy.md).
#
# The ORDER of cloud routes comes from resolving the turn's lane against the
# live model catalog, instead of the hand-assigned routing_priority integers in
# llm/profiles.json. Everything else about a route — provider construction,
# health, spend gates, concurrency — is unchanged.
#
# ON by default since 2026-08-01. It shipped off, set in no .env, no
# .env.example, no compose file and no deployment bundle, which meant the
# correct resolver was dead code and the live ordering was the sum of
# hand-typed priority integers: a shortlist ranked by a stored human preference
# order, which is the mechanism the doctrine records as built and deleted.
# Correct behaviour that ships disabled is not behaviour, it is a comment.
#
# The flag survives as an ESCAPE HATCH to the legacy priority ordering, so an
# operator whose lanes are mis-tuned has a one-variable way back without a
# redeploy. Set ENABLE_CAPABILITY_ROUTING=0 to get it.
CAPABILITY_ROUTING_ENV = "ENABLE_CAPABILITY_ROUTING"
CAPABILITY_ROUTING_DEFAULT = True

# Which OBJECTIVE an operator's routing strategy declares, among the models that
# already qualify for the lane. The lane states the requirement; the tier states
# the objective; the two never collapse onto one axis. `auto` is the default and
# `cheap` is deliberately unreachable from a strategy: it is a claim somebody
# owns ("this work is commodity") on a lane whose bar is deliberately set at the
# lowest reasonable place, never a hidden default that makes the floor the
# target.
STRATEGY_TIERS = {
    "best_value": "auto",
    "prefer_local": "auto",
    "prefer_cloud_quality": "best",
    "local_only": "auto",
    "cloud_only": "auto",
}

# Transport mapping is configuration, not judgement: the gateway prefixes its
# aliases (`openrouter/anthropic/claude-haiku-4.5`) while the catalog keys the
# same model as `anthropic/claude-haiku-4.5`.
GATEWAY_PREFIX_ENV = "MODEL_GATEWAY_PREFIX"
DEFAULT_GATEWAY_PREFIX = "openrouter/"

# Which lane a task belongs to. Unmapped tasks use the guide lane, the default
# conversational turn.
TASK_LANES = {
    "code_review": "review",
    "critical_code_review": "review",
    "deep_code_review": "review",
    "security_review": "review",
    "coding": "build",
    "implementation_help": "build",
    "large_refactor": "build",
    "classification": "lookup",
    "routine_chat": "lookup",
    "basic_rewst_help": "lookup",
}
DEFAULT_LANE = "guide"


# `:free` aggregator tiers are excluded as a DATA-HANDLING rule, not as a cost
# preference. Those endpoints generally train on submitted prompts, so routing a
# user's turn to one hands that turn to a third party as training data. Per
# docs/standards/model-routing-policy.md the exclusion is UNLIFTABLE: no env var,
# no operator setting and no per-deployment config key may re-enable it, because
# no setting makes a training corpus forget. "Free" in this shop means local
# inference on our own hardware; it never means an aggregator's free tier.
#
# The check runs on the DISPATCH id, which is the only id anything actually
# sends, and it runs before every strategy, policy and enablement check.
FREE_TIER_SEGMENTS = frozenset({"free", "free-tier"})


def is_free_tier_model(model_id: str) -> bool:
    """True when a dispatch id names an aggregator `:free` endpoint."""
    lowered = (model_id or "").strip().lower()
    return any(segment in FREE_TIER_SEGMENTS for segment in lowered.split(":")[1:])


# ---------------------------------------------------------------------------
# Catalog-derived routes: closing the candidate-set gap
# ---------------------------------------------------------------------------
#
# resolve_lane ranks the WHOLE live catalog, but the router used to keep only
# the routes it already had configured in llm/profiles.json:
#
#     cleared = [r for r in cloud_routes if self._catalog_id(r.model) in rank]
#
# So it REORDERED a hand-typed list and never SELECTED from the catalog. A
# better or cheaper model could ship and never be chosen. That is the same
# stale-constant failure the whole doctrine exists to abolish, relocated from a
# model id to a curated list, and the doctrine names it outright: "resolve to a
# shortlist, then order it by a stored human preference" is a hardwire with a
# config file in front of it.
#
# The fix is to let the lane winner be CONSTRUCTED on demand. It is only clean
# because the OpenRouter transport is fully generic: a route is
# OpenAICompatibleProvider(base_url, api_key, model, params) where the only
# per-model input is the model string, so every catalog model is reachable
# through the same key and the same base URL. Nothing else is synthesizable and
# nothing else is synthesized: never a local route (there is no such thing as a
# model we do not host) and never a direct-vendor route (its alias is named by
# the gateway, not derivable).
#
# llm/profiles.json therefore stops being the candidate set. It is OVERRIDES AND
# PINS: a configured profile still wins over a synthesized route for the same
# catalog id, so operator params, task tags and price overrides are never
# silently discarded.
SYNTHESIZED_ROUTE_PREFIX = "catalog:"

# Which spend bucket a catalog-derived route lands in, so caps and reporting
# keep working instead of being handed an invented tier.
#
# One number: the model's COMPLETION price. Completion is priced 3-5x input, so
# it dominates the bill on any turn that produces real work, and the pathology
# the doctrine names by name -- cheap to prompt, expensive to answer -- lands on
# the premium side, which is the safe side of a spend cap.
#
# $5.00/M is where the configured table already splits, with a wide gap on
# either side rather than a knife edge: every low_cost profile completes at
# $0.20 (deepseek-v4-flash), $0.60 (gemini-2.5-flash) or $4.00 (claude-haiku-4-5)
# per million, and every premium profile at $15.00 (sonnet) or $25.00 (opus).
# The comparison is strictly greater-than, so claude-haiku-4.5's catalog rate of
# exactly $5.00/M classifies low_cost, matching its configured sibling.
#
# This bucket is NOT how routes are ranked. Ranking is cost per turn at the
# lane's declared shape, computed in model_resolver against input, output and
# cache-read rates. A bucket has to be a property of the MODEL rather than of
# the turn, or the same model would land in different buckets on different lanes
# and a spend cap would stop meaning anything.
SYNTHESIZED_PREMIUM_COMPLETION_PRICE_USD_PER_MILLION = 5.0

# Which response policies a catalog-derived route may serve, per bucket. This
# mirrors what the configured profiles declare for the same buckets, with ONE
# property that is load-bearing rather than cosmetic:
#
#     "local_only" appears in neither list, and must never appear in either.
#
# A synthesized route is a third party on the public internet. The previous pass
# proved free OpenRouter endpoints had been silently serving local_only turns,
# which is the exact data-handling breach being closed; letting a route the
# router invented for itself serve local_only would reopen it wider, because
# nobody typed the route into a file where a reviewer could see it.
SYNTHESIZED_RESPONSE_POLICIES: dict[str, list[str]] = {
    "low_cost": ["local_then_low_cost", "local_then_claude_if_high_value"],
    "premium": ["local_then_claude_if_high_value"],
}

# Published agentic index at or above which a catalog-derived route claims
# "strong" tool calling. `agentic_index` is the doctrine's stated proxy for tool
# adherence, and it is measured, which a hand-typed profile string is not.
# 44.4 is the p90 of the published distribution (2026-07-25, n=107: p50 18.2,
# p90 44.4, max 55.3), the same capture the guide lane's floors were calibrated
# against. Top decile of measured tool adherence is the honest reading of
# "strong"; anything else that publishes `tools` claims "ok".
SYNTHESIZED_STRONG_TOOL_AGENTIC_INDEX = 44.4

# A synthesized route has no operator-tuned generation params by definition:
# that is precisely what a configured profile carries and why one wins over a
# synthesized route for the same catalog id. Spend caps still override this via
# CloudBudgetGate.config.max_output_tokens_per_call.
SYNTHESIZED_MAX_OUTPUT_TOKENS = 4096


ROUTE_COST_ADJUSTMENTS = {
    "best_value": {"local": 120, "low_cost": 40, "premium": 0},
    "prefer_local": {"local": 300, "low_cost": 75, "premium": 0},
    "prefer_cloud_quality": {"local": 0, "low_cost": 250, "premium": 300},
    "local_only": {"local": 0, "low_cost": 0, "premium": 0},
    "cloud_only": {"local": 0, "low_cost": 0, "premium": 0},
}


@dataclass
class ProviderRoute:
    name: str
    provider: LLMProvider | None
    provider_type: str
    model: str
    role: str
    cost_tier: str
    allowed_response_policies: list[str]
    recommended_for: list[str] = field(default_factory=list)
    avoid_for: list[str] = field(default_factory=list)
    routing_priority: int = 0
    configured: bool = True
    blocked_reason: str = ""
    deployment_status: str = "active"
    runtime_group: str = ""
    enabled_by_default: bool = True
    capabilities: list[str] = field(default_factory=list)
    tool_calling_quality: str = "none"
    input_price_usd_per_million: float = 0.0
    output_price_usd_per_million: float = 0.0
    provider_budget_id: str = "default"
    preferred_provider_id: str = ""
    catalog_derived: bool = False
    """Built on demand from the live catalog rather than from llm/profiles.json.

    Surfaced in diagnostics, health output and the admin panel so a route nobody
    typed into a file is never mistaken for one somebody did. The name carries
    the same signal (`catalog:<id>`), because a log line only has the name.
    """

    @property
    def is_cloud(self) -> bool:
        # "Cloud" means the prompt leaves this box, which is a data-handling
        # question and not a billing one. This used to read
        # `cost_tier in {"low_cost", "premium"}`, so a `cost_tier: "free"`
        # OpenRouter route counted as NOT cloud: it skipped cloud_allowed, it
        # skipped the response_policy check, and a turn declared
        # response_policy="local_only" could be served over the public internet
        # by a third party. Only local inference on our own hardware is not
        # cloud. Billing exposure is a separate question, answered by
        # `is_metered`.
        return self.cost_tier != "local"

    @property
    def is_metered(self) -> bool:
        """Does a turn on this route cost money? Spend caps key off this."""
        return self.cost_tier in {"low_cost", "premium"}

    @property
    def is_free_tier(self) -> bool:
        """An aggregator `:free` endpoint. Never selectable; see the module note."""
        return is_free_tier_model(self.model)

    @property
    def supports_tools(self) -> bool:
        return "tools" in {capability.strip().lower() for capability in self.capabilities}

    @property
    def reliable_tool_calling(self) -> bool:
        return self.supports_tools and self.tool_calling_quality in {"ok", "strong"}


class LLMRouter:
    _LOCAL_UNAVAILABLE = {
        "content": (
            "The local model is temporarily unavailable. Cloud escalation is not "
            "available for this session. Please try again shortly."
        )
    }
    _SENSITIVE_CONTEXT_LOCAL_REQUIRED = {
        "content": (
            "This context is marked local-only, so I cannot send it to a cloud model. "
            "The local model is unavailable right now. Please retry when local inference is healthy or ask an operator to review the source privacy settings."
        )
    }

    def __init__(
        self,
        primary: LLMProvider,
        secondary: LLMProvider | None = None,
        error_threshold: int = 3,
        cloud_budget: CloudBudgetGate | None = None,
        routes: list[ProviderRoute] | None = None,
        routing_strategy: str = "best_value",
        health_cache_ttl_seconds: float = 5.0,
        runtime_manager: RuntimeManager | None = None,
        generation_concurrency_policy: GenerationConcurrencyPolicy | None = None,
    ) -> None:
        self.primary = primary
        self.secondary = secondary
        self.error_threshold = error_threshold
        self.cloud_budget = cloud_budget
        self.routes = routes or self._default_routes(primary, secondary)
        self._error_count = 0
        self._breaker_open = False
        self._route_error_count: dict[str, int] = {}
        self._route_breaker_open: set[str] = set()
        self._route_overrides: dict[str, bool] = {}
        self._health_cache_ttl_seconds = max(0.0, health_cache_ttl_seconds)
        self._health_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._last_route_decision: dict[str, Any] = {}
        # Catalog-derived routes, keyed by catalog id. Cached so the httpx
        # client behind a synthesized route is built once and can be closed and
        # re-keyed like any other, not rebuilt per turn and leaked. It holds one
        # entry per model that has ever won a lane in this process, which is
        # bounded by lanes x shortlist_size at any moment and grows only as
        # slowly as the catalog's winners change.
        self._synthesized_routes: dict[str, ProviderRoute] = {}
        self.routing_strategy = self._clean_routing_strategy(routing_strategy)
        self.runtime_manager = runtime_manager
        self.generation_concurrency_policy = generation_concurrency_policy or GenerationConcurrencyPolicy.from_env()
        self._generation_lock = asyncio.Lock()
        self._active_generations_total = 0
        self._active_local_generations = 0
        self._active_cloud_generations = 0

    def _default_routes(self, primary: LLMProvider, secondary: LLMProvider | None) -> list[ProviderRoute]:
        routes = [self._route_from_provider("primary", primary, "primary", "local", ["local_only"])]
        if secondary:
            routes.append(self._route_from_provider("secondary", secondary, "secondary", "low_cost", ["local_then_low_cost"]))
        return routes

    def _route_from_provider(
        self,
        name: str,
        provider: LLMProvider,
        role: str,
        cost_tier: str,
        policies: list[str],
    ) -> ProviderRoute:
        profile_name = getattr(provider, "profile_name", name)
        provider_role = getattr(provider, "provider_role", role)
        provider_cost_tier = getattr(provider, "cost_tier", cost_tier)
        provider_policies = getattr(provider, "allowed_response_policies", policies)
        provider_capabilities = getattr(provider, "capabilities", [])
        provider_tool_quality = getattr(provider, "tool_calling_quality", "none")
        return ProviderRoute(
            name=profile_name if isinstance(profile_name, str) else name,
            provider=provider,
            provider_type=provider.__class__.__name__,
            model=getattr(provider, "model", ""),
            role=provider_role if isinstance(provider_role, str) else role,
            cost_tier=provider_cost_tier if isinstance(provider_cost_tier, str) else cost_tier,
            allowed_response_policies=provider_policies if isinstance(provider_policies, list) else policies,
            capabilities=provider_capabilities if isinstance(provider_capabilities, list) else [],
            tool_calling_quality=provider_tool_quality if isinstance(provider_tool_quality, str) else "none",
            configured=True,
        )

    def _all_routes(self) -> list[ProviderRoute]:
        """Configured routes plus every catalog-derived route built so far.

        Enablement, health, key rotation, the breaker and shutdown all have to
        see a synthesized route: one the operator cannot disable, cannot see the
        health of and cannot rotate a key on would be a route outside the
        controls, which is worse than the stale list it replaces.
        """
        return [*self.routes, *self._synthesized_routes.values()]

    async def close(self) -> None:
        closed: set[int] = set()
        for route in self._all_routes():
            provider = route.provider
            if provider is None or id(provider) in closed or not hasattr(provider, "close"):
                continue
            closed.add(id(provider))
            await provider.close()

    async def rotate_provider_key(self, provider_budget_id: str, new_key: str) -> int:
        """Push a new API key to all live routes matching provider_budget_id.

        Calls update_api_key(new_key) on each provider that supports it.
        Returns the count of providers updated.
        """
        updated: set[int] = set()
        for route in self._all_routes():
            if route.provider_budget_id != provider_budget_id:
                continue
            provider = route.provider
            if provider is None or id(provider) in updated:
                continue
            if not hasattr(provider, "update_api_key"):
                continue
            await provider.update_api_key(new_key)
            updated.add(id(provider))
        return len(updated)

    def has_tool_capable_route(
        self,
        *,
        cloud_allowed: bool = False,
        response_policy: str | None = None,
        task: str | None = None,
        context_cloud_eligible: bool = True,
    ) -> bool:
        ordered_routes, _diagnostics = self._ordered_routes(
            False,
            cloud_allowed,
            response_policy,
            task,
            context_cloud_eligible,
            require_tools=True,
            publish=False,
        )
        return bool(ordered_routes)

    async def provider_statuses(
        self,
        response_policy: str | None = None,
        cloud_allowed: bool = False,
        enabled_cost_tiers: set[str] | None = None,
        force_refresh: bool = False,
        check_disabled_local_routes: bool = False,
    ) -> list[dict[str, Any]]:
        statuses = []
        for route in self._all_routes():
            health = {
                "endpoint_healthy": False,
                "model_available": False,
                "available_models": [],
                "checked": False,
                "cached": False,
                "checked_at": None,
            }
            if route.provider is not None and self._should_check_health(route, check_disabled_local_routes):
                health = await self._provider_health(route, force_refresh=force_refresh)
            model_available = bool(health["model_available"])
            endpoint_healthy = bool(health["endpoint_healthy"])
            runtime = self._runtime_status(route, health)
            callable_now = (
                model_available
                and self._route_allowed(route, cloud_allowed, response_policy, enabled_cost_tiers)
                and not self._route_temporarily_blocked(route)
            )
            statuses.append({
                "name": route.name,
                "role": route.role,
                "configured": route.configured,
                "admin_enabled": self.route_enabled(route.name),
                "enabled_by_default": route.enabled_by_default,
                "deployment_status": route.deployment_status,
                "runtime_group": route.runtime_group,
                "healthy": model_available,
                "endpoint_healthy": endpoint_healthy,
                "model_available": model_available,
                "health_checked": bool(health.get("checked")),
                "health_cached": bool(health.get("cached")),
                "health_checked_at": health.get("checked_at"),
                "available_models": health["available_models"],
                "callable": callable_now,
                "blocked_reason": self._route_blocked_reason(route, health, cloud_allowed, response_policy, enabled_cost_tiers),
                "provider_type": route.provider_type,
                "model": route.model,
                "profile_name": route.name,
                "provider_role": route.role,
                "cost_tier": route.cost_tier,
                "allowed_response_policies": route.allowed_response_policies,
                "capabilities": route.capabilities,
                "tool_calling_quality": route.tool_calling_quality,
                "supports_tools": route.supports_tools,
                "reliable_tool_calling": route.reliable_tool_calling,
                "recommended_for": route.recommended_for,
                "avoid_for": route.avoid_for,
                "routing_priority": route.routing_priority,
                "catalog_derived": route.catalog_derived,
                **runtime,
            })
        return statuses

    def _should_check_health(self, route: ProviderRoute, check_disabled_local_routes: bool = False) -> bool:
        return route.provider is not None and route.configured and (
            self.route_enabled(route.name) or (check_disabled_local_routes and not route.is_cloud)
        )

    async def _provider_health(self, route: ProviderRoute, force_refresh: bool = False) -> dict[str, Any]:
        key = self._route_key(route)
        now = time.monotonic()
        cached = self._health_cache.get(key)
        if (
            cached
            and not force_refresh
            and self._health_cache_ttl_seconds > 0
            and now - cached[0] <= self._health_cache_ttl_seconds
        ):
            return {**cached[1], "cached": True}

        provider = route.provider
        assert provider is not None
        checked_at = time.time()  # Wall time is for display only; monotonic time controls TTL.
        try:
            health_status = getattr(provider, "health_status", None)
            if callable(health_status):
                result = await health_status()
                if isinstance(result, dict) and "endpoint_healthy" in result and "model_available" in result:
                    payload = {
                        "endpoint_healthy": bool(result.get("endpoint_healthy")),
                        "model_available": bool(result.get("model_available")),
                        "available_models": result.get("available_models") or [],
                        "checked": True,
                        "cached": False,
                        "checked_at": checked_at,
                    }
                    self._health_cache[key] = (now, payload)
                    return payload
            healthy = await provider.is_healthy()
            payload = {
                "endpoint_healthy": healthy,
                "model_available": healthy,
                "available_models": [],
                "checked": True,
                "cached": False,
                "checked_at": checked_at,
            }
            self._health_cache[key] = (now, payload)
            return payload
        except Exception:
            payload = {
                "endpoint_healthy": False,
                "model_available": False,
                "available_models": [],
                "checked": True,
                "cached": False,
                "checked_at": checked_at,
            }
            self._health_cache[key] = (now, payload)
            return payload

    def _route_allowed(
        self,
        route: ProviderRoute,
        cloud_allowed: bool,
        response_policy: str | None,
        enabled_cost_tiers: set[str] | None = None,
    ) -> bool:
        # Unliftable, and first: no strategy, policy or operator override may
        # re-admit a `:free` endpoint.
        if route.is_free_tier:
            return False
        if route.deployment_status == "disabled":
            return False
        if not route.configured or route.provider is None:
            return False
        if not self.route_enabled(route.name):
            return False
        if not route.is_cloud:
            return True
        if not cloud_allowed:
            return False
        if enabled_cost_tiers is not None and route.cost_tier not in enabled_cost_tiers:
            return False
        if response_policy is None:
            return True
        return response_policy in route.allowed_response_policies

    def _route_blocked_reason(
        self,
        route: ProviderRoute,
        health: dict[str, Any],
        cloud_allowed: bool,
        response_policy: str | None,
        enabled_cost_tiers: set[str] | None = None,
    ) -> str:
        if route.is_free_tier:
            return "free_tier_excluded"
        if route.deployment_status == "disabled":
            return "deployment_disabled"
        if not route.configured:
            return route.blocked_reason or "not_configured"
        if route.provider is None:
            return route.blocked_reason or "provider_not_built"
        if not self.route_enabled(route.name):
            if route.deployment_status == "candidate" and route.name not in self._route_overrides:
                return "candidate_not_enabled"
            return "route_disabled"
        if not health.get("checked"):
            return "health_not_checked"
        if not health["endpoint_healthy"]:
            return "health_check_failed"
        if not health["model_available"]:
            return "model_not_available"
        if self._route_temporarily_blocked(route):
            return "circuit_breaker_open"
        if route.is_cloud and not cloud_allowed:
            return "cloud_policy_not_enabled"
        if route.is_cloud and enabled_cost_tiers is not None and route.cost_tier not in enabled_cost_tiers:
            return "cloud_tier_disabled"
        if route.is_cloud and response_policy is not None and response_policy not in route.allowed_response_policies:
            return "not_allowed_for_response_policy"
        return ""

    def route_enabled(self, route_name: str) -> bool:
        route = next((item for item in self._all_routes() if item.name == route_name), None)
        if route and route.deployment_status == "disabled":
            return False
        if route_name in self._route_overrides:
            return self._route_overrides[route_name]
        return route.enabled_by_default if route else True

    def set_route_enabled(self, route_name: str, enabled: bool) -> None:
        self._route_overrides[route_name] = enabled
        route = next((item for item in self._all_routes() if item.name == route_name), None)
        if route:
            self._health_cache.pop(self._route_key(route), None)

    def route_overrides(self) -> dict[str, bool]:
        return {route.name: self.route_enabled(route.name) for route in self._all_routes()}

    def _clean_routing_strategy(self, strategy: str | None) -> str:
        value = (strategy or "best_value").strip().lower()
        if value in ROUTING_STRATEGIES:
            return value
        logger.warning("Unknown routing strategy %r; falling back to best_value", strategy)
        return "best_value"

    def set_routing_strategy(self, strategy: str | None) -> None:
        value = (strategy or "").strip().lower()
        if value not in ROUTING_STRATEGIES:
            raise ValueError(f"Unknown routing_strategy: {strategy}")
        self.routing_strategy = value

    # ------------------------------------------------------------------
    # Capability-based ordering
    # ------------------------------------------------------------------

    @staticmethod
    def _capability_routing_enabled() -> bool:
        raw = os.getenv(CAPABILITY_ROUTING_ENV, "").strip().lower()
        if not raw:
            return CAPABILITY_ROUTING_DEFAULT
        if raw in {"1", "true", "yes", "on"}:
            return True
        if raw in {"0", "false", "no", "off"}:
            return False
        logger.warning(
            "%s=%r is not a boolean; using the default (%s)",
            CAPABILITY_ROUTING_ENV,
            raw,
            CAPABILITY_ROUTING_DEFAULT,
        )
        return CAPABILITY_ROUTING_DEFAULT

    def _transports(self):
        return load_transport_map(self._deployment_bundle_dir())

    def _catalog_id(self, model: str) -> str:
        """The catalog id an alias refers to, via either transport.

        House doctrine is OpenRouter primary with Anthropic backup, so the
        aggregator alias and the direct alias are two routes to one model and
        must resolve to the same catalog entry — otherwise the backup never
        inherits the lane ranking and can never be chosen.
        """
        return self._transports().catalog_id(model)

    @staticmethod
    def _deployment_bundle_dir() -> Path | None:
        """Where this deployment's lanes.yaml lives, if the bundle resolves."""
        try:
            from gestaltworkframe.core.deployment_config import _deployment_id, _deployment_root

            return _deployment_root(_deployment_id())
        except Exception:  # bad DEPLOYMENT_ID or missing bundle: defaults stand
            return None

    def _lane_for(self, task: str | None) -> Lane:
        lanes = load_lanes(self._deployment_bundle_dir())
        name = TASK_LANES.get((task or "").strip(), DEFAULT_LANE)
        return lanes.get(name) or lanes[DEFAULT_LANE]

    @staticmethod
    def _openrouter_credentials() -> tuple[str, str]:
        """The one key and base URL every catalog model is reachable through.

        Empty key means no synthesis at all. This is the same pair
        `provider_registry._route_from_openrouter_profile` builds a configured
        OpenRouter route from, and the dispatch id is the BARE catalog id for
        the same reason it is there: the OpenRouter API keys models by catalog
        slug. `MODEL_GATEWAY_PREFIX` describes a broker in front of the
        aggregator and does not apply to a route pointed straight at it.
        """
        key = os.getenv(llm_env.OPENROUTER_API_KEY, "").strip()
        base_url = os.getenv(llm_env.OPENROUTER_BASE_URL, "").strip() or llm_env.DEFAULT_OPENROUTER_BASE_URL
        return key, base_url

    @staticmethod
    def _catalog_cost_tier(model: CatalogModel) -> str:
        """Spend bucket from the published price. See the constant's note."""
        completion_per_million = model.completion_price * 1_000_000
        if completion_per_million > SYNTHESIZED_PREMIUM_COMPLETION_PRICE_USD_PER_MILLION:
            return "premium"
        return "low_cost"

    def _synthesized_base_route(self, model: CatalogModel) -> ProviderRoute | None:
        """Build (once) the catalog-derived route for one model, or None.

        The provider is cached so its httpx client is created once and stays
        visible to close(), rotate_provider_key() and the health path.
        """
        cached = self._synthesized_routes.get(model.id)
        if cached is not None:
            return cached
        api_key, base_url = self._openrouter_credentials()
        if not api_key:
            return None
        provider = OpenAICompatibleProvider(
            base_url=base_url,
            api_key=api_key,
            model=model.id,
            params=GenerationParams(max_tokens=SYNTHESIZED_MAX_OUTPUT_TOKENS),
        )
        capabilities = ["chat", "rag_answering"]
        if model.supports("tools"):
            capabilities.append("tools")
        if (model.agentic_index or 0.0) >= SYNTHESIZED_STRONG_TOOL_AGENTIC_INDEX:
            tool_quality = "strong"
        elif model.supports("tools"):
            tool_quality = "ok"
        else:
            tool_quality = "none"
        route = ProviderRoute(
            name=f"{SYNTHESIZED_ROUTE_PREFIX}{model.id}",
            provider=provider,
            provider_type="openrouter",
            model=model.id,
            role="secondary",
            cost_tier=self._catalog_cost_tier(model),
            allowed_response_policies=list(
                SYNTHESIZED_RESPONSE_POLICIES[self._catalog_cost_tier(model)]
            ),
            # No routing_priority. A catalog-derived route has no hand-typed
            # integer to carry, which is the entire point; ordering comes from
            # the lane, redealt onto the cloud family's own anchors.
            routing_priority=0,
            configured=True,
            deployment_status="active",
            runtime_group="openrouter",
            enabled_by_default=True,
            capabilities=capabilities,
            tool_calling_quality=tool_quality,
            input_price_usd_per_million=model.prompt_price * 1_000_000,
            output_price_usd_per_million=model.completion_price * 1_000_000,
            provider_budget_id="openrouter",
            catalog_derived=True,
        )
        self._synthesized_routes[model.id] = route
        return route

    def _synthesize_cloud_routes(
        self,
        resolution: Resolution,
        configured_cloud: list[ProviderRoute],
        task: str | None,
    ) -> list[ProviderRoute]:
        """Catalog-derived routes for lane winners nothing configured covers.

        Every candidate here already cleared the lane inside `resolve_lane`,
        which means it has already passed the unliftable `:free` and `:batch`
        exclusions, the router pseudo-model exclusion, the non-positive-price
        rejection, the lane's floors and the lane's DECLARED PRICE CEILING. The
        checks repeated below are belt and braces on a data-handling rule, not
        redundancy for its own sake: they run against the DISPATCH id, which is
        the string that would actually be sent.
        """
        configured_ids = {self._catalog_id(route.model) for route in configured_cloud}
        # A configured profile earns +1000 from `recommended_for` naming this
        # task. A catalog-derived route cleared the same task's LANE against
        # measured indices, which is the machine-checkable version of the same
        # claim, so it carries the task as its own recommendation -- but ONLY
        # when the cloud family already claims the task. Without the first half
        # the hand-typed tag list would still decide every tagged turn and the
        # candidate-set gap would merely have moved from `routing_priority` to
        # `recommended_for`. Without the second half a synthesized route would
        # introduce a cloud task match that did not exist before, and a turn
        # tagged for a local profile would start escalating.
        cloud_claims_task = any(
            self._task_matches(task, route.recommended_for) for route in configured_cloud
        )
        synthesized: list[ProviderRoute] = []
        for candidate in resolution.candidates:
            model = candidate.model
            if model.id in configured_ids:
                continue  # an operator's pin, params and overrides win
            if is_free_tier_model(model.id) or model.is_batch_tier or model.is_router_pseudo_model:
                continue
            if not (candidate.cost_per_turn_usd > 0):
                continue
            base = self._synthesized_base_route(model)
            if base is None:
                return []  # no key: nothing is reachable, so synthesize nothing
            synthesized.append(
                replace(
                    base,
                    cost_tier=self._catalog_cost_tier(model),
                    allowed_response_policies=list(
                        SYNTHESIZED_RESPONSE_POLICIES[self._catalog_cost_tier(model)]
                    ),
                    input_price_usd_per_million=model.prompt_price * 1_000_000,
                    output_price_usd_per_million=model.completion_price * 1_000_000,
                    recommended_for=[task] if (cloud_claims_task and task) else [],
                )
            )
        return synthesized

    def _capability_order(
        self,
        cloud_routes: list[ProviderRoute],
        task: str | None,
        diagnostics: dict[str, Any],
        strategy: str | None = None,
        gate: Callable[[ProviderRoute], str] | None = None,
    ) -> list[ProviderRoute]:
        """Order cloud routes by resolving the turn's lane against the catalog.

        Returns the routes whose model cleared the lane, ranked by the lane's
        preference. Routes the catalog does not know about are kept, ordered
        last: an unlisted model is unranked, not disqualified, and dropping it
        would take a working self-hosted or private route offline.

        When `gate` is supplied the candidate set is the CATALOG, not
        llm/profiles.json: lane winners nothing configured covers are
        synthesized onto the OpenRouter transport and then put through that same
        gate, which is the caller's own `_selection_blocked_reason`. A
        synthesized route therefore passes every check a configured route
        passes, including the response-policy rules that keep any cloud route
        out of a `local_only` turn. A caller that only wants an ordering (there
        is no gate to run and nothing to dispatch) passes no gate and gets none.

        Falls back to the caller's existing order if nothing clears the lane,
        so a mis-tuned floor degrades the ordering rather than the service.
        """
        if not cloud_routes and gate is None:
            return cloud_routes

        lane = self._lane_for(task)
        tier = STRATEGY_TIERS.get(self._clean_routing_strategy(strategy), "auto")
        catalog = load_cached_catalog_sync()
        # Availability is observed, not published. Reuse the breaker the router
        # already maintains rather than inventing a second failure signal.
        benched = {
            self._catalog_id(route.model)
            for route in cloud_routes
            if self._route_key(route) in self._route_breaker_open
            or self._route_error_count.get(self._route_key(route), 0) >= self.error_threshold
        }
        benched |= {
            route.model
            for route in self._synthesized_routes.values()
            if self._route_key(route) in self._route_breaker_open
            or self._route_error_count.get(self._route_key(route), 0) >= self.error_threshold
        }
        resolution = resolve_lane(lane, catalog, tier=tier, benched=benched)

        candidates = list(cloud_routes)
        if gate is not None:
            for route in self._synthesize_cloud_routes(resolution, cloud_routes, task):
                blocked = gate(route)
                diagnostics.setdefault("candidates", []).append(
                    self._candidate_diagnostic(route, blocked, task, strategy)
                )
                if not blocked:
                    candidates.append(route)

        rank = {candidate.id: position for position, candidate in enumerate(resolution.candidates)}
        known = {model.id for model in catalog}
        transports = self._transports()

        cleared = [r for r in candidates if self._catalog_id(r.model) in rank]
        unlisted = [r for r in candidates if self._catalog_id(r.model) not in known]
        # Model rank first, then transport: the aggregator is primary and the
        # direct provider is the backup for the SAME model, so a failure of the
        # aggregator retries that model directly before moving to another one.
        cleared.sort(key=lambda r: (rank[self._catalog_id(r.model)], transports.transport_kind(r.model)))

        diagnostics["capability_lane"] = lane.name
        diagnostics["capability_tier"] = resolution.tier
        diagnostics["capability_objective"] = resolution.objective
        diagnostics["capability_choice"] = resolution.explain()
        diagnostics["capability_synthesized"] = [r.name for r in candidates if r.catalog_derived]
        diagnostics["capability_rejected"] = [
            f"{rejection.model_id}: {rejection.reason}"
            for rejection in resolution.rejections
            if rejection.model_id in {self._catalog_id(r.model) for r in cloud_routes}
        ]

        ordered = cleared + unlisted
        if not ordered:
            diagnostics["capability_choice"] = (
                f"lane {lane.name}: nothing cleared it, configured or catalog-derived; "
                "keeping static order"
            )
            return cloud_routes
        return ordered

    def _ordered_routes(
        self,
        force_secondary: bool,
        cloud_allowed: bool,
        response_policy: str | None,
        task: str | None,
        context_cloud_eligible: bool = True,
        require_tools: bool = False,
        publish: bool = True,
    ) -> tuple[list[ProviderRoute], dict[str, Any]]:
        strategy = self.routing_strategy
        if force_secondary and strategy == "local_only":
            logger.warning("force_secondary requested while routing_strategy=local_only; no provider route can match both constraints.")
        diagnostics: dict[str, Any] = {
            "routing_strategy": strategy,
            "force_secondary": force_secondary,
            "cloud_allowed": cloud_allowed,
            "response_policy": response_policy,
            "task": task,
            "context_cloud_eligible": context_cloud_eligible,
            "require_tools": require_tools,
            "candidates": [],
            "ordered_routes": [],
            "selected_route": "",
            "empty_reason": "",
            "concurrency": self._generation_concurrency_snapshot_unlocked(),
        }

        # The one gate every route goes through, configured or catalog-derived.
        # A synthesized route that skipped any part of this would be a route
        # outside the policy, which is the failure it exists to fix, inverted.
        def gate(route: ProviderRoute) -> str:
            return self._selection_blocked_reason(
                route,
                force_secondary,
                cloud_allowed,
                response_policy,
                strategy,
                context_cloud_eligible,
                require_tools,
            )

        routes = []
        for route in self.routes:
            blocked_reason = gate(route)
            diagnostics["candidates"].append(
                self._candidate_diagnostic(route, blocked_reason, task, strategy)
            )
            if not blocked_reason:
                routes.append(route)
        local = [route for route in routes if not route.is_cloud]
        cloud = [route for route in routes if route.is_cloud]
        # The lane's ranking SUBSTITUTES for the hand-assigned routing_priority
        # on cloud routes; it does not replace the whole scoring path. Task fit,
        # the tool-calling requirement, runtime health and the direct-provider
        # headroom bonus are router concerns and still apply. What stops
        # deciding is the priority integer typed into llm/profiles.json, which
        # is the "shortlist ranked by a stored human preference order" the
        # doctrine records as deleted.
        capability_priority: dict[str, int] = {}
        if self._capability_routing_enabled():
            reordered = self._capability_order(cloud, task, diagnostics, strategy, gate=gate)
            if reordered is not cloud:
                # Redeal the cloud family's OWN priority numbers in lane order.
                # Which cloud route gets the top anchor is now the lane's answer
                # rather than whichever integer somebody typed highest, while the
                # numbers themselves are unchanged, so the calibration between
                # the cloud family and local routes (the best_value lean, the
                # task-fit weights) keeps working exactly as it did. Inventing a
                # fresh scale here silently overpowered that lean and sent
                # routine, task-less turns to cloud.
                # The anchor pool is every cloud candidate the turn had, not
                # only the survivors: a lane that drops two configured routes
                # must not also shrink the cloud family's top score against
                # local. Catalog-derived routes contribute their own (zero)
                # priority, so they widen the pool without inflating it.
                anchors = sorted(
                    [route.routing_priority for route in cloud]
                    + [route.routing_priority for route in reordered if route.catalog_derived],
                    reverse=True,
                )
                capability_priority = {
                    route.name: anchors[position] if position < len(anchors) else 0
                    for position, route in enumerate(reordered)
                }
                # Routes the lane dropped are gone; the survivors are listed in
                # lane order so a score tie falls through to the lane, never to
                # however the profile file happened to be written.
                routes = local + list(reordered)
            cloud = reordered

        def _score(route: ProviderRoute) -> int:
            return self._route_score(route, task, strategy, capability_priority)

        if strategy == "prefer_local":
            ordered = sorted(local, key=_score, reverse=True) + sorted(cloud, key=_score, reverse=True)
        elif strategy == "prefer_cloud_quality":
            ordered = sorted(cloud, key=_score, reverse=True) + sorted(local, key=_score, reverse=True)
        else:
            ordered = sorted(routes, key=_score, reverse=True)
        diagnostics["ordered_routes"] = [route.name for route in ordered]
        if not ordered and force_secondary and strategy == "local_only":
            diagnostics["empty_reason"] = "force_secondary_conflicts_with_local_only"
        if publish:
            self._publish_route_decision(diagnostics)
        logger.debug(
            "LLM route decision strategy=%s task=%s ordered=%s",
            strategy,
            task or "",
            diagnostics["ordered_routes"],
        )
        return ordered, diagnostics

    def _candidate_diagnostic(
        self,
        route: ProviderRoute,
        blocked_reason: str,
        task: str | None,
        strategy: str | None,
    ) -> dict[str, Any]:
        """One route's line in the decision record.

        Configured and catalog-derived routes report identically apart from the
        `catalog_derived` flag, so "where did this route come from" and "why did
        it lose" have the same answer shape for both.
        """
        return {
            "name": route.name,
            "model": route.model,
            "cost_tier": route.cost_tier,
            "deployment_status": route.deployment_status,
            "runtime_group": route.runtime_group,
            "enabled": self.route_enabled(route.name),
            "supports_tools": route.supports_tools,
            "tool_calling_quality": route.tool_calling_quality,
            "score": None if blocked_reason else self._route_score(route, task, strategy),
            "blocked_reason": blocked_reason,
            "recommended_match": self._task_matches(task, route.recommended_for),
            "avoid_match": self._task_matches(task, route.avoid_for),
            "catalog_derived": route.catalog_derived,
            **self._runtime_status(route, self._cached_provider_health(route)),
        }

    def _selection_blocked_reason(
        self,
        route: ProviderRoute,
        force_secondary: bool,
        cloud_allowed: bool,
        response_policy: str | None,
        strategy: str,
        context_cloud_eligible: bool = True,
        require_tools: bool = False,
    ) -> str:
        if route.is_free_tier:
            return "free_tier_excluded"
        if strategy == "local_only" and route.is_cloud:
            return "strategy_local_only"
        if require_tools and not route.reliable_tool_calling:
            return "tool_calling_not_supported"
        if strategy == "cloud_only" and not route.is_cloud:
            return "strategy_cloud_only"
        if force_secondary and not route.is_cloud:
            return "force_secondary"
        if route.is_cloud and not context_cloud_eligible:
            return "context_cloud_ineligible"
        if self._route_temporarily_blocked(route):
            return "circuit_breaker_open"
        if not self._route_allowed(route, cloud_allowed, response_policy):
            return self._policy_blocked_reason(route, cloud_allowed, response_policy)
        runtime_reason = self._runtime_selection_blocked_reason(route)
        if runtime_reason:
            return runtime_reason
        return ""

    def _policy_blocked_reason(self, route: ProviderRoute, cloud_allowed: bool, response_policy: str | None) -> str:
        if route.is_free_tier:
            return "free_tier_excluded"
        if route.deployment_status == "disabled":
            return "deployment_disabled"
        if not route.configured:
            return route.blocked_reason or "not_configured"
        if route.provider is None:
            return route.blocked_reason or "provider_not_built"
        if not self.route_enabled(route.name):
            if route.deployment_status == "candidate" and route.name not in self._route_overrides:
                return "candidate_not_enabled"
            return "route_disabled"
        if route.is_cloud and not cloud_allowed:
            return "cloud_policy_not_enabled"
        if route.is_cloud and response_policy is not None and response_policy not in route.allowed_response_policies:
            return "not_allowed_for_response_policy"
        return "policy_blocked"

    def _route_score(
        self,
        route: ProviderRoute,
        task: str | None,
        strategy: str | None = None,
        capability_priority: dict[str, int] | None = None,
    ) -> int:
        strategy = self._clean_routing_strategy(strategy or self.routing_strategy)
        # The lane's ranking, when there is one, stands in for the hand-typed
        # routing_priority. A catalog-resolved route is never ordered by a
        # stored human preference integer.
        base = (capability_priority or {}).get(route.name, route.routing_priority)
        score = base + ROUTE_COST_ADJUSTMENTS[strategy].get(route.cost_tier, 0)
        if self._task_matches(task, route.recommended_for):
            score += 1000
        if self._task_matches(task, route.avoid_for):
            score -= 2000
        if route.reliable_tool_calling:
            score += {"ok": 100, "strong": 250}.get(route.tool_calling_quality, 0)
        if self.runtime_manager is not None:
            score += self.runtime_manager.score_adjustment(
                route,
                self._cached_provider_health(route),
                route_enabled=self.route_enabled(route.name),
            )
        # Blast-radius preference bonus: prefer the direct provider over
        # OpenRouter aggregation when the direct-provider budget has headroom.
        # Bonus of 50 * headroom (0-50 pts) tiebreaks same-tier same-task
        # routes and backs off automatically as the direct cap is consumed.
        if route.preferred_provider_id and route.preferred_provider_id == route.provider_budget_id:
            score += int(50 * self.provider_budget_headroom(route.preferred_provider_id))
        return score

    def _task_matches(self, task: str | None, tags: list[str]) -> bool:
        if not task:
            return False
        normalized = task.strip().lower()
        return normalized in {tag.strip().lower() for tag in tags}

    def route_diagnostics(self) -> dict[str, Any]:
        return copy.deepcopy(self._last_route_decision)

    def _publish_route_decision(self, diagnostics: dict[str, Any]) -> None:
        self._last_route_decision = copy.deepcopy(diagnostics)

    async def generation_concurrency_status(self) -> dict[str, Any]:
        async with self._generation_lock:
            return self._generation_concurrency_snapshot_unlocked()

    def _generation_concurrency_snapshot_unlocked(self) -> dict[str, Any]:
        return {
            "policy": self.generation_concurrency_policy.snapshot(),
            "active_total": self._active_generations_total,
            "active_local": self._active_local_generations,
            "active_cloud": self._active_cloud_generations,
        }

    async def _try_start_generation(self, route: ProviderRoute, diagnostics: dict[str, Any]) -> bool:
        async with self._generation_lock:
            if not self.generation_concurrency_policy.can_start(
                route.cost_tier,
                active_total=self._active_generations_total,
                active_local=self._active_local_generations,
                active_cloud=self._active_cloud_generations,
            ):
                self._record_generation_blocked(route, diagnostics)
                return False
            self._active_generations_total += 1
            if route.is_cloud:
                self._active_cloud_generations += 1
            else:
                self._active_local_generations += 1
            diagnostics["concurrency"] = self._generation_concurrency_snapshot_unlocked()
            self._publish_route_decision(diagnostics)
            return True

    async def _finish_generation(self, route: ProviderRoute, diagnostics: dict[str, Any]) -> None:
        async with self._generation_lock:
            self._active_generations_total = max(0, self._active_generations_total - 1)
            if route.is_cloud:
                self._active_cloud_generations = max(0, self._active_cloud_generations - 1)
            else:
                self._active_local_generations = max(0, self._active_local_generations - 1)
            diagnostics["concurrency"] = self._generation_concurrency_snapshot_unlocked()
            self._publish_route_decision(diagnostics)

    @asynccontextmanager
    async def _generation_slot(self, route: ProviderRoute, diagnostics: dict[str, Any]) -> AsyncGenerator[bool, None]:
        started = await self._try_start_generation(route, diagnostics)
        try:
            yield started
        finally:
            if started:
                await self._finish_generation(route, diagnostics)

    def _record_generation_blocked(self, route: ProviderRoute, diagnostics: dict[str, Any]) -> None:
        diagnostics.setdefault("generation_blocked_routes", []).append(route.name)
        for candidate in diagnostics.get("candidates", []):
            if isinstance(candidate, dict) and candidate.get("name") == route.name:
                candidate["blocked_reason"] = "generation_concurrency_limit"
        diagnostics["concurrency"] = self._generation_concurrency_snapshot_unlocked()
        self._publish_route_decision(diagnostics)

    def _record_route_selected(self, route: ProviderRoute, diagnostics: dict[str, Any]) -> None:
        diagnostics["selected_route"] = route.name
        self._publish_route_decision(diagnostics)
        logger.info("Selected LLM route %s (%s/%s)", route.name, route.provider_type, route.model)

    def _route_key(self, route: ProviderRoute) -> str:
        return route.name or f"{route.provider_type}:{route.model}:{route.role}"

    def _cached_provider_health(self, route: ProviderRoute) -> dict[str, Any] | None:
        cached = self._health_cache.get(self._route_key(route))
        if not cached or self._health_cache_ttl_seconds <= 0:
            return None
        if time.monotonic() - cached[0] > self._health_cache_ttl_seconds:
            return None
        return {**cached[1], "cached": True}

    def _runtime_status(self, route: ProviderRoute, health: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.runtime_manager is None:
            return {}
        return self.runtime_manager.status(route, health, route_enabled=self.route_enabled(route.name))

    def _runtime_selection_blocked_reason(self, route: ProviderRoute) -> str:
        if self.runtime_manager is None:
            return ""
        return self.runtime_manager.selection_blocked_reason(
            route,
            self._cached_provider_health(route),
            route_enabled=self.route_enabled(route.name),
        )

    def _route_temporarily_blocked(self, route: ProviderRoute) -> bool:
        key = self._route_key(route)
        if key in self._route_breaker_open:
            return True
        return self._breaker_open and route.provider is self.primary

    async def _local_route_callable(self, route: ProviderRoute) -> bool:
        """Fast health gate for a local route before a full chat attempt.

        Route selection (_ordered_routes) does not probe provider health, so a
        down local endpoint would otherwise be ordered first, selected, and
        block the caller for the provider's full chat timeout (tens of seconds)
        before raising and falling through to cloud. Probing here via the
        short-timeout, TTL-cached _provider_health lets the router skip a
        known-down local route in well under a second and escalate to an
        eligible cloud route immediately. Cloud routes are never gated here;
        on any probe error we fail open so a flaky probe degrades to the prior
        attempt-and-fail behavior instead of dropping the only local route.
        """
        if route.is_cloud or route.provider is None:
            return True
        try:
            health = await self._provider_health(route)
        except Exception:
            logger.warning("Local health gate probe failed for %s; attempting anyway.", route.name)
            return True
        return bool(health.get("model_available"))

    def circuit_breaker_status(self) -> dict[str, Any]:
        return {
            "open": self._breaker_open,
            "error_count": self._error_count,
            "error_threshold": self.error_threshold,
            "routes": {
                self._route_key(route): {
                    "open": self._route_key(route) in self._route_breaker_open,
                    "error_count": self._route_error_count.get(self._route_key(route), 0),
                }
                for route in self._all_routes()
            },
        }

    async def _trip_breaker(self) -> None:
        self._breaker_open = True
        logger.warning(
            "Circuit breaker tripped. Secondary provider is eligible only if policy "
            "and budget allow it."
        )

    async def _reset_breaker_if_healthy(self) -> None:
        if self._breaker_open and await self.primary.is_healthy():
            logger.info("Primary provider recovered. Closing circuit breaker.")
            self._breaker_open = False
            self._error_count = 0
            for route in self.routes:
                if route.provider is self.primary:
                    self._route_breaker_open.discard(self._route_key(route))
                    self._route_error_count[self._route_key(route)] = 0

    async def _record_route_failure(self, route: ProviderRoute, exc: Exception) -> None:
        logger.error("Provider error for %s: %s", route.name, exc)
        self._error_count += 1
        key = self._route_key(route)
        self._route_error_count[key] = self._route_error_count.get(key, 0) + 1
        if self._route_error_count[key] >= self.error_threshold:
            self._route_breaker_open.add(key)
            if route.provider is self.primary:
                await self._trip_breaker()

    def provider_budget_headroom(self, provider_id: str) -> float:
        """Return cached 0.0-1.0 daily cap headroom for provider_id.

        Reads from MultiProviderBudgetGate._headroom_cache populated by
        refresh_headroom_cache() at the start of each _ordered_routes() call.
        Returns 1.0 when no multi gate is configured or provider has no cap.
        """
        from gestaltworkframe.core.cloud_budget import MultiProviderBudgetGate
        if not isinstance(self.cloud_budget, MultiProviderBudgetGate):
            return 1.0
        return self.cloud_budget.headroom(provider_id)

    async def _cloud_spend_allowed(
        self,
        route: ProviderRoute,
        cloud_allowed: bool,
        response_policy: str | None,
        session_id: str | None,
        messages: list[Message],
    ) -> bool:
        if not route.is_cloud or not self._route_allowed(route, cloud_allowed, response_policy):
            return False
        if self.cloud_budget is None:
            logger.warning("Cloud escalation blocked: no budget gate configured.")
            return False

        gate = (
            self.cloud_budget.gate_for(route.provider_budget_id)
            if isinstance(self.cloud_budget, MultiProviderBudgetGate)
            else self.cloud_budget
        )
        decision = await gate.reserve(
            session_id,
            estimated_input_tokens=self._estimate_input_tokens(messages),
            requested_output_tokens=self._cloud_max_tokens(route),
        )
        if not decision.allowed:
            logger.warning("Cloud escalation blocked by budget gate: %s", decision.reason)
            return False
        return True

    async def _cloud_chat(
        self,
        route: ProviderRoute,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        session_id: str | None,
    ) -> Any:
        assert route.provider is not None
        response = await route.provider.chat(
            messages,
            tools,
            max_tokens=self._cloud_max_tokens(route),
        )
        if self.cloud_budget:
            input_tokens, output_tokens = self._extract_usage(response)
            if isinstance(self.cloud_budget, MultiProviderBudgetGate):
                await self.cloud_budget.record_usage(
                    session_id,
                    provider=route.provider.__class__.__name__,
                    model=route.model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    input_price_usd_per_million=route.input_price_usd_per_million or None,
                    output_price_usd_per_million=route.output_price_usd_per_million or None,
                    provider_id=route.provider_budget_id,
                )
            else:
                await self.cloud_budget.record_usage(
                    session_id,
                    provider=route.provider.__class__.__name__,
                    model=route.model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    input_price_usd_per_million=route.input_price_usd_per_million or None,
                    output_price_usd_per_million=route.output_price_usd_per_million or None,
                )
        return response

    def _cloud_max_tokens(self, route: ProviderRoute) -> int:
        if (
            self.cloud_budget
            and self.cloud_budget.config.enabled
            and self.cloud_budget.config.max_output_tokens_per_call > 0
        ):
            return self.cloud_budget.config.max_output_tokens_per_call

        params = getattr(route.provider, "params", None)
        try:
            return int(getattr(params, "max_tokens", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _estimate_input_tokens(self, messages: list[Message]) -> int:
        text = " ".join(str(message.get("content", "")) for message in messages)
        return max(1, math.ceil(len(text) / 4))

    def _extract_usage(self, response: Any) -> tuple[int | None, int | None]:
        usage = getattr(response, "usage", None)
        if usage is None and isinstance(response, dict):
            usage = response.get("usage")
        if usage is None:
            return None, None
        if isinstance(usage, dict):
            return usage.get("input_tokens"), usage.get("output_tokens")
        return getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None)

    def _response_text(self, response: Any) -> str:
        # Empty cloud responses return "" so the orchestrator's grading layer
        # can detect them as unknown-answers and run the fallback ladder.
        # The previous "I don't have a response yet." sentinel was invisible to
        # callers (they recognize UNKNOWN_ANSWER, not that string) and never
        # observed in tests.
        if isinstance(response, dict):
            return response.get("content", "") or ""
        return next((block.text for block in response.content if block.type == "text"), "")

    def _chunk_text(self, chunk: Any) -> str:
        """Extract text from provider stream chunks.

        Supports plain strings, OpenAI-compatible dict deltas from llama.cpp,
        and Anthropic-style event objects with ``delta.text``.
        """
        if isinstance(chunk, str):
            return chunk
        if isinstance(chunk, dict):
            choices = chunk.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                return delta.get("content") or ""
            return chunk.get("content", "") or ""

        delta = getattr(chunk, "delta", None)
        if delta is not None:
            text = getattr(delta, "text", "")
            if text:
                return text

        return getattr(chunk, "text", "") or ""

    async def stream_chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        force_secondary: bool = False,
        cloud_allowed: bool = False,
        response_policy: str | None = None,
        task: str | None = None,
        session_id: str | None = None,
        context_cloud_eligible: bool = True,
    ) -> AsyncGenerator[str, None]:
        """Stream primary provider chunks, with policy-gated non-streamed fallback.

        Secondary fallback is intentionally emitted as one complete text chunk so
        cloud budget can be reserved before the call and usage recorded after it.
        If the primary has already emitted text, stream errors are raised instead
        of falling back to avoid concatenating partial local output with a full
        secondary answer.

        ChatTurnOrchestrator currently drains this generator before yielding to
        SSE. If a future caller streams it directly to clients, disconnect paths
        must explicitly close the generator so the generation slot is released.
        """
        await self._reset_breaker_if_healthy()

        sent_primary_chunk = False
        require_tools = bool(tools)
        from gestaltworkframe.core.cloud_budget import MultiProviderBudgetGate
        if isinstance(self.cloud_budget, MultiProviderBudgetGate):
            try:
                await self.cloud_budget.refresh_headroom_cache()
            except Exception as exc:
                logger.debug("headroom cache refresh failed, using cached values: %s", exc)
        ordered_routes, diagnostics = self._ordered_routes(
            force_secondary,
            cloud_allowed,
            response_policy,
            task,
            context_cloud_eligible,
            require_tools=require_tools,
        )
        for route in ordered_routes:
            if route.provider is None:
                continue
            if route.is_cloud:
                async with self._generation_slot(route, diagnostics) as started:
                    if not started:
                        continue
                    if await self._cloud_spend_allowed(route, cloud_allowed, response_policy, session_id, messages):
                        response = await self._cloud_chat(route, messages, tools, session_id)
                        self._record_route_selected(route, diagnostics)
                        yield self._response_text(response)
                        return
                continue
            if not await self._local_route_callable(route):
                # Local endpoint down/unavailable: skip without paying the
                # provider full chat timeout so the loop escalates to cloud.
                continue
            try:
                async with self._generation_slot(route, diagnostics) as started:
                    if not started:
                        continue
                    # Local streaming providers use their profile params; cloud routes
                    # take the non-streamed _cloud_chat path so spend caps can set max_tokens.
                    async for chunk in route.provider.stream_chat(messages, tools):
                        text = self._chunk_text(chunk)
                        if text:
                            if not sent_primary_chunk:
                                self._record_route_selected(route, diagnostics)
                            sent_primary_chunk = True
                            yield text
                    if not sent_primary_chunk:
                        # An empty successful stream still means the route was selected.
                        self._record_route_selected(route, diagnostics)
                    return
            except Exception as exc:
                await self._record_route_failure(route, exc)
                if sent_primary_chunk:
                    raise
                continue

        if sent_primary_chunk:
            return
        if self._error_count >= self.error_threshold:
            await self._trip_breaker()
        yield (self._SENSITIVE_CONTEXT_LOCAL_REQUIRED if not context_cloud_eligible else self._LOCAL_UNAVAILABLE)["content"]

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        force_secondary: bool = False,
        cloud_allowed: bool = False,
        response_policy: str | None = None,
        task: str | None = None,
        session_id: str | None = None,
        context_cloud_eligible: bool = True,
    ) -> Any:
        await self._reset_breaker_if_healthy()

        require_tools = bool(tools)
        from gestaltworkframe.core.cloud_budget import MultiProviderBudgetGate
        if isinstance(self.cloud_budget, MultiProviderBudgetGate):
            try:
                await self.cloud_budget.refresh_headroom_cache()
            except Exception as exc:
                logger.debug("headroom cache refresh failed, using cached values: %s", exc)
        ordered_routes, diagnostics = self._ordered_routes(
            force_secondary,
            cloud_allowed,
            response_policy,
            task,
            context_cloud_eligible,
            require_tools=require_tools,
        )
        for route in ordered_routes:
            if route.provider is None:
                continue
            if not route.is_cloud and not await self._local_route_callable(route):
                # Local endpoint down/unavailable: skip the full-timeout chat
                # attempt so the loop escalates to an eligible cloud route.
                continue
            try:
                async with self._generation_slot(route, diagnostics) as started:
                    if not started:
                        continue
                    if route.is_cloud:
                        if await self._cloud_spend_allowed(route, cloud_allowed, response_policy, session_id, messages):
                            response = await self._cloud_chat(route, messages, tools, session_id)
                            self._record_route_selected(route, diagnostics)
                            return response
                        continue
                    response = await route.provider.chat(messages, tools)
                    self._record_route_selected(route, diagnostics)
                    return response
            except Exception as exc:
                await self._record_route_failure(route, exc)

        logger.warning("No provider route succeeded. Returning degraded response.")
        return self._SENSITIVE_CONTEXT_LOCAL_REQUIRED if not context_cloud_eligible else self._LOCAL_UNAVAILABLE