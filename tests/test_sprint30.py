"""
Sprint 30: Approval card UI, i18n coverage, and approval flow polish.

Tests for:
- Approval card HTML structure (all 4 buttons, IDs, data-i18n attrs)
- Keyboard shortcut handler presence in boot.js
- i18n keys for approval card in both locales
- CSS for approval-btn states (loading, disabled, kbd badge)
- respondApproval loading/disable pattern in messages.js
- streaming.py scoping fix (_unreg_notify=None initialisation)
- Approval respond HTTP endpoint (existing + new behaviour)
"""

import json
import re
import urllib.request
import urllib.error
import urllib.parse

import pytest

BASE = "http://127.0.0.1:8788"


def get(path):
    url = BASE + path
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def post(path, body=None):
    url = BASE + path
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data,
          headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


import pathlib
REPO = pathlib.Path(__file__).parent.parent


# ── HTML structure ───────────────────────────────────────────────────────────

class TestApprovalCardHTML:

    def test_approval_card_has_four_buttons(self):
        html = read(REPO / "static/index.html")
        for choice in ("once", "session", "always", "deny"):
            assert f"respondApproval('{choice}')" in html, \
                f"approval button for '{choice}' missing from index.html"

    def test_approval_buttons_have_ids(self):
        html = read(REPO / "static/index.html")
        for btn_id in ("approvalBtnOnce", "approvalBtnSession",
                       "approvalBtnAlways", "approvalBtnDeny"):
            assert f'id="{btn_id}"' in html, \
                f"button id '{btn_id}' missing from approval card"

    def test_approval_heading_has_data_i18n(self):
        html = read(REPO / "static/index.html")
        assert 'data-i18n="approval_heading"' in html, \
            "approval heading missing data-i18n attribute"

    def test_approval_buttons_have_data_i18n_labels(self):
        html = read(REPO / "static/index.html")
        for key in ("approval_btn_once", "approval_btn_session",
                    "approval_btn_always", "approval_btn_deny"):
            assert f'data-i18n="{key}"' in html, \
                f"button label data-i18n='{key}' missing"

    def test_approval_once_button_has_kbd_badge(self):
        html = read(REPO / "static/index.html")
        assert '<kbd class="approval-kbd">' in html, \
            "kbd badge missing from Allow once button"

    def test_approval_card_has_aria_roles(self):
        html = read(REPO / "static/index.html")
        assert 'role="alertdialog"' in html, \
            "approval card missing role=alertdialog for accessibility"
        assert 'aria-labelledby="approvalHeading"' in html, \
            "approval card missing aria-labelledby"


# ── CSS ──────────────────────────────────────────────────────────────────────

class TestApprovalCardCSS:

    def test_btn_disabled_style_present(self):
        css = read(REPO / "static/style.css")
        assert ".approval-btn:disabled" in css, \
            "disabled state style missing for approval buttons"

    def test_btn_loading_class_present(self):
        css = read(REPO / "static/style.css")
        assert ".approval-btn.loading" in css, \
            "loading class style missing for approval buttons"

    def test_approval_kbd_style_present(self):
        css = read(REPO / "static/style.css")
        assert ".approval-kbd" in css, \
            ".approval-kbd style missing from style.css"

    def test_approval_kbd_hidden_on_mobile(self):
        css = read(REPO / "static/style.css")
        # Should be display:none inside the mobile media query
        assert ".approval-kbd{display:none;}" in css or \
               ".approval-kbd { display: none; }" in css or \
               re.search(r'\.approval-kbd\s*\{[^}]*display\s*:\s*none', css), \
            ".approval-kbd should be hidden on mobile"

    def test_btn_transform_on_hover(self):
        css = read(REPO / "static/style.css")
        assert "translateY(-1px)" in css, \
            "hover lift effect missing from approval buttons"

    def test_four_choice_styles_present(self):
        css = read(REPO / "static/style.css")
        for cls in (".approval-btn.once", ".approval-btn.session",
                    ".approval-btn.always", ".approval-btn.deny"):
            assert cls in css, f"CSS class '{cls}' missing"


# ── i18n keys ────────────────────────────────────────────────────────────────

