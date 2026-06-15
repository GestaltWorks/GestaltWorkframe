import json
import logging

import pytest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import gestaltworkframe.api.chat as api_chat
import gestaltworkframe.api.main as api_main
from gestaltworkframe.core.db import add_chat_usage_event as db_add_chat_usage_event
from gestaltworkframe.core.db import chat_usage_snapshot as db_chat_usage_snapshot
from gestaltworkframe.api.main import INTAKE_QUESTIONS, ChatRequest, get_intake_questions
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel


@pytest.fixture(autouse=True)
def allow_chat_usage(monkeypatch):
    # Most chat_stream unit tests mock storage-heavy dependencies. Tests that need
    # real chat usage persistence import and call the DB helpers directly.
    async def chat_usage_snapshot(*args, **kwargs):
        return {"ip_requests": 0, "session_requests": 0, "daily_tokens": 0}

    async def add_chat_usage_event(*args, **kwargs):
        return SimpleNamespace(id="chat-usage-1")

    async def add_chat_usage_event_in_new_session(**kwargs):
        return SimpleNamespace(id="chat-usage-2")

    monkeypatch.setattr(api_chat, "chat_usage_snapshot", chat_usage_snapshot)
    monkeypatch.setattr(api_chat, "add_chat_usage_event", add_chat_usage_event)
    monkeypatch.setattr(api_chat, "add_chat_usage_event_in_new_session", add_chat_usage_event_in_new_session)


def test_chat_request_rejects_intake_complete_without_answers():
    # Closed-by-default validator: claiming completion without payload bypasses
    # the guided-intake gate at the router. Reject up front.
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        ChatRequest(message="hello", intake_complete=True)


def test_chat_request_accepts_intake_complete_with_answers():
    request = ChatRequest(
        message="hello",
        intake_complete=True,
        intake={
            "objective": "Get help building or debugging a workflow",
            "building": "validator coverage",
            "maturity": "Some scripts/workflows",
            "help_needed": "Give me a technical answer I can use",
        },
    )
    assert request.intake is not None
    assert request.intake.building == "validator coverage"


def test_chat_request_accepts_intake_incomplete_without_answers():
    # Pre-intake turns are legitimate; intake is only required when the client
    # asserts completion. The redirect/intake-gate logic lives at the router.
    request = ChatRequest(message="hi", intake_complete=False)
    assert request.intake is None


@pytest.mark.asyncio
async def test_intake_questions_route_uses_extracted_questions():
    result = await get_intake_questions()

    assert result == {"questions": INTAKE_QUESTIONS}
    assert INTAKE_QUESTIONS[0]["id"] == "objective"
    assert INTAKE_QUESTIONS[0]["label"] == "What are you hoping to accomplish?"


class _FakeServices:
    def __init__(self) -> None:
        self.cloud_budget = SimpleNamespace(init=AsyncMock())
        self.local_provider = SimpleNamespace(is_healthy=AsyncMock(return_value=True))
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_lifespan_builds_and_closes_app_services(monkeypatch):
    services = _FakeServices()
    app = SimpleNamespace(state=SimpleNamespace())
    init_db = AsyncMock()

    async def build_services():
        return services

    monkeypatch.setattr(api_main, "build_app_services", build_services)
    monkeypatch.setattr(api_main, "init_db", init_db)

    async with api_main.lifespan(app):
        assert app.state.services is services
        init_db.assert_awaited_once()
        services.cloud_budget.init.assert_awaited_once()
        services.local_provider.is_healthy.assert_awaited_once()

    assert services.closed is True


@pytest.mark.asyncio
async def test_lifespan_closes_services_when_startup_fails(monkeypatch):
    services = _FakeServices()

    async def build_services():
        return services

    init_db = AsyncMock(side_effect=RuntimeError("db unavailable"))
    monkeypatch.setattr(api_main, "build_app_services", build_services)
    monkeypatch.setattr(api_main, "init_db", init_db)

    with pytest.raises(RuntimeError, match="db unavailable"):
        async with api_main.lifespan(SimpleNamespace(state=SimpleNamespace())):
            pass

    assert services.closed is True


@pytest.mark.asyncio
async def test_app_services_close_delegates_to_router():
    # AppServices.close previously had a fallback that walked the local/secondary
    # providers if the router did not expose close(). That fallback was
    # unreachable in production because LLMRouter always owns every provider via
    # its ProviderRoute list. The contract today: AppServices delegates and the
    # router handles its own pool.
    router_close = AsyncMock()
    services = api_main.AppServices(
        local_provider=SimpleNamespace(),
        secondary_provider=None,
        cloud_budget=SimpleNamespace(),
        llm_router=SimpleNamespace(close=router_close),
        orchestrator=SimpleNamespace(),
        chat_turns=SimpleNamespace(),
    )

    await services.close()

    router_close.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_metrics_records_safe_operational_counts():
    metrics = api_main.ChatMetrics()

    await metrics.record({
        "status": "completed",
        "duration_ms": 125,
        "output_tokens_estimate": 50,
        "output_chars": 200,
        "mode": "automator",
        "intent": "technical_help",
        "selected_route": "claude-sonnet-4-6",
        "selected_route_family": "cloud",
    })
    await metrics.record({
        "status": "failed",
        "duration_ms": 75,
        "output_tokens_estimate": 0,
        "output_chars": 0,
        "mode": "pipeline",
        "intent": "service_inquiry",
        "selected_route": None,
        "selected_route_family": None,
    })

    snapshot = await metrics.snapshot()
    assert snapshot["total_turns"] == 2
    assert snapshot["completed_turns"] == 1
    assert snapshot["failed_turns"] == 1
    assert snapshot["avg_duration_ms"] == 100
    assert snapshot["by_mode"] == {"automator": 1, "pipeline": 1}
    assert snapshot["by_route"]["claude-sonnet-4-6"] == 1
    assert snapshot["by_route_family"]["cloud"] == 1


@pytest.mark.asyncio
async def test_admin_health_payload_includes_chat_metrics():
    metrics = api_main.ChatMetrics()
    await metrics.record({"status": "completed", "mode": "automator", "intent": "technical_help", "selected_route_family": "local"})
    policy = SimpleNamespace(
        low_cost_enabled=False,
        claude_enabled=True,
        max_cloud_calls_per_turn=1,
        max_cloud_calls_per_session=10,
    )
    budget = SimpleNamespace(
        enabled=True,
        max_calls_per_day=20,
        max_calls_per_month=500,
        max_daily_usd=5,
        max_monthly_usd=50,
        max_input_tokens_per_call=16000,
        max_output_tokens_per_call=2048,
    )
    router = SimpleNamespace(
        routing_strategy="best_value",
        runtime_manager=None,
        routes=[],
        provider_statuses=AsyncMock(return_value=[{"callable": True, "cost_tier": "local", "name": "local"}]),
        route_overrides=lambda: {},
        circuit_breaker_status=lambda: {},
        generation_concurrency_status=AsyncMock(return_value={"active_total": 0}),
        route_diagnostics=lambda: {},
    )
    services = SimpleNamespace(
        orchestrator=SimpleNamespace(cloud_policy=policy),
        cloud_budget=SimpleNamespace(config=budget, snapshot=AsyncMock(return_value={})),
        llm_router=router,
        chat_metrics=metrics,
    )

    payload = await api_main._admin_health_payload(services)

    assert payload["metrics"]["chat"]["total_turns"] == 1
    assert payload["metrics"]["chat"]["by_route_family"]["local"] == 1
    assert payload["generation_concurrency"] == {"active_total": 0}


def test_get_app_services_returns_503_when_uninitialized():
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    with pytest.raises(api_main.HTTPException) as exc:
        api_main.get_app_services(request)

    assert exc.value.status_code == 503


def test_admin_token_requires_configured_secret(monkeypatch):
    monkeypatch.setenv("ADMIN_POLICY_TOKEN", "secret")
    request = SimpleNamespace(headers={"host": "example.com"}, client=SimpleNamespace(host="203.0.113.10"))

    with pytest.raises(api_main.HTTPException) as exc:
        api_main.require_admin_token(request, x_admin_token="wrong")

    assert exc.value.status_code == 401
    api_main.require_admin_token(request, x_admin_token="secret")


