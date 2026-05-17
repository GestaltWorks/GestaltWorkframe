"""Phase D2 tests: terminal grounding behavior.

Verifies the three coordinated changes that aim to stop canned answers:

1. Persona system prompts include the CITATION_DISCIPLINE block which
   demands specific title + author + year + URL citation when library has
   a specific source, and permits a soft Acme handoff when the user
   expresses build intent.
2. Retrieval chunk shape (kb/retrieval_format.py) presents cite-able
   fields up front so the LLM can extract them reliably.
3. Orchestrator sets soft_service_offer=True when the user expresses
   build intent in Automator/Educator mode, and the answer grader
   accepts the soft handoff phrase under that flag.
"""

from __future__ import annotations

from core.answer_grading import AnswerGrade, AnswerGrader
from core.orchestrator import BUILD_INTENT_TERMS, Orchestrator
from core.personas import (
    AUTOMATOR_PERSONA,
    CITATION_DISCIPLINE,
    DEFAULT_BOT_IDENTITY,
    EDUCATOR_PERSONA,
    PIPELINE_PERSONA,
)
from core.policy import (
    ChatMode,
    CloudSpendPolicy,
    RoutingDecision,
    ToneSignal,
    UserIntent,
)


# ---- CITATION_DISCIPLINE in every persona ---------------------------------

def test_every_persona_carries_citation_discipline():
    """All three personas must embed the citation rules so the LLM behaves
    consistently regardless of which mode the router lands on."""
    for persona in (PIPELINE_PERSONA, AUTOMATOR_PERSONA, EDUCATOR_PERSONA):
        assert "GROUNDING RULES" in persona.system_prompt, persona.id
        assert "NAME IT EXPLICITLY" in persona.system_prompt, persona.id
        assert "Source:" in persona.system_prompt, persona.id


def test_citation_discipline_demands_specifics_over_paraphrase():
    """The rule against vague paraphrase ('many', 'a lot', 'most') is the
    main lever against canned answers."""
    assert "Do not paraphrase a specific number" in CITATION_DISCIPLINE
    assert "Never fabricate" in CITATION_DISCIPLINE
    assert "Never invent URLs" in CITATION_DISCIPLINE


def test_citation_discipline_permits_soft_handoff_on_build_intent():
    """The discipline block is what tells the model that a soft handoff
    is allowed when build/implement intent is clear."""
    assert "Want help getting this implemented?" in CITATION_DISCIPLINE
    assert "one short sentence" in CITATION_DISCIPLINE


# ---- soft_service_offer trigger -------------------------------------------

def _orch() -> Orchestrator:
    return Orchestrator(cloud_policy=CloudSpendPolicy())


def test_build_intent_in_automator_sets_soft_service_offer():
    orch = _orch()
    decision = orch.decide(
        starting_mode="automator",
        message="I want to build a workflow that onboards new users",
    )
    assert decision.selected_mode == ChatMode.AUTOMATOR
    assert decision.soft_service_offer is True
    # Service handoff is the HARD path; soft offer should not also trigger it.
    assert decision.service_handoff_suggested is False


def test_pure_technical_question_does_not_set_soft_offer():
    orch = _orch()
    decision = orch.decide(
        starting_mode="automator",
        message="What's the syntax for a Jinja filter that lowercases a string?",
    )
    assert decision.soft_service_offer is False


def test_build_intent_in_service_mode_does_not_set_soft_offer():
    """In Service mode, soft offer is unnecessary; the harder handoff path applies."""
    orch = _orch()
    decision = orch.decide(
        starting_mode="pipeline",
        message="I want to hire someone to build this onboarding workflow",
    )
    assert decision.selected_mode == ChatMode.SERVICE
    assert decision.soft_service_offer is False


def test_implement_intent_in_educator_sets_soft_offer():
    orch = _orch()
    decision = orch.decide(
        starting_mode="educator",
        message="how do I implement a webhook listener in Automation?",
    )
    # Mode may shift based on intent but soft offer should fire either way
    # when build/implement intent is detected.
    assert decision.soft_service_offer is True


def test_build_intent_terms_cover_common_phrasings():
    """Sanity check that the trigger list catches the common phrasings the
    user mentioned in feedback."""
    for phrase in (
        "i want to build",
        "trying to build",
        "how do i implement",
        "is there a workflow",
        "looking for an example",
        "need a workflow",
    ):
        assert phrase in BUILD_INTENT_TERMS, phrase


# ---- grader accepts soft handoff with the flag ----------------------------

def _decision(
    *,
    mode: ChatMode = ChatMode.AUTOMATOR,
    soft_offer: bool = False,
    service_handoff: bool = False,
    retrieval_required: bool = False,
) -> RoutingDecision:
    return RoutingDecision(
        selected_mode=mode,
        intent=UserIntent.TECHNICAL_HELP,
        tone=ToneSignal.NEUTRAL,
        service_handoff_suggested=service_handoff,
        soft_service_offer=soft_offer,
        retrieval_required=retrieval_required,
    )


def test_grader_blocks_service_handoff_phrase_in_automator_without_flag():
    grader = AnswerGrader()
    decision = _decision(mode=ChatMode.AUTOMATOR, soft_offer=False)
    answer = "Here's the answer. Want help? Open the contact form at /services."
    grade = grader.grade(answer, decision, retrieval=None)
    assert grade.adequate is False
    assert grade.reason == "unexpected_service_handoff"


def test_grader_permits_service_handoff_phrase_when_soft_offer_set():
    """Phase D2: soft_service_offer=True is the operator's signal that the
    bridge sentence is allowed even in Automator mode."""
    grader = AnswerGrader()
    decision = _decision(mode=ChatMode.AUTOMATOR, soft_offer=True)
    answer = "Use this workflow: example-author/automation-bundles. Want help getting it implemented? Open the contact form at /services."
    grade = grader.grade(answer, decision, retrieval=None)
    assert grade.adequate is True
    assert grade.reason in {"retrieval_not_required", "grounded_or_declined"}


def test_grader_still_blocks_service_handoff_phrase_in_educator_without_flag():
    grader = AnswerGrader()
    decision = _decision(mode=ChatMode.EDUCATOR, soft_offer=False)
    answer = "Here's how Jinja filters work... Open the contact form at /services."
    grade = grader.grade(answer, decision, retrieval=None)
    assert grade.adequate is False
    assert grade.reason == "unexpected_service_handoff"
