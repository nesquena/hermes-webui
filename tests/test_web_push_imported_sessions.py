"""First browser continuation of imported sessions enrolls before run admission."""
from collections import OrderedDict
from urllib.parse import urlparse

import pytest

from api import models, routes, web_push
from tests.test_chat_start_claim_cli_session import (
    _FakePostHandler, _make_state_db, _response_json,
    isolated_state_db,  # noqa: F401 -- shared pytest fixture
)
from tests.test_web_push import DEV_A, OWNER_A, _enable


@pytest.fixture
def imported_chat(push_env, tmp_path, monkeypatch, request):
    state = request.getfixturevalue("isolated_state_db")
    _enable(monkeypatch, push_env)
    sessions = OrderedDict()
    monkeypatch.setattr(models, "SESSIONS", sessions)
    monkeypatch.setattr(routes, "SESSIONS", sessions)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(models, "get_last_workspace", lambda **kw: str(tmp_path))
    monkeypatch.setattr(routes, "get_last_workspace", lambda **kw: str(tmp_path))
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **kw: None)
    monkeypatch.setattr(routes, "run_admission_closed_response", lambda: None)
    monkeypatch.setattr(routes, "get_config", lambda: {})
    monkeypatch.setattr(routes, "get_config_snapshot", lambda: {})
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda s, w: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda s, p: (None, None, None))
    monkeypatch.setattr(routes, "_resolve_compatible_session_model_state", lambda *a, **kw: ("test-model", None, "test-model"))
    return state


def _seed_import(monkeypatch, state, sid, source, import_path, *, profile="default"):
    _make_state_db(state["db"], sid, source=source, message_count=2, title="Fixture imported session")
    metadata = {"session_id": sid, "source_tag": source, "raw_source": source,
                "session_source": "other", "profile": profile, "model": "test-model"}
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid: dict(metadata))
    monkeypatch.setattr(routes, "_resolve_cli_import_metadata", lambda sid: dict(metadata) if import_path == "metadata" else {})
    monkeypatch.setattr(routes, "get_cli_session_messages", lambda sid, **kw: [
        {"role": "user", "content": "Fixture question"},
        {"role": "assistant", "content": "Fixture response"},
    ])


def _post_start(sid, **extra):
    handler = _FakePostHandler({"session_id": sid, "message": "Fixture follow-up", **extra}, path="/api/chat/start")
    handler.headers[web_push.DEVICE_HEADER] = DEV_A
    routes.handle_post(handler, urlparse(handler.path))
    return handler


@pytest.mark.parametrize("source", ["cli", "tui", "desktop"])
@pytest.mark.parametrize("import_path", ["metadata", "state_db"])
def test_first_imported_continuation_enrolls_before_run(imported_chat, monkeypatch, source, import_path):
    sid = f"first-{source}-{import_path}"
    _seed_import(monkeypatch, imported_chat, sid, source, import_path)
    assert not routes._push_session_exists(sid)
    started = []

    def start(session, **kwargs):
        assert (imported_chat["sessions_dir"] / f"{sid}.json").exists()
        assert web_push.session_owners(sid) == [OWNER_A]
        started.append(session)
        return {"stream_id": "fixture-stream", "session_id": sid}

    monkeypatch.setattr(routes, "_start_run", start)
    handler = _post_start(sid)
    assert handler.status == 200, _response_json(handler)
    assert len(started) == 1
    assert web_push.session_owners(sid) == [OWNER_A]


@pytest.mark.parametrize("import_path", ["metadata", "state_db"])
def test_foreign_imported_session_never_enrolls_requesting_device(imported_chat, monkeypatch, import_path):
    sid = f"foreign-{import_path}"
    _seed_import(monkeypatch, imported_chat, sid, "tui", import_path, profile="private")
    monkeypatch.setattr(routes, "_start_run", lambda *a, **kw: pytest.fail("foreign profile started"))
    handler = _post_start(sid)
    assert handler.status == 409
    assert _response_json(handler)["code"] == "session_profile_mismatch"
    assert web_push.session_owners(sid) == []


def test_invalid_profile_after_import_does_not_enroll(imported_chat, monkeypatch):
    sid = "invalid-profile-import"
    _seed_import(monkeypatch, imported_chat, sid, "tui", "metadata")
    monkeypatch.setattr(routes, "_start_run", lambda *a, **kw: pytest.fail("invalid profile started"))
    handler = _post_start(sid, profile="../private")
    assert handler.status == 400
    assert web_push.session_owners(sid) == []


def test_failed_materialization_does_not_enroll(imported_chat, monkeypatch):
    sid = "failed-import"
    _seed_import(monkeypatch, imported_chat, sid, "tui", "state_db")

    def fail_save(self, *args, **kwargs):
        raise OSError("fixture write failure")

    monkeypatch.setattr(models.Session, "save", fail_save)
    monkeypatch.setattr(routes, "_start_run", lambda *a, **kw: pytest.fail("failed import started"))
    handler = _post_start(sid)
    assert handler.status == 500
    assert web_push.session_owners(sid) == []
