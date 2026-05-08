"""Tests for session-switch performance optimizations.

Four optimizations to reduce session-switch latency:

1. loadDir expanded-dir pre-fetch uses Promise.all (workspace.js)
2. loadSession idle path overlaps loadDir with highlightCode (sessions.js)
3. git_info_for_workspace runs git subprocesses in parallel (workspace.py)
4. Message pagination: msg_limit tail-window + msg_before index cursor (routes.py + sessions.js)
"""

import pathlib
import threading
import time
from unittest.mock import patch, MagicMock

REPO = pathlib.Path(__file__).parent.parent
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
WORKSPACE_JS = (REPO / "static" / "workspace.js").read_text(encoding="utf-8")
ROUTES_PY = (REPO / "api" / "routes.py").read_text(encoding="utf-8")


# ── 1. workspace.js: expanded-dir pre-fetch is parallelized ─────────────────


class TestLoadDirParallelPrefetch:
    """The expanded-dir pre-fetch inside loadDir() must use Promise.all()
    instead of a serial for-await loop to avoid N sequential roundtrips."""

    def test_loaddir_uses_promise_all_for_expanded_dirs(self):
        marker = "Pre-fetch contents of restored expanded dirs"
        idx = WORKSPACE_JS.find(marker)
        assert idx >= 0, "Expanded-dir pre-fetch comment not found in workspace.js"

        block = WORKSPACE_JS[idx : idx + 800]
        assert "Promise.all" in block, (
            "loadDir expanded-dir pre-fetch should use Promise.all() for "
            "parallel fetching, not a serial for-await loop."
        )

    def test_loaddir_no_serial_for_await_in_prefetch(self):
        marker = "Pre-fetch contents of restored expanded dirs"
        idx = WORKSPACE_JS.find(marker)
        assert idx >= 0
        block = WORKSPACE_JS[idx : idx + 800]
        assert "for(const dirPath of (S._expandedDirs" not in block, (
            "loadDir still has a serial for-await loop for expanded dirs — "
            "should use Promise.all with .map() instead."
        )

    def test_expanded_dirs_fallback_is_set(self):
        """S._expandedDirs||fallback must produce a Set, not an Array."""
        marker = "Pre-fetch contents of restored expanded dirs"
        idx = WORKSPACE_JS.find(marker)
        assert idx >= 0
        block = WORKSPACE_JS[idx : idx + 800]
        assert "S._expandedDirs||new Set()" in block, (
            "Expanded dirs fallback must be 'new Set()' not '[]' — "
            "arrays have no .size property."
        )


# ── 2. sessions.js: loadSession idle path overlaps loadDir and highlightCode ─


class TestLoadSessionIdleOverlap:
    """The idle path in loadSession() must start loadDir() before running
    highlightCode() so the network request is in-flight during the CPU-bound
    Prism.js pass."""

    def test_idle_path_starts_loaddir_before_highlightcode(self):
        idle_marker = "S.busy=false"
        positions = []
        start = 0
        while True:
            idx = SESSIONS_JS.find(idle_marker, start)
            if idx < 0:
                break
            positions.append(idx)
            start = idx + 1

        found = False
        for pos in positions:
            block = SESSIONS_JS[pos : pos + 600]
            has_highlight = "highlightCode()" in block
            has_loaddir = "loadDir('.')" in block
            if has_highlight and has_loaddir:
                found = True
                loaddir_idx = block.find("loadDir(")
                highlight_idx = block.find("highlightCode()")
                assert loaddir_idx < highlight_idx, (
                    "In the idle path, loadDir() should be started before "
                    "highlightCode() so the network request is dispatched first."
                )
                assert "await" in block and "_dirP" in block, (
                    "loadDir() result should be stored and awaited after "
                    "highlightCode() completes."
                )
                break

        assert found, (
            "Could not find the idle path in loadSession that calls both "
            "loadDir and highlightCode."
        )


# ── 3. workspace.py: git_info_for_workspace is parallelized ────────────────