class TestApprovalI18nKeys:

    REQUIRED_KEYS = [
        "approval_heading",
        "approval_btn_once",
        "approval_btn_session",
        "approval_btn_always",
        "approval_btn_deny",
        "approval_responding",
    ]

    def test_english_locale_has_all_approval_keys(self):
        src = read(REPO / "static/i18n.js")
        # Find en locale block (before the first closing };)
        en_block_end = src.find("\n};")
        en_block = src[:en_block_end]
        for key in self.REQUIRED_KEYS:
            assert f"{key}:" in en_block, \
                f"English locale missing i18n key: {key}"

    def test_chinese_locale_has_all_approval_keys(self):
        src = read(REPO / "static/i18n.js")
        # Find zh locale block (from `  zh: {` to the closing `  },` before `};`)
        zh_start = src.find("\n  zh: {")
        assert zh_start != -1, "zh locale block not found in i18n.js"
        zh_block = src[zh_start:]
        for key in self.REQUIRED_KEYS:
            assert f"{key}:" in zh_block, \
                f"Chinese locale missing i18n key: {key}"

    def test_approval_heading_english_value(self):
        src = read(REPO / "static/i18n.js")
        assert "approval_heading: 'Approval required'" in src, \
            "English approval_heading value incorrect"

    def test_approval_btn_once_english_value(self):
        src = read(REPO / "static/i18n.js")
        assert "approval_btn_once: 'Allow once'" in src, \
            "English approval_btn_once value incorrect"

    def test_approval_btn_deny_english_value(self):
        src = read(REPO / "static/i18n.js")
        assert "approval_btn_deny: 'Deny'" in src, \
            "English approval_btn_deny value incorrect"


# ── messages.js behaviour ────────────────────────────────────────────────────

class TestApprovalMessagesJS:

    def test_show_approval_card_re_enables_buttons(self):
        src = read(REPO / "static/messages.js")
        assert "b.disabled = false" in src and "loading" in src, \
            "showApprovalCard should re-enable buttons on each show"

    def test_respond_disables_buttons_immediately(self):
        src = read(REPO / "static/messages.js")
        assert "b.disabled = true" in src, \
            "respondApproval should disable buttons immediately to prevent double-submit"

    def test_respond_uses_i18n_for_error(self):
        src = read(REPO / "static/messages.js")
        # Should use t('approval_responding') not a hardcoded string
        assert "t(\"approval_responding\")" in src or "t('approval_responding')" in src, \
            "respondApproval error message should use t('approval_responding')"

    def test_show_card_applies_locale_to_dom(self):
        src = read(REPO / "static/messages.js")
        assert "applyLocaleToDOM" in src, \
            "showApprovalCard should call applyLocaleToDOM to translate data-i18n labels"

    def test_show_card_focuses_once_button(self):
        src = read(REPO / "static/messages.js")
        assert "approvalBtnOnce" in src and "focus()" in src, \
            "showApprovalCard should focus the Allow once button"


# ── boot.js keyboard shortcut ────────────────────────────────────────────────

class TestApprovalKeyboardShortcut:

    def test_enter_shortcut_present_in_boot_js(self):
        src = read(REPO / "static/boot.js")
        assert "respondApproval('once')" in src or 'respondApproval("once")' in src, \
            "Enter shortcut calling respondApproval('once') missing from boot.js"

    def test_enter_shortcut_checks_card_visible(self):
        src = read(REPO / "static/boot.js")
        assert "approvalCard" in src and "visible" in src, \
            "Enter shortcut should check if approval card is visible"

    def test_enter_shortcut_guards_input_elements(self):
        src = read(REPO / "static/boot.js")
        assert "TEXTAREA" in src and "INPUT" in src, \
            "Enter shortcut should not fire when focus is on TEXTAREA or INPUT"


# ── streaming.py scoping fix ─────────────────────────────────────────────────

class TestStreamingApprovalScoping:

    def test_unreg_notify_initialised_to_none(self):
        src = read(REPO / "api/streaming.py")
        assert "_unreg_notify = None" in src, \
            "_unreg_notify must be initialised to None before the try block"

    def test_finally_checks_unreg_notify_not_none(self):
        src = read(REPO / "api/streaming.py")
        assert "_unreg_notify is not None" in src, \
            "finally block must check '_unreg_notify is not None' before calling it"

    def test_approval_registered_flag_present(self):
        src = read(REPO / "api/streaming.py")
        assert "_approval_registered = False" in src, \
            "_approval_registered flag must be initialised to False"


# ── HTTP regression: approval respond ────────────────────────────────────────

class TestApprovalRespondHTTP:

    def test_respond_ok_with_all_choices(self):
        for choice in ("once", "session", "always", "deny"):
            import uuid
            sid = f"sprint30-{uuid.uuid4().hex[:8]}"
            result, status = post("/api/approval/respond",
                                  {"session_id": sid, "choice": choice})
            assert status == 200, f"choice={choice} should return 200"
            assert result["ok"] is True
            assert result["choice"] == choice

    def test_respond_rejects_bad_choice(self):
        result, status = post("/api/approval/respond",
                              {"session_id": "x", "choice": "HACKED"})
        assert status == 400

    def test_respond_requires_session_id(self):
        result, status = post("/api/approval/respond", {"choice": "deny"})
        assert status == 400

    def test_respond_returns_choice_field(self):
        import uuid
        sid = f"sprint30-choice-{uuid.uuid4().hex[:8]}"
        result, status = post("/api/approval/respond",
                              {"session_id": sid, "choice": "always"})
        assert status == 200
        assert "choice" in result
        assert result["choice"] == "always"
