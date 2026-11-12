"""Native-image turns keep model context private from the visible transcript."""

import json
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.helpers import public_session_projection
from api.models import get_state_db_session_messages
from api.process_event_utils import build_active_turn_token
from api.streaming import (
    _materialize_active_turn_user,
    _new_turn_context_from_messages,
    _active_turn_authority,
    _find_active_turn_checkpoint_index,
    _sanitize_messages_for_agent,
    _settle_result_messages,
)


IMAGE_A = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADUlEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
IMAGE_B = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNg+M8AAAICAQB7CYF4AAAAAElFTkSuQmCC"
RECALL_NOTE = "Recall note: the sample is neutral."
PLUGIN_NOTE = "Pre-call note: summarize visible details only."


def _js_function_source(src, name):
    start = src.find(f"function {name}(")
    assert start != -1, f"{name} not found"
    brace = src.find("{", start)
    depth = 0
    for index in range(brace, len(src)):
        if src[index] == "{":
            depth += 1
        elif src[index] == "}":
            depth -= 1
            if depth == 0:
                return src[start:index + 1]
    raise AssertionError(f"{name} body unterminated")


def _native_user_content(text, image_url, extra_text=()):
    parts = [{"type": "text", "text": f"[Workspace::v1: /fixture]\n{text}"}]
    if image_url:
        parts.append({"type": "image_url", "image_url": {"url": image_url}})
    parts.extend({"type": "text", "text": value} for value in extra_text)
    return parts


def _durable_agent_content(content):
    parts = []
    for part in content:
        parts.append("[screenshot]" if part.get("type") == "image_url" else part["text"])
    return " ".join("\n".join(parts).split())


def _settle_image_turn(
    *,
    session_id="native-image-session",
    text="Describe this image",
    image_url=IMAGE_A,
    timestamp=100.0,
    extra_text=(),
    queued_notifications=(),
    agent_row_id=None,
    empty_display_before_settle=False,
    previous_messages=(),
    previous_context=(),
    attachment_name="sample.png",
):
    stream_id = f"native-image-stream-{timestamp}"
    token = build_active_turn_token(stream_id, timestamp)
    attachments = (
        [{"name": attachment_name, "mime": "image/png", "is_image": True}]
        if attachment_name
        else []
    )
    checkpoint = ({
        "role": "user",
        "content": text,
        "timestamp": timestamp,
        "attachments": attachments,
        "_active_turn_token": token,
    } if text else None)
    session = SimpleNamespace(
        session_id=session_id,
        messages=[*previous_messages, *([checkpoint] if checkpoint else [])],
        context_messages=list(previous_context),
        pending_user_message=text,
        pending_attachments=attachments,
        pending_started_at=timestamp,
        pending_user_source="webui",
    )
    identity = _active_turn_authority(session, stream_id, text)
    agent_input_text = (
        "\n\n".join([*queued_notifications, text]).strip()
        if queued_notifications
        else text
    )
    identity["trusted_agent_input_text"] = agent_input_text
    identity.update({
        "current_turn_user_idx": len(previous_context),
        "turn_id": f"agent-turn-{timestamp}",
        "agent_turn_boundary_resolved": True,
    })
    rich_content = _native_user_content(agent_input_text, image_url, extra_text)
    api_content = json.dumps(rich_content)
    current_user = {
        "role": "user",
        "content": rich_content,
        "timestamp": timestamp,
        "api_content": api_content,
    }
    if agent_row_id is not None:
        current_user["_row_id"] = agent_row_id
    result_messages = [
        *previous_context,
        current_user,
        {"role": "assistant", "content": "A neutral sample image."},
    ]
    if empty_display_before_settle:
        session.messages = []
        assert not any(
            message.get("_active_turn_token") == identity["token"]
            for message in session.messages
        )
    _settle_result_messages(
        session,
        session.messages,
        previous_context,
        result_messages,
        text,
        "webui",
        identity,
    )
    return session, identity, api_content


def test_trusted_notification_prefix_keeps_image_turn_clean_after_reload(
    monkeypatch, tmp_path
):
    import api.models as models

    text = "Describe this image"
    notification = "Queued process update: sample job completed."
    session, identity, _ = _settle_image_turn(
        text=text,
        queued_notifications=(notification,),
        extra_text=(RECALL_NOTE, PLUGIN_NOTE),
        timestamp=900.0,
    )
    expected_context = _native_user_content(
        f"{notification}\n\n{text}", IMAGE_A, (RECALL_NOTE, PLUGIN_NOTE)
    )
    display_users = [
        message for message in session.messages if message.get("role") == "user"
    ]
    assert len(display_users) == 1
    assert display_users[0]["content"] == text
    assert display_users[0]["attachments"] == [
        {"name": "sample.png", "mime": "image/png", "is_image": True}
    ]
    context_users = [
        message for message in session.context_messages
        if message.get("role") == "user"
    ]
    assert len(context_users) == 1
    assert context_users[0]["content"] == expected_context

    session_dir = tmp_path / "webui-sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    models.Session(
        session_id=session.session_id,
        workspace="/fixture",
        model="fixture-model",
        context_length=128_000,
        messages=session.messages,
        context_messages=session.context_messages,
    ).save(skip_index=True)
    reloaded = models.Session.load(session.session_id)
    assert reloaded is not None
    reloaded_users = [
        message for message in reloaded.messages if message.get("role") == "user"
    ]
    assert len(reloaded_users) == 1
    assert reloaded_users[0]["content"] == text
    assert reloaded_users[0]["attachments"] == display_users[0]["attachments"]
    reloaded_context_users = [
        message for message in reloaded.context_messages
        if message.get("role") == "user"
    ]
    assert len(reloaded_context_users) == 1
    assert reloaded_context_users[0]["content"] == expected_context

    identity["current_turn_user_idx"] = 0
    untrusted = [{
        "role": "user",
        "content": _native_user_content(
            f"Untrusted text prefix\n\n{text}", IMAGE_A, (RECALL_NOTE, PLUGIN_NOTE)
        ),
    }]
    assert _find_active_turn_checkpoint_index(
        untrusted, [], identity, text,
    ) is None


def _write_state_db(path, session_id, rows):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE messages ("
            "id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, "
            "timestamp REAL, active INTEGER DEFAULT 1, api_content TEXT)"
        )
        conn.executemany(
            "INSERT INTO messages (session_id, role, content, timestamp, api_content) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (session_id, "user", content, timestamp, api_content)
                for content, timestamp, api_content in rows
            ],
        )


@pytest.mark.parametrize("msg_limit", [None, 5])
def test_get_session_projects_marked_payload_conflict_in_full_and_limited_paths(
    monkeypatch, tmp_path, msg_limit,
):
    import api.config
    import api.models as models
    import api.session_ops
    from api import routes

    session_id = f"native-image-route-{msg_limit}"
    timestamp = 880.0
    seed, identity, _ = _settle_image_turn(
        session_id=session_id,
        timestamp=timestamp,
        agent_row_id=41,
    )
    context_user = next(
        message for message in seed.context_messages
        if message.get("_active_turn_token") == identity["token"]
    )
    mirror = _durable_agent_content(context_user["content"])
    sidecar_payload = "SIDE-CAR-PROVIDER-PAYLOAD"
    state_payload = "STATE-DB-PROVIDER-PAYLOAD"
    sidecar_attachments = [{
        "name": "sidecar-owned.png",
        "mime": "image/png",
        "is_image": True,
    }]
    sidecar_mirror = {
        "role": "user",
        "content": mirror,
        "timestamp": timestamp,
        "_state_db_row_id": 1,
        "api_content": sidecar_payload,
        "attachments": sidecar_attachments,
    }
    seed.messages.append(dict(sidecar_mirror))
    seed.context_messages.append(dict(sidecar_mirror))

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    db_path = tmp_path / "state.db"
    _write_state_db(db_path, session_id, [(mirror, timestamp, state_payload)])
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)
    session = models.Session(
        session_id=session_id,
        workspace="/fixture",
        model="fixture-model",
        context_length=128_000,
        messages=seed.messages,
        context_messages=seed.context_messages,
        source_tag="webui",
        session_source="webui",
    )
    session.save(skip_index=True)
    with models.LOCK:
        models.SESSIONS.pop(session_id, None)

    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_: True)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda *_: {})
    monkeypatch.setattr(routes, "find_run_summary", lambda *_: None)
    monkeypatch.setattr(api.config, "load_settings", lambda: {"api_redact_enabled": False})
    monkeypatch.setattr(api.session_ops, "regeneration_state", lambda _session: ([], []))
    monkeypatch.setattr(
        api.session_ops,
        "regeneration_authority",
        lambda *_args, **_kwargs: None,
    )
    response = {}
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, **_kwargs: (
            response.update(payload=payload, status=status) or payload
        ),
    )
    query = f"session_id={session_id}&resolve_model=0"
    if msg_limit is not None:
        query += f"&msg_limit={msg_limit}"
    routes._handle_session_get(None, SimpleNamespace(path="/api/session", query=query))

    assert response["status"] == 200
    public_session = response["payload"]["session"]
    public_messages = public_session["messages"]
    mirror_rows = [message for message in public_messages if message.get("content") == mirror]
    assert len(mirror_rows) == 1, f"expected one visible mirror, found {len(mirror_rows)}"
    assert all("api_content" not in message for message in mirror_rows)
    assert public_session["message_count"] == 3
    assert mirror_rows[0]["attachments"] == sidecar_attachments

    state_rows = models.get_state_db_session_messages(session_id)
    replay_context = models.reconciled_state_db_messages_for_session(
        session,
        prefer_context=True,
        state_messages=state_rows,
    )
    replay_rows = [
        message for message in _sanitize_messages_for_agent(replay_context)
        if message.get("content") == mirror
    ]
    assert len(replay_rows) == 2
    assert {message["api_content"] for message in replay_rows} == {
        sidecar_payload,
        state_payload,
    }


@pytest.mark.parametrize("msg_limit", [None, 5])
@pytest.mark.parametrize("removed_payload_matches_retained", [False, True])
def test_truncation_watermark_keeps_proven_retained_image_row_only(
    monkeypatch, tmp_path, msg_limit, removed_payload_matches_retained,
):
    import api.config
    import api.models as models
    import api.session_ops
    from api import routes

    session_id = f"native-image-truncated-route-{msg_limit}"
    timestamp = 860.0
    retained_tail_timestamp = 880.0
    sidecar_payload = "SIDECAR-IMAGE-PAYLOAD-A"
    state_payload = "STATE-DB-IMAGE-PAYLOAD-B"
    removed_payload = (
        sidecar_payload if removed_payload_matches_retained
        else "STATE-DB-REMOVED-IMAGE-PAYLOAD"
    )
    seed, identity, _ = _settle_image_turn(
        session_id=session_id,
        timestamp=timestamp,
        agent_row_id=41,
    )
    context_user = next(
        message for message in seed.context_messages
        if message.get("_active_turn_token") == identity["token"]
    )
    mirror = _durable_agent_content(context_user["content"])
    older_rows = [
        {"role": "user", "content": "Earlier prompt", "timestamp": 100.0},
        {"role": "assistant", "content": "Earlier response", "timestamp": 101.0},
    ]
    seed.messages[:0] = [dict(row) for row in older_rows]
    seed.context_messages[:0] = [dict(row) for row in older_rows]
    sidecar_attachments = [{
        "name": "sidecar-owned.png",
        "mime": "image/png",
        "is_image": True,
    }]
    retained_sidecar_row = {
        "role": "user",
        "content": mirror,
        "timestamp": timestamp,
        "_state_db_row_id": 42,
        "api_content": sidecar_payload,
        "attachments": sidecar_attachments,
    }
    retained_tail_row = {
        "role": "assistant",
        "content": "Retained after Undo",
        "timestamp": retained_tail_timestamp,
    }
    seed.messages.extend([retained_sidecar_row, retained_tail_row])
    seed.context_messages.extend([retained_sidecar_row, retained_tail_row])

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
            "content TEXT, timestamp REAL, active INTEGER DEFAULT 1, api_content TEXT)"
        )
        conn.executemany(
            "INSERT INTO messages (id, session_id, role, content, timestamp, api_content) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (42, session_id, "user", mirror, timestamp, state_payload),
                (43, session_id, "user", mirror, timestamp, removed_payload),
                (44, session_id, "user", mirror, retained_tail_timestamp, "SAME-SECOND-REMOVED-PAYLOAD"),
            ],
        )
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)
    session = models.Session(
        session_id=session_id,
        workspace="/fixture",
        model="fixture-model",
        context_length=128_000,
        messages=seed.messages,
        context_messages=seed.context_messages,
        truncation_watermark=retained_tail_timestamp,
        truncation_boundary=retained_tail_timestamp,
        source_tag="webui",
        session_source="webui",
    )
    session.save(skip_index=True)
    with models.LOCK:
        models.SESSIONS.pop(session_id, None)
    state_rows = models.get_state_db_session_messages(session_id)
    marked_state_rows = models._suppress_native_image_display_mirrors(
        session,
        state_rows,
    )
    assert all(
        message["_webui_unmatched_native_image_mirror"] is True
        for message in marked_state_rows[:2]
    )

    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_: True)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda *_: {})
    monkeypatch.setattr(routes, "find_run_summary", lambda *_: None)
    monkeypatch.setattr(api.config, "load_settings", lambda: {"api_redact_enabled": False})
    monkeypatch.setattr(api.session_ops, "regeneration_state", lambda _session: ([], []))
    monkeypatch.setattr(api.session_ops, "regeneration_authority", lambda *_args, **_kwargs: None)
    response = {}
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, **_kwargs: (
            response.update(payload=payload, status=status) or payload
        ),
    )
    query = f"session_id={session_id}&resolve_model=0"
    if msg_limit is not None:
        query += f"&msg_limit={msg_limit}"
    routes._handle_session_get(None, SimpleNamespace(path="/api/session", query=query))

    assert response["status"] == 200
    public_session = response["payload"]["session"]
    public_messages = public_session["messages"]
    if msg_limit is not None:
        assert len(public_messages) == msg_limit
        assert public_session["message_count"] > len(public_messages)
    public_mirrors = [
        message for message in public_messages
        if message.get("content") == mirror
    ]
    assert len(public_mirrors) == 1
    assert public_mirrors[0]["attachments"] == sidecar_attachments
    assert any(
        message.get("content") == "Retained after Undo"
        for message in public_messages
    )
    assert next(
        message for message in public_messages
        if message.get("content") == "Describe this image"
    )["attachments"][0]["name"] == "sample.png"

    first_public = None
    first_context = None
    for _ in range(3):
        display = models.reconciled_state_db_messages_for_session(
            session,
            state_messages=state_rows,
        )
        context = models.reconciled_state_db_messages_for_session(
            session,
            prefer_context=True,
            state_messages=state_rows,
        )
        display_row_42 = [
            message for message in display
            if message.get("_state_db_row_id") == 42
        ]
        context_row_42 = [
            message for message in context
            if message.get("_state_db_row_id") == 42
        ]
        assert len(display_row_42) == 1
        assert display_row_42[0]["api_content"] == sidecar_payload
        assert display_row_42[0]["attachments"] == sidecar_attachments
        assert len(context_row_42) == 2
        assert {message["api_content"] for message in context_row_42} == {
            sidecar_payload, state_payload,
        }
        assert not any(
            message.get("_state_db_row_id") in (43, 44)
            for message in (*display, *context)
        )
        assert any(
            message.get("_active_turn_token") == identity["token"]
            for message in context
        )
        public = public_session_projection({"messages": display})["messages"]
        replay = _sanitize_messages_for_agent(context)
        assert sum(message.get("content") == mirror for message in public) == 1
        replay_row_42 = [
            message for message in replay
            if message.get("content") == mirror
        ]
        assert len(replay_row_42) == 2
        assert {message["api_content"] for message in replay_row_42} == {
            sidecar_payload, state_payload,
        }
        if first_public is None:
            first_public = public
            first_context = replay
        else:
            assert public == first_public
            assert replay == first_context

    session.truncation_boundary = timestamp - 10
    advanced_state_copy = {
        "role": "user",
        "content": mirror,
        "timestamp": retained_tail_timestamp + 5,
        "_state_db_row_id": 42,
        "api_content": state_payload,
    }
    advanced_context = models.reconciled_state_db_messages_for_session(
        session,
        prefer_context=True,
        state_messages=[advanced_state_copy],
    )
    assert not any(
        message.get("timestamp") == retained_tail_timestamp + 5
        for message in advanced_context
    )


def test_get_session_projects_parent_only_payload_conflict_without_losing_parent_rows(
    monkeypatch, tmp_path,
):
    import api.config
    import api.models as models
    import api.session_ops
    from api import routes

    session_id = "native-image-lineage-child"
    parent_id = "native-image-lineage-parent"
    timestamp = 890.0
    seed, identity, _ = _settle_image_turn(
        session_id=session_id,
        timestamp=timestamp,
        agent_row_id=41,
    )
    context_user = next(
        message for message in seed.context_messages
        if message.get("_active_turn_token") == identity["token"]
    )
    mirror = _durable_agent_content(context_user["content"])
    sidecar_payload = "PARENT-SIDECAR-PROVIDER-PAYLOAD"
    state_payload = "CHILD-STATE-DB-PROVIDER-PAYLOAD"
    sidecar_attachments = [{
        "name": "parent-owned.png",
        "mime": "image/png",
        "is_image": True,
    }]
    parent_mirror = {
        "role": "user",
        "content": mirror,
        "timestamp": timestamp,
        "_state_db_row_id": 1,
        "api_content": sidecar_payload,
        "attachments": sidecar_attachments,
    }
    seed.context_messages.append(dict(parent_mirror))

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    db_path = tmp_path / "state.db"
    _write_state_db(db_path, session_id, [(mirror, timestamp, state_payload)])
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)
    parent = models.Session(
        session_id=parent_id,
        workspace="/fixture",
        model="fixture-model",
        context_length=128_000,
        messages=[
            {"role": "user", "content": "Unrelated parent-only row", "timestamp": 870.0},
            dict(parent_mirror),
        ],
        source_tag="webui",
        session_source="webui",
        pre_compression_snapshot=False,
    )
    parent.save(skip_index=True)
    session = models.Session(
        session_id=session_id,
        workspace="/fixture",
        model="fixture-model",
        context_length=128_000,
        messages=seed.messages,
        context_messages=seed.context_messages,
        parent_session_id=parent_id,
        source_tag="webui",
        session_source="webui",
    )
    session.save(skip_index=True)
    with models.LOCK:
        models.SESSIONS.pop(session_id, None)
        models.SESSIONS.pop(parent_id, None)

    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_: True)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda *_: {})
    monkeypatch.setattr(routes, "find_run_summary", lambda *_: None)
    monkeypatch.setattr(api.config, "load_settings", lambda: {"api_redact_enabled": False})
    monkeypatch.setattr(api.session_ops, "regeneration_state", lambda _session: ([], []))
    monkeypatch.setattr(
        api.session_ops,
        "regeneration_authority",
        lambda *_args, **_kwargs: None,
    )
    response = {}
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, **_kwargs: (
            response.update(payload=payload, status=status) or payload
        ),
    )
    original_get_session = routes.get_session
    parent_loads = []

    def get_session_with_fresh_parent(requested_id, metadata_only=False):
        if requested_id == parent_id:
            parent_session = models.Session.load(parent_id)
            parent_loads.append(parent_session)
            return parent_session
        return original_get_session(requested_id, metadata_only=metadata_only)

    monkeypatch.setattr(routes, "get_session", get_session_with_fresh_parent)
    routes._handle_session_get(
        None,
        SimpleNamespace(path="/api/session", query=f"session_id={session_id}&resolve_model=0"),
    )

    assert len(parent_loads) == 1
    assert response["status"] == 200
    public_messages = response["payload"]["session"]["messages"]
    mirror_rows = [message for message in public_messages if message.get("content") == mirror]
    assert len(mirror_rows) == 1, f"expected one visible mirror, found {len(mirror_rows)}"
    assert mirror_rows[0]["attachments"] == sidecar_attachments
    assert "Unrelated parent-only row" in [message.get("content") for message in public_messages]
    assert all("api_content" not in message for message in mirror_rows)

    state_rows = models.get_state_db_session_messages(session_id)
    replay_context = models.reconciled_state_db_messages_for_session(
        session,
        prefer_context=True,
        state_messages=state_rows,
    )
    assert "Unrelated parent-only row" not in [
        message.get("content") for message in replay_context
    ]
    replay_rows = [
        message for message in _sanitize_messages_for_agent(replay_context)
        if message.get("content") == mirror
    ]
    assert len(replay_rows) == 2
    assert {message["api_content"] for message in replay_rows} == {
        sidecar_payload,
        state_payload,
    }


def test_limited_conflict_projection_keeps_state_row_when_owner_is_outside_slice():
    from api import routes

    timestamp = 881.0
    mirror = "Describe this image [screenshot]"
    sidecar_slice = [{"role": "user", "content": "Earlier submission", "timestamp": 879.0}]
    marked_state_row = {
        "role": "user",
        "content": mirror,
        "timestamp": timestamp,
        "_state_db_row_id": 42,
        "api_content": "STATE-DB-PROVIDER-PAYLOAD",
        "_webui_unmatched_native_image_mirror": True,
    }
    session = SimpleNamespace(
        session_id="limited-conflict-owner-outside-page",
        messages=sidecar_slice,
        context_messages=[],
    )
    merged = routes._limited_webui_messages_for_display_with_sidecar(
        session,
        sidecar_slice,
        [marked_state_row],
        state_db_signature=None,
        msg_before=1,
    )
    assert any(message.get("content") == "Earlier submission" for message in merged)
    state_rows = [message for message in merged if message.get("content") == mirror]
    assert len(state_rows) == 1
    assert state_rows[0]["api_content"] == marked_state_row["api_content"]


@pytest.mark.parametrize("msg_limit", [None, 5])
@pytest.mark.parametrize("attachment_mode", ["native_image", "text_attachment"])
def test_get_session_keeps_pending_agent_projection_private_but_in_context(
    monkeypatch, tmp_path, msg_limit, attachment_mode,
):
    import api.config
    import api.models as models
    import api.session_ops
    from api import routes
    from api.streaming import (
        _build_run_conversation_kwargs,
        _register_pending_user_timestamp_identity,
    )

    timestamp = time.time()
    stream_id = "pending-webui-stream"
    session_id = f"pending-display-{attachment_mode}-{'full' if msg_limit is None else 'limited'}"
    prompt = "Recall note: I authored this literal text."
    attachments = [
        {"name": "sample.png" if attachment_mode == "native_image" else "sample.txt",
         "mime": "image/png" if attachment_mode == "native_image" else "text/plain",
         "is_image": attachment_mode == "native_image"}
    ]
    if attachment_mode == "native_image":
        agent_content = _native_user_content(
            prompt, IMAGE_A, (RECALL_NOTE, PLUGIN_NOTE),
        )
        stored_content = "\x00json:" + json.dumps(agent_content)
        api_content = json.dumps(agent_content)
    else:
        agent_content = f"{prompt}\n\n{RECALL_NOTE}\n\n{PLUGIN_NOTE}"
        stored_content = agent_content
        api_content = None

    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
            "content TEXT, timestamp REAL, active INTEGER DEFAULT 1, api_content TEXT)"
        )

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)
    session = models.Session(
        session_id=session_id,
        workspace="/fixture",
        model="fixture-model",
        context_length=128_000,
        messages=[],
        context_messages=[],
        active_stream_id=stream_id,
        pending_user_message=prompt,
        pending_attachments=attachments,
        pending_started_at=timestamp,
        pending_user_source="webui",
        source_tag="webui",
        session_source="webui",
    )
    session.save(skip_index=True)

    def agent_run(user_message, persist_user_timestamp=None, **_kwargs):
        assert user_message == prompt
        assert persist_user_timestamp == timestamp
        persisted = models.Session.load(session_id)
        assert persisted._webui_pending_user_timestamp_identity == (stream_id, timestamp)
        with sqlite3.connect(db_path) as conn:
            conn.executemany(
                "INSERT INTO messages (id, session_id, role, content, timestamp, api_content) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (1, session_id, "user", "A preserved prior turn", timestamp - 2, None),
                    (2, session_id, "user", stored_content, timestamp, api_content),
                    (3, session_id, "user", "A distinct later user row", timestamp + 2, None),
                    (4, "other-profile-session", "user", stored_content, timestamp, api_content),
                ],
            )

    with api.config._get_session_agent_lock(session_id):
        _register_pending_user_timestamp_identity(agent_run, session, timestamp)
    run_kwargs = _build_run_conversation_kwargs(
        agent_run,
        user_message=prompt,
        system_message="system",
        conversation_history=[],
        conversation_history_revision=None,
        task_id=session_id,
        persist_user_message=prompt,
        persist_user_timestamp=timestamp,
    )
    agent_run(**run_kwargs)
    # A cold Session.load restores only the proof persisted before the Agent
    # wrote its pending user row.
    session = models.Session.load(session_id)
    assert session._webui_pending_user_timestamp_identity == (stream_id, timestamp)

    response = {}
    # Simulate a WebUI process restart: no stream or worker remains, but the
    # pending timestamp is inside the existing stale-repair grace window.
    monkeypatch.setattr(routes, "STREAMS", {})
    monkeypatch.setattr(api.config, "ACTIVE_RUNS", {})
    with models.LOCK:
        models.SESSIONS.pop(session_id, None)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_args: True)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda *_args: {})
    monkeypatch.setattr(routes, "find_run_summary", lambda *_args: None)
    monkeypatch.setattr(api.config, "load_settings", lambda: {"api_redact_enabled": False})
    monkeypatch.setattr(api.session_ops, "regeneration_state", lambda _session: ([], []))
    monkeypatch.setattr(api.session_ops, "regeneration_authority", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, **_kwargs: (
            response.update(payload=payload, status=status) or payload
        ),
    )
    query = f"session_id={session_id}&resolve_model=0"
    if msg_limit is not None:
        query += f"&msg_limit={msg_limit}"
    routes._handle_session_get(
        None,
        SimpleNamespace(path="/api/session", query=query),
    )

    assert response["status"] == 200
    public_session = response["payload"]["session"]
    public_messages = public_session["messages"]
    assert public_session["active_stream_id"] == stream_id
    assert public_session["pending_user_message"] == prompt
    assert public_session["pending_attachments"] == attachments
    assert prompt in json.dumps(public_session)
    assert "_webui_pending_user_timestamp_identity" not in json.dumps(public_session)
    assert RECALL_NOTE not in json.dumps(public_session)
    assert PLUGIN_NOTE not in json.dumps(public_session)
    assert all(
        RECALL_NOTE not in json.dumps(message)
        and PLUGIN_NOTE not in json.dumps(message)
        for message in public_messages
    )
    assert any(message.get("content") == "A preserved prior turn" for message in public_messages)
    assert any(message.get("content") == "A distinct later user row" for message in public_messages)
    assert not any(message.get("session_id") == "other-profile-session" for message in public_messages)

    state_rows = models.get_state_db_session_messages(session_id)
    context = models.reconciled_state_db_messages_for_session(
        session,
        prefer_context=True,
        state_messages=state_rows,
    )
    active_context_rows = [
        message for message in context
        if message.get("role") == "user" and message.get("timestamp") == timestamp
    ]
    assert len(active_context_rows) == 1
    assert RECALL_NOTE in json.dumps(active_context_rows[0]["content"])
    assert PLUGIN_NOTE in json.dumps(active_context_rows[0]["content"])
    if attachment_mode == "native_image":
        assert any(
            part.get("type") == "image_url"
            for part in active_context_rows[0]["content"]
        )
    replay_rows = _sanitize_messages_for_agent(context)
    assert sum(
        RECALL_NOTE in json.dumps(message)
        and PLUGIN_NOTE in json.dumps(message)
        for message in replay_rows
    ) == 1
    assert RECALL_NOTE in json.dumps(replay_rows)
    assert PLUGIN_NOTE in json.dumps(replay_rows)
    assert not session.messages and not session.context_messages

    # A metadata-only poll may arrive after the full load. It must not erase
    # the pending attachment from the browser's session state.
    response.clear()
    routes._handle_session_get(
        None,
        SimpleNamespace(
            path="/api/session",
            query=f"session_id={session_id}&resolve_model=0&messages=0",
        ),
    )
    metadata = response["payload"]["session"]
    assert metadata["pending_user_message"] == prompt
    assert metadata["pending_attachments"] == attachments
    assert RECALL_NOTE not in json.dumps(metadata)
    assert PLUGIN_NOTE not in json.dumps(metadata)

    next_session = models.Session.load(session_id)
    monkeypatch.setattr(routes, "register_session_writeback_owner", lambda *_args: None)
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "deferred")
    routes._prepare_chat_start_session_for_stream(
        next_session,
        msg="A separate next turn",
        attachments=[],
        workspace="/fixture",
        model="fixture-model",
        model_provider=None,
        stream_id="next-webui-stream",
        started_at=timestamp + 1,
    )
    assert next_session._webui_pending_user_timestamp_identity is None
    assert models.Session.load(session_id)._webui_pending_user_timestamp_identity is None


def test_pending_row_identity_requires_timestamp_handoff_to_active_agent():
    from api.models import _suppress_native_image_display_mirrors
    from api.streaming import (
        _build_run_conversation_kwargs,
        _register_pending_user_timestamp_identity,
        _get_session_agent_lock,
    )

    def modern_run(user_message, persist_user_timestamp=None):
        return None

    def legacy_run(user_message):
        return None

    def opaque_run(user_message, **kwargs):
        return None

    timestamp = 1700000000.125
    session = SimpleNamespace(
        active_stream_id="stream-identity",
        pending_started_at=timestamp,
        pending_user_message="prompt",
        pending_user_source="webui",
    )
    kwargs = {
        "user_message": "prompt",
        "system_message": "system",
        "conversation_history": [],
        "conversation_history_revision": None,
        "task_id": "session",
        "persist_user_message": "prompt",
        "persist_user_timestamp": timestamp,
    }
    with _get_session_agent_lock("session"):
        _register_pending_user_timestamp_identity(modern_run, session, timestamp)
    modern = _build_run_conversation_kwargs(modern_run, **kwargs)
    assert modern["persist_user_timestamp"] == timestamp
    assert session._webui_pending_user_timestamp_identity == (
        "stream-identity", timestamp,
    )
    with _get_session_agent_lock("session"):
        _register_pending_user_timestamp_identity(modern_run, session, timestamp)
    rebuilt = _build_run_conversation_kwargs(modern_run, **kwargs)
    assert rebuilt["persist_user_timestamp"] == timestamp
    assert session._webui_pending_user_timestamp_identity == (
        "stream-identity", timestamp,
    )
    # Credential-heal fallback can replace the callable mid-turn. Proof from
    # the first invocation must not authorize rows from an older Agent.
    with _get_session_agent_lock("session"):
        _register_pending_user_timestamp_identity(legacy_run, session, timestamp)
    fallback = _build_run_conversation_kwargs(legacy_run, **kwargs)
    assert "persist_user_timestamp" not in fallback
    assert session._webui_pending_user_timestamp_identity is None

    legacy_session = SimpleNamespace(
        active_stream_id="stream-identity",
        pending_started_at=timestamp,
        pending_user_message="prompt",
        pending_user_source="webui",
    )
    with _get_session_agent_lock("session"):
        _register_pending_user_timestamp_identity(
            legacy_run, legacy_session, timestamp
        )
    legacy = _build_run_conversation_kwargs(legacy_run, **kwargs)
    assert "persist_user_timestamp" not in legacy
    assert legacy_session._webui_pending_user_timestamp_identity is None
    with _get_session_agent_lock("session"):
        _register_pending_user_timestamp_identity(
            opaque_run, legacy_session, timestamp
        )
    opaque = _build_run_conversation_kwargs(opaque_run, **kwargs)
    assert "persist_user_timestamp" in opaque
    assert legacy_session._webui_pending_user_timestamp_identity is None
    row = {"role": "user", "content": "Agent-only memory", "timestamp": timestamp}
    assert _suppress_native_image_display_mirrors(legacy_session, [row]) == [row]

    for field, value in (
        ("active_stream_id", "another-stream"),
        ("pending_user_source", "cli"),
        ("pending_started_at", timestamp + 1),
        ("pending_user_message", None),
    ):
        mismatched = SimpleNamespace(
            active_stream_id="stream-identity",
            pending_started_at=timestamp,
            pending_user_message="prompt",
            pending_user_source="webui",
            _webui_pending_user_timestamp_identity=("stream-identity", timestamp),
        )
        setattr(mismatched, field, value)
        assert _suppress_native_image_display_mirrors(mismatched, [row]) == [row]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_clean_pending_get_payload_renders_submitted_prompt_and_attachment():
    root = Path(__file__).resolve().parents[1]
    ui_js = (root / "static" / "ui.js").read_text(encoding="utf-8")
    sessions_js = (root / "static" / "sessions.js").read_text(encoding="utf-8")
    helpers = "\n".join(
        [
            *[
                _js_function_source(sessions_js, name)
                for name in (
                    "_messageComparableText",
                    "_stripAttachedFilesMarker",
                    "_stripForcedSkillEnvelope",
                    "_normalizeUserTranscriptText",
                    "_sameTranscriptMessage",
                    "_currentTailUserMessage",
                    "_hasCurrentTailUserDuplicate",
                    "_mergePendingSessionMessage",
                )
            ],
            *[
                _js_function_source(ui_js, name)
                for name in (
                    "_pendingCurrentTailUserMessage",
                    "_messageTimestampSeconds",
                    "_activeTurnTokenMatches",
                    "_pendingActiveTurnUserMessage",
                    "getPendingSessionMessage",
                )
            ],
        ]
    )
    payload = {
        "session_id": "pending-display-session",
        "active_stream_id": "pending-webui-stream",
        "pending_started_at": 1700000000.125,
        "pending_user_source": "webui",
        "pending_user_message": "Recall note: I authored this literal text.",
        "pending_attachments": [{"name": "literal-note.txt", "mime": "text/plain"}],
        "messages": [],
    }
    assert "_mergePendingSessionMessage(S.session,S.messages)" in sessions_js
    assert "_mergePendingSessionMessage(data.session, S.messages)" in ui_js
    assert "await refreshSession();" in ui_js
    script = f"""
{helpers}
const session={json.dumps(payload)};
const messages=session.messages;
const inserted=_mergePendingSessionMessage(session,messages);
const duplicate=_mergePendingSessionMessage(session,messages);
process.stdout.write(JSON.stringify({{inserted,duplicate,messages}}));
"""
    completed = subprocess.run(
        [shutil.which("node"), "-e", script],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    result = json.loads(completed.stdout)
    assert result["inserted"] is True
    assert result["duplicate"] is False
    assert result["messages"] == [
        {
            "role": "user",
            "content": "Recall note: I authored this literal text.",
            "attachments": [{"name": "literal-note.txt", "mime": "text/plain"}],
            "_ts": 1700000000.125,
            "_pending": True,
            "_source": "webui",
        }
    ]


def test_settlement_reload_and_next_turn_keep_one_clean_bubble_and_rich_context(
    monkeypatch, tmp_path
):
    import api.config
    import api.models as models
    from api import routes

    text = "Describe this image"
    notification = "Queued process update: sample job completed."
    session, identity, api_content = _settle_image_turn(
        text=text,
        queued_notifications=(notification,),
        extra_text=(RECALL_NOTE, PLUGIN_NOTE),
        agent_row_id=1,
        empty_display_before_settle=True,
    )
    token = identity["token"]
    session_dir = tmp_path / "webui-sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    persisted = models.Session(
        session_id=session.session_id,
        workspace="/fixture",
        model="fixture-model",
        context_length=128_000,
        messages=session.messages,
        context_messages=session.context_messages,
    )
    persisted.save(skip_index=True)
    session = models.Session.load(session.session_id)
    assert session is not None

    current_rows = [
        message for message in session.messages
        if isinstance(message, dict) and message.get("_active_turn_token") == token
    ]
    assert len(current_rows) == 1
    assert current_rows[0]["content"] == text
    assert "_webui_display_content" not in current_rows[0]
    assert "api_content" not in current_rows[0]
    assert current_rows[0]["attachments"][0]["name"] == "sample.png"
    assert current_rows[0]["_row_id"] == 1
    settled_current = [
        message for message in session.messages
        if message.get("role") == "user" and message.get("timestamp") == 100.0
    ]
    assert len(settled_current) == 1
    assert settled_current[0]["content"] == text

    context_user = next(
        message for message in session.context_messages
        if message.get("_active_turn_token") == token
    )
    expected_context = _native_user_content(
        f"{notification}\n\n{text}", IMAGE_A, (RECALL_NOTE, PLUGIN_NOTE)
    )
    assert context_user["content"] == expected_context
    assert context_user["api_content"] == api_content
    assert context_user["_row_id"] == 1
    assert context_user["_webui_trusted_agent_input_text"] == f"{notification}\n\n{text}"
    assert "_webui_trusted_agent_input_text" not in current_rows[0]

    db_path = tmp_path / "state.db"
    _write_state_db(
        db_path,
        session.session_id,
        [(_durable_agent_content(context_user["content"]), 100.0, None)],
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)
    state_rows = get_state_db_session_messages(session.session_id)
    assert len(state_rows) == 1
    assert state_rows[0].get("api_content") is None
    assert state_rows[0]["_state_db_row_id"] == 1
    display_rows = models.reconciled_state_db_messages_for_session(
        session,
        state_messages=state_rows,
    )
    current_display_rows = [
        message for message in display_rows
        if message.get("_active_turn_token") == token
    ]
    assert len(current_display_rows) == 1
    reconciled_current = [
        message for message in display_rows
        if message.get("role") == "user" and message.get("timestamp") == 100.0
    ]
    assert len(reconciled_current) == 1
    assert reconciled_current[0]["content"] == text
    assert reconciled_current[0]["attachments"][0]["name"] == "sample.png"

    monkeypatch.setattr(api.config, "load_settings", lambda: {"api_redact_enabled": False})
    public = public_session_projection({
        "session_id": session.session_id,
        "messages": display_rows,
    })
    public_current = [
        message for message in public["messages"]
        if message.get("role") == "user" and message.get("timestamp") == 100.0
    ]
    assert len(public_current) == 1
    assert public_current[0]["content"] == text
    assert public_current[0]["attachments"][0]["name"] == "sample.png"
    assert not any(
        key in public_current[0]
        for key in ("_webui_display_content", "_active_turn_token", "api_content")
    )

    route_response = {}
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, metadata_only=False: models.Session.load(sid),
    )
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_args: True)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, **_kwargs: (
            route_response.update(payload=payload, status=status) or payload
        ),
    )
    routes._handle_session_get(
        None,
        SimpleNamespace(
            path="/api/session",
            query=f"session_id={session.session_id}&resolve_model=0",
        ),
    )
    assert route_response["status"] == 200
    route_messages = route_response["payload"]["session"]["messages"]
    route_current = [
        message for message in route_messages
        if message.get("role") == "user" and message.get("timestamp") == 100.0
    ]
    assert len(route_current) == 1
    assert route_current[0]["content"] == text
    assert route_current[0]["attachments"][0]["name"] == "sample.png"
    assert not any(
        key in route_current[0]
        for key in ("_webui_display_content", "_active_turn_token", "api_content")
    )
    projected_context = public_session_projection({
        "context_messages": session.context_messages,
    })["context_messages"]
    assert not any(
        "_webui_trusted_agent_input_text" in message
        for message in projected_context
    )

    recovered_context = models.reconciled_state_db_messages_for_session(
        session,
        prefer_context=True,
        state_messages=state_rows,
    )
    replay = _sanitize_messages_for_agent(recovered_context)
    replay_users = [message for message in replay if message.get("role") == "user"]
    assert len(replay_users) == 1
    assert replay_users[0]["content"] == expected_context
    assert replay_users[0]["api_content"] == api_content

    second_text = "What object is shown?"
    next_turn_history = _new_turn_context_from_messages(
        recovered_context,
        second_text,
    )
    model_history = _sanitize_messages_for_agent(next_turn_history)
    model_history_user = next(
        message for message in model_history if message.get("role") == "user"
    )
    assert model_history_user["content"] == expected_context
    assert model_history_user["api_content"] == api_content

    second_stream_id = "native-image-second-stream"
    session.active_stream_id = second_stream_id
    session.pending_user_message = second_text
    session.pending_attachments = []
    session.pending_started_at = 200.0
    session.pending_user_source = "webui"
    second_identity = _active_turn_authority(session, second_stream_id, second_text)
    second_identity.update({
        "current_turn_user_idx": len(model_history),
        "turn_id": "agent-turn-second",
        "agent_turn_boundary_resolved": True,
    })
    checkpoint = _materialize_active_turn_user(second_identity, second_text, "webui")
    second_identity["checkpoint"] = checkpoint
    previous_display = [*display_rows, checkpoint]
    session.messages = previous_display
    result_messages = [
        *model_history,
        {
            "role": "user",
            "content": f"[Workspace::v1: /fixture]\n{second_text}",
            "timestamp": 200.0,
        },
        {"role": "assistant", "content": "A neutral follow-up.", "timestamp": 200.1},
    ]
    state_rows_before_second_turn = list(state_rows)
    _settle_result_messages(
        session,
        previous_display,
        recovered_context,
        result_messages,
        second_text,
        "webui",
        second_identity,
    )

    settled_users = [
        message for message in session.messages if message.get("role") == "user"
    ]
    assert [message["content"] for message in settled_users] == [text, second_text]
    assert settled_users[0]["attachments"][0]["name"] == "sample.png"
    rich_after_second_turn = next(
        message for message in session.context_messages
        if message.get("_active_turn_token") == token
    )
    assert rich_after_second_turn["content"] == expected_context
    assert rich_after_second_turn["_webui_trusted_agent_input_text"] == (
        f"{notification}\n\n{text}"
    )
    assert get_state_db_session_messages(session.session_id) == state_rows_before_second_turn

    session.active_stream_id = None
    session.pending_user_message = None
    session.pending_attachments = []
    session.pending_started_at = None
    session.pending_user_source = None
    session.save(skip_index=True)
    session = models.Session.load(session.session_id)
    assert session is not None
    reloaded_users = [
        message for message in session.messages if message.get("role") == "user"
    ]
    assert [message["content"] for message in reloaded_users] == [text, second_text]
    assert reloaded_users[0]["attachments"][0]["name"] == "sample.png"
    reloaded_rich_user = next(
        message for message in session.context_messages
        if message.get("_active_turn_token") == token
    )
    assert reloaded_rich_user["content"] == expected_context
    assert reloaded_rich_user["_webui_trusted_agent_input_text"] == (
        f"{notification}\n\n{text}"
    )

    route_response.clear()
    routes._handle_session_get(
        None,
        SimpleNamespace(
            path="/api/session",
            query=f"session_id={session.session_id}&resolve_model=0",
        ),
    )
    api_users = [
        message for message in route_response["payload"]["session"]["messages"]
        if message.get("role") == "user"
    ]
    assert [message["content"] for message in api_users] == [text, second_text]
    assert api_users[0]["attachments"][0]["name"] == "sample.png"


