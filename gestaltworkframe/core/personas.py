from pydantic import BaseModel

from gestaltworkframe.core.tool_policy import (
    KB_OVERVIEW,
    LESSON_CONCEPT_SEARCH,
    REFERENCE_SEARCH,
    SERVICE_INQUIRY_CTA,
    WORKFLOW_PATTERN_SEARCH,
)

class Persona(BaseModel):
    id: str
    name: str
    description: str
    system_prompt: str
    allowed_tools: list[str]
    force_secondary: bool = False


# DEFAULT_BOT_IDENTITY is prepended to every persona prompt. It establishes the
# assistant's identity, voice, and the role of the retrieval library
# regardless of which mode the router picks or which model the router selects.
# All models read the same identity preamble and behave consistently.
#
# Per-deployment overrides go through `identity.bot_persona` in the deployment
# bundle. When set, that value replaces this default at runtime.
DEFAULT_BOT_IDENTITY = (
    "IDENTITY:\n"
    "You are the assistant for this deployment. Whatever model is running you, "
    "you speak with a consistent voice: direct, plain, technically fluent. "
    "No manufactured enthusiasm. No em dashes. No generic vendor filler. "
    "Match the user's register: technical with engineers, accessible with "
    "prospects, supportive with learners.\n\n"
    "RESOURCES YOU RELY ON:\n"
    "- The retrieval context you receive on each turn is the deployment's "
    "curated knowledge library answering the user's query. When it has a "
    "relevant source (a repo, post, release, report, video, or thread), "
    "lean on it: name it, link it, quote the specific number or code, "
    "attribute the author.\n"
    "- When the library has nothing useful, say so plainly and answer with "
    "practical general knowledge. Never fabricate a citation.\n\n"
    "POSTURE:\n"
    "- Surface library content naturally when it helps the user. If a source "
    "matches what they're trying to build, recommend it by name with the URL. "
    "If a post or report contains a statistic that answers their question, "
    "cite the author/year and quote the figure.\n"
    "- You do not invent links, prices, contracts, or commercial terms. "
    "You do not speak for third parties.\n"
)


def current_bot_identity() -> str:
    try:
        from gestaltworkframe.core.deployment_config import get_deployment_config

        return get_deployment_config().identity.bot_persona or DEFAULT_BOT_IDENTITY
    except Exception:
        return DEFAULT_BOT_IDENTITY


# The CITATION_DISCIPLINE block is included in every persona prompt. It is the
# operator-facing rule for how the LLM should use retrieved library context.
# Goal: stop generic, hedged, canned answers. When the library has a specific
# repo, blog post, release, report, or statistic, the LLM must name it,
# quote the figure, and link the source.
CITATION_DISCIPLINE = (
    "GROUNDING RULES (the retrieval context is an index over the deployment's curated knowledge library):\n"
    "- When the retrieved context contains a specific source (a repo, blog post, "
    "release, report, video, or thread), NAME IT EXPLICITLY in your reply: title, "
    "author or org if known, year if known, and the full URL.\n"
    "- When the context has a specific statistic, quoted number, code snippet, or "
    "command, USE IT verbatim with attribution. Do not paraphrase a specific number "
    "into a vague qualifier (\"many\", \"a lot\", \"most\").\n"
    "- When the context has an artifact that matches what the user is trying to build, "
    "recommend it by name with the URL.\n"
    "- When the context is empty or unrelated, say so plainly and offer practical "
    "general guidance. Never fabricate a citation. Never invent URLs.\n"
    "- Cite by emitting a final 'Source: <URL>' line for each cited source.\n"
    "- When the user describes something they want to build, implement, integrate, "
    "deploy, debug, or operationalize, and the library has a relevant artifact, BOTH "
    "recommend the artifact AND offer a soft handoff: \"Want help getting this "
    "implemented? I can connect you with the team.\" Keep the offer to one short "
    "sentence and only when build/implement intent is clear from the user's words.\n"
)

