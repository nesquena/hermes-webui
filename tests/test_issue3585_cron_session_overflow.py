"""Regression coverage for cron sessions squeezing out Discord/Telegram rows (#3585).

When ``_load_cli_sessions_uncached`` passed ``exclude_sources=None`` to
``read_importable_agent_session_rows``, the 20-row window filled with cron
entries, hiding Discord/Telegram sessions entirely.  The fix narrows the
exclusion to ``("cron",)`` so cron rows stay out of the main pass (the cron
second-pass recovers them independently) while ``source='webui'`` rows remain
visible for sidecar-less recovery.
"""

import pathlib
import sqlite3
import time
from unittest import mock

import api.models as models

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _make_state_db(path, *, cron_count=15, discord_count=5):
    """Create a state.db with cron and discord sessions.

    Cron sessions are created with newer timestamps so they dominate the
    ORDER BY window when ``exclude_sources`` is not applied.
    """
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            session_source TEXT,
            title TEXT,
            model TEXT,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0,
            parent_session_id TEXT,
            ended_at REAL,
            end_reason TEXT
        );
        CREATE INDEX idx_sessions_started ON sessions(started_at);
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL
        );
        CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
        """
    )
    # Cron sessions: newest timestamps (highest integers) to dominate window
    now = time.time()
    for i in range(cron_count):
        sid = f"cron_job_{i:04d}_{int(now) + i}"
        started = now + i  # newer than discord sessions
        conn.execute(
            """
            INSERT INTO sessions
            (id, source, session_source, title, model, started_at, message_count,
             parent_session_id, ended_at, end_reason)
            VALUES (?, 'cron', 'cron', ?, 'deepseek/deepseek-chat', ?, 1, NULL, NULL, NULL)
            """,
            (sid, f"Cron job {i}", started),
        )
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp)"
            " VALUES (?, ?, 'user', 'cron task', ?)",
            (f"cron_msg_{i:04d}", sid, started),
        )

    # Discord sessions: older timestamps so they lose to cron in raw ORDER BY
    for i in range(discord_count):
        sid = f"discord_{i:04d}"
        started = now - 100 + i  # older than cron sessions
        conn.execute(
            """
            INSERT INTO sessions
            (id, source, session_source, title, model, started_at, message_count,
             parent_session_id, ended_at, end_reason)
            VALUES (?, 'discord', 'discord', ?, 'deepseek/deepseek-chat', ?, 1, NULL, NULL, NULL)
            """,
            (sid, f"Discord chat {i}", started),
        )
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp)"
            " VALUES (?, ?, 'user', 'hello from discord', ?)",
            (f"discord_msg_{i:04d}", sid, started),
        )

    conn.commit()
    conn.close()


def test_discord_sessions_not_squeezed_out_by_cron(tmp_path):
    """Discord sessions must appear in the main pass even when cron rows are newer."""
    db = tmp_path / "state.db"
    _make_state_db(db, cron_count=15, discord_count=5)

    with (
        mock.patch("api.models.get_claude_code_sessions", return_value=[]),
        mock.patch("api.models.get_last_workspace", return_value=tmp_path),
        mock.patch("api.models.ensure_cron_project", return_value="cron-project-id"),
        mock.patch("api.models.Session.load_metadata_only", return_value=None),
    ):
        result = models._load_cli_sessions_uncached(tmp_path, db, _cli_profile=None)

    source_tags = {s["source_tag"] for s in result}
    session_ids = {s["session_id"] for s in result}

    discord_ids = {f"discord_{i:04d}" for i in range(5)}
    assert discord_ids <= session_ids, (
        "Discord sessions were squeezed out of the sidebar by cron entries. "
        f"Missing: {discord_ids - session_ids}"
    )
    assert "discord" in source_tags


def test_cron_sessions_recovered_by_second_pass(tmp_path):
    """Cron sessions must still appear via the second pass after the main-pass exclusion."""
    db = tmp_path / "state.db"
    _make_state_db(db, cron_count=15, discord_count=5)

    with (
        mock.patch(
            "api.models.read_importable_agent_session_rows",
            wraps=models.read_importable_agent_session_rows,
        ) as read_rows,
        mock.patch("api.models.get_claude_code_sessions", return_value=[]),
        mock.patch("api.models.get_last_workspace", return_value=tmp_path),
        mock.patch("api.models.ensure_cron_project", return_value="cron-project-id"),
        mock.patch("api.models.Session.load_metadata_only", return_value=None),
    ):
        result = models._load_cli_sessions_uncached(tmp_path, db, _cli_profile=None)

    cron_sessions = [s for s in result if s["source_tag"] == "cron"]
    assert len(cron_sessions) > 0, "Cron sessions should be recovered by the second pass"
    assert len(read_rows.call_args_list) == 2
    assert read_rows.call_args_list[1].kwargs["exclude_sources"] is None
    assert read_rows.call_args_list[1].kwargs["include_sources"] == ("cron",)


def test_webui_sidecarless_sessions_not_excluded(tmp_path):
    """WebUI sessions in state.db without JSON sidecars must remain visible.

    The exclude_sources must only filter cron, not webui. A source='webui'
    row with messages but no JSON sidecar file needs to appear in the sidebar
    so the session_recovery path can materialize the sidecar on demand.
    """
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            session_source TEXT,
            title TEXT,
            model TEXT,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0,
            parent_session_id TEXT,
            ended_at REAL,
            end_reason TEXT
        );
        CREATE INDEX idx_sessions_started ON sessions(started_at);
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL
        );
        CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
        """
    )
    now = time.time()
    conn.execute(
        """
        INSERT INTO sessions
        (id, source, session_source, title, model, started_at, message_count,
         parent_session_id, ended_at, end_reason)
        VALUES ('webui_orphan_001', 'webui', 'webui', 'Lost sidecar session',
                'claude-sonnet-4.6', ?, 3, NULL, NULL, NULL)
        """,
        (now,),
    )
    for i in range(3):
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp)"
            " VALUES (?, 'webui_orphan_001', 'user', 'message', ?)",
            (f"webui_msg_{i}", now + i),
        )
    conn.commit()
    conn.close()

    with (
        mock.patch("api.models.get_claude_code_sessions", return_value=[]),
        mock.patch("api.models.get_last_workspace", return_value=tmp_path),
        mock.patch("api.models.ensure_cron_project", return_value="cron-project-id"),
        mock.patch("api.models.Session.load_metadata_only", return_value=None),
    ):
        result = models._load_cli_sessions_uncached(tmp_path, db, _cli_profile=None)

    webui_ids = {s["session_id"] for s in result if s["source_tag"] == "webui"}
    assert "webui_orphan_001" in webui_ids, (
        "WebUI sidecar-less sessions must not be excluded from the main pass. "
        "The exclude_sources should be ('cron',), not ('cron', 'webui')."
    )
