"""Route-level behavioral tests for issue #6610."""

from collections import OrderedDict
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import patch


class _FakeSession:
    def __init__(self, messages, *, read_only=False, profile="default"):
        self.session_id = "issue6610-session"
        self.messages = messages
        self.read_only = read_only
        self.is_cli_session = False
        self.source_tag = "webui"
        self.raw_source = "webui"
        self.session_source = "webui"
        self.profile = profile
        self.active_stream_id = None
        self.pending_user_message = None
        self.pending_started_at = None
        self.has_pending_user_message = False
        self.path = Path("issue6610-session.json")
        self.save_count = 0

    def save(self):
        self.save_count += 1

    def compact(self):
        return {
            "session_id": self.session_id,
            "message_count": len(self.messages),
            "messages": self.messages,
        }


def _post(session, body):
    import api.routes as routes

    captured = {}

    def fake_response(_handler, data, status=200, extra_headers=None):
        captured["data"] = data
        captured["status"] = status
        return True

    handler = SimpleNamespace(headers={})
    parsed = urlparse("/api/session/message/dismiss-error")
    with patch.object(routes, "read_body", return_value=body), \
         patch.object(routes, "_check_csrf", return_value=True), \
         patch.object(routes, "_guard_request_session_visibility", return_value=True), \
         patch.object(routes, "_get_or_materialize_session", return_value=session), \
         patch.object(routes, "_session_visible_to_active_profile", return_value=True), \
         patch.object(routes, "_get_session_agent_lock", return_value=nullcontext()), \
         patch("api.config._evict_session_agent"), \
         patch.object(routes, "j", side_effect=fake_response), \
         patch.object(routes, "bad", side_effect=fake_response):
        handled = routes.handle_post(handler, parsed)
    captured["handled"] = handled
    return captured


def _message(content="provider failed", **extra):
    row = {
        "role": "assistant",
        "content": content,
        "_error": True,
        "provider_details": "quota",
        "timestamp": 7,
    }
    row.update(extra)
    return row


def test_route_dismisses_exact_row_and_preserves_neighbors():
    messages = [
        {"role": "user", "content": "keep me"},
        _message(),
        {"role": "assistant", "content": "later answer"},
    ]
    session = _FakeSession(messages)
    result = _post(
        session,
        {
            "session_id": session.session_id,
            "message_index": 1,
            "expected_message": {"role": "assistant", "content": "provider failed", "timestamp": 7},
        },
    )
    assert result["handled"] is True
    assert result["status"] == 200
    assert messages[1]["_dismissed"] is True
    assert messages[0] == {"role": "user", "content": "keep me"}
    assert messages[2] == {"role": "assistant", "content": "later answer"}
    assert session.save_count == 1


def test_route_projects_returned_session_through_public_response_scrubber():
    import api.routes as routes

    session = _FakeSession([_message()])
    body = {
        "session_id": session.session_id,
        "message_index": 0,
        "expected_message": {"role": "assistant", "content": "provider failed", "timestamp": 7},
    }
    with patch.object(routes, "public_session_projection", return_value={"scrubbed": True}) as projection:
        result = _post(session, body)
    assert result["data"]["session"] == {"scrubbed": True}
    projection.assert_called_once()


def test_stale_expected_row_is_rejected_without_writing():
    messages = [_message()]
    session = _FakeSession(messages)
    result = _post(
        session,
        {
            "session_id": session.session_id,
            "message_index": 0,
            "expected_message": {"role": "assistant", "content": "different", "timestamp": 7},
        },
    )
    assert result["status"] == 409
    assert "_dismissed" not in messages[0]
    assert session.save_count == 0


def test_control_rows_are_rejected_without_writing():
    for control in (
        {"provider_details_label": "Cancellation details"},
        {"type": "interrupted"},
        {"recovery_control": True},
        {"_compressionRecovery": {"kind": "retry"}},
    ):
        message = _message(**control)
        session = _FakeSession([message])
        result = _post(
            session,
            {
                "session_id": session.session_id,
                "message_index": 0,
                "expected_message": {"role": "assistant", "content": "provider failed", "timestamp": 7},
            },
        )
        assert result["status"] == 409
        assert session.save_count == 0


