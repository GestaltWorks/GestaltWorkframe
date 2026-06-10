from gestaltworkframe.core.handoff_packets import (
    build_contact_handoff_packet,
    build_terminal_intake_handoff_packet,
    render_packet_html,
    render_packet_text,
)


def test_contact_service_packet_summarizes_safe_review_fields():
    packet = build_contact_handoff_packet(
        "interested_party",
        "A User",
        "user@example.com",
        {
            "company": "Acme MSP",
            "dream_automations": ["Ticket routing", "License cleanup"],
            "timeline": "ASAP",
            "notes": "Need implementation help\x00 this week.",
        },
    )

    assert packet.packet_type == "service_inquiry"
    assert packet.contact == {"name": "A User", "email": "user@example.com"}
    assert "Acme MSP" in packet.summary
    assert "ASAP" in packet.summary
    assert "\x00" not in render_packet_text(packet)
    assert "systems, risk, and desired outcome" in render_packet_text(packet)


def test_terminal_intake_packet_routes_technical_help_to_automator_support():
    packet = build_terminal_intake_handoff_packet(
        "automator",
        {
            "objective": "Get help building or debugging a workflow",
            "building": "Automation onboarding workflow",
            "maturity": "Some workflows already exist",
            "help_needed": "Technical answer I can use",
        },
    )

    assert packet.source == "terminal_intake"
    assert packet.packet_type == "automator_support"
    assert "Automation onboarding workflow" in packet.summary
    assert "failure point" in render_packet_text(packet)


def test_terminal_intake_packet_routes_learning_to_education_interest():
    packet = build_terminal_intake_handoff_packet(
        "pipeline",
        {
            "objective": "Learn how automation works",
            "building": "Training path",
            "maturity": "Just starting",
            "help_needed": "Walk me through it so I understand",
        },
    )

    assert packet.packet_type == "education_interest"
    assert "learning path" in render_packet_text(packet)


def test_packet_html_escapes_user_values():
    packet = build_contact_handoff_packet(
        "student",
        "<script>A User</script>",
        "user@example.com",
        {"learning_notes": "<b>raw html</b>"},
    )

    html = render_packet_html(packet)

    assert "<script>" not in html
    assert "&lt;script&gt;A User&lt;/script&gt;" in html
    assert "&lt;b&gt;raw html&lt;/b&gt;" in html