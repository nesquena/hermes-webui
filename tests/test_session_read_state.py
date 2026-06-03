"""Regression tests for server-synced session unread/read state.

The sidebar used to keep unread state only in per-browser localStorage. These
contracts pin a profile-scoped server store, a non-mutating mark-read endpoint,
and the frontend wiring that exposes a clear unread group/badge/action while
leaving recents ordering untouched.
"""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROUTES = ROOT / "api" / "routes.py"
SESSIONS_JS = ROOT / "static" / "sessions.js"
MESSAGES_JS = ROOT / "static" / "messages.js"
I18N = ROOT / "static" / "i18n.js"
STYLE = ROOT / "static" / "style.css"


def _reload_read_state(tmp_path: Path):
    mod = importlib.import_module("api.session_read_state")
    mod = importlib.reload(mod)
    setattr(mod, "READ_STATE_FILE", tmp_path / "session_read_state.json")
    return mod


class TestSessionReadStateStore:
    def test_mark_read_persists_profile_scoped_json_atomically(self, tmp_path):
        state = _reload_read_state(tmp_path)

        row = state.mark_session_read("default", "sid-1", 7)
        state.mark_session_read("work", "sid-1", 2)

        assert row["read_message_count"] == 7
        assert row["read_at"] > 0
        payload = json.loads(state.READ_STATE_FILE.read_text(encoding="utf-8"))
        assert payload["version"] == 1
        assert payload["profiles"]["default"]["sid-1"]["read_message_count"] == 7
        assert payload["profiles"]["work"]["sid-1"]["read_message_count"] == 2
        assert oct(state.READ_STATE_FILE.stat().st_mode & 0o777) == "0o600"

    def test_mark_read_never_decreases_count(self, tmp_path):
        state = _reload_read_state(tmp_path)

        state.mark_session_read("default", "sid-1", 10)
        state.mark_session_read("default", "sid-1", 4)

        row = state.get_session_read_state("default", "sid-1")
        assert row["read_message_count"] == 10
        assert row["manual_unread"] is False

    def test_mark_unread_persists_manual_override_without_reordering_cursor(self, tmp_path):
        state = _reload_read_state(tmp_path)

        state.mark_session_read("default", "sid-1", 10)
        row = state.mark_session_unread("default", "sid-1", 10)

        assert row["read_message_count"] == 10
        assert row["manual_unread"] is True

    def test_prune_deleted_sessions_only_touches_requested_profile(self, tmp_path):
        state = _reload_read_state(tmp_path)
        state.mark_session_read("default", "keep", 1)
        state.mark_session_read("default", "drop", 1)
        state.mark_session_read("other", "drop", 1)

        assert state.prune_deleted_sessions("default", {"keep"}) is True

        assert state.get_session_read_state("default", "keep")
        assert state.get_session_read_state("default", "drop") is None
        assert state.get_session_read_state("other", "drop")


class TestSessionReadEndpointContracts:
    def test_post_session_read_endpoint_is_wired_without_saving_session(self):
        src = ROUTES.read_text(encoding="utf-8")
        assert 'parsed.path == "/api/session/read"' in src
        assert "mark_session_read" in src
        assert "publish_session_list_changed(\"session_read_state\")" in src

        block_start = src.index('parsed.path == "/api/session/read"')
        block_end = src.index('if parsed.path == "/api/session/unread"', block_start)
        block = src[block_start:block_end]
        assert ".save(" not in block
        assert "touch_updated_at" not in block

    def test_post_session_unread_endpoint_is_wired_without_saving_session(self):
        src = ROUTES.read_text(encoding="utf-8")
        assert 'parsed.path == "/api/session/unread"' in src
        assert "mark_session_unread" in src
        assert "publish_session_list_changed(\"session_read_state\")" in src

        block_start = src.index('parsed.path == "/api/session/unread"')
        block_end = src.index('if parsed.path == "/api/personality/set"', block_start)
        block = src[block_start:block_end]
        assert ".save(" not in block
        assert "touch_updated_at" not in block

    def test_sessions_response_merges_read_metadata_after_sorting(self):
        src = ROUTES.read_text(encoding="utf-8")
        sort_idx = src.index("merged.sort(")
        read_idx = src.index("merge_session_read_state(")
        response_idx = src.index('"sessions": safe_merged', read_idx)
        assert sort_idx < read_idx < response_idx


class TestSessionUnreadSidebarContracts:
    def test_server_read_sync_requires_active_visible_focused_session(self):
        src = SESSIONS_JS.read_text(encoding="utf-8")

        helper_start = src.index("function _isSessionActivelyViewedForList")
        helper_end = src.index("function _isSessionLocallyStreaming", helper_start)
        helper = src[helper_start:helper_end]
        assert "S.session.session_id !== sid" in helper
        assert "_loadingSessionId" in helper
        assert "document.visibilityState !== 'visible'" in helper
        assert "!document.hasFocus()" in helper

        setter_start = src.index("function _setSessionViewedCount")
        setter_end = src.index("function _applyServerReadStateToSession", setter_start)
        setter = src[setter_start:setter_end]
        gate_idx = setter.index("_isSessionActivelyViewedForList(sid)")
        post_idx = setter.index("_markSessionReadServer(sid, next)")
        assert gate_idx < post_idx

    def test_message_viewed_helper_noops_when_session_is_not_actively_viewed(self):
        src = MESSAGES_JS.read_text(encoding="utf-8")
        start = src.index("function _markSessionViewed")
        end = src.index("function _isDocumentVisibleAndFocused", start)
        body = src[start:end]

        assert "!_isSessionActivelyViewed(sid)" in body
        assert body.index("!_isSessionActivelyViewed(sid)") < body.rindex("_setSessionViewedCount")

    def test_frontend_uses_server_read_state_before_local_storage_fallback(self):
        src = SESSIONS_JS.read_text(encoding="utf-8")
        assert "function _getServerReadState" in src
        assert "function _markSessionReadServer" in src
        assert "_serverReadStateLoaded" in src

        server_start = src.index("function _getServerReadState")
        server_end = src.index("async function _markSessionReadServer", server_start)
        server_body = src[server_start:server_end]
        assert "manual_unread" in server_body
        assert "read_state_source" in server_body

        func_start = src.index("function _hasUnreadForSession")
        func_end = src.index("function _isSessionActivelyViewedForList", func_start)
        body = src[func_start:func_end]
        assert body.index("_getServerReadState") < body.index("_hasSessionCompletionUnread")
        assert "serverRead.manual_unread" in body
        assert "_clearSessionCompletionUnread(s.session_id)" in body

    def test_pinned_idle_group_renders_before_active_unread_and_recent_without_duplication(self):
        src = SESSIONS_JS.read_text(encoding="utf-8")
        assert "const activeSessions=" in src
        assert "const unreadSessions=" in src
        assert "const pinnedIdleSessions=" in src
        assert "const unpinned=" in src and "!_hasUnreadForSession(s)" in src
        active_idx = src.index("groups.push({label:t('session_time_bucket_active')")
        unread_idx = src.index("groups.push({label:t('session_time_bucket_unread')")
        pinned_idx = src.index("groups.push({label:'★ Pinned',items:pinnedIdleSessions,isPinned:true})")
        bucket_idx = src.index("for(const s of unpinned){")
        assert pinned_idx < active_idx < unread_idx < bucket_idx

    def test_unread_indicator_and_read_state_actions_are_wired_without_text_badge(self):
        src = SESSIONS_JS.read_text(encoding="utf-8")
        assert "function _appendSessionMarkReadAction(menu, session)" in src
        assert "function _appendSessionMarkUnreadAction(menu, session)" in src
        assert "function _appendSessionReadStateAction(menu, session)" in src
        assert "t('session_mark_read')" in src
        assert "t('session_mark_unread')" in src
        assert "'/api/session/read'" in src
        assert "'/api/session/unread'" in src
        assert "session-unread-badge" not in src
        assert "t('session_unread_badge')" not in src
        assert "is-unread" in src

    def test_background_completion_persists_stale_read_cursor_for_cross_browser_sync(self):
        src = SESSIONS_JS.read_text(encoding="utf-8")
        start = src.index("function _markSessionCompletionUnread")
        end = src.index("function _clearSessionCompletionUnread", start)
        body = src[start:end]
        assert "const counts = _getSessionViewedCounts()" in body
        assert "_applyServerReadStateToCachedSession(sid" in body
        assert "_markSessionReadServer(sid, readCount)" in body

    def test_unread_translations_and_styles_exist(self):
        i18n = I18N.read_text(encoding="utf-8")
        for key in (
            "session_time_bucket_unread",
            "session_mark_read",
            "session_mark_read_desc",
            "session_marked_read",
            "session_mark_read_failed",
            "session_mark_unread",
            "session_mark_unread_desc",
            "session_marked_unread",
            "session_mark_unread_failed",
        ):
            assert f"{key}:" in i18n
        assert "session_unread_badge:" not in i18n

        css = STYLE.read_text(encoding="utf-8")
        assert ".session-unread-badge" not in css
        assert ".session-date-header.active" in css
        assert ".session-date-header.pinned" in css
        assert ".session-date-header.unread" in css
        assert ".session-date-header.active .session-date-count" in css
        assert ".session-date-header.pinned .session-date-count" in css
        assert ".session-date-header.unread .session-date-count" in css
        assert ".session-item.unread::before" in css