def test_repeated_request_is_idempotent():
    session = _FakeSession([_message()])
    body = {
        "session_id": session.session_id,
        "message_index": 0,
        "expected_message": {"role": "assistant", "content": "provider failed", "timestamp": 7},
    }
    assert _post(session, body)["status"] == 200
    assert _post(session, body)["status"] == 200
    assert session.save_count == 1


def test_read_only_and_busy_sessions_are_fail_closed():
    read_only = _FakeSession([_message()], read_only=True)
    body = {
        "session_id": read_only.session_id,
        "message_index": 0,
        "expected_message": {"role": "assistant", "content": "provider failed", "timestamp": 7},
    }
    assert _post(read_only, body)["status"] == 403
    assert read_only.save_count == 0

    busy = _FakeSession([_message()])
    busy.active_stream_id = "run-1"
    assert _post(busy, body)["status"] == 409
    assert busy.save_count == 0


def test_malformed_indexes_are_rejected_before_session_load():
    session = _FakeSession([_message()])
    base = {
        "session_id": session.session_id,
        "expected_message": {"role": "assistant", "content": "provider failed", "timestamp": 7},
    }
    for value in (-1, 1_000_001, 1.5, None):
        body = dict(base, message_index=value)
        result = _post(session, body)
        assert result["status"] == 400
    assert session.save_count == 0


def test_provider_error_classifier_rejects_ambiguous_control_rows():
    import api.routes as routes

    assert routes._is_provider_error_card_message(_message()) is True
    assert routes._is_provider_error_card_message(
        {"role": "assistant", "content": "**Goal command failed:** local", "_error": True}
    ) is False
    assert routes._is_provider_error_card_message(
        {"role": "assistant", "content": "**Task cancelled:** Task cancelled.", "_error": True}
    ) is False
    assert routes._is_provider_error_card_message(
        {"role": "assistant", "content": "gateway failed", "_error": True, "_provider_error_type": "gateway_error"}
    ) is True


def test_external_missing_sidecar_is_rejected_before_materialization():
    import api.routes as routes

    with patch.object(routes, "get_session", side_effect=KeyError), \
         patch.object(routes, "_lookup_cli_session_metadata", return_value={"session_id": "cli", "source_tag": "cli"}), \
         patch.object(routes, "_state_db_session_source", return_value="cli"), \
         patch.object(routes, "_session_is_subagent_view_only", return_value=False):
        assert routes._dismiss_error_source_rejection("cli", SimpleNamespace()) == (
            "Read-only imported sessions cannot be modified",
            403,
        )


def test_real_route_persists_dismissal_and_reload_preserves_neighbors(tmp_path, monkeypatch):
    import api.models as models
    import api.routes as routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    store = OrderedDict()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "SESSIONS", store)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(routes, "SESSIONS", store)

    session = models.Session(
        session_id="issue6610-real",
        profile="default",
        messages=[
            {"role": "user", "content": "keep"},
            _message(),
            {"role": "assistant", "content": "later"},
        ],
    )
    session.save()
    captured = {}

    def response(_handler, data, status=200, extra_headers=None):
        captured.update(data=data, status=status)
        return True

    handler = SimpleNamespace(headers={})
    body = {
        "session_id": session.session_id,
        "message_index": 1,
        "expected_message": {"role": "assistant", "content": "provider failed", "timestamp": 7},
    }
    with patch.object(routes, "read_body", return_value=body), \
         patch.object(routes, "_check_csrf", return_value=True), \
         patch.object(routes, "_guard_request_session_visibility", return_value=True), \
         patch.object(routes, "j", side_effect=response), \
         patch.object(routes, "bad", side_effect=response), \
         patch("api.config._evict_session_agent"):
        assert routes.handle_post(handler, urlparse("/api/session/message/dismiss-error")) is True

    assert captured["status"] == 200
    reloaded = models.Session.load(session.session_id)
    assert reloaded is not None
    assert reloaded.messages[1]["_dismissed"] is True
    assert reloaded.messages[0] == {"role": "user", "content": "keep"}
    assert reloaded.messages[2] == {"role": "assistant", "content": "later"}