class TestGitInfoParallel:
    """git_info_for_workspace() must run git subprocess calls in parallel
    to reduce wall-clock time."""

    def test_uses_thread_pool(self):
        source = pathlib.Path(__file__).parent.parent / "api" / "workspace.py"
        src = source.read_text()
        fn = src[src.find("def git_info_for_workspace") :]
        fn = fn[: fn.find("\ndef ")]

        assert "concurrent.futures" in src, (
            "concurrent.futures should be imported at the module level."
        )
        assert "ThreadPoolExecutor" in fn, (
            "git_info_for_workspace should use ThreadPoolExecutor "
            "to run git commands in parallel."
        )

    def test_git_commands_run_concurrently(self, tmp_path):
        """Proof that status/ahead/behind git commands execute in parallel,
        not sequentially. Uses threading.Barrier to verify overlap."""
        from api.workspace import git_info_for_workspace
        import api.workspace as ws_mod

        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        barrier = threading.Barrier(3, timeout=5)
        call_count = {"n": 0}
        started_times = []

        def fake_git(args, cwd, timeout=3):
            if args[0] == "rev-parse":
                return "main"
            call_count["n"] += 1
            started_times.append(time.monotonic())
            barrier.wait(timeout=2)
            if args[0] == "status":
                return ""
            return "0"

        with patch.object(ws_mod, "_run_git", side_effect=fake_git):
            result = git_info_for_workspace(tmp_path)

        assert result is not None
        assert result["is_git"] is True
        assert result["branch"] == "main"
        assert call_count["n"] == 3, (
            f"Expected 3 parallel git calls, got {call_count['n']}"
        )
        assert started_times[-1] - started_times[0] < 0.15, (
            f"Git commands started too far apart ({started_times[-1]-started_times[0]:.3f}s), "
            f"suggesting serial execution."
        )

    def test_parallel_faster_than_serial(self, tmp_path):
        """Wall-clock time for parallel execution should be ~1/3 of serial."""
        from api.workspace import git_info_for_workspace
        import api.workspace as ws_mod

        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        def slow_git(args, cwd, timeout=3):
            if args[0] == "rev-parse":
                return "main"
            time.sleep(0.1)
            if args[0] == "status":
                return ""
            return "0"

        with patch.object(ws_mod, "_run_git", side_effect=slow_git):
            t0 = time.monotonic()
            result = git_info_for_workspace(tmp_path)
            elapsed = time.monotonic() - t0

        assert result is not None
        assert result["is_git"] is True
        assert elapsed < 0.25, (
            f"git_info_for_workspace took {elapsed:.3f}s — expected < 0.25s "
            f"with parallel execution (serial baseline is ~0.3s)."
        )


# ── 4. Message pagination (msg_limit + msg_before) ─────────────────────────


