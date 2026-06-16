"""Targeted tests for chat_orchestrator helper methods and tool-loop misses.

The main suite drives the orchestrator end-to-end; this reaches the smaller
decision helpers (route-aware tool-loop downgrade, router-task classification,
directional fallbacks, the service CTA tool result) and the backend-retrieval
recovery that fires when a model tool loop returns no tool calls.
"""

from __future__ import annotations

from gestaltworkframe.core.chat_orchestrator import ChatTurnOrchestrator, ServiceCtaToolArgs
from gestaltworkframe.core.orchestrator import Orchestrator
from gestaltworkframe.core.personas import get_persona
from gestaltworkframe.core.policy import (
    ChatMode,
    RouteFrame,
    RoutingDecision,
    ToolExecutionMode,
    UserIntent,
)
from gestaltworkframe.core.retrieval import RetrievalResult
from gestaltworkframe.core.tool_policy import REFERENCE_SEARCH, WORKFLOW_PATTERN_SEARCH


class _Router:
    def __init__(self, content="Answer\nSource: docs/automation.md", responses=None):
        self.content = content
        self.responses = list(responses or [])
        self.calls = []

    async def chat(self, messages, tools=None, force_secondary=False, cloud_allowed=False, session_id=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools})
        if self.responses:
            return self.responses.pop(0)
        return {"content": self.content}


class _Retriever:
    def __init__(self, content="Result 1\nSource: docs/automation.md\nContent:\nCTX info"):
        self.content = content
        self.calls = []

    async def retrieve(self, query, tool_name, limit=5):
        self.calls.append({"query": query, "tool_name": tool_name, "limit": limit})
        return RetrievalResult(tool_name=tool_name, query=query, content=self.content)


class _CapableRouter(_Router):
    def __init__(self, capable, **kw):
        super().__init__(**kw)
        self._capable = capable

    def has_tool_capable_route(self, **_kw):
        return self._capable


def _decision(**overrides) -> RoutingDecision:
    base = dict(selected_mode=ChatMode.AUTOMATOR, intent=UserIntent.TECHNICAL_HELP)
    base.update(overrides)
    return RoutingDecision(**base)


def _engine(router=None, retriever=None) -> ChatTurnOrchestrator:
    return ChatTurnOrchestrator(Orchestrator(), router or _Router(), retriever or _Retriever())


# --- _route_aware_tool_loop_decision ---------------------------------------


def test_route_aware_noop_when_not_tool_loop():
    engine = _engine()
    decision = _decision()  # tool_execution_mode defaults to DISABLED
    assert engine._route_aware_tool_loop_decision(decision, "msg") is decision


def test_route_aware_noop_when_router_lacks_capability_probe():
    engine = ChatTurnOrchestrator(Orchestrator(), object(), _Retriever())
    decision = _decision(
        tool_execution_mode=ToolExecutionMode.MODEL_TOOL_LOOP,
        tool_loop_requires_route=True,
        provider_tools=[REFERENCE_SEARCH],
    )
    assert engine._route_aware_tool_loop_decision(decision, "msg") is decision


def test_route_aware_keeps_loop_when_route_is_capable():
    engine = _engine(router=_CapableRouter(True))
    decision = _decision(
        tool_execution_mode=ToolExecutionMode.MODEL_TOOL_LOOP,
        tool_loop_requires_route=True,
        provider_tools=[REFERENCE_SEARCH],
    )
    assert engine._route_aware_tool_loop_decision(decision, "msg") is decision


def test_route_aware_downgrades_to_backend_when_no_capable_route():
    engine = _engine(router=_CapableRouter(False))
    decision = _decision(
        tool_execution_mode=ToolExecutionMode.MODEL_TOOL_LOOP,
        tool_loop_requires_route=True,
        provider_tools=[REFERENCE_SEARCH],
    )
    out = engine._route_aware_tool_loop_decision(decision, "msg")
    assert out is not decision
    assert out.provider_tools == []
    assert out.tool_execution_mode == ToolExecutionMode.BACKEND_RETRIEVAL_ONLY
    assert out.tool_loop_requires_route is False
    assert "backend_fallback_no_capable_route" in out.reason


# --- _router_task ----------------------------------------------------------


def test_router_task_prefers_explicit_frame_task():
    engine = _engine()
    decision = _decision(frame=RouteFrame(task="explicit_task"))
    assert engine._router_task(decision, "anything") == "explicit_task"


def test_router_task_high_value_for_service_handoff():
    engine = _engine()
    decision = _decision(frame=RouteFrame(task=""), service_handoff_suggested=True)
    assert engine._router_task(decision, "msg") == "high_value_service_inquiry"


def test_router_task_workflow_examples_vs_debugging():
    engine = _engine()
    examples = _decision(frame=RouteFrame(task=""), retrieval_tool=WORKFLOW_PATTERN_SEARCH)
    debugging = _decision(frame=RouteFrame(task=""), retrieval_tool=WORKFLOW_PATTERN_SEARCH)
    assert engine._router_task(examples, "show me a sample bundle") == "workflow_examples"
    assert engine._router_task(debugging, "why does my run break") == "workflow_debugging"


def test_router_task_jinja_help_and_intent_default():
    engine = _engine()
    jinja = _decision(frame=RouteFrame(task=""), retrieval_tool=REFERENCE_SEARCH)
    other = _decision(frame=RouteFrame(task=""), retrieval_tool=None)
    assert engine._router_task(jinja, "how do I use ctx here") == "jinja_help"
    assert engine._router_task(other, "general question") == UserIntent.TECHNICAL_HELP.value


# --- _general_direction_fallback -------------------------------------------


def test_general_direction_fallback_varies_with_repeat():
    engine = _engine()
    decision = _decision(intake={"building": "the PSA sync"})
    first = engine._general_direction_fallback(decision, 0)
    again = engine._general_direction_fallback(decision, 1)
    assert first.startswith("I did not find enough verified source context")
    assert "the PSA sync" in first
    assert again.startswith("I still don't have a verified source")
    assert first != again


# --- _service_cta_tool_result ----------------------------------------------


def test_service_cta_tool_result_includes_optional_fields():
    engine = _engine()
    minimal = engine._service_cta_tool_result(ServiceCtaToolArgs(summary="need help"))
    assert "Service handoff target:" in minimal
    assert "Summary: need help" in minimal
    assert "Urgency:" not in minimal

    full = engine._service_cta_tool_result(
        ServiceCtaToolArgs(summary="need help", urgency="high", desired_outcome="ship it")
    )
    assert "Urgency: high" in full
    assert "Desired outcome: ship it" in full


# --- _backend_retrieval_answer_after_tool_loop_miss ------------------------


async def test_backend_retrieval_recovery_returns_none_without_retrieval():
    engine = _engine()
    decision = _decision(retrieval_required=False)
    persona = get_persona("automator")
    out = await engine._backend_retrieval_answer_after_tool_loop_miss(
        decision, persona, [], "msg", "conv", None
    )
    assert out is None


async def test_backend_retrieval_recovery_runs_backend_answer():
    router = _Router(content="Backend answer\nSource: docs/automation.md")
    retriever = _Retriever()
    engine = _engine(router=router, retriever=retriever)
    decision = _decision(
        retrieval_required=True, retrieval_tool=REFERENCE_SEARCH, allowed_tools=[REFERENCE_SEARCH]
    )
    persona = get_persona("automator")

    out = await engine._backend_retrieval_answer_after_tool_loop_miss(
        decision, persona, [], "how do I use CTX", "conv", None
    )

    assert out is not None
    text, retrieval = out
    assert text.startswith("Backend answer")
    assert retrieval is not None
    # It performed its own retrieval since none was passed in.
    assert retriever.calls and retriever.calls[0]["tool_name"] == REFERENCE_SEARCH


async def test_model_tool_loop_with_no_tool_calls_falls_back_to_backend_retrieval():
    # First (and only) model response carries no tool_calls, so the tool loop
    # immediately recovers via backend retrieval rather than returning bare text.
    router = _Router(responses=[{"content": "Answer\nSource: docs/automation.md"}])
    retriever = _Retriever()
    engine = _engine(router=router, retriever=retriever)
    decision = RoutingDecision(
        selected_mode=ChatMode.AUTOMATOR,
        intent=UserIntent.TECHNICAL_HELP,
        retrieval_required=True,
        retrieval_tool=REFERENCE_SEARCH,
        allowed_tools=[REFERENCE_SEARCH],
        provider_tools=[REFERENCE_SEARCH],
        tool_execution_mode=ToolExecutionMode.MODEL_TOOL_LOOP,
        max_model_calls_per_turn=2,
    )

    result = await engine.run(decision, "how do I use CTX", [], "conv-1")
    assert "Source:" in result.content
