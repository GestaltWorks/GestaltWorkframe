"""Admin policy, admin health, and handoff packet endpoints.

This module owns the token-gated `/admin/api/health`, `/admin/api/policy`,
and `/admin/api/handoffs` surfaces. Discovery admin endpoints live in
api/admin_discovery.py for the same reason - they're a large enough subsystem
to warrant their own module.

`AdminPolicyPatch` is the public schema for runtime cloud-policy changes:
cloud spillover, low-cost and Claude toggles, the budget caps, and the
per-route override map. Validation rejects unknown routing strategies up
front and unknown route names at apply time.

`_admin_health_payload` is the canonical full-fidelity health snapshot:
model statuses with admin-only fields, cloud budget state, circuit breaker
state, generation concurrency, route diagnostics, chat metrics. The public
/health/providers endpoint in api/health.py reads a redacted subset of the
same router data.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import SQLModel  # noqa: F401  - keep sqlmodel imported alongside select for parity

from gestaltworkframe.api.services import AppServices, enabled_cost_tiers, get_app_services, require_admin_token
from gestaltworkframe.core.key_store import _PROVIDER_ENV_VARS as _KEY_STORE_PROVIDER_ENV_VARS
from gestaltworkframe.core.key_validation_monitor import KeyValidationMonitor
from gestaltworkframe.core.rate_limiter import get_key_store_rate_limiter
from gestaltworkframe.core.provider_balance import BalanceSnapshot, local_tracking_balance
from gestaltworkframe.core.db import ContactRecord, TerminalIntakeRecord, async_session_maker
from gestaltworkframe.core.handoff_packets import (
    build_contact_handoff_packet,
    build_terminal_intake_handoff_packet,
    packet_to_dict,
)
from gestaltworkframe.core.retention import RetentionPolicy, sweep as retention_sweep
from gestaltworkframe.core.router import ROUTING_STRATEGIES


ADMIN_HANDOFF_LIMIT = 12
DEFAULT_CLOUD_INPUT_TOKEN_CAP = 8000
DEFAULT_CLOUD_OUTPUT_TOKEN_CAP = 2048


class AdminPolicyPatch(BaseModel):
    routing_strategy: str | None = None
    cloud_spillover_enabled: bool | None = None
    low_cost_enabled: bool | None = None
    claude_enabled: bool | None = None
    max_calls_per_turn: int | None = None
    max_calls_per_session: int | None = None
    max_calls_per_day: int | None = None
    max_calls_per_month: int | None = None
    max_daily_usd: float | None = None
    max_monthly_usd: float | None = None
    max_input_tokens_per_call: int | None = None
    max_output_tokens_per_call: int | None = None
    routes: dict[str, bool] | None = None
    provider_budgets: dict[str, dict[str, float]] | None = None

    @field_validator("routing_strategy")
    @classmethod
    def validate_routing_strategy(cls, value: str | None) -> str | None:
        if value is not None and value not in ROUTING_STRATEGIES:
            raise ValueError(f"Unknown routing_strategy: {value}")
        return value


class ProviderKeyPatch(BaseModel):
    key: str = Field(..., min_length=1, max_length=500)


class ProviderKeyTestResult(BaseModel):
    valid: bool
    error: str = ""


# Global validation monitor instance (initialized with default DB path)
_validation_monitor: KeyValidationMonitor | None = None


def _get_validation_monitor(services: AppServices) -> KeyValidationMonitor:
    """Get or create the validation monitor with the correct DB path."""
    global _validation_monitor
    if _validation_monitor is None:
        _db_path = "database.db"
        if getattr(services, 'key_store', None) is not None:
            _db_path = getattr(services.key_store, "_path", _db_path)
        _validation_monitor = KeyValidationMonitor(_db_path)
    return _validation_monitor


def _get_client_ip(request: Request) -> str:
    """Extract client IP from request, considering X-Forwarded-For."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _check_key_store_rate_limit(request: Request) -> None:
    """Enforce rate limiting on key store operations."""
    limiter = get_key_store_rate_limiter()
    client_ip = _get_client_ip(request)
    allowed, meta = await limiter.is_allowed(f"keystore:{client_ip}")
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded. Retry after {meta.get('retry_after', 1):.0f} seconds.",
        )


async def _test_provider_key(provider_id: str, api_key: str, services: AppServices | None = None) -> ProviderKeyTestResult:
    """Make a minimal live test call to verify the key is accepted.

    Uses cheapest read-only endpoints: /v1/models for Anthropic and OpenAI,
    the Gemini models list for Google. No inference tokens consumed.
    Records validation attempts for monitoring.
    """
    import httpx

    monitor = _get_validation_monitor(services) if services else KeyValidationMonitor()

    async def record(success: bool, error: str = "") -> None:
        failure_type = ""
        if not success:
            failure_type = error.split(":")[0] if ":" in error else error
        await monitor.record_attempt(provider_id, success, failure_type or None, error)

    if provider_id == "openrouter":
        from gestaltworkframe.core.provider_balance import OpenRouterBalanceChecker
        checker = OpenRouterBalanceChecker(api_key)
        snap = await checker.get()
        result = ProviderKeyTestResult(valid=snap.available, error=snap.error)
        await record(result.valid, result.error)
        return result

    if provider_id == "anthropic":
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://api.anthropic.com/v1/models",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                    },
                )
            if resp.status_code == 200:
                await record(True)
                return ProviderKeyTestResult(valid=True)
            if resp.status_code == 401:
                await record(False, "invalid_api_key")
                return ProviderKeyTestResult(valid=False, error="invalid_api_key")
            error = f"unexpected_status_{resp.status_code}"
            await record(False, error)
            return ProviderKeyTestResult(valid=False, error=error)
        except Exception as exc:
            error = f"request_error: {exc}"
            await record(False, error)
            return ProviderKeyTestResult(valid=False, error=error)

    if provider_id == "openai":
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            if resp.status_code == 200:
                await record(True)
                return ProviderKeyTestResult(valid=True)
            if resp.status_code == 401:
                await record(False, "invalid_api_key")
                return ProviderKeyTestResult(valid=False, error="invalid_api_key")
            error = f"unexpected_status_{resp.status_code}"
            await record(False, error)
            return ProviderKeyTestResult(valid=False, error=error)
        except Exception as exc:
            error = f"request_error: {exc}"
            await record(False, error)
            return ProviderKeyTestResult(valid=False, error=error)

    if provider_id == "google":
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    params={"key": api_key},
                )
            if resp.status_code == 200:
                await record(True)
                return ProviderKeyTestResult(valid=True)
            if resp.status_code in (400, 403):
                await record(False, "invalid_api_key")
                return ProviderKeyTestResult(valid=False, error="invalid_api_key")
            error = f"unexpected_status_{resp.status_code}"
            await record(False, error)
            return ProviderKeyTestResult(valid=False, error=error)
        except Exception as exc:
            error = f"request_error: {exc}"
            await record(False, error)
            return ProviderKeyTestResult(valid=False, error=error)

    # Unknown provider: presence check only.
    valid = bool(api_key)
    await record(valid, "" if valid else "empty_key")
    return ProviderKeyTestResult(valid=valid, error="" if valid else "empty_key")


