from collections import OrderedDict
from types import SimpleNamespace

from api import routes
from api.models import Session


def _message(role, content, timestamp, message_id=None, **extra):
    message = {
        "role": role,
        "content": content,
        "timestamp": timestamp,
    }
    if message_id is not None:
        message["id"] = message_id
    message.update(extra)
    return message


def test_squash_summary_watermark_blocks_older_state_db_replay():
    from api.routes import _merged_session_messages_for_display

    summary = _message(
        "assistant",
        "# Session compactée\n\nRésumé opérationnel vérifié.",
        200.0,
        message_id="squash-200",
        _squash_summary=True,
    )
    session = SimpleNamespace(
        session_id="fd05-copy",
        messages=[summary],
        session_source="webui",
        parent_session_id=None,
        truncation_watermark=200.0,
        truncation_boundary=200.0,
        compression_anchor_mode="manual",
    )
    state_db_messages = [
        _message("user", "ancien prompt", 100.0, message_id=1),
        _message("assistant", "ancienne réponse", 110.0, message_id=2),
    ]

    merged = _merged_session_messages_for_display(session, state_db_messages)

    assert merged == [summary]


def test_non_squash_short_sidecar_keeps_existing_merge_behavior():
    from api.routes import _merged_session_messages_for_display

    sidecar = _message("assistant", "nouvelle réponse", 200.0, message_id=3)
    session = SimpleNamespace(
        session_id="ordinary-session",
        messages=[sidecar],
        session_source="webui",
        parent_session_id=None,
        truncation_watermark=None,
        truncation_boundary=None,
        compression_anchor_mode=None,
    )
    state_db_messages = [
        _message("user", "ancien prompt", 100.0, message_id=1),
        _message("assistant", "ancienne réponse", 110.0, message_id=2),
    ]

    merged = _merged_session_messages_for_display(session, state_db_messages)

    assert [message["id"] for message in merged] == [1, 2, 3]


def _production_squash_projection():
    summary = _message(
        "assistant",
        "# Session compactée\n\nRésumé opérationnel vérifié.",
        200.0,
        message_id="squash-200",
        _squash_summary=True,
    )
    post_user = _message("user", "nouvelle demande", 300.0, message_id=3)
    post_assistant = _message("assistant", "nouvelle réponse", 310.0, message_id=4)
    sidecar_messages = [summary, post_user, post_assistant]
    cli_messages = [
        _message("user", "ancien prompt", 100.0, message_id=1),
        _message("assistant", "ancienne réponse", 110.0, message_id=2),
        dict(post_user),
        dict(post_assistant),
    ]
    return sidecar_messages, cli_messages


def test_squash_summary_with_tail_keeps_projection_authoritative_over_longer_cli_state():
    sidecar_messages, cli_messages = _production_squash_projection()
    session = SimpleNamespace(
        session_id="production-squash-tail",
        messages=sidecar_messages,
        session_source="webui",
        parent_session_id=None,
        truncation_watermark=200.0,
        truncation_boundary=200.0,
        compression_anchor_mode="manual",
    )

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    contents = [message["content"] for message in merged]
    assert contents == [
        "# Session compactée\n\nRésumé opérationnel vérifié.",
        "nouvelle demande",
        "nouvelle réponse",
    ]
    assert "ancien prompt" not in contents
    assert "ancienne réponse" not in contents
    assert contents.count("nouvelle demande") == 1
    assert contents.count("nouvelle réponse") == 1


def test_branch_keep_count_uses_squash_authoritative_display_coordinates(monkeypatch, tmp_path):
    sidecar_messages, cli_messages = _production_squash_projection()
    source = Session(
        session_id="production-squash-branch",
        title="Squashed source",
        workspace=str(tmp_path),
        messages=sidecar_messages,
        context_messages=list(sidecar_messages),
        session_source="webui",
        truncation_watermark=200.0,
        truncation_boundary=200.0,
        compression_anchor_mode="manual",
    )
    branch_store = OrderedDict()
    response = {}

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        routes,
        "read_body",
        lambda _handler: {"session_id": source.session_id, "keep_count": 2},
    )
    monkeypatch.setattr(routes, "_load_branch_source_or_refuse", lambda *_args: source)
    monkeypatch.setattr(routes, "_session_requires_cli_metadata_lookup", lambda _source: True)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda _sid: {})
    monkeypatch.setattr(routes, "_is_messaging_session_record", lambda _record: True)
    monkeypatch.setattr(routes, "get_cli_session_messages", lambda _sid: cli_messages)
    monkeypatch.setattr(Session, "save", lambda self, *args, **kwargs: None)
    monkeypatch.setattr(routes, "SESSIONS", branch_store)
    monkeypatch.setattr(routes, "_evict_sessions_over_cap", lambda: None)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_args, **_kwargs: None)

    def capture_json(_handler, payload, status=200, **_kwargs):
        response.update({"payload": payload, "status": status})
        return True

    monkeypatch.setattr(routes, "j", capture_json)

    handled = routes.handle_post(
        SimpleNamespace(),
        SimpleNamespace(path="/api/session/branch", query=""),
    )

    assert handled is True
    assert response["status"] == 200
    branch = branch_store[response["payload"]["session_id"]]
    assert [message["content"] for message in branch.messages] == [
        "# Session compactée\n\nRésumé opérationnel vérifié.",
        "nouvelle demande",
    ]
