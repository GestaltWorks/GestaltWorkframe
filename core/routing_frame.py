from core.policy import AudienceSegment, ChatMode, OutputShape, RouteFrame, SearchPlan, ToneSignal, UserIntent, UserNeed


SMALL_TALK = {"hi", "hello", "hey", "yo", "test", "thanks", "thank you"}
LEARNING_SIGNALS = ("teach", "explain", "walk me through", "lesson", "learn", "quiz", "scenario", "socratic", "practice")
RESOURCE_SIGNALS = ("github", "repo", "duplicate", "copy", "sample", "example", "template", "bundle", "library", "script")
IMPLEMENTATION_SIGNALS = ("how do i", "how can i", "build", "create", "configure", "integrate", "connect", "api", "webhook", "jinja", "ctx", "workflow")
TROUBLE_SIGNALS = ("broken", "error", "failing", "doesn't work", "not working", "debug", "troubleshoot", "stuck", "already tried", "wrong")
SERVICE_SIGNALS = (
    "hire", "consult", "consulting", "work with you", "bring you in", "contact", "demo", "proposal", "scope",
    "build this", "build it for", "can you build", "debug this for me", "fix this for me", "do this for me", "for us",
)
PRICING_SIGNALS = ("pricing", "price", "cost", "rate", "contract", "terms", "retainer", "sow", "msa", "statement of work")
DISCOVERY_SIGNALS = (
    "where to start", "what should we automate", "worth automating", "help us decide", "not sure what to automate",
    "how can you help", "how do you help", "what can you do",
)
DOMAIN_SIGNALS = IMPLEMENTATION_SIGNALS + RESOURCE_SIGNALS + SERVICE_SIGNALS + PRICING_SIGNALS + DISCOVERY_SIGNALS + LEARNING_SIGNALS + ("automation", "automator", "security", "engineer", "student")
CONFUSION_SIGNALS = ("confused", "lost", "don't understand", "do not understand", "not following", "break that down")
URGENCY_SIGNALS = ("urgent", "production", "client", "blocked", "down", "asap")


def classify_route(starting_mode: str, message: str, intake: dict[str, str] | None = None) -> tuple[RouteFrame, UserIntent, ToneSignal]:
    text = _normalize(message)
    intake_text = _normalize(" ".join((intake or {}).values()))
    combined = f"{text} {intake_text}".strip()

    need = _need(text, combined)
    audience = _audience(starting_mode, need, combined)
    tone = _tone(combined)
    frame = RouteFrame(
        audience=audience,
        need=need,
        output_shape=_output_shape(need),
        search_plan=_search_plan(need),
        task=_task(need, audience, tone, combined),
    )
    return frame, _intent(need), tone


def _normalize(value: str) -> str:
    return " ".join(value.strip().lower().replace(",", "").rstrip(".!?").split())


def _need(message: str, combined: str) -> UserNeed:
    if message in SMALL_TALK and not _has(combined, DOMAIN_SIGNALS):
        return UserNeed.SMALL_TALK
    if _has(combined, PRICING_SIGNALS):
        return UserNeed.PRICING_TERMS
    if _has(message, SERVICE_SIGNALS):
        return UserNeed.PROJECT_INTAKE
    if _has(combined, DISCOVERY_SIGNALS) or _has(combined, SERVICE_SIGNALS):
        return UserNeed.SERVICE_DISCOVERY
    if _has(combined, TROUBLE_SIGNALS):
        return UserNeed.TROUBLESHOOTING
    if _has(combined, LEARNING_SIGNALS):
        return UserNeed.EDUCATION
    if _has(combined, RESOURCE_SIGNALS):
        return UserNeed.RESOURCE_LOOKUP
    if _has(combined, IMPLEMENTATION_SIGNALS):
        return UserNeed.IMPLEMENTATION_HELP
    if _has(combined, CONFUSION_SIGNALS):
        return UserNeed.EDUCATION
    if len(message.split()) >= 4 and not _has(combined, DOMAIN_SIGNALS):
        return UserNeed.OUT_OF_SCOPE
    return UserNeed.UNKNOWN


def _audience(starting_mode: str, need: UserNeed, combined: str) -> AudienceSegment:
    if need == UserNeed.EDUCATION or starting_mode == ChatMode.EDUCATOR.value:
        return AudienceSegment.STUDENT
    if need in {UserNeed.RESOURCE_LOOKUP, UserNeed.IMPLEMENTATION_HELP, UserNeed.TROUBLESHOOTING}:
        return AudienceSegment.PRACTITIONER
    if need in {UserNeed.SERVICE_DISCOVERY, UserNeed.PRICING_TERMS, UserNeed.PROJECT_INTAKE} or starting_mode in {ChatMode.SERVICE.value, "sales"}:
        return AudienceSegment.CLIENT
    if any(term in combined for term in ("student", "junior", "learning")):
        return AudienceSegment.STUDENT
    if any(term in combined for term in ("client", "customer", "contract", "pricing")):
        return AudienceSegment.CLIENT
    return AudienceSegment.UNKNOWN


