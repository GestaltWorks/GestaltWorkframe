import os

from core.policy import (
    ChatMode,
    CloudSpendPolicy,
    ConversationStage,
    ResponsePolicy,
    RoutingDecision,
    SearchPlan,
    ToneSignal,
    ToolExecutionMode,
    UserIntent,
    UserNeed,
)
from core.routing_frame import classify_route
from core.tool_policy import allowed_tools_for_mode, retrieval_tool_for


# Two keyword sets survive after Phase 2 deletions: TECHNICAL_TERMS gates the
# retrieval branch in _needs_retrieval; DIRECT_SERVICE_HANDOFF_TERMS gates the
# explicit service-help branch in _should_suggest_service_handoff. The rest of
# the orchestrator's classification work happens through core.routing_frame,
# which owns the canonical signal taxonomy.
TECHNICAL_TERMS = (
    "jinja",
    "workflow",
    "bundle",
    ".bundle.json",
    "template",
    "ctx",
    "tasks",
    "crate",
    "api",
    "n8n",
    "webhook",
)
DIRECT_SERVICE_HANDOFF_TERMS = (
    "hire",
    "consult",
    "consulting",
    "build this",
    "debug this for me",
    "fix this for me",
    "do this for me",
    "work with you",
    "bring you in",
    "contact",
    "demo",
)
# Soft service offer triggers (Phase D2): the user expresses a
# build/implement/integrate intent without explicitly asking to hire.
# In Automator/Educator mode the assistant is allowed to add a single
# "Want help getting this implemented?" bridge sentence when both this
# and a relevant retrieval hit are present. Broad enough to catch real
# intent, narrow enough not to fire on every technical question.
BUILD_INTENT_TERMS = (
    "i want to build",
    "i'm trying to build",
    "im trying to build",
    "trying to build",
    "want to build",
    "need to build",
    "how do i build",
    "how would i build",
    "how do i implement",
    "how would i implement",
    "i want to implement",
    "need to implement",
    "how do i set up",
    "how do i deploy",
    "how would i deploy",
    "where can i find",
    "is there a workflow",
    "is there a bundle",
    "is there an example",
    "is there a template",
    "looking for a workflow",
    "looking for an example",
    "looking for a template",
    "need a workflow",
    "need an example",
    "how do i automate",
)
MODE_ALIASES = {"sales": ChatMode.SERVICE.value}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Orchestrator:
    def __init__(self, cloud_policy: CloudSpendPolicy | None = None, model_tool_loop_enabled: bool | None = None):
        self.cloud_policy = cloud_policy or CloudSpendPolicy()
        self.model_tool_loop_enabled = _env_bool("ENABLE_MODEL_TOOL_LOOP") if model_tool_loop_enabled is None else model_tool_loop_enabled

    def decide(
        self,
        starting_mode: str,
        message: str,
        intake_complete: bool = True,
        intake: dict[str, str] | None = None,
    ) -> RoutingDecision:
        normalized = self._normalize(message)
        intake = self._clean_intake(intake)

        if not intake_complete:
            return self._intake_decision(starting_mode, normalized)

        frame, intent, tone = classify_route(starting_mode, normalized, intake)
        if intent in {UserIntent.UNKNOWN, UserIntent.SMALL_TALK} and intake:
            intent = self._detect_intake_intent(intake) or intent

        if intent == UserIntent.OUT_OF_SCOPE:
            return self._redirect_decision(starting_mode, intent, tone)

        selected_mode = self._select_mode(starting_mode, intent, tone)
        allowed_tools = allowed_tools_for_mode(selected_mode)
        retrieval_required = self._needs_retrieval(intent, selected_mode, normalized, frame.search_plan)
        retrieval_tool = retrieval_tool_for(
            intent,
            selected_mode,
            normalized,
            need=frame.need,
            search_plan=frame.search_plan,
        ) if retrieval_required else None
        service_handoff = self._should_suggest_service_handoff(selected_mode, intent, tone, normalized, frame.need)
        # Soft offer: when the user expresses build/implement intent in
        # Automator or Educator mode, the assistant is allowed to bridge
        # to the deployment's service contact with a one-sentence offer.
        # Does NOT shift mode to Service; the grader treats this as a
        # permitted soft handoff.
        soft_offer = self._should_offer_soft_service(selected_mode, normalized) and not service_handoff
        response_policy = self._select_policy(selected_mode, tone, service_handoff)
        cloud_allowed = self._cloud_allowed(response_policy)
        tool_execution_mode = self._tool_execution_mode(retrieval_required, retrieval_tool)
        provider_tools = [retrieval_tool] if tool_execution_mode == ToolExecutionMode.MODEL_TOOL_LOOP and retrieval_tool else []

        return RoutingDecision(
            stage=ConversationStage.ACTIVE,
            selected_mode=selected_mode,
            intent=intent,
            tone=tone,
            response_policy=response_policy,
            retrieval_required=retrieval_required,
            retrieval_tool=retrieval_tool,
            allowed_tools=allowed_tools,
            provider_tools=provider_tools,
            tool_execution_mode=tool_execution_mode,
            max_model_calls_per_turn=2 if tool_execution_mode == ToolExecutionMode.MODEL_TOOL_LOOP else 1,
            service_handoff_suggested=service_handoff,
            soft_service_offer=soft_offer,
            cloud_allowed=cloud_allowed,
            intake=intake,
            frame=frame,
            reason=self._reason(selected_mode, intent, tone, retrieval_required, cloud_allowed, frame.task),
        )

    def _clean_intake(self, intake: dict[str, str] | None) -> dict[str, str]:
        if not intake:
            return {}
        return {
            key: str(intake.get(key, "")).strip()
            for key in ("objective", "building", "maturity", "help_needed")
            if str(intake.get(key, "")).strip()
        }

    def _intake_text(self, intake: dict[str, str]) -> str:
        return " ".join(intake.values()).lower()

    def _detect_intake_intent(self, intake: dict[str, str]) -> UserIntent | None:
        text = self._intake_text(intake)
        if not text:
            return None
        if "service inquiry" in text or "consult" in text or "support" in text:
            return UserIntent.SERVICE_INQUIRY
        if "educator" in text or "learn" in text or "teach" in text or "walk me through" in text or "understand" in text:
            return UserIntent.EDUCATION
        if "workflow" in text or "debug" in text or "pattern" in text or "technical answer" in text or "examples" in text:
            return UserIntent.TECHNICAL_HELP
        return None

    def _normalize(self, message: str) -> str:
        return message.strip().lower().replace(",", "").rstrip(".!?")

    def _select_mode(self, starting_mode: str, intent: UserIntent, tone: ToneSignal) -> ChatMode:
        if intent == UserIntent.EDUCATION:
            return ChatMode.EDUCATOR
        if intent in {UserIntent.SERVICE_INQUIRY, UserIntent.TROUBLESHOOTING} and tone in {ToneSignal.FRUSTRATED, ToneSignal.URGENT}:
            return ChatMode.SERVICE
        if tone == ToneSignal.URGENT and intent in {UserIntent.TECHNICAL_HELP, UserIntent.UNKNOWN}:
            return ChatMode.SERVICE
        if tone == ToneSignal.HESITANT:
            return ChatMode.SERVICE
        if intent == UserIntent.SERVICE_INQUIRY:
            return ChatMode.SERVICE
        if tone == ToneSignal.CONFUSED and intent in {UserIntent.TECHNICAL_HELP, UserIntent.UNKNOWN}:
            return ChatMode.EDUCATOR
        if intent == UserIntent.TECHNICAL_HELP:
            return ChatMode.AUTOMATOR
        starting_mode = MODE_ALIASES.get(starting_mode, starting_mode)
        try:
            return ChatMode(starting_mode)
        except ValueError:
            return ChatMode.AUTOMATOR

    def _needs_retrieval(self, intent: UserIntent, mode: ChatMode, message: str, search_plan: SearchPlan) -> bool:
        if search_plan == SearchPlan.NONE:
            return False
        if intent == UserIntent.SMALL_TALK:
            return False
        if mode in {ChatMode.AUTOMATOR, ChatMode.EDUCATOR}:
            return True
        return any(term in message for term in TECHNICAL_TERMS)

    def _tool_execution_mode(self, retrieval_required: bool, retrieval_tool: str | None) -> ToolExecutionMode:
        if self.model_tool_loop_enabled and retrieval_required and retrieval_tool:
            return ToolExecutionMode.MODEL_TOOL_LOOP
        if retrieval_required:
            return ToolExecutionMode.BACKEND_RETRIEVAL_ONLY
        return ToolExecutionMode.DISABLED

    def _select_policy(
        self,
        mode: ChatMode,
        tone: ToneSignal,
        service_handoff: bool,
    ) -> ResponsePolicy:
        """Pick the response policy that gates which model tiers are on the menu.

        Phase D3: when claude_enabled is on, the default is the most permissive
        policy (LOCAL_THEN_CLAUDE_IF_HIGH_VALUE). The router then picks the best
        model for the query based on task fit, with the CloudSpendPolicy USD and
        per-call caps as the real spend safety net. Previously this method
        narrowly gated Sonnet to Service+handoff or frustrated/urgent tone,
        which kept Sonnet off the menu for routine technical turns where it
        was actually the best fit.

        Mapping when both flags are on:
        - claude_enabled: LOCAL_THEN_CLAUDE_IF_HIGH_VALUE (Sonnet eligible)
        - claude_enabled is off but low_cost_enabled: LOCAL_THEN_LOW_COST (Haiku/Gemini eligible)
        - neither: LOCAL_ONLY
        """
        if self.cloud_policy.claude_enabled:
            return ResponsePolicy.LOCAL_THEN_CLAUDE_IF_HIGH_VALUE
        if self.cloud_policy.low_cost_enabled:
            return ResponsePolicy.LOCAL_THEN_LOW_COST
        return ResponsePolicy.LOCAL_ONLY

    def _should_suggest_service_handoff(
        self,
        mode: ChatMode,
        intent: UserIntent,
        tone: ToneSignal,
        message: str,
        need: UserNeed,
    ) -> bool:
        if mode != ChatMode.SERVICE or intent == UserIntent.SMALL_TALK:
            return False
        if need in {UserNeed.PRICING_TERMS, UserNeed.PROJECT_INTAKE}:
            return True
        if tone in {ToneSignal.FRUSTRATED, ToneSignal.URGENT}:
            return True
        return any(term in message for term in DIRECT_SERVICE_HANDOFF_TERMS)

    def _should_offer_soft_service(self, mode: ChatMode, message: str) -> bool:
        """Bridge offer in Automator/Educator mode when the user is clearly
        trying to build, implement, or find a reusable workflow.

        Narrow on purpose: only fires when the user's words explicitly
        signal build intent. The assistant is told (via CITATION_DISCIPLINE)
        to emit a single short bridge sentence, not a sales pitch.
        """
        if mode == ChatMode.SERVICE:
            return False  # Service mode already has the harder handoff path.
        if not message:
            return False
        return any(term in message for term in BUILD_INTENT_TERMS)

    def _cloud_allowed(self, policy: ResponsePolicy) -> bool:
        if self.cloud_policy.max_cloud_calls_per_turn < 1:
            return False
        if policy == ResponsePolicy.LOCAL_THEN_LOW_COST:
            return self.cloud_policy.low_cost_enabled
        if policy == ResponsePolicy.LOCAL_THEN_CLAUDE_IF_HIGH_VALUE:
            return self.cloud_policy.claude_enabled
        return False

    def _intake_decision(self, starting_mode: str, message: str) -> RoutingDecision:
        selected_mode = self._select_mode(starting_mode, UserIntent.UNKNOWN, ToneSignal.NEUTRAL)
        return RoutingDecision(
            stage=ConversationStage.INTAKE,
            selected_mode=selected_mode,
            intent=UserIntent.UNKNOWN,
            tone=ToneSignal.NEUTRAL,
            retrieval_required=False,
            allowed_tools=[],
            provider_tools=[],
            tool_execution_mode=ToolExecutionMode.DISABLED,
            answer_allowed=False,
            intake_required=True,
            cloud_allowed=False,
            redirect_message="Before we chat, please confirm your objective.",
            reason=f"intake_required; mode={selected_mode.value}; message_seen={bool(message)}",
        )

    def _redirect_decision(self, starting_mode: str, intent: UserIntent, tone: ToneSignal) -> RoutingDecision:
        selected_mode = self._select_mode(starting_mode, UserIntent.UNKNOWN, tone)
        return RoutingDecision(
            stage=ConversationStage.REDIRECT,
            selected_mode=selected_mode,
            intent=intent,
            tone=tone,
            retrieval_required=False,
            allowed_tools=[],
            provider_tools=[],
            tool_execution_mode=ToolExecutionMode.DISABLED,
            answer_allowed=False,
            cloud_allowed=False,
            redirect_message="I can help with automation workflows, learning automation concepts, or scoping a service inquiry. Which path should we take?",
            reason=f"out_of_scope_redirect; mode={selected_mode.value}; tone={tone.value}",
        )

    def _reason(self, mode: ChatMode, intent: UserIntent, tone: ToneSignal, retrieval: bool, cloud: bool, task: str = "") -> str:
        suffix = f"; task={task}" if task else ""
        return f"mode={mode.value}; intent={intent.value}; tone={tone.value}; retrieval={retrieval}; cloud={cloud}{suffix}"