async def _admin_health_payload(services: AppServices, force_refresh: bool = False) -> dict[str, Any]:
    policy = services.orchestrator.cloud_policy
    budget = services.cloud_budget.config
    cloud_allowed = budget.enabled and policy.max_cloud_calls_per_turn > 0
    model_statuses = await services.llm_router.provider_statuses(
        cloud_allowed=cloud_allowed,
        enabled_cost_tiers=enabled_cost_tiers(services),
        force_refresh=force_refresh,
        check_disabled_local_routes=True,
    )
    return {
        "status": "ok" if any(status["callable"] for status in model_statuses) else "degraded",
        "models": model_statuses,
        "policy": {
            "routing_strategy": services.llm_router.routing_strategy,
            "cloud_spillover_enabled": budget.enabled,
            "low_cost_enabled": policy.low_cost_enabled,
            "claude_enabled": policy.claude_enabled,
            "max_calls_per_turn": policy.max_cloud_calls_per_turn,
            "max_calls_per_session": policy.max_cloud_calls_per_session,
            "max_calls_per_day": budget.max_calls_per_day,
            "max_calls_per_month": budget.max_calls_per_month,
            "max_daily_usd": budget.max_daily_usd,
            "max_monthly_usd": budget.max_monthly_usd,
            "max_input_tokens_per_call": budget.max_input_tokens_per_call,
            "max_output_tokens_per_call": budget.max_output_tokens_per_call,
            "route_overrides": services.llm_router.route_overrides(),
        },
        "cloud_budget": await _cloud_budget_with_balance(services),
        "circuit_breaker": services.llm_router.circuit_breaker_status(),
        "generation_concurrency": await services.llm_router.generation_concurrency_status(),
        "route_diagnostics": services.llm_router.route_diagnostics(),
        "metrics": {"chat": await services.chat_metrics.snapshot()},
        "key_validation": _get_validation_monitor(services).get_config(),
    }


async def _cloud_budget_with_balance(services: AppServices) -> dict[str, Any]:
    snap = await services.cloud_budget.snapshot()
    # Attach live / estimated balance data to each provider bucket.
    providers = snap.get("providers")
    if not providers:
        return snap
    from gestaltworkframe.core.cloud_budget import MultiProviderBudgetGate
    if not isinstance(services.cloud_budget, MultiProviderBudgetGate):
        return snap
    for pid, entry in providers.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("enabled"):
            balance = await _get_provider_balance(services, pid)
            entry["balance"] = balance.to_dict() if isinstance(balance, BalanceSnapshot) else balance
    return snap


async def _get_provider_balance(services: AppServices, provider_id: str) -> BalanceSnapshot | dict[str, Any]:
    """Return live or estimated balance for a provider."""
    if provider_id == "openrouter" and services.balance_checker is not None:
        return await services.balance_checker.get()
    # For other providers, compute estimated remaining from budget - usage.
    from gestaltworkframe.core.cloud_budget import MultiProviderBudgetGate
    if isinstance(services.cloud_budget, MultiProviderBudgetGate):
        gate = services.cloud_budget.provider_gates.get(provider_id)
        if gate is not None:
            snap = await gate.snapshot()
            used_day = snap.get("used", {}).get("day_usd", 0.0)
            used_month = snap.get("used", {}).get("month_usd", 0.0)
            max_daily = snap.get("limits", {}).get("max_daily_usd", 0.0)
            max_monthly = snap.get("limits", {}).get("max_monthly_usd", 0.0)
            return local_tracking_balance(provider_id, max_daily, max_monthly, used_day, used_month)
    return {"error": "no_balance_data"}


async def _get_handoff_record(db: AsyncSession, record_type: str, uid: Any) -> Any | None:
    """Fetch a contact or terminal-intake record by id for handoff endpoints."""
    model = ContactRecord if record_type == "contact" else TerminalIntakeRecord
    result = await db.execute(select(model).where(model.id == str(uid)))
    return result.scalar_one_or_none()


router = APIRouter(prefix="/admin/api", tags=["admin"])


@router.get("/health")
async def admin_health(
    request: Request,
    force_refresh: bool = False,
    _: None = Depends(require_admin_token),
) -> dict[str, Any]:
    services = get_app_services(request)
    return await _admin_health_payload(services, force_refresh=force_refresh)


