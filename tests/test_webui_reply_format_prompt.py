"""WebUI ephemeral prompt asks for sectioned markdown finals (report layout)."""

from api.streaming import _WEBUI_REPLY_FORMAT_PROMPT, _webui_ephemeral_system_prompt


def test_reply_format_prompt_requires_markdown_headings_and_lists():
    assert "WebUI reply format" in _WEBUI_REPLY_FORMAT_PROMPT
    assert "##" in _WEBUI_REPLY_FORMAT_PROMPT or "markdown heading" in _WEBUI_REPLY_FORMAT_PROMPT.lower()
    assert "bullet" in _WEBUI_REPLY_FORMAT_PROMPT.lower() or "numbered" in _WEBUI_REPLY_FORMAT_PROMPT.lower()
    assert "dense paragraph" in _WEBUI_REPLY_FORMAT_PROMPT.lower() or "one dense" in _WEBUI_REPLY_FORMAT_PROMPT.lower()


def test_reply_format_prompt_skips_structure_for_short_answers():
    lower = _WEBUI_REPLY_FORMAT_PROMPT.lower()
    assert "short" in lower or "one-liner" in lower or "one liner" in lower
    assert "skip" in lower or "plain" in lower


def test_reply_format_flows_into_ephemeral_system_prompt():
    prompt = _webui_ephemeral_system_prompt(
        "Use a concise tone.",
        surface_context={"source": "webui"},
    )

    assert _WEBUI_REPLY_FORMAT_PROMPT in prompt
    assert "WebUI progress guidance" in prompt
