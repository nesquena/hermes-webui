"""Tests for incremental streaming-markdown (smd) integration in messages.js.

PR: feat: use streaming-markdown for incremental live rendering

The change replaces the per-rAF `assistantBody.innerHTML = renderMd(...)` call
with an incremental DOM-building approach powered by the streaming-markdown
library (https://github.com/nicholasgasior/streaming-markdown):

  - During streaming: smd.parser_write() feeds new text deltas into a live DOM
    tree — no full re-render per frame, no innerHTML thrash.
  - On done/apperror/cancel: smd.parser_end() flushes remaining parser state,
    then Prism / copy buttons / KaTeX are run on the live segment.
  - On tool event: smd.parser_end() finalises the current segment; the next
    token after the tool creates a fresh parser bound to the new assistantBody.
  - Fallback: when window.smd is not yet loaded, the old renderMd path is used.
  - Reconnect: _smdReconnect flag clears stale DOM from the previous parser run
    and restarts the smd parser from the reconnect point.

Tests are static (regex / AST-level) — no browser required.
"""

import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")


# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_fn(src, name, *, brace_depth=1):
    """Return the text of a JS function starting from `function <name>` to its
    closing brace.  Works for both standalone and closure-local functions.
    Does a simple brace-counting walk so it handles nested blocks correctly.
    """
    pattern = rf"function {re.escape(name)}\s*\("
    m = re.search(pattern, src)
    if not m:
        return None
    start = m.start()
    # Find the opening brace
    brace_pos = src.index("{", m.end())
    depth = 1
    pos = brace_pos + 1
    while pos < len(src) and depth > 0:
        ch = src[pos]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        pos += 1
    return src[start:pos]


def extract_event_handler(src, event_name):
    """Return the text of a source.addEventListener('<event_name>', ...) block."""
    pattern = rf"source\.addEventListener\('{re.escape(event_name)}'"
    m = re.search(pattern, src)
    if not m:
        return None
    # Walk forward to collect the matching parenthesis
    paren_depth = 0
    start = m.start()
    pos = m.end()
    # Count back to the opening paren
    paren_depth = 1
    while pos < len(src) and paren_depth > 0:
        ch = src[pos]
        if ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth -= 1
        pos += 1
    return src[start:pos]


def extract_attach_live_stream_prelude(src):
    """Return the text from attachLiveStream opening to the first nested fn."""
    m = re.search(r"function attachLiveStream\(", src)
    if not m:
        return None
    # Find the first nested function definition inside the closure
    inner = re.search(r"\bfunction _isActiveSession\b", src[m.start():])
    if not inner:
        return src[m.start(): m.start() + 5000]
    return src[m.start(): m.start() + inner.start()]


# ── 1. index.html: smd script tag ─────────────────────────────────────────────

class TestIndexHtmlSmdScript:
    """streaming-markdown must be loaded in index.html before messages.js uses it."""

    def test_smd_cdn_url_present(self):
        assert "streaming-markdown" in INDEX_HTML, (
            "index.html must include a <script> tag loading streaming-markdown"
        )

    def test_smd_assigned_to_window(self):
        assert "window.smd" in INDEX_HTML, (
            "The smd ES module must be assigned to window.smd so messages.js can reach it"
        )

    def test_smd_loaded_as_module(self):
        assert 'type="module"' in INDEX_HTML or "type='module'" in INDEX_HTML, (
            "streaming-markdown must be loaded with type=\"module\" (it is an ES module)"
        )

    def test_smd_vendor_import_is_mount_agnostic(self):
        assert "static/vendor/smd.min.js" in INDEX_HTML, (
            "index.html must load the vendored streaming-markdown module"
        )
        assert "from '/static/vendor/smd.min.js'" not in INDEX_HTML, (
            "streaming-markdown import must not be root-absolute; root-absolute "
            "static paths break subpath deployments such as /hermes/"
        )
        assert 'from "/static/vendor/smd.min.js"' not in INDEX_HTML, (
            "streaming-markdown import must not be root-absolute; root-absolute "
            "static paths break subpath deployments such as /hermes/"
        )


# ── 2. Closure variable declarations ─────────────────────────────────────────