def test_admin_token_allows_local_dev_fallback_when_unconfigured(monkeypatch):
    monkeypatch.delenv("ADMIN_POLICY_TOKEN", raising=False)
    monkeypatch.setenv("ALLOW_LOOPBACK_DEV_ADMIN", "1")
    request = SimpleNamespace(headers={"host": "localhost:8000"}, client=SimpleNamespace(host="127.0.0.1"))

    api_main.require_admin_token(request, x_admin_token="local-dev-admin")


def test_admin_token_rejects_spoofed_localhost_host_when_unconfigured(monkeypatch):
    monkeypatch.delenv("ADMIN_POLICY_TOKEN", raising=False)
    monkeypatch.setenv("ALLOW_LOOPBACK_DEV_ADMIN", "1")
    request = SimpleNamespace(headers={"host": "localhost:8000"}, client=SimpleNamespace(host="203.0.113.10"))

    with pytest.raises(api_main.HTTPException) as exc:
        api_main.require_admin_token(request, x_admin_token="local-dev-admin")

    assert exc.value.status_code == 503


def test_admin_token_loopback_fallback_refused_without_explicit_optin(monkeypatch):
    # The production case: no policy token AND no opt-in flag. Behind a reverse
    # proxy every client looks loopback, so the fallback must NOT fire.
    monkeypatch.delenv("ADMIN_POLICY_TOKEN", raising=False)
    monkeypatch.delenv("ALLOW_LOOPBACK_DEV_ADMIN", raising=False)
    request = SimpleNamespace(headers={"host": "localhost:8000"}, client=SimpleNamespace(host="127.0.0.1"))

    with pytest.raises(api_main.HTTPException) as exc:
        api_main.require_admin_token(request, x_admin_token="local-dev-admin")

    assert exc.value.status_code == 503


def test_admin_token_constant_time_compare_still_rejects_wrong_token(monkeypatch):
    monkeypatch.setenv("ADMIN_POLICY_TOKEN", "the-real-token")
    request = SimpleNamespace(headers={"host": "example.com"}, client=SimpleNamespace(host="203.0.113.10"))

    with pytest.raises(api_main.HTTPException) as exc:
        api_main.require_admin_token(request, x_admin_token="the-real-tokeX")
    assert exc.value.status_code == 401

    # Empty token never satisfies compare_digest.
    with pytest.raises(api_main.HTTPException):
        api_main.require_admin_token(request, x_admin_token="")

    api_main.require_admin_token(request, x_admin_token="the-real-token")


def test_admin_policy_rejects_unknown_routing_strategy():
    with pytest.raises(ValueError, match="Unknown routing_strategy"):
        api_main.AdminPolicyPatch(routing_strategy="typo_strategy")


def test_chat_request_validates_terminal_session_id():
    request = ChatRequest(
        message=" hello\x00 ",
        terminal_session_id=" terminal-session-1\x00 ",
        intake={
            "objective": " Automate tickets\x00 ",
            "building": " PSA cleanup\x07 ",
            "maturity": " Just starting\x1f ",
            "help_needed": " Automator Assistance\x00 ",
        },
    )

    assert request.message == "hello"
    assert request.terminal_session_id == "terminal-session-1"
    assert request.intake is not None
    assert request.intake.objective == "Automate tickets"
    assert request.intake.building == "PSA cleanup"
    with pytest.raises(ValueError):
        ChatRequest(message="hello", terminal_session_id="short")


def test_chat_request_rejects_oversized_message():
    with pytest.raises(ValueError):
        ChatRequest(message="x" * (api_main.CHAT_MAX_MESSAGE_CHARS + 1))


