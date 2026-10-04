"""The silent-failure retry must not replay work that already happened.

Drives the real `_run_agent_streaming` settlement path through the
`worker_scene` fixture (its external Agent is synthetic — no provider call, no
tool execution). The four shapes below are the ones the review pinned on the
first revision of this fix: a turn that ran a tool, a turn whose tool rows were
already persisted, a turn that exited on the tool-iteration limit, and a turn
that truly produced nothing.
"""

import threading

from api import streaming
from tests.test_steer_worker_boundaries import worker_scene as worker_scene


def _user_row():
    return {"role": "user", "content": "Do the task."}


def _materialized_user_row():
    """A user row as the worker persists it (turn bookkeeping attached)."""
    row = _user_row()
    row.update({"timestamp": 1.0, "_source": "webui", "attachments": []})
    return row


def _tool_rows():
    """A completed tool round: the assistant call plus its result row."""
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "shell", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "done"},
    ]


def _silent_result():
    """A turn that produced nothing: no reply, no error, no text."""
    return {"messages": [_user_row()]}


def _runs(scene):
    return scene.calls.count("run")


def _session_text(scene):
    return "\n".join(str(row.get("content") or "") for row in scene.session.messages)


def _error_rows(scene):
    return [row for row in scene.session.messages if row.get("_error")]


# ── Work already done is never replayed ──────────────────────────────────────


def test_tool_activity_blocks_the_silent_retry(worker_scene):
    """A turn that ran a tool already produced side effects."""
    scene = worker_scene
    scene.result = {"messages": [_user_row()] + _tool_rows()}

    scene.run()

    assert _runs(scene) == 1, "a tool-running turn must not be replayed"
    assert "No response from provider" in _session_text(scene)


class _LiveToolProgressAgent:
    """Synthetic agent that reports one streamed tool round, then stays silent.

    Mirrors production: a turn that ran a tool always emits tool progress, and
    the worker's per-turn guard (`_live_tool_calls`) reads that signal — not the
    transcript — because the two attempts' rows are identical once the first
    attempt's tool rows have been persisted.
    """

    instances = []
    result_messages = []

    def __init__(self, **kwargs):
        self.session_id = kwargs.get("session_id")
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.context_compressor = None
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.session_estimated_cost_usd = None
        self.reasoning_config = None
        self.ephemeral_system_prompt = None
        self._last_error = None
        self.pending = []
        self.pending_lock = threading.Lock()
        self.runs = 0
        _LiveToolProgressAgent.instances.append(self)

    def run_conversation(self, **_kwargs):
        self.runs += 1
        if self.tool_progress_callback is not None:
            self.tool_progress_callback("tool.started", "shell", "", "{}")
        return {"messages": [dict(row) for row in self.result_messages]}

    def interrupt(self, _message):
        return None

    def steer(self, _text):
        return True

    def _drain_pending_steer(self):
        return None


def test_persisted_tool_rows_do_not_replay_the_turn(worker_scene, monkeypatch):
    """Tool work already in the transcript must not be executed a second time."""
    scene = worker_scene
    persisted = [_materialized_user_row()] + _tool_rows()
    scene.session.messages = [dict(row) for row in persisted]
    scene.session.context_messages = [dict(row) for row in persisted]
    _LiveToolProgressAgent.instances = []
    _LiveToolProgressAgent.result_messages = persisted
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: _LiveToolProgressAgent)

    scene.run()

    assert [agent.runs for agent in _LiveToolProgressAgent.instances] == [1], (
        "a turn that streamed tool progress must not be replayed"
    )
    tool_rows = [row for row in scene.session.messages if row.get("role") == "tool"]
    assert len(tool_rows) == 1, "the tool round must not be executed a second time"


def test_tool_limit_exit_keeps_its_own_card(worker_scene):
    """The tool-iteration limit owns the card; the silent branch must not shadow it."""
    scene = worker_scene
    scene.result = {
        "turn_exit_reason": "max_iterations_reached(30/30)",
        "messages": [_user_row()] + _tool_rows(),
    }

    scene.run()

    assert _runs(scene) == 1, "a tool-limit exit is not a silent failure"
    assert "Tool iteration limit reached" in _session_text(scene)


# ── The genuinely silent turn is retried, once ───────────────────────────────


def _silent_then(scene, second_result):
    """Return an on_run hook: silent first attempt, `second_result` afterwards."""

    def on_run():
        scene.result = _silent_result() if _runs(scene) == 1 else second_result

    return on_run


def test_truly_silent_turn_retries_once_and_recovers(worker_scene):
    scene = worker_scene
    scene.on_run = _silent_then(scene, None)  # second attempt: fixture success result

    scene.run()

    assert _runs(scene) == 2, "a silent turn earns exactly one retry"
    assert _error_rows(scene) == [], "the recovered turn must not carry an error card"
    assert "Finished." in _session_text(scene)


def test_silent_retry_does_not_route_through_the_credential_self_heal(
    worker_scene, monkeypatch,
):
    """A silent cut is not a credential problem: never re-read auth.json for it."""
    scene = worker_scene
    credential_calls = []

    def spy(*args, **kwargs):
        credential_calls.append((args, kwargs))
        return None

    monkeypatch.setattr(streaming, "_attempt_credential_self_heal", spy)
    monkeypatch.setattr(streaming, "_attempt_model_alias_credential_self_heal", spy)
    scene.on_run = _silent_then(scene, None)

    scene.run()

    assert _runs(scene) == 2, (
        "the retry must proceed on the runtime already resolved for the turn, "
        "not wait on a credential refresh it does not need"
    )
    assert credential_calls == [], "the credential self-heal was consulted"


def test_a_failed_retry_keeps_the_no_response_card(worker_scene):
    """Both attempts silent: the card stays the pre-retry no-response wording."""
    scene = worker_scene
    scene.result = _silent_result()

    scene.run()

    assert _runs(scene) == 2
    errors = _error_rows(scene)
    assert len(errors) == 1
    assert "No response from provider" in errors[0]["content"]
    assert "Authentication failed" not in errors[0]["content"], (
        "a non-auth failure must not be labelled as an auth failure"
    )


def test_a_failed_retry_reports_the_retrys_own_error(worker_scene):
    """The second attempt's real cause owns the card, not the silent first one."""
    scene = worker_scene
    scene.on_run = _silent_then(scene, {
        "error": "429 rate limit exceeded",
        "messages": [_user_row()],
    })

    scene.run()

    assert _runs(scene) == 2
    errors = _error_rows(scene)
    assert len(errors) == 1
    assert "No response from provider" not in errors[0]["content"], (
        "the retry's own failure must replace the silent-first-attempt wording"
    )
