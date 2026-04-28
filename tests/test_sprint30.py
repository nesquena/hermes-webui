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
import pathlib
import re
import urllib.request
import urllib.error
import urllib.parse

from tests._pytest_port import BASE


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


class TestClarifyCardHTML:

    def test_clarify_card_markup_present(self):
        html = read(REPO / "static/index.html")
        assert 'id="clarifyCard"' in html, "clarify card missing from index.html"
        assert 'id="clarifyHeading"' in html, "clarify heading missing"
        assert 'id="clarifyQuestion"' in html, "clarify question text missing"
        assert 'id="clarifyChoices"' in html, "clarify choices container missing"
        assert 'id="clarifyInput"' in html, "clarify input missing"
        assert 'id="clarifySubmit"' in html, "clarify submit button missing"

    def test_clarify_card_has_data_i18n(self):
        html = read(REPO / "static/index.html")
        assert 'data-i18n="clarify_heading"' in html
        assert 'data-i18n="clarify_send"' in html
        assert 'data-i18n-placeholder="clarify_input_placeholder"' in html

    def test_clarify_card_has_aria_roles(self):
        html = read(REPO / "static/index.html")
        assert 'role="dialog"' in html, \
            "clarify card missing role=dialog for accessibility"
        assert 'aria-labelledby="clarifyHeading"' in html, \
            "clarify card missing aria-labelledby"


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


class TestClarifyCardCSS:

    def test_clarify_styles_present(self):
        css = read(REPO / "static/style.css")
        for cls in (
            ".clarify-card",
            ".clarify-card.visible",
            ".clarify-inner",
            ".clarify-header",
            ".clarify-question",
            ".clarify-choices",
            ".clarify-choice",
            ".clarify-response",
            ".clarify-input",
            ".clarify-submit",
            ".clarify-hint",
        ):
            assert cls in css, f"CSS class '{cls}' missing"

    def test_clarify_mobile_styles_present(self):
        css = read(REPO / "static/style.css")
        assert ".clarify-card{padding:0 10px 8px;}" in css or \
               ".clarify-card { padding:0 10px 8px; }" in css or \
               "clarify-card" in css, "clarify mobile styles missing"

    def test_clarify_focus_styles_present(self):
        css = read(REPO / "static/style.css")
        assert ".clarify-choice:focus" in css and ".clarify-submit:focus" in css, \
            "clarify focus styles missing"


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


class TestClarifyI18nKeys:

    REQUIRED_KEYS = [
        "clarify_heading",
        "clarify_hint",
        "clarify_other",
        "clarify_send",
        "clarify_input_placeholder",
        "clarify_responding",
    ]

    def test_english_locale_has_all_clarify_keys(self):
        src = read(REPO / "static/i18n.js")
        en_block_end = src.find("\n};")
        en_block = src[:en_block_end]
        for key in self.REQUIRED_KEYS:
            assert f"{key}:" in en_block, f"English locale missing i18n key: {key}"

    def test_chinese_locale_has_all_clarify_keys(self):
        src = read(REPO / "static/i18n.js")
        zh_start = src.find("\n  zh: {")
        assert zh_start != -1, "zh locale block not found in i18n.js"
        zh_block = src[zh_start:]
        for key in self.REQUIRED_KEYS:
            assert f"{key}:" in zh_block, f"Chinese locale missing i18n key: {key}"

    def test_clarify_heading_english_value(self):
        src = read(REPO / "static/i18n.js")
        assert "clarify_heading: 'Clarification needed'" in src, \
            "English clarify_heading value incorrect"


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