@pytest.mark.asyncio
async def test_chat_body_size_limit_rejects_actual_body(monkeypatch):
    monkeypatch.setattr(api_chat, "CHAT_MAX_BODY_BYTES", 5)
    request = SimpleNamespace(
        url=SimpleNamespace(path="/chat/stream"),
        body=AsyncMock(return_value=b"x" * 6),
    )

    async def call_next(_request):
        raise AssertionError("oversized chat body reached the route")

    response = await api_main.chat_body_size_limit(request, call_next)

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_state_changing_origin_guard_rejects_untrusted_origin(monkeypatch):
    monkeypatch.setattr(api_main, "CORS_ALLOWED_ORIGINS", ("https://example.com",))
    request = SimpleNamespace(
        method="POST",
        url=SimpleNamespace(path="/contact"),
        headers={"origin": "https://evil.example"},
    )

    async def call_next(_request):
        raise AssertionError("untrusted origin reached the route")

    response = await api_main.state_changing_origin_guard(request, call_next)

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_state_changing_origin_guard_allows_same_site_origin(monkeypatch):
    monkeypatch.setattr(api_main, "CORS_ALLOWED_ORIGINS", ("https://example.com",))
    request = SimpleNamespace(
        method="POST",
        url=SimpleNamespace(path="/intake/submissions"),
        headers={"origin": "https://example.com"},
    )

    async def call_next(_request):
        return SimpleNamespace(status_code=201)

    response = await api_main.state_changing_origin_guard(request, call_next)

    assert response.status_code == 201


def test_allowed_origins_uses_cached_cors_source(monkeypatch):
    monkeypatch.setattr(api_main, "CORS_ALLOWED_ORIGINS", ("https://example.com", "https://www.example.com"))
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://evil.example")

    assert api_main._allowed_origins() == {"https://example.com", "https://www.example.com"}


@pytest.mark.asyncio
async def test_chat_usage_snapshot_counts_requests_and_tokens():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    session_maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_maker() as session:
            await db_add_chat_usage_event(
                session,
                ip_address="203.0.113.10",
                session_key="terminal:test-session",
                input_tokens=10,
            )
            # Output-only records count toward token totals but not request counts.
            await db_add_chat_usage_event(
                session,
                ip_address="203.0.113.10",
                session_key="terminal:test-session",
                output_tokens=20,
            )
            snapshot = await db_chat_usage_snapshot(
                session,
                ip_address="203.0.113.10",
                session_key="terminal:test-session",
                ip_rate_since=datetime.now(timezone.utc) - timedelta(hours=1),
                session_rate_since=datetime.now(timezone.utc) - timedelta(hours=1),
                token_since=datetime.now(timezone.utc) - timedelta(hours=1),
            )
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.drop_all)
        await engine.dispose()
    assert snapshot == {"ip_requests": 1, "session_requests": 1, "daily_tokens": 30}


@pytest.mark.asyncio
async def test_chat_stream_rejects_ip_rate_limit(monkeypatch):
    async def chat_usage_snapshot(*args, **kwargs):
        return {"ip_requests": api_main.CHAT_IP_LIMIT, "session_requests": 0, "daily_tokens": 0}

    monkeypatch.setattr(api_chat, "chat_usage_snapshot", chat_usage_snapshot)
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))

    with pytest.raises(api_main.HTTPException) as exc:
        await api_main.chat_stream(ChatRequest(message="hello"), request, session=AsyncMock())

    assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_chat_stream_rejects_daily_token_cap(monkeypatch):
    async def chat_usage_snapshot(*args, **kwargs):
        return {"ip_requests": 0, "session_requests": 0, "daily_tokens": api_main.CHAT_DAILY_TOKEN_LIMIT}

    monkeypatch.setattr(api_chat, "chat_usage_snapshot", chat_usage_snapshot)
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))

    with pytest.raises(api_main.HTTPException) as exc:
        await api_main.chat_stream(ChatRequest(message="hello"), request, session=AsyncMock())

    assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_kb_startup_status_logs_empty_store(monkeypatch, caplog):
    monkeypatch.setattr(api_main, "vectorstore_document_count", lambda: 0)

    await api_main._log_kb_startup_status()

    assert "Knowledge base vector store is empty" in caplog.text


