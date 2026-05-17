"""Public health endpoints.

`/health` is a liveness probe: returns 200 OK if the FastAPI process is up.
Used by uptime monitors, deploy smoke tests, and load balancers.

`/health/providers` is a redacted readiness probe: reports whether the
configured model routes are callable from this process. Public callers see
endpoint-up / model-available / cloud-fallback-ready signals; budget caps,
runtime endpoints, and route diagnostics stay behind /admin/api/health.

The helpers in this module mirror the admin path's per-route status builder
but drop sensitive fields. Keeping them in their own module makes the
public-vs-admin distinction explicit at the import layer.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from api.services import AppServices, get_app_services


router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/providers")
async def provider_health_check(request: Request) -> dict[str, Any]:
    services = get_app_services(request)
    llm_router = services.llm_router
    cloud_allowed, enabled_cost_tiers, cloud_fallback_reason = await _public_cloud_health_controls(services)
    model_statuses = await llm_router.provider_statuses(
        cloud_allowed=cloud_allowed,
        enabled_cost_tiers=enabled_cost_tiers,
        force_refresh=False,
    )
    local_statuses = [status for status in model_statuses if not _is_cloud_status(status)]
    cloud_statuses = [status for status in model_statuses if _is_cloud_status(status)]
    if not local_statuses:
        local_statuses = [await _provider_status("primary", llm_router.primary)]
    if not cloud_statuses:
        cloud_statuses = [await _provider_status("secondary", llm_router.secondary)]
    primary = _public_provider_group("primary", local_statuses)
    secondary = _public_provider_group("secondary", cloud_statuses)
    local_available = any(bool(status.get("model_available")) for status in local_statuses)
    any_callable = primary["callable"] or secondary["callable"]
    degraded = not any_callable

    return {
        "status": "degraded" if degraded else "ok",
        "local_model_available": local_available,
        "cloud_fallback_configured": secondary["configured"],
        "cloud_fallback_ready": secondary["callable"],
        "cloud_fallback_reason": cloud_fallback_reason,
        "primary": primary,
        "secondary": secondary,
        "models": [primary, secondary],
    }


async def _provider_status(role: str, provider: Any) -> dict[str, Any]:
    if provider is None:
        return {"role": role, "configured": False, "healthy": False}

    try:
        healthy = await provider.is_healthy()
    except Exception:
        healthy = False

    return {
        "role": role,
        "configured": True,
        "healthy": healthy,
        "provider_type": provider.__class__.__name__,
        "model": getattr(provider, "model", ""),
        "profile_name": getattr(provider, "profile_name", ""),
        "provider_role": getattr(provider, "provider_role", role),
        "cost_tier": getattr(provider, "cost_tier", ""),
        "allowed_response_policies": getattr(provider, "allowed_response_policies", []),
    }


def _is_cloud_status(status: dict[str, Any]) -> bool:
    return status.get("cost_tier") in {"low_cost", "premium"}


def _public_provider_group(role: str, statuses: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "role": role,
        "configured": any(bool(status.get("configured")) for status in statuses),
        "healthy": any(bool(status.get("healthy")) for status in statuses),
        "callable": any(bool(status.get("callable")) for status in statuses),
    }


async def _public_cloud_health_controls(services: AppServices) -> tuple[bool, set[str], str]:
    budget = getattr(getattr(services, "cloud_budget", None), "config", None)
    policy = getattr(getattr(services, "orchestrator", None), "cloud_policy", None)
    if budget is None or policy is None:
        return False, set(), "not_configured"
    cloud_allowed = bool(getattr(budget, "enabled", False) and getattr(policy, "max_cloud_calls_per_turn", 0) > 0)
    tiers = set()
    if getattr(policy, "low_cost_enabled", False):
        tiers.add("low_cost")
    if getattr(policy, "claude_enabled", False):
        tiers.add("premium")
    if not cloud_allowed or not tiers:
        return False, tiers, "policy_disabled"
    gate = getattr(services, "cloud_budget", None)
    availability = getattr(gate, "availability", None)
    if callable(availability):
        output_cap = int(getattr(budget, "max_output_tokens_per_call", 0) or 0)
        decision = await availability(requested_output_tokens=output_cap)
        if not decision.allowed:
            return False, tiers, _public_cloud_block_reason(decision.reason)
    return True, tiers, "ready"


def _public_cloud_block_reason(reason: str) -> str:
    if reason.endswith("_zero"):
        return "budget_caps_unset"
    if reason in {"budget_store_unavailable", "budget_accounting_blocked"}:
        return reason
    if reason.endswith("_exhausted"):
        return "budget_exhausted"
    if reason.endswith("_exceeded"):
        return "request_exceeds_budget_caps"
    return "cloud_budget_blocked"