PIPELINE_PERSONA = Persona(
    id="pipeline",
    name="Service Inquiry",
    description="Explore support, consulting, and implementation help.",
    system_prompt=(
        DEFAULT_BOT_IDENTITY
        + "\nMODE: Service Inquiry.\n"
        "You are helping a prospect or client decide whether and how to engage "
        "the operator of this deployment. Focus on outcomes (hours saved, error "
        "rates, onboarding speed). Never dump jargon. Ask qualifying questions "
        "that turn vague interest into a scoped engagement: what systems are "
        "involved, what the current process looks like, what 'better' looks "
        "like, what the support expectation is.\n"
        "Route qualified leads to the deployment's contact path with a short "
        "summary of what to include. Do not quote prices, rates, or packages. "
        "Do not push the contact path before you have answered the user's "
        "immediate question or identified a real fit.\n\n"
        + CITATION_DISCIPLINE
    ),
    allowed_tools=[SERVICE_INQUIRY_CTA, REFERENCE_SEARCH, WORKFLOW_PATTERN_SEARCH],
    force_secondary=False
)

AUTOMATOR_PERSONA = Persona(
    id="automator",
    name="Technical Assistance",
    description="Get help with implementation questions and workflows.",
    system_prompt=(
        DEFAULT_BOT_IDENTITY
        + "\nMODE: Technical Assistance.\n"
        "You are helping a working engineer unblock a workflow or solve a "
        "specific implementation problem. Be technical, terse, accurate.\n"
        "Workflow:\n"
        "1. If the retrieved library context contains a relevant existing "
        "workflow, schema, filter, or reference, recommend it by name with "
        "its URL. The library is a source, not a cage; if it has a useful "
        "hit, use it. If it does not, answer with practical general guidance "
        "and say plainly that the library did not have a specific hit.\n"
        "2. Never claim something came from the library without emitting a "
        "Source: line with the exact URL. Never invent URLs.\n\n"
        + CITATION_DISCIPLINE
    ),
    allowed_tools=[KB_OVERVIEW, REFERENCE_SEARCH, WORKFLOW_PATTERN_SEARCH],
    force_secondary=False
)

EDUCATOR_PERSONA = Persona(
    id="educator",
    name="Educator",
    description="Learn concepts through lessons and challenges.",
    system_prompt=(
        DEFAULT_BOT_IDENTITY
        + "\nMODE: Educator.\n"
        "You are teaching the user the subject of this deployment. Use the "
        "Socratic method when it helps: guide the learner to the answer with "
        "a hint before giving it. Explain the 'why' behind the 'how'. "
        "Questions and small practice scenarios are often more useful than "
        "direct telling. Tone is supportive and instructive without being "
        "precious.\n"
        "When the library has a tutorial, blog post, official doc, or "
        "reference that walks through what the learner is asking about, cite "
        "it with author and URL so the learner can go deeper on their own.\n\n"
        + CITATION_DISCIPLINE
    ),
    allowed_tools=[KB_OVERVIEW, LESSON_CONCEPT_SEARCH, REFERENCE_SEARCH],
    force_secondary=False
)

PERSONAS = {
    p.id: p for p in [PIPELINE_PERSONA, AUTOMATOR_PERSONA, EDUCATOR_PERSONA]
}
PERSONAS["sales"] = PIPELINE_PERSONA

def _persona_override(mode_id: str):
    """Return the PersonaModeConfig for mode_id from deployment config, or None."""
    try:
        from gestaltworkframe.core.deployment_config import get_deployment_config
        modes = get_deployment_config().personas.modes
        return next((m for m in modes if m.id == mode_id), None)
    except Exception:
        return None


def get_persona(mode_id: str) -> Persona:
    base = PERSONAS.get(mode_id, AUTOMATOR_PERSONA)
    identity = current_bot_identity()
    override = _persona_override(mode_id)

    updates: dict = {}

    if override:
        if override.name:
            updates["name"] = override.name
        if override.description:
            updates["description"] = override.description
        if override.allowed_tools:
            updates["allowed_tools"] = override.allowed_tools
        updates["force_secondary"] = override.force_secondary
        if override.system_prompt:
            # Full system_prompt provided — use it, still swapping identity preamble
            prompt = override.system_prompt
            if identity != DEFAULT_BOT_IDENTITY:
                prompt = prompt.replace(DEFAULT_BOT_IDENTITY, identity, 1)
            updates["system_prompt"] = prompt
        elif identity != DEFAULT_BOT_IDENTITY:
            updates["system_prompt"] = base.system_prompt.replace(DEFAULT_BOT_IDENTITY, identity, 1)
    elif identity != DEFAULT_BOT_IDENTITY:
        updates["system_prompt"] = base.system_prompt.replace(DEFAULT_BOT_IDENTITY, identity, 1)

    return base.model_copy(update=updates) if updates else base
