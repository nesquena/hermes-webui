import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from api.session_discoverability import repair_session_discoverability


def _write_sidecar(session_dir: Path, sid: str, *, messages=1, **metadata):
    payload = {
        "session_id": sid,
        "id": sid,
        "title": metadata.pop("title", sid),
        "messages": [{"role": "user", "content": f"message {i}"} for i in range(messages)],
        **metadata,
    }
    path = session_dir / f"{sid}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_index(session_dir: Path, *entries):
    (session_dir / "_index.json").write_text(json.dumps(list(entries)), encoding="utf-8")


def _state_db(session_dir: Path, rows, message_counts=None):
    db = session_dir / "state.db"
    message_counts = message_counts or {}
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            create table sessions (
                id text primary key,
                source text,
                title text,
                parent_session_id text,
                message_count integer,
                started_at real,
                model text,
                workspace text
            )
            """
        )
        conn.execute("create table messages (id integer primary key, session_id text, role text, content text, timestamp real)")
        for row in rows:
            conn.execute(
                """
                insert into sessions (id, source, title, parent_session_id, message_count, started_at, model, workspace)
                values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row.get("source"),
                    row.get("title") or row["id"],
                    row.get("parent_session_id"),
                    row.get("message_count", message_counts.get(row["id"], 0)),
                    row.get("started_at", 10.0),
                    row.get("model", "gpt-test"),
                    row.get("workspace", "/tmp/workspace"),
                ),
            )
            for i in range(message_counts.get(row["id"], 0)):
                conn.execute(
                    "insert into messages (session_id, role, content, timestamp) values (?, 'user', ?, ?)",
                    (row["id"], f"message {i}", 10.0 + i),
                )
    return db


def test_repair_discoverability_dry_run_plans_without_mutating_files(tmp_path):
    stale = "webui-stale-cli-flag"
    missing = "state-only-messageful"
    _write_sidecar(tmp_path, stale, messages=3, source_tag="webui", session_source="webui", is_cli_session=True)
    _write_index(tmp_path, {"session_id": stale, "message_count": 3, "source_tag": "webui", "session_source": "webui", "is_cli_session": True})
    db = _state_db(
        tmp_path,
        [
            {"id": stale, "source": "webui", "message_count": 3},
            {"id": missing, "source": "webui", "message_count": 2},
        ],
        {stale: 3, missing: 2},
    )

    result = repair_session_discoverability(tmp_path, state_db_path=db, dry_run=True, backup_dir=tmp_path / "backup")

    assert result["dry_run"] is True
    assert result["applied"] == []
    assert {action["action"] for action in result["planned"]} == {
        "clear_sidecar_cli_flag",
        "clear_index_cli_flag",
        "materialize_sidecar_from_state_db",
    }
    assert json.loads((tmp_path / f"{stale}.json").read_text())["is_cli_session"] is True
    assert json.loads((tmp_path / "_index.json").read_text())[0]["is_cli_session"] is True
    assert not (tmp_path / f"{missing}.json").exists()
    assert not (tmp_path / "backup").exists()


def test_repair_discoverability_apply_requires_backup_dir(tmp_path):
    sid = "webui-stale-cli-flag"
    _write_sidecar(tmp_path, sid, messages=1, source_tag="webui", session_source="webui", is_cli_session=True)
    _write_index(tmp_path, {"session_id": sid, "message_count": 1, "source_tag": "webui", "session_source": "webui", "is_cli_session": True})
    db = _state_db(tmp_path, [{"id": sid, "source": "webui", "message_count": 1}], {sid: 1})

    result = repair_session_discoverability(tmp_path, state_db_path=db, dry_run=False)

    assert result["ok"] is False
    assert result["error"] == "backup_dir_required_for_apply"
    assert json.loads((tmp_path / f"{sid}.json").read_text())["is_cli_session"] is True


def test_repair_discoverability_apply_backs_up_and_repairs_safe_findings(tmp_path):
    stale = "webui-stale-cli-flag"
    missing = "state-only-messageful"
    _write_sidecar(tmp_path, stale, messages=3, source_tag="webui", session_source="webui", is_cli_session=True)
    _write_index(tmp_path, {"session_id": stale, "message_count": 3, "source_tag": "webui", "session_source": "webui", "is_cli_session": True})
    db = _state_db(
        tmp_path,
        [
            {"id": stale, "source": "webui", "message_count": 3},
            {"id": missing, "source": "webui", "message_count": 2, "title": "Recovered From State"},
        ],
        {stale: 3, missing: 2},
    )

    result = repair_session_discoverability(tmp_path, state_db_path=db, dry_run=False, backup_dir=tmp_path / "backup")

    assert result["ok"] is True
    assert result["dry_run"] is False
    assert {action["action"] for action in result["applied"]} == {
        "clear_sidecar_cli_flag",
        "clear_index_cli_flag",
        "materialize_sidecar_from_state_db",
    }
    assert json.loads((tmp_path / f"{stale}.json").read_text())["is_cli_session"] is False
    assert json.loads((tmp_path / "_index.json").read_text())[0]["is_cli_session"] is False
    index_rows = json.loads((tmp_path / "_index.json").read_text())
    assert {row["session_id"] for row in index_rows} == {stale, missing}
    recovered = json.loads((tmp_path / f"{missing}.json").read_text())
    assert recovered["title"] == "Recovered From State"
    assert recovered["message_count"] == 2
    assert len(recovered["messages"]) == 2
    backed_up = {p.name for p in (tmp_path / "backup").iterdir()}
    assert f"{stale}.json" in backed_up
    assert "_index.json" in backed_up
    assert "state.db" in backed_up


def test_repair_discoverability_cli_defaults_to_dry_run(tmp_path):
    sid = "webui-stale-cli-flag"
    _write_sidecar(tmp_path, sid, messages=1, source_tag="webui", session_source="webui", is_cli_session=True)
    _write_index(tmp_path, {"session_id": sid, "message_count": 1, "source_tag": "webui", "session_source": "webui", "is_cli_session": True})
    db = _state_db(tmp_path, [{"id": sid, "source": "webui", "message_count": 1}], {sid: 1})

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "api.session_discoverability",
            "--repair-safe",
            "--session-dir",
            str(tmp_path),
            "--state-db",
            str(db),
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    result = json.loads(completed.stdout)
    assert result["dry_run"] is True
    assert [action["action"] for action in result["planned"]] == ["clear_sidecar_cli_flag", "clear_index_cli_flag"]
    assert json.loads((tmp_path / f"{sid}.json").read_text())["is_cli_session"] is True