@pytest.mark.asyncio
async def test_chat_stream_persists_assistant_message_before_done(monkeypatch):
    order: list[str] = []
    usage: list[dict[str, object]] = []

    class _ChatTurns:
        def plan(self, mode, message, intake_complete, intake=None):
            return SimpleNamespace(
                selected_mode=SimpleNamespace(value="automator"),
                stage=SimpleNamespace(value="answer"),
                retrieval_tool=None,
            )

        async def stream(self, decision, user_message, history, session_id):
            order.append("stream:hel")
            yield "hel"
            order.append("stream:lo")
            yield "lo"

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(services=SimpleNamespace(chat_turns=_ChatTurns()))),
        headers={"referer": "https://example.com/terminal", "user-agent": "pytest"},
        client=SimpleNamespace(host="127.0.0.1"),
    )

    async def create_conversation(mode, session):
        return SimpleNamespace(id="conv-1")

    async def add_message(conv_id, role, content, session):
        return SimpleNamespace(id=f"{role}-1")

    async def get_messages(conv_id, session):
        return [SimpleNamespace(role="user", content="hello")]

    async def save_assistant(conv_id, role, content):
        order.append(f"save:{role}:{content}")

    async def add_chat_usage_event(*args, **kwargs):
        usage.append(kwargs)
        return SimpleNamespace(id=f"usage-{len(usage)}")

    async def add_chat_usage_event_in_new_session(**kwargs):
        usage.append(kwargs)
        return SimpleNamespace(id=f"usage-{len(usage)}")

    monkeypatch.setattr(api_chat, "create_conversation", create_conversation)
    monkeypatch.setattr(api_chat, "add_message", add_message)
    monkeypatch.setattr(api_chat, "get_messages", get_messages)
    monkeypatch.setattr(api_chat, "add_message_in_new_session", save_assistant)
    monkeypatch.setattr(api_chat, "add_chat_usage_event", add_chat_usage_event)
    monkeypatch.setattr(api_chat, "add_chat_usage_event_in_new_session", add_chat_usage_event_in_new_session)

    response = await api_main.chat_stream(
        # These tests cover message persistence, error handling, and logging in
        # chat_stream. They do not exercise save_intake_record, so they keep
        # intake_complete=False to avoid the validator-enforced intake-required
        # contract. The dedicated terminal intake test below uses the full path.
        ChatRequest(message="hello", intake_complete=False),
        request,
        session=object(),
    )

    events = []
    async for raw in response.body_iterator:
        text = raw.decode() if isinstance(raw, bytes) else raw
        events.append(text)
        if "[DONE]" in text:
            order.append("done")

    assert any('"content": "hel"' in event for event in events)
    assert any('"content": "lo"' in event for event in events)
    assert usage[0]["input_tokens"] > 0
    assert usage[1]["output_tokens"] > 0
    assert usage[1]["conversation_id"] == "conv-1"
    assert order == ["stream:hel", "stream:lo", "save:assistant:hello", "done"]


@pytest.mark.asyncio
async def test_chat_stream_logs_safe_structured_turn_metadata(monkeypatch, caplog):
    class _Router:
        def route_diagnostics(self):
            return {
                "routing_strategy": "best_value",
                "selected_route": "claude-sonnet-4-6",
                "ordered_routes": ["claude-sonnet-4-6"],
                "empty_reason": "",
                "candidates": [{"name": "claude-sonnet-4-6", "cost_tier": "premium"}],
            }

    class _ChatTurns:
        def plan(self, mode, message, intake_complete, intake=None):
            return SimpleNamespace(
                selected_mode=SimpleNamespace(value="pipeline"),
                stage=SimpleNamespace(value="active"),
                intent=SimpleNamespace(value="service_inquiry"),
                tone=SimpleNamespace(value="neutral"),
                response_policy=SimpleNamespace(value="local_then_claude_if_high_value"),
                retrieval_required=False,
                retrieval_tool=None,
                tool_execution_mode=SimpleNamespace(value="disabled"),
                cloud_allowed=True,
                service_handoff_suggested=True,
            )

        async def stream(self, decision, user_message, history, session_id):
            yield "assistant body should not be logged"

    metrics = api_main.ChatMetrics()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(services=SimpleNamespace(chat_turns=_ChatTurns(), llm_router=_Router(), chat_metrics=metrics))),
        headers={"referer": "https://example.com/terminal", "user-agent": "pytest"},
        client=SimpleNamespace(host="127.0.0.1"),
    )

    async def create_conversation(mode, session):
        return SimpleNamespace(id="conv-1")

    async def add_message(conv_id, role, content, session):
        return SimpleNamespace(id=f"{role}-1")

    async def get_messages(conv_id, session):
        return []

    async def save_assistant(conv_id, role, content):
        return SimpleNamespace(id="assistant-1")

    monkeypatch.setattr(api_chat, "create_conversation", create_conversation)
    monkeypatch.setattr(api_chat, "add_message", add_message)
    monkeypatch.setattr(api_chat, "get_messages", get_messages)
    monkeypatch.setattr(api_chat, "add_message_in_new_session", save_assistant)
    # _log_chat_turn now lives in api.chat after the api/main.py split.
    caplog.set_level(logging.INFO, logger=api_chat.__name__)

    response = await api_main.chat_stream(
        ChatRequest(message="sensitive workflow details", intake_complete=False),
        request,
        session=object(),
    )
    async for _ in response.body_iterator:
        pass

    structured = [record.message for record in caplog.records if record.message.startswith("chat_turn ")]
    assert structured
    payload = json.loads(structured[-1].split("chat_turn ", 1)[1])
    assert payload["event"] == "chat_turn"
    assert payload["status"] == "completed"
    assert payload["mode"] == "pipeline"
    assert payload["selected_route"] == "claude-sonnet-4-6"
    assert payload["selected_route_family"] == "cloud"
    assert payload["cloud_allowed"] is True
    assert "request_id" in payload
    assert (await metrics.snapshot())["by_route_family"]["cloud"] == 1
    assert "sensitive workflow details" not in caplog.text
    assert "assistant body should not be logged" not in caplog.text


