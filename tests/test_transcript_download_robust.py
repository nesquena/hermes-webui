"""Regression tests for the Transcript (Markdown) download button (#btnDownload).

History: the original handler revoked the blob URL synchronously after a.click()
and never appended the anchor to the DOM. Some browsers tear down the blob URL
before the download stream actually starts, causing the download to hang or
silently fail. This file pins the corrected pattern.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
BOOT_JS = (ROOT / "static" / "boot.js").read_text()


def _btn_download_block() -> str:
    """Return the JS block that defines #btnDownload's click handler."""
    m = re.search(
        r"\$\(['\"]btnDownload['\"]\)\.onclick\s*=\s*\(\)\s*=>\s*\{(.*?)\};",
        BOOT_JS,
        flags=re.S,
    )
    assert m, "could not locate #btnDownload click handler in boot.js"
    return m.group(1)


def test_handler_creates_markdown_blob():
    body = _btn_download_block()
    assert "transcript()" in body, "handler must call transcript() to build markdown"
    assert "text/markdown" in body, "handler must mark blob as text/markdown"


def test_handler_appends_anchor_to_dom():
    """Firefox/Safari (and some embedded WebViews) require the <a> to be in the DOM tree
    for a programmatic .click() to actually trigger a download."""
    body = _btn_download_block()
    assert "appendChild" in body, (
        "Anchor element must be appended to document before click() — "
        "without it Firefox/Safari silently skip the download"
    )


def test_handler_defers_revokeObjectURL():
    """The download stream is asynchronous; revoking the blob URL synchronously
    after .click() can race the browser and abort the download."""
    body = _btn_download_block()
    assert "revokeObjectURL" in body, "must clean up the blob URL eventually"
    # The revoke MUST be inside a setTimeout (or any async deferral), not on the
    # line immediately after click().
    has_deferred_revoke = bool(
        re.search(r"setTimeout\s*\(.*?revokeObjectURL", body, flags=re.S)
    )
    assert has_deferred_revoke, (
        "URL.revokeObjectURL must be deferred via setTimeout to avoid racing "
        "the browser's async download read"
    )


def test_handler_removes_anchor_after_click():
    body = _btn_download_block()
    assert "removeChild" in body or "remove(" in body, (
        "anchor element should be removed after the download is initiated to "
        "avoid leaking DOM nodes on repeated downloads"
    )


def test_handler_has_error_handling():
    """Silent failures hide the real problem — wrap in try/catch and surface a toast."""
    body = _btn_download_block()
    assert "try" in body and "catch" in body, (
        "handler must use try/catch so transcript()/blob errors surface to the user"
    )
    assert "showToast" in body, "errors should be visible via showToast"
