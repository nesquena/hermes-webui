"""Durable worker regressions for the title review's route/lifecycle matrix."""
import json
import threading
from contextlib import nullcontext
from unittest.mock import patch

import pytest
import api.models as models
import api.streaming as streaming
from api.models import Session
from tests._aux_client_helpers import auxiliary_client_modules, patch_tg_config
from tests.test_title_reasoning_trace_guard import _fixture_agent, _response


@pytest.fixture
def scene(tmp_path, monkeypatch):
    directory = tmp_path / "sessions"
    directory.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", directory)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", directory / "_index.json")
    session = Session(session_id="review-worker", title="Untitled", messages=[
        {"role": "user", "content": "Fix login."},
        {"role": "assistant", "content": "Inspect login."},
    ], llm_title_generated=False, manual_title=False)
    monkeypatch.setattr(streaming, "get_session", lambda sid: session)
    monkeypatch.setattr(streaming, "SESSIONS", {session.session_id: session})
    monkeypatch.setattr(streaming, "LOCK", threading.Lock())
    monkeypatch.setattr(streaming, "_aux_title_generation_enabled", lambda: True)
    monkeypatch.setattr("api.profiles.profile_env_for_background_worker", lambda *a, **k: nullcontext())
    monkeypatch.setattr("api.state_sync.sync_session_title", lambda *a, **k: None)
    return session, directory / (session.session_id + ".json")


@pytest.mark.parametrize("route", ("aux", "agent"))
@pytest.mark.parametrize("content", (
    "<think>I should choose a title.</think>\nMobile Login Repair",
    "Let me think through this.\nMobile Login Repair",
    "Here's my thinking process: short.\nMobile Login Repair",
    "The user is asking about login.\nMobile Login Repair",
))
def test_worker_reuses_visible_title_in_one_call(scene, monkeypatch, route, content):
    session, path = scene
    monkeypatch.setattr(streaming, "_aux_title_configured", lambda: route == "aux")
    calls = []

    def respond(**kwargs):
        calls.append(kwargs)
        return _response(content)

    agent = _fixture_agent(respond) if route == "agent" else None
    events = []
    with auxiliary_client_modules(), patch_tg_config({"provider": "custom", "model": "fixture"}):
        with patch("agent.auxiliary_client.call_llm", side_effect=respond, create=True):
            streaming._run_background_title_update(
                session.session_id, "Fix login.", "Inspect login.", "Untitled",
                lambda name, data: events.append((name, data)), agent=agent)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["title"] == "Mobile Login Repair"
    assert saved["llm_title_generated"] is True
    assert len(calls) == 1
    assert [data["title"] for name, data in events if name == "title"] == ["Mobile Login Repair"]


@pytest.mark.parametrize("primary", ("aux", "agent"))
def test_refresh_invalid_trace_uses_alternate_route(scene, monkeypatch, primary):
    session, path = scene
    session.title = "Existing Title"
    session.llm_title_generated = True
    session.save()
    monkeypatch.setattr(streaming, "_aux_title_configured", lambda: primary == "aux")
    calls = []

    def respond(route, **kwargs):
        calls.append(route)
        return _response("I should choose a title." if route == primary else "Recovered Refresh Title")

    agent = _fixture_agent(lambda **kw: respond("agent", **kw))
    events = []
    with auxiliary_client_modules(), patch_tg_config({"provider": "custom", "model": "fixture"}):
        with patch("agent.auxiliary_client.call_llm", side_effect=lambda **kw: respond("aux", **kw), create=True):
            streaming._run_background_title_refresh(
                session.session_id, "Fix login.", "Inspect login.", "Existing Title",
                lambda name, data: events.append((name, data)), agent=agent)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["title"] == "Recovered Refresh Title"
    assert saved["llm_title_generated"] is True
    assert calls == [primary] * 3 + ["agent" if primary == "aux" else "aux"]
    assert [data["title"] for name, data in events if name == "title"] == ["Recovered Refresh Title"]
    assert any(data.get("status") == "refreshed" for name, data in events if name == "title_status")
