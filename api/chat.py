"""Chat HTTP surface: /chat/stream, /intake/questions, /modes.

This module owns everything that runs on a chat turn: the public ChatRequest
contract, the abuse-limit gate, the per-turn structured logger, and the
streaming SSE handler that ties them together. It does NOT own the routing
decision, the model providers, the cloud budget, or the persistence layer.
Those collaborators come in through `AppServices` and the `core/db` helpers.

The module is split out from api/main.py to keep the FastAPI app entry point
focused on application construction. Backward-compat re-exports in api/main.py
preserve the historical import surface (`api.main.chat_stream`,
`api.main.ChatRequest`, etc.) so tests and external code continue to work.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from api.request_helpers import client_ip, make_body_size_limit
from api.services import AppServices, get_app_services
from core.db import (
    add_chat_usage_event,
    add_chat_usage_event_in_new_session,
    add_message,
    add_message_in_new_session,
    chat_usage_snapshot,
    create_conversation,
    get_conversation,
    get_messages,
    get_session,
    save_intake_record,
    save_terminal_intake_submission,
)
logger = logging.getLogger(__name__)


_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_intake_text(value: str) -> str:
    return _CONTROL_RE.sub("", value).strip()


# Module-level limits. Read at import time but consumed via lazy lookups in
# closures so tests can monkeypatch and have the change take effect on the
# next request.
CHAT_MAX_BODY_BYTES = int(os.getenv("CHAT_MAX_BODY_BYTES", "32768"))
CHAT_MAX_MESSAGE_CHARS = int(os.getenv("CHAT_MAX_MESSAGE_CHARS", "4000"))
CHAT_IP_WINDOW = timedelta(seconds=int(os.getenv("CHAT_IP_WINDOW_SECONDS", "3600")))
CHAT_SESSION_WINDOW = timedelta(seconds=int(os.getenv("CHAT_SESSION_WINDOW_SECONDS", "3600")))
CHAT_IP_LIMIT = int(os.getenv("CHAT_IP_LIMIT", "30"))
CHAT_SESSION_LIMIT = int(os.getenv("CHAT_SESSION_LIMIT", "12"))
CHAT_DAILY_TOKEN_LIMIT = int(os.getenv("CHAT_DAILY_TOKEN_LIMIT", "100000"))
CHAT_OUTPUT_TOKEN_RESERVE = max(int(os.getenv("CHAT_OUTPUT_TOKEN_RESERVE", "1000")), 0)
# SSE heartbeat: a colon-prefixed comment line that browsers, EventSource
# clients, and any conformant SSE parser ignore. Without it, reverse proxies
# (nginx, CloudFront, Cloudflare) idle out connections during long model turns.
# Set to 0 to disable, useful in tests that want a synchronous stream.
SSE_HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("SSE_HEARTBEAT_INTERVAL_SECONDS", "15"))


async def _with_heartbeat(stream: AsyncIterator[str], interval: float) -> AsyncIterator[str]:
    """Yield items from `stream`, emitting an SSE heartbeat comment if idle.

    Races the underlying iterator against an asyncio timeout. When the
    iterator produces nothing for `interval` seconds, emits `: ka\\n\\n`
    (a comment, ignored by the EventSource parser) so the connection
    stays warm through reverse proxies that drop idle TCP after some
    window. Exceptions raised inside `stream` are re-raised on the
    outer iteration so the caller's existing try/except still works.
    """
    if interval <= 0:
        async for item in stream:
            yield item
        return

    queue: asyncio.Queue = asyncio.Queue()
    sentinel: Any = object()

    async def pump() -> None:
        try:
            async for item in stream:
                await queue.put(item)
        except BaseException as exc:  # noqa: BLE001 - re-raised in outer loop
            await queue.put(exc)
        else:
            await queue.put(sentinel)

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=interval)
            except asyncio.TimeoutError:
                yield ": ka\n\n"
                continue
            if item is sentinel:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


# The guided-intake question set returned by GET /intake/questions. Frontend
# ChatWidget renders these directly; the option strings are part of the public
# contract (helpNeededModeMap and objectiveOptionLabels match against them).
INTAKE_QUESTIONS: list[dict[str, Any]] = [
    {
        "id": "objective",
        "label": "What are you hoping to accomplish?",
        "options": [
            "Explore automation support or consulting",
            "Get help building or debugging a workflow",
            "Learn how automation works",
            "Find reusable workflows, patterns, or examples",
        ],
    },
    {
        "id": "building",
        "label": "What are you trying to do or build?",
        "type": "text",
        "placeholder": "Example: onboard users, clean PSA data, sync tickets, learn CTX/TASKS...",
    },
    {
        "id": "maturity",
        "label": "How automated are you today?",
        "options": ["Just starting", "Some scripts/workflows", "Several production automations", "Mature automation program"],
    },
    {
        "id": "help_needed",
        "label": "What would be most useful right now?",
        "options": [
            "Help me choose the next step",
            "Give me a technical answer I can use",
            "Show me examples or patterns",
            "Walk me through it so I understand",
        ],
    },
]


class IntakeAnswers(BaseModel):
    objective: str = Field(min_length=1, max_length=300)
    building: str = Field(min_length=1, max_length=1000)
    maturity: str = Field(min_length=1, max_length=200)
    help_needed: str = Field(min_length=1, max_length=200)

    @field_validator("*")
    @classmethod
    def clean_strings(cls, value: str) -> str:
        return clean_intake_text(value)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=CHAT_MAX_MESSAGE_CHARS)
    mode: str = "automator"
    conversation_id: str | None = None
    terminal_session_id: str | None = Field(default=None, min_length=8, max_length=100)
    intake_complete: bool = False
    intake: IntakeAnswers | None = None

    @field_validator("message", mode="before")
    @classmethod
    def clean_message(cls, value: object) -> object:
        if value is None:
            raise ValueError("message is required")
        return clean_intake_text(str(value))

    @field_validator("terminal_session_id", mode="before")
    @classmethod
    def clean_terminal_session_id(cls, value: object) -> object:
        if value is None:
            return None
        cleaned = clean_intake_text(str(value))
        return cleaned or None

    @model_validator(mode="after")
    def intake_required_when_complete(self) -> "ChatRequest":
        # A client claiming `intake_complete=True` with no `intake` payload
        # bypasses the guided-intake gate at the router (decision.intake is
        # empty) while still flipping past the intake stage. Reject up front
        # so the contract is unambiguous: completion requires answers.
        if self.intake_complete and self.intake is None:
            raise ValueError("intake answers are required when intake_complete is true")
        return self


@dataclass(frozen=True)
class ChatAbuseContext:
    ip_address: str
    session_key: str
    input_tokens: int


# Body-size middleware bound to /chat/stream. The lazy max_bytes lookup keeps
# the module-level constant monkeypatchable in tests.
chat_body_size_limit = make_body_size_limit(
    path="/chat/stream",
    max_bytes=lambda: CHAT_MAX_BODY_BYTES,
    detail="Chat request body is too large.",
)


def _estimate_chat_tokens(text: str) -> int:
    # Soft abuse-budget estimate, not provider-grade accounting. This can
    # undercount token-dense Unicode input, so hard provider caps still apply.
    if not text:
        return 0
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


def _enum_value(value: Any) -> Any:
    if value is None:
        return None
    return getattr(value, "value", value)


def _safe_route_log_payload(services: AppServices) -> dict[str, Any]:
    router = getattr(services, "llm_router", None)
    diagnostics = router.route_diagnostics() if router and hasattr(router, "route_diagnostics") else {}
    if not isinstance(diagnostics, dict):
        return {}
    candidates = diagnostics.get("candidates") if isinstance(diagnostics.get("candidates"), list) else []
    selected_route = diagnostics.get("selected_route") or None
    selected_tier = _selected_route_tier(services, selected_route, candidates)
    return {
        "routing_strategy": diagnostics.get("routing_strategy"),
        "selected_route": selected_route,
        "selected_route_tier": selected_tier,
        "selected_route_family": _route_family(selected_tier, selected_route),
        "ordered_routes": diagnostics.get("ordered_routes") or [],
        "empty_reason": diagnostics.get("empty_reason") or None,
        "candidate_count": len(candidates),
    }


def _selected_route_tier(services: AppServices, selected_route: Any, candidates: list[Any]) -> str | None:
    if not selected_route:
        return None
    router = getattr(services, "llm_router", None)
    for route in getattr(router, "routes", []) or []:
        if getattr(route, "name", None) == selected_route:
            return getattr(route, "cost_tier", None)
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate.get("name") == selected_route:
            return candidate.get("cost_tier")
    return None


def _route_family(cost_tier: str | None, selected_route: Any) -> str | None:
    if not selected_route:
        return None
    if cost_tier in {"low_cost", "premium"}:
        return "cloud"
    if cost_tier:
        return "local"
    return "unknown"


def _safe_decision_log_payload(decision: Any) -> dict[str, Any]:
    return {
        "mode": _enum_value(getattr(decision, "selected_mode", None)),
        "stage": _enum_value(getattr(decision, "stage", None)),
        "intent": _enum_value(getattr(decision, "intent", None)),
        "tone": _enum_value(getattr(decision, "tone", None)),
        "response_policy": _enum_value(getattr(decision, "response_policy", None)),
        "retrieval_required": bool(getattr(decision, "retrieval_required", False)),
        "retrieval_tool": getattr(decision, "retrieval_tool", None),
        "tool_execution_mode": _enum_value(getattr(decision, "tool_execution_mode", None)),
        "cloud_allowed": bool(getattr(decision, "cloud_allowed", False)),
        "service_handoff_suggested": bool(getattr(decision, "service_handoff_suggested", False)),
    }


async def _log_chat_turn(
    *,
    request_id: str,
    status_value: str,
    started_at: float,
    decision: Any,
    services: AppServices,
    output_text: str,
    error_type: str | None = None,
) -> None:
    payload = {
        "event": "chat_turn",
        "request_id": request_id,
        "status": status_value,
        "duration_ms": round((time.monotonic() - started_at) * 1000),
        "output_tokens_estimate": _estimate_chat_tokens(output_text),
        "output_chars": len(output_text),
        **_safe_decision_log_payload(decision),
        **_safe_route_log_payload(services),
    }
    if error_type:
        payload["error_type"] = error_type
    logger.info("chat_turn %s", json.dumps(payload, sort_keys=True))
    metrics = getattr(services, "chat_metrics", None)
    if metrics and hasattr(metrics, "record"):
        await metrics.record(payload)


def _chat_session_key(chat_request: ChatRequest, ip_address: str) -> str:
    # Session keys support a separate per-user throttle. IP counts are checked
    # independently, so rotating session IDs does not bypass the network limit.
    if chat_request.terminal_session_id:
        return f"terminal:{chat_request.terminal_session_id}"
    if chat_request.conversation_id:
        return f"conversation:{chat_request.conversation_id}"
    return f"ip:{ip_address}"


def _utc_day_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def _enforce_chat_abuse_limits(
    chat_request: ChatRequest,
    http_request: Request,
    session: AsyncSession,
) -> ChatAbuseContext:
    ip_address = client_ip(http_request)
    session_key = _chat_session_key(chat_request, ip_address)
    now = datetime.now(timezone.utc)
    input_tokens = _estimate_chat_tokens(chat_request.message)
    snapshot = await chat_usage_snapshot(
        session,
        ip_address=ip_address,
        session_key=session_key,
        ip_rate_since=now - CHAT_IP_WINDOW,
        session_rate_since=now - CHAT_SESSION_WINDOW,
        token_since=_utc_day_start(),
    )

    if CHAT_IP_LIMIT > 0 and snapshot["ip_requests"] >= CHAT_IP_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many chat requests from this network. Please try again later.",
        )
    if CHAT_SESSION_LIMIT > 0 and snapshot["session_requests"] >= CHAT_SESSION_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many chat requests from this session. Please try again later.",
        )
    if CHAT_DAILY_TOKEN_LIMIT > 0:
        # These are soft public caps. The read/write pair can race under
        # concurrency, but provider/router budget gates still enforce spend caps.
        # Worst-case soft overage is roughly concurrent_requests * output_reserve.
        projected_tokens = snapshot["daily_tokens"] + input_tokens + CHAT_OUTPUT_TOKEN_RESERVE
        if projected_tokens > CHAT_DAILY_TOKEN_LIMIT:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="The public chat daily token budget is exhausted. Please try again later.",
            )

    # Count accepted requests before model work so retries and failing provider
    # calls still contribute to abuse throttling.
    await add_chat_usage_event(
        session,
        ip_address=ip_address,
        session_key=session_key,
        input_tokens=input_tokens,
    )
    return ChatAbuseContext(ip_address=ip_address, session_key=session_key, input_tokens=input_tokens)


router = APIRouter(tags=["chat"])


@router.get("/intake/questions")
async def get_intake_questions():
    return {"questions": INTAKE_QUESTIONS}


@router.post("/chat/stream")
async def chat_stream(chat_request: ChatRequest, http_request: Request, session: AsyncSession = Depends(get_session)):
    request_id = uuid.uuid4().hex
    started_at = time.monotonic()
    abuse_context = await _enforce_chat_abuse_limits(chat_request, http_request, session)
    services = get_app_services(http_request)
    decision = services.chat_turns.plan(
        chat_request.mode,
        chat_request.message,
        intake_complete=chat_request.intake_complete,
        intake=chat_request.intake.model_dump() if chat_request.intake else None,
    )
    selected_mode = decision.selected_mode.value

    # 1. Manage Conversation
    if chat_request.conversation_id:
        conv = await get_conversation(chat_request.conversation_id, session)
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")
        conv_id = conv.id
    else:
        conv = await create_conversation(selected_mode, session)
        conv_id = conv.id

    if chat_request.intake_complete and chat_request.intake:
        intake_payload = chat_request.intake.model_dump()
        await save_intake_record(
            conv_id,
            selected_mode,
            intake_payload,
            session,
        )
        if chat_request.terminal_session_id:
            # /intake/submissions already counted the user action; chat only links it.
            await save_terminal_intake_submission(
                chat_request.terminal_session_id,
                selected_mode,
                intake_payload,
                session,
                source_path="/chat/stream",
                referrer=clean_intake_text(http_request.headers.get("referer", ""))[:500],
                user_agent=clean_intake_text(http_request.headers.get("user-agent", ""))[:500],
                ip_address=client_ip(http_request),
                conversation_id=conv_id,
                count_submission=False,
            )

    # 2. Add user message
    await add_message(conv_id, "user", chat_request.message, session)

    # 3. Load history
    history = await get_messages(conv_id, session)
    messages = [{"role": msg.role, "content": msg.content} for msg in history]

    async def generate():
        full_response = ""
        usage_recorded = False
        stream_status = "started"
        error_type = None

        async def record_output_usage() -> None:
            nonlocal usage_recorded
            if usage_recorded or not full_response:
                return
            usage_recorded = True
            try:
                await add_chat_usage_event_in_new_session(
                    ip_address=abuse_context.ip_address,
                    session_key=abuse_context.session_key,
                    conversation_id=conv_id,
                    output_tokens=_estimate_chat_tokens(full_response),
                )
            except Exception:
                logger.exception("Failed to record chat output usage")

        try:
            # Yield conversation ID first so client knows it
            yield f"data: {json.dumps({'conversation_id': conv_id, 'request_id': request_id, 'selected_mode': selected_mode, 'stage': decision.stage.value, 'retrieval_tool': decision.retrieval_tool})}\n\n"

            async for chunk in services.chat_turns.stream(
                decision,
                chat_request.message,
                messages,
                conv_id,
            ):
                full_response += chunk
                yield f"data: {json.dumps({'content': chunk})}\n\n"

            if full_response:
                try:
                    await add_message_in_new_session(conv_id, "assistant", full_response)
                except Exception:
                    logger.exception("Failed to persist assistant message")

            stream_status = "completed"
            yield "data: [DONE]\n\n"

        except Exception as exc:
            stream_status = "failed"
            error_type = exc.__class__.__name__
            logger.error("Chat stream failed request_id=%s error_type=%s", request_id, error_type)
            yield f"data: {json.dumps({'error': {'code': 'stream_failed', 'message': 'The chat stream failed. Please try again.', 'request_id': request_id}})}\n\n"
        finally:
            await record_output_usage()
            await _log_chat_turn(
                request_id=request_id,
                status_value=stream_status,
                started_at=started_at,
                decision=decision,
                services=services,
                output_text=full_response,
                error_type=error_type,
            )

    response = StreamingResponse(
        _with_heartbeat(generate(), SSE_HEARTBEAT_INTERVAL_SECONDS),
        media_type="text/event-stream",
    )
    # Helps reverse proxies (nginx in particular) not buffer the SSE stream,
    # which would defeat the live-chunk fix from the streaming-buffer phase.
    response.headers["Cache-Control"] = "no-cache, no-transform"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["X-Request-Id"] = request_id
    return response


@router.get("/modes")
async def get_modes_endpoint():
    return {
        "modes": [
            {"id": "pipeline", "name": "Service Inquiry", "description": "Explore automation support, consulting, and implementation help."},
            {"id": "automator", "name": "Automator Assistance", "description": "Get help with automation workflows."},
            {"id": "educator", "name": "Educator", "description": "Learn automation concepts through lessons and challenges."},
        ]
    }
