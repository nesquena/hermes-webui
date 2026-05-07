"""Regression coverage for issue #1800 file-picker and HTML-open interactions."""

from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
ROUTES_PY = (REPO / "api" / "routes.py").read_text(encoding="utf-8")


def _slice_after(source: str, needle: str, chars: int = 900) -> str:
    idx = source.find(needle)
    assert idx >= 0, f"{needle!r} not found"
    return source[idx : idx + chars]


def test_attach_button_is_non_submit_button():
    """Attach must not act like a submit button in browser/container shells."""
    m = re.search(r"<button[^>]*id=\"btnAttach\"[^>]*>", INDEX_HTML)
    assert m, "btnAttach button not found"
    assert 'type="button"' in m.group(0)


def test_file_input_is_visually_hidden_not_display_none():
    """Hidden file inputs are more consistently opened by user-gesture clicks."""
    m = re.search(r"<input[^>]*id=\"fileInput\"[^>]*>", INDEX_HTML)
    assert m, "fileInput not found"
    tag = m.group(0)
    assert "file-input-visually-hidden" in tag
    assert "display:none" not in tag
    rule = _slice_after(STYLE_CSS, ".file-input-visually-hidden", 240)
    assert "position:absolute" in rule
    assert "opacity:0" in rule


def test_attach_click_prevents_default_and_opens_picker():
    body = _slice_after(BOOT_JS, "$('btnAttach').onclick", 300)
    assert "preventDefault" in body
    assert "$('fileInput').value=''" in body
    assert "$('fileInput').click()" in body


def test_html_chat_attachment_opens_sandboxed_inline_raw_file():
    """Uploaded .html attachments render as an openable link, not an inert badge."""
    body = _slice_after(UI_JS, "function _renderAttachmentHtml", 900)
    assert "_HTML_EXTS.test(fname)" in body
    assert "inline=1" in body
    assert "target=\"_blank\"" in body
    assert "rel=\"noopener\"" in body
    assert "msg-file-badge--html" in body


def test_html_media_open_full_uses_inline_new_tab_not_download():
    """MEDIA: HTML preview's Open full page link should open a browser view."""
    body = _slice_after(UI_JS, "function loadHtmlInline", 1800)
    assert "'&inline=1'" in body
    assert "target=\"_blank\"" in body
    assert "rel=\"noopener\"" in body
    normal_open = next(line for line in body.splitlines() if "html-open-link" in line)
    assert "download=" not in normal_open


def test_media_html_inline_keeps_csp_sandbox():
    """api/media may serve HTML inline only behind a CSP sandbox."""
    body = _slice_after(ROUTES_PY, "def _handle_media", 4000)
    assert 'html_inline_ok = inline_preview and mime == "text/html"' in body
    assert 'csp = "sandbox allow-scripts" if html_inline_ok else None' in body
    assert "csp=csp" in body
    assert "allow-same-origin" not in body


def test_sandboxed_file_responses_do_not_send_x_frame_options():
    """X-Frame-Options: DENY would block the sandbox iframe preview."""
    body = _slice_after(ROUTES_PY, "def _serve_file_bytes", 1800)
    csp_branch = body[body.find("if csp:") : body.find("else:", body.find("if csp:"))]
    assert "Content-Security-Policy" in csp_branch
    assert 'send_header("X-Frame-Options"' not in csp_branch
