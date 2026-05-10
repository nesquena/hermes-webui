"""Regressions for first-turn sessions appearing in the sidebar immediately."""

import pathlib

REPO = pathlib.Path(__file__).parent.parent


def read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


class TestSidebarFirstTurnVisibility:
    def test_messages_send_optimistically_upserts_active_sidebar_row(self):
        src = read("static/messages.js")
        assert "upsertActiveSessionForLocalTurn" in src, (
            "send() must optimistically upsert the active session into the sidebar "
            "as soon as the local user message is pushed."
        )
        push_idx = src.index(
            "S.messages.push(userMsg);renderMessages();appendThinking();setBusy(true);"
        )
        helper_idx = src.index("upsertActiveSessionForLocalTurn", push_idx)
        start_idx = src.index("api('/api/chat/start'", push_idx)
        assert helper_idx < start_idx, (
            "The sidebar row must be rendered before /api/chat/start returns so "
            "tool calls are reachable while the first agent turn is still running."
        )
        pre_start = src[helper_idx:start_idx]
        assert "renderSessionList();" not in pre_start, (
            "Do not re-fetch /api/sessions before /api/chat/start saves pending state; "
            "that race can overwrite the optimistic first-turn row with an empty list."
        )

    def test_sessions_js_has_local_turn_upsert_helper(self):
        src = read("static/sessions.js")
        assert "function upsertActiveSessionForLocalTurn" in src
        start = src.index("function upsertActiveSessionForLocalTurn")
        end = src.index("function renderSessionListFromCache", start)
        body = src[start:end]
        assert "_allSessions.unshift" in body or "_allSessions.splice" in body, (
            "Helper must add a missing active session to the cached sidebar list."
        )
        assert "S.session.message_count" in body and "S.messages.length" in body, (
            "Helper must treat the locally pushed user message as a real sidebar message."
        )
        assert "is_streaming:true" in body.replace(" ", ""), (
            "Optimistic row should render as streaming until the backend reconciles."
        )

    def test_messages_comments_document_why_each_optimistic_upsert_stays_separate(self):
        src = read("static/messages.js")
        assert "First optimistic pass" in src and "before /api/chat/start" in src
        assert "Second optimistic pass" in src and "provisional title" in src
        assert "Third optimistic pass" in src and "stream_id is now known" in src

    def test_chat_start_failure_clears_optimistic_streaming_state(self):
        messages = read("static/messages.js")
        catch_start = messages.index(
            "}catch(e){", messages.index("api('/api/chat/start'")
        )
        failure_start = messages.index(
            "S.messages.push({role:'assistant',content:`**Error:** ${errMsg}`});",
            catch_start,
        )
        catch_body = messages[failure_start : messages.index("return;", failure_start)]
        assert "setBusy(false)" in catch_body, (
            "chat/start failure must leave the active pane idle"
        )
        assert "clearOptimisticSessionStreaming(activeSid)" in catch_body, (
            "If /api/chat/start fails after the optimistic sidebar upsert, the cached row "
            "must drop its streaming spinner immediately instead of waiting for polling."
        )
        assert "void renderSessionList()" in catch_body, (
            "After clearing the optimistic spinner locally, fetch /api/sessions to reconcile "
            "with whatever the server persisted before failing."
        )

        sessions = read("static/sessions.js")
        assert "function clearOptimisticSessionStreaming" in sessions
        clear_start = sessions.index("function clearOptimisticSessionStreaming")
        clear_end = sessions.index("function renderSessionListFromCache", clear_start)
        clear_body = sessions[clear_start:clear_end]
        assert "is_streaming:false" in clear_body.replace(" ", "")
        assert "active_stream_id:null" in clear_body.replace(" ", "")
        assert "_sessionStreamingById.set(sid,false)" in clear_body.replace(" ", "")

    def test_backend_compact_counts_pending_first_turn_as_visible(self):
        src = read("api/models.py")
        compact = src[src.index("def compact") : src.index("def _get_profile_home")]
        assert (
            "has_pending_user_message" in compact and "pending_user_message" in compact
        ), (
            "Session.compact() must account for pending_user_message in sidebar metadata."
        )
        assert "message_count = max(message_count, 1)" in compact, (
            "Pending first user turn should make message_count non-zero for /api/sessions."
        )
        assert "pending_started_at" in compact and "last_message_at" in compact, (
            "Pending first user turn should sort by pending_started_at in the sidebar."
        )

    def test_backend_index_filter_keeps_pending_first_turn_sessions(self):
        src = read("api/models.py")
        index_filter_start = src.index(
            "# Hide empty Untitled sessions from the UI entirely"
        )
        index_filter_end = src.index(
            "result = [s for s in result if not _hide_from_default_sidebar",
            index_filter_start,
        )
        index_filter = src[index_filter_start:index_filter_end]
        assert "has_pending_user_message" in index_filter, (
            "The index-path empty-session filter must exempt pending first-turn sessions, "
            "matching the full-scan fallback."
        )

    def test_session_refresh_preserves_optimistic_first_turn_rows_when_server_lags(
        self,
    ):
        src = read("static/sessions.js")
        assert "function _mergeOptimisticFirstTurnSessions" in src, (
            "renderSessionList() must merge locally optimistically inserted first-turn rows "
            "back into the fetched /api/sessions result. A session switch can re-fetch before "
            "the server has saved pending state, and replacing _allSessions would hide the "
            "new in-flight chat until the stream finishes."
        )
        render_start = src.index("async function renderSessionList")
        render_end = src.index("// ── Gateway session SSE", render_start)
        render_body = src[render_start:render_end]
        assign_idx = render_body.index("_allSessions =")
        assert "_mergeOptimisticFirstTurnSessions" in render_body[: assign_idx + 160], (
            "The fetched session list should be merged with optimistic rows at the assignment "
            "site, before completion transitions or renderSessionListFromCache() run."
        )