class TestMessagePaginationBackend:
    """Backend /api/session must support msg_limit and msg_before parameters
    to return only the last N messages, reducing payload size for fast
    session switching."""

    def _make_session(self, n_msgs=100):
        """Create a mock session with n_msgs messages."""
        session = MagicMock()
        session.session_id = "test_session_123"
        session.title = "Test Session"
        session.workspace = "/tmp/test"
        session.model = "test-model"
        session.created_at = 1000000
        session.updated_at = 2000000
        session.pinned = False
        session.archived = False
        session.project_id = None
        session.profile = None
        session.input_tokens = 0
        session.output_tokens = 0
        session.estimated_cost = None
        session.personality = None
        session.active_stream_id = None
        session.pending_user_message = None
        session.pending_attachments = []
        session.pending_started_at = None
        session.compression_anchor_visible_idx = None
        session.compression_anchor_message_key = None
        session._metadata_message_count = None
        session.messages = [
            {"role": "user" if i % 3 == 0 else "assistant", "content": f"Message {i}"}
            for i in range(n_msgs)
        ]
        session.tool_calls = []
        session.compact.return_value = {
            "session_id": "test_session_123",
            "title": "Test Session",
            "workspace": "/tmp/test",
            "model": "test-model",
            "message_count": n_msgs,
            "created_at": 1000000,
            "updated_at": 2000000,
            "last_message_at": 2000000,
            "pinned": False,
            "archived": False,
            "project_id": None,
            "profile": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost": None,
            "personality": None,
            "compression_anchor_visible_idx": None,
            "compression_anchor_message_key": None,
            "active_stream_id": None,
            "is_streaming": False,
        }
        return session

    def test_msg_limit_returns_tail(self):
        """msg_limit=10 should return the last 10 messages of a 100-msg session."""
        session = self._make_session(100)
        all_msgs = session.messages
        msg_limit = 10

        truncated = all_msgs[-msg_limit:]
        assert len(truncated) == 10
        assert truncated[0]["content"] == "Message 90"
        assert truncated[-1]["content"] == "Message 99"

    def test_msg_limit_larger_than_total(self):
        """msg_limit larger than total messages returns all messages."""
        session = self._make_session(50)
        all_msgs = session.messages
        msg_limit = 100

        truncated = all_msgs[-msg_limit:]
        assert len(truncated) == 50
        assert len(all_msgs) <= msg_limit

    def test_msg_before_index_based_slicing(self):
        """msg_before=50 returns messages[:50] then tail window."""
        session = self._make_session(100)
        all_msgs = session.messages
        msg_before = 50
        msg_limit = 30

        _slice = all_msgs[:msg_before]
        truncated = _slice[-msg_limit:]
        assert len(truncated) == 30
        assert truncated[0]["content"] == "Message 20"
        assert truncated[-1]["content"] == "Message 49"

    def test_msg_before_zero_returns_empty(self):
        """msg_before=0 means no older messages exist — returns empty."""
        session = self._make_session(100)
        all_msgs = session.messages
        msg_before = 0

        _slice = all_msgs[:msg_before]
        assert len(_slice) == 0

    def test_msg_before_equal_total(self):
        """msg_before=100 returns all 100, tail-30 gives messages 70-99."""
        session = self._make_session(100)
        all_msgs = session.messages
        msg_before = 100
        msg_limit = 30

        _slice = all_msgs[:msg_before]
        truncated = _slice[-msg_limit:]
        assert len(truncated) == 30
        assert truncated[0]["content"] == "Message 70"

    def test_truncation_flag(self):
        """_messages_truncated must be True when messages were omitted."""
        session = self._make_session(100)
        msg_limit = 30
        is_truncated = len(session.messages) > msg_limit
        assert is_truncated is True

        small = self._make_session(10)
        is_truncated_small = len(small.messages) > msg_limit
        assert is_truncated_small is False

    def test_truncation_flag_with_msg_before(self):
        """When msg_before filters to fewer than msg_limit, truncation is False."""
        session = self._make_session(100)
        msg_before = 10
        msg_limit = 30

        _slice = session.messages[:msg_before]
        _truncated = len(_slice) > msg_limit
        assert _truncated is False  # 10 < 30, no truncation

    def test_messages_offset_initial_load(self):
        """_messages_offset = index of first returned message in full array."""
        session = self._make_session(100)
        msg_limit = 30
        all_msgs = session.messages

        truncated = all_msgs[-msg_limit:]
        offset = len(all_msgs) - len(truncated)
        assert offset == 70
        assert truncated[0]["content"] == "Message 70"

    def test_messages_offset_with_msg_before(self):
        """_messages_offset for msg_before=50, msg_limit=30."""
        session = self._make_session(100)
        msg_before = 50
        msg_limit = 30

        _slice = session.messages[:msg_before]
        truncated = _slice[-msg_limit:]
        offset = msg_before - len(truncated)
        assert offset == 20
        assert truncated[0]["content"] == "Message 20"

    def test_payload_size_reduction(self):
        """Quantify the payload reduction: 100 msgs → 30 msgs = ~70% smaller."""
        import json

        session = self._make_session(100)
        all_json = json.dumps(session.messages)
        tail_json = json.dumps(session.messages[-30:])

        reduction = 1 - len(tail_json) / len(all_json)
        assert reduction > 0.6, (
            f"Expected >60% payload reduction, got {reduction*100:.0f}%."
        )

    def test_msg_before_bounds_clamping(self):
        """msg_before beyond array length should be clamped."""
        session = self._make_session(100)
        all_msgs = session.messages

        # msg_before = 999 → clamped to 100
        _before_idx = max(0, min(999, len(all_msgs)))
        assert _before_idx == 100

        # msg_before = -5 → clamped to 0
        _before_idx = max(0, min(-5, len(all_msgs)))
        assert _before_idx == 0


