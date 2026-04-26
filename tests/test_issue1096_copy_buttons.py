"""Tests for #1096 — copy buttons work without HTTPS (execCommand fallback)."""
import os
import re
import pytest


# ── Helpers ─────────────────────────────────────────────────────────────────────

def _read_js(name):
    path = os.path.join('static', name)
    with open(path) as f:
        return f.read()


def _read_i18n():
    return _read_js('i18n.js')


def _read_ui():
    return _read_js('ui.js')


# ── _copyText helper ────────────────────────────────────────────────────────────

class TestCopyTextHelperExists:
    """_copyText() helper must exist with secure context check + execCommand fallback."""

    def test_function_exists(self):
        ui = _read_ui()
        assert 'function _copyText(' in ui, '_copyText helper missing from ui.js'

    def test_uses_secure_context_check(self):
        ui = _read_ui()
        assert 'isSecureContext' in ui, 'isSecureContext check missing — fallback wont trigger on HTTP'

    def test_uses_exec_command_fallback(self):
        ui = _read_ui()
        assert 'execCommand' in ui, 'execCommand fallback missing — copy fails on HTTP'


# ── copyMsg() uses helper ──────────────────────────────────────────────────────

class TestCopyMsgUsesHelper:

    def test_copy_msg_exists(self):
        ui = _read_ui()
        assert 'function copyMsg(' in ui

    def test_copy_msg_calls_helper_not_clipboard_directly(self):
        """copyMsg must use _copyText, not navigator.clipboard.writeText."""
        ui = _read_ui()
        # Find copyMsg function body
        m = re.search(r'function copyMsg\(', ui)
        assert m, 'copyMsg function not found'
        body = ui[m.start():m.start() + 600]
        assert '_copyText(' in body, 'copyMsg must call _copyText helper'
        assert 'navigator.clipboard.writeText' not in body, 'copyMsg must NOT call navigator.clipboard directly'

    def test_copy_msg_has_i18n_error(self):
        """Error message must use t() for i18n, not hardcoded string."""
        ui = _read_ui()
        m = re.search(r'function copyMsg\(', ui)
        body = ui[m.start():m.start() + 600]
        assert "t('copy_failed')" in body, 'copyMsg error must use t("copy_failed") not hardcoded string'


# ── Code block copy uses helper ────────────────────────────────────────────────

class TestCodeBlockCopyUsesHelper:

    def test_add_copy_buttons_exists(self):
        ui = _read_ui()
        assert 'function addCopyButtons(' in ui

    def test_code_copy_calls_helper(self):
        """addCopyButtons must use _copyText, not navigator.clipboard directly."""
        ui = _read_ui()
        m = re.search(r'function addCopyButtons\(', ui)
        assert m, 'addCopyButtons function not found'
        body = ui[m.start():m.start() + 2000]
        assert '_copyText(' in body, 'addCopyButtons must call _copyText helper'
        assert 'navigator.clipboard.writeText' not in body, 'addCopyButtons must NOT call navigator.clipboard directly'

    def test_code_copy_has_catch_handler(self):
        """Code block copy must have .catch() — previously had none (silent failure)."""
        ui = _read_ui()
        m = re.search(r'function addCopyButtons\(', ui)
        body = ui[m.start():m.start() + 2000]
        assert '.catch(' in body, 'Code block copy button has no .catch() handler'


# ── i18n ────────────────────────────────────────────────────────────────────────

class TestCopyFailedI18n:

    def test_copy_failed_in_all_locales(self):
        """copy_failed key must exist in all locale blocks (currently 7 with Korean)."""
        i18n = _read_i18n()
        count = i18n.count('copy_failed')
        assert count >= 6, f'Expected copy_failed in at least 6 locale blocks, found {count}'
