from types import SimpleNamespace

import httpx
import pytest

import api.chat as api_chat
import api.intake as api_intake
import api.main as api_main
from core.chat_orchestrator import ChatTurnOrchestrator
from core.orchestrator import Orchestrator


class _Router:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def chat(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools or [], "kwargs": kwargs})
        return {"content": "terminal ok"}

    async def stream_chat(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools or [], "kwargs": kwargs})
        yield "terminal ok"


class _Retriever:
    async def retrieve(self, user_message: str, tool_name: str):
        raise AssertionError("retrieval should not run for this smoke path")


@pytest.fixture
def terminal_http_app(monkeypatch):
    router = _Router()
    chat_turns = ChatTurnOrchestrator(Orchestrator(), router, _Retriever())
    saved: dict[str, object] = {"messages": [], "usage": [], "intake": []}

    api_main.app.state.services = SimpleNamespace(chat_turns=chat_turns)

    async def fake_session():
        return object()

    async def create_conversation(mode, session):
        return SimpleNamespace(id="conv-http-1")

    async def add_message(conv_id, role, content, session):
        saved["messages"].append({"conv_id": conv_id, "role": role, "content": content})
        return SimpleNamespace(id=f"{role}-1")

    async def get_messages(conv_id, session):
        return [SimpleNamespace(role=item["role"], content=item["content"]) for item in saved["messages"]]

    async def save_assistant(conv_id, role, content):
        saved["messages"].append({"conv_id": conv_id, "role": role, "content": content})

    async def save_intake_record(conv_id, selected_mode, intake, session):
        saved["intake"].append({"conv_id": conv_id, "selected_mode": selected_mode, "intake": intake})

    async def save_terminal_intake_submission(terminal_session_id, selected_mode, intake, session, **metadata):
        saved["intake"].append({"terminal_session_id": terminal_session_id, "selected_mode": selected_mode, "intake": intake, "metadata": metadata})
        return SimpleNamespace(id="intake-http-1")

    async def chat_usage_snapshot(*args, **kwargs):
        return {"ip_requests": 0, "session_requests": 0, "daily_tokens": 0}

    async def add_chat_usage_event(*args, **kwargs):
        saved["usage"].append(kwargs)
        return SimpleNamespace(id=f"usage-{len(saved['usage'])}")

    async def latest_submission(session, terminal_session_id):
        return None

    async def enforce_rate_limits(session, terminal_session_id, ip_address, existing):
        return None

    monkeypatch.setattr(api_chat, "create_conversation", create_conversation)
    monkeypatch.setattr(api_chat, "add_message", add_message)
    monkeypatch.setattr(api_chat, "get_messages", get_messages)
    monkeypatch.setattr(api_chat, "add_message_in_new_session", save_assistant)
    monkeypatch.setattr(api_chat, "save_intake_record", save_intake_record)
    monkeypatch.setattr(api_chat, "save_terminal_intake_submission", save_terminal_intake_submission)
    monkeypatch.setattr(api_chat, "chat_usage_snapshot", chat_usage_snapshot)
    monkeypatch.setattr(api_chat, "add_chat_usage_event", add_chat_usage_event)
    monkeypatch.setattr(api_chat, "add_chat_usage_event_in_new_session", add_chat_usage_event)
    monkeypatch.setattr(api_intake, "_latest_submission", latest_submission)
    monkeypatch.setattr(api_intake, "_enforce_rate_limits", enforce_rate_limits)
    monkeypatch.setattr(api_intake, "save_terminal_intake_submission", save_terminal_intake_submission)
    api_main.app.dependency_overrides[api_main.get_session] = fake_session
    api_main.app.dependency_overrides[api_intake.get_session] = fake_session

    yield api_main.app, router, saved

    api_main.app.dependency_overrides.clear()


def _sse_data(body: str) -> list[str]:
    events: list[str] = []
    for block in body.split("\n\n"):
        lines = [line.replace("data: ", "", 1) for line in block.splitlines() if line.startswith("data:")]
        if lines:
            events.append("\n".join(lines))
    return events


@pytest.mark.asyncio
async def test_terminal_http_intake_to_chat_stream_contract(terminal_http_app):
    app, router, saved = terminal_http_app
    transport = httpx.ASGITransport(app=app)
    answers = {
        "objective": "Explore automation support or consulting",
        "building": "HTTP terminal smoke",
        "maturity": "Just starting",
        "help_needed": "Service Inquiry",
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        questions = await client.get("/intake/questions")
        intake = await client.post("/intake/submissions", json={
            "terminal_session_id": "terminal-http-1",
            "selected_mode": "pipeline",
            "intake": answers,
            "source_path": "/terminal",
        })
        stream = await client.post("/chat/stream", json={
            "message": "hello",
            "mode": "pipeline",
            "terminal_session_id": "terminal-http-1",
            "intake_complete": True,
            "intake": answers,
        })

    assert questions.status_code == 200
    assert questions.json()["questions"][0]["label"] == "What are you hoping to accomplish?"
    assert questions.json()["questions"][3]["label"] == "What would be most useful right now?"
    assert "Automator Assistance" not in questions.json()["questions"][3]["options"]
    assert intake.status_code == 201
    assert intake.json() == {"status": "received", "id": "intake-http-1"}
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")

    events = _sse_data(stream.text)
    assert '"conversation_id": "conv-http-1"' in events[0]
    assert '"selected_mode": "pipeline"' in events[0]
    assert any('"content": "terminal ok"' in event for event in events)
    assert events[-1] == "[DONE]"
    assert router.calls
    assert saved["messages"][-1]["role"] == "assistant"
    assert saved["messages"][-1]["content"] == "terminal ok"


@pytest.mark.asyncio
async def test_terminal_http_chat_is_gated_until_intake_complete(terminal_http_app):
    app, router, _saved = terminal_http_app
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        stream = await client.post("/chat/stream", json={"message": "hello", "mode": "automator"})

    assert stream.status_code == 200
    assert "Before we chat, please confirm your objective." in stream.text
    assert "[DONE]" in stream.text
    assert router.calls == []