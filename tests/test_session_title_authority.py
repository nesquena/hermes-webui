"""Title authority across Agent SQLite, WebUI sidecars and consumed projections."""
from collections import OrderedDict
from contextlib import nullcontext
import json
import sys
import types

import pytest

from api import models, profiles, routes, state_sync, streaming

_REAL_AUX_GENERATOR = streaming._generate_llm_session_title_via_aux


@pytest.fixture
def title_state(tmp_path, monkeypatch):
    SessionDB = pytest.importorskip("hermes_state").SessionDB

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None, **kwargs: SessionDB(db_path=db_path))
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "index.json")
    sessions = OrderedDict()
    for module in (models, routes, streaming):
        monkeypatch.setattr(module, "SESSIONS", sessions)
    monkeypatch.setattr(profiles, "profile_env_for_background_worker", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(streaming, "_aux_title_generation_enabled", lambda: True)
    monkeypatch.setattr(streaming, "_aux_title_configured", lambda: True)
    monkeypatch.setattr(streaming, "_generate_llm_session_title_via_aux", lambda *a, **kw: ("Divergent legacy title", "llm_aux", ""))
    monkeypatch.setattr(routes, "_publish_session_list_changed", lambda *a, **kw: None)
    monkeypatch.setattr(routes, "_sync_session_title_to_insights", lambda *a: None)
    def make(sid, title="Untitled", source=None):
        s = models.Session(session_id=sid, title=title, workspace=str(tmp_path), profile="default")
        s.messages = [{"role": "user", "content": "BG3 infernal engine upgrade limit"},
                      {"role": "assistant", "content": "Here are the upgrade limits."}]
        s.save(touch_updated_at=False)
        sessions[sid] = s
        db.ensure_session(session_id=sid, source="webui")
        if source == "user":
            db.set_session_title(sid, title)
        elif source:
            db.set_auto_title(sid, title, source=source)
        return s
    yield db, make, tmp_path
    db.close()


def _update(s):
    events = []
    streaming._run_background_title_update(s.session_id, s.messages[0]["content"],
                                          s.messages[1]["content"], s.title,
                                          lambda e, d: events.append((e, d)))
    return events


def _assert_projected(s, path, title):
    assert s.compact()["title"] == title
    assert json.loads((path / f"{s.session_id}.json").read_text())["title"] == title


def test_initial_adopts_exact_agent_title_without_second_generation(title_state, monkeypatch):
    db, make, path = title_state
    s = make("authority-initial")
    title = "[BG3] Infernal engine upgrade limit"
    db.set_auto_title(s.session_id, title, source="llm")
    events = _update(s)
    _assert_projected(s, path, title)
    assert db.get_session_title(s.session_id) == title
    assert ("title", {"session_id": s.session_id, "title": title}) in events


def test_refresh_updates_equal_provenance_db_and_projection(title_state, monkeypatch):
    db, make, path = title_state
    s = make("authority-refresh", "[BG3] Old title", "llm")
    s.llm_title_generated = True
    title = "[BG3] Infernal engine upgrade limit"
    monkeypatch.setattr(streaming, "_generate_llm_session_title_via_aux", lambda *a, **kw: (title, "llm_aux", ""))
    streaming._run_background_title_refresh(s.session_id, "question", "answer", s.title, lambda *a: None)
    assert db.get_session_title(s.session_id) == title
    assert db.get_session_title_source(s.session_id) == "llm"
    _assert_projected(s, path, title)


def test_manual_regeneration_persists_without_insights(title_state):
    db, make, path = title_state
    s = make("authority-manual", "[BG3] Old title", "llm")
    title = "[BG3] Infernal engine upgrade limit"
    routes._persist_generated_session_title(s, title, event_reason="session_title_regenerate")
    assert db.get_session_title(s.session_id) == title
    _assert_projected(s, path, title)


def test_shared_generator_owns_new_and_manual_titles(title_state, monkeypatch):
    _db, make, _path = title_state
    s = make("authority-shared")
    monkeypatch.setattr(streaming, "_generate_llm_session_title_via_aux", _REAL_AUX_GENERATOR)
    # Keep the baseline regression deterministic too: an old WebUI must receive
    # a divergent fixture, never make a real provider request.
    monkeypatch.setattr(streaming, "generate_title_raw_via_aux",
                        lambda *a, **kw: ("Divergent legacy title", "llm_aux"))
    calls = []
    module = types.ModuleType("agent.title_generator")
    def generate_title(user_message, **kwargs):
        calls.append(user_message)
        return "[BG3] Infernal engine upgrade limit"
    module.generate_title = generate_title
    monkeypatch.setitem(sys.modules, "agent.title_generator", module)
    title, _status, _raw = streaming.generate_session_title_for_session(s)
    assert title == "[BG3] Infernal engine upgrade limit"
    assert calls == [s.messages[0]["content"]]
    _update(s)
    assert s.title == title
    s.llm_title_generated = True
    streaming._run_background_title_refresh(s.session_id, "latest user request", "answer", s.title, lambda *a: None)
    assert calls == [s.messages[0]["content"], s.messages[0]["content"], "latest user request"]


@pytest.mark.parametrize("mode", ["initial", "refresh", "manual"])
@pytest.mark.parametrize("fails", [False, True])
def test_shared_generator_failure_keeps_provisional_title(title_state, monkeypatch, mode, fails):
    db, make, path = title_state
    title = "Untitled" if mode == "initial" else "Old automatic title"
    s = make("authority-shared-empty", title, None if mode == "initial" else "llm")
    original_db_title = db.get_session_title(s.session_id)
    monkeypatch.setattr(streaming, "_generate_llm_session_title_via_aux", _REAL_AUX_GENERATOR)
    module = types.ModuleType("agent.title_generator")
    def generate_title(*args, **kwargs):
        if fails:
            raise RuntimeError("Provider unavailable")
        return None
    module.generate_title = generate_title
    monkeypatch.setitem(sys.modules, "agent.title_generator", module)
    def divergent_fallback(*args, **kwargs):
        pytest.fail("Modern generator failure must not use a divergent fallback")
    monkeypatch.setattr(streaming, "generate_title_raw_via_aux", divergent_fallback)
    monkeypatch.setattr(streaming, "generate_title_raw_via_agent", divergent_fallback)
    monkeypatch.setattr(streaming, "_fallback_title_from_exchange", divergent_fallback)
    if mode == "initial":
        events = _update(s)
        assert events[-1][0] == "stream_end"
        assert not any(event == "title" for event, _ in events)
    elif mode == "refresh":
        events = []
        streaming._run_background_title_refresh(s.session_id, "question", "answer", title,
                                                lambda e, d: events.append((e, d)))
        assert not any(event == "title" for event, _ in events)
    else:
        status, _payload = _regenerate_request(monkeypatch, s)
        assert status == 422
    _assert_projected(s, path, title)
    assert db.get_session_title(s.session_id) == original_db_title


@pytest.mark.parametrize("source", ["user", None])
def test_initial_respects_user_and_unknown_db_authority(title_state, source):
    db, make, path = title_state
    s = make("authority-protected")
    db.set_session_title(s.session_id, "My exact manual title")
    if source is None:
        db._execute_write(lambda conn: conn.execute("UPDATE sessions SET title_source = NULL WHERE id = ?", (s.session_id,)))
    _update(s)
    _assert_projected(s, path, "My exact manual title")
    assert s.manual_title is True


def test_webui_manual_title_wins_over_agent_auto_title(title_state):
    db, make, path = title_state
    s = make("authority-webui", "My WebUI name", "llm")
    s.manual_title = True
    s.save(touch_updated_at=False)
    db.set_session_title(s.session_id, "Different CLI name")
    events = _update(s)
    _assert_projected(s, path, "My WebUI name")
    assert not any(e == "title" for e, _ in events)


@pytest.mark.parametrize("mode", ["initial", "refresh"])
def test_racing_cli_rename_is_adopted_not_overwritten(title_state, monkeypatch, mode):
    db, make, path = title_state
    s = make("authority-cli-race", "Untitled" if mode == "initial" else "Old automatic title", None if mode == "initial" else "llm")
    def generate(*args, **kwargs):
        db.set_session_title(s.session_id, "CLI rename during generation")
        return "Generated loser", "llm_aux", ""
    monkeypatch.setattr(streaming, "_generate_llm_session_title_via_aux", generate)
    if mode == "initial":
        _update(s)
    else:
        streaming._run_background_title_refresh(s.session_id, "question", "answer", s.title, lambda *a: None)
    _assert_projected(s, path, "CLI rename during generation")
    assert db.get_session_title_source(s.session_id) == "user"
    assert s.manual_title is True


@pytest.mark.parametrize("mode", ["initial", "refresh"])
def test_racing_webui_rename_is_not_overwritten(title_state, monkeypatch, mode):
    from api.session_ops import apply_session_title_rename
    db, make, path = title_state
    s = make("authority-webui-race", "Untitled" if mode == "initial" else "Old automatic title", None if mode == "initial" else "llm")
    original_db_title = db.get_session_title(s.session_id)
    def generate(*args, **kwargs):
        apply_session_title_rename(s, "WebUI rename during generation")
        s.save(touch_updated_at=False)
        return "Generated loser", "llm_aux", ""
    monkeypatch.setattr(streaming, "_generate_llm_session_title_via_aux", generate)
    if mode == "initial":
        _update(s)
    else:
        streaming._run_background_title_refresh(s.session_id, "question", "answer", s.title, lambda *a: None)
    _assert_projected(s, path, "WebUI rename during generation")
    assert db.get_session_title(s.session_id) == original_db_title


@pytest.mark.parametrize("mode", ["initial", "refresh", "manual"])
def test_collision_suffix_is_exact_in_projection(title_state, monkeypatch, mode):
    db, make, path = title_state
    title = "[FF] Sleeper league roster audit"
    make("authority-collision-owner", title, "llm")
    s = make("authority-collision", "Untitled" if mode == "initial" else "Old automatic title", None if mode == "initial" else "llm")
    monkeypatch.setattr(streaming, "_generate_llm_session_title_via_aux", lambda *a, **kw: (title, "llm_aux", ""))
    if mode == "initial":
        _update(s)
    elif mode == "refresh":
        streaming._run_background_title_refresh(s.session_id, "question", "answer", s.title, lambda *a: None)
    else:
        routes._persist_generated_session_title(s, title, event_reason="session_title_regenerate")
    _assert_projected(s, path, title + " #2")
    assert db.get_session_title(s.session_id) == title + " #2"


def test_explicit_regeneration_can_replace_original_manual_title(title_state):
    db, make, path = title_state
    s = make("authority-explicit", "My manual title", "user")
    s.manual_title = True
    routes._persist_generated_session_title(s, "[FF] Sleeper league roster audit", event_reason="session_title_regenerate")
    _assert_projected(s, path, "[FF] Sleeper league roster audit")
    assert db.get_session_title_source(s.session_id) == "llm"
    assert s.manual_title is False


def test_usage_sync_never_promotes_or_clobbers_title_authority(title_state):
    db, make, _path = title_state
    s = make("authority-usage", "[FF] Sleeper league roster audit", "llm")
    state_sync.sync_session_usage(s.session_id, title="Stale sidecar title", profile="default")
    assert db.get_session_title(s.session_id) == s.title
    assert db.get_session_title_source(s.session_id) == "llm"


def test_db_failure_does_not_publish_candidate(title_state, monkeypatch):
    _db, make, path = title_state
    s = make("authority-db-failure")
    def fail(*a, **kw):
        raise RuntimeError("Database unavailable")
    monkeypatch.setattr(state_sync, "sync_session_title", fail)
    events = _update(s)
    _assert_projected(s, path, "Untitled")
    assert not any(e == "title" for e, _ in events)
    assert events[-1][0] == "stream_end"
    assert any(d.get("reason") == "title_persistence_error" for _, d in events)


def test_legacy_store_returns_winning_title_and_closes(monkeypatch):
    class LegacyDB:
        title = "Legacy manual title"
        closed = False
        def ensure_session(self, **kwargs):
            pass
        def get_session_title(self, sid):
            return self.title
        def set_auto_title_if_empty(self, sid, title):
            if self.title is None:
                self.title = title
        def close(self):
            self.closed = True
    db = LegacyDB()
    monkeypatch.setattr(state_sync, "_get_state_db", lambda **kwargs: db)
    assert state_sync.sync_session_title("legacy", "Generated title", replace=True) == (db.title, None)
    assert db.closed
    db.title = None
    assert state_sync.sync_session_title("legacy", "Generated title") == ("Generated title", None)


def _regenerate_request(monkeypatch, s):
    from urllib.parse import urlparse
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: {"session_id": s.session_id})
    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda sid: s)
    monkeypatch.setattr(routes, "j", lambda handler, payload, status=200, **kw: (status, payload))
    monkeypatch.setattr(routes, "bad", lambda handler, message, status=400: (status, {"error": message}))
    return routes.handle_post(types.SimpleNamespace(headers={}, command="POST"), urlparse("/api/session/title/regenerate"))


