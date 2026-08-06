import sqlite3

import api.models as models
from api.models import Session, get_state_db_session_messages, reconciled_state_db_messages_for_session


def _make_state_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT,
            content TEXT,
            timestamp REAL,
            active INTEGER
        )
        """
    )
    conn.executemany(
        "INSERT INTO messages (session_id, role, content, timestamp, active) VALUES (?, ?, ?, ?, ?)",
        [
            ("sid", "user", "archived prompt", 1.0, 0),
            ("sid", "assistant", "archived answer", 2.0, 0),
            ("sid", "assistant", "compacted summary", 3.0, 1),
            ("sid", "user", "live follow-up", 4.0, 1),
        ],
    )
    conn.commit()
    conn.close()


def test_state_db_reader_excludes_inactive_compaction_archive_by_default(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _make_state_db(db)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)

    messages = get_state_db_session_messages("sid")

    assert [m["content"] for m in messages] == ["compacted summary", "live follow-up"]


def test_state_db_reader_can_include_inactive_for_explicit_recovery(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _make_state_db(db)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)

    messages = get_state_db_session_messages("sid", include_inactive=True)

    assert [m["content"] for m in messages] == [
        "archived prompt",
        "archived answer",
        "compacted summary",
        "live follow-up",
    ]


def test_reconciled_context_does_not_resurrect_inactive_archive_rows(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _make_state_db(db)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)

    session = Session(
        session_id="sid",
        messages=[{"role": "user", "content": "full display transcript"}],
        context_messages=[{"role": "assistant", "content": "compacted summary", "timestamp": 3.0}],
    )

    state_messages = get_state_db_session_messages("sid")
    reconciled = reconciled_state_db_messages_for_session(
        session,
        prefer_context=True,
        state_messages=state_messages,
    )

    assert [m["content"] for m in reconciled] == ["compacted summary", "live follow-up"]
