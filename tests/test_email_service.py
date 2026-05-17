from core.email_service import _build_html


def test_contact_email_uses_current_services_url() -> None:
    html = _build_html(
        "interested_party",
        "A User",
        "user@example.com",
        {"problem_statement": "Need help with routing."},
    )

    assert "<meta charset='utf-8'>" in html
    assert "Contact intake" in html
    assert "services-RnD" not in html


def test_contact_email_escapes_user_supplied_values() -> None:
    html = _build_html(
        "interested_party",
        "<script>A User</script>",
        "user@example.com",
        {"notes": "<b>raw html</b>"},
    )

    assert "<script>" not in html
    assert "&lt;script&gt;A User&lt;/script&gt;" in html
    assert "&lt;b&gt;raw html&lt;/b&gt;" in html
