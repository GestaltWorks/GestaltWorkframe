from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
import asyncio

from fastapi import FastAPI, HTTPException, Depends, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Any, Literal
import os
import json
import logging
import time
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

# Backward-compat: api.main historically exposed every chat-surface symbol.
# The chat router, models, helpers, and constants now live in api.chat;
# importing them here keeps the historical surface area for tests and external
# code that does `import gestaltworkframe.api.main as api_main` and reaches for ChatRequest,
# ChatMetrics, chat_stream, etc.
from gestaltworkframe.api.chat import (
    CHAT_DAILY_TOKEN_LIMIT,
    CHAT_IP_LIMIT,
    CHAT_IP_WINDOW,
    CHAT_MAX_BODY_BYTES,
    CHAT_MAX_MESSAGE_CHARS,
    CHAT_OUTPUT_TOKEN_RESERVE,
    CHAT_SESSION_LIMIT,
    CHAT_SESSION_WINDOW,
    ChatAbuseContext,
    ChatRequest,
    IntakeAnswers,
    INTAKE_QUESTIONS,
    chat_body_size_limit,
    chat_stream,
    get_intake_questions,
    get_modes_endpoint,
    router as chat_router,
    _chat_session_key,
    _enforce_chat_abuse_limits,
    _enum_value,
    _estimate_chat_tokens,
    _log_chat_turn,
    _route_family,
    _safe_decision_log_payload,
    _safe_route_log_payload,
    _selected_route_tier,
    _utc_day_start,
)
from gestaltworkframe.api.contact import contact_body_size_limit, router as contact_router
from gestaltworkframe.api.admin import (
    ADMIN_HANDOFF_LIMIT,
    AdminPolicyPatch,
    DEFAULT_CLOUD_INPUT_TOKEN_CAP,
    DEFAULT_CLOUD_OUTPUT_TOKEN_CAP,
    admin_health_check,
    admin_handoff_packets,
    update_admin_policy,
    router as admin_router,
    _admin_health_payload,
    _admin_packet_payload,
    _apply_admin_policy,
    _recent_handoff_packets,
    _safe_json_dict,
)
from gestaltworkframe.api.admin_discovery import (
    DISCOVERY_RUN_ONCE_MIN_INTERVAL_SECONDS,
    DiscoveryLibraryPromotionRequest,
    DiscoveryDecisionRequest,
    DiscoverySourceCreate,
    DiscoverySourcePatch,
    DiscoverySourcePromotionRequest,
    admin_discovery_approve_find,
    admin_discovery_create_source,
    admin_discovery_finds,
    admin_discovery_promote_library,
    admin_discovery_promote_source,
    admin_discovery_reject_find,
    admin_discovery_run_once,
    admin_discovery_sources,
    admin_discovery_update_source,
    router as admin_discovery_router,
)
from gestaltworkframe.api.library_feed import router as library_feed_router, library_latest_feed
from gestaltworkframe.api.health import (
    router as health_router,
    provider_health_check,
    _is_cloud_status,
    _provider_status,
    _public_cloud_block_reason,
    _public_cloud_health_controls,
    _public_provider_group,
)
from gestaltworkframe.api.deployment_config import router as deployment_config_router
from gestaltworkframe.api.privacy_audit import router as privacy_audit_router
from gestaltworkframe.api.connectors_webhook import router as connectors_webhook_router
from gestaltworkframe.api.admin_newsletter import router as admin_newsletter_router
from gestaltworkframe.api.intake import intake_body_size_limit, router as intake_router
from gestaltworkframe.api.newsletter_public import router as newsletter_public_router
from gestaltworkframe.api.services import (
    AppServices,
    ChatMetrics,
    build_app_services,
    enabled_cost_tiers,
    get_app_services,
    require_admin_token,
)
from gestaltworkframe.core.cloud_budget import CloudBudgetGate  # re-exported for tests
from gestaltworkframe.core.db import (
    ContactRecord,
    TerminalIntakeRecord,
    add_chat_usage_event,
    add_chat_usage_event_in_new_session,
    add_message,
    add_message_in_new_session,
    chat_usage_snapshot,
    create_conversation,
    get_conversation,
    get_messages,
    get_session,
    init_db,
    save_intake_record,
    save_terminal_intake_submission,
)
from gestaltworkframe.core.handoff_packets import (
    build_contact_handoff_packet,
    build_terminal_intake_handoff_packet,
    packet_to_dict,
)
from gestaltworkframe.core.discovery_digest import send_discovery_digest
from gestaltworkframe.core.discovery_queue import (
    add_watched_source,
    decide_find,
    list_public_latest_finds,
    list_recent_finds,
    list_source_health,
    promote_find_to_library,
    promote_find_to_source,
    update_watched_source,
)
from gestaltworkframe.core.discovery_scheduler import run_one_pass
from gestaltworkframe.core.discovery_summary import summarize_discovery_finds
from gestaltworkframe.core.router import ROUTING_STRATEGIES
# Backward-compat: tests historically reached for `api.main.clean_intake_text`.
# The implementation now lives wherever the consumer needs it; re-export here.
from gestaltworkframe.api.chat import clean_intake_text  # noqa: F401
from gestaltworkframe.kb.library_publisher import LibraryPublisherConfigError, LibraryPublisherError
from gestaltworkframe.kb.watchlist import CADENCE_SECONDS, WatchedSource, validate_watchlist
from gestaltworkframe.mcp_servers.kb_server import vectorstore_document_count