@router.get("/policy")
async def admin_policy(
    request: Request,
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    services = get_app_services(request)
    budget = services.cloud_budget.config
    return {
        "routing_strategy": services.llm_router.routing_strategy,
        "cloud_spillover_enabled": budget.enabled,
        "cloud_spillover": {
            "max_calls_per_turn": budget.max_calls_per_turn,
            "max_calls_per_session": budget.max_calls_per_session,
            "max_calls_per_day": budget.max_calls_per_day,
            "max_calls_per_month": budget.max_calls_per_month,
            "max_daily_usd": budget.max_daily_usd,
            "max_monthly_usd": budget.max_monthly_usd,
            "max_input_tokens_per_call": budget.max_input_tokens_per_call,
            "max_output_tokens_per_call": budget.max_output_tokens_per_call,
        },
        "route_overrides": services.llm_router.route_overrides(),
    }


@router.patch("/policy")
async def admin_policy_patch(
    request: Request,
    patch: AdminPolicyPatch,
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    services = get_app_services(request)
    budget = services.cloud_budget.config

    if patch.routing_strategy is not None:
        services.llm_router.routing_strategy = patch.routing_strategy

    if patch.cloud_spillover_enabled is not None:
        budget.enabled = patch.cloud_spillover_enabled

    if patch.max_calls_per_turn is not None:
        budget.max_calls_per_turn = patch.max_calls_per_turn
    if patch.max_calls_per_session is not None:
        budget.max_calls_per_session = patch.max_calls_per_session
    if patch.max_calls_per_day is not None:
        budget.max_calls_per_day = patch.max_calls_per_day
    if patch.max_calls_per_month is not None:
        budget.max_calls_per_month = patch.max_calls_per_month
    if patch.max_daily_usd is not None:
        budget.max_daily_usd = patch.max_daily_usd
    if patch.max_monthly_usd is not None:
        budget.max_monthly_usd = patch.max_monthly_usd
    if patch.max_input_tokens_per_call is not None:
        budget.max_input_tokens_per_call = patch.max_input_tokens_per_call
    if patch.max_output_tokens_per_call is not None:
        budget.max_output_tokens_per_call = patch.max_output_tokens_per_call

    if patch.routes is not None:
        for route_name, enabled in patch.routes.items():
            services.llm_router.set_route_override(route_name, enabled)

    if patch.provider_budgets is not None:
        from gestaltworkframe.core.cloud_budget import MultiProviderBudgetGate
        if isinstance(services.cloud_budget, MultiProviderBudgetGate):
            for pid, limits in patch.provider_budgets.items():
                await services.cloud_budget.update_provider_budget(
                    pid,
                    max_daily_usd=limits.get("max_daily_usd"),
                    max_monthly_usd=limits.get("max_monthly_usd"),
                )

    return {
        "applied": patch.model_dump(exclude_unset=True),
        "policy": {
            "routing_strategy": services.llm_router.routing_strategy,
            "cloud_spillover_enabled": budget.enabled,
            "route_overrides": services.llm_router.route_overrides(),
        },
        "cloud_budget": await services.cloud_budget.snapshot(),
    }


@router.get("/handoffs")
async def admin_handoffs(
    request: Request,
    limit: int = 20,
    offset: int = 0,
    contact_only: bool = False,
    _: None = Depends(require_admin_token),
) -> dict[str, Any]:
    get_app_services(request)  # 503 guard: ensure services are initialized
    async with async_session_maker() as db:
        total_contact = (await db.execute(select(ContactRecord).order_by(ContactRecord.created_at.desc()))).scalars().all()
        total_terminal = (await db.execute(select(TerminalIntakeRecord).order_by(TerminalIntakeRecord.created_at.desc()))).scalars().all()
    all_records: list[dict] = []
    if not contact_only:
        for t in total_terminal:
            all_records.append({"type": "terminal", "created_at": t.created_at, "id": str(t.id), "record": t.to_dict()})
    for c in total_contact:
        all_records.append({"type": "contact", "created_at": c.created_at, "id": str(c.id), "record": c.to_dict()})
    all_records.sort(key=lambda x: x["created_at"], reverse=True)
    total = len(all_records)
    sliced = all_records[offset : offset + limit]
    packets: list[dict] = []
    for item in sliced:
        record = item["record"]
        if item["type"] == "contact":
            packet = build_contact_handoff_packet(record)
        else:
            packet = build_terminal_intake_handoff_packet(record)
        packets.append(packet_to_dict(packet))
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "contact_only": contact_only,
        "packets": packets,
    }


@router.post("/handoffs/{record_type}/{record_id}/approve")
async def admin_handoff_approve(
    record_type: str,
    record_id: str,
    request: Request,
    _: None = Depends(require_admin_token),
) -> dict[str, Any]:
    get_app_services(request)  # 503 guard: ensure services are initialized
    from uuid import UUID
    try:
        uid = UUID(record_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid record_id: {exc}")
    if record_type not in ("contact", "terminal"):
        raise HTTPException(status_code=400, detail=f"Unknown record_type: {record_type}")
    async with async_session_maker() as db:
        record = await _get_handoff_record(db, record_type, uid)
        if record is None:
            raise HTTPException(status_code=404, detail=f"{record_type} record not found")
        if getattr(record, "admin_approved", False):
            return {"message": "Already approved", "id": record_id}
        record.admin_approved = True
        db.add(record)
        await db.commit()
    return {"message": "Approved", "id": record_id}


@router.post("/provider-keys/{provider_id}")
async def set_provider_key(
    provider_id: str,
    body: ProviderKeyPatch,
    request: Request,
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    """Store a provider API key in the encrypted key store and rotate live instances."""
    # Validate provider_id is known
    if provider_id not in _KEY_STORE_PROVIDER_ENV_VARS:
        raise HTTPException(status_code=400, detail={"code": "unknown_provider", "message": f"Unknown provider: {provider_id}"})
    await _check_key_store_rate_limit(request)
    services = get_app_services(request)
    admin_token = request.headers.get("x-admin-token", "").strip()
    if not admin_token:
        raise HTTPException(status_code=401, detail="Missing admin token")
    if services.key_store is None:
        raise HTTPException(status_code=503, detail="Key store not available")
    ok = await services.key_store.set_key(provider_id, body.key, admin_token)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to store key")
    # Invalidate cached balance so the next health poll sees fresh data.
    if provider_id == "openrouter" and services.balance_checker is not None:
        services.balance_checker.invalidate()
    # Push the new key to any live provider instances so they take effect immediately.
    await services.llm_router.rotate_provider_key(provider_id, body.key)
    test_result = await _test_provider_key(provider_id, body.key, services)
    has = await services.key_store.has_key(provider_id)
    return {"provider_id": provider_id, "stored": has, "test": test_result.model_dump()}


@router.delete("/provider-keys/{provider_id}")
async def delete_provider_key(
    provider_id: str,
    request: Request,
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    """Remove the stored key for a provider. Env-var fallback resumes."""
    await _check_key_store_rate_limit(request)
    services = get_app_services(request)
    if services.key_store is None:
        raise HTTPException(status_code=503, detail="Key store not available")
    deleted = await services.key_store.delete_key(provider_id)
    env_key = services.key_store.env_fallback(provider_id)
    env_fallback = bool(env_key)
    if provider_id == "openrouter" and services.balance_checker is not None:
        services.balance_checker.invalidate()
    # Rotate live providers to the env fallback key (or empty string to deactivate).
    if env_key:
        await services.llm_router.rotate_provider_key(provider_id, env_key)
    return {"provider_id": provider_id, "deleted": deleted, "env_fallback_available": env_fallback}


@router.post("/provider-keys/{provider_id}/test")
async def test_provider_key(
    provider_id: str,
    request: Request,
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    """Test the currently active key for a provider (stored or env fallback)."""
    await _check_key_store_rate_limit(request)
    services = get_app_services(request)
    admin_token = request.headers.get("x-admin-token", "").strip()
    key = ""
    if services.key_store is not None:
        key = await services.key_store.get_key(provider_id, admin_token) or ""
    if not key:
        key = services.key_store.env_fallback(provider_id) if services.key_store else ""
    if not key:
        return {"provider_id": provider_id, "valid": False, "error": "no_key_configured"}
    result = await _test_provider_key(provider_id, key, services)
    return {"provider_id": provider_id, **result.model_dump()}


@router.get("/provider-keys")
async def list_provider_keys(
    request: Request,
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    """List all providers with masked key presence status."""
    services = get_app_services(request)
    statuses: dict[str, object] = {}
    for pid in sorted(_KEY_STORE_PROVIDER_ENV_VARS):
        has_stored = await services.key_store.has_key(pid) if services.key_store else False
        has_env = bool(services.key_store.env_fallback(pid)) if services.key_store else False
        statuses[pid] = {
            "has_stored_key": has_stored,
            "has_env_key": has_env,
            "active_source": "store" if has_stored else ("env" if has_env else "none"),
        }
    monitor = _get_validation_monitor(services)
    return {"providers": statuses, "validation_monitor": monitor.get_config()}


@router.get("/provider-keys/validation-stats")
async def get_validation_stats(
    request: Request,
    provider_id: str | None = None,
    hours: int = 24,
    _: None = Depends(require_admin_token),
) -> dict[str, Any]:
    """Get key validation statistics."""
    services = get_app_services(request)
    monitor = _get_validation_monitor(services)
    stats = await monitor.get_stats(provider_id, hours)
    return stats


@router.post("/cloud-budget/clear-accounting-block")
async def admin_clear_accounting_block(
    request: Request,
    _: None = Depends(require_admin_token),
) -> dict[str, Any]:
    """Clear a stuck cloud_budget accounting_blocked flag.

    Accounting blocks are raised by record_usage when a provider returns
    no token-usage metadata or invalid values. While blocked, every
    cloud call is denied. This endpoint is the operator recovery path:
    it clears the flag and the last_accounting_error string, then
    returns the updated snapshot so the admin UI can confirm.
    """
    services = get_app_services(request)
    decision = await services.cloud_budget.clear_accounting_block()
    snapshot = await services.cloud_budget.snapshot()
    return {"decision": decision.model_dump(), "cloud_budget": snapshot}


@router.post("/retention/sweep")
async def admin_retention_sweep(
    request: Request,
    dry_run: bool = True,
    _: None = Depends(require_admin_token),
) -> dict[str, Any]:
    """Trigger a manual retention sweep.

    dry_run=True reports what would be deleted without deleting.
    dry_run=False actually deletes records beyond the retention window.
    """
    get_app_services(request)  # 503 guard: ensure services are initialized
    policy = RetentionPolicy.from_env()
    result = await retention_sweep(policy, dry_run=dry_run)
    return {
        "dry_run": dry_run,
        "policy": policy.model_dump(),
        "result": result,
    }


@router.get("/handoff-packet/{record_type}/{record_id}")
async def admin_handoff_packet(
    record_type: str,
    record_id: str,
    request: Request,
    _: None = Depends(require_admin_token),
) -> dict[str, Any]:
    """Return a single handoff packet for a contact or terminal record.

    This is the canonical representation used for human review and downstream
    pipeline integration. It includes redaction of PII and structure suitable
    for email or ticketing systems.
    """
    from uuid import UUID
    try:
        uid = UUID(record_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid record_id: {exc}")
    if record_type not in ("contact", "terminal"):
        raise HTTPException(status_code=400, detail=f"Unknown record_type: {record_type}")
    async with async_session_maker() as db:
        record = await _get_handoff_record(db, record_type, uid)
        if record is None:
            raise HTTPException(status_code=404, detail=f"{record_type} record not found")
        if record_type == "contact":
            packet = build_contact_handoff_packet(record)
        else:
            packet = build_terminal_intake_handoff_packet(record)
    return packet_to_dict(packet)


@router.get("/router-diagnostics")
async def admin_router_diagnostics(
    request: Request,
    _: None = Depends(require_admin_token),
) -> dict[str, Any]:
    """Return detailed router diagnostics for troubleshooting routing decisions."""
    services = get_app_services(request)
    return {
        "routing_strategy": services.llm_router.routing_strategy,
        "route_overrides": services.llm_router.route_overrides(),
        "circuit_breaker": services.llm_router.circuit_breaker_status(),
        "generation_concurrency": await services.llm_router.generation_concurrency_status(),
        "provider_routes": services.llm_router.route_diagnostics(),
    }


# Export alias for backward compatibility
admin_health_check = admin_health
admin_handoff_packets = admin_handoffs
update_admin_policy = admin_policy_patch


# Backward compatibility stubs for internal helpers
async def _admin_packet_payload(record_type, record_id, services):
    return {}


def _safe_json_dict(obj):
    if hasattr(obj, 'model_dump'):
        return obj.model_dump()
    if hasattr(obj, '__dict__'):
        return obj.__dict__
    return {}


async def _recent_handoff_packets(session, limit=10):
    """Return recent handoff packets from contact form and terminal intake records."""
    packets = []

    # Query recent contact records
    from sqlalchemy import select, desc
    result = await session.execute(
        select(ContactRecord).order_by(desc(ContactRecord.created_at)).limit(limit)
    )
    contact_records = result.scalars().all()

    for record in contact_records:
        try:
            fields = json.loads(record.data) if record.data else {}
        except json.JSONDecodeError:
            fields = {}
        packet = build_contact_handoff_packet(
            role=record.role,
            name=record.name,
            email=record.email,
            fields=fields,
        )
        packets.append(packet_to_dict(packet))

    # Query recent terminal intake records
    result = await session.execute(
        select(TerminalIntakeRecord).order_by(desc(TerminalIntakeRecord.created_at)).limit(limit)
    )
    intake_records = result.scalars().all()

    for record in intake_records:
        try:
            intake_data = json.loads(record.data) if record.data else {}
        except json.JSONDecodeError:
            intake_data = {}
        packet = build_terminal_intake_handoff_packet(
            selected_mode=record.selected_mode,
            intake=intake_data,
            contact={"name": record.objective, "email": ""} if record.objective else None,
        )
        packets.append(packet_to_dict(packet))

    return packets


async def _apply_admin_policy(services, patch):
    """Apply admin policy patch to the services.

    Handles cloud_spillover_enabled, routing_strategy, route toggles,
    cloud policy settings, and provider_budgets updates.
    """
    results = {"applied": True, "provider_budgets_updated": []}

    # Handle cloud spillover enablement (direct or via tier enablement)
    spillover_enabled = patch.cloud_spillover_enabled
    if spillover_enabled is None and patch.low_cost_enabled is True:
        spillover_enabled = True

    if spillover_enabled is not None and services.cloud_budget:
        services.cloud_budget.config.enabled = spillover_enabled
        # Initialize the budget if enabling
        if spillover_enabled and hasattr(services.cloud_budget, 'init'):
            await services.cloud_budget.init()
        results["cloud_spillover_enabled"] = spillover_enabled

        # Disable cloud tiers when spillover is disabled
        if not spillover_enabled and services.orchestrator and hasattr(services.orchestrator, 'cloud_policy'):
            policy = services.orchestrator.cloud_policy
            if hasattr(policy, 'low_cost_enabled'):
                policy.low_cost_enabled = False
            if hasattr(policy, 'claude_enabled'):
                policy.claude_enabled = False

    # Handle routing strategy
    if patch.routing_strategy is not None and services.llm_router:
        if hasattr(services.llm_router, 'set_routing_strategy'):
            services.llm_router.set_routing_strategy(patch.routing_strategy)
        results["routing_strategy"] = patch.routing_strategy

    # Handle route enablement toggles
    if patch.routes is not None and services.llm_router:
        known_route_names = set()
        if hasattr(services.llm_router, 'routes') and services.llm_router.routes:
            known_route_names = {getattr(r, 'name', None) for r in services.llm_router.routes}
        # Collect all unknown routes first
        unknown_routes = [route_name for route_name in patch.routes.keys() if route_name not in known_route_names]
        if unknown_routes:
            raise HTTPException(
                status_code=400,
                detail={"code": "unknown_route_names", "routes": unknown_routes}
            )
        for route_name, enabled in patch.routes.items():
            if hasattr(services.llm_router, 'set_route_enabled'):
                services.llm_router.set_route_enabled(route_name, enabled)
        results["routes_updated"] = list(patch.routes.keys())

    # Handle cloud policy settings
    if services.orchestrator and hasattr(services.orchestrator, 'cloud_policy'):
        policy = services.orchestrator.cloud_policy
        # Check if spillover is explicitly disabled (wins over tier enable)
        spillover_explicitly_disabled = patch.cloud_spillover_enabled is False
        if patch.claude_enabled is not None:
            # Don't enable tier if spillover is explicitly disabled
            if not (spillover_explicitly_disabled and patch.claude_enabled):
                policy.claude_enabled = patch.claude_enabled
                results["claude_enabled"] = patch.claude_enabled
        if patch.low_cost_enabled is not None:
            # Don't enable tier if spillover is explicitly disabled
            if not (spillover_explicitly_disabled and patch.low_cost_enabled):
                policy.low_cost_enabled = patch.low_cost_enabled
                results["low_cost_enabled"] = patch.low_cost_enabled
        if patch.max_calls_per_turn is not None:
            policy.max_cloud_calls_per_turn = patch.max_calls_per_turn
            results["max_calls_per_turn"] = patch.max_calls_per_turn
        if patch.max_calls_per_session is not None:
            policy.max_cloud_calls_per_session = patch.max_calls_per_session
            results["max_calls_per_session"] = patch.max_calls_per_session

    # Handle budget config settings
    if services.cloud_budget and hasattr(services.cloud_budget, 'config'):
        config = services.cloud_budget.config
        if patch.max_calls_per_day is not None:
            config.max_calls_per_day = patch.max_calls_per_day
        if patch.max_calls_per_month is not None:
            config.max_calls_per_month = patch.max_calls_per_month
        if patch.max_daily_usd is not None:
            config.max_daily_usd = patch.max_daily_usd
        if patch.max_monthly_usd is not None:
            config.max_monthly_usd = patch.max_monthly_usd
        # Set default token caps if cloud is enabled and caps are zero
        if patch.cloud_spillover_enabled and hasattr(config, 'max_input_tokens_per_call'):
            if config.max_input_tokens_per_call == 0:
                config.max_input_tokens_per_call = DEFAULT_CLOUD_INPUT_TOKEN_CAP
            if config.max_output_tokens_per_call == 0:
                config.max_output_tokens_per_call = DEFAULT_CLOUD_OUTPUT_TOKEN_CAP

    # Handle provider_budgets updates
    if patch.provider_budgets and services.cloud_budget:
        for provider_id, budget_changes in patch.provider_budgets.items():
            max_daily = budget_changes.get("max_daily_usd")
            max_monthly = budget_changes.get("max_monthly_usd")

            # Validate: reject both zero (would disable the provider budget entirely)
            if max_daily is not None and max_monthly is not None and max_daily == 0 and max_monthly == 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot set both caps to zero for {provider_id}"
                )

            await services.cloud_budget.update_provider_budget(
                provider_id=provider_id,
                max_daily_usd=max_daily,
                max_monthly_usd=max_monthly,
            )
            results["provider_budgets_updated"].append(provider_id)

    return results
