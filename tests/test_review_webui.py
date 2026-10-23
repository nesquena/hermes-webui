"""WebUI /review: dispatch the independent reviewer subagent for a chat (CLI/gateway parity)."""

from pathlib import Path
from types import SimpleNamespace as NS

from tests.conftest import requires_agent_modules


def _fake_agent(messages=None):
    return NS(
        _session_messages=list(
            messages if messages is not None else [{"role": "user", "content": "hi"}]
        )
    )


def _patch_engine(monkeypatch, fn):
    """Patch the engine entry point the adapter imports at call time."""
    import agent.review_engine as engine

    monkeypatch.setattr(engine, "start_review", fn)


def _wire(monkeypatch, *, agent=None, profile=None, active=False):
    import collections

    from api import background_process, config, models, profiles

    monkeypatch.setattr(
        profiles, "get_hermes_home_for_profile", lambda p: "/tmp/home-test"
    )
    cache = collections.OrderedDict()
    if agent is not None:
        cache["s1"] = (agent, "sig")
    monkeypatch.setattr(config, "SESSION_AGENT_CACHE", cache)
    monkeypatch.setattr(models, "get_session", lambda sid, **kw: NS(profile=profile))
    monkeypatch.setattr(
        background_process, "_session_has_active_turn", lambda sid: active
    )


@requires_agent_modules
def test_review_dispatches_on_cached_agent(monkeypatch):
    from gateway.session_context import get_session_env

    from api.review import run_review_command

    seen = {}

    def fake_start(agent, messages, user_prompt=""):
        seen["agent"] = agent
        seen["messages"] = messages
        seen["user_prompt"] = user_prompt
        # The dispatch must carry this chat's return address so the completion
        # event routes back here (xsession wakeup routing).
        seen["ui_session_id"] = get_session_env("HERMES_UI_SESSION_ID", "")
        return {"status": "dispatched", "delegation_id": "d1"}

    _patch_engine(monkeypatch, fake_start)
    agent = _fake_agent()
    _wire(monkeypatch, agent=agent)

    out = run_review_command("s1", "", request_profile=None)

    assert out == "Review started. Results will return here."
    assert seen["agent"] is agent
    assert seen["messages"] == agent._session_messages
    assert seen["user_prompt"] == ""
    assert seen["ui_session_id"] == "s1"


@requires_agent_modules
def test_review_passes_focus(monkeypatch):
    from api.review import run_review_command

    seen = {}

    def fake_start(agent, messages, user_prompt=""):
        seen["user_prompt"] = user_prompt
        return {"status": "dispatched"}

    _patch_engine(monkeypatch, fake_start)
    _wire(monkeypatch, agent=_fake_agent())

    out = run_review_command("s1", "check the tests", request_profile=None)

    assert seen["user_prompt"] == "check the tests"
    assert out == "Review started. Results will return here."


@requires_agent_modules
def test_review_refuses_while_a_turn_is_running(monkeypatch):
    from api.review import BUSY_TEXT, run_review_command

    called = []
    _patch_engine(monkeypatch, lambda *a, **k: called.append(True))
    _wire(monkeypatch, agent=_fake_agent(), active=True)

    assert run_review_command("s1", "", request_profile=None) == BUSY_TEXT
    assert called == []


@requires_agent_modules
def test_review_requires_a_conversation(monkeypatch):
    from api.review import NOTHING_YET_TEXT, run_review_command

    _wire(monkeypatch, agent=None)
    assert run_review_command("s1", "", request_profile=None) == NOTHING_YET_TEXT

    # An empty conversation reaches the engine and refuses with the engine's
    # own text (the adapter does not invent its own wording).
    _wire(monkeypatch, agent=_fake_agent(messages=[]))
    assert (
        run_review_command("s1", "", request_profile=None)
        == "Nothing to review yet — the conversation is empty."
    )


@requires_agent_modules
def test_review_surfaces_engine_refusals_verbatim(monkeypatch):
    from api.review import run_review_command

    def refuse(agent, messages, user_prompt=""):
        raise ValueError("Review dispatch failed: capacity")

    _patch_engine(monkeypatch, refuse)
    _wire(monkeypatch, agent=_fake_agent())

    assert (
        run_review_command("s1", "", request_profile=None)
        == "Review dispatch failed: capacity"
    )


@requires_agent_modules
def test_review_fails_closed_when_chat_belongs_to_another_profile(monkeypatch):
    from api.review import run_review_command

    called = []
    _patch_engine(monkeypatch, lambda *a, **k: called.append(True))
    _wire(monkeypatch, agent=_fake_agent(), profile="other")

    out = run_review_command("s1", "", request_profile="default")

    assert "another profile" in out
    assert called == []


@requires_agent_modules
def test_review_reports_dispatch_failure(monkeypatch):
    from api.review import run_review_command

    def boom(agent, messages, user_prompt=""):
        raise RuntimeError("boom")

    _patch_engine(monkeypatch, boom)
    _wire(monkeypatch, agent=_fake_agent())

    assert (
        run_review_command("s1", "", request_profile=None)
        == "/review failed to start: boom"
    )


def test_review_controls_bypass_busy_routing():
    src = (Path(__file__).resolve().parents[1] / "static" / "messages.js").read_text(
        encoding="utf-8"
    )
    busy = src[
        src.index("Busy-control slash commands must be intercepted") : src.index(
            "const defaultMessageMode="
        )
    ]
    assert (
        "_pc.name==='review'" in busy
        and "executeAgentCommand(text,{name:'review'})" in busy
    )


def test_review_surface_wiring():
    """Announce, dispatch (with session_id), and exec-endpoint branch must all exist."""
    root = Path(__file__).resolve().parents[1]
    commands_js = (root / "static" / "commands.js").read_text(encoding="utf-8")
    messages_js = (root / "static" / "messages.js").read_text(encoding="utf-8")
    routes_py = (root / "api" / "routes.py").read_text(encoding="utf-8")

    announce = commands_js[commands_js.index("_WEBUI_DISPATCHABLE_AGENT_COMMANDS") :]
    announce = announce[: announce.index("]);")]
    assert "'review'" in announce
    assert "session_id:S.session&&S.session.session_id||''" in commands_js

    run_set = messages_js[messages_js.index("_AGENT_COMMANDS_RUN_ON_WEBUI") :]
    run_set = run_set[: run_set.index("]);")]
    assert "'review'" in run_set

    assert "run_review_command(" in routes_py
    assert 'in ("/review", "review")' in routes_py
