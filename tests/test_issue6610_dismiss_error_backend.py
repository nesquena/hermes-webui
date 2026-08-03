import copy
import io
import json
from urllib.parse import urlparse

from api.models import Session
from api.transcript_mutations import (
    TranscriptProjection,
    admit_generated_provider_error,
    decorate_projection,
    materialize_duplicate,
    materialize_fork,
    project_transcript,
    projected_count_for_session,
    record_dismissal,
)


def _session(sid="source-6610"):
    return Session(
        session_id=sid,
        source_tag="webui",
        raw_source="webui",
        session_source="webui",
        messages=[],
        tool_calls=[],
    )


def _error(session, text="provider failure"):
    row = {"role": "assistant", "content": text, "_error": True}
    admit_generated_provider_error(row, session)
    return row


def test_projection_is_pure_and_rebases_tool_calls_after_dismissal():
    session = _session()
    error = _error(session)
    rows = [{"role": "user", "content": "keep"}, error, {"role": "assistant", "content": "later"}]
    session.messages = rows
    session.tool_calls = [{"name": "tool", "assistant_msg_idx": 2}]
    record_dismissal(session, session.session_id, error)
    before = copy.deepcopy((session.messages, session.tool_calls, session.transcript_dismissals))

    projection = project_transcript(session, session.messages)

    assert [row["content"] for row in projection.messages] == ["keep", "later"]
    assert projection.projected_count == 2
    assert projection.visible_to_raw == {0: 0, 1: 2}
    assert projection.raw_to_visible == {0: 0, 2: 1}
    assert projection.tool_calls == [{"name": "tool", "assistant_msg_idx": 1}]
    assert (session.messages, session.tool_calls, session.transcript_dismissals) == before
    assert "_provider_error_dismissal_capability" not in projection.messages[0]


def test_transport_decoration_is_separate_from_clean_projection():
    session = _session()
    row = _error(session)
    projection = project_transcript(session, [row])
    assert "_provider_error_dismissal_capability" not in projection.messages[0]
    decorated = decorate_projection(session, projection)
    assert decorated[0]["_provider_error_dismissal_capability"]
    assert "_provider_error_dismissal_capability" not in row


def test_duplicate_materialization_rehomes_only_admitted_errors_and_strips_transport():
    source = _session()
    row = _error(source)
    row["_generated_error_source_session_id"] = "parent-snapshot"
    projection = project_transcript(source, [row])
    decorated = decorate_projection(source, projection)
    duplicate_rows, duplicate_calls = materialize_duplicate(
        TranscriptProjection(decorated, 1, {0: 0}, {0: 0}, []),
        source_session_id=source.session_id,
        destination_session_id="copy-6610",
    )
    assert duplicate_rows[0]["_generated_error_source_session_id"] == "copy-6610"
    assert "_provider_error_dismissal_capability" not in duplicate_rows[0]
    assert duplicate_calls == []


def test_fork_materialization_strips_capability_and_preserves_parent_ownership():
    source = _session()
    row = _error(source)
    projection = project_transcript(source, [row])
    decorated = decorate_projection(source, projection)
    fork_rows, _ = materialize_fork(
        TranscriptProjection(decorated, 1, {0: 0}, {0: 0}, [])
    )
    assert fork_rows[0]["_generated_error_source_session_id"] == source.session_id
    assert "_provider_error_dismissal_capability" not in fork_rows[0]


def test_filtered_tool_anchor_fails_closed_without_retargeting():
    session = _session()
    error = _error(session)
    record_dismissal(session, session.session_id, error)
    projection = project_transcript(
        session,
        [error, {"role": "assistant", "content": "later"}],
        tool_calls=[{"name": "stale", "assistant_msg_idx": 0}],
    )
    assert projection.tool_calls == []


def test_pending_user_floor_is_shared_by_full_projection_and_compact():
    session = _session("pending-floor-6610")
    session.pending_user_message = "still pending"
    projection = project_transcript(session, [])
    assert projection.projected_count == 1
    assert projected_count_for_session(session, raw_count=0, messages=[]) == 1
    assert session.compact()["message_count"] == 1


def test_truncation_reconciles_removed_dismissal_identity():
    from api.session_ops import truncate_session_at_keep

    session = _session("truncate-6610")
    error = _error(session)
    session.messages = [
        {"role": "user", "content": "keep"},
        error,
        {"role": "assistant", "content": "later"},
    ]
    record_dismissal(session, session.session_id, error)
    assert session.transcript_dismissal_active_count == 1
    truncate_session_at_keep(session, 1)
    assert session.transcript_dismissal_active_count == 0
    assert session.transcript_dismissals["entries"][0]["active"] is False


def test_gateway_terminal_provider_error_uses_canonical_admission(monkeypatch, tmp_path):
    from api import gateway_chat, streaming

    session = _session("gateway-6610")
    session.workspace = str(tmp_path)
    session.model = "test"
    session.model_provider = "test"
    session.active_stream_id = "gateway-stream-6610"
    monkeypatch.setattr(gateway_chat, "get_session", lambda _sid: session)
    monkeypatch.setattr(gateway_chat, "_get_session_agent_lock", lambda _sid: __import__("contextlib").nullcontext())
    monkeypatch.setattr(streaming, "_classify_provider_error", lambda _error: {"type": "provider", "label": "Provider error", "hint": ""})
    monkeypatch.setattr(streaming, "_provider_error_payload", lambda _error, _kind, _hint: {"message": "failed"})
    monkeypatch.setattr(streaming, "_materialize_pending_user_turn_before_error", lambda _session: None)
    monkeypatch.setattr(streaming, "_snapshot_and_append_partial_on_error", lambda *_args: None)
    monkeypatch.setattr(streaming, "_terminal_turn_duration", lambda _session: None)
    monkeypatch.setattr(session, "save", lambda: None)

    result = gateway_chat._settle_gateway_terminal_error(
        session.session_id,
        session.active_stream_id,
        session.workspace,
        session.model,
        session.model_provider,
        RuntimeError("failed"),
    )
    row = session.messages[-1]
    assert result["terminal_session_persisted"] is True
    assert row["_generated_error_source_session_id"] == session.session_id
    assert row["_generated_error_provenance"]
    assert row["id"]


class _Handler:
    def __init__(self, payload):
        raw = json.dumps(payload).encode()
        self.rfile = io.BytesIO(raw)
        self.headers = {"Content-Length": str(len(raw)), "Host": "127.0.0.1:8787"}
        self.wfile = self
        self.body = bytearray()
        self.status = None
        self.client_address = ("127.0.0.1", 8787)

    def send_response(self, status):
        self.status = status

    def send_header(self, *_args):
        pass

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)


def test_duplicate_route_preserves_source_and_persists_clean_copy(monkeypatch):
    from api import routes

    source = _session("route-source-6610")
    error = _error(source)
    source.messages = [{"role": "user", "content": "keep"}, error, {"role": "assistant", "content": "later"}]
    record_dismissal(source, source.session_id, error)
    source.tool_calls = [{"name": "tool", "assistant_msg_idx": 2}]
    source_bytes = copy.deepcopy(source.messages)
    monkeypatch.setattr(routes.Session, "load", classmethod(lambda cls, _sid: source))
    monkeypatch.setattr(routes.Session, "save", lambda self, **_kwargs: None)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *args, **kwargs: None)
    monkeypatch.setattr(routes, "_evict_sessions_over_cap", lambda: None)

    handler = _Handler({"session_id": source.session_id})
    routes.handle_post(handler, urlparse("/api/session/duplicate"))

    assert handler.status == 200
    payload = json.loads(handler.body)
    copied = payload["session"]
    assert [row["content"] for row in copied["messages"]] == ["keep", "later"]
    assert copied["tool_calls"][0]["assistant_msg_idx"] == 1
    assert source.messages == source_bytes
    assert source.transcript_dismissal_active_count == 1
    assert source.tool_calls[0]["assistant_msg_idx"] == 2
    assert all("_provider_error_dismissal_capability" not in row for row in source.messages)


def test_reload_and_metadata_polling_preserve_ledger_and_source_bytes(tmp_path, monkeypatch):
    import api.models as models

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    session = _session("reload-6610")
    error = _error(session)
    session.messages = [{"role": "user", "content": "keep"}, error]
    record_dismissal(session, session.session_id, error)
    session.save()
    source_bytes = session.path.read_bytes()

    reloaded = models.Session.load(session.session_id)
    metadata = models.Session.load_metadata_only(session.session_id)

    assert reloaded.transcript_dismissals == session.transcript_dismissals
    assert reloaded.transcript_dismissal_active_count == 1
    assert metadata.transcript_dismissals == session.transcript_dismissals
    assert metadata.transcript_dismissal_active_count == 1
    assert reloaded.path.read_bytes() == source_bytes


def test_sidebar_refresh_and_state_db_overlay_keep_projected_count(tmp_path, monkeypatch):
    import api.models as models

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    session = _session("sidebar-6610")
    error = _error(session)
    session.messages = [{"role": "user", "content": "keep"}, error, {"role": "assistant", "content": "later"}]
    record_dismissal(session, session.session_id, error)
    session.save()
    row = {"session_id": session.session_id, "message_count": 3, "updated_at": 1}

    refreshed = models._refresh_index_rows_from_sidecar_metadata(
        [row], index_message_counts={session.session_id: 3}
    )
    assert refreshed[0]["message_count"] == 2
    assert refreshed[0]["transcript_dismissal_active_count"] == 1

    sidebar_row = {
        "session_id": session.session_id,
        "message_count": 0,
        "transcript_dismissal_active_count": 1,
        "last_message_at": 1,
        "updated_at": 1,
    }
    models._apply_sidebar_state_db_override_metadata(
        [sidebar_row],
        {session.session_id: {
            "_state_db_source": "webui",
            "_state_db_message_count": 3,
            "_state_db_last_message_at": 2,
        }},
    )
    assert sidebar_row["message_count"] == 2
    assert sidebar_row["actual_message_count"] == 3