logger = logging.getLogger(__name__)

STATE_CHANGING_PUBLIC_PATHS = {
    "/chat/stream",
    "/contact",
    "/intake/submissions",
    # Phase: newsletter signup. Outbound auto-reply email is triggered
    # on POST, so the same cross-origin guard as /contact applies.
    "/newsletter/api/subscribe",
    # RFC 8058 one-click unsubscribe POST (no body required; Gmail / Yahoo
    # bulk-sender compliance). Same-origin or absent Origin (mail-client
    # initiated) only.
    "/newsletter/unsubscribe",
}
DEFAULT_CORS_ALLOWED_ORIGINS = "http://localhost:3000,http://127.0.0.1:3000"

# ADMIN_HANDOFF_LIMIT, DEFAULT_CLOUD_INPUT_TOKEN_CAP, DEFAULT_CLOUD_OUTPUT_TOKEN_CAP
# are re-exported from gestaltworkframe.api.admin. DISCOVERY_RUN_ONCE_MIN_INTERVAL_SECONDS and the
# scheduler-trigger lock are re-exported from gestaltworkframe.api.admin_discovery.


def _parse_allowed_origins(raw: str) -> tuple[str, ...]:
    return tuple(sorted({origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip()}))


CORS_ALLOWED_ORIGINS = _parse_allowed_origins(os.getenv("CORS_ALLOWED_ORIGINS", DEFAULT_CORS_ALLOWED_ORIGINS))


# AdminPolicyPatch, all Discovery* request models, _admin_health_payload,
# _apply_admin_policy, _recent_handoff_packets, and every /admin/api/*
# endpoint live in api/admin.py and api/admin_discovery.py. Backward-compat
# re-exports happen at the top of this file.


# ChatMetrics, AppServices, build_app_services, get_app_services,
# require_admin_token, and the loopback / enabled-cost-tiers helpers all live
# in api/services.py. They are re-exported above for backward compatibility
# with `import gestaltworkframe.api.main as api_main` consumers.
_enabled_cost_tiers = enabled_cost_tiers


async def _log_kb_startup_status() -> None:
    try:
        count = await asyncio.to_thread(vectorstore_document_count)
    except Exception:
        logger.exception("Knowledge base vector store startup check failed")
        return
    if count is None:
        logger.warning("Knowledge base vector store document count is unavailable")
    elif count <= 0:
        logger.warning("Knowledge base vector store is empty; grounded retrieval will return no documents")
    else:
        logger.info("Knowledge base vector store contains %s documents", count)


