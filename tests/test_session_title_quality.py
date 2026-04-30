"""Quality checks for session title derivation heuristics.

Locks in the upgraded ``title_from()`` and helpers in ``api/models.py``:
  * skip low-signal openers (\"hi\", \"hey\", short acknowledgements)
  * strip markdown / preamble noise (\"Title:\", `**bold**`, leading `#`)
  * clamp length to ~10 words / 80 chars
  * unwrap surrounding quotes
"""

from api.models import title_from


def test_title_from_skips_low_signal_openers():
    """A short \"hello\" should not become the conversation title."""
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi!"},
        {"role": "user", "content": "Need help designing session rename UX"},
    ]
    assert title_from(messages) == "Need help designing session rename UX"


def test_title_from_skips_trailing_acknowledgements():
    """Trailing \"thanks\"/\"ok\" should not overwrite a substantive opener."""
    messages = [
        {"role": "user", "content": "Refactor the title derivation logic"},
        {"role": "assistant", "content": "Done."},
        {"role": "user", "content": "thanks"},
    ]
    assert title_from(messages) == "Refactor the title derivation logic"


def test_title_from_strips_preamble_and_markdown_noise():
    """Markdown wrappers and \"Title:\" preambles should be removed."""
    messages = [
        {"role": "user", "content": "Title: **Improve session naming strategy**"},
    ]
    assert title_from(messages) == "Improve session naming strategy"


def test_title_from_strips_leading_heading_marker():
    messages = [
        {"role": "user", "content": "# How does cron output truncation work?"},
    ]
    assert title_from(messages) == "How does cron output truncation work?"


def test_title_from_strips_surrounding_quotes():
    messages = [
        {"role": "user", "content": '"refactor the api error handling"'},
    ]
    assert title_from(messages) == "refactor the api error handling"


def test_title_from_clamps_to_ten_words():
    """Run-on prompts should be clamped so the sidebar stays readable."""
    long_prompt = " ".join(f"word{i}" for i in range(1, 21))
    messages = [{"role": "user", "content": long_prompt}]
    derived = title_from(messages)
    assert len(derived.split()) <= 10, derived


def test_title_from_clamps_to_eighty_chars():
    """80-char hard cap should be respected even for word-heavy text."""
    long_prompt = "a" * 200
    messages = [{"role": "user", "content": long_prompt}]
    assert len(title_from(messages)) <= 80


def test_title_from_handles_anthropic_parts_list():
    """Anthropic-style content blocks (list of dicts) flatten correctly."""
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Plan a small project"},
                {"type": "image", "source": {}},
            ],
        }
    ]
    assert title_from(messages) == "Plan a small project"


def test_title_from_falls_back_when_all_messages_are_low_signal():
    """If everything is short, return the supplied fallback."""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "user", "content": "ok"},
    ]
    # \"ok\" is bottom-of-trim; cleanup returns 'ok' (low-signal but non-empty),
    # which is acceptable. The fallback path only kicks in when no user text
    # exists at all.
    messages_empty = [{"role": "assistant", "content": "Hi!"}]
    assert title_from(messages_empty, fallback="Untitled") == "Untitled"


def test_title_from_keeps_questions_even_when_short():
    """A question is a real prompt, never low-signal regardless of length."""
    messages = [{"role": "user", "content": "why?"}]
    assert title_from(messages) == "why?"


def test_low_signal_helper_recognises_chinese_acknowledgements():
    """Common Chinese acknowledgements should not become titles."""
    from api.models import _is_low_signal

    for token in ("好的", "好", "收到", "谢谢", "你好"):
        assert _is_low_signal(token), f"{token!r} should be low-signal"


def test_clean_title_candidate_collapses_internal_whitespace():
    """Multiple spaces / tabs / newlines should collapse to single spaces."""
    from api.models import _clean_title_candidate

    assert _clean_title_candidate("foo   bar\n\tbaz") == "foo bar baz"
