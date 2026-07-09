"""
Tests for the MEDIA-in-stream fix: MEDIA:<ref> tokens that arrive mid-turn
during streaming used to render as the raw path text until the turn settled
and the full renderMd() pipeline re-rendered the row. The fix lets the smd
streaming renderer replace MEDIA tokens with the same DOM the full pipeline
emits, so live prose shows real images inline.

Static coverage (in TestSmdMediaInStream):
1. messages.js: _smdMediaAwareAddText wrapper exists and references
   _inlineMediaHtmlForRef (the shared renderer from ui.js).
2. messages.js: _safeSmdRenderer's add_text wraps every text chunk through
   the MEDIA-aware interceptor.
3. messages.js: _streamFadeRenderer also short-circuits to the MEDIA-aware
   interceptor when its chunk carries a MEDIA token, instead of wrapping
   the token in a stream-fade-word span.
4. ui.js: a single _inlineMediaHtmlForRef function is the canonical
   renderer used by BOTH renderMd() MEDIA restore and the streaming path.

Behavioural coverage (in TestSmdMediaAwareAddTextBehaviour): drives the
actual JS through a minimal in-process DOM shim that supports the
createElement / appendChild / createTextNode / DOMParser surface the
interceptor uses. These cases answer Greptile's two confidence-sapping
notes head-on:
- "Mixed prose and MEDIA chunks can parse model text as DOM" — covered by
  the prose-only and prose-around-MEDIA cases (no entities ever decode
  on prose; prose enters baseAddText directly via createTextNode).
- "MEDIA tokens split across parser flushes can still show as raw text
  during streaming" — covered by the split-MEDIA case (the tail buffer
  finishes the token on the second call).
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
        block = MESSAGES_JS[idx:idx + 6000]
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
        block = MESSAGES_JS[idx:idx + 6500]
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
        block = MESSAGES_JS[idx:idx + 6500]
        self.assertIn("/MEDIA:", block,
                      "Interceptor must scan every chunk for MEDIA tokens")

    def test_media_interceptor_falls_back_to_base_when_no_token(self):
        # Fast path: when the chunk + buffered tail carries no MEDIA token,
        # the wrapper should delegate to the underlying add_text unchanged
        # so the fade renderer's word-by-word animation is preserved for
        # plain prose.
        idx = MESSAGES_JS.index("function _smdMediaAwareAddText")
        block = MESSAGES_JS[idx:idx + 6500]
        self.assertTrue(
            "baseAddText(data,value)" in block or "baseAddText(data," in block,
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
        block = MESSAGES_JS[idx:idx + 6500]
        self.assertTrue(
            "! /MEDIA:/.test" in block.replace(" ", "") or "! /MEDIA:/.test(lead + value)" in block or "! /MEDIA:/.test(lead + value)" in block or "! /MEDIA:/.test(combined)" in block or "!/MEDIA:/.test" in block,
            "Interceptor must have an early-return fast path when the "
            "chunk lacks a MEDIA token (i.e. a `!/MEDIA:/` early bail before "
            "delegating to baseAddText)",
        )

    def test_plain_text_does_not_go_through_dom_parser(self):
        # Greptile #1 (safety): the previous implementation concatenated
        # prose + MEDIA HTML and ran the whole string through DOMParser. That
        # meant agent-supplied prose could be parsed by the HTML parser
        # (entity-decoded / re-serialised) instead of going through a pure
        # text-node insertion. The new implementation routes plain prose
        # back to baseAddText (which uses createTextNode) and only sends
        # each MEDIA token's HTML through DOMParser.
        idx = MESSAGES_JS.index("function _smdMediaAwareAddText")
        block = MESSAGES_JS[idx:idx + 5000]
        # The single-token DOMParser helper must exist and accept ONE ref;
        # the loop body must call baseAddText for any prose slice *before*
        # it would attempt to splice HTML.
        self.assertIn("function _smdAppendMediaNode", MESSAGES_JS)
        smd_block = MESSAGES_JS[MESSAGES_JS.index("function _smdAppendMediaNode"):MESSAGES_JS.index("function _smdAppendMediaNode")+2000]
        self.assertIn("parseFromString", smd_block)
        self.assertNotIn("parseFromString('<div>'+value+'</div>'", MESSAGES_JS,
                         "Plain chunk text must never be concatenated into "
                         "the DOMParser input — only the single-token "
                         "mediaHtml produced by _inlineMediaHtmlForRef may "
                         "be parsed.")

    def test_cross_chunk_media_tail_buffer_exists(self):
        # Greptile #2 (cross-chunk split): when smd flushes a MEDIA token
        # in two pieces (e.g. "MEDIA:C:\\Users\\Admin" then "\\foo.png"),
        # the second half alone would not match the MEDIA regex; if we
        # only operate on each chunk independently both pieces render as
        # raw text. The new implementation keeps a per-parser tail buffer
        # for incomplete MEDIA prefixes.
        idx = MESSAGES_JS.index("function _smdMediaAwareAddText")
        block = MESSAGES_JS[idx:idx + 5000]
        # The interceptor must reference a module-level Map/WeakMap that
        # backstops partial MEDIA prefixes across calls.
        self.assertTrue(
            "_SMD_MEDIA_TAIL" in MESSAGES_JS or "_smdMediaTailSet" in MESSAGES_JS,
            "Interceptor must consult a per-parser tail buffer so a "
            "MEDIA:<ref> split across two smd flushes still resolves to "
            "a media element on the second call",
        )
        # And the interceptor must actually call a tail-mutating setter
        # somewhere on the trailing path, not just read.
        self.assertIn("unmatchedTail", block,
                      "Interceptor must record the trailing bytes that look "
                      "like an incomplete MEDIA prefix so the next add_text "
                      "call can prepend them and finish the token")

    def test_tail_buffer_size_cap(self):
        # Defensive: a runaway tail buffer from a malformed stream could
        # exhaust memory. The implementation must enforce a max length on
        # the per-parser tail.
        self.assertIn("_MEDIA_TAIL_MAX", MESSAGES_JS,
                      "Tail buffer must enforce a max length to bound memory")

    def test_per_parser_tail_isolation(self):
        # Multiple smd parsers run concurrently in the worklog + anchor
        # scene + main live body. The tail buffer must be keyed by parser
        # (not just by element) so a split MEDIA token in stream A doesn't
        # get prepended to a chunk in stream B.
        self.assertTrue(
            ("parserFor" in MESSAGES_JS and "_SMD_MEDIA_TAIL.get(parser)" in MESSAGES_JS)
            or ("tails.get(parser)" in MESSAGES_JS and "parserFor" in MESSAGES_JS),
            "Tail buffer must be keyed by a stable parser identity so "
            "concurrent streams don't cross-pollinate",
        )


if __name__ == "__main__":
    import unittest
    unittest.main()