class TestClosureVariables:
    """_smdParser, _smdWrittenLen and _smdReconnect must be declared in the
    attachLiveStream closure, not inside a helper or handler."""

    def get_prelude(self):
        return extract_attach_live_stream_prelude(MESSAGES_JS)

    def test_smd_parser_declared(self):
        prelude = self.get_prelude()
        assert prelude and "_smdParser" in prelude, (
            "_smdParser must be declared in the attachLiveStream closure scope"
        )

    def test_smd_written_len_declared(self):
        prelude = self.get_prelude()
        assert prelude and "_smdWrittenLen" in prelude, (
            "_smdWrittenLen must be declared in the attachLiveStream closure scope"
        )

    def test_smd_reconnect_declared(self):
        prelude = self.get_prelude()
        assert prelude and "_smdReconnect" in prelude, (
            "_smdReconnect must be declared in the attachLiveStream closure scope"
        )

    def test_smd_written_text_declared(self):
        prelude = self.get_prelude()
        assert prelude and "_smdWrittenText" in prelude, (
            "_smdWrittenText must be declared in the attachLiveStream closure scope"
        )

    def test_smd_parser_initialised_null(self):
        prelude = self.get_prelude()
        assert prelude and (
            "_smdParser=null" in prelude or "_smdParser = null" in prelude
        ), "_smdParser must be initialised to null"

    def test_smd_written_len_initialised_zero(self):
        prelude = self.get_prelude()
        assert prelude and (
            "_smdWrittenLen=0" in prelude or "_smdWrittenLen = 0" in prelude
        ), "_smdWrittenLen must be initialised to 0"


# ── 3. Helper functions ───────────────────────────────────────────────────────

class TestSmdHelpers:
    """_smdNewParser, _smdEndParser and _smdWrite must exist and have the right shape."""

    def test_smd_new_parser_exists(self):
        fn = extract_fn(MESSAGES_JS, "_smdNewParser")
        assert fn is not None, "_smdNewParser function must be defined"

    def test_smd_new_parser_resets_written_len(self):
        fn = extract_fn(MESSAGES_JS, "_smdNewParser")
        assert fn and (
            "_smdWrittenLen=0" in fn or "_smdWrittenLen = 0" in fn
        ), "_smdNewParser must reset _smdWrittenLen to 0"

    def test_smd_new_parser_calls_default_renderer(self):
        fn = extract_fn(MESSAGES_JS, "_smdNewParser")
        assert fn and "default_renderer" in fn, (
            "_smdNewParser must call smd.default_renderer() to create a renderer"
        )

    def test_smd_new_parser_calls_parser(self):
        fn = extract_fn(MESSAGES_JS, "_smdNewParser")
        assert fn and (
            "window.smd.parser(" in fn or "smd.parser(" in fn
        ), "_smdNewParser must call smd.parser(renderer) to create a parser"

    def test_smd_new_parser_guards_on_window_smd(self):
        fn = extract_fn(MESSAGES_JS, "_smdNewParser")
        assert fn and "window.smd" in fn, (
            "_smdNewParser must guard on window.smd before using the library"
        )

    def test_smd_end_parser_exists(self):
        fn = extract_fn(MESSAGES_JS, "_smdEndParser")
        assert fn is not None, "_smdEndParser function must be defined"

    def test_smd_end_parser_calls_parser_end(self):
        fn = extract_fn(MESSAGES_JS, "_smdEndParser")
        assert fn and "parser_end" in fn, (
            "_smdEndParser must call smd.parser_end() to flush remaining parser state"
        )

    def test_smd_end_parser_nulls_parser(self):
        fn = extract_fn(MESSAGES_JS, "_smdEndParser")
        assert fn and (
            "_smdParser=null" in fn or "_smdParser = null" in fn
        ), "_smdEndParser must set _smdParser to null after flushing"

    def test_smd_end_parser_resets_written_len(self):
        fn = extract_fn(MESSAGES_JS, "_smdEndParser")
        assert fn and (
            "_smdWrittenLen=0" in fn or "_smdWrittenLen = 0" in fn
        ), "_smdEndParser must reset _smdWrittenLen to 0"

    def test_smd_write_exists(self):
        fn = extract_fn(MESSAGES_JS, "_smdWrite")
        assert fn is not None, "_smdWrite function must be defined"

    def test_smd_write_slices_delta(self):
        fn = extract_fn(MESSAGES_JS, "_smdWrite")
        assert fn and "_smdWrittenLen" in fn, (
            "_smdWrite must slice from _smdWrittenLen to send only new chars"
        )

    def test_smd_write_calls_parser_write(self):
        fn = extract_fn(MESSAGES_JS, "_smdWrite")
        assert fn and "parser_write" in fn, (
            "_smdWrite must call smd.parser_write() to feed the chunk"
        )

    def test_smd_write_updates_written_len(self):
        fn = extract_fn(MESSAGES_JS, "_smdWrite")
        assert fn and "displayText.length" in fn, (
            "_smdWrite must advance _smdWrittenLen to displayText.length after writing"
        )

    def test_smd_write_has_prefix_desync_guard(self):
        fn = extract_fn(MESSAGES_JS, "_smdWrite")
        assert fn and "startsWith(_smdWrittenText)" in fn, (
            "_smdWrite must detect prefix desyncs and rebuild parser to avoid dropped chars"
        )

    def test_smd_write_guards_on_parser(self):
        fn = extract_fn(MESSAGES_JS, "_smdWrite")
        assert fn and "_smdParser" in fn, (
            "_smdWrite must guard on _smdParser before calling parser_write"
        )


# ── 4. _scheduleRender: smd path vs fallback ──────────────────────────────────

class TestScheduleRenderSmdPath:
    """_scheduleRender must use smd when available and fall back to renderMd."""

    def get_fn(self):
        return extract_fn(MESSAGES_JS, "_scheduleRender")

    def test_smd_path_present(self):
        fn = self.get_fn()
        assert fn and "_smdParser" in fn, (
            "_scheduleRender must check for _smdParser to take the smd path"
        )

    def test_smd_write_called_in_schedule_render(self):
        fn = self.get_fn()
        assert fn and "_smdWrite(" in fn, (
            "_scheduleRender must call _smdWrite() to feed incremental text"
        )

    def test_fallback_rendermd_still_present(self):
        fn = self.get_fn()
        assert fn and "renderMd" in fn, (
            "renderMd fallback must still exist in _scheduleRender when smd unavailable"
        )

    def test_fallback_formats_first_segment_with_render_md(self):
        fn = self.get_fn()
        assert fn, "_scheduleRender not found"
        assert "const fallbackText" in fn, (
            "_scheduleRender fallback should choose the visible segment text once"
        )
        assert "renderMd(fallbackText)" in fn, (
            "When smd is unavailable, the first live segment must still be "
            "formatted with renderMd instead of inserting raw parsed.displayText"
        )

    def test_smd_new_parser_called_lazily(self):
        fn = self.get_fn()
        assert fn and "_smdNewParser(" in fn, (
            "_scheduleRender must lazily call _smdNewParser() on first token after body creation"
        )

    def test_reconnect_clears_body(self):
        fn = self.get_fn()
        assert fn and "_smdReconnect" in fn, (
            "_scheduleRender must handle the reconnect case by checking _smdReconnect"
        )

    def test_no_raw_innerhtml_assignment_in_smd_path(self):
        """When smd is active, innerHTML must NOT be set — only _smdWrite() feeds the DOM."""
        fn = self.get_fn()
        assert fn, "_scheduleRender not found"
        # The smd branch must be separated from the innerHTML branch by an if/else.
        # A crude but effective check: _smdWrite and innerHTML=... must not appear
        # on the same code path (i.e., _smdWrite must be inside an `if(_smdParser)` block).
        smd_write_pos = fn.find("_smdWrite(")
        innerhtml_pos = fn.find("assistantBody.innerHTML =")
        # Both must exist
        assert smd_write_pos != -1, "_smdWrite( not found in _scheduleRender"
        assert innerhtml_pos != -1, "innerHTML fallback not found in _scheduleRender"
        # They must be separated by an if/else construct — there must be a `} else {`
        # between them (in either order). We just verify `else` appears between them.
        lo, hi = sorted([smd_write_pos, innerhtml_pos])
        between = fn[lo:hi]
        assert "else" in between, (
            "smd path and innerHTML fallback must be in separate if/else branches"
        )


# ── 5. tool event: smd parser finalised between segments ──────────────────────

class TestToolEventSmdEnd:
    """When a tool call is received, the current smd parser must be ended so
    the next text segment gets a fresh parser bound to the new assistantBody."""

    def get_fn(self):
        return extract_event_handler(MESSAGES_JS, "tool")

    def test_smd_end_parser_called_on_tool(self):
        fn = self.get_fn()
        assert fn and "_smdEndParser(" in fn, (
            "The 'tool' event handler must call _smdEndParser() to finalise the "
            "current segment before creating a new assistantBody for post-tool text"
        )


# ── 6. done event: smd parser finalized + post-finalize highlighting ──────────