def _tone(combined: str) -> ToneSignal:
    if _has(combined, URGENCY_SIGNALS):
        return ToneSignal.URGENT
    if _has(combined, TROUBLE_SIGNALS):
        return ToneSignal.FRUSTRATED
    if _has(combined, DISCOVERY_SIGNALS):
        return ToneSignal.HESITANT
    if _has(combined, CONFUSION_SIGNALS):
        return ToneSignal.CONFUSED
    return ToneSignal.NEUTRAL


def need_to_intent(need: UserNeed) -> UserIntent:
    """Coarsen a UserNeed into the matching UserIntent.

    Public function (was _intent) so consumers and tests can reach it.
    The mapping is the canonical Need -> Intent contract referenced by
    core.policy enum docstrings.
    """

    if need == UserNeed.SMALL_TALK:
        return UserIntent.SMALL_TALK
    if need == UserNeed.OUT_OF_SCOPE:
        return UserIntent.OUT_OF_SCOPE
    if need == UserNeed.EDUCATION:
        return UserIntent.EDUCATION
    if need in {UserNeed.SERVICE_DISCOVERY, UserNeed.PRICING_TERMS, UserNeed.PROJECT_INTAKE}:
        return UserIntent.SERVICE_INQUIRY
    if need == UserNeed.TROUBLESHOOTING:
        return UserIntent.TROUBLESHOOTING
    if need in {UserNeed.RESOURCE_LOOKUP, UserNeed.IMPLEMENTATION_HELP}:
        return UserIntent.TECHNICAL_HELP
    return UserIntent.UNKNOWN


# Internal alias kept so existing call sites in this module stay readable.
_intent = need_to_intent


def _output_shape(need: UserNeed) -> OutputShape:
    if need == UserNeed.RESOURCE_LOOKUP:
        return OutputShape.RECOMMENDATION
    if need == UserNeed.TROUBLESHOOTING:
        return OutputShape.TROUBLESHOOTING_PLAN
    if need == UserNeed.PROJECT_INTAKE:
        return OutputShape.INTAKE_PACKET
    if need == UserNeed.EDUCATION:
        return OutputShape.SOCRATIC_LESSON
    if need == UserNeed.OUT_OF_SCOPE:
        return OutputShape.REDIRECT
    return OutputShape.DIRECT_ANSWER


def _search_plan(need: UserNeed) -> SearchPlan:
    if need in {UserNeed.RESOURCE_LOOKUP, UserNeed.IMPLEMENTATION_HELP, UserNeed.TROUBLESHOOTING}:
        return SearchPlan.LOCAL_PLUS_PUBLIC
    if need == UserNeed.EDUCATION:
        return SearchPlan.LOCAL_FIRST
    return SearchPlan.NONE


# Build-intent phrases that elevate implementation_help to complex_implementation.
# When a user says "I'm trying to build X", "how do I implement Y", or asks for a
# reusable workflow/example, the task complexity warrants a model that's actually
# good at code generation and citation discipline (Sonnet/Opus), not a small
# local model. Triggers map a task tag the profile registry recognizes.
_COMPLEX_BUILD_PHRASES = (
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
    "is there a workflow",
    "is there a bundle",
    "looking for a workflow",
    "need a workflow",
    "how do i automate",
)


def _task(need: UserNeed, audience: AudienceSegment, tone: ToneSignal, combined: str) -> str:
    if need == UserNeed.RESOURCE_LOOKUP:
        return "workflow_examples"
    if need == UserNeed.TROUBLESHOOTING:
        return "high_value_service_inquiry" if tone in {ToneSignal.FRUSTRATED, ToneSignal.URGENT} else "workflow_debugging"
    if need == UserNeed.IMPLEMENTATION_HELP:
        if any(term in combined for term in ("jinja", "ctx", "tasks")):
            return "jinja_help"
        # Build-intent signal: elevate to complex_implementation so the router's
        # task-fit score picks a model genuinely good at code generation and
        # citation discipline, not the next-best local 7-8B available.
        if any(phrase in combined for phrase in _COMPLEX_BUILD_PHRASES):
            return "complex_implementation"
        return "implementation_help"
    if need == UserNeed.EDUCATION:
        return "socratic_tutor"
    if need in {UserNeed.PRICING_TERMS, UserNeed.SERVICE_DISCOVERY, UserNeed.PROJECT_INTAKE}:
        return "high_value_service_inquiry" if audience == AudienceSegment.CLIENT else "routine_support"
    return "routine_chat"


def _has(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)