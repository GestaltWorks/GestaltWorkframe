from pathlib import Path


def test_terminal_widget_renders_assistant_output_as_text_only():
    source = Path("web/src/components/ChatWidget.tsx").read_text(encoding="utf-8")

    assert "dangerouslySetInnerHTML" not in source
    assert ".innerHTML" not in source
    assert "<Markdown" not in source
    assert "{msg.content}" in source


def test_terminal_widget_does_not_turn_model_urls_into_links():
    source = Path("web/src/components/ChatWidget.tsx").read_text(encoding="utf-8")

    transcript_block = source[source.index("messages.map") : source.index("{isTyping")]
    assert "href=" not in transcript_block
    assert "target=" not in transcript_block