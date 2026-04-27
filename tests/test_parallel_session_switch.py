"""Tests for session-switch parallelization (PR: perf/parallelize-session-switch).

Three optimizations to reduce session-switch latency:

1. loadDir expanded-dir pre-fetch uses Promise.all (workspace.js)
2. loadSession idle path overlaps loadDir with highlightCode (sessions.js)
3. git_info_for_workspace runs git subprocesses in parallel (workspace.py)
"""

import pathlib
import threading
import time
from unittest.mock import patch

REPO = pathlib.Path(__file__).parent.parent
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
WORKSPACE_JS = (REPO / "static" / "workspace.js").read_text(encoding="utf-8")


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
