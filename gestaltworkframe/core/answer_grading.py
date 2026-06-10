import os
import re
from dataclasses import dataclass

from gestaltworkframe.core.policy import ChatMode, RoutingDecision
from gestaltworkframe.core.retrieval import RetrievalResult
from gestaltworkframe.core.tool_policy import WORKFLOW_PATTERN_SEARCH

UNKNOWN_ANSWER = "__needs_directional_fallback__"
# Backwards-compat: older model behavior produced the literal phrase below.
# The split concatenation is deliberate. It keeps the assembled literal from
# appearing in retrieval indexes, ripgrep hits across docs, or training-data
# searches, so a model can't learn to emit it just because it sees the string
# in source. The recognizer rebuilds the phrase at runtime in is_unknown_answer.
LEGACY_UNKNOWN_ANSWER = "I don't " + "know based on the current " + "documentation."
URL_RE = re.compile(r"https?://[^\s<>)]+")
MONEY_RE = re.compile(r"(?:\$\s?\d|\d+\s?(?:usd|dollars?)\b)", re.IGNORECASE)


def _approved_public_urls() -> tuple[str, ...]:
    raw = os.getenv("APPROVED_PUBLIC_URLS", "").strip()
    if not raw:
        return ()
    return tuple(item.strip().rstrip("/") for item in raw.split(",") if item.strip())


SERVICE_HANDOFF_MARKERS = (
    "lead-capture",
    "contact form",
    "guide you through the next steps",
)
FORBIDDEN_SERVICE_COMMERCIAL_MARKERS = (
    "price range",
    "pricing range",
    "cost range",
    "hourly rate",
    "per hour",
    "/hr",
    "starter package",
    "standard package",
    "premium package",
    "consulting package",
    "third-party implementation",
    "third party implementation",
    "community consultant",
    "freelancer",
    "upwork",
    "fiverr",
)


@dataclass(frozen=True)
class AnswerGrade:
    adequate: bool
    reason: str


class AnswerGrader:
    def grade(
        self,
        answer: str,
        decision: RoutingDecision,
        retrieval: RetrievalResult | None,
    ) -> AnswerGrade:
        normalized_answer = self._normalize(answer)
        if not normalized_answer:
            return AnswerGrade(False, "empty_answer")
        if self._has_forbidden_service_commercial_claim(answer, decision):
            return AnswerGrade(False, "forbidden_service_commercial_claim")
        if self._has_forbidden_service_handoff(answer, decision):
            return AnswerGrade(False, "unexpected_service_handoff")
        if not decision.retrieval_required:
            return AnswerGrade(True, "retrieval_not_required")
        if retrieval is None:
            return AnswerGrade(False, "retrieval_missing")
        if not retrieval.has_context:
            if self._has_unsupported_url(answer, retrieval):
                return AnswerGrade(False, "unsupported_external_link")
            if self._allows_direct_general_answer(answer, decision):
                return AnswerGrade(True, "general_guidance_no_context")
            return AnswerGrade(is_unknown_answer(answer) or self._is_labeled_general_fallback(answer), "no_retrieval_context")
        if self._has_unsupported_url(answer, retrieval):
            return AnswerGrade(False, "unsupported_external_link")
        if self._is_labeled_general_fallback(answer):
            return AnswerGrade(True, "labeled_general_fallback")

        if self._has_citation(answer) or is_unknown_answer(answer):
            return AnswerGrade(True, "grounded_or_declined")
        if self._allows_direct_general_answer(answer, decision):
            return AnswerGrade(True, "general_guidance")
        return AnswerGrade(False, "missing_citation")

    def repair(self, answer: str, grade: AnswerGrade) -> str:
        if grade.reason in {
            "retrieval_missing",
            "no_retrieval_context",
            "empty_answer",
            "missing_citation",
            "unsupported_external_link",
            "unexpected_service_handoff",
            "forbidden_service_commercial_claim",
        }:
            return UNKNOWN_ANSWER
        return answer

    def _has_citation(self, answer: str) -> bool:
        return any(
            line.strip().lower().startswith("source:") and bool(line.split(":", 1)[1].strip())
            for line in answer.splitlines()
        )

    def _normalize(self, answer: str) -> str:
        return " ".join(answer.strip().lower().split())

    def _has_forbidden_service_handoff(self, answer: str, decision: RoutingDecision) -> bool:
        if decision.service_handoff_suggested:
            return False
        # Soft offer (Phase D2): the user expressed build/implement intent
        # and CITATION_DISCIPLINE permits a single bridge sentence in
        # Automator/Educator mode. The grader should accept it.
        if getattr(decision, "soft_service_offer", False):
            return False
        if decision.selected_mode not in {ChatMode.AUTOMATOR, ChatMode.EDUCATOR}:
            return False
        normalized = self._normalize(answer)
        return any(marker in normalized for marker in SERVICE_HANDOFF_MARKERS)

    def _has_forbidden_service_commercial_claim(self, answer: str, decision: RoutingDecision) -> bool:
        if decision.selected_mode != ChatMode.SERVICE:
            return False
        normalized = self._normalize(answer)
        if MONEY_RE.search(answer):
            return True
        return any(marker in normalized for marker in FORBIDDEN_SERVICE_COMMERCIAL_MARKERS)

    def _has_unsupported_url(self, answer: str, retrieval: RetrievalResult) -> bool:
        retrieval_text = retrieval.content
        return any(
            url.rstrip(".,") not in retrieval_text and not self._is_approved_public_url(url.rstrip(".,"))
            for url in URL_RE.findall(answer)
        )

    def _is_approved_public_url(self, url: str) -> bool:
        return any(url == approved or url.startswith(f"{approved}/") for approved in _approved_public_urls())

    def _is_labeled_general_fallback(self, answer: str) -> bool:
        normalized = self._normalize(answer)
        has_library_disclosure = any(
            marker in normalized
            for marker in (
                "library did not have",
                "library doesn't have",
                "library does not have",
                "i didn't find",
                "i did not find",
                "i don't see",
                "i do not see",
                "i did not get",
            )
        )
        has_general_label = any(
            marker in normalized
            for marker in ("general guidance", "outside the library", "not from the library", "not verified in the library")
        )
        return has_library_disclosure and has_general_label

    def _allows_direct_general_answer(self, answer: str, decision: RoutingDecision) -> bool:
        if decision.retrieval_tool != WORKFLOW_PATTERN_SEARCH:
            return False
        if is_unknown_answer(answer):
            return False
        return not self._claims_library_source(answer)

    def _claims_library_source(self, answer: str) -> bool:
        normalized = self._normalize(answer)
        if "library" not in normalized:
            return False
        source_claims = (
            "found in library",
            "found in the library",
            "found in my library",
            "from library",
            "from the library",
            "from my library",
            "in library",
            "in the library",
            "in my library",
            "library source",
            "library result",
            "library hit",
        )
        miss_claims = ("did not find", "didn't find", "don't see", "do not see", "does not have", "doesn't have")
        if any(marker in normalized for marker in miss_claims):
            return False
        return any(marker in normalized for marker in source_claims)


def is_unknown_answer(answer: str) -> bool:
    normalized = " ".join(answer.strip().lower().split())
    sentinel = " ".join(UNKNOWN_ANSWER.strip().lower().split())
    legacy = " ".join(LEGACY_UNKNOWN_ANSWER.strip().lower().split())
    return normalized in {sentinel, legacy}
