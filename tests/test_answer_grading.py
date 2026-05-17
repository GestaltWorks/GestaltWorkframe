from core.answer_grading import UNKNOWN_ANSWER, AnswerGrader
from core.policy import ChatMode, RoutingDecision, UserIntent
from core.retrieval import RetrievalResult


def _decision(retrieval_required: bool = True, retrieval_tool: str | None = None) -> RoutingDecision:
    return RoutingDecision(
        selected_mode=ChatMode.AUTOMATOR,
        intent=UserIntent.TECHNICAL_HELP,
        retrieval_required=retrieval_required,
        retrieval_tool=retrieval_tool,
    )


def _service_decision() -> RoutingDecision:
    return RoutingDecision(
        selected_mode=ChatMode.SERVICE,
        intent=UserIntent.SERVICE_INQUIRY,
        retrieval_required=False,
        service_handoff_suggested=True,
    )


def _retrieval(content: str) -> RetrievalResult:
    return RetrievalResult(tool_name="reference_search", query="ctx", content=content)


def test_answer_grader_skips_grounding_when_retrieval_not_required():
    grade = AnswerGrader().grade("No citation needed here.", _decision(retrieval_required=False), None)

    assert grade.adequate is True
    assert grade.reason == "retrieval_not_required"


def test_answer_grader_accepts_source_line_with_context():
    grade = AnswerGrader().grade(
        "CTX is available in the platform.\nSource: docs/automation.md",
        _decision(),
        _retrieval("Result 1\nSource: docs/automation.md\nContent:\nCTX info"),
    )

    assert grade.adequate is True
    assert grade.reason == "grounded_or_declined"


def test_answer_grader_rejects_filename_without_source_marker():
    grade = AnswerGrader().grade(
        "Open docs/automation.md and use CTX.",
        _decision(),
        _retrieval("Result 1\nSource: docs/automation.md\nContent:\nCTX info"),
    )

    assert grade.adequate is False
    assert grade.reason == "missing_citation"


def test_answer_grader_accepts_labeled_general_fallback_without_context():
    grader = AnswerGrader()
    no_context = _retrieval("No relevant information found")

    accepted = grader.grade(UNKNOWN_ANSWER, _decision(), no_context)
    general = grader.grade(
        "I did not find a relevant Library hit. General guidance outside Library: start with user creation, licensing, group assignment, and welcome notification.",
        _decision(),
        no_context,
    )
    rejected = grader.grade("Start with user creation, licensing, group assignment, and welcome notification.", _decision(), no_context)

    assert accepted.adequate is True
    assert general.adequate is True
    assert rejected.adequate is False
    assert rejected.reason == "no_retrieval_context"


def test_answer_grader_accepts_direct_general_workflow_guidance_without_context():
    grade = AnswerGrader().grade(
        "Start with user creation, licensing, group assignment, and welcome notification.",
        _decision(retrieval_tool="workflow_pattern_search"),
        _retrieval("No relevant information found"),
    )

    assert grade.adequate is True
    assert grade.reason == "general_guidance_no_context"


def test_answer_grader_requires_citation_for_library_source_claim():
    grade = AnswerGrader().grade(
        "I found this in Library: use the onboarding bundle.",
        _decision(retrieval_tool="workflow_pattern_search"),
        _retrieval("Result 1\nSource: workflows/onboarding.bundle.json\nContent:\nOnboarding bundle"),
    )

    assert grade.adequate is False
    assert grade.reason == "missing_citation"


def test_answer_grader_allows_public_library_links_when_retrieved():
    answer = "Use the public LIBRARY entry point: https://example.com/library.\nSource: INDEX.md"
    retrieval = _retrieval(
        "Result 1\nSource: INDEX.md\nLink: https://github.com/example-org/library-repo/blob/main/INDEX.md\nLibrary: https://example.com/library\nContent:\nLIBRARY index"
    )

    grade = AnswerGrader().grade(answer, _decision(), retrieval)

    assert grade.adequate is True


def test_answer_grader_repairs_missing_citation_to_unknown():
    grader = AnswerGrader()
    grade = grader.grade(
        "CTX is context data.",
        _decision(),
        _retrieval("Result 1\nSource: docs/automation.md\nContent:\nCTX info"),
    )

    repaired = grader.repair("CTX is context data.", grade)

    assert repaired == UNKNOWN_ANSWER


def test_answer_grader_rejects_automator_service_cta_without_handoff():
    grade = AnswerGrader().grade(
        "If you're ready to explore how our services can help, submit our contact form.\nSource: docs/automation.md",
        _decision(),
        _retrieval("Result 1\nSource: docs/automation.md\nContent:\nCTX info"),
    )

    assert grade.adequate is False
    assert grade.reason == "unexpected_service_handoff"


def test_answer_grader_rejects_service_pricing_and_third_party_menus():
    grader = AnswerGrader()

    price = grader.grade("Typical consulting runs $150 per hour.", _service_decision(), None)
    menu = grader.grade("Option 1: our team. Option 2: third-party implementation through a freelancer.", _service_decision(), None)
    safe = grader.grade("our team scopes pricing after reviewing systems, risk, and support needs.", _service_decision(), None)

    assert price.adequate is False
    assert price.reason == "forbidden_service_commercial_claim"
    assert menu.adequate is False
    assert menu.reason == "forbidden_service_commercial_claim"
    assert safe.adequate is True


def test_answer_grader_rejects_external_url_not_present_in_retrieval():
    grade = AnswerGrader().grade(
        "Check https://github.com/not-approved/library for bundles.\nSource: docs/automation.md",
        _decision(),
        _retrieval("Result 1\nSource: docs/automation.md\nContent:\nCTX info"),
    )

    assert grade.adequate is False
    assert grade.reason == "unsupported_external_link"


def test_answer_grader_allows_approved_public_resource_with_general_fallback(monkeypatch):
    monkeypatch.setenv("APPROVED_PUBLIC_URLS", "https://github.com/community-org/library-samples")
    grade = AnswerGrader().grade(
        "I did not get a directly usable Library result. General guidance outside Library: check https://github.com/community-org/library-samples for community workflow ideas.",
        _decision(),
        _retrieval("No relevant information found"),
    )

    assert grade.adequate is True
    assert grade.reason == "no_retrieval_context"


def test_answer_grader_allows_labeled_general_fallback_with_noisy_context(monkeypatch):
    monkeypatch.setenv("APPROVED_PUBLIC_URLS", "https://github.com/community-org/library-samples")
    grade = AnswerGrader().grade(
        "I did not get a directly usable Library result. General guidance outside Library: check https://github.com/community-org/library-samples for community workflow ideas.",
        _decision(),
        _retrieval("Result 1\nSource: schemas/noisy.json\nContent:\nNot useful for onboarding."),
    )

    assert grade.adequate is True
    assert grade.reason == "labeled_general_fallback"


def test_answer_grader_repairs_empty_answer_to_unknown():
    grader = AnswerGrader()
    grade = grader.grade(
        "",
        _decision(),
        _retrieval("Result 1\nSource: docs/automation.md\nContent:\nCTX info"),
    )

    assert grade.adequate is False
    assert grade.reason == "empty_answer"
    assert grader.repair("", grade) == UNKNOWN_ANSWER