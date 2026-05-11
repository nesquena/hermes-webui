import json
import sqlite3

from api.session_recovery import recover_missing_sidecars_from_state_db, audit_session_recovery


def _make_state_db(path, *, sid="state_only_001", source="webui", messages=2):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, model TEXT, started_at REAL, message_count INTEGER, parent_session_id TEXT)"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, timestamp REAL)"
    )
    conn.execute(
        "INSERT INTO sessions (id, source, title, model, started_at, message_count, parent_session_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sid, source, "Recovered from DB", "openai/gpt-5", 1234.0, messages, "parent-1"),
    )
    for i in range(messages):
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (sid, "user" if i % 2 == 0 else "assistant", f"message {i + 1}", 1234.0 + i),
        )
    conn.commit()
    conn.close()
    return sid


def test_recover_missing_sidecars_from_state_db_materializes_webui_row(tmp_path):
    sid = _make_state_db(tmp_path / "state.db")

    result = recover_missing_sidecars_from_state_db(tmp_path, tmp_path / "state.db")

    assert result["materialized"] == 1
    sidecar = tmp_path / f"{sid}.json"
    assert sidecar.exists()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["session_id"] == sid
    assert data["title"] == "Recovered from DB"
    assert data["model"] == "openai/gpt-5"
    assert data["parent_session_id"] == "parent-1"
    assert data["source_tag"] == "webui"
    assert data["session_source"] == "webui"
    assert [m["content"] for m in data["messages"]] == ["message 1", "message 2"]


def test_recover_missing_sidecars_from_state_db_skips_existing_sidecar(tmp_path):
    sid = _make_state_db(tmp_path / "state.db")
    existing = tmp_path / f"{sid}.json"
    existing.write_text(json.dumps({"session_id": sid, "messages": [{"role": "user", "content": "keep"}]}), encoding="utf-8")

    result = recover_missing_sidecars_from_state_db(tmp_path, tmp_path / "state.db")

    assert result["materialized"] == 0
    assert json.loads(existing.read_text(encoding="utf-8"))["messages"][0]["content"] == "keep"


def test_audit_reports_state_db_row_missing_sidecar(tmp_path):
    sid = _make_state_db(tmp_path / "state.db")

    report = audit_session_recovery(tmp_path, state_db_path=tmp_path / "state.db")

    assert any(
        item["session_id"] == sid
        and item["kind"] == "state_db_missing_sidecar"
        and item["category"] == "repairable"
        and item["recommendation"] == "materialize_from_state_db"
        for item in report["items"]
    )
