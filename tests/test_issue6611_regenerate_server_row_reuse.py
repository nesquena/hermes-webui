"""Server-owned regeneration identity and settlement regressions for #6611."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

import api.models as models
import api.routes as routes
import api.streaming as streaming
from api.models import Session
from api.process_event_utils import build_active_turn_token


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "webui-PR-TARGET-6611-REPRO.json").read_text(
        encoding="utf-8"
    )
)


@pytest.fixture(autouse=True)
def isolated_sessions(monkeypatch, tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    yield
    models.SESSIONS.clear()


def _session(session_id="regen-server", messages=None):
    rows = copy.deepcopy(messages if messages is not None else FIXTURE["transcript"][:1])
    session = Session(
        session_id=session_id,
        messages=rows,
        context_messages=copy.deepcopy(rows),
        workspace=str(Path.cwd()),
        model="test-model",
    )
    session.save()
    models.SESSIONS[session_id] = session
    return session


def _target(session, row=None, *, display_index=None, display_keep_count=None):
    row = row or session.messages[-1]
    display = routes._merged_session_messages_for_display(session)
    if display_index is None:
        display_index = next(
            idx
            for idx, item in enumerate(display)
            if str(item.get("id")) == str(row.get("id"))
        )
    return {
        "session_id": session.session_id,
        "message_id": row["id"],
        "timestamp": row["timestamp"],
        "display_index": display_index,
        "display_keep_count": len(display) if display_keep_count is None else display_keep_count,
    }


def _prepare(session, target, *, stream_id="regen-stream", started_at=1700000100.875):
    return routes._prepare_chat_start_session_for_stream(
        session,
        msg="untrusted browser text",
        attachments=[],
        workspace=session.workspace,
        model=session.model,
        model_provider=None,
        stream_id=stream_id,
        started_at=started_at,
        regenerate_target=target,
    )


def test_issue_fixture_local_error_reuses_one_persisted_user_after_reload():
    session = _session(messages=FIXTURE["transcript"][:1])
    retained_before = copy.deepcopy(session.messages[0])
    _prepare(session, _target(session))

    assert streaming._materialize_pending_user_turn_before_error(session) is False
    session.messages.append(
        {
            "id": "terminal-error",
            "role": "assistant",
            "content": "provider failed",
            "timestamp": 1700000101.0,
            "_error": True,
        }
    )
    session.active_stream_id = None
    session.pending_user_message = None
    session.pending_started_at = None
    session.save()

    loaded = Session.load(session.session_id)
    users = [row for row in loaded.messages if row.get("role") == "user"]
    assert len(users) == 1
    for key, value in retained_before.items():
        assert users[0][key] == value


def test_local_success_settlement_reuses_token_owned_row_and_preserves_metadata():
    session = _session(messages=FIXTURE["attachment_transcript"][:1])
    retained_before = copy.deepcopy(session.messages[0])
    _prepare(session, _target(session))
    identity = streaming._active_turn_authority(session, "regen-stream", "describe this")
    previous_display = copy.deepcopy(session.messages)
    previous_context = copy.deepcopy(session.context_messages)
    result = copy.deepcopy(previous_context) + [
        {
            "id": "assistant-success",
            "role": "assistant",
            "content": "a screenshot",
            "timestamp": 1700000101.0,
        }
    ]

    streaming._settle_result_messages(
        session,
        previous_display,
        previous_context,
        result,
        "describe this",
        "webui",
        identity,
    )

    users = [row for row in session.messages if row.get("role") == "user"]
    assert len(users) == 1
    for key, value in retained_before.items():
        assert users[0][key] == value
    assert [row["content"] for row in session.messages if row.get("role") == "assistant"] == [
        "a screenshot"
    ]


def test_token_first_error_materialization_ignores_fresh_pending_timestamp():
    rows = copy.deepcopy(FIXTURE["earlier_identical_prompt_transcript"][:3])
    session = _session(messages=rows)
    target = _target(session, rows[-1])
    _prepare(session, target, started_at=1800000000.875)

    assert streaming._materialize_pending_user_turn_before_error(session) is False
    assert [row["id"] for row in session.messages if row.get("role") == "user"] == [
        "u-earlier",
        "u-current",
    ]


def test_restart_recovery_returns_retained_token_row_without_append():
    session = _session()
    _prepare(session, _target(session), stream_id="restart-stream", started_at=1900000000.625)
    before = copy.deepcopy(session.messages)

    recovered = models._append_recovered_pending_turn(session, timestamp=1900000000)

    assert recovered is session.messages[0]
    assert session.messages == before


def test_regeneration_binding_uses_server_row_content_attachments_and_source():
    session = _session(messages=FIXTURE["attachment_transcript"][:1])
    prepared = _prepare(session, _target(session))
    token = build_active_turn_token("regen-stream", 1700000100.875)

    assert prepared["message"] == "describe this"
    assert prepared["attachments"] == FIXTURE["attachment_transcript"][0]["attachments"]
    assert prepared["source"] == "webui"
    assert session.messages[0]["_active_turn_token"] == token
    assert session.context_messages[0]["_active_turn_token"] == token


@pytest.mark.parametrize(
    "mutate",
    [
        lambda target: target.update(session_id="other"),
        lambda target: target.update(message_id="missing"),
        lambda target: target.update(timestamp=0.25),
        lambda target: target.update(display_index=99),
        lambda target: target.pop("message_id"),
        lambda target: target.update(_active_turn_token="client-token"),
    ],
)
def test_stale_or_malformed_claims_return_conflict_without_mutation(mutate):
    session = _session()
    target = _target(session)
    mutate(target)
    before = copy.deepcopy(session.__dict__)

    with pytest.raises(routes._RegenerationTargetConflict) as exc_info:
        _prepare(session, target)

    assert exc_info.value.code == "stale_regeneration_target"
    assert session.__dict__ == before


def test_concurrent_writer_after_truncate_makes_claim_stale_without_append():
    session = _session()
    target = _target(session)
    session.messages.append(
        {"id": "concurrent", "role": "assistant", "content": "new", "timestamp": 180.0}
    )
    before = copy.deepcopy(session.messages)

    with pytest.raises(routes._RegenerationTargetConflict):
        _prepare(session, target)

    assert session.messages == before


def test_chat_start_seam_returns_typed_409_before_registering_stream():
    session = _session()
    target = _target(session)
    target["message_id"] = "stale-id"
    before = copy.deepcopy(session.__dict__)

    response = routes._start_chat_stream_for_session(
        session,
        msg="same prompt",
        attachments=[],
        workspace=session.workspace,
        model=session.model,
        regenerate_target=target,
    )

    assert response["_status"] == 409
    assert response["code"] == "stale_regeneration_target"
    assert session.__dict__ == before


def test_public_terminal_payload_strips_internal_token_but_disk_retains_it():
    session = _session()
    _prepare(session, _target(session))
    payload = streaming._session_payload_with_full_messages(session)

    assert "_active_turn_token" not in payload["messages"][0]
    assert "_active_turn_token" in session.messages[0]
    loaded = Session.load(session.session_id)
    assert "_active_turn_token" in loaded.messages[0]


def test_recovered_pending_user_rows_receive_regeneration_identity():
    session = _session(messages=[])
    session.pending_user_message = "failed prompt"
    session.pending_started_at = 1700000200.5
    session.pending_user_source = "webui"

    assert streaming._materialize_pending_user_turn_before_error(session) is True
    recovered = session.messages[0]
    assert recovered["role"] == "user"
    assert recovered["id"] is not None

    restart = _session(session_id="restart-recovered", messages=[])
    restart.pending_user_message = "restart prompt"
    restart.pending_started_at = 1700000300.5
    restart.pending_user_source = "webui"
    recovered = models._append_recovered_pending_turn(restart)
    assert recovered is restart.messages[0]
    assert recovered["id"] is not None


def test_display_mapping_includes_state_db_rows_used_by_session_get(monkeypatch):
    session = _session(
        session_id="state-db-display",
        messages=[{"id": "u1", "role": "user", "content": "one", "timestamp": 1.0}],
    )
    state_rows = [
        session.messages[0],
        {"id": "a1", "role": "assistant", "content": "answer", "timestamp": 2.0},
    ]
    monkeypatch.setattr(routes, "get_state_db_session_messages", lambda *args, **kwargs: state_rows)
    display = routes._regeneration_display_messages(session)
    assert [row["id"] for row in display] == ["u1", "a1"]
    target = {
        "session_id": session.session_id,
        "message_id": "u1",
        "timestamp": 1.0,
        "display_index": 0,
        "display_keep_count": 2,
    }
    assert routes._regeneration_target_row(session, target)["id"] == "u1"
    assert routes._display_keep_count_to_session_keep(session, 2, target) == 1


def test_display_truncate_response_strips_active_turn_token():
    session = _session()
    _prepare(session, _target(session))
    public = routes._public_session_messages(session.messages)
    assert "_active_turn_token" not in public[0]
    assert "_active_turn_token" in session.messages[0]


def test_get_session_route_redacts_internal_token(monkeypatch):
    session = _session()
    _prepare(session, _target(session))
    session.active_stream_id = None
    session.pending_user_message = None
    session.pending_attachments = []
    session.pending_started_at = None
    session.pending_user_source = None
    session.save()
    captured = {}
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload, status=status
        ),
    )
    handler = SimpleNamespace(_safe_webui_print=lambda *_args, **_kwargs: None)

    routes.handle_get(
        handler,
        SimpleNamespace(path="/api/session", query=f"session_id={session.session_id}&msg_limit=200"),
    )

    assert captured["status"] == 200
    assert len(captured["payload"]["session"]["messages"]) == 1
    assert "_active_turn_token" not in captured["payload"]["session"]["messages"][0]


def test_ordinary_prepare_still_checkpoints_one_fresh_user(monkeypatch):
    session = _session(messages=[])
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "eager")

    routes._prepare_chat_start_session_for_stream(
        session,
        msg="ordinary prompt",
        attachments=[],
        workspace=session.workspace,
        model=session.model,
        model_provider=None,
        stream_id="ordinary-stream",
        started_at=200.25,
    )

    assert [row["content"] for row in session.messages] == ["ordinary prompt"]
    assert session.messages[0]["timestamp"] == 200.25


def test_runtime_adapter_legacy_closure_forwards_regeneration_target(monkeypatch):
    import api.runtime_adapter as runtime_adapter

    session = _session()
    target = _target(session)
    captured = {}
    monkeypatch.setattr(runtime_adapter, "runtime_adapter_enabled", lambda: True)
    monkeypatch.setattr(runtime_adapter, "runtime_adapter_runner_enabled", lambda: False)
    monkeypatch.setattr(
        runtime_adapter,
        "build_runtime_adapter",
        lambda legacy_adapter_factory, runner_client_factory: legacy_adapter_factory(),
    )

    def fake_start(_session, **kwargs):
        captured.update(kwargs)
        return {"stream_id": "adapter-stream", "session_id": _session.session_id}

    monkeypatch.setattr(routes, "_start_chat_stream_for_session", fake_start)

    response = routes._start_run(
        session,
        msg="same prompt",
        attachments=[],
        workspace=session.workspace,
        model=session.model,
        model_provider=None,
        normalized_model=False,
        source="webui",
        route="/api/chat/start",
        regenerate_target=target,
    )

    assert response["stream_id"] == "adapter-stream"
    assert captured["regenerate_target"] == target


def test_runtime_adapter_runner_rejects_regeneration_claim(monkeypatch):
    import api.runtime_adapter as runtime_adapter

    session = _session(session_id="runner-regen")
    target = _target(session)
    monkeypatch.setattr(runtime_adapter, "runtime_adapter_enabled", lambda: False)
    monkeypatch.setattr(runtime_adapter, "runtime_adapter_runner_enabled", lambda: True)

    response = routes._start_run(
        session,
        msg="same prompt",
        attachments=[],
        workspace=session.workspace,
        model=session.model,
        model_provider=None,
        normalized_model=False,
        source="webui",
        route="/api/chat/start",
        regenerate_target=target,
    )

    assert response == {
        "error": "Regeneration is not supported by the runner backend.",
        "code": "stale_regeneration_target",
        "_status": 409,
    }


def test_local_settlement_deduplicates_echoed_user_after_intermediate_tool_rows():
    session = _session(messages=FIXTURE["transcript"][:1])
    _prepare(session, _target(session))
    session.messages.extend(
        [
            {"role": "assistant", "content": "tool call", "timestamp": 1700000101.0},
            {"role": "tool", "content": "tool result", "timestamp": 1700000102.0},
        ]
    )
    previous_display = copy.deepcopy(session.messages)
    previous_context = copy.deepcopy(session.context_messages)
    identity = streaming._active_turn_authority(session, "regen-stream", "same prompt")
    result = previous_context + [
        {"role": "user", "content": "same prompt", "timestamp": 1700000100.875},
        {"role": "assistant", "content": "new answer", "timestamp": 1700000103.0},
    ]

    streaming._settle_result_messages(
        session,
        previous_display,
        previous_context,
        result,
        "same prompt",
        "webui",
        identity,
    )

    assert len([row for row in session.messages if row.get("role") == "user"]) == 1


def test_provider_history_drops_token_row_without_touching_earlier_identical_prompt():
    rows = copy.deepcopy(FIXTURE["earlier_identical_prompt_transcript"][:3])
    session = _session(messages=rows)
    _prepare(session, _target(session, session.messages[-1]))
    identity = streaming._active_turn_authority(session, "regen-stream", "same prompt")

    history = streaming._new_turn_context_from_messages(
        session.context_messages,
        "same prompt",
        identity,
    )

    assert [row.get("id") for row in history if row.get("role") == "user"] == ["u-earlier"]
    assert all("_active_turn_token" not in row for row in history)