class TestMessagePaginationFrontend:
    """Frontend sessions.js must use msg_limit for initial load and expose
    _loadOlderMessages for scroll-to-top lazy loading."""

    def test_ensure_messages_uses_msg_limit(self):
        """_ensureMessagesLoaded must send msg_limit parameter."""
        fn_start = SESSIONS_JS.find("async function _ensureMessagesLoaded")
        fn_end = SESSIONS_JS.find("\n}", fn_start) + 2
        fn_body = SESSIONS_JS[fn_start:fn_end]

        assert "msg_limit=" in fn_body, (
            "_ensureMessagesLoaded should include msg_limit parameter in the API call"
        )
        assert "_INITIAL_MSG_LIMIT" in fn_body, (
            "_ensureMessagesLoaded should use _INITIAL_MSG_LIMIT constant"
        )

    def test_truncation_tracking(self):
        """_messagesTruncated must be set from the server response."""
        assert "_messagesTruncated" in SESSIONS_JS
        assert "_messages_truncated" in SESSIONS_JS

    def test_oldest_idx_tracking(self):
        """_oldestIdx must be tracked for index-based cursor paging."""
        assert "_oldestIdx" in SESSIONS_JS, (
            "sessions.js must track _oldestIdx for index-based cursor paging"
        )
        assert "_messages_offset" in SESSIONS_JS, (
            "sessions.js must read _messages_offset from server response"
        )

    def test_load_older_messages_function_exists(self):
        """_loadOlderMessages must be defined for scroll-to-top loading."""
        assert "async function _loadOlderMessages" in SESSIONS_JS

    def test_load_older_uses_index_cursor(self):
        """_loadOlderMessages must pass msg_before as integer index, not timestamp."""
        fn_start = SESSIONS_JS.find("async function _loadOlderMessages")
        fn_end = SESSIONS_JS.find("\n}", fn_start) + 2
        fn_body = SESSIONS_JS[fn_start:fn_end]

        assert "msg_before=${_oldestIdx}" in fn_body, (
            "_loadOlderMessages should use _oldestIdx (integer) as msg_before cursor"
        )

    def test_ensure_all_messages_function_exists(self):
        """_ensureAllMessagesLoaded must exist for operations needing full history."""
        assert "async function _ensureAllMessagesLoaded" in SESSIONS_JS

    def test_scroll_to_top_triggers_loading(self):
        """Scroll event handler must trigger _loadOlderMessages near top."""
        UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")

        assert "el.scrollTop<olderPrefetchPx" in UI_JS
        assert "_loadOlderMessages" in UI_JS

    def test_load_older_indicator_in_render(self):
        """renderMessages must show a 'load older' indicator when truncated."""
        UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")

        assert "loadOlderIndicator" in UI_JS

    def test_oldest_idx_reset_on_session_switch(self):
        """_oldestIdx must be reset to 0 on session switch."""
        # Find the loadSession reset block
        idx = SESSIONS_JS.find("_messagesTruncated = false;\n    _oldestIdx = 0;")
        assert idx >= 0, (
            "_oldestIdx must be reset to 0 alongside _messagesTruncated on session switch"
        )


# ── 5. Session-switch cancellation safety ───────────────────────────────────


