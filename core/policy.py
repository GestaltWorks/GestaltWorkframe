import os
from enum import StrEnum
from pydantic import BaseModel, Field


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int = 0) -> int:
    try:
        return max(int(os.getenv(name, str(default))), 0)
    except ValueError:
        return default


class ChatMode(StrEnum):
    SERVICE = "pipeline"
    AUTOMATOR = "automator"
    EDUCATOR = "educator"


# UserNeed and UserIntent describe the same turn at different granularities.
# UserNeed is the fine-grained classification produced by routing_frame; it
# distinguishes pricing-terms from project-intake from service-discovery, for
# example. UserIntent is a coarser bucketing used by Orchestrator._select_mode
# to keep mode-selection branches tidy: it folds the three service-flavor
# needs into one SERVICE_INQUIRY, the two technical needs into TECHNICAL_HELP,
# and so on. The Need -> Intent mapping is centralized in
# routing_frame.need_to_intent(); changing one side requires updating the other
# and the tests that pin the mapping.
class UserIntent(StrEnum):
    SMALL_TALK = "small_talk"
    SERVICE_INQUIRY = "service_inquiry"
    TECHNICAL_HELP = "technical_help"
    EDUCATION = "education"
    TROUBLESHOOTING = "troubleshooting"
    OUT_OF_SCOPE = "out_of_scope"
    UNKNOWN = "unknown"


class AudienceSegment(StrEnum):
    PRACTITIONER = "practitioner"
    CLIENT = "client"
    STUDENT = "student"
    UNKNOWN = "unknown"


class UserNeed(StrEnum):
    SMALL_TALK = "small_talk"
    RESOURCE_LOOKUP = "resource_lookup"          # -> TECHNICAL_HELP
    IMPLEMENTATION_HELP = "implementation_help"  # -> TECHNICAL_HELP
    TROUBLESHOOTING = "troubleshooting"
    SERVICE_DISCOVERY = "service_discovery"      # -> SERVICE_INQUIRY
    PRICING_TERMS = "pricing_terms"              # -> SERVICE_INQUIRY
    PROJECT_INTAKE = "project_intake"            # -> SERVICE_INQUIRY
    EDUCATION = "education"
    OUT_OF_SCOPE = "out_of_scope"
    UNKNOWN = "unknown"


class OutputShape(StrEnum):
    DIRECT_ANSWER = "direct_answer"
    RECOMMENDATION = "recommendation"
    TROUBLESHOOTING_PLAN = "troubleshooting_plan"
    INTAKE_PACKET = "intake_packet"
    SOCRATIC_LESSON = "socratic_lesson"
    REDIRECT = "redirect"


class SearchPlan(StrEnum):
    NONE = "none"
    LOCAL_FIRST = "local_first"
    LOCAL_PLUS_PUBLIC = "local_plus_public"


class ToneSignal(StrEnum):
    NEUTRAL = "neutral"
    CONFUSED = "confused"
    HESITANT = "hesitant"
    FRUSTRATED = "frustrated"
    URGENT = "urgent"


class ResponsePolicy(StrEnum):
    LOCAL_ONLY = "local_only"
    LOCAL_THEN_LOW_COST = "local_then_low_cost"
    LOCAL_THEN_CLAUDE_IF_HIGH_VALUE = "local_then_claude_if_high_value"
    DEMO_SAFE = "demo_safe"


class ConversationStage(StrEnum):
    INTAKE = "intake"
    ACTIVE = "active"
    REDIRECT = "redirect"


class ToolExecutionMode(StrEnum):
    DISABLED = "disabled"
    BACKEND_RETRIEVAL_ONLY = "backend_retrieval_only"
    MODEL_TOOL_LOOP = "model_tool_loop"


class CloudSpendPolicy(BaseModel):
    low_cost_enabled: bool = False
    claude_enabled: bool = False
    max_cloud_calls_per_turn: int = 0
    max_cloud_calls_per_session: int = 0

    @classmethod
    def from_env(cls) -> "CloudSpendPolicy":
        spillover_enabled = _env_bool("ENABLE_CLOUD_SPILLOVER")
        return cls(
            low_cost_enabled=_env_bool("ENABLE_LOW_COST_CLOUD") and spillover_enabled,
            claude_enabled=_env_bool("ENABLE_CLAUDE_FALLBACK") and spillover_enabled,
            max_cloud_calls_per_turn=_env_int("CLOUD_SPILLOVER_MAX_CALLS_PER_TURN"),
            max_cloud_calls_per_session=_env_int("CLOUD_SPILLOVER_MAX_CALLS_PER_SESSION"),
        )


class RouteFrame(BaseModel):
    audience: AudienceSegment = AudienceSegment.UNKNOWN
    need: UserNeed = UserNeed.UNKNOWN
    output_shape: OutputShape = OutputShape.DIRECT_ANSWER
    search_plan: SearchPlan = SearchPlan.NONE
    task: str = "routine_chat"


class RoutingDecision(BaseModel):
    stage: ConversationStage = ConversationStage.ACTIVE
    selected_mode: ChatMode
    intent: UserIntent
    tone: ToneSignal = ToneSignal.NEUTRAL
    response_policy: ResponsePolicy = ResponsePolicy.LOCAL_ONLY
    retrieval_required: bool = False
    retrieval_tool: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    provider_tools: list[str] = Field(default_factory=list)
    tool_execution_mode: ToolExecutionMode = ToolExecutionMode.DISABLED
    max_model_calls_per_turn: int = 1
    max_retrieval_calls_per_turn: int = 1
    max_tool_calls_per_turn: int = 1
    answer_allowed: bool = True
    intake_required: bool = False
    service_handoff_suggested: bool = False
    # Soft offer: even in Automator/Educator mode, when the user expresses a
    # build/implement/integrate intent and library likely has a relevant
    # artifact, the system prompt's CITATION_DISCIPLINE invites the model
    # to add a one-sentence "Want help getting this implemented?" offer.
    # Distinct from service_handoff_suggested, which triggers a mode shift
    # into Service. soft_service_offer keeps the current mode and just
    # permits the bridge sentence.
    soft_service_offer: bool = False
    cloud_allowed: bool = False
    intake: dict[str, str] = Field(default_factory=dict)
    frame: RouteFrame = Field(default_factory=RouteFrame)
    redirect_message: str | None = None
    reason: str = ""