def test_regenerate_api_returns_exact_db_title_and_compact_projection(title_state, monkeypatch):
    db, make, path = title_state
    s = make("authority-api", "Old automatic title", "llm")
    monkeypatch.setattr(routes, "generate_session_title_for_session", lambda *a, **kw: ("[FF] Sleeper league roster audit", "shared_title", ""))
    status, payload = _regenerate_request(monkeypatch, s)
    assert status == 200
    assert payload["title"] == payload["session"]["title"] == db.get_session_title(s.session_id) == "[FF] Sleeper league roster audit"
    _assert_projected(s, path, payload["title"])


@pytest.mark.parametrize("writer", ["webui", "cli"])
def test_manual_regenerate_api_rejects_concurrent_rename(title_state, monkeypatch, writer):
    from api.session_ops import apply_session_title_rename
    db, make, _path = title_state
    s = make("authority-api-race", "Old automatic title", "llm")
    def generate(*args, **kwargs):
        if writer == "webui":
            apply_session_title_rename(s, "New manual title")
            s.save(touch_updated_at=False)
        else:
            db.set_session_title(s.session_id, "New manual title")
        return "Generated loser", "shared_title", ""
    monkeypatch.setattr(routes, "generate_session_title_for_session", generate)
    status, payload = _regenerate_request(monkeypatch, s)
    assert status == 409
    assert "changed" in payload["error"]
    assert (s.title if writer == "webui" else db.get_session_title(s.session_id)) == "New manual title"

