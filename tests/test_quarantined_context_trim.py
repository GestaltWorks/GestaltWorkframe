"""Tests for the quarantined-context length trimming in ChatTurnOrchestrator.

Untrusted context (KB retrieval, tool results, intake) is wrapped in a
BEGIN_UNTRUSTED_CONTEXT envelope. Without a cap, a chatty retriever or
a tool result with a giant log can blow past the model's context
window. The trim keeps a head + tail slice with a visible truncation
marker in between.
"""

from __future__ import annotations

import core.chat_orchestrator as chat_mod
from core.chat_orchestrator import ChatTurnOrchestrator


def _orchestrator() -> ChatTurnOrchestrator:
    """Build a ChatTurnOrchestrator stub that only needs the trim method.

    The trim method does not touch any other collaborator, so we can
    bypass full construction by allocating an instance and calling the
    method directly. This avoids dragging the whole AppServices stack
    into a one-method unit test.
    """
    return ChatTurnOrchestrator.__new__(ChatTurnOrchestrator)


def test_short_content_passes_through_unchanged():
    orch = _orchestrator()
    out = orch._trim_quarantined_payload("short payload")
    assert out == "short payload"


def test_long_content_is_trimmed_to_head_plus_tail_with_marker(monkeypatch):
    monkeypatch.setattr(chat_mod, "QUARANTINED_CONTEXT_MAX_CHARS", 200)
    orch = _orchestrator()
    payload = ("AAAA" * 200) + ("BBBB" * 200)  # 1600 chars

    out = orch._trim_quarantined_payload(payload)

    assert chat_mod.QUARANTINED_CONTEXT_TRUNCATION_MARKER in out
    assert out.startswith("A")  # head retained
    assert out.endswith("B")    # tail retained
    # Length is at most the configured cap (the marker counts against the budget).
    assert len(out) <= 200 + len(chat_mod.QUARANTINED_CONTEXT_TRUNCATION_MARKER)


def test_disabled_when_max_is_zero(monkeypatch):
    monkeypatch.setattr(chat_mod, "QUARANTINED_CONTEXT_MAX_CHARS", 0)
    orch = _orchestrator()
    payload = "X" * 10_000
    assert orch._trim_quarantined_payload(payload) == payload


def test_envelope_includes_trimmed_marker_in_full_quarantine(monkeypatch):
    """End-to-end: the public _quarantined_context method applies the trim."""
    monkeypatch.setattr(chat_mod, "QUARANTINED_CONTEXT_MAX_CHARS", 200)
    orch = _orchestrator()
    huge = "Z" * 2000

    enveloped = orch._quarantined_context("tool result from kb_overview", huge)

    assert "BEGIN_UNTRUSTED_CONTEXT" in enveloped
    assert "END_UNTRUSTED_CONTEXT" in enveloped
    assert chat_mod.QUARANTINED_CONTEXT_TRUNCATION_MARKER in enveloped
    # The full 2000 Zs should NOT be in the envelope.
    assert "Z" * 2000 not in enveloped
