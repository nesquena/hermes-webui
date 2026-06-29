"""
Regression tests for #1432 (new-chat empty-session guard ignores in-flight streams)
and #1423 (profile name input lacks autocapitalize/spellcheck attrs).

Both bugs ship as static-asset diffs verified by reading the JS files.
"""
import os
import re

STATIC_DIR = os.path.join(os.path.dirname(__file__), '..', 'static')


def _read(filename):
    return open(os.path.join(STATIC_DIR, filename), encoding='utf-8').read()


def _current_session_is_reusable_empty_chat_body(src):
    start = src.find("function _currentSessionIsReusableEmptyChat()")
    end = src.find("$('fileInput').onchange", start)
    assert start >= 0 and end >= 0, "empty-session reuse helper not found in boot.js"
    return src[start:end]


class TestIssue1432NewChatGuardInFlight:
    """`+` button and Cmd/Ctrl+K must create a new chat even while the current
    session is still streaming. The empty-session guard from #1171 was checking
    `message_count===0` only, which is true the entire time the first user
    message is in flight (server-side count not yet updated). The guard now
    also requires `!S.busy && !S.session.active_stream_id &&
    !S.session.pending_user_message` — same in-flight signal used at
    `static/messages.js:_restoreSettledSession()`.
    """

    def test_btnNewChat_handler_checks_in_flight_state(self):
        src = _read('boot.js')
        helper = _current_session_is_reusable_empty_chat_body(src)

        # The empty-session guard must check all three in-flight signals.
        assert 'message_count' in helper, \
            "shared New Chat guard missing message_count check"
        assert 'S.busy' in helper, \
            "shared New Chat guard missing S.busy check (#1432)"
        assert 'active_stream_id' in helper, \
            "shared New Chat guard missing active_stream_id check (#1432)"
        assert 'pending_user_message' in helper, \
            "shared New Chat guard missing pending_user_message check (#1432)"

        # Locate the btnNewChat onclick handler and verify it uses the helper.
        m = re.search(
            r"\$\('btnNewChat'\)\.onclick=async\(\)=>\{(.*?)\};",
            src, re.DOTALL,
        )
        assert m, "btnNewChat onclick handler not found in boot.js"
        body = m.group(1)
        assert '_currentSessionIsReusableEmptyChat()' in body, \
            "btnNewChat handler must use the shared empty-session guard"

    def test_cmdK_handler_checks_in_flight_state(self):
        src = _read('boot.js')
        helper = _current_session_is_reusable_empty_chat_body(src)

        assert 'message_count' in helper, \
            "shared New Chat guard missing message_count check"
        assert 'S.busy' in helper, \
            "shared New Chat guard missing S.busy check (#1432)"
        assert 'active_stream_id' in helper, \
            "shared New Chat guard missing active_stream_id check (#1432)"
        assert 'pending_user_message' in helper, \
            "shared New Chat guard missing pending_user_message check (#1432)"

        # Locate the Cmd/Ctrl+K branch — it sits inside a keydown listener
        idx = src.find("(e.metaKey||e.ctrlKey)&&e.key==='k'")
        assert idx >= 0, "Cmd/Ctrl+K handler not found in boot.js"
        # Read the next ~1500 chars (handler body)
        body = src[idx:idx + 1500]
        assert '_currentSessionIsReusableEmptyChat()' in body, \
            "Cmd/Ctrl+K handler must use the shared empty-session guard"

    def test_in_flight_signal_matches_restoreSettledSession(self):
        """The new in-flight check uses the same signal as the canonical
        'session is in flight' detector at messages.js:_restoreSettledSession.
        Verifying both files use the same shape so future refactors don't
        diverge."""
        msgs_src = _read('messages.js')
        # The canonical detector
        assert 'session.active_stream_id||session.pending_user_message' in msgs_src, \
            "Canonical in-flight detector at _restoreSettledSession changed shape — " \
            "boot.js #1432 fix uses the same signals; keep them aligned"


class TestIssue1423ProfileFormAutocapitalize:
    """Profile name and base-url inputs must suppress browser
    auto-capitalization, autocorrect, and spell-check. Without these
    attributes, mobile keyboards (iOS/Android) capitalize the first letter
    and desktop spellcheck can rewrite the typed value on blur — even though
    the placeholder/hint says lowercase only. The form lowercases on submit
    so stored data is correct; the bug is purely a misleading display."""

    def _profile_input_html(self, input_id):
        src = _read('panels.js')
        # Match the input element — pull the full opening tag
        m = re.search(
            rf'<input\s+[^>]*id="{re.escape(input_id)}"[^>]*>',
            src,
        )
        return m.group(0) if m else None

    def test_profile_name_has_autocapitalize_none(self):
        html = self._profile_input_html('profileFormName')
        assert html, "profileFormName input not found in panels.js"
        assert 'autocapitalize="none"' in html, \
            f"profileFormName missing autocapitalize=\"none\" (#1423): {html}"

    def test_profile_name_has_spellcheck_false(self):
        html = self._profile_input_html('profileFormName')
        assert html, "profileFormName input not found"
        assert 'spellcheck="false"' in html, \
            f"profileFormName missing spellcheck=\"false\" (#1423): {html}"

    def test_profile_name_has_autocorrect_off(self):
        html = self._profile_input_html('profileFormName')
        assert html, "profileFormName input not found"
        assert 'autocorrect="off"' in html, \
            f"profileFormName missing autocorrect=\"off\" (#1423): {html}"

    def test_profile_name_keeps_required(self):
        """Regression guard: required must still be present."""
        html = self._profile_input_html('profileFormName')
        assert ' required' in html, \
            f"profileFormName lost required attribute: {html}"

    def test_profile_baseurl_has_autocapitalize_none(self):
        """Base URL inputs are equally bad targets for autocapitalize."""
        html = self._profile_input_html('profileFormBaseUrl')
        assert html, "profileFormBaseUrl input not found"
        assert 'autocapitalize="none"' in html, \
            f"profileFormBaseUrl missing autocapitalize=\"none\" (#1423)"
        assert 'spellcheck="false"' in html, \
            f"profileFormBaseUrl missing spellcheck=\"false\" (#1423)"