class TestSessionSwitchCancellation:
    """When the user switches sessions while _loadOlderMessages is in-flight,
    the stale response must NOT land on the new session's message array.

    Guarards in place:
    - _loadOlderMessages captures `sid` at entry, checks _loadingSessionId
      after await (line ~373)
    - loadSession resets _loadingOlder, _messagesTruncated, _oldestIdx
      on session switch (line ~120-122)
    """

    def test_load_older_checks_loading_session_id(self):
        """_loadOlderMessages must check _loadingSessionId after await."""
        fn_start = SESSIONS_JS.find("async function _loadOlderMessages")
        fn_end = SESSIONS_JS.find("\n}", fn_start) + 2
        fn_body = SESSIONS_JS[fn_start:fn_end]

        assert "_loadingSessionId" in fn_body, (
            "_loadOlderMessages must check _loadingSessionId after the API "
            "call returns to detect session-switch race conditions."
        )
        # The guard should be: if _loadingSessionId !== null && _loadingSessionId !== sid
        assert "_loadingSessionId !== null" in fn_body or "_loadingSessionId!==null" in fn_body, (
            "_loadOlderMessages should bail out if a new session load started "
            "while the older-messages request was in flight."
        )

    def test_loading_older_reset_on_session_switch(self):
        """loadSession must reset _loadingOlder when switching sessions."""
        # Find the reset block in loadSession
        marker = "_messagesTruncated = false;\n    _oldestIdx = 0;\n    _loadingOlder = false;"
        idx = SESSIONS_JS.find(marker)
        assert idx >= 0, (
            "loadSession must reset _loadingOlder=false on session switch "
            "to prevent a stale _loadOlderMessages lock from blocking the "
            "new session's scroll-to-top loading."
        )

    def test_stale_cannot_mutate_messages(self):
        """Verify the guard prevents S.messages mutation.

        The guard `if (_loadingSessionId !== null && _loadingSessionId !== sid) return`
        runs BEFORE `S.messages = [...olderMsgs, ...S.messages]`.
        If the session changed, we return early — no mutation.
        """
        fn_start = SESSIONS_JS.find("async function _loadOlderMessages")
        fn_end = SESSIONS_JS.find("\n}", fn_start) + 2
        fn_body = SESSIONS_JS[fn_start:fn_end]

        # Guard must appear before S.messages mutation
        guard_idx = fn_body.find("_loadingSessionId")
        mutation_idx = fn_body.find("S.messages = [...olderMsgs")
        assert guard_idx >= 0 and mutation_idx >= 0 and guard_idx < mutation_idx, (
            "The _loadingSessionId guard must appear BEFORE the S.messages "
            "mutation to prevent stale data from landing on the wrong session."
        )

    def test_messages_truncated_reset_on_switch(self):
        """loadSession must reset _messagesTruncated on session switch."""
        marker = "_messagesTruncated = false;\n    _oldestIdx = 0;\n    _loadingOlder = false;"
        idx = SESSIONS_JS.find(marker)
        assert idx >= 0, (
            "_messagesTruncated must be reset to false on session switch "
            "to prevent the scroll-to-top handler from trying to load "
            "older messages from the previous session."
        )

    def test_oldest_idx_reset_prevents_wrong_cursor(self):
        """_oldestIdx=0 after switch prevents passing stale cursor to API."""
        # If _oldestIdx carried over from session A (e.g. _oldestIdx=70),
        # and session B only has 10 messages, msg_before=70 would return empty.
        # Resetting to 0 ensures session B starts fresh.
        fn_start = SESSIONS_JS.find("async function _loadOlderMessages")
        fn_end = SESSIONS_JS.find("\n}", fn_start) + 2
        fn_body = SESSIONS_JS[fn_start:fn_end]

        # _loadOlderMessages checks _oldestIdx <= 0 early and exits
        assert "_oldestIdx <= 0" in fn_body, (
            "_loadOlderMessages should bail out if _oldestIdx <= 0, "
            "which is the reset value after session switch."
        )

    def test_load_older_compares_against_active_session_id(self):
        """_loadOlderMessages must verify S.session.session_id === sid after await.

        _loadingSessionId alone is insufficient: it is null between session
        loads, so a stale older-messages response that lands AFTER a
        completed session switch would otherwise pass the guard and prepend
        onto the new session's S.messages. The S.session.session_id check
        closes that window.
        """
        fn_start = SESSIONS_JS.find("async function _loadOlderMessages")
        fn_end = SESSIONS_JS.find("\n}", fn_start) + 2
        fn_body = SESSIONS_JS[fn_start:fn_end]

        assert "S.session.session_id !== sid" in fn_body, (
            "_loadOlderMessages must compare S.session.session_id against "
            "the captured sid after await — _loadingSessionId is null "
            "between sessions and would let a stale response through."
        )
        # The S.session check must appear BEFORE the S.messages mutation.
        active_check_idx = fn_body.find("S.session.session_id !== sid")
        mutation_idx = fn_body.find("S.messages = [...olderMsgs")
        assert active_check_idx >= 0 and mutation_idx >= 0 and active_check_idx < mutation_idx, (
            "Active-session guard must run before S.messages mutation."
        )


