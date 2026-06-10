from gestaltworkframe.core.policy import AudienceSegment, ChatMode, OutputShape, RouteFrame, SearchPlan, ToneSignal, UserIntent, UserNeed


SMALL_TALK: frozenset[str] = frozenset({"hi", "hello", "hey", "yo", "test", "thanks", "thank you"})
LEARNING_SIGNALS = ("teach", "explain", "walk me through", "lesson", "learn", "quiz", "scenario", "socratic", "practice")
RESOURCE_SIGNALS = ("github", "repo", "duplicate", "copy", "sample", "example", "template", "bundle", "library", "script")
IMPLEMENTATION_SIGNALS = ("how do i", "how can i", "build", "create", "configure", "integrate", "connect", "api", "webhook", "jinja", "ctx", "workflow")
TROUBLE_SIGNALS = ("broken", "error", "failing", "doesn\'t work", "not working", "debug", "troubleshoot", "stuck", "already tried", "wrong")
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
CONFUSION_SIGNALS = ("confused", "lost", "don\'t understand", "do not understand", "not following", "break that down")
URGENCY_SIGNALS = ("urgent", "production", "client", "blocked", "down", "asap")


# ---------------------------------------------------------------------------
# Deployment-config signal resolution
# ---------------------------------------------------------------------------

def _routing_cfg():
    """Return the active RoutingConfig or None if unavailable."""
    try:
        from gestaltworkframe.core.deployment_config import get_deployment_config  # lazy — avoids circular import
        return get_deployment_config().routing
    except Exception:
        return None


class _Signals:
    """Holds resolved signal sets for one classify_route call.

    When the deployment config has a non-empty list for a group, that list
    wins.  Otherwise the module-level hardcoded default is used.
    """

    __slots__ = (
        "small_talk", "learning", "resource", "implementation", "trouble",
        "service", "pricing", "discovery", "confusion", "urgency", "domain",
        "complex_build",
    )

    def __init__(self, cfg) -> None:
        def _s(lst, default: frozenset) -> frozenset:
            return frozenset(lst) if lst else default

        def _t(lst, default: tuple) -> tuple:
            return tuple(lst) if lst else default

        self.small_talk    = _s(cfg.small_talk if cfg else [],                   SMALL_TALK)
        self.learning      = _t(cfg.learning_signals if cfg else [],              LEARNING_SIGNALS)
        self.resource      = _t(cfg.resource_signals if cfg else [],              RESOURCE_SIGNALS)
        self.implementation = _t(cfg.implementation_signals if cfg else [],       IMPLEMENTATION_SIGNALS)
        self.trouble       = _t(cfg.trouble_signals if cfg else [],               TROUBLE_SIGNALS)
        self.service       = _t(cfg.service_signals if cfg else [],               SERVICE_SIGNALS)
        self.pricing       = _t(cfg.pricing_signals if cfg else [],               PRICING_SIGNALS)
        self.discovery     = _t(cfg.discovery_signals if cfg else [],             DISCOVERY_SIGNALS)
        self.confusion     = _t(cfg.confusion_signals if cfg else [],             CONFUSION_SIGNALS)
        self.urgency       = _t(cfg.urgency_signals if cfg else [],               URGENCY_SIGNALS)
        self.complex_build = _t(cfg.complex_build_phrases if cfg else [],         _COMPLEX_BUILD_PHRASES)
        # domain is the union of component signals + a few fixed audience terms
        self.domain = (
            self.implementation + self.resource + self.service + self.pricing
            + self.discovery + self.learning
            + ("automation", "automator", "security", "engineer", "student")
        )


def classify_route(starting_mode: str, message: str, intake: dict[str, str] | None = None) -> tuple[RouteFrame, UserIntent, ToneSignal]:
    cfg = _routing_cfg()
    sig = _Signals(cfg)
    text = _normalize(message)
    intake_text = _normalize(" ".join((intake or {}).values()))
    combined = f"{text} {intake_text}".strip()

    need = _need(text, combined, sig)
    audience = _audience(starting_mode, need, combined)
    tone = _tone(combined, sig)
    frame = RouteFrame(
        audience=audience,
        need=need,
        output_shape=_output_shape(need),
        search_plan=_search_plan(need),
        task=_task(need, audience, tone, combined, sig),
    )
    return frame, _intent(need), tone


def _normalize(value: str) -> str:
    return " ".join(value.strip().lower().replace(",", "").rstrip(".!?").split())


def _need(message: str, combined: str, sig: "_Signals") -> UserNeed:
    if message in sig.small_talk and not _has(combined, sig.domain):
        return UserNeed.SMALL_TALK
    if _has(combined, sig.pricing):
        return UserNeed.PRICING_TERMS
    if _has(message, sig.service):
        return UserNeed.PROJECT_INTAKE
    if _has(combined, sig.discovery) or _has(combined, sig.service):
        return UserNeed.SERVICE_DISCOVERY
    if _has(combined, sig.trouble):
        return UserNeed.TROUBLESHOOTING
    if _has(combined, sig.learning):
        return UserNeed.EDUCATION
    if _has(combined, sig.resource):
        return UserNeed.RESOURCE_LOOKUP
    if _has(combined, sig.implementation):
        return UserNeed.IMPLEMENTATION_HELP
    if _has(combined, sig.confusion):
        return UserNeed.EDUCATION
    if len(message.split()) >= 4 and not _has(combined, sig.domain):
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


def _tone(combined: str, sig: "_Signals") -> ToneSignal:
    if _has(combined, sig.urgency):
        return ToneSignal.URGENT
    if _has(combined, sig.trouble):
        return ToneSignal.FRUSTRATED
    if _has(combined, sig.discovery):
        return ToneSignal.HESITANT
    if _has(combined, sig.confusion):
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


def _task(need: UserNeed, audience: AudienceSegment, tone: ToneSignal, combined: str, sig: "_Signals") -> str:
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
        if any(phrase in combined for phrase in sig.complex_build):
            return "complex_implementation"
        return "implementation_help"
    if need == UserNeed.EDUCATION:
        return "socratic_tutor"
    if need in {UserNeed.PRICING_TERMS, UserNeed.SERVICE_DISCOVERY, UserNeed.PROJECT_INTAKE}:
        return "high_value_service_inquiry" if audience == AudienceSegment.CLIENT else "routine_support"
    return "routine_chat"


def _has(text: str, terms) -> bool:
    return any(term in text for term in terms)
