import importlib.util
import json
import sqlite3
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "repair_workspace_user_turns.py"
)
spec = importlib.util.spec_from_file_location("repair_workspace_user_turns", SCRIPT)
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


def test_clean_message_list_strips_workspace_prefix_and_dedupes_adjacent_user_turns():
    cleaned, stats = repair.clean_message_list(
        [
            {"role": "user", "content": "Ok, mache weiter"},
            {"role": "user", "content": "[Workspace: /tmp/project]\nOk, mache weiter"},
            {"role": "assistant", "content": "continuing"},
            {"role": "user", "content": "[Workspace: /tmp/project]\nNext"},
        ]
    )

    assert stats == {
        "stripped_workspace_prefixes": 2,
        "removed_adjacent_user_duplicates": 1,
    }
    assert [m["role"] for m in cleaned] == ["user", "assistant", "user"]
    assert [m["content"] for m in cleaned] == ["Ok, mache weiter", "continuing", "Next"]


def test_repair_sidecars_writes_backup_and_updates_message_count(tmp_path):
    sessions_dir = tmp_path / "sessions"
    backup_dir = tmp_path / "backup"
    sessions_dir.mkdir()
    sidecar = sessions_dir / "abc.json"
    sidecar.write_text(
        json.dumps(
            {
                "session_id": "abc",
                "message_count": 3,
                "messages": [
                    {"role": "user", "content": "ping"},
                    {"role": "user", "content": "[Workspace: /tmp]\nping"},
                    {"role": "assistant", "content": "pong"},
                ],
            }
        ),
        encoding="utf-8",
    )

    report = repair.repair_sidecars(sessions_dir, backup_dir=backup_dir, dry_run=False)

    assert report["changed_sidecars"][0]["removed_adjacent_user_duplicates"] == 1
    updated = json.loads(sidecar.read_text(encoding="utf-8"))
    assert updated["message_count"] == 2
    assert [m["content"] for m in updated["messages"]] == ["ping", "pong"]
    assert (backup_dir / "abc.json").exists()


def test_repair_state_db_strips_prefixes_deletes_duplicates_and_updates_counts(
    tmp_path,
):
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript("""
        create table sessions (
            id text primary key,
            message_count integer default 0,
            tool_call_count integer default 0
        );
        create table messages (
            id integer primary key autoincrement,
            session_id text not null,
            role text not null,
            content text,
            tool_name text
        );
    """)
    con.execute(
        "insert into sessions(id, message_count, tool_call_count) values ('s1', 4, 1)"
    )
    con.executemany(
        "insert into messages(session_id, role, content, tool_name) values (?, ?, ?, ?)",
        [
            ("s1", "user", "hello", None),
            ("s1", "user", "[Workspace: /tmp]\nhello", None),
            ("s1", "assistant", "hi", None),
            ("s1", "tool", "{}", "read_file"),
        ],
    )
    con.commit()
    con.close()

    report = repair.repair_state_db(db, backup_dir=tmp_path / "backup", dry_run=False)

    assert report["updated_workspace_prefix_user_messages"] == 1
    assert report["removed_adjacent_user_duplicates"] == 1
    con = sqlite3.connect(db)
    assert con.execute(
        "select message_count, tool_call_count from sessions where id = 's1'"
    ).fetchone() == (3, 1)
    assert con.execute("select role, content from messages order by id").fetchall() == [
        ("user", "hello"),
        ("assistant", "hi"),
        ("tool", "{}"),
    ]
    con.close()