def test_image_only_and_no_context_turns_keep_the_submitted_display_text():
    image_only, image_only_identity, _ = _settle_image_turn(
        text="",
        image_url=IMAGE_A,
        timestamp=200.0,
        attachment_name="image-only.png",
    )
    image_only_row = next(
        message for message in image_only.messages
        if message.get("_active_turn_token") == image_only_identity["token"]
    )
    assert image_only_row["content"] == ""
    assert "_webui_display_content" not in image_only_row
    assert "api_content" not in image_only_row
    assert image_only_row["attachments"][0]["name"] == "image-only.png"

    no_context, no_context_identity, _ = _settle_image_turn(
        text="What is in this image?",
        image_url=IMAGE_B,
        timestamp=300.0,
    )
    no_context_row = next(
        message for message in no_context.messages
        if message.get("_active_turn_token") == no_context_identity["token"]
    )
    assert no_context_row["content"] == "What is in this image?"
    assert "_webui_display_content" not in no_context_row

    no_prefetch, no_prefetch_identity, _ = _settle_image_turn(
        text="This image was not prefetched.",
        image_url=IMAGE_A,
        timestamp=350.0,
        attachment_name=None,
    )
    no_prefetch_row = next(
        message for message in no_prefetch.messages
        if message.get("_active_turn_token") == no_prefetch_identity["token"]
    )
    assert no_prefetch_row["content"] == "This image was not prefetched."
    assert no_prefetch_row.get("attachments") in (None, [])


def test_repeated_literal_marker_prompt_keeps_distinct_image_turns(monkeypatch):
    import api.config
    import api.models as models

    monkeypatch.setattr(api.config, "load_settings", lambda: {"api_redact_enabled": False})
    prompt = "Please keep <memory-context>this literal text</memory-context>."
    first, first_identity, _ = _settle_image_turn(
        text=prompt,
        image_url=IMAGE_A,
        timestamp=400.0,
        attachment_name="first.png",
    )
    second, second_identity, _ = _settle_image_turn(
        text=prompt,
        image_url=IMAGE_B,
        timestamp=500.0,
        previous_messages=first.messages,
        previous_context=first.context_messages,
        attachment_name="second.png",
    )
    tokens = {first_identity["token"], second_identity["token"]}
    for row_id, token in enumerate((first_identity["token"], second_identity["token"]), 1):
        for rows in (second.messages, second.context_messages):
            next(message for message in rows if message.get("_active_turn_token") == token)[
                "_row_id"
            ] = row_id
    settled_turns = [
        message for message in second.messages
        if message.get("role") == "user" and message.get("timestamp") in (400.0, 500.0)
    ]
    assert len(settled_turns) == 2
    assert [message["content"] for message in settled_turns] == [prompt, prompt]
    assert {message.get("_active_turn_token") for message in settled_turns} == tokens
    assert [message["attachments"][0]["name"] for message in settled_turns] == [
        "first.png", "second.png",
    ]

    state_rows = []
    for message in second.context_messages:
        if message.get("_active_turn_token") in tokens:
            state_rows.append({
                "role": "user",
                "content": _durable_agent_content(message["content"]),
                "timestamp": message["timestamp"],
                "api_content": message["api_content"],
                "_state_db_row_id": len(state_rows) + 1,
            })
    display_rows = models.reconciled_state_db_messages_for_session(
        second,
        state_messages=state_rows,
    )
    displayed_turns = [
        message for message in display_rows
        if message.get("role") == "user" and message.get("timestamp") in (400.0, 500.0)
    ]
    assert len(displayed_turns) == 2
    assert [message["content"] for message in displayed_turns] == [prompt, prompt]
    assert [message["attachments"][0]["name"] for message in displayed_turns] == [
        "first.png", "second.png",
    ]

    public = public_session_projection({
        "session_id": second.session_id,
        "messages": display_rows,
    })
    public_turns = [
        message for message in public["messages"]
        if message.get("role") == "user" and message.get("timestamp") in (400.0, 500.0)
    ]
    assert len(public_turns) == 2
    assert [message["content"] for message in public_turns] == [prompt, prompt]
    assert [message["attachments"][0]["name"] for message in public_turns] == [
        "first.png", "second.png",
    ]


def test_state_db_mirror_with_conflicting_identity_is_not_suppressed():
    import api.models as models

    session, identity, api_content = _settle_image_turn(
        text="Describe this image",
        extra_text=(RECALL_NOTE,),
        timestamp=600.0,
    )
    context_user = next(
        message for message in session.context_messages
        if message.get("_active_turn_token") == identity["token"]
    )
    conflicting = {
        "role": "user",
        "content": _durable_agent_content(context_user["content"]),
        "timestamp": 600.0,
        "api_content": api_content + " ",
        "_state_db_row_id": 1,
    }
    reconciled = models.reconciled_state_db_messages_for_session(
        session,
        state_messages=[conflicting],
    )
    assert any(message.get("content") == conflicting["content"] for message in reconciled)


def test_ambiguous_state_db_mirror_identity_is_not_suppressed():
    import api.models as models

    session, identity, api_content = _settle_image_turn(
        text="Describe this image",
        extra_text=(RECALL_NOTE,),
        timestamp=700.0,
    )
    context_user = next(
        message for message in session.context_messages
        if message.get("_active_turn_token") == identity["token"]
    )
    mirror = _durable_agent_content(context_user["content"])
    state_rows = [
        {
            "role": "user",
            "content": mirror,
            "timestamp": 700.0,
            "api_content": api_content,
            "_state_db_row_id": row_id,
        }
        for row_id in (11, 12)
    ]
    reconciled = models.reconciled_state_db_messages_for_session(
        session,
        state_messages=state_rows,
    )
    assert any(message.get("content") == mirror for message in reconciled)