class TestClarifyMessagesJS:

    def test_clarify_event_listener_present(self):
        src = read(REPO / "static/messages.js")
        assert "addEventListener('clarify'" in src, \
            "clarify SSE listener missing from messages.js"

    def test_show_clarify_card_present(self):
        src = read(REPO / "static/messages.js")
        assert "function showClarifyCard" in src, "showClarifyCard missing"
        assert "clarifyChoices" in src and "clarifyInput" in src, \
            "showClarifyCard should manage clarify DOM elements"

    def test_respond_clarify_uses_api_endpoint(self):
        src = read(REPO / "static/messages.js")
        assert '/api/clarify/respond' in src, \
            "respondClarify should POST to /api/clarify/respond"

    def test_clarify_polling_helpers_present(self):
        src = read(REPO / "static/messages.js")
        for token in ("startClarifyPolling", "stopClarifyPolling", "hideClarifyCard", "_clarifySessionId"):
            assert token in src, f"{token} missing from messages.js"


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

    def test_clarify_registered_flag_present(self):
        src = read(REPO / "api/streaming.py")
        assert "_clarify_registered = False" in src, \
            "_clarify_registered flag must be initialised to False"

    def test_clarify_unreg_notify_initialised_to_none(self):
        src = read(REPO / "api/streaming.py")
        assert "_unreg_clarify_notify = None" in src, \
            "_unreg_clarify_notify must be initialised to None before the try block"

    def test_finally_checks_clarify_unreg_notify_not_none(self):
        src = read(REPO / "api/streaming.py")
        assert "_unreg_clarify_notify is not None" in src, \
            "finally block must check '_unreg_clarify_notify is not None' before calling it"


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


class TestApprovalCardTimerLogic:
    """Tests for the 30s minimum visibility guard introduced in PR #225."""

    def _get_js(self):
        return pathlib.Path(__file__).parent.parent / 'static' / 'messages.js'

    def test_approval_min_visible_ms_constant_present(self):
        """APPROVAL_MIN_VISIBLE_MS constant exists and is 30000."""
        src = self._get_js().read_text()
        assert 'APPROVAL_MIN_VISIBLE_MS' in src
        import re
        m = re.search(r'APPROVAL_MIN_VISIBLE_MS\s*=\s*(\d+)', src)
        assert m is not None, 'APPROVAL_MIN_VISIBLE_MS not assigned'
        assert int(m.group(1)) == 30000, f'Expected 30000, got {m.group(1)}'

    def test_hide_approval_card_has_force_parameter(self):
        """hideApprovalCard() accepts a force parameter."""
        src = self._get_js().read_text()
        assert 'hideApprovalCard(force=false)' in src or \
               'hideApprovalCard(force = false)' in src, \
            'hideApprovalCard must have force=false default parameter'

    def test_hide_approval_card_checks_force_flag(self):
        """hideApprovalCard body has a conditional on force."""
        src = self._get_js().read_text()
        # The guard: if (!force && _approvalVisibleSince)
        assert '!force' in src, 'hideApprovalCard must check !force before deferred hide'

    def test_approval_hide_timer_variable_present(self):
        """Module-level _approvalHideTimer variable is declared."""
        src = self._get_js().read_text()
        assert '_approvalHideTimer' in src

    def test_approval_visible_since_variable_present(self):
        """Module-level _approvalVisibleSince variable is declared."""
        src = self._get_js().read_text()
        assert '_approvalVisibleSince' in src

    def test_approval_signature_variable_present(self):
        """Module-level _approvalSignature variable is declared."""
        src = self._get_js().read_text()
        assert '_approvalSignature' in src

    def test_respond_approval_calls_hide_with_force(self):
        """respondApproval must call hideApprovalCard(true) — not no-arg."""
        src = self._get_js().read_text()
        # Extract respondApproval function body
        import re
        m = re.search(r'async function respondApproval.*?(?=\nasync function|\nfunction |\Z)',
                      src, re.DOTALL)
        assert m, 'respondApproval function not found'
        body = m.group(0)
        # Must call hideApprovalCard(true), not the bare hideApprovalCard()
        assert 'hideApprovalCard(true)' in body, \
            'respondApproval must call hideApprovalCard(true) so card hides immediately after user clicks'
        # Must NOT have bare hideApprovalCard() without force
        bare_calls = re.findall(r'hideApprovalCard\((?!true)', body)
        assert not bare_calls, \
            f'respondApproval has bare hideApprovalCard() calls (no force=true): {bare_calls}'

    def test_stream_done_calls_hide_with_force(self):
        """Done SSE event handler must call hideApprovalCard(true)."""
        src = self._get_js().read_text()
        # Find the done event handler section (stopApprovalPolling followed by hideApprovalCard)
        import re
        # Look for pattern: stopApprovalPolling();\n + hideApprovalCard
        matches = re.findall(
            r'stopApprovalPolling\(\);\s*\n\s*if\(!_approvalSessionId[^)]*\)\s*hideApprovalCard\((\w*)\)',
            src
        )
        # All stopApprovalPolling paths that call hideApprovalCard should use force=true
        for match in matches:
            assert match == 'true', \
                f'After stopApprovalPolling(), hideApprovalCard called without force=true (got: {match!r})'

    def test_poll_loop_still_uses_no_force(self):
        """Poll loop hideApprovalCard() (when pending gone) keeps no-force — correct behavior."""
        src = self._get_js().read_text()
        # Line 446: else { hideApprovalCard(); } — this is the poll-loop path
        # The 30s guard should protect this call (don't force from poll ticks)
        assert 'else { hideApprovalCard(); }' in src or \
               'else {hideApprovalCard();}' in src or \
               'else { hideApprovalCard() }' in src, \
            'Poll loop should still call hideApprovalCard() without force=true'

    def test_show_approval_card_signature_dedup(self):
        """showApprovalCard uses a signature to avoid resetting timer on repeat polls."""
        src = self._get_js().read_text()
        # The sig computation must use JSON.stringify on card content
        import re
        m = re.search(r'function showApprovalCard.*?(?=\nfunction |\nasync function |\Z)',
                      src, re.DOTALL)
        assert m, 'showApprovalCard function not found'
        body = m.group(0)
        assert 'JSON.stringify' in body, 'showApprovalCard must compute a signature via JSON.stringify'
        assert '_approvalSignature' in body, 'showApprovalCard must check/set _approvalSignature'

    def test_clear_approval_hide_timer_helper_present(self):
        """_clearApprovalHideTimer helper exists to cancel deferred hides."""
        src = self._get_js().read_text()
        assert '_clearApprovalHideTimer' in src, \
            '_clearApprovalHideTimer helper must exist to cancel deferred setTimeout'


