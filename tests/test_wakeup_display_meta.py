"""#6345 — display metadata for process-wakeup messages.

``wakeup_display_meta`` must be an exact inverse of the two structured
``format_wakeup_prompt`` shapes (completion, watch_match) so the WebUI can
render a collapsed summary card without re-showing the agent-facing
``[IMPORTANT: …]`` scaffolding. Everything else must return None so the UI
keeps its raw-notice fallback.
"""

from api.background_process import format_wakeup_prompt
from api.process_event_utils import attach_wakeup_display_meta, wakeup_display_meta


def _completion_evt(**overrides):
    evt = {
        "type": "completion",
        "session_id": "proc_123",
        "command": "npm run build",
        "exit_code": 0,
        "output": "done\nall green",
    }
    evt.update(overrides)
    return evt


def test_completion_round_trip():
    meta = wakeup_display_meta(format_wakeup_prompt(_completion_evt()))
    assert meta == {
        "type": "completion",
        "task_id": "proc_123",
        "command": "npm run build",
        "exit_code": 0,
    }


def test_completion_missing_exit_code_round_trips_as_question_mark():
    evt = _completion_evt()
    del evt["exit_code"]
    meta = wakeup_display_meta(format_wakeup_prompt(evt))
    assert meta["exit_code"] == "?"


def test_completion_nonzero_exit_and_truncated_output():
    evt = _completion_evt(exit_code=3, output="x" * 5000)
    meta = wakeup_display_meta(format_wakeup_prompt(evt))
    assert meta["exit_code"] == 3


def test_completion_negative_exit_code_round_trips():
    # Signal-killed processes report negative returncodes (e.g. SIGKILL → -9).
    meta = wakeup_display_meta(format_wakeup_prompt(_completion_evt(exit_code=-9)))
    assert meta["exit_code"] == -9


def test_completion_empty_sid_and_command():
    meta = wakeup_display_meta(format_wakeup_prompt(_completion_evt(session_id="", command="")))
    assert meta["type"] == "completion"
    assert meta["task_id"] == ""
    assert meta["command"] == ""


def test_watch_match_header_only_no_suppression_derived():
    # The producer appends a suppression note to the body; the parser must NOT
    # lift it into metadata — it is free-form output tail (#6350 finding 2).
    evt = {
        "type": "watch_match",
        "session_id": "w1",
        "command": "tail -f app.log",
        "pattern": "ERROR.*timeout",
        "output": "ERROR line",
        "suppressed": 3,
    }
    meta = wakeup_display_meta(format_wakeup_prompt(evt))
    assert meta == {
        "type": "watch_match",
        "task_id": "w1",
        "command": "tail -f app.log",
        "pattern": "ERROR.*timeout",
    }
    assert "suppressed" not in meta


def test_watch_match_output_that_looks_like_suppression_is_not_misparsed():
    # Adversarial: legitimate output ends with the exact suppression phrasing
    # while the event's real suppressed count is 0. The metadata must carry no
    # suppression field, and the body must still contain the output verbatim so
    # the UI renders it (the phrase is not stripped).
    evt = {
        "type": "watch_match",
        "session_id": "w2",
        "command": "tail",
        "pattern": "ERR",
        "output": "log line\n(3 earlier matches were suppressed by rate limit)",
        "suppressed": 0,
    }
    body = format_wakeup_prompt(evt)
    meta = wakeup_display_meta(body)
    assert meta == {
        "type": "watch_match",
        "task_id": "w2",
        "command": "tail",
        "pattern": "ERR",
    }
    assert "suppressed" not in meta
    assert "(3 earlier matches were suppressed by rate limit)" in body


def test_watch_match_pattern_with_quotes_round_trips():
    evt = {
        "type": "watch_match",
        "session_id": "w2",
        "command": "tail",
        "pattern": 'has "quotes" inside',
        "output": "m",
    }
    meta = wakeup_display_meta(format_wakeup_prompt(evt))
    assert meta["pattern"] == 'has "quotes" inside'
    assert "suppressed" not in meta


def test_free_text_and_unknown_bodies_return_none():
    assert wakeup_display_meta("[IMPORTANT: watcher disabled after errors]") is None
    assert wakeup_display_meta("hello") is None
    assert wakeup_display_meta("") is None
    assert wakeup_display_meta(None) is None


def test_attach_stamps_only_process_wakeup_sources():
    body = format_wakeup_prompt(_completion_evt())
    msg = {"role": "user", "content": body, "_source": "process_wakeup"}
    attach_wakeup_display_meta(msg, "process_wakeup")
    assert msg["_wakeup_meta"]["task_id"] == "proc_123"

    untouched = {"role": "user", "content": body, "_source": "telegram"}
    attach_wakeup_display_meta(untouched, "telegram")
    assert "_wakeup_meta" not in untouched


def test_attach_is_idempotent_and_tolerates_junk():
    msg = {"role": "user", "content": "unparseable", "_source": "process_wakeup"}
    attach_wakeup_display_meta(msg, "process_wakeup")
    assert "_wakeup_meta" not in msg

    stamped = {
        "role": "user",
        "content": format_wakeup_prompt(_completion_evt()),
        "_wakeup_meta": {"type": "completion", "task_id": "keep_me"},
    }
    attach_wakeup_display_meta(stamped, "process_wakeup")
    assert stamped["_wakeup_meta"]["task_id"] == "keep_me"

    # Never raises on non-dict / content-less inputs.
    attach_wakeup_display_meta(None, "process_wakeup")
    attach_wakeup_display_meta({"role": "user", "content": ["parts"]}, "process_wakeup")
