"""Tests for inline HTML preview in workspace panel (issue #779)."""
import pytest


def _get_routes_content():
    return open("api/routes.py", encoding="utf-8").read()


def _get_workspace_js():
    return open("static/workspace.js", encoding="utf-8").read()


def _get_index_html():
    return open("static/index.html", encoding="utf-8").read()


def test_inline_preview_param_in_file_raw():
    """?inline=1 must bypass Content-Disposition: attachment for text/html."""
    content = _get_routes_content()
    assert "inline_preview" in content, (
        "_handle_file_raw must read the inline query parameter"
    )
    assert "html_inline_ok" in content, (
        "_handle_file_raw must allow HTML inline when inline_preview=True"
    )


def test_iframe_uses_inline_param():
    """workspace.js must pass &inline=1 when setting the preview iframe src."""
    content = _get_workspace_js()
    assert "inline=1" in content, (
        "workspace.js must pass ?inline=1 to api/file/raw for the HTML preview iframe"
    )


def test_html_preview_iframe_exists_in_html():
    """The previewHtmlIframe element must be present in index.html."""
    content = _get_index_html()
    assert "previewHtmlIframe" in content, (
        "index.html must contain the previewHtmlIframe element"
    )


def test_html_exts_defined_in_workspace_js():
    """HTML_EXTS set must include .html and .htm."""
    content = _get_workspace_js()
    assert "HTML_EXTS" in content, "workspace.js must define HTML_EXTS"
    assert "'.html'" in content or '".html"' in content, "HTML_EXTS must include .html"
    assert "'.htm'" in content or '".htm"' in content, "HTML_EXTS must include .htm"


def test_sandbox_allows_scripts_only():
    """iframe sandbox must not include allow-same-origin (XSS risk)."""
    content = _get_index_html()
    # Find the sandbox attribute value
    import re
    sandboxes = re.findall(r'sandbox="([^"]*)"', content)
    preview_sandboxes = [s for s in sandboxes if "allow" in s]
    for sb in preview_sandboxes:
        assert "allow-same-origin" not in sb, (
            "HTML preview iframe must not have allow-same-origin (would expose parent cookies)"
        )