# Admin policy + admin health + handoffs live in api/admin.py.
# Admin discovery endpoints live in api/admin_discovery.py.
# Both are wired into the FastAPI app via include_router below; helpers
# (_admin_health_payload, _apply_admin_policy, etc.) are re-exported at
# the top of this file for backward compatibility.


@asynccontextmanager
async def lifespan(app: FastAPI):
    services: AppServices | None = None
    try:
        services = await build_app_services()
    except Exception:
        logger.exception("Failed to build application services")
        # `services` is None here; falling through to the outer try would mask
        # the real startup error with AttributeError on services.close().
        raise
    try:
        app.state.services = services
        # Production startup uses SQLModel create_all for additive SQLite tables.
        await init_db()
        await services.cloud_budget.init()
        await _log_kb_startup_status()
        # Startup: check initial provider health
        local_healthy = await services.local_provider.is_healthy()
        logger.info("Local LLM Provider healthy: %s", local_healthy)
        yield
    finally:
        # `services` is guaranteed non-None here because build_app_services
        # raised above if it failed. The guard is paranoia for future refactors
        # that might restructure the early-error path.
        if services is not None:
            await services.close()

app = FastAPI(title="Gestalt Workframe API", lifespan=lifespan)
# Route-specific guards ignore paths they do not own. Starlette wraps middleware in
# reverse registration order; state_changing_origin_guard is registered last and runs first.
app.middleware("http")(contact_body_size_limit)  # executes fourth
app.middleware("http")(intake_body_size_limit)  # executes third
app.middleware("http")(chat_body_size_limit)  # executes second; defined in api.chat


def _allowed_origins() -> set[str]:
    return set(CORS_ALLOWED_ORIGINS)


async def state_changing_origin_guard(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path in STATE_CHANGING_PUBLIC_PATHS:
        origin = request.headers.get("origin", "").strip().rstrip("/")
        if origin and origin not in _allowed_origins():
            return JSONResponse(
                {"detail": "Request origin is not allowed."},
                status_code=status.HTTP_403_FORBIDDEN,
            )
    return await call_next(request)


async def request_id_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Stamp every response with a stable X-Request-Id for log correlation.

    Honors a caller-supplied X-Request-Id when it's a plausible UUID-ish
    token (alphanumeric, 8-64 chars). Otherwise generates a uuid4 hex.
    chat_stream sets the header explicitly inside its handler so the same
    id flows through SSE payloads; this middleware's value is overridden
    there by the handler's later assignment.
    """
    incoming = request.headers.get("x-request-id", "").strip()
    if incoming and len(incoming) <= 64 and incoming.replace("-", "").replace("_", "").isalnum():
        rid = incoming
    else:
        import uuid as _uuid
        rid = _uuid.uuid4().hex
    request.state.request_id = rid
    response = await call_next(request)
    response.headers.setdefault("X-Request-Id", rid)
    return response


app.middleware("http")(state_changing_origin_guard)  # executes first
app.middleware("http")(request_id_middleware)  # executes before origin guard so even rejected requests carry an id

app.include_router(admin_discovery_router)
app.include_router(admin_router)
app.include_router(chat_router)
app.include_router(library_feed_router)
app.include_router(contact_router)
app.include_router(health_router)
app.include_router(deployment_config_router)
app.include_router(privacy_audit_router)
app.include_router(connectors_webhook_router)
app.include_router(admin_newsletter_router)
app.include_router(intake_router)
app.include_router(newsletter_public_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(CORS_ALLOWED_ORIGINS),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Health endpoints (/health, /health/providers) and their helpers live in
# api/health.py. The api.health router is wired in below alongside the chat
# router. Backward-compat re-exports of the helpers are added at the top of
# the file.
