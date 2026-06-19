from api.background_process import format_wakeup_prompt


def test_format_wakeup_prompt_skips_empty_event():
    assert format_wakeup_prompt({}) is None


def test_format_wakeup_prompt_skips_non_dict_event():
    assert format_wakeup_prompt(None) is None
    assert format_wakeup_prompt(42) is None


def test_format_wakeup_prompt_skips_unknown_event_type():
    evt = {"type": "unknown_event", "message": "do not inject me"}
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