@pytest.mark.asyncio
async def test_chat_stream_returns_generic_error_without_internal_details(monkeypatch):
    class _ChatTurns:
        def plan(self, mode, message, intake_complete, intake=None):
            return SimpleNamespace(
                selected_mode=SimpleNamespace(value="automator"),
                stage=SimpleNamespace(value="answer"),
                retrieval_tool=None,
            )

        async def stream(self, decision, user_message, history, session_id):
            raise RuntimeError("secret provider detail")
            yield ""

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(services=SimpleNamespace(chat_turns=_ChatTurns()))),
        headers={"referer": "https://example.com/terminal", "user-agent": "pytest"},
        client=SimpleNamespace(host="127.0.0.1"),
    )

    async def create_conversation(mode, session):
        return SimpleNamespace(id="conv-1")

    async def add_message(conv_id, role, content, session):
        return SimpleNamespace(id=f"{role}-1")

    async def get_messages(conv_id, session):
        return [SimpleNamespace(role="user", content="hello")]

    monkeypatch.setattr(api_chat, "create_conversation", create_conversation)
    monkeypatch.setattr(api_chat, "add_message", add_message)
    monkeypatch.setattr(api_chat, "get_messages", get_messages)

    response = await api_main.chat_stream(
        # These tests cover message persistence, error handling, and logging in
        # chat_stream. They do not exercise save_intake_record, so they keep
        # intake_complete=False to avoid the validator-enforced intake-required
        # contract. The dedicated terminal intake test below uses the full path.
        ChatRequest(message="hello", intake_complete=False),
        request,
        session=object(),
    )

    body = ""
    async for raw in response.body_iterator:
        body += raw.decode() if isinstance(raw, bytes) else raw

    assert "The chat stream failed. Please try again." in body
    assert "secret provider detail" not in body


@pytest.mark.asyncio
async def test_chat_stream_reports_generic_error_when_assistant_save_fails(monkeypatch):
    class _ChatTurns:
        def plan(self, mode, message, intake_complete, intake=None):
            return SimpleNamespace(
                selected_mode=SimpleNamespace(value="automator"),
                stage=SimpleNamespace(value="answer"),
                retrieval_tool=None,
            )

        async def stream(self, decision, user_message, history, session_id):
            yield "hello"

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(services=SimpleNamespace(chat_turns=_ChatTurns())))
    )

    async def create_conversation(mode, session):
        return SimpleNamespace(id="conv-1")

    async def add_message(conv_id, role, content, session):
        return SimpleNamespace(id=f"{role}-1")

    async def get_messages(conv_id, session):
        return [SimpleNamespace(role="user", content="hello")]

    async def save_assistant(conv_id, role, content):
        raise RuntimeError("database secret")

    monkeypatch.setattr(api_chat, "create_conversation", create_conversation)
    monkeypatch.setattr(api_chat, "add_message", add_message)
    monkeypatch.setattr(api_chat, "get_messages", get_messages)
    monkeypatch.setattr(api_chat, "add_message_in_new_session", save_assistant)

    response = await api_main.chat_stream(
        # These tests cover message persistence, error handling, and logging in
        # chat_stream. They do not exercise save_intake_record, so they keep
        # intake_complete=False to avoid the validator-enforced intake-required
        # contract. The dedicated terminal intake test below uses the full path.
        ChatRequest(message="hello", intake_complete=False),
        request,
        session=object(),
    )

    body = ""
    async for raw in response.body_iterator:
        body += raw.decode() if isinstance(raw, bytes) else raw

    assert '"content": "hello"' in body
    assert "database secret" not in body
    assert "The chat stream failed. Please try again." not in body
    assert "[DONE]" in body


