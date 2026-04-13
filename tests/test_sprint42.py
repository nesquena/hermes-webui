"""
Sprint 42 Tests: SessionDB injection into AIAgent for WebUI sessions (PR #356).

Covers:
- streaming.py: SessionDB is initialized inside _run_agent_streaming (import present)
- streaming.py: try/except guards SessionDB init so failures are non-fatal
- streaming.py: session_db= kwarg is passed to AIAgent constructor
- streaming.py: SessionDB init failure prints a WARNING (not silently swallowed)
- streaming.py: SessionDB init is placed before AIAgent construction
"""
import ast
import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).parent.parent
STREAMING_PY = (REPO_ROOT / "api" / "streaming.py").read_text()


class TestSessionDBInjection(unittest.TestCase):
    """Verify SessionDB is initialized and passed to AIAgent in streaming.py."""

    def test_hermes_state_import_present(self):
        """SessionDB must be imported from hermes_state inside _run_agent_streaming."""
        self.assertIn(
            "from hermes_state import SessionDB",
            STREAMING_PY,
            "SessionDB import missing from streaming.py (PR #356)",
        )

    def test_session_db_kwarg_passed_to_agent(self):
        """session_db= must be passed to the AIAgent constructor call."""
        self.assertIn(
            "session_db=_session_db",
            STREAMING_PY,
            "session_db kwarg not passed to AIAgent (PR #356)",
        )

    def test_sessiondb_init_in_try_except(self):
        """SessionDB() init must be wrapped in try/except for non-fatal failure handling."""
        # Check that the try/except pattern surrounding SessionDB() is present
        pattern = r"try:\s*\n\s*from hermes_state import SessionDB\s*\n\s*_session_db\s*=\s*SessionDB\(\)"
        self.assertRegex(
            STREAMING_PY,
            pattern,
            "SessionDB() init must be inside a try block for non-fatal error handling (PR #356)",
        )

    def test_sessiondb_failure_logs_warning(self):
        """A failure initializing SessionDB must print a WARNING (not silently drop the error)."""
        self.assertIn(
            "WARNING: SessionDB init failed",
            STREAMING_PY,
            "SessionDB init failure must log a WARNING message (PR #356)",
        )

    def test_session_db_initialized_before_agent_construction(self):
        """SessionDB initialization must appear before the AIAgent(...) constructor call."""
        db_pos = STREAMING_PY.find("from hermes_state import SessionDB")
        agent_pos = STREAMING_PY.find("session_db=_session_db")
        self.assertGreater(
            agent_pos,
            db_pos,
            "SessionDB init must appear before AIAgent construction (PR #356)",
        )

    def test_session_db_default_is_none(self):
        """_session_db must be initialized to None before the try block (safe default)."""
        # Pattern: _session_db = None followed (eventually) by the try/SessionDB block
        pattern = r"_session_db\s*=\s*None\s*\n\s*try:"
        self.assertRegex(
            STREAMING_PY,
            pattern,
            "_session_db must default to None before try/except block (PR #356)",
        )


class TestSessionDBAST(unittest.TestCase):
    """AST-level checks: verify the try/except is not inside _ENV_LOCK (deadlock guard)."""

    def setUp(self):
        self.tree = ast.parse(STREAMING_PY)

    def test_sessiondb_try_not_inside_env_lock(self):
        """The try block that wraps SessionDB init must NOT be inside a 'with _ENV_LOCK:' block.

        Putting a try/except inside _ENV_LOCK is the deadlock pattern caught by test_sprint34.
        The SessionDB try/except is outside the lock scope, which is correct.
        """
        # Find all 'with _ENV_LOCK:' nodes; check none of their bodies contain
        # a Try node that also contains 'from hermes_state import SessionDB'
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.With):
                continue
            names = [getattr(item.context_expr, "id", "") for item in node.items]
            if "_ENV_LOCK" not in names:
                continue
            # Walk the with-body for Try nodes
            for stmt in node.body:
                if isinstance(stmt, ast.Try):
                    # Check if this try imports hermes_state
                    src = ast.unparse(stmt)
                    self.assertNotIn(
                        "hermes_state",
                        src,
                        "SessionDB try/except must NOT be inside _ENV_LOCK body (deadlock risk)",
                    )