def test_state_db_image_projection_suppresses_only_exact_agent_row_id(
    monkeypatch, tmp_path
):
    import api.models as models

    timestamp = 800.0
    session, identity, _ = _settle_image_turn(
        text="Describe this image",
        queued_notifications=("Queued process update: sample job completed.",),
        extra_text=(RECALL_NOTE, PLUGIN_NOTE),
        timestamp=timestamp,
        agent_row_id=41,
    )
    context_user = next(
        message for message in session.context_messages
        if message.get("_active_turn_token") == identity["token"]
    )
    mirror = _durable_agent_content(context_user["content"])

    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE messages ("
            "id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, "
            "timestamp REAL, active INTEGER DEFAULT 1, api_content TEXT)"
        )
        conn.executemany(
            "INSERT INTO messages (id, session_id, role, content, timestamp, api_content) "
            "VALUES (?, ?, ?, ?, ?, NULL)",
            [
                (41, session.session_id, "user", mirror, timestamp),
                (42, session.session_id, "user", mirror, timestamp),
            ],
        )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)
    state_rows = models.get_state_db_session_messages(session.session_id)
    assert [message["_state_db_row_id"] for message in state_rows] == [41, 42]
    assert all("api_content" not in message for message in state_rows)

    reconciled = models.reconciled_state_db_messages_for_session(
        session,
        state_messages=state_rows,
    )
    visible_literals = [
        message for message in reconciled
        if message.get("role") == "user" and message.get("content") == mirror
    ]
    assert len(visible_literals) == 1
    assert visible_literals[0]["_state_db_row_id"] == 42
    public_literals = [
        message for message in public_session_projection({"messages": reconciled})["messages"]
        if message.get("role") == "user" and message.get("content") == mirror
    ]
    assert len(public_literals) == 1
    assert "_state_db_row_id" not in public_literals[0]

    reconciled_context = models.reconciled_state_db_messages_for_session(
        session,
        prefer_context=True,
        state_messages=state_rows,
    )
    session.messages = reconciled
    session.context_messages = reconciled_context
    first_api_transcript = public_session_projection(
        {"messages": session.messages}
    )["messages"]
    first_next_replay = _new_turn_context_from_messages(
        session.context_messages,
        "Tell me more",
    )

    reconciled_again = models.reconciled_state_db_messages_for_session(
        session,
        state_messages=state_rows,
    )
    reconciled_context_again = models.reconciled_state_db_messages_for_session(
        session,
        prefer_context=True,
        state_messages=state_rows,
    )
    for messages in (reconciled_again, reconciled_context_again):
        assert sum(
            message.get("_state_db_row_id") == 42
            for message in messages
        ) == 1
    assert public_session_projection(
        {"messages": reconciled_again}
    )["messages"] == first_api_transcript
    assert _new_turn_context_from_messages(
        reconciled_context_again,
        "Tell me more",
    ) == first_next_replay

    for untrusted_identity in (
        {key: value for key, value in state_rows[0].items() if key != "_state_db_row_id"},
        {**state_rows[0], "_row_id": 42},
    ):
        unresolved = models.merge_session_messages_append_only(
            session.messages,
            models._suppress_native_image_display_mirrors(
                session,
                [untrusted_identity],
            ),
            incoming_provenance="state_db",
        )
        assert any(message.get("content") == mirror for message in unresolved)
        assert not any(
            "_webui_unmatched_native_image_mirror" in message
            for message in unresolved
        )


def test_marked_native_image_mirror_repairs_malformed_sidecar_once():
    import api.models as models

    timestamp = 850.0
    session, identity, api_content = _settle_image_turn(
        timestamp=timestamp,
        agent_row_id=41,
    )
    context_user = next(
        message for message in session.context_messages
        if message.get("_active_turn_token") == identity["token"]
    )
    mirror = _durable_agent_content(context_user["content"])
    session.messages.append({
        "role": "user",
        "content": mirror,
        "timestamp": timestamp,
        "_state_db_row_id": 42,
        "api_content": [],
    })
    state_row = {
        "role": "user",
        "content": mirror,
        "timestamp": timestamp,
        "_state_db_row_id": 42,
        "api_content": api_content,
    }

    marked = models._suppress_native_image_display_mirrors(session, [state_row])
    assert len(marked) == 1
    assert marked[0]["_webui_unmatched_native_image_mirror"] is True

    for _ in range(2):
        reconciled = models.reconciled_state_db_messages_for_session(
            session,
            state_messages=[state_row],
        )
        replay_rows = [
            message for message in _sanitize_messages_for_agent(reconciled)
            if message.get("content") == mirror
        ]
        assert len(replay_rows) == 1
        assert replay_rows[0]["api_content"] == api_content


@pytest.mark.parametrize(
    ("sidecar_payload", "state_payload"),
    [
        ("OLDER-SIDECAR-BYTES", "NEWER-DB-BYTES"),
        ("NEWER-SIDECAR-BYTES", "OLDER-DB-BYTES"),
    ],
)
def test_marked_native_image_mirror_conflict_stays_bounded_across_recovery(
    sidecar_payload, state_payload,
):
    import api.models as models

    timestamp = 860.0
    session, identity, image_api_content = _settle_image_turn(
        timestamp=timestamp,
        agent_row_id=41,
    )
    context_user = next(
        message for message in session.context_messages
        if message.get("_active_turn_token") == identity["token"]
    )
    mirror = _durable_agent_content(context_user["content"])
    display_attachments = [{
        "name": "sidecar-owned.png",
        "mime": "image/png",
        "is_image": True,
    }]
    sidecar_mirror_row = {
        "role": "user",
        "content": mirror,
        "timestamp": timestamp,
        "_state_db_row_id": 42,
        "api_content": sidecar_payload,
        "attachments": display_attachments,
    }
    session.messages.append(dict(sidecar_mirror_row))
    session.context_messages.append(dict(sidecar_mirror_row))
    state_row = {
        "role": "user",
        "content": mirror,
        "timestamp": timestamp,
        "_state_db_row_id": 42,
        "api_content": state_payload,
    }
    marked = models._suppress_native_image_display_mirrors(session, [state_row])
    assert marked[0]["_webui_unmatched_native_image_mirror"] is True

    first_public = None
    first_replay = None
    for _ in range(4):
        display = models.reconciled_state_db_messages_for_session(
            session,
            state_messages=[state_row],
        )
        context = models.reconciled_state_db_messages_for_session(
            session,
            prefer_context=True,
            state_messages=[state_row],
        )
        display_row_42 = [
            message for message in display
            if message.get("_state_db_row_id") == 42
        ]
        context_row_42 = [
            message for message in context
            if message.get("_state_db_row_id") == 42
        ]
        assert len(display_row_42) == 1
        assert display_row_42[0]["api_content"] == sidecar_payload
        assert display_row_42[0]["attachments"] == display_attachments
        assert len(context_row_42) == 2
        assert {message["api_content"] for message in context_row_42} == {
            sidecar_payload, state_payload,
        }

        public = public_session_projection({"messages": display})["messages"]
        public_mirrors = [message for message in public if message.get("content") == mirror]
        assert len(public_mirrors) == 1
        assert all("api_content" not in message for message in public_mirrors)
        assert public_mirrors[0]["attachments"] == display_attachments
        display_image_turn = next(
            message for message in display
            if message.get("_active_turn_token") == identity["token"]
        )
        assert display_image_turn["attachments"][0]["name"] == "sample.png"
        public_image_turn = next(
            message for message in public
            if message.get("content") == "Describe this image"
        )
        assert public_image_turn["attachments"][0]["name"] == "sample.png"

        replay = _sanitize_messages_for_agent(context)
        replay_row_42 = [message for message in replay if message.get("content") == mirror]
        assert len(replay_row_42) == 2
        assert {message["api_content"] for message in replay_row_42} == {
            sidecar_payload, state_payload,
        }
        live_row_42 = [
            message for message in session.messages
            if message.get("_state_db_row_id") == 42
        ]
        assert len(live_row_42) == 1
        assert live_row_42[0]["api_content"] == sidecar_payload
        replay_image_turn = next(
            message for message in replay
            if isinstance(message.get("content"), list)
            and any(
                part.get("type") == "image_url"
                and part.get("image_url", {}).get("url") == IMAGE_A
                for part in message["content"]
                if isinstance(part, dict)
            )
        )
        assert replay_image_turn["api_content"] == image_api_content
        if first_public is None:
            first_public = public
            first_replay = replay
        else:
            assert public == first_public
            assert replay == first_replay
        session.messages = display
        session.context_messages = context


def test_native_image_payload_conflict_with_distinct_visible_text_stays_separate():
    import api.models as models

    timestamp = 865.0
    session, identity, _ = _settle_image_turn(timestamp=timestamp, agent_row_id=41)
    context_user = next(
        message for message in session.context_messages
        if message.get("_active_turn_token") == identity["token"]
    )
    mirror = _durable_agent_content(context_user["content"])
    sidecar_row = {
        "role": "user",
        "content": mirror,
        "timestamp": timestamp,
        "_state_db_row_id": 42,
        "api_content": "SIDECAR-PAYLOAD",
    }
    session.messages.append(sidecar_row)
    state_row = {
        "role": "user",
        "content": "A separate visible submission",
        "timestamp": timestamp,
        "_state_db_row_id": 42,
        "api_content": "STATE-DB-PAYLOAD",
        "_webui_unmatched_native_image_mirror": True,
    }

    display = models.reconciled_state_db_messages_for_session(
        session,
        state_messages=[state_row],
    )
    same_row = [
        message for message in display
        if message.get("_state_db_row_id") == 42
    ]
    assert {message["content"] for message in same_row} == {
        mirror,
        "A separate visible submission",
    }


