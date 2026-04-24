"""
Tests for fix #677: auto-scroll override during streaming.

The scroll system has a _scrollPinned flag and scrollIfPinned() to respect
user scroll position. The bug was that scrollToBottom() was called
unconditionally inside renderMessages() and appendThinking(), even during
an active stream — overriding any scroll position the user had set.
"""
import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


class TestScrollPinningFix:

    def test_render_messages_respects_active_stream(self):
        """renderMessages() must not call scrollToBottom() while streaming (#677).

        During an active stream, scrollToBottom() unconditionally re-pins scroll
        and overrides the user's position. renderMessages() must use scrollIfPinned()
        instead when S.activeStreamId is set.
        """
        # Find renderMessages function
        rm_start = UI_JS.find("function renderMessages()")
        assert rm_start != -1, "renderMessages() not found in ui.js"
        rm_end = UI_JS.find("\nfunction ", rm_start + 1)
        rm_body = UI_JS[rm_start:rm_end]

        # Must check activeStreamId before deciding which scroll fn to call
        assert "activeStreamId" in rm_body, (
            "renderMessages() must check S.activeStreamId before scrolling — "
            "unconditional scrollToBottom() overrides user scroll position (#677)"
        )
        # scrollIfPinned must be called inside renderMessages (stream path)
        assert "scrollIfPinned()" in rm_body, (
            "renderMessages() must call scrollIfPinned() during streaming (#677)"
        )

    def test_append_thinking_uses_scroll_if_pinned(self):
        """appendThinking() must use scrollIfPinned() not scrollToBottom() (#677).

        appendThinking() fires continuously during streaming — calling scrollToBottom()
        inside it re-pins on every token, preventing the user from scrolling up.
        """
        at_start = UI_JS.find("function appendThinking(")
        assert at_start != -1, "appendThinking() not found in ui.js"
        at_end = UI_JS.find("\nfunction ", at_start + 1)
        at_body = UI_JS[at_start:at_end]

        assert "scrollIfPinned()" in at_body, (
            "appendThinking() must call scrollIfPinned() not scrollToBottom() (#677)"
        )
        assert "scrollToBottom()" not in at_body, (
            "appendThinking() must not call scrollToBottom() — it fires mid-stream (#677)"
        )

    def test_scroll_threshold_increased(self):
        """Scroll re-pin threshold must be at least 150px (#677).

        80px was too small — a fast mouse scroll wheel can jump 100–120px in one
        tick, causing unintended re-pin. 150px gives a proper dead zone.
        """
        # Find the nearBottom assignment in the scroll listener
        near_bottom_pos = UI_JS.find("nearBottom=")
        if near_bottom_pos == -1:
            near_bottom_pos = UI_JS.find("nearBottom =")
        assert near_bottom_pos != -1, "nearBottom scroll threshold assignment not found"
        threshold_line = UI_JS[near_bottom_pos:near_bottom_pos + 120]
        # Extract the numeric threshold
        match = re.search(r"<\s*(\d+)", threshold_line)
        assert match, f"Numeric threshold not found near nearBottom assignment: {threshold_line!r}"
        threshold = int(match.group(1))
        assert threshold >= 150, (
            f"Scroll re-pin threshold is {threshold}px — must be >= 150px to avoid "
            f"hair-trigger re-pinning on fast scroll wheels (#677)"
        )

    def test_scroll_to_bottom_button_exists_in_html(self):
        """index.html must contain a scroll-to-bottom button (#677).

        All major streaming chat UIs (Claude, ChatGPT) show a floating ↓ button
        when the user has scrolled up, giving a clear escape hatch to return to live output.
        """
        assert "scrollToBottomBtn" in INDEX_HTML, (
            "index.html must contain a #scrollToBottomBtn element (#677)"
        )
        assert "scroll-to-bottom-btn" in INDEX_HTML, (
            "index.html must use class scroll-to-bottom-btn for the scroll button (#677)"
        )

    def test_scroll_to_bottom_button_hidden_by_default(self):
        """Scroll-to-bottom button must be hidden by default (display:none) (#677)."""
        btn_pos = INDEX_HTML.find("scrollToBottomBtn")
        assert btn_pos != -1
        btn_context = INDEX_HTML[btn_pos:btn_pos + 200]
        assert "display:none" in btn_context or 'display="none"' in btn_context, (
            "scrollToBottomBtn must be hidden by default — only shown when user scrolls up (#677)"
        )

    def test_scroll_to_bottom_button_css_exists(self):
        """style.css must have styling for .scroll-to-bottom-btn (#677)."""
        assert ".scroll-to-bottom-btn" in STYLE_CSS, (
            "style.css must define .scroll-to-bottom-btn styles (#677)"
        )

    def test_scroll_to_bottom_button_is_sticky(self):
        """Scroll-to-bottom button must use position:sticky so it stays visible (#677)."""
        btn_css_pos = STYLE_CSS.find(".scroll-to-bottom-btn")
        assert btn_css_pos != -1
        btn_css = STYLE_CSS[btn_css_pos:btn_css_pos + 300]
        assert "sticky" in btn_css, (
            ".scroll-to-bottom-btn must use position:sticky to stay at bottom of viewport (#677)"
        )

    def test_scroll_listener_hides_button_when_pinned(self):
        """Scroll listener must hide the button when user is near the bottom (#677)."""
        scroll_listener_start = UI_JS.find("el.addEventListener('scroll'")
        assert scroll_listener_start != -1, "scroll event listener not found"
        listener_block = UI_JS[scroll_listener_start:scroll_listener_start + 300]
        assert "scrollToBottomBtn" in listener_block, (
            "Scroll listener must show/hide scrollToBottomBtn based on _scrollPinned (#677)"
        )

    def test_scroll_to_bottom_button_calls_scroll_to_bottom(self):
        """scrollToBottomBtn onclick must call scrollToBottom() (#677)."""
        btn_pos = INDEX_HTML.find("scrollToBottomBtn")
        assert btn_pos != -1
        btn_context = INDEX_HTML[btn_pos:btn_pos + 200]
        assert "scrollToBottom()" in btn_context, (
            "scrollToBottomBtn onclick must call scrollToBottom() (#677)"
        )
