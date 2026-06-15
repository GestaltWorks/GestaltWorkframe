from collections.abc import Awaitable, Callable
import asyncio

from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import os
import logging
from contextlib import asynccontextmanager

from gestaltworkframe.api.chat import (
    chat_body_size_limit,
    router as chat_router,
)
from gestaltworkframe.api.contact import contact_body_size_limit, router as contact_router
from gestaltworkframe.api.admin import (
    router as admin_router,
)
from gestaltworkframe.api.admin_discovery import (
    router as admin_discovery_router,
)
from gestaltworkframe.api.library_feed import router as library_feed_router
from gestaltworkframe.api.health import (
    router as health_router,
)
from gestaltworkframe.api.deployment_config import router as deployment_config_router
from gestaltworkframe.api.privacy_audit import router as privacy_audit_router
from gestaltworkframe.api.connectors_webhook import router as connectors_webhook_router
from gestaltworkframe.api.admin_newsletter import router as admin_newsletter_router
from gestaltworkframe.api.intake import intake_body_size_limit, router as intake_router
from gestaltworkframe.api.newsletter_public import router as newsletter_public_router
from gestaltworkframe.api.services import (
    AppServices,
    build_app_services,
    enabled_cost_tiers,
)
from gestaltworkframe.core.db import (
    init_db,
)
from gestaltworkframe.mcp_servers.kb_server import vectorstore_document_count

# Re-exported for `import gestaltworkframe.api.main as api_main` consumers (tests).
# These are the only symbols still reached through api.main; everything else now
# imports from its owning module directly. Keep this list minimal.
from fastapi import HTTPException  # noqa: F401
from gestaltworkframe.api.admin import (  # noqa: F401
    AdminPolicyPatch,
    DEFAULT_CLOUD_INPUT_TOKEN_CAP,
    DEFAULT_CLOUD_OUTPUT_TOKEN_CAP,
    _admin_health_payload,
    _apply_admin_policy,
)
from gestaltworkframe.api.chat import (  # noqa: F401
    CHAT_DAILY_TOKEN_LIMIT,
    CHAT_IP_LIMIT,
    CHAT_MAX_MESSAGE_CHARS,
    INTAKE_QUESTIONS,
    ChatRequest,
    chat_stream,
    clean_intake_text,
    get_intake_questions,
)
from gestaltworkframe.api.health import provider_health_check  # noqa: F401
from gestaltworkframe.api.services import (  # noqa: F401
    ChatMetrics,
    get_app_services,
    require_admin_token,
)
from gestaltworkframe.core.db import get_session  # noqa: F401

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


def _parse_allowed_origins(raw: str) -> tuple[str, ...]:
    return tuple(sorted({origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip()}))


CORS_ALLOWED_ORIGINS = _parse_allowed_origins(os.getenv("CORS_ALLOWED_ORIGINS", DEFAULT_CORS_ALLOWED_ORIGINS))


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


# Admin policy + admin health + handoffs live in api/admin.py; admin discovery
# endpoints live in api/admin_discovery.py. Both are wired in via include_router below.


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

# Health endpoints (/health, /health/providers) live in api/health.py; the
# router is wired in below alongside the chat router.
