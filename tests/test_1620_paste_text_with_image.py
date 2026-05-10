"""Tests for #1620 — Cmd+V always attaches an image when clipboard contains both text and image.

The composer paste handler in `static/boot.js` previously intercepted any paste
event whose clipboard carried an `image/*` item, called `e.preventDefault()`,
and attached the image as a screenshot. When the clipboard came from a rich-text
source (Notes, Word, Slack, browser selection), macOS/Windows/Linux attach a
rendered preview image alongside the plain text — so the handler swallowed the
text payload and only the rogue image was attached.

The fix:
  • Skip image-attach when the clipboard also carries `text/plain` or `text/html`
    string items (rich-text source — let the browser paste text normally).
  • Tighten the image filter to `kind === 'file'` so string items advertising an
    image MIME are not misclassified as a true screenshot paste.

These tests guard the handler shape against regression by static-analyzing
`static/boot.js`. They follow the same pattern as `test_issue1095_pasted_images.py`.
"""

import os
import re


def _read_boot_js() -> str:
    with open(os.path.join("static", "boot.js")) as f:
        return f.read()


def _paste_handler_body() -> str:
    """Extract the body of the #msg paste handler for assertions."""
    src = _read_boot_js()
    m = re.search(r"\$\('msg'\)\.addEventListener\('paste',\s*e\s*=>\s*\{", src)
    assert m, "#msg paste handler not found in static/boot.js"
    # Walk braces from the opening { to find the matching close.
    start = m.end() - 1
    depth = 0
    for i in range(start, len(src)):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError("Unbalanced braces in #msg paste handler")


class TestPasteHandlerTextWithImage:
    """Regression suite for #1620."""

    def test_handler_detects_text_in_clipboard(self):
        """Handler must inspect string items for text/plain or text/html so it can
        defer to the browser's default text-paste behavior when text is present."""
        body = _paste_handler_body()
        assert (
            "kind==='string'" in body
            or 'kind === "string"' in body
            or "kind === 'string'" in body
        ), "paste handler must check items[].kind === 'string' to detect text payload"
        assert "'text/plain'" in body, "paste handler must check for text/plain"
        assert "'text/html'" in body, "paste handler must check for text/html"

    def test_image_filter_requires_kind_file(self):
        """Image filter must require kind === 'file' to avoid misclassifying string
        items that advertise an image MIME (e.g. text/html with embedded data URIs)."""
        body = _paste_handler_body()
        # The image filter line must combine kind==='file' with type.startsWith('image/').
        assert re.search(
            r"kind\s*===\s*'file'\s*&&\s*[a-zA-Z_$][\w$]*\.type\.startsWith\('image/'\)",
            body,
        ), "imageItems filter must use kind === 'file' && type.startsWith('image/')"

    def test_handler_skips_attach_when_text_present(self):
        """The early-return guard must short-circuit when text is in the clipboard,
        so the browser's default text-paste runs and no image is attached."""
        body = _paste_handler_body()
        # Guard shape: if(!imageItems.length || hasText) return;
        assert re.search(
            r"if\s*\(\s*!\s*imageItems\.length\s*\|\|\s*hasText\s*\)\s*return\s*;",
            body,
        ), "guard must early-return when there are no image files OR text is present"

    def test_handler_still_intercepts_pure_screenshot_paste(self):
        """Pure-screenshot paste (image-only clipboard) must still call preventDefault()
        and route through addFiles() so the screenshot attaches as a file."""
        body = _paste_handler_body()
        assert "e.preventDefault()" in body, (
            "handler must still preventDefault on image-only paste"
        )
        assert "addFiles(files)" in body, (
            "handler must still call addFiles(files) for screenshots"
        )
        assert "screenshot-" in body, (
            "handler must still synthesize screenshot-<ts> filename"
        )

    def test_handler_does_not_use_loose_image_check(self):
        """The pre-fix loose check `i.type.startsWith('image/')` (without kind==='file')
        must not be the imageItems filter — that was the source of the bug."""
        body = _paste_handler_body()
        # Find the imageItems assignment line.
        m = re.search(r"const\s+imageItems\s*=\s*items\.filter\([^)]*\)", body)
        assert m, "imageItems filter not found"
        filter_expr = m.group(0)
        assert "kind==='file'" in filter_expr or "kind === 'file'" in filter_expr, (
            "imageItems filter must be tightened with kind === 'file' (regression for #1620)"
        )

    def test_handler_does_not_lose_status_message(self):
        """The image_pasted status message must still be emitted on the screenshot path."""
        body = _paste_handler_body()
        assert "setStatus(t('image_pasted')" in body, (
            "handler must still emit the image_pasted status on screenshot attach"
        )
