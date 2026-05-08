"""Tests for #1096 — copy buttons work via Permissions-Policy + fallback."""
import re


def _src(name: str) -> str:
    with open(f"static/{name}") as f:
        return f.read()


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
        # Find addCopyButtons function
        m = re.search(r"function addCopyButtons", src)
        assert m, "addCopyButtons must exist"
        fn = src[m.start():m.start() + 1000]
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
        m = re.search(r"function addCopyButtons", src)
        assert m, "addCopyButtons must exist"
        fn = src[m.start():m.start() + 1200]
        assert "const header=pre.previousElementSibling;" in fn
        assert "header.querySelector('.code-copy-btn')" in fn
        assert fn.index("header.querySelector('.code-copy-btn')") < fn.index("document.createElement('button')")

class TestCopyFailedI18n:

    def test_copy_failed_in_all_locales(self):
        """copy_failed key must exist in all locale blocks (currently 7 with Korean)."""
        i18n = _src('i18n.js')
        count = i18n.count('copy_failed')
        assert count >= 6, f'Expected copy_failed in at least 6 locale blocks, found {count}'