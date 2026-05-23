"""Tests for #1096 — copy buttons work via Permissions-Policy + fallback."""
import re


def _src(name: str) -> str:
    with open(f"static/{name}") as f:
        return f.read()


def _function_body(src: str, name: str, window: int = 2200) -> str:
    m = re.search(rf"function {re.escape(name)}\b", src)
    assert m, f"{name} must exist"
    return src[m.start():m.start() + window]


def _py_src() -> str:
    with open("api/helpers.py") as f:
        return f.read()


class TestClipboardPermissions:
    """Permissions-Policy must allow clipboard-write for the origin."""

    def test_permissions_policy_includes_clipboard_write(self):
        """Permissions-Policy header must include clipboard-write=(self)."""
        src = _py_src()
        # Match the Permissions-Policy value string (may span lines)
        m = re.search(r"Permissions-Policy',\s*'(.*?)'", src, re.DOTALL)
        assert m, "Permissions-Policy header value must exist"
        assert "clipboard-write=(self)" in m.group(1), \
            "Permissions-Policy must include clipboard-write=(self)"


class TestCopyTextFunction:
    """_copyText must use clipboard API with fallback to execCommand."""

    def test_copyText_uses_clipboard_api(self):
        """_copyText must call navigator.clipboard.writeText."""
        src = _src("ui.js")
        assert "navigator.clipboard.writeText(text)" in src, \
            "_copyText must use Clipboard API"

    def test_copyText_has_fallback(self):
        """_copyText must fall back to execCommand if clipboard API fails."""
        src = _src("ui.js")
        assert "function _fallbackCopy" in src, \
            "Must have a separate _fallbackCopy function"
        # Clipboard API call must .catch() to fallback
        m = re.search(r"navigator\.clipboard\.writeText\(text\)", src)
        assert m, "Must call clipboard API"
        after = src[m.start():m.start() + 300]
        assert "_fallbackCopy" in after, \
            "clipboard.writeText must .catch() → _fallbackCopy"

    def test_fallbackCopy_uses_execCommand(self):
        """_fallbackCopy must use document.execCommand('copy')."""
        src = _src("ui.js")
        assert "document.execCommand('copy')" in src, \
            "_fallbackCopy must use execCommand('copy')"

    def test_fallbackCopy_focuses_textarea(self):
        """_fallbackCopy must explicitly focus textarea before select()."""
        src = _src("ui.js")
        # Find _fallbackCopy function
        m = re.search(r"function _fallbackCopy", src)
        assert m, "_fallbackCopy function must exist"
        fn = src[m.start():m.start() + 600]
        assert "ta.focus()" in fn, \
            "Must call .focus() on textarea before .select()"

    def test_fallbackCopy_not_offscreen(self):
        """_fallbackCopy textarea must NOT be positioned at -9999px (fails in some browsers)."""
        src = _src("ui.js")
        m = re.search(r"function _fallbackCopy", src)
        fn = src[m.start():m.start() + 600]
        assert "-9999" not in fn, \
            "Textarea must not be positioned at -9999px (offscreen select fails)"

    def test_copyMsg_copies_raw_text(self):
        """copyMsg must extract text from data-raw-text attribute."""
        src = _src("ui.js")
        assert "closest('[data-raw-text]')" in src, \
            "copyMsg must find nearest element with data-raw-text"
        assert "dataset.rawText" in src, \
            "copyMsg must read rawText from dataset"


class TestCodeCopyButton:
    """Code block copy button must also use _copyText."""

    def test_code_copy_uses_copyText(self):
        """Code copy button onclick must call _copyText."""
        src = _src("ui.js")
        fn = _function_body(src, "addCopyButtons")
        assert "_copyText" in fn, \
            "Code copy button must use _copyText function"
        assert "codeEl.textContent" in fn, \
            "Code copy must copy the code element's textContent"

    def test_code_copy_button_is_idempotent_for_header_blocks(self):
        """Repeated post-render passes must not append duplicate header buttons.

        addCopyButtons() can be called multiple times after render/cache/streaming
        updates.  For fenced blocks with a language header, the copy button is
        appended to the sibling .pre-header, not inside <pre>, so the duplicate
        guard must check the header as well as the <pre>.
        """
        src = _src("ui.js")
        fn = _function_body(src, "addCopyButtons")
        assert "header.querySelector('.code-copy-btn')" in fn
        assert "pre.querySelector('.code-copy-btn')" in fn
        assert fn.index("header.querySelector('.code-copy-btn')") < fn.index("document.createElement('button')")

    def test_sticky_code_copy_wrapper_is_idempotent(self):
        """Repeated post-render passes must not double-wrap code blocks or duplicate sticky actions."""
        src = _src("ui.js")
        wrap_fn = _function_body(src, "_ensureCodeBlockWrap")
        sticky_fn = _function_body(src, "_ensureStickyCodeCopyButton")
        assert "pre.closest('.code-block-wrap')" in wrap_fn
        assert "if(existing) return existing;" in wrap_fn
        assert "code-tree-wrap" in wrap_fn, "JSON/YAML tree wrappers must be preserved as the code-block wrapper"
        assert "querySelector('.code-copy-sticky-actions')" in sticky_fn
        assert "querySelector('.code-copy-sticky-btn')" in sticky_fn

    def test_sticky_code_copy_button_uses_code_text_content(self):
        """Sticky copy affordance must copy the same code text as the existing copy button."""
        src = _src("ui.js")
        sticky_fn = _function_body(src, "_ensureStickyCodeCopyButton")
        assert "code-copy-sticky-btn" in sticky_fn
        assert "_copyText(codeEl.textContent)" in sticky_fn

    def test_sticky_code_copy_uses_native_css_sticky_without_scroll_listener(self):
        """The floating affordance should be pure CSS sticky, not JS scroll tracking."""
        ui_src = _src("ui.js")
        css = _src("style.css")
        sticky_fn = _function_body(ui_src, "_ensureStickyCodeCopyButton")
        assert "addEventListener('scroll'" not in sticky_fn
        assert 'addEventListener("scroll"' not in sticky_fn
        assert ".onscroll" not in sticky_fn
        assert ".code-copy-sticky-actions" in css
        assert "position:sticky" in css.replace(" ", "")
        assert "pointer-events:none" in css.replace(" ", "")
        assert "pointer-events:auto" in css.replace(" ", "")

class TestCopyFailedI18n:

    def test_copy_failed_in_all_locales(self):
        """copy_failed key must exist in all locale blocks (currently 7 with Korean)."""
        i18n = _src('i18n.js')
        count = i18n.count('copy_failed')
        assert count >= 6, f'Expected copy_failed in at least 6 locale blocks, found {count}'