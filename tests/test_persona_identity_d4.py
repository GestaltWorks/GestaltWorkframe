"""Identity preamble tests.

Every persona shares the DEFAULT_BOT_IDENTITY preamble so the assistant
behaves with a consistent voice regardless of which model the router
selects. Deployments override the preamble via `identity.bot_persona`
in their deployment bundle; this test verifies the framework default.
"""

from __future__ import annotations

from gestaltworkframe.core.personas import (
    AUTOMATOR_PERSONA,
    DEFAULT_BOT_IDENTITY,
    EDUCATOR_PERSONA,
    PIPELINE_PERSONA,
)


def test_identity_block_establishes_model_consistency():
    assert "Whatever model is running you" in DEFAULT_BOT_IDENTITY


def test_identity_block_documents_library_grounding():
    assert "curated knowledge library" in DEFAULT_BOT_IDENTITY
    assert "Never fabricate" in DEFAULT_BOT_IDENTITY


def test_identity_block_enforces_voice_rules():
    assert "No em dashes" in DEFAULT_BOT_IDENTITY
    assert "No manufactured enthusiasm" in DEFAULT_BOT_IDENTITY
    assert "No generic vendor filler" in DEFAULT_BOT_IDENTITY
    assert "\u2014" not in DEFAULT_BOT_IDENTITY


def test_identity_block_documents_posture():
    assert "Surface library content naturally" in DEFAULT_BOT_IDENTITY
    assert "do not invent" in DEFAULT_BOT_IDENTITY.lower()


def test_every_persona_prepends_identity():
    for persona in (PIPELINE_PERSONA, AUTOMATOR_PERSONA, EDUCATOR_PERSONA):
        assert persona.system_prompt.startswith(DEFAULT_BOT_IDENTITY[:80]), persona.id


def test_every_persona_has_a_named_mode_section():
    assert "MODE: Service Inquiry" in PIPELINE_PERSONA.system_prompt
    assert "MODE: Technical Assistance" in AUTOMATOR_PERSONA.system_prompt
    assert "MODE: Educator" in EDUCATOR_PERSONA.system_prompt
