from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

import api.models as models
import api.routes as routes


def _session(sid, messages):
    session = models.Session(
        session_id=sid,
        title="Cached tail",
        workspace="/tmp",
        model="test-model",
        messages=messages,
        context_length=32_000,
    )
    session.session_source = "webui"
    return session


def _metadata_stub(session):
    stub = models.Session(
        session_id=session.session_id,
        title=session.title,
        workspace=session.workspace,
        model=session.model,
        profile=session.profile,
        context_length=session.context_length,
    )
    stub.session_source = "webui"
    stub._metadata_message_count = len(session.messages)
    stub._loaded_metadata_only = True
    return stub


def _invoke_twice(tmp_path, monkeypatch):
    sid = "detail_cache_001"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    sidecar = session_dir / f"{sid}.json"
    sidecar.write_text('{"version":1}', encoding="utf-8")
    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text("model: {}\n", encoding="utf-8")
    state_db = tmp_path / "state.db"

    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SETTINGS_FILE", settings)
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: state_db)
    monkeypatch.setattr(routes, "_active_profile_config_path", lambda: config)
    routes._clear_session_detail_tail_cache()

    full = _session(
        sid,
        [
            {"role": "user", "content": "question", "timestamp": 1.0},
            {"role": "assistant", "content": "answer", "timestamp": 2.0},
        ],
    )
    metadata = _metadata_stub(full)
    load_modes = []
    responses = []

    def fake_get_session(_sid, metadata_only=False):
        assert _sid == sid
        load_modes.append(bool(metadata_only))
        return metadata if metadata_only else full

    def fake_j(_handler, payload, status=200, extra_headers=None, **_kwargs):
        responses.append((status, payload))
        return payload

    parsed = urlparse(
        f"/api/session?session_id={sid}&messages=1&resolve_model=0"
        "&msg_limit=30&expand_renderable=1"
    )
    common_patches = (
        patch.object(routes, "get_session", side_effect=fake_get_session),
        patch.object(routes, "_session_visible_to_active_profile", return_value=True),
        patch.object(routes, "_clear_stale_stream_state", return_value=False),
        patch.object(routes, "_lookup_cli_session_metadata", return_value={}),
        patch.object(routes, "get_state_db_session_messages", return_value=[]),
        patch.object(routes, "redact_session_data", side_effect=lambda raw: raw),
        patch.object(routes, "_active_stream_ids", return_value=set()),
        patch.object(routes, "j", side_effect=fake_j),
    )
    for context in common_patches:
        context.start()
    try:
        routes.handle_get(SimpleNamespace(), parsed)
        routes.handle_get(SimpleNamespace(), parsed)
    finally:
        for context in reversed(common_patches):
            context.stop()

    return sidecar, parsed, full, metadata, load_modes, responses


def test_second_initial_tail_request_skips_full_session_load(tmp_path, monkeypatch):
    _sidecar, _parsed, _full, _metadata, load_modes, responses = _invoke_twice(
        tmp_path,
        monkeypatch,
    )

    assert load_modes == [True, False, True]
    assert len(responses) == 2
    assert responses[0][1] == responses[1][1]
    assert responses[1][1]["session"]["messages"][-1]["content"] == "answer"


def test_sidecar_change_invalidates_cached_initial_tail(tmp_path, monkeypatch):
    sidecar, parsed, full, metadata, load_modes, _responses = _invoke_twice(
        tmp_path,
        monkeypatch,
    )
    sidecar.write_text('{"version":2,"changed":true}', encoding="utf-8")

    def fake_get_session(_sid, metadata_only=False):
        load_modes.append(bool(metadata_only))
        return metadata if metadata_only else full

    captured = []

    def fake_j(_handler, payload, status=200, extra_headers=None, **_kwargs):
        captured.append(payload)
        return payload

    with patch.object(routes, "get_session", side_effect=fake_get_session), \
         patch.object(routes, "_session_visible_to_active_profile", return_value=True), \
         patch.object(routes, "_clear_stale_stream_state", return_value=False), \
         patch.object(routes, "_lookup_cli_session_metadata", return_value={}), \
         patch.object(routes, "get_state_db_session_messages", return_value=[]), \
         patch.object(routes, "redact_session_data", side_effect=lambda raw: raw), \
         patch.object(routes, "_active_stream_ids", return_value=set()), \
         patch.object(routes, "j", side_effect=fake_j):
        routes.handle_get(SimpleNamespace(), parsed)

    assert load_modes[-2:] == [True, False]
    assert captured


def test_active_and_lineage_sessions_never_use_detail_tail_cache():
    active = _session("active_cache_001", [])
    active.active_stream_id = "stream-1"
    assert routes._session_detail_tail_cache_eligible(active) is False

    child = _session("lineage_cache_001", [])
    child.parent_session_id = "parent_001"
    assert routes._session_detail_tail_cache_eligible(child) is False
