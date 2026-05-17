from dataclasses import dataclass

from core.policy import ChatMode, SearchPlan, UserIntent, UserNeed

KB_OVERVIEW = "kb_overview"
REFERENCE_SEARCH = "reference_search"
WORKFLOW_PATTERN_SEARCH = "workflow_pattern_search"
LESSON_CONCEPT_SEARCH = "lesson_concept_search"
SERVICE_INQUIRY_CTA = "service_inquiry_cta"

_MODE_TOOLS = {
    ChatMode.SERVICE: [SERVICE_INQUIRY_CTA, REFERENCE_SEARCH, WORKFLOW_PATTERN_SEARCH],
    ChatMode.AUTOMATOR: [KB_OVERVIEW, REFERENCE_SEARCH, WORKFLOW_PATTERN_SEARCH],
    ChatMode.EDUCATOR: [KB_OVERVIEW, LESSON_CONCEPT_SEARCH, REFERENCE_SEARCH],
}


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict
    destructive: bool = False

    def as_provider_tool(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


TOOL_DEFINITIONS = {
    KB_OVERVIEW: ToolDefinition(
        name=KB_OVERVIEW,
        description="Retrieve high-level automation platform context from the KB.",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    ),
    REFERENCE_SEARCH: ToolDefinition(
        name=REFERENCE_SEARCH,
        description="Search reference material and cheat-sheet snippets with source citations.",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    ),
    WORKFLOW_PATTERN_SEARCH: ToolDefinition(
        name=WORKFLOW_PATTERN_SEARCH,
        description="Search workflow examples, patterns, and troubleshooting context.",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    ),
    LESSON_CONCEPT_SEARCH: ToolDefinition(
        name=LESSON_CONCEPT_SEARCH,
        description="Search educational explanations, concepts, and lesson-oriented KB context.",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    ),
    SERVICE_INQUIRY_CTA: ToolDefinition(
        name=SERVICE_INQUIRY_CTA,
        description=(
            "Route a qualified service-inquiry user to the deployment's configured contact path "
            "or public email and summarize the problem, urgency, and desired outcome for handoff."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "urgency": {"type": "string"},
                "desired_outcome": {"type": "string"},
            },
            "required": ["summary"],
        },
    ),
}


def allowed_tools_for_mode(mode: ChatMode) -> list[str]:
    return list(_MODE_TOOLS.get(mode, []))


def tool_definitions_for_mode(mode: ChatMode) -> list[ToolDefinition]:
    return [TOOL_DEFINITIONS[name] for name in allowed_tools_for_mode(mode)]


def provider_tools_for_mode(mode: ChatMode, allowed_tools: list[str]) -> list[dict]:
    allowed = set(allowed_tools)
    return [tool.as_provider_tool() for tool in tool_definitions_for_mode(mode) if tool.name in allowed]


def retrieval_tool_for(
    intent: UserIntent,
    mode: ChatMode,
    message: str,
    need: UserNeed | None = None,
    search_plan: SearchPlan | None = None,
) -> str | None:
    if search_plan == SearchPlan.NONE:
        return None
    if intent == UserIntent.SMALL_TALK:
        return None
    if mode == ChatMode.EDUCATOR or intent == UserIntent.EDUCATION:
        return LESSON_CONCEPT_SEARCH
    if need in {UserNeed.RESOURCE_LOOKUP, UserNeed.TROUBLESHOOTING}:
        return WORKFLOW_PATTERN_SEARCH
    if _looks_like_workflow_library_request(message) or intent == UserIntent.TROUBLESHOOTING:
        return WORKFLOW_PATTERN_SEARCH
    if mode in {ChatMode.AUTOMATOR, ChatMode.SERVICE}:
        return REFERENCE_SEARCH
    return KB_OVERVIEW


def _looks_like_workflow_library_request(message: str) -> bool:
    return any(
        term in message
        for term in ("workflow", "example", "sample", "script", "onboarding", "pattern", "template", "bundle", "import", "plug in", "library")
    )