@pytest.mark.asyncio
async def test_chat_stream_catalogs_intake_answers(monkeypatch):
    saved = {}

    class _ChatTurns:
        def plan(self, mode, message, intake_complete, intake=None):
            saved["planned_intake"] = intake
            return SimpleNamespace(
                selected_mode=SimpleNamespace(value="pipeline"),
                stage=SimpleNamespace(value="answer"),
                retrieval_tool=None,
            )

        async def stream(self, decision, user_message, history, session_id):
            yield "ok"

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(services=SimpleNamespace(chat_turns=_ChatTurns()))),
        headers={"referer": "https://example.com/terminal", "user-agent": "pytest"},
        client=SimpleNamespace(host="127.0.0.1"),
    )

    async def create_conversation(mode, session):
        return SimpleNamespace(id="conv-1")

    async def add_message(conv_id, role, content, session):
        return SimpleNamespace(id=f"{role}-1")

    async def get_messages(conv_id, session):
        return []

    async def save_assistant(conv_id, role, content):
        return SimpleNamespace(id="assistant-1")

    async def save_intake_record(conv_id, selected_mode, intake, session):
        saved["catalog"] = {"conv_id": conv_id, "selected_mode": selected_mode, "intake": intake}

    async def save_terminal_intake_submission(terminal_session_id, selected_mode, intake, session, **metadata):
        saved["terminal_intake"] = {
            "terminal_session_id": terminal_session_id,
            "selected_mode": selected_mode,
            "intake": intake,
            "metadata": metadata,
        }

    monkeypatch.setattr(api_chat, "create_conversation", create_conversation)
    monkeypatch.setattr(api_chat, "add_message", add_message)
    monkeypatch.setattr(api_chat, "get_messages", get_messages)
    monkeypatch.setattr(api_chat, "add_message_in_new_session", save_assistant)
    monkeypatch.setattr(api_chat, "save_intake_record", save_intake_record)
    monkeypatch.setattr(api_chat, "save_terminal_intake_submission", save_terminal_intake_submission)

    response = await api_main.chat_stream(
        ChatRequest(
            message="hello",
            terminal_session_id="terminal-session-1",
            intake_complete=True,
            intake={
                "objective": "Explore automation support or consulting",
                "building": "PSA cleanup",
                "maturity": "Just starting",
                "help_needed": "Service Inquiry",
            },
        ),
        request,
        session=object(),
    )

    async for _ in response.body_iterator:
        pass

    assert saved["planned_intake"]["objective"] == "Explore automation support or consulting"
    assert saved["catalog"]["conv_id"] == "conv-1"
    assert saved["catalog"]["selected_mode"] == "pipeline"
    assert saved["catalog"]["intake"]["building"] == "PSA cleanup"
    assert saved["terminal_intake"]["terminal_session_id"] == "terminal-session-1"
    assert saved["terminal_intake"]["metadata"]["conversation_id"] == "conv-1"
    assert saved["terminal_intake"]["metadata"]["source_path"] == "/chat/stream"
    assert saved["terminal_intake"]["metadata"]["count_submission"] is False


@pytest.mark.asyncio
async def test_lifespan_logs_service_build_failure(monkeypatch):
    async def build_services():
        raise RuntimeError("bad config")

    monkeypatch.setattr(api_main, "build_app_services", build_services)

    with pytest.raises(RuntimeError, match="bad config"):
        async with api_main.lifespan(SimpleNamespace(state=SimpleNamespace())):
            pass