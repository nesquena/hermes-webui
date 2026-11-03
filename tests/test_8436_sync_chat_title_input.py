"""#8436 maintainer fold: the synchronous /api/chat route gives the Agent the same title-only input.

The streaming worker builds its run_conversation kwargs through ``_build_run_conversation_kwargs``, which #8436 taught
to forward the submitted prompt as ``title_user_message``. ``_handle_chat_sync`` calls ``run_conversation`` directly,
so without the fold its state.db title still came from the ``[Workspace::v1: ...]``-prefixed model input. The kwarg is
signature-gated the same way: an Agent whose ``run_conversation`` predates it is still called without it.
"""
from __future__ import annotations

from types import SimpleNamespace
import sys


def _drive_sync_chat(monkeypatch, tmp_path, agent_cls, message="Plan the release notes"):
    import api.config as config
    import api.models as models
    import api.routes as routes

    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    monkeypatch.setattr(routes, "get_session", models.get_session)
    monkeypatch.setattr(routes, "title_from", models.title_from)
    monkeypatch.setattr(config, "get_config", lambda: {"model": "test-model", "provider": "test-provider"})
    monkeypatch.setattr(routes, "get_config", lambda: {"model": "test-model", "provider": "test-provider"})
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda value, **_kw: tmp_path)
    monkeypatch.setattr(routes, "load_settings", lambda: {})
    monkeypatch.setattr(routes, "_resolve_cli_toolsets", lambda: [])

    session = models.Session(
        session_id="f8436sync",
        workspace=str(tmp_path),
        messages=[],
        context_messages=[],
        model="test-model",
        model_provider="test-provider",
    )
    session.save(touch_updated_at=False)
    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=agent_cls))

    class Handler:
        def __init__(self):
            self.headers = {}
            self.status = None
            self.wfile = SimpleNamespace(write=lambda _body: None)

        def send_response(self, status):
            self.status = status

        def send_header(self, _name, _value):
            return None

        def end_headers(self):
            return None

    handler = Handler()
    routes._handle_chat_sync(
        handler, {"session_id": session.session_id, "message": message, "workspace": str(tmp_path)}
    )
    return handler


def _reply(kwargs):
    history = list(kwargs.get("conversation_history") or [])
    return {
        "messages": history
        + [
            {"role": "user", "content": kwargs["persist_user_message"]},
            {"role": "assistant", "content": "ok"},
        ],
        "final_response": "ok",
        "completed": True,
    }


def test_sync_chat_passes_the_submitted_prompt_as_the_title_input(monkeypatch, tmp_path):
    captured = {}

    class CurrentAgent:
        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, **kwargs):
            captured.update(kwargs)
            return _reply(kwargs)

    handler = _drive_sync_chat(monkeypatch, tmp_path, CurrentAgent)

    assert handler.status == 200
    assert captured["title_user_message"] == "Plan the release notes"
    assert captured["persist_user_message"] == "Plan the release notes"
    # The model still receives the enriched input; only the title source changes.
    assert captured["user_message"].endswith("Plan the release notes")
    assert captured["user_message"] != captured["title_user_message"]


def test_sync_chat_still_calls_an_agent_without_the_title_kwarg(monkeypatch, tmp_path):
    captured = {}

    class LegacyAgent:
        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, user_message, system_message=None, conversation_history=None,
                             task_id=None, persist_user_message=None):
            captured.update(user_message=user_message, persist_user_message=persist_user_message)
            return _reply({"conversation_history": conversation_history,
                           "persist_user_message": persist_user_message})

    handler = _drive_sync_chat(monkeypatch, tmp_path, LegacyAgent)

    assert handler.status == 200
    assert captured["persist_user_message"] == "Plan the release notes"
