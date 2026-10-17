"""WebUI /refine: spawn the memory/skill review fork for a chat (CLI/gateway parity)."""

from pathlib import Path
from types import SimpleNamespace as NS

from tests.conftest import requires_agent_modules


def _fake_agent(messages=None, skill_tools=True, spawn=None):
    agent = NS(
        _session_messages=list(
            messages if messages is not None else [{"role": "user", "content": "hi"}]
        ),
        valid_tool_names={"skill_manage"} if skill_tools else {"read_file"},
    )
    agent._spawn_background_review = spawn or (lambda **kwargs: None)
    return agent


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
def test_refine_spawns_review_on_cached_agent(monkeypatch):
    from api.refine import run_refine_command

    spawned = {}

    def spawn(**kwargs):
        spawned.update(kwargs)

    agent = _fake_agent(spawn=spawn)
    _wire(monkeypatch, agent=agent)

    out = run_refine_command("s1", "", request_profile=None)

    assert out.startswith("⚗ Reviewing this conversation in the background")
    assert spawned["messages_snapshot"] == agent._session_messages
    assert spawned["review_memory"] is True
    assert spawned["review_skills"] is True
    assert spawned["focus"] is None
    assert spawned["explicit"] is True


@requires_agent_modules
def test_refine_passes_focus_and_skill_gate(monkeypatch):
    from api.refine import run_refine_command

    spawned = {}
    agent = _fake_agent(skill_tools=False, spawn=lambda **kw: spawned.update(kw))
    _wire(monkeypatch, agent=agent)

    out = run_refine_command("s1", "tighten the prompts", request_profile=None)

    assert "(focus: tighten the prompts)" in out
    assert spawned["focus"] == "tighten the prompts"
    assert spawned["review_skills"] is False


@requires_agent_modules
def test_refine_refuses_while_a_turn_is_running(monkeypatch):
    from api.refine import BUSY_TEXT, run_refine_command

    called = []
    agent = _fake_agent(spawn=lambda **kw: called.append(True))
    _wire(monkeypatch, agent=agent, active=True)

    assert run_refine_command("s1", "", request_profile=None) == BUSY_TEXT
    assert called == []


@requires_agent_modules
def test_refine_requires_a_conversation(monkeypatch):
    from api.refine import EMPTY_TEXT, NOTHING_YET_TEXT, run_refine_command

    _wire(monkeypatch, agent=None)
    assert run_refine_command("s1", "", request_profile=None) == NOTHING_YET_TEXT

    _wire(monkeypatch, agent=_fake_agent(messages=[]))
    assert run_refine_command("s1", "", request_profile=None) == EMPTY_TEXT


@requires_agent_modules
def test_refine_fails_closed_when_chat_belongs_to_another_profile(monkeypatch):
    from api.refine import run_refine_command

    called = []
    agent = _fake_agent(spawn=lambda **kw: called.append(True))
    _wire(monkeypatch, agent=agent, profile="other")

    out = run_refine_command("s1", "", request_profile="default")

    assert "another profile" in out
    assert called == []


@requires_agent_modules
def test_refine_reports_spawn_failure(monkeypatch):
    from api.refine import run_refine_command

    def boom(**kwargs):
        raise RuntimeError("boom")

    _wire(monkeypatch, agent=_fake_agent(spawn=boom))

    assert (
        run_refine_command("s1", "", request_profile=None)
        == "/refine failed to start: boom"
    )


@requires_agent_modules
def test_review_summary_delivery_appends_row_and_refreshes_sidebar(monkeypatch):
    from api import models, refine, session_events

    saved, events = [], []
    session = NS(messages=[], save=lambda: saved.append(True))
    monkeypatch.setattr(models, "get_session", lambda sid, **kw: session)
    monkeypatch.setattr(
        session_events,
        "publish_session_list_changed",
        lambda reason="session_changed", profile=None, session_id=None: events.append(
            {"reason": reason, "session_id": session_id}
        ),
    )

    refine.deliver_review_summary(
        "s1", "  💾 Self-improvement review: memory updated  "
    )

    assert len(session.messages) == 1
    row = session.messages[0]
    assert row["role"] == "assistant"
    assert row["content"] == "💾 Self-improvement review: memory updated"
    assert isinstance(row["timestamp"], float)
    assert saved == [True]
    assert events == [{"reason": "background_review", "session_id": "s1"}]


@requires_agent_modules
def test_review_summary_delivery_noops_for_missing_session(monkeypatch):
    from api import models, refine

    def gone(sid, **kw):
        raise KeyError(sid)

    monkeypatch.setattr(models, "get_session", gone)

    refine.deliver_review_summary("s1", "💾 x")
    refine.deliver_review_summary("s1", "   ")


@requires_agent_modules
def test_review_summary_delivery_survives_save_failure(monkeypatch):
    from api import models, refine, session_events

    def bad_save():
        raise OSError("disk")

    session = NS(messages=[], save=bad_save)
    monkeypatch.setattr(models, "get_session", lambda sid, **kw: session)
    events = []
    monkeypatch.setattr(
        session_events, "publish_session_list_changed", lambda *a, **k: events.append(1)
    )

    refine.deliver_review_summary("s1", "💾 x")  # must not raise

    assert events == []


def test_refine_controls_bypass_busy_routing():
    src = (Path(__file__).resolve().parents[1] / "static" / "messages.js").read_text(
        encoding="utf-8"
    )
    busy = src[
        src.index("Busy-control slash commands must be intercepted") : src.index(
            "const defaultMessageMode="
        )
    ]
    assert (
        "_pc.name==='refine'" in busy
        and "executeAgentCommand(text,{name:'refine'})" in busy
    )


def test_refine_surface_wiring():
    """Announce, dispatch (with session_id), and exec-endpoint branch must all exist."""
    root = Path(__file__).resolve().parents[1]
    commands_js = (root / "static" / "commands.js").read_text(encoding="utf-8")
    messages_js = (root / "static" / "messages.js").read_text(encoding="utf-8")
    routes_py = (root / "api" / "routes.py").read_text(encoding="utf-8")

    announce = commands_js[commands_js.index("_WEBUI_DISPATCHABLE_AGENT_COMMANDS") :]
    announce = announce[: announce.index("]);")]
    assert "'refine'" in announce
    assert "session_id:S.session&&S.session.session_id||''" in commands_js

    run_set = messages_js[messages_js.index("_AGENT_COMMANDS_RUN_ON_WEBUI") :]
    run_set = run_set[: run_set.index("]);")]
    assert "'refine'" in run_set

    assert "run_refine_command(" in routes_py
    assert 'in ("/refine", "refine")' in routes_py
