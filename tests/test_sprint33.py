"""
Sprint 33 Tests: Shared app dialogs replace native confirm/prompt usage.

These tests verify the static assets expose the reusable confirm/input modal
and that browser-native confirm/prompt calls are no longer used in the Web UI.
"""

import pathlib
import re


REPO = pathlib.Path(__file__).parent.parent


def read(path):
    return (REPO / path).read_text(encoding="utf-8")


def test_index_has_shared_app_dialog_markup():
    html = read("static/index.html")
    assert 'id="appDialogOverlay"' in html
    assert 'id="appDialog"' in html
    assert 'id="appDialogTitle"' in html
    assert 'id="appDialogDesc"' in html
    assert 'id="appDialogInput"' in html
    assert 'id="appDialogCancel"' in html
    assert 'id="appDialogConfirm"' in html


def test_app_dialog_css_rules_exist():
    css = read("static/style.css")
    for selector in (
        ".app-dialog-overlay",
        ".app-dialog",
        ".app-dialog-input",
        ".app-dialog-actions",
        ".app-dialog-btn.confirm",
        ".app-dialog-btn.confirm.danger",
    ):
        assert selector in css, f"missing CSS selector: {selector}"


def test_ui_js_exposes_shared_dialog_helpers():
    src = read("static/ui.js")
    assert "function showConfirmDialog(opts={})" in src
    assert "function showPromptDialog(opts={})" in src
    assert "document.addEventListener('keydown'" in src


def test_no_native_confirm_calls_remain_in_static_js():
    for path in (REPO / "static").glob("*.js"):
        src = path.read_text(encoding="utf-8")
        assert not re.search(r"\bconfirm\s*\(", src), (
            f"native confirm() remains in {path.name}"
        )


def test_no_native_prompt_calls_remain_in_static_js():
    for path in (REPO / "static").glob("*.js"):
        src = path.read_text(encoding="utf-8")
        assert not re.search(r"\bprompt\s*\(", src), (
            f"native prompt() remains in {path.name}"
        )