class TestClarifyCardTimerLogic:

    def _get_js(self):
        return pathlib.Path(__file__).parent.parent / 'static' / 'messages.js'

    def _get_html(self):
        return pathlib.Path(__file__).parent.parent / 'static' / 'index.html'

    def _get_css(self):
        return pathlib.Path(__file__).parent.parent / 'static' / 'style.css'

    def test_clarify_min_visible_ms_constant_present(self):
        src = self._get_js().read_text()
        assert 'CLARIFY_MIN_VISIBLE_MS' in src
        import re
        m = re.search(r'CLARIFY_MIN_VISIBLE_MS\s*=\s*(\d+)', src)
        assert m is not None, 'CLARIFY_MIN_VISIBLE_MS not assigned'
        assert int(m.group(1)) == 30000, f'Expected 30000, got {m.group(1)}'

    def test_hide_clarify_card_has_force_parameter(self):
        src = self._get_js().read_text()
        assert 'hideClarifyCard(force=false)' in src or \
               'hideClarifyCard(force=false, reason=' in src or \
               'hideClarifyCard(force = false)' in src, \
            'hideClarifyCard must have force=false default parameter'

    def test_hide_clarify_card_checks_force_flag(self):
        src = self._get_js().read_text()
        assert '!force' in src, 'hideClarifyCard must check !force before deferred hide'

    def test_clarify_hide_timer_variable_present(self):
        src = self._get_js().read_text()
        assert '_clarifyHideTimer' in src

    def test_clarify_visible_since_variable_present(self):
        src = self._get_js().read_text()
        assert '_clarifyVisibleSince' in src

    def test_clarify_signature_variable_present(self):
        src = self._get_js().read_text()
        assert '_clarifySignature' in src

    def test_clarify_countdown_element_present(self):
        html = self._get_html().read_text()
        assert 'id="clarifyCountdown"' in html, \
            'clarify card must include a countdown element so users see timeout risk'

    def test_clarify_countdown_uses_pending_expiry(self):
        src = self._get_js().read_text()
        assert '_clarifyCountdownTimer' in src
        assert 'function _startClarifyCountdown' in src
        assert 'expires_at' in src, \
            'clarify countdown must use expires_at from the pending payload'

    def test_clarify_countdown_does_not_restart_for_same_expiry(self):
        src = self._get_js().read_text()
        m = re.search(r'function _startClarifyCountdown.*?(?=\nfunction |\nasync function |\Z)',
                      src, re.DOTALL)
        assert m, '_startClarifyCountdown function not found'
        body = m.group(0)
        assert 'const expiresAt = _clarifyExpiryMs(pending)' in body, \
            'countdown start should compute the next expiry before clearing the existing timer'
        assert '_clarifyCountdownTimer && _clarifyExpiresAt === expiresAt' in body, \
            'same pending clarify poll updates must not restart the countdown interval'
        assert body.index('_clarifyCountdownTimer && _clarifyExpiresAt === expiresAt') < \
               body.index('_clearClarifyCountdownTimer()'), \
            'same-expiry guard must run before clearing the current interval'

    def test_hide_clarify_card_can_preserve_draft(self):
        src = self._get_js().read_text()
        assert 'function _stashClarifyDraft' in src
        assert 'sessionStorage.setItem' in src
        assert "$('msg')" in src, \
            'clarify timeout should keep the typed draft visible in the composer'

    def test_clarify_draft_appends_to_existing_composer_text(self):
        src = self._get_js().read_text()
        m = re.search(r'function _stashClarifyDraft.*?(?=\nfunction |\nasync function |\Z)',
                      src, re.DOTALL)
        assert m, '_stashClarifyDraft function not found'
        body = m.group(0)
        assert 'current.replace(/\\s+$/, "")' in body, \
            'preserved clarify drafts must append after existing composer text instead of replacing it'
        assert '\\n\\n${draft}' in body, \
            'preserved clarify drafts should be separated from existing composer text'

    def test_cancel_stream_does_not_preserve_clarify_draft(self):
        src = self._get_js().read_text()
        m = re.search(r"source\.addEventListener\('cancel'.*?\n    \}\);",
                      src, re.DOTALL)
        assert m, 'cancel event handler not found'
        body = m.group(0)
        assert "hideClarifyCard(true, 'cancelled')" in body, \
            'explicit stream cancel must not use the timeout/terminal draft preservation path'

    def test_clarify_urgent_countdown_has_non_color_cue(self):
        css = self._get_css().read_text()
        m = re.search(r'\.clarify-countdown\.urgent\{([^}]*)\}', css)
        assert m, 'urgent clarify countdown style missing'
        body = m.group(1)
        assert any(prop in body for prop in ('box-shadow', 'outline', 'border', 'text-decoration')), \
            'urgent countdown styling must include a non-color visual cue'

    def test_respond_clarify_calls_hide_with_force(self):
        src = self._get_js().read_text()
        import re
        m = re.search(r'async function respondClarify.*?(?=\nasync function|\nfunction |\Z)',
                      src, re.DOTALL)
        assert m, 'respondClarify function not found'
        body = m.group(0)
        assert 'hideClarifyCard(true' in body, \
            'respondClarify must call hideClarifyCard(true) so card hides immediately after user clicks'
        assert "'sent'" in body, \
            'respondClarify must mark user-submitted hides so drafts are not re-stashed'

    def test_clarify_poll_loop_uses_no_force(self):
        src = self._get_js().read_text()
        assert "else { hideClarifyCard(false, 'expired'); }" in src or \
               "else {hideClarifyCard(false,'expired');}" in src, \
            'Clarify poll loop should hide without force=true'

    def test_show_clarify_card_signature_dedup(self):
        src = self._get_js().read_text()
        import re
        m = re.search(r'function showClarifyCard.*?(?=\nfunction |\nasync function |\Z)',
                      src, re.DOTALL)
        assert m, 'showClarifyCard function not found'
        body = m.group(0)
        assert 'JSON.stringify' in body, 'showClarifyCard must compute a signature via JSON.stringify'
        assert '_clarifySignature' in body, 'showClarifyCard must check/set _clarifySignature'
