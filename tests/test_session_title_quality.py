"""Quality checks for session title derivation heuristics."""

from api.models import title_from


def test_title_from_skips_low_signal_openers():
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi!"},
        {"role": "user", "content": "Need help designing session rename UX"},
    ]
    assert title_from(messages) == "Need help designing session rename UX"


def test_title_from_strips_preamble_and_markdown_noise():
    messages = [
        {"role": "user", "content": "Title: **Improve session naming strategy**"},
    ]
    assert title_from(messages) == "Improve session naming strategy"