class TestDoneEventSmd:
    """The 'done' handler must end the smd parser and trigger Prism/KaTeX/copy."""

    def get_fn(self):
        return extract_event_handler(MESSAGES_JS, "done")

    def test_smd_end_parser_called_on_done(self):
        fn = self.get_fn()
        assert fn and "_smdEndParser(" in fn, (
            "'done' handler must call _smdEndParser() to flush remaining parser state"
        )

    def test_highlight_code_called_on_done(self):
        fn = self.get_fn()
        assert fn and "highlightCode" in fn, (
            "'done' handler must call highlightCode() on the finalized live segment"
        )

    def test_add_copy_buttons_called_on_done(self):
        fn = self.get_fn()
        assert fn and "addCopyButtons" in fn, (
            "'done' handler must call addCopyButtons() on the finalized live segment"
        )

    def test_render_katex_called_on_done(self):
        fn = self.get_fn()
        assert fn and "renderKatexBlocks" in fn, (
            "'done' handler must call renderKatexBlocks() after smd parser end"
        )

    def test_highlight_scheduled_via_raf_before_render_messages(self):
        """highlightCode must be called via requestAnimationFrame that is scheduled
        before renderMessages() runs — so the live segment is highlighted while it's
        still in the DOM, before renderMessages() replaces it with the final content.

        Source-order check: the requestAnimationFrame(...highlightCode...) block must
        appear earlier in the done handler than the renderMessages() call.
        """
        fn = self.get_fn()
        assert fn, "'done' handler not found"
        # Strip single-line comments to avoid matching 'renderMessages(' inside comments
        fn_no_comments = re.sub(r'//[^\n]*', '', fn)
        # Find the rAF that contains highlightCode
        raf_pos = fn_no_comments.find("requestAnimationFrame")
        render_messages_pos = fn_no_comments.find("renderMessages(")
        assert raf_pos != -1, "requestAnimationFrame not found in 'done' handler"
        assert render_messages_pos != -1, "renderMessages() not in 'done' handler"
        # Verify highlightCode is inside the rAF block
        raf_block_end = fn_no_comments.find("});", raf_pos)
        assert raf_block_end != -1, "rAF closing }); not found"
        raf_block = fn_no_comments[raf_pos:raf_block_end]
        assert "highlightCode" in raf_block, (
            "highlightCode must be inside the requestAnimationFrame callback in 'done'"
        )
        # The rAF scheduling call must appear before renderMessages in source
        assert raf_pos < render_messages_pos, (
            "The requestAnimationFrame (which schedules highlightCode) must appear "
            "before renderMessages() in the 'done' handler source"
        )


# ── 7. apperror event: smd parser ends cleanly ───────────────────────────────

class TestAppErrorSmd:
    """The 'apperror' handler must call _smdEndParser to avoid leaking state."""

    def get_fn(self):
        return extract_event_handler(MESSAGES_JS, "apperror")

    def test_smd_end_parser_called_on_apperror(self):
        fn = self.get_fn()
        assert fn and "_smdEndParser(" in fn, (
            "'apperror' handler must call _smdEndParser()"
        )


# ── 8. cancel event: smd parser ends cleanly ─────────────────────────────────

class TestCancelSmd:
    """The 'cancel' handler must call _smdEndParser to avoid leaking state."""

    def get_fn(self):
        return extract_event_handler(MESSAGES_JS, "cancel")

    def test_smd_end_parser_called_on_cancel(self):
        fn = self.get_fn()
        assert fn and "_smdEndParser(" in fn, (
            "'cancel' handler must call _smdEndParser()"
        )


# ── 9. Regression: existing streaming guards still intact ─────────────────────

class TestExistingStreamingGuardsIntact:
    """The smd integration must not break pre-existing correctness properties."""

    def test_stream_finalized_still_guards_schedule_render(self):
        fn = extract_fn(MESSAGES_JS, "_scheduleRender")
        assert fn and "_streamFinalized" in fn, (
            "_streamFinalized guard must still be present in _scheduleRender"
        )

    def test_done_still_sets_stream_finalized(self):
        fn = extract_event_handler(MESSAGES_JS, "done")
        assert fn and (
            "_streamFinalized=true" in fn or "_streamFinalized = true" in fn
        ), "'done' must still set _streamFinalized=true"

    def test_apperror_still_sets_stream_finalized(self):
        fn = extract_event_handler(MESSAGES_JS, "apperror")
        assert fn and (
            "_streamFinalized=true" in fn or "_streamFinalized = true" in fn
        ), "'apperror' must still set _streamFinalized=true"

    def test_cancel_still_sets_stream_finalized(self):
        fn = extract_event_handler(MESSAGES_JS, "cancel")
        assert fn and (
            "_streamFinalized=true" in fn or "_streamFinalized = true" in fn
        ), "'cancel' must still set _streamFinalized=true"

    def test_wire_sse_does_not_reset_accumulators(self):
        fn = extract_fn(MESSAGES_JS, "_wireSSE")
        assert fn is not None, "_wireSSE not found"
        assert "assistantText=''" not in fn and 'assistantText=""' not in fn, (
            "_wireSSE must NOT reset assistantText on reconnect"
        )

    def test_segment_start_still_tracked(self):
        src = MESSAGES_JS
        assert "segmentStart=assistantText.length" in src or \
               "segmentStart = assistantText.length" in src, (
            "segmentStart must still be advanced on tool events"
        )

    def test_fresh_segment_flag_still_set_on_tool(self):
        fn = extract_event_handler(MESSAGES_JS, "tool")
        assert fn and (
            "_freshSegment=true" in fn or "_freshSegment = true" in fn
        ), "_freshSegment must still be set on tool events"


# ── XSS: smd does NOT sanitize URL schemes — we must do it ourselves ──────────

class TestSmdUrlSchemeSanitization:
    """streaming-markdown@0.2.15 preserves `javascript:`, `vbscript:`, and dangerous
    `data:` URLs in href/src attributes. Verified via Node + jsdom harness:

        [click](javascript:alert(1))  →  <a href="javascript:alert(1">click</a>

    The existing renderMd() path filters these via its http(s)-only regex. When
    streaming with smd, we must walk the live DOM after each parser_write and
    remove unsafe schemes, otherwise agent-echoed prompt-injection content
    becomes a click-to-XSS vector in the webui origin.
    """

    def test_sanitize_helper_exists(self):
        assert "_sanitizeSmdLinks" in MESSAGES_JS, (
            "messages.js must define _sanitizeSmdLinks() to strip javascript:/data:/vbscript: "
            "URLs from smd-rendered anchors and images (agent output is untrusted)"
        )

    def test_sanitize_uses_scheme_allowlist(self):
        # The allowlist regex must permit the safe schemes that the legacy
        # renderMd path emitted (http/https + relative/anchor paths + mailto/tel)
        # and reject everything else — including javascript:, data:, vbscript:, file:.
        assert "_SMD_SAFE_URL_RE" in MESSAGES_JS, (
            "Expected a _SMD_SAFE_URL_RE regex defining the safe-scheme allowlist"
        )
        # Find the regex definition
        import re as _re
        m = _re.search(r"_SMD_SAFE_URL_RE\s*=\s*/([^/]+)/i?", MESSAGES_JS)
        assert m, "_SMD_SAFE_URL_RE regex literal not found in messages.js"
        pattern = m.group(1)
        # Must mention https? and must NOT mention javascript/vbscript/data
        assert "https?" in pattern, "allowlist must permit https?:"
        for bad in ("javascript", "vbscript", "data:"):
            assert bad not in pattern, (
                f"allowlist must NOT mention {bad!r} — schemes are denied by default"
            )

    def test_sanitize_called_after_smd_write(self):
        # _smdWrite must invoke _sanitizeSmdLinks on assistantBody after feeding the parser,
        # so anchors/images created mid-stream get their javascript:/data:/vbscript:
        # hrefs/srcs stripped before the user can click them.
        fn = extract_fn(MESSAGES_JS, "_smdWrite")
        assert fn, "_smdWrite function not found"
        assert "_sanitizeSmdLinks" in fn, (
            "_smdWrite must call _sanitizeSmdLinks(assistantBody) after parser_write "
            "so unsafe URL schemes are stripped from newly-added anchors/images "
            "before the user can click them"
        )

    def test_sanitize_called_at_parser_end(self):
        # _smdEndParser flushes any remaining markdown — that flush can create new links,
        # so we must re-sanitize before the DOM is handed off to highlightCode / renderMessages.
        fn = extract_fn(MESSAGES_JS, "_smdEndParser")
        assert fn, "_smdEndParser function not found"
        assert "_sanitizeSmdLinks" in fn, (
            "_smdEndParser must call _sanitizeSmdLinks(assistantBody) after parser_end "
            "so any links flushed at end-of-stream are also scheme-sanitized"
        )

    def test_sanitize_strips_href_and_src(self):
        # The sanitizer must guard BOTH <a href> and <img src> — smd uses the same
        # href/src pipeline for markdown links and images respectively, and images
        # with javascript: src (e.g., ![alt](javascript:...)) are equally risky.
        fn = extract_fn(MESSAGES_JS, "_sanitizeSmdLinks")
        assert fn, "_sanitizeSmdLinks function not found"
        assert "a[href]" in fn, "_sanitizeSmdLinks must query for a[href]"
        assert "img[src]" in fn, "_sanitizeSmdLinks must query for img[src]"
        assert "removeAttribute" in fn, (
            "_sanitizeSmdLinks must removeAttribute('href'/'src') on unsafe schemes"
        )
