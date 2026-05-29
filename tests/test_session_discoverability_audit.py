import json
import sqlite3
from pathlib import Path

from api.session_discoverability import audit_session_discoverability


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
                message_count integer
            )
            """
        )
        conn.execute("create table messages (session_id text, role text, content text)")
        for row in rows:
            conn.execute(
                "insert into sessions (id, source, title, parent_session_id, message_count) values (?, ?, ?, ?, ?)",
                (
                    row["id"],
                    row.get("source"),
                    row.get("title"),
                    row.get("parent_session_id"),
                    row.get("message_count", message_counts.get(row["id"], 0)),
                ),
            )
            for i in range(message_counts.get(row["id"], 0)):
                conn.execute(
                    "insert into messages (session_id, role, content) values (?, 'user', ?)",
                    (row["id"], f"message {i}"),
                )
    return db


def test_audit_reports_messageful_sidecar_missing_from_api_visible_sessions(tmp_path):
    sid = "20260526_234828_b8393a"
    _write_sidecar(tmp_path, sid, messages=12, source_tag="webui", session_source="webui")
    _write_index(tmp_path, {"session_id": sid, "message_count": 12, "source_tag": "webui"})
    db = _state_db(tmp_path, [{"id": sid, "source": "webui", "message_count": 12}], {sid: 12})

    report = audit_session_discoverability(tmp_path, state_db_path=db, api_sessions=[])

    assert report["status"] == "warn"
    item = report["items"][0]
    assert item["kind"] == "api_missing_messageful"
    assert item["category"] == "warning"
    assert item["session_id"] == sid
    assert item["message_count"] == 12
    assert item["present_in"] == {"sidecar": True, "index": True, "state_db": True, "api": False}


def test_audit_reports_stale_cli_flag_on_webui_session(tmp_path):
    sid = "webui-marked-cli"
    _write_sidecar(tmp_path, sid, messages=3, source_tag="webui", session_source="webui", is_cli_session=True)
    _write_index(tmp_path, {"session_id": sid, "message_count": 3, "source_tag": "webui", "is_cli_session": True})
    db = _state_db(tmp_path, [{"id": sid, "source": "webui", "message_count": 3}], {sid: 3})

    report = audit_session_discoverability(
        tmp_path,
        state_db_path=db,
        api_sessions=[{"session_id": sid, "title": "WebUI Project Work", "message_count": 3, "source_tag": "webui", "session_source": "webui", "is_cli_session": True}],
    )

    kinds = {item["kind"] for item in report["items"]}
    assert "persisted_source_flag_stale" in kinds
    assert "source_misclassified" not in kinds
    item = next(item for item in report["items"] if item["kind"] == "persisted_source_flag_stale")
    assert item["session_id"] == sid
    assert item["state_source"] == "webui"
    assert item["api_is_cli_session"] is True
    assert item["api_computed_is_cli_session"] is False
    assert item["index_is_cli_session"] is True
    assert item["sidecar_is_cli_session"] is True


def test_stale_flag_hidden_snapshot_reports_visible_api_representative(tmp_path):
    root = "webui-hidden-stale-cli-root"
    tip = "webui-visible-lineage-tip"
    _write_sidecar(
        tmp_path,
        root,
        messages=12,
        source_tag="webui",
        session_source="webui",
        is_cli_session=True,
        pre_compression_snapshot=True,
    )
    _write_index(
        tmp_path,
        {
            "session_id": root,
            "message_count": 12,
            "source_tag": "webui",
            "session_source": "webui",
            "is_cli_session": True,
            "pre_compression_snapshot": True,
        },
    )
    db = _state_db(
        tmp_path,
        [
            {"id": root, "source": "webui", "message_count": 12},
            {"id": tip, "source": "webui", "parent_session_id": root, "message_count": 14},
        ],
        {root: 12, tip: 14},
    )

    report = audit_session_discoverability(
        tmp_path,
        state_db_path=db,
        api_sessions=[{"session_id": tip, "message_count": 14, "parent_session_id": root, "_lineage_root_id": root}],
    )

    item = next(item for item in report["items"] if item["session_id"] == root)
    assert item["kind"] == "persisted_source_flag_stale"
    assert item["present_in"] == {"sidecar": True, "index": True, "state_db": True, "api": False}
    assert item["represented_by_api_lineage"] is True
    assert item["api_representative_session_id"] == tip


def test_audit_classifies_state_db_only_messageful_rows_as_missing_sidecar(tmp_path):
    sid = "state-only-session"
    db = _state_db(tmp_path, [{"id": sid, "source": "webui", "message_count": 5}], {sid: 5})

    report = audit_session_discoverability(tmp_path, state_db_path=db, api_sessions=[])

    assert report["items"][0]["kind"] == "state_db_messageful_missing_sidecar"
    assert report["items"][0]["recommendation"] == "materialize_sidecar_or_archive_state_row"
    assert report["items"][0]["present_in"] == {"sidecar": False, "index": False, "state_db": True, "api": False}


def test_audit_treats_hidden_snapshot_as_represented_when_visible_lineage_tip_exists(tmp_path):
    root = "root-session"
    child = "child-session"
    _write_sidecar(
        tmp_path,
        root,
        messages=8,
        source_tag="webui",
        session_source="webui",
        pre_compression_snapshot=True,
    )
    _write_sidecar(
        tmp_path,
        child,
        messages=2,
        source_tag="webui",
        session_source="webui",
        parent_session_id=root,
    )
    _write_index(tmp_path, {"session_id": root, "message_count": 8}, {"session_id": child, "message_count": 2})
    db = _state_db(
        tmp_path,
        [
            {"id": root, "source": "webui", "message_count": 8},
            {"id": child, "source": "webui", "parent_session_id": root, "message_count": 2},
        ],
        {root: 8, child: 2},
    )

    report = audit_session_discoverability(
        tmp_path,
        state_db_path=db,
        api_sessions=[{"session_id": child, "message_count": 2, "parent_session_id": root}],
    )

    assert report["status"] == "ok"
    assert report["items"] == []


def test_audit_treats_siblings_as_represented_when_visible_tip_points_at_lineage_root(tmp_path):
    root = "root-session"
    hidden_sibling = "hidden-sibling"
    visible_tip = "visible-tip"
    _write_sidecar(
        tmp_path,
        root,
        messages=8,
        source_tag="webui",
        session_source="webui",
        pre_compression_snapshot=True,
    )
    _write_sidecar(
        tmp_path,
        hidden_sibling,
        messages=5,
        source_tag="webui",
        session_source="webui",
        parent_session_id=root,
    )
    _write_index(
        tmp_path,
        {"session_id": root, "message_count": 8, "pre_compression_snapshot": True},
        {"session_id": hidden_sibling, "message_count": 5, "parent_session_id": root},
    )
    db = _state_db(
        tmp_path,
        [
            {"id": root, "source": "webui", "message_count": 8},
            {"id": hidden_sibling, "source": "webui", "parent_session_id": root, "message_count": 5},
            {"id": visible_tip, "source": "webui", "parent_session_id": root, "message_count": 9},
        ],
        {root: 8, hidden_sibling: 5, visible_tip: 9},
    )

    report = audit_session_discoverability(
        tmp_path,
        state_db_path=db,
        api_sessions=[{"session_id": visible_tip, "message_count": 9, "_lineage_root_id": root}],
    )

    assert report["status"] == "ok"
    assert report["items"] == []


def test_audit_reports_lineage_without_visible_representative(tmp_path):
    root = "root-session"
    _write_sidecar(
        tmp_path,
        root,
        messages=8,
        source_tag="webui",
        session_source="webui",
        pre_compression_snapshot=True,
    )
    _write_index(tmp_path, {"session_id": root, "message_count": 8})
    db = _state_db(tmp_path, [{"id": root, "source": "webui", "message_count": 8}], {root: 8})

    report = audit_session_discoverability(tmp_path, state_db_path=db, api_sessions=[])

    assert report["status"] == "warn"
    assert [item["kind"] for item in report["items"]] == ["lineage_missing_visible_representative"]
    assert report["items"][0]["session_id"] == root
