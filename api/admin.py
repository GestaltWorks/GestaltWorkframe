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
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import SQLModel  # noqa: F401  - keep sqlmodel imported alongside select for parity

from api.services import AppServices, enabled_cost_tiers, get_app_services, require_admin_token
from core.db import ContactRecord, TerminalIntakeRecord, async_session_maker, get_session
from core.handoff_packets import (
    build_contact_handoff_packet,
    build_terminal_intake_handoff_packet,
    packet_to_dict,
)
from core.retention import RetentionPolicy, sweep as retention_sweep
from core.router import ROUTING_STRATEGIES


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

    @field_validator("routing_strategy")
    @classmethod
    def validate_routing_strategy(cls, value: str | None) -> str | None:
        if value is not None and value not in ROUTING_STRATEGIES:
            raise ValueError(f"Unknown routing_strategy: {value}")
        return value


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
        "cloud_budget": await services.cloud_budget.snapshot(),
        "circuit_breaker": services.llm_router.circuit_breaker_status(),
        "generation_concurrency": await services.llm_router.generation_concurrency_status(),
        "route_diagnostics": services.llm_router.route_diagnostics(),
        "metrics": {"chat": await services.chat_metrics.snapshot()},
    }


def _safe_json_dict(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _admin_packet_payload(packet, *, record_id: str, created_at: datetime) -> dict[str, Any]:
    payload = packet_to_dict(packet)
    payload["record_id"] = record_id
    payload["created_at"] = created_at.isoformat()
    return payload


async def _recent_handoff_packets(session: AsyncSession, limit: int = ADMIN_HANDOFF_LIMIT) -> list[dict[str, Any]]:
    capped_limit = min(max(limit, 1), 50)
    contact_result = await session.execute(
        select(ContactRecord).order_by(ContactRecord.created_at.desc()).limit(capped_limit)
    )
    intake_result = await session.execute(
        select(TerminalIntakeRecord).order_by(TerminalIntakeRecord.updated_at.desc()).limit(capped_limit)
    )
    packets: list[dict[str, Any]] = []
    for record in contact_result.scalars().all():
        packets.append(_admin_packet_payload(
            build_contact_handoff_packet(record.role, record.name, record.email, _safe_json_dict(record.data)),
            record_id=record.id,
            created_at=record.updated_at or record.created_at,
        ))
    for record in intake_result.scalars().all():
        packets.append(_admin_packet_payload(
            build_terminal_intake_handoff_packet(record.selected_mode, _safe_json_dict(record.data)),
            record_id=record.id,
            created_at=record.updated_at or record.created_at,
        ))
    return sorted(packets, key=lambda item: item["created_at"], reverse=True)[:capped_limit]


async def _apply_admin_policy(services: AppServices, patch: AdminPolicyPatch) -> None:
    budget = services.cloud_budget.config
    policy = services.orchestrator.cloud_policy
    if patch.routing_strategy is not None:
        services.llm_router.set_routing_strategy(patch.routing_strategy)
    if patch.cloud_spillover_enabled is not None:
        budget.enabled = patch.cloud_spillover_enabled
    elif patch.low_cost_enabled is True or patch.claude_enabled is True:
        budget.enabled = True
    if patch.max_calls_per_turn is not None:
        budget.max_calls_per_turn = max(patch.max_calls_per_turn, 0)
    if patch.max_calls_per_session is not None:
        budget.max_calls_per_session = max(patch.max_calls_per_session, 0)
    if patch.max_calls_per_day is not None:
        budget.max_calls_per_day = max(patch.max_calls_per_day, 0)
    if patch.max_calls_per_month is not None:
        budget.max_calls_per_month = max(patch.max_calls_per_month, 0)
    if patch.max_daily_usd is not None:
        budget.max_daily_usd = max(patch.max_daily_usd, 0.0)
    if patch.max_monthly_usd is not None:
        budget.max_monthly_usd = max(patch.max_monthly_usd, 0.0)
    if patch.max_input_tokens_per_call is not None:
        budget.max_input_tokens_per_call = max(patch.max_input_tokens_per_call, 0)
    if patch.max_output_tokens_per_call is not None:
        budget.max_output_tokens_per_call = max(patch.max_output_tokens_per_call, 0)
    if patch.low_cost_enabled is not None:
        policy.low_cost_enabled = patch.low_cost_enabled and budget.enabled
    if patch.claude_enabled is not None:
        policy.claude_enabled = patch.claude_enabled and budget.enabled
    if not budget.enabled:
        policy.low_cost_enabled = False
        policy.claude_enabled = False
    elif patch.max_input_tokens_per_call is None and budget.max_input_tokens_per_call < 1:
        budget.max_input_tokens_per_call = DEFAULT_CLOUD_INPUT_TOKEN_CAP
    if budget.enabled and patch.max_output_tokens_per_call is None and budget.max_output_tokens_per_call < 1:
        budget.max_output_tokens_per_call = DEFAULT_CLOUD_OUTPUT_TOKEN_CAP
    policy.max_cloud_calls_per_turn = budget.max_calls_per_turn
    policy.max_cloud_calls_per_session = budget.max_calls_per_session
    if patch.routes:
        known = {route.name for route in services.llm_router.routes}
        unknown = [name for name in patch.routes if name not in known]
        if unknown:
            # The previous behavior silently dropped typo'd route names so an
            # admin saw a 200 OK but the toggle never took effect. Surface the
            # bad input instead.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "unknown_route_names",
                    "routes": sorted(unknown),
                    "known": sorted(known),
                },
            )
        for name, enabled in patch.routes.items():
            services.llm_router.set_route_enabled(name, enabled)
    if budget.enabled:
        await services.cloud_budget.init()


router = APIRouter(prefix="/admin/api", tags=["admin"])


@router.get("/health")
async def admin_health_check(request: Request, refresh: bool = False, _: None = Depends(require_admin_token)):
    return await _admin_health_payload(get_app_services(request), force_refresh=refresh)


@router.get("/handoffs")
async def admin_handoff_packets(
    limit: int = ADMIN_HANDOFF_LIMIT,
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    return {"packets": await _recent_handoff_packets(session, limit=limit)}


@router.patch("/policy")
async def update_admin_policy(
    patch: AdminPolicyPatch,
    request: Request,
    _: None = Depends(require_admin_token),
):
    services = get_app_services(request)
    await _apply_admin_policy(services, patch)
    return await _admin_health_payload(services, force_refresh=False)


@router.get("/retention/preview")
async def admin_retention_preview(_: None = Depends(require_admin_token)) -> dict[str, Any]:
    """Dry-run a retention sweep: report what would be deleted without writing."""
    policy = RetentionPolicy.from_env()
    summary = await retention_sweep(policy, async_session_maker, dry_run=True)
    return summary.to_dict()


@router.post("/retention/sweep")
async def admin_retention_sweep(_: None = Depends(require_admin_token)) -> dict[str, Any]:
    """Run the retention sweep: delete operational records older than the policy windows."""
    policy = RetentionPolicy.from_env()
    summary = await retention_sweep(policy, async_session_maker, dry_run=False)
    return summary.to_dict()


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
