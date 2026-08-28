from collections import OrderedDict
from types import SimpleNamespace

import pytest

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


def test_squash_projection_preserves_distinct_state_only_rows_after_watermark():
    sidecar_messages, cli_messages = _production_squash_projection()
    cli_messages.insert(
        2,
        _message(
            "assistant",
            "réponse disponible uniquement dans state.db",
            250.0,
            message_id="state-only-250",
        ),
    )
    session = SimpleNamespace(
        session_id="production-squash-state-only-tail",
        messages=sidecar_messages,
        session_source="webui",
        parent_session_id=None,
        truncation_watermark=200.0,
        truncation_boundary=200.0,
        compression_anchor_mode="manual",
    )

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert [message["content"] for message in merged] == [
        "# Session compactée\n\nRésumé opérationnel vérifié.",
        "réponse disponible uniquement dans state.db",
        "nouvelle demande",
        "nouvelle réponse",
    ]


@pytest.mark.parametrize(
    ("generation", "cutoff"),
    [("", 200.0), ("not-a-uuid", 200.0), ("0123456789ab4cde8f0123456789abcd", 199.0)],
)
def test_malformed_squash_projection_authority_fails_closed(generation, cutoff):
    sidecar_messages, cli_messages = _production_squash_projection()
    cli_messages.insert(
        2,
        _message("assistant", "state-only row", 250.0, message_id="state-only-invalid"),
    )
    session = SimpleNamespace(
        session_id="invalid-squash-authority",
        messages=[sidecar_messages[0]],
        session_source="webui",
        parent_session_id=None,
        truncation_watermark=200.0,
        truncation_boundary=200.0,
        squash_projection_generation=generation,
        squash_projection_cutoff=cutoff,
        squash_projection_superseded_by=None,
    )

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert merged == [sidecar_messages[0]]


@pytest.mark.parametrize("operation", ["truncate", "retry", "undo"])
def test_post_squash_intentional_shrink_supersedes_state_projection(
    operation, monkeypatch, tmp_path
):
    from api import models, session_ops

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "_write_session_index", lambda *_args, **_kwargs: None)

    sidecar_messages, cli_messages = _production_squash_projection()
    cli_messages.insert(
        2,
        _message(
            "assistant",
            "state-only row removed by later shrink",
            250.0,
            message_id="state-only-250",
        ),
    )
    session = Session(
        session_id=f"squash-{operation}",
        workspace=str(tmp_path),
        messages=sidecar_messages,
        context_messages=list(sidecar_messages),
        session_source="webui",
        truncation_watermark=200.0,
        truncation_boundary=200.0,
        compression_anchor_mode="manual",
    )
    session.save(skip_index=True, touch_updated_at=False)
    active_projection_generation = session.squash_projection_generation
    assert active_projection_generation

    if operation == "truncate":
        session_ops.truncate_session_at_keep(session, 1)
        session.save(skip_index=True, touch_updated_at=False)
    else:
        store = OrderedDict([(session.session_id, session)])
        monkeypatch.setattr(session_ops, "SESSIONS", store)
        monkeypatch.setattr(session_ops, "get_session", lambda _sid: session)
        getattr(session_ops, f"{operation}_last")(session.session_id)

    reloaded = Session.load(session.session_id)
    assert reloaded is not None
    assert reloaded.squash_projection_generation == active_projection_generation
    assert reloaded.squash_projection_superseded_by == reloaded.intentional_shrink_generation
    assert reloaded.squash_projection_cutoff == 200.0

    merged = routes._merged_session_messages_for_display(reloaded, cli_messages)

    assert [message["content"] for message in merged] == [
        "# Session compactée\n\nRésumé opérationnel vérifié."
    ]


def test_branch_after_post_squash_truncate_uses_superseded_coordinates(
    monkeypatch, tmp_path
):
    from api.session_ops import truncate_session_at_keep

    sidecar_messages, cli_messages = _production_squash_projection()
    cli_messages.insert(
        2,
        _message(
            "assistant",
            "state-only row removed before branch",
            250.0,
            message_id="state-only-branch-250",
        ),
    )
    source = Session(
        session_id="post-squash-truncate-branch",
        title="Squashed then truncated source",
        workspace=str(tmp_path),
        messages=sidecar_messages,
        context_messages=list(sidecar_messages),
        session_source="webui",
        truncation_watermark=200.0,
        truncation_boundary=200.0,
        compression_anchor_mode="manual",
        squash_projection_generation="0123456789ab4cde8f0123456789abcd",
    )
    truncate_session_at_keep(source, 1)
    branch_store = OrderedDict()
    response = {}

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        routes,
        "read_body",
        lambda _handler: {"session_id": source.session_id, "keep_count": 1},
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
        "# Session compactée\n\nRésumé opérationnel vérifié."
    ]
