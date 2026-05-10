"""
Tests for #634: CLI sessions not visible when setting is enabled.

Root cause: get_cli_sessions() swallowed all errors silently (bare except → return []).
Users with older hermes-agent versions (missing 'source' column in state.db) got
an empty list with no log output, making diagnosis impossible.

Fixes:
1. Schema introspection: check for 'source' column before querying, log a warning
   if missing and return early.
2. Exception path: log a warning instead of silently returning [].
"""

import pathlib

MODELS_PY = pathlib.Path(__file__).parent.parent / "api" / "models.py"
AGENT_SESSIONS_PY = pathlib.Path(__file__).parent.parent / "api" / "agent_sessions.py"
src = MODELS_PY.read_text(encoding="utf-8")
agent_src = AGENT_SESSIONS_PY.read_text(encoding="utf-8")
combined_src = src + "\n" + agent_src


class TestCliSessionsErrorSurface:
    """get_cli_sessions() must log warnings instead of silently returning []."""

    def test_schema_introspection_present(self):
        """The function must check for the 'source' column before querying."""
        assert "PRAGMA table_info(sessions)" in combined_src

    def test_missing_source_column_logs_warning(self):
        """If 'source' column is absent, a warning is logged."""
        # The warning message must mention the missing column and how to fix it
        assert (
            "no 'source' column" in combined_src
            or "has no 'source' column" in combined_src
        )

    def test_missing_source_column_suggests_upgrade(self):
        """Warning message must suggest upgrading hermes-agent."""
        assert (
            "Upgrade hermes-agent" in combined_src
            or "upgrade hermes-agent" in combined_src.lower()
        )

    def test_exception_path_logs_warning(self):
        """The except clause must call logger.warning, not silently pass."""
        # Find the exception handler in get_cli_sessions
        func_start = src.find("def get_cli_sessions()")
        func_end = src.find("\ndef ", func_start + 1)
        func_body = src[func_start:func_end] if func_end != -1 else src[func_start:]
        assert "warning(" in func_body, (
            "get_cli_sessions() exception handler must call logging.warning()"
        )

    def test_exception_path_includes_db_path(self):
        """The warning must include the db_path for diagnosability."""
        func_start = src.find("def get_cli_sessions()")
        func_end = src.find("\ndef ", func_start + 1)
        func_body = src[func_start:func_end] if func_end != -1 else src[func_start:]
        # db_path should appear in the warning call
        warning_pos = func_body.find("warning(")
        warning_block = func_body[warning_pos : warning_pos + 300]
        assert "db_path" in warning_block, (
            "Warning must include db_path so admins can find the problematic database"
        )

    def test_still_returns_empty_on_error(self):
        """Function must still return [] after logging (graceful degradation)."""
        # After the warning, it should return cli_sessions (the empty list) not raise
        func_start = src.find("def get_cli_sessions()")
        func_end = src.find("\ndef ", func_start + 1)
        func_body = src[func_start:func_end] if func_end != -1 else src[func_start:]
        # Must have a 'return' after the warning call
        warning_pos = func_body.find("_cli_err:")
        after_warning = func_body[warning_pos : warning_pos + 400]
        assert "return" in after_warning, (
            "Function must return after the warning (not raise)"
        )

    def test_source_column_check_before_sql_query(self):
        """Schema check must happen before the main SQL SELECT."""
        pragma_pos = agent_src.find("PRAGMA table_info(sessions)")
        select_pos = agent_src.find("SELECT s.id, s.title, s.model")
        assert pragma_pos != -1, "PRAGMA check not found"
        assert select_pos != -1, "SELECT query not found"
        assert pragma_pos < select_pos, (
            "Schema introspection must run before the main SQL query"
        )
