"""Metadata-only conversation thread tests.

Conversation threads link multiple fresh WebUI sessions into one ordered human
workstream without copying transcripts into the new chat or reusing fork lineage.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

from api.config import SESSION_DIR
from api.models import Session


THREAD_FIELDS = {
    "thread_id": "thread_alpha",
    "thread_root_session_id": "root_session",
    "thread_prev_session_id": "prev_session",
    "thread_sequence": 2,
    "thread_link_type": "manual_continue",
    "thread_linked_at": 1234.5,
}


def _request_json(method, base_url, path, body=None):
    payload = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        base_url + path,
        data=payload,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return exc.code, payload


def _post(base_url, path, body=None):
    return _request_json("POST", base_url, path, body or {})


def _get(base_url, path, params=None):
    if params:
        path = path + "?" + urllib.parse.urlencode(params)
    return _request_json("GET", base_url, path)


def _create_session(cleanup_test_sessions, *, title="Thread source", messages=None, **kwargs):
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    session = Session(
        title=title,
        messages=messages if messages is not None else [
            {"role": "user", "content": "original context"},
            {"role": "assistant", "content": "original answer"},
        ],
        profile="default",
        **kwargs,
    )
    session.save()
    cleanup_test_sessions.append(session.session_id)
    return session


def test_session_compact_includes_thread_metadata():
    session = Session(title="Threaded", messages=[{"role": "user", "content": "hello"}], **THREAD_FIELDS)

    compact = session.compact()

    for key, value in THREAD_FIELDS.items():
        assert compact[key] == value


def test_session_save_load_preserves_thread_metadata(cleanup_test_sessions):
    session = _create_session(
        cleanup_test_sessions,
        title="Persisted threaded session",
        messages=[{"role": "user", "content": "keep me"}],
        **THREAD_FIELDS,
    )

    loaded = Session.load(session.session_id)

    assert loaded is not None
    assert loaded.messages == [{"role": "user", "content": "keep me"}]
    for key, value in THREAD_FIELDS.items():
        assert getattr(loaded, key) == value
    assert loaded.compact()["thread_id"] == THREAD_FIELDS["thread_id"]


def test_create_thread_metadata_object_from_session(cleanup_test_sessions):
    from api import session_threads

    session = _create_session(cleanup_test_sessions, title="Root topic", project_id="project-1")

    thread = session_threads.create_thread_for_session(
        session,
        title="Custom thread title",
        now=1000.0,
        thread_id="thread_custom",
    )

    assert thread["thread_id"] == "thread_custom"
    assert thread["title"] == "Custom thread title"
    assert thread["profile"] == "default"
    assert thread["project_id"] == "project-1"
    assert thread["status"] == "active"
    assert thread["root_session_id"] == session.session_id
    assert thread["latest_session_id"] == session.session_id
    assert session.thread_id == "thread_custom"
    assert session.thread_sequence == 1
    assert session.thread_link_type == "manual_link"


def test_thread_status_validation_rejects_unknown_status():
    from api import session_threads

    for status in ("active", "later", "blocked", "archived"):
        assert session_threads.validate_thread_status(status) == status

    try:
        session_threads.validate_thread_status("someday")
    except ValueError as exc:
        assert "active/later/blocked/archived" in str(exc)
    else:
        raise AssertionError("unknown thread status should raise ValueError")


def test_thread_summary_derives_counts_and_latest_without_messages():
    from api import session_threads

    threads = [{
        "thread_id": "thread_sum",
        "title": "Summary thread",
        "profile": "default",
        "status": "active",
        "root_session_id": "s1",
        "latest_session_id": "stale",
        "created_at": 1,
        "updated_at": 1,
    }]
    rows = [
        {"session_id": "s1", "title": "Part 1", "thread_id": "thread_sum", "thread_sequence": 1, "message_count": 3, "updated_at": 10, "messages": [{"role": "user", "content": "hidden"}]},
        {"session_id": "s2", "title": "Part 2", "thread_id": "thread_sum", "thread_sequence": 2, "message_count": 5, "updated_at": 20, "messages": [{"role": "assistant", "content": "hidden"}]},
    ]

    summaries = session_threads.summarize_threads(threads, rows)

    assert summaries[0]["session_count"] == 2
    assert summaries[0]["message_count"] == 8
    assert summaries[0]["latest_session_id"] == "s2"
    assert summaries[0]["sessions_preview"][0]["session_id"] == "s1"
    assert "messages" not in summaries[0]["sessions_preview"][0]
    assert "messages" not in summaries[0]["sessions_preview"][1]


def test_thread_store_does_not_store_messages():
    from api import session_threads

    session_threads.save_threads([
        {
            "thread_id": "thread_clean",
            "title": "Clean",
            "profile": "default",
            "status": "active",
            "root_session_id": "s1",
            "latest_session_id": "s1",
            "created_at": 1,
            "updated_at": 1,
            "messages": [{"role": "user", "content": "must not persist"}],
            "transcript": "must not persist",
        }
    ])

    loaded = session_threads.load_threads()
    assert loaded[0]["thread_id"] == "thread_clean"
    assert "messages" not in loaded[0]
    assert "transcript" not in loaded[0]
    raw = session_threads.SESSION_THREADS_FILE.read_text(encoding="utf-8")
    assert "must not persist" not in raw


def test_continue_in_linked_chat_creates_empty_session_without_copying_messages(base_url, cleanup_test_sessions):
    source = _create_session(cleanup_test_sessions, title="Source with history")

    status, payload = _post(base_url, "/api/session/new", {"prev_session_id": source.session_id, "link_to_prev": True})

    assert status == 200, payload
    new_session = payload["session"]
    cleanup_test_sessions.append(new_session["session_id"])
    assert new_session["messages"] == []
    assert new_session["message_count"] == 0
    assert new_session["thread_prev_session_id"] == source.session_id
    assert "parent_session_id" not in new_session
    assert new_session.get("session_source") != "fork"

    status, source_payload = _get(base_url, "/api/session", {"session_id": source.session_id})
    assert status == 200, source_payload
    assert len(source_payload["session"]["messages"]) == 2
    assert source_payload["session"]["thread_sequence"] == 1
    assert source_payload["session"]["thread_id"] == new_session["thread_id"]


def test_continue_in_linked_chat_creates_thread_if_source_has_none(base_url, cleanup_test_sessions):
    source = _create_session(cleanup_test_sessions, title="Unthreaded source")

    status, payload = _post(base_url, "/api/session/new", {"prev_session_id": source.session_id, "link_to_prev": True})

    assert status == 200, payload
    new_session = payload["session"]
    cleanup_test_sessions.append(new_session["session_id"])
    assert new_session["thread_id"]
    assert new_session["thread_root_session_id"] == source.session_id
    assert new_session["thread_sequence"] == 2
    assert new_session["thread_link_type"] == "manual_continue"

    status, thread_payload = _get(base_url, "/api/thread", {"thread_id": new_session["thread_id"]})
    assert status == 200, thread_payload
    assert thread_payload["thread"]["root_session_id"] == source.session_id
    assert [row["session_id"] for row in thread_payload["sessions"]] == [source.session_id, new_session["session_id"]]
    assert [row["thread_sequence"] for row in thread_payload["sessions"]] == [1, 2]


def test_continue_in_linked_chat_appends_to_existing_thread(base_url, cleanup_test_sessions):
    source = _create_session(cleanup_test_sessions, title="Thread root")
    status, first_payload = _post(base_url, "/api/session/new", {"prev_session_id": source.session_id, "link_to_prev": True})
    assert status == 200, first_payload
    first = first_payload["session"]
    cleanup_test_sessions.append(first["session_id"])

    status, second_payload = _post(base_url, "/api/session/new", {"prev_session_id": first["session_id"], "link_to_prev": True})

    assert status == 200, second_payload
    second = second_payload["session"]
    cleanup_test_sessions.append(second["session_id"])
    assert second["thread_id"] == first["thread_id"]
    assert second["thread_root_session_id"] == source.session_id
    assert second["thread_prev_session_id"] == first["session_id"]
    assert second["thread_sequence"] == 3


def test_linking_existing_session_orders_thread_by_session_time(base_url, cleanup_test_sessions):
    root = _create_session(
        cleanup_test_sessions,
        title="Thread root",
        created_at=1000,
        updated_at=1000,
    )
    middle = _create_session(
        cleanup_test_sessions,
        title="Middle session",
        created_at=2000,
        updated_at=2000,
    )
    newer = _create_session(
        cleanup_test_sessions,
        title="Newer session",
        created_at=3000,
        updated_at=3000,
    )
    status, created = _post(base_url, "/api/thread/create", {"session_id": root.session_id, "title": "Ordered thread"})
    assert status == 200, created
    thread_id = created["thread"]["thread_id"]
    status, linked_newer = _post(base_url, "/api/thread/link-session", {
        "session_id": newer.session_id,
        "thread_id": thread_id,
    })
    assert status == 200, linked_newer
    assert linked_newer["session"]["thread_sequence"] == 2

    status, linked_middle = _post(base_url, "/api/thread/link-session", {
        "session_id": middle.session_id,
        "thread_id": thread_id,
    })

    assert status == 200, linked_middle
    assert linked_middle["session"]["thread_sequence"] == 2
    assert linked_middle["session"]["thread_prev_session_id"] == root.session_id
    status, detail = _get(base_url, "/api/thread", {"thread_id": thread_id})
    assert status == 200, detail
    assert [row["session_id"] for row in detail["sessions"]] == [root.session_id, middle.session_id, newer.session_id]
    assert [row["thread_sequence"] for row in detail["sessions"]] == [1, 2, 3]
    assert [row.get("thread_prev_session_id") for row in detail["sessions"]] == [None, root.session_id, middle.session_id]
    assert detail["thread"]["root_session_id"] == root.session_id
    assert detail["thread"]["latest_session_id"] == newer.session_id


def test_thread_routes_return_manifest_without_messages_by_default(base_url, cleanup_test_sessions):
    source = _create_session(cleanup_test_sessions, title="Manifest root")
    status, payload = _post(base_url, "/api/session/new", {"prev_session_id": source.session_id, "link_to_prev": True})
    assert status == 200, payload
    new_session = payload["session"]
    cleanup_test_sessions.append(new_session["session_id"])
    thread_id = new_session["thread_id"]

    status, list_payload = _get(base_url, "/api/threads")
    assert status == 200, list_payload
    thread_cards = [t for t in list_payload["threads"] if t["thread_id"] == thread_id]
    assert thread_cards
    assert "messages" not in json.dumps(thread_cards[0])

    status, detail_payload = _get(base_url, "/api/thread", {"thread_id": thread_id})
    assert status == 200, detail_payload
    assert detail_payload["thread"]["session_count"] == 2
    assert all("messages" not in row for row in detail_payload["sessions"])

    status, export_payload = _get(base_url, "/api/thread/export", {"thread_id": thread_id})
    assert status == 200, export_payload
    assert export_payload["include_messages"] is False
    assert all("messages" not in row for row in export_payload["sessions"])
    assert "original context" not in json.dumps(export_payload)


def test_thread_linking_does_not_set_parent_session_id_or_session_source_fork(base_url, cleanup_test_sessions):
    root = _create_session(cleanup_test_sessions, title="Root")
    target = _create_session(cleanup_test_sessions, title="Retroactive target")
    status, created = _post(base_url, "/api/thread/create", {"session_id": root.session_id, "title": "Retro thread"})
    assert status == 200, created
    thread_id = created["thread"]["thread_id"]

    status, linked = _post(base_url, "/api/thread/link-session", {
        "session_id": target.session_id,
        "thread_id": thread_id,
        "prev_session_id": root.session_id,
    })

    assert status == 200, linked
    session = linked["session"]
    assert session["thread_id"] == thread_id
    assert session["thread_prev_session_id"] == root.session_id
    assert session["thread_link_type"] == "manual_link"
    assert "parent_session_id" not in session
    assert session.get("session_source") != "fork"


def test_thread_update_can_mark_statuses(base_url, cleanup_test_sessions):
    source = _create_session(cleanup_test_sessions, title="Status root")
    status, created = _post(base_url, "/api/thread/create", {"session_id": source.session_id})
    assert status == 200, created
    thread_id = created["thread"]["thread_id"]

    for status_value in ("later", "blocked", "archived", "active"):
        status, updated = _post(base_url, "/api/thread/update", {"thread_id": thread_id, "status": status_value})
        assert status == 200, updated
        assert updated["thread"]["status"] == status_value
