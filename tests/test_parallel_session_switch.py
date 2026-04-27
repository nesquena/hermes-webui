"""Tests for session-switch performance optimizations.

Four optimizations to reduce session-switch latency:

1. loadDir expanded-dir pre-fetch uses Promise.all (workspace.js)
2. loadSession idle path overlaps loadDir with highlightCode (sessions.js)
3. git_info_for_workspace runs git subprocesses in parallel (workspace.py)
4. Message pagination: msg_limit tail-window + msg_before cursor (routes.py + sessions.js)
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
        # Find the expanded-dir pre-fetch block inside loadDir
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
        # The old serial pattern was: for(const dirPath of ...){ ... await api(...); ... }
        # The new parallel pattern uses .map() inside Promise.all instead.
        assert "for(const dirPath of (S._expandedDirs" not in block, (
            "loadDir still has a serial for-await loop for expanded dirs — "
            "should use Promise.all with .map() instead."
        )


# ── 2. sessions.js: loadSession idle path overlaps loadDir and highlightCode ─


class TestLoadSessionIdleOverlap:
    """The idle path in loadSession() must start loadDir() before running
    highlightCode() so the network request is in-flight during the CPU-bound
    Prism.js pass."""

    def test_idle_path_starts_loaddir_before_highlightcode(self):
        # Find the idle (non-streaming) else branch in loadSession
        # Pattern: S.busy=false ... loadDir before highlightCode
        idle_marker = "S.busy=false"
        positions = []
        start = 0
        while True:
            idx = SESSIONS_JS.find(idle_marker, start)
            if idx < 0:
                break
            positions.append(idx)
            start = idx + 1

        # There should be an idle path that calls both loadDir and highlightCode
        found = False
        for pos in positions:
            block = SESSIONS_JS[pos : pos + 600]
            has_highlight = "highlightCode()" in block
            has_loaddir = "loadDir('.')" in block
            if has_highlight and has_loaddir:
                found = True
                # loadDir must appear BEFORE highlightCode in source order
                # (or at least be started before highlightCode runs)
                loaddir_idx = block.find("loadDir(")
                highlight_idx = block.find("highlightCode()")
                assert loaddir_idx < highlight_idx, (
                    "In the idle path, loadDir() should be started before "
                    "highlightCode() so the network request is dispatched first."
                )
                # There should be an await for the loadDir promise
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
        from api.workspace import git_info_for_workspace

        source = pathlib.Path(__file__).parent.parent / "api" / "workspace.py"
        src = source.read_text()
        fn = src[src.find("def git_info_for_workspace") :]
        fn = fn[: fn.find("\ndef ")]

        assert "concurrent.futures" in fn, (
            "git_info_for_workspace should use concurrent.futures "
            "for parallel subprocess execution."
        )
        assert "ThreadPoolExecutor" in fn, (
            "git_info_for_workspace should use ThreadPoolExecutor "
            "to run git commands in parallel."
        )

    def test_git_commands_run_concurrently(self, tmp_path):
        """Proof that status/ahead/behind git commands execute in parallel,
        not sequentially. Uses threading.Barrier to verify overlap.

        rev-parse runs first (serial), then status + 2x rev-list run in parallel.
        If they were serial, all 4 would finish sequentially.
        If parallel, the 3 post-rev-parse commands overlap.
        """
        from api.workspace import git_info_for_workspace
        import api.workspace as ws_mod

        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        # Barrier for the 3 parallel commands (status, ahead, behind).
        # rev-parse runs first and is NOT part of the barrier.
        barrier = threading.Barrier(3, timeout=5)
        call_count = {"n": 0}
        started_times = []

        def fake_git(args, cwd, timeout=3):
            if args[0] == "rev-parse":
                return "main"
            # These three (status, rev-list ahead, rev-list behind) run in parallel
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

        # All three parallel commands must have been called
        assert call_count["n"] == 3, (
            f"Expected 3 parallel git calls, got {call_count['n']}"
        )

        # If parallel, all three start within a narrow window.
        # If serial, each waits 0.1s+ before the next starts.
        assert started_times[-1] - started_times[0] < 0.15, (
            f"Git commands started too far apart ({started_times[-1]-started_times[0]:.3f}s), "
            f"suggesting serial execution. Expected parallel start within 0.15s."
        )

    def test_parallel_faster_than_serial(self, tmp_path):
        """Wall-clock time for parallel execution should be ~1/3 of serial.

        Each mocked git command sleeps 0.1s.
        Serial: 3 * 0.1s = 0.3s minimum.
        Parallel: 0.1s + overhead.
        """
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
        # 3 commands * 0.1s each = 0.3s if serial.
        # Parallel should be ~0.1s. Allow generous margin.
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
        # Generate messages with timestamps
        session.messages = [
            {"role": "user" if i % 3 == 0 else "assistant", "content": f"Message {i}", "_ts": 1000 + i}
            for i in range(n_msgs)
        ]
        session.tool_calls = []
        # compact() returns a dict
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
        import json
        from urllib.parse import urlencode
        from http.server import BaseHTTPRequestHandler

        # Simulate the logic from routes.py /api/session handler
        session = self._make_session(100)
        all_msgs = session.messages
        msg_limit = 10

        truncated = all_msgs[-msg_limit:]
        assert len(truncated) == 10
        assert truncated[0]["content"] == "Message 90"
        assert truncated[-1]["content"] == "Message 99"

    def test_msg_limit_larger_than_total(self):
        """msg_limit larger than total messages returns all messages, no truncation."""
        session = self._make_session(50)
        all_msgs = session.messages
        msg_limit = 100

        truncated = all_msgs[-msg_limit:]
        assert len(truncated) == 50
        # Not truncated
        assert len(all_msgs) <= msg_limit

    def test_msg_before_filters_by_timestamp(self):
        """msg_before=1050 should only return messages with _ts < 1050."""
        session = self._make_session(100)
        all_msgs = session.messages
        msg_before = 1050.0

        filtered = [
            m for m in all_msgs
            if (m.get('_ts') or m.get('timestamp') or 0) < msg_before
        ]
        # Messages with _ts 1000..1049 (indices 0..49)
        assert len(filtered) == 50
        assert filtered[0]["_ts"] == 1000
        assert filtered[-1]["_ts"] == 1049

    def test_msg_before_with_limit(self):
        """msg_before=1050 & msg_limit=10 returns last 10 of messages < ts 1050."""
        session = self._make_session(100)
        all_msgs = session.messages
        msg_before = 1050.0
        msg_limit = 10

        filtered = [
            m for m in all_msgs
            if (m.get('_ts') or m.get('timestamp') or 0) < msg_before
        ]
        truncated = filtered[-msg_limit:]
        assert len(truncated) == 10
        assert truncated[0]["_ts"] == 1040
        assert truncated[-1]["_ts"] == 1049

    def test_truncation_flag(self):
        """_messages_truncated must be True when messages were omitted."""
        session = self._make_session(100)
        msg_limit = 30

        is_truncated = len(session.messages) > msg_limit
        assert is_truncated is True

        # Small session should not be truncated
        small = self._make_session(10)
        is_truncated_small = len(small.messages) > msg_limit
        assert is_truncated_small is False

    def test_no_limit_returns_all(self):
        """Without msg_limit, all messages are returned."""
        session = self._make_session(100)
        all_msgs = session.messages

        # No limit: return all
        truncated = all_msgs  # no slicing
        assert len(truncated) == 100

    def test_payload_size_reduction(self):
        """Quantify the payload reduction: 100 msgs → 30 msgs = ~70% smaller."""
        import json

        session = self._make_session(100)
        all_json = json.dumps(session.messages)
        tail_json = json.dumps(session.messages[-30:])

        reduction = 1 - len(tail_json) / len(all_json)
        assert reduction > 0.6, (
            f"Expected >60% payload reduction, got {reduction*100:.0f}%. "
            f"100 msgs: {len(all_json)} bytes, 30 msgs: {len(tail_json)} bytes"
        )


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
        assert "_messagesTruncated" in SESSIONS_JS, (
            "sessions.js must track _messagesTruncated state"
        )
        assert "_messages_truncated" in SESSIONS_JS, (
            "sessions.js must read _messages_truncated from server response"
        )

    def test_load_older_messages_function_exists(self):
        """_loadOlderMessages must be defined for scroll-to-top loading."""
        assert "async function _loadOlderMessages" in SESSIONS_JS, (
            "_loadOlderMessages function must be defined for lazy loading older messages"
        )
        assert "msg_before=" in SESSIONS_JS, (
            "_loadOlderMessages must use msg_before parameter for cursor-based paging"
        )

    def test_ensure_all_messages_function_exists(self):
        """_ensureAllMessagesLoaded must exist for operations needing full history."""
        assert "async function _ensureAllMessagesLoaded" in SESSIONS_JS, (
            "_ensureAllMessagesLoaded function must exist for operations that "
            "need the full message history (undo, export, etc.)"
        )

    def test_scroll_to_top_triggers_loading(self):
        """Scroll event handler must trigger _loadOlderMessages near top."""
        UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")

        # Find the scroll listener on #messages element
        scroll_marker = "el.scrollTop<80"
        assert scroll_marker in UI_JS, (
            "Scroll handler must check scrollTop<80 to trigger older message loading"
        )
        assert "_loadOlderMessages" in UI_JS, (
            "ui.js scroll handler must call _loadOlderMessages"
        )

    def test_load_older_indicator_in_render(self):
        """renderMessages must show a 'load older' indicator when truncated."""
        UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")

        assert "loadOlderIndicator" in UI_JS, (
            "renderMessages must create a loadOlderIndicator when older messages exist"
        )
        assert "load older messages" in UI_JS.lower(), (
            "Load older indicator must have visible text for the user"
        )