def test_marked_native_image_conflict_keeps_ambiguous_sidecar_row_id_visible():
    import api.models as models

    timestamp = 867.0
    session, identity, _ = _settle_image_turn(timestamp=timestamp, agent_row_id=41)
    context_user = next(
        message for message in session.context_messages
        if message.get("_active_turn_token") == identity["token"]
    )
    mirror = _durable_agent_content(context_user["content"])
    sidecar_rows = [
        {
            "role": "user",
            "content": mirror,
            "timestamp": timestamp,
            "_state_db_row_id": 42,
            "api_content": "SIDECAR-PAYLOAD-ONE",
            "attachments": [{"name": "first.png"}],
        },
        {
            "role": "user",
            "content": mirror,
            "timestamp": timestamp,
            "_state_db_row_id": 42,
            "api_content": "SIDECAR-PAYLOAD-TWO",
            "attachments": [{"name": "second.png"}],
        },
    ]
    session.messages.extend(sidecar_rows)
    state_row = {
        "role": "user",
        "content": mirror,
        "timestamp": timestamp,
        "_state_db_row_id": 42,
        "api_content": "STATE-DB-PAYLOAD",
    }
    marked = models._suppress_native_image_display_mirrors(session, [state_row])
    assert marked[0]["_webui_unmatched_native_image_mirror"] is True

    display = models.reconciled_state_db_messages_for_session(
        session,
        state_messages=[state_row],
    )
    same_row = [
        message for message in display
        if message.get("_state_db_row_id") == 42
    ]
    assert len(same_row) == 3
    assert {message["api_content"] for message in same_row} == {
        "SIDECAR-PAYLOAD-ONE",
        "SIDECAR-PAYLOAD-TWO",
        "STATE-DB-PAYLOAD",
    }
    assert [message.get("attachments") for message in same_row[:2]] == [
        [{"name": "first.png"}],
        [{"name": "second.png"}],
    ]


def test_marked_native_image_rows_with_distinct_ids_survive_recovery():
    import api.models as models

    timestamp = 870.0
    session, identity, api_content = _settle_image_turn(
        timestamp=timestamp,
        agent_row_id=41,
    )
    context_user = next(
        message for message in session.context_messages
        if message.get("_active_turn_token") == identity["token"]
    )
    mirror = _durable_agent_content(context_user["content"])
    state_rows = [
        {
            "role": "user",
            "content": mirror,
            "timestamp": timestamp,
            "_state_db_row_id": row_id,
            "api_content": api_content,
        }
        for row_id in (42, 43)
    ]

    first_public = None
    first_replay = None
    for _ in range(4):
        display = models.reconciled_state_db_messages_for_session(
            session,
            state_messages=state_rows,
        )
        context = models.reconciled_state_db_messages_for_session(
            session,
            prefer_context=True,
            state_messages=state_rows,
        )
        for messages in (display, context):
            rows = [
                message for message in messages
                if message.get("_state_db_row_id") in (42, 43)
            ]
            assert [message["_state_db_row_id"] for message in rows] == [42, 43]
            assert all(message["api_content"] == api_content for message in rows)

        public = public_session_projection({"messages": display})["messages"]
        replay = _sanitize_messages_for_agent(context)
        if first_public is None:
            first_public = public
            first_replay = replay
            assert sum(message.get("content") == mirror for message in public) == 2
        else:
            assert public == first_public
            assert replay == first_replay
        session.messages = display
        session.context_messages = context


@pytest.mark.parametrize(
    ("row_identity", "copies"),
    [
        pytest.param({}, 1, id="idless-row"),
        pytest.param({}, 2, id="duplicate-idless-rows"),
        pytest.param({"_state_db_row_id": "x"}, 1, id="malformed-row-id"),
        pytest.param(
            {"_row_id": 42, "_state_db_row_id": 43},
            1,
            id="conflicting-row-id-aliases",
        ),
    ],
)
def test_untrusted_native_image_row_identity_deduplicates_stably(
    row_identity, copies
):
    import api.models as models

    timestamp = 880.0
    session, identity, api_content = _settle_image_turn(
        timestamp=timestamp,
        agent_row_id=41,
    )
    context_user = next(
        message for message in session.context_messages
        if message.get("_active_turn_token") == identity["token"]
    )
    mirror = _durable_agent_content(context_user["content"])
    state_rows = [
        {
            "role": "user",
            "content": mirror,
            "timestamp": timestamp,
            "api_content": api_content,
            **row_identity,
        }
        for _ in range(copies)
    ]

    first_public = None
    first_replay = None
    for _ in range(4):
        display = models.reconciled_state_db_messages_for_session(
            session,
            state_messages=state_rows,
        )
        context = models.reconciled_state_db_messages_for_session(
            session,
            prefer_context=True,
            state_messages=state_rows,
        )
        for messages in (display, context):
            mirrored_rows = [
                message for message in messages
                if message.get("content") == mirror
                and message.get("timestamp") == timestamp
            ]
            assert len(mirrored_rows) == 1

        public = public_session_projection({"messages": display})["messages"]
        public_mirrors = [
            message for message in public
            if message.get("content") == mirror
            and message.get("timestamp") == timestamp
        ]
        assert len(public_mirrors) == 1
        assert "api_content" not in public_mirrors[0]
        assert all("api_content" not in message for message in public)
        replay = _sanitize_messages_for_agent(context)
        if first_public is None:
            first_public = public
            first_replay = replay
        else:
            assert public == first_public
            assert replay == first_replay
        session.messages = display
        session.context_messages = context


def test_unlinked_state_db_image_projection_uses_existing_reconciliation():
    import api.models as models

    for prefer_context in (False, True):
        for flushed in (False, True):
            timestamp = 900.0
            session, identity, _ = _settle_image_turn(
                timestamp=timestamp,
                extra_text=(RECALL_NOTE, PLUGIN_NOTE),
                agent_row_id=None,
            )
            context_user = next(
                message for message in session.context_messages
                if message.get("_active_turn_token") == identity["token"]
            )
            assert not any(
                key in context_user
                for key in ("_row_id", "_state_db_row_id", "_db_row_id", "state_db_row_id")
            )
            state_rows = (
                [{
                    "role": "user",
                    "content": _durable_agent_content(context_user["content"]),
                    "timestamp": timestamp,
                    "_state_db_row_id": 1,
                }]
                if flushed
                else []
            )

            recovered = models.reconciled_state_db_messages_for_session(
                session,
                prefer_context=prefer_context,
                state_messages=state_rows,
            )
            users = [
                message for message in recovered
                if message.get("role") == "user" and message.get("timestamp") == timestamp
            ]
            assert len(users) == 1
            display_users = [
                message for message in session.messages
                if message.get("role") == "user" and message.get("timestamp") == timestamp
            ]
            assert len(display_users) == 1
            assert display_users[0]["content"] == "Describe this image"
            assert users[0]["content"] == (
                context_user["content"] if prefer_context else "Describe this image"
            )
            if prefer_context:
                next_turn = _new_turn_context_from_messages(
                    recovered,
                    "Tell me more",
                )
                replay_users = [message for message in next_turn if message.get("role") == "user"]
                assert len(replay_users) == 1
                assert replay_users[0]["content"] == context_user["content"]


def test_agent_index_and_turn_id_do_not_claim_unrelated_user_row():
    _, identity, _ = _settle_image_turn(text="Describe this image")
    unrelated = [{"role": "user", "content": "Different submitted text"}]
    identity["current_turn_user_idx"] = 0
    assert _find_active_turn_checkpoint_index(
        unrelated,
        [],
        identity,
        "Describe this image",
    ) is None

    matching = [{
        "role": "user",
        "content": _native_user_content(
            "Describe this image", IMAGE_A, (RECALL_NOTE, PLUGIN_NOTE)
        ),
    }]
    assert _find_active_turn_checkpoint_index(
        matching,
        [],
        identity,
        "Describe this image",
    ) == 0
