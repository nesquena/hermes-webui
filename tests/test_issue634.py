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
import re

MODELS_PY = pathlib.Path(__file__).parent.parent / 'api' / 'models.py'
AGENT_SESSIONS_PY = pathlib.Path(__file__).parent.parent / 'api' / 'agent_sessions.py'
src = MODELS_PY.read_text(encoding='utf-8')
agent_src = AGENT_SESSIONS_PY.read_text(encoding='utf-8')
combined_src = src + "\n" + agent_src


def _get_cli_sessions_source() -> str:
    match = re.search(r"^def get_cli_sessions\(", src, re.M)
    assert match is not None, "get_cli_sessions() definition not found"
    func_start = match.start()
    func_end = src.find("\ndef ", func_start + 1)
    return src[func_start:func_end] if func_end != -1 else src[func_start:]


class TestCliSessionsErrorSurface:
    """get_cli_sessions() must log warnings instead of silently returning []."""

    def test_schema_introspection_present(self):
        """The function must check for the 'source' column before querying."""
        assert "PRAGMA table_info(sessions)" in combined_src

    def test_missing_source_column_logs_warning(self):
        """If 'source' column is absent, a warning is logged."""
        # The warning message must mention the missing column and how to fix it
        assert "no 'source' column" in combined_src or "has no 'source' column" in combined_src

    def test_missing_source_column_suggests_upgrade(self):
        """Warning message must suggest upgrading hermes-agent."""
        assert "Upgrade hermes-agent" in combined_src or "upgrade hermes-agent" in combined_src.lower()

    def test_exception_path_logs_warning(self):
        """The except clause must call logger.warning, not silently pass."""
        func_body = _get_cli_sessions_source()
        assert "warning(" in func_body, \
            "get_cli_sessions() exception handler must call logging.warning()"

    def test_exception_path_includes_db_path(self):
        """The warning must include the db_path for diagnosability."""
        func_body = _get_cli_sessions_source()
        # db_path should appear in the warning call
        warning_pos = func_body.find("warning(")
        warning_block = func_body[warning_pos:warning_pos + 300]
        assert "db_path" in warning_block, \
            "Warning must include db_path so admins can find the problematic database"

    def test_still_returns_empty_on_error(self):
        """Function must still return [] after logging (graceful degradation)."""
        # After the warning, it should return cli_sessions (the empty list) not raise
        func_body = _get_cli_sessions_source()
        # Must have a 'return' after the warning call
        warning_pos = func_body.find("_cli_err:")
        after_warning = func_body[warning_pos:warning_pos + 400]
        assert "return" in after_warning, \
            "Function must return after the warning (not raise)"

    def test_source_column_check_before_sql_query(self):
        """Schema check must happen before the main SQL SELECT."""
        pragma_pos = agent_src.find("PRAGMA table_info(sessions)")
        select_pos = agent_src.find("SELECT s.id, s.title, s.model")
        assert pragma_pos != -1, "PRAGMA check not found"
        assert select_pos != -1, "SELECT query not found"
        assert pragma_pos < select_pos, \
            "Schema introspection must run before the main SQL query"


# ---------------------------------------------------------------------------
# Follow-up: the warning must fire once per state.db, not once per poll.
#
# get_cli_sessions(all_profiles=True) calls read_importable_agent_session_rows()
# for every profile on every sidebar poll (behind a 5 s cache). On an install
# with one pre-``source`` profile DB that produced one identical WARNING line
# every ~15 s in errors.log. The message is correct; its repetition is not.
# ---------------------------------------------------------------------------
import logging
import sqlite3

import pytest

from api import agent_sessions


def _make_pre_source_state_db(path):
    """Build a state.db whose ``sessions`` table predates the ``source`` column."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, model TEXT, "
        "message_count INTEGER, started_at REAL)"
    )
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, timestamp REAL)")
    conn.commit()
    conn.close()


def _source_column_warnings(caplog):
    return [
        rec for rec in caplog.records
        if rec.levelno == logging.WARNING and "has no 'source' column" in rec.getMessage()
    ]


class TestMissingSourceColumnWarnsOncePerDb:
    @pytest.fixture(autouse=True)
    def _fresh_dedupe_state(self, monkeypatch):
        # The dedupe set is process-lifetime state; give each test its own.
        monkeypatch.setattr(agent_sessions, "_SOURCE_COLUMN_WARNED_DB_PATHS", set(), raising=False)

    def test_same_db_path_twice_logs_once(self, tmp_path, caplog):
        db = tmp_path / "state.db"
        _make_pre_source_state_db(db)
        with caplog.at_level(logging.WARNING, logger="api.agent_sessions"):
            first = agent_sessions.read_importable_agent_session_rows(db, exclude_sources=None)
            second = agent_sessions.read_importable_agent_session_rows(db, exclude_sources=None)
        assert first == [] and second == []
        warnings = _source_column_warnings(caplog)
        assert len(warnings) == 1, [w.getMessage() for w in warnings]
        # Message text is unchanged: still names the path and the upgrade hint.
        assert str(db) in warnings[0].getMessage()
        assert "Upgrade hermes-agent" in warnings[0].getMessage()

    def test_different_db_path_logs_again(self, tmp_path, caplog):
        db_a = tmp_path / "planner" / "state.db"
        db_b = tmp_path / "coder" / "state.db"
        db_a.parent.mkdir()
        db_b.parent.mkdir()
        _make_pre_source_state_db(db_a)
        _make_pre_source_state_db(db_b)
        with caplog.at_level(logging.WARNING, logger="api.agent_sessions"):
            agent_sessions.read_importable_agent_session_rows(db_a, exclude_sources=None)
            agent_sessions.read_importable_agent_session_rows(db_b, exclude_sources=None)
            agent_sessions.read_importable_agent_session_rows(db_a, exclude_sources=None)
        warnings = _source_column_warnings(caplog)
        assert len(warnings) == 2, [w.getMessage() for w in warnings]
        assert str(db_a) in warnings[0].getMessage()
        assert str(db_b) in warnings[1].getMessage()