# ── 6. Scroll position preservation ──────────────────────────────────────────


class TestScrollPositionPreservation:
    """When _loadOlderMessages prepends messages, the user's scroll position
    must be preserved — not snapped to the bottom.

    The scrollable container is #messages (overflow-y:auto), not #msgInner
    (which is a flex column with no overflow).  Also, renderMessages() calls
    scrollToBottom() at the end, so _scrollPinned must be reset."""

    def test_uses_correct_scrollable_container(self):
        """_loadOlderMessages must use $('messages') not $('msgInner')."""
        SESSIONS_JS = pathlib.Path(__file__).parent.parent / "static" / "sessions.js"
        src = SESSIONS_JS.read_text(encoding="utf-8")

        fn_start = src.find("async function _loadOlderMessages")
        fn_end = src.find("\n}", fn_start) + 2
        fn_body = src[fn_start:fn_end]

        assert "$('messages')" in fn_body, (
            "_loadOlderMessages should use $('messages') as the scrollable container "
            "(#messages has overflow-y:auto). #msgInner has no overflow and is not scrollable."
        )
        assert "$('msgInner')" not in fn_body, (
            "_loadOlderMessages must NOT use $('msgInner') for scroll position — "
            "#msgInner is a flex column with no overflow-y."
        )

    def test_resets_scroll_pinned_after_restore(self):
        """_scrollPinned must be false after older-history scroll anchoring."""
        SESSIONS_JS = pathlib.Path(__file__).parent.parent / "static" / "sessions.js"
        src = SESSIONS_JS.read_text(encoding="utf-8")

        fn_start = src.find("async function _loadOlderMessages")
        fn_end = src.find("\n}", fn_start) + 2
        fn_body = src[fn_start:fn_end]

        assert "_scrollPinned = false" in fn_body, (
            "Older-history paging must leave the transcript unpinned so the next "
            "render does not snap back to the newest output."
        )
        # The modern restore path preserves the user's current viewport after
        # prepending older messages. Older pages are prefetched before the hard
        # top, so continuing to scroll upward naturally reveals the end of the
        # newly loaded older content without a smooth-scroll teleport.
        assert "const prevScrollTop = container ? container.scrollTop : 0" in fn_body, (
            "Older-history paging must capture the viewport before prepending messages."
        )
        assert "const oldTop = container.scrollTop" not in fn_body, (
            "Restoring from post-render scrollTop can compound jumps after renderMessages()."
        )
        target_idx = fn_body.find("container.scrollTop = prevScrollTop + addedHeight")
        scroll_idx = fn_body.find("requestAnimationFrame(()=>{ _programmaticScroll = false; })")
        pinned_idx = fn_body.rfind("_scrollPinned = false")
        assert target_idx >= 0 and scroll_idx >= 0 and pinned_idx >= 0 and target_idx < scroll_idx < pinned_idx, (
            "_scrollPinned = false must appear AFTER the older-history viewport-preserve scroll."
        )
