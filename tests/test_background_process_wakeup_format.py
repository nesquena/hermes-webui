from api.background_process import format_wakeup_prompt


def test_format_wakeup_prompt_skips_empty_event():
    assert format_wakeup_prompt({}) is None


def test_format_wakeup_prompt_skips_non_dict_event():
    assert format_wakeup_prompt(None) is None
    assert format_wakeup_prompt(42) is None


def test_format_wakeup_prompt_skips_unknown_event_type():
    evt = {"type": "unknown_event", "message": "do not inject me"}
    assert format_wakeup_prompt(evt) is None


def test_format_wakeup_prompt_handles_async_delegation(monkeypatch):
    """#4912 — async_delegation completion events (from a background
    delegate_task) must be rendered, not silently dropped, so the subagent
    result re-enters the parent conversation. format_wakeup_prompt delegates
    to the agent-side tools.process_registry.format_process_notification.

    We inject a stub agent module so this validates the delegation WIRING
    independent of whether the full hermes-agent package is importable in the
    test environment (it isn't in WebUI-only CI shards).
    """
    import sys
    import types

    seen = {}

    def _fake_fmt(evt):
        seen["evt"] = evt
        return "[ASYNC DELEGATION COMPLETE — t_1]\nSubagent finished: the answer is 42."

    fake_mod = types.ModuleType("tools.process_registry")
    fake_mod.format_process_notification = _fake_fmt
    fake_pkg = sys.modules.get("tools") or types.ModuleType("tools")
    monkeypatch.setitem(sys.modules, "tools", fake_pkg)
    monkeypatch.setitem(sys.modules, "tools.process_registry", fake_mod)

    evt = {
        "type": "async_delegation",
        "session_id": "proc_deleg1",
        "session_key": "webui-session",
        "task_id": "t_1",
        "summary": "Subagent finished: the answer is 42.",
        "status": "completed",
    }
    result = format_wakeup_prompt(evt)
    assert result is not None, (
        "async_delegation completion must produce a wakeup prompt (#4912), "
        "not be dropped"
    )
    assert "ASYNC DELEGATION" in result.upper()
    # Confirm it actually delegated the event to the agent-side formatter.
    assert seen.get("evt", {}).get("type") == "async_delegation"


def test_format_wakeup_prompt_async_delegation_drops_gracefully_without_agent(monkeypatch):
    """If the agent-side formatter is unavailable (import fails), async_delegation
    degrades to None instead of raising — same safe behavior as before #4912."""
    import builtins

    real_import = builtins.__import__

    def _blocking_import(name, *args, **kwargs):
        if name == "tools.process_registry" or name.startswith("tools.process_registry"):
            raise ImportError("simulated: agent module unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)
    evt = {"type": "async_delegation", "session_id": "s", "task_id": "t"}
    # Must not raise; returns None when the agent formatter can't be imported.
    assert format_wakeup_prompt(evt) is None


def test_format_wakeup_prompt_handles_watch_disabled():
    evt = {
        "type": "watch_disabled",
        "session_id": "proc_abc",
        "session_key": "webui-session",
        "command": "tail -f log",
        "message": "Watch patterns disabled for process proc_abc.",
    }
    assert (
        format_wakeup_prompt(evt)
        == "[IMPORTANT: Watch patterns disabled for process proc_abc.]"
    )


def test_format_wakeup_prompt_skips_blank_watch_disabled():
    assert format_wakeup_prompt({"type": "watch_disabled"}) is None
    assert format_wakeup_prompt({"type": "watch_disabled", "message": ""}) is None


def test_format_wakeup_prompt_handles_watch_overflow_tripped():
    evt = {
        "type": "watch_overflow_tripped",
        "session_id": "",
        "session_key": "",
        "command": "",
        "message": "Watch-pattern overflow: suppressing further watch_match events.",
    }
    assert (
        format_wakeup_prompt(evt)
        == "[IMPORTANT: Watch-pattern overflow: suppressing further watch_match events.]"
    )


def test_format_wakeup_prompt_handles_watch_overflow_released():
    evt = {
        "type": "watch_overflow_released",
        "session_id": "",
        "session_key": "",
        "command": "",
        "suppressed": 3,
        "message": "Watch-pattern notifications resumed. 3 match event(s) were suppressed.",
    }
    assert (
        format_wakeup_prompt(evt)
        == "[IMPORTANT: Watch-pattern notifications resumed. 3 match event(s) were suppressed.]"
    )


def test_format_wakeup_prompt_keeps_normal_completion():
    evt = {
        "type": "completion",
        "session_id": "proc_abc",
        "command": "sleep 1",
        "exit_code": 0,
        "output": "done",
    }
    result = format_wakeup_prompt(evt)
    assert result is not None
    assert "Background process proc_abc completed" in result
    assert "Command: sleep 1" in result
    assert "Output:\ndone" in result
