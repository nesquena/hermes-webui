"""
Tests for the MEDIA-in-stream fix: MEDIA:<ref> tokens that arrive mid-turn
during streaming used to render as the raw path text until the turn settled
and the full renderMd() pipeline re-rendered the row. The fix lets the smd
streaming renderer replace MEDIA tokens with the same DOM the full pipeline
emits, so live prose shows real images inline.

Coverage:
1. messages.js: _smdMediaAwareAddText wrapper exists and references
   _inlineMediaHtmlForRef (the shared renderer from ui.js).
2. messages.js: _safeSmdRenderer's add_text wraps every text chunk through
   the MEDIA-aware interceptor.
3. messages.js: _streamFadeRenderer also short-circuits to the MEDIA-aware
   interceptor when its chunk carries a MEDIA token, instead of wrapping
   the token in a stream-fade-word span.
4. ui.js: a single _inlineMediaHtmlForRef function is the canonical
   renderer used by BOTH renderMd() MEDIA restore and the streaming path.
"""
from __future__ import annotations

import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).parent.parent
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")


class TestSmdMediaInStream(unittest.TestCase):
    """Verify the streaming smd path produces real <img> for MEDIA tokens."""

    def test_inline_media_renderer_exists_in_ui_js(self):
        self.assertIn(
            "function _inlineMediaHtmlForRef",
            UI_JS,
            "ui.js must export _inlineMediaHtmlForRef so messages.js can reuse it",
        )

    def test_render_md_media_restore_uses_shared_renderer(self):
        # The renderMd MEDIA restore pass now delegates to the shared helper
        # instead of carrying its own copy of the URL → HTML mapping.
        marker = "_inlineMediaHtmlForRef(media_stash["
        self.assertIn(
            marker, UI_JS,
            "renderMd MEDIA restore must delegate to _inlineMediaHtmlForRef "
            "so the live + settled representations of the same MEDIA token "
            "stay byte-identical",
        )

    def test_messages_has_smd_media_aware_wrapper(self):
        self.assertIn(
            "function _smdMediaAwareAddText",
            MESSAGES_JS,
            "messages.js must define _smdMediaAwareAddText to convert MEDIA "
            "tokens into DOM elements at smd insert time",
        )

    def test_smd_media_aware_wrapper_invokes_shared_renderer(self):
        # The whole point of the fix: the streaming path uses the SAME renderer
        # the renderMd pipeline uses. If messages.js constructed the HTML
        # inline, the live + settled images could diverge.
        idx = MESSAGES_JS.index("function _smdMediaAwareAddText")
        block = MESSAGES_JS[idx:idx + 4000]
        self.assertIn(
            "_inlineMediaHtmlForRef", block,
            "_smdMediaAwareAddText must call _inlineMediaHtmlForRef to keep "
            "streaming and settled MEDIA paths byte-identical",
        )

    def test_safe_smd_renderer_wraps_add_text_with_media_interceptor(self):
        idx = MESSAGES_JS.index("function _safeSmdRenderer")
        block = MESSAGES_JS[idx:idx + 2000]
        self.assertIn(
            "_smdMediaAwareAddText", block,
            "_safeSmdRenderer's add_text override must route text chunks "
            "through _smdMediaAwareAddText so MEDIA tokens become DOM nodes",
        )

    def test_stream_fade_renderer_short_circuits_media_chunks(self):
        idx = MESSAGES_JS.index("function _streamFadeRenderer")
        block = MESSAGES_JS[idx:idx + 4000]
        self.assertIn(
            "_smdMediaAwareAddText", block,
            "_streamFadeRenderer's add_text override must short-circuit to "
            "_smdMediaAwareAddText when the chunk carries a MEDIA token "
            "(otherwise the token would be wrapped in a stream-fade-word "
            "span and stay visible as literal text)",
        )

    def test_media_interceptor_handles_token_at_chunk_start(self):
        # The smd parser can split chunks mid-text. The fix must handle MEDIA
        # tokens wherever they appear in a single add_text call, not just at
        # the boundary of the chunk.
        idx = MESSAGES_JS.index("function _smdMediaAwareAddText")
        block = MESSAGES_JS[idx:idx + 4000]
        self.assertIn("/MEDIA:", block,
                      "Interceptor must scan every chunk for MEDIA tokens")

    def test_media_interceptor_falls_back_to_base_when_no_token(self):
        # Fast path: when the chunk carries no MEDIA, the wrapper should
        # delegate to the underlying add_text unchanged so the fade renderer's
        # word-by-word animation is preserved for plain prose.
        idx = MESSAGES_JS.index("function _smdMediaAwareAddText")
        block = MESSAGES_JS[idx:idx + 4000]
        # Either a guard like !/MEDIA:/.test(...) → baseAddText, or an "if
        # (!host...) baseAddText" fallback when parsing produced nothing.
        self.assertTrue(
            "baseAddText(data,value)" in block,
            "Interceptor must delegate to baseAddText for plain text so the "
            "underlying renderer's semantics survive",
        )

    def test_no_recursive_infinite_loop_via_baseAddText(self):
        # Regression guard: the fade renderer's add_text is itself a wrapper.
        # If the MEDIA interceptor re-routed ALL chunks through baseAddText
        # regardless of token presence, plain prose would re-enter the fade
        # wrapper and on the next chunk also be re-processed. The fast-path
        # delegation happens only when /MEDIA:/ does NOT match.
        idx = MESSAGES_JS.index("function _smdMediaAwareAddText")
        block = MESSAGES_JS[idx:idx + 4000]
        # The "no MEDIA" branch must be reachable without further MEDIA checks.
        self.assertIn("!/MEDIA:/.test(value)", block,
                      "Interceptor must have an early-return fast path when "
                      "the chunk lacks a MEDIA token")


if __name__ == "__main__":
    import unittest
    unittest.main()
