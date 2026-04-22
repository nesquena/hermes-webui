"""Regression tests for #853 (image_generate inline rendering) and
#857 (auto-title strips thinking preambles)."""
import os
import re


_SRC = os.path.join(os.path.dirname(__file__), "..")


def _read(name):
    return open(os.path.join(_SRC, name), encoding="utf-8").read()


# ── #853: MEDIA: URL restore renders any https:// as <img> ────────────────────

class TestMediaUrlRendersInline:
    """The `MEDIA:` stash restore in renderMd() must render https:// URLs
    as <img> even when they lack a recognized file extension (FAL.ai and
    similar CDNs serve images via content-addressed paths)."""

    def test_render_md_checks_https_scheme_for_img_tag(self):
        js = _read("static/ui.js")
        # The fix OR-chains the extension check with a scheme check so any
        # https:// URL in a MEDIA: token renders as <img>
        assert re.search(
            r"_IMAGE_EXTS\.test\(.*?\)\s*\|\|\s*/\^https\?:\\/\\/",
            js,
        ), (
            "renderMd MEDIA: restore must accept any https:// URL as <img>, "
            "not only those with image file extensions (#853)"
        )

    def test_img_class_applied_to_media_image(self):
        """The resulting <img> uses the existing msg-media-img class so the
        styling/fullscreen-click interaction is preserved."""
        js = _read("static/ui.js")
        # The img tag is constructed with the existing class + onclick toggle
        assert "msg-media-img" in js
        assert "msg-media-img--full" in js


# ── #857: thinking-preamble stripping in auto-title ──────────────────────────

class TestThinkingPreambleStripping:
    """Qwen3 and similar models emit plain-text thinking preambles
    ("Here's a thinking process:", "Let me think through this…") without
    <think> tags. These must be stripped before the text is used for
    session auto-titling."""

    def test_strip_thinking_markup_drops_heres_thinking_process(self):
        from api.streaming import _strip_thinking_markup
        raw = "Here's a thinking process: 1. Analyze the request.\nThe answer is 42."
        out = _strip_thinking_markup(raw)
        assert "thinking process" not in out.lower()
        assert "42" in out, "Non-preamble content must be preserved"

    def test_strip_thinking_markup_drops_let_me_think(self):
        from api.streaming import _strip_thinking_markup
        raw = "Let me think through this carefully.\nHere's the answer."
        out = _strip_thinking_markup(raw)
        assert "let me think" not in out.lower()

    def test_strip_thinking_markup_drops_ill_think_about(self):
        from api.streaming import _strip_thinking_markup
        raw = "I'll think about this step by step.\nFinal result: 7."
        out = _strip_thinking_markup(raw)
        assert "i'll think about" not in out.lower()
        assert "result" in out.lower()

    def test_strip_thinking_markup_drops_okay_let_me(self):
        from api.streaming import _strip_thinking_markup
        raw = "Okay, let me break this down.\nThe answer is yes."
        out = _strip_thinking_markup(raw)
        assert "okay, let me break" not in out.lower()

    def test_strip_thinking_markup_preserves_non_preamble_content(self):
        """When the text doesn't start with a thinking preamble, leave it alone."""
        from api.streaming import _strip_thinking_markup
        raw = "The user's question is about Python imports."
        out = _strip_thinking_markup(raw)
        assert "python imports" in out.lower()

    def test_strip_thinking_markup_case_insensitive(self):
        from api.streaming import _strip_thinking_markup
        assert "Here's" not in _strip_thinking_markup("HERE'S A THINKING PROCESS:\nThe answer.")

    def test_looks_invalid_generated_title_catches_heres_thinking(self):
        """The belt-and-suspenders guard on titles that slip past the strip."""
        from api.streaming import _looks_invalid_generated_title
        assert _looks_invalid_generated_title("Here's a thinking process about Python"), (
            "_looks_invalid_generated_title must reject 'Here's a thinking ...' titles"
        )

    def test_looks_invalid_generated_title_accepts_real_titles(self):
        """Normal, non-preamble titles must not be rejected."""
        from api.streaming import _looks_invalid_generated_title
        assert not _looks_invalid_generated_title("Python import debugging"), (
            "Real titles must still pass the invalid-title guard"
        )
