"""End-to-end chat happy-path test.

Exercises the full /chat/stream HTTP surface against the real ASGI app
with an in-memory SQLite database and a synthetic AppServices stack.
Verifies the public contract:

- POST /chat/stream returns 200 with an SSE response.
- The first SSE event carries conversation_id, request_id, selected_mode.
- Subsequent events carry assistant content chunks.
- The stream ends with [DONE].
- The conversation, user message, and assistant message are persisted.

This test complements the unit-level chat_stream tests in test_api_main.py:
those tests mock storage to assert specific code paths, while this one
runs the whole pipeline end-to-end through httpx + the ASGI app so we
catch regressions in middleware order, dependency-injection wiring,
streaming response shape, and persistence side effects.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import api.main as api_main
from api.chat import ChatRequest as _ChatRequest  # noqa: F401  - keep import path validated
from api.services import AppServices, ChatMetrics
from gestaltworkframe.core.db import Conversation, MessageRecord
from gestaltworkframe.core.policy import ChatMode, ConversationStage, ResponsePolicy, RoutingDecision, ToneSignal, UserIntent


class _FakeChatTurns:
    """Minimal ChatTurnOrchestrator stand-in that yields a fixed reply."""

    def plan(self, mode, message, intake_complete, intake=None):
        return RoutingDecision(
            stage=ConversationStage.ACTIVE,
            selected_mode=ChatMode.AUTOMATOR,
            intent=UserIntent.TECHNICAL_HELP,
            tone=ToneSignal.NEUTRAL,
            response_policy=ResponsePolicy.LOCAL_ONLY,
            reason="e2e_happy_path",
        )

    async def stream(self, decision, user_message, history, conv_id):
        for chunk in ("Hello", " from", " the", " router."):
            yield chunk


@dataclass
class _FakeRouter:
    routes: list = field(default_factory=list)

    async def provider_statuses(self, *args, **kwargs):
        return []

    async def close(self):
        return None


def _services() -> AppServices:
    """Build a minimal AppServices that satisfies every attribute access in chat_stream."""
    return AppServices(
        local_provider=SimpleNamespace(),
        secondary_provider=None,
        cloud_budget=SimpleNamespace(snapshot=lambda: {}),
        llm_router=_FakeRouter(),
        orchestrator=SimpleNamespace(),
        chat_turns=_FakeChatTurns(),
        chat_metrics=ChatMetrics(),
    )


async def _start_app(tmp_path):
    """Wire api_main.app to a per-test in-memory SQLite DB and fake services."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'e2e.db'}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with maker() as session:
            yield session

    # Inject the fake services into app state and override get_session so
    # every storage helper inside chat_stream commits into our test DB.
    api_main.app.state.services = _services()
    api_main.app.dependency_overrides[api_main.get_session] = override_get_session

    # crud helpers that open their own session (the *_in_new_session variants)
    # don't go through Depends, so they use the module-level async_session_maker.
    # Point that at our test maker for the duration of the test.
    import importlib
    engine_mod = importlib.import_module("gestaltworkframe.core.db.engine")
    crud_mod = importlib.import_module("gestaltworkframe.core.db.crud")
    original_maker = engine_mod.async_session_maker
    engine_mod.async_session_maker = maker
    crud_mod.async_session_maker = maker

    return engine, maker, original_maker


def _restore_app(original_maker):
    api_main.app.dependency_overrides.clear()
    import importlib
    engine_mod = importlib.import_module("gestaltworkframe.core.db.engine")
    crud_mod = importlib.import_module("gestaltworkframe.core.db.crud")
    engine_mod.async_session_maker = original_maker
    crud_mod.async_session_maker = original_maker
    if hasattr(api_main.app.state, "services"):
        delattr(api_main.app.state, "services")


def _parse_sse(body: str) -> list[dict | str]:
    """Parse the SSE body into a list of event payloads (dict or '[DONE]')."""
    events: list[dict | str] = []
    for block in body.split("\n\n"):
        for line in block.splitlines():
            if not line.startswith("data: "):
                continue
            payload = line[len("data: "):]
            if payload == "[DONE]":
                events.append("[DONE]")
            else:
                try:
                    events.append(json.loads(payload))
                except json.JSONDecodeError:
                    events.append({"raw": payload})
    return events


@pytest.mark.asyncio
async def test_chat_stream_happy_path_persists_conversation_and_returns_sse(tmp_path, monkeypatch):
    monkeypatch.setenv("SSE_HEARTBEAT_INTERVAL_SECONDS", "0")
    # Reload the chat module so the env override takes effect on the SSE_HEARTBEAT constant.
    import importlib
    import api.chat
    importlib.reload(api.chat)
    # Re-mount the chat router after the reload so api_main.app picks up the reloaded handler.
    # Note: this is heavy-handed but keeps the test deterministic without a heartbeat dependency.
    api_main.app.router.routes = [r for r in api_main.app.router.routes if getattr(r, "path", "") != "/chat/stream"]
    api_main.app.include_router(api.chat.router)

    engine, maker, original_maker = await _start_app(tmp_path)

    try:
        transport = httpx.ASGITransport(app=api_main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/chat/stream",
                json={
                    "message": "Help me debug this workflow",
                    "mode": "automator",
                    "intake_complete": False,  # Skip the intake save branch in this happy path.
                },
                # Origin header satisfies the state_changing_origin_guard for the public path.
                headers={"origin": "http://localhost:3000"},
            )

        assert response.status_code == 200, response.text
        assert response.headers.get("content-type", "").startswith("text/event-stream")
        assert "x-request-id" in {k.lower() for k in response.headers.keys()}

        events = _parse_sse(response.text)

        # First event: setup payload with conversation_id and request_id.
        assert events, f"expected SSE events, got: {response.text!r}"
        setup = events[0]
        assert isinstance(setup, dict)
        assert "conversation_id" in setup
        assert "request_id" in setup
        assert setup["selected_mode"] == "automator"

        # Content chunks should be present in order.
        content = "".join(e["content"] for e in events if isinstance(e, dict) and "content" in e)
        assert content == "Hello from the router."

        # Stream terminates with [DONE].
        assert "[DONE]" in events

        # Persistence: one conversation, one user message, one assistant message.
        async with maker() as session:
            conv_count = (await session.execute(select(func.count()).select_from(Conversation))).scalar_one()
            assert conv_count == 1

            messages = (await session.execute(select(MessageRecord).order_by(MessageRecord.created_at))).scalars().all()
            roles = [m.role for m in messages]
            assert roles == ["user", "assistant"]
            assert messages[0].content == "Help me debug this workflow"
            assert messages[1].content == "Hello from the router."
    finally:
        _restore_app(original_maker)
        await engine.dispose()
