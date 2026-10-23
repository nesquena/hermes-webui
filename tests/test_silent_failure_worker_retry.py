"""The silent-failure retry must not replay work that already happened.

Drives the real `_run_agent_streaming` settlement path through the
`worker_scene` fixture (its external Agent is synthetic — no provider call, no
tool execution). The four shapes below are the ones the review pinned on the
first revision of this fix: a turn that ran a tool, a turn whose tool rows were
already persisted, a turn that exited on the tool-iteration limit, and a turn
that truly produced nothing.
"""

import threading

import pytest

from api import streaming
from api import config
from api import config as api_config
from tests.test_steer_worker_boundaries import worker_scene as worker_scene


@pytest.fixture(autouse=True)
def _enable_silent_retry(monkeypatch):
    """These worker cases exercise the retry, so opt in for the module.

    The retry is off by default (see ``_silent_retry_enabled``): the Agent has
    its own budgeted empty-response ladder and a WebUI whole-turn replay resets
    that budget, so an operator must enable it. Every test here is about the
    retry's behaviour once enabled; the default-off shape is pinned separately
    at the end of this module.
    """
    monkeypatch.setenv("HERMES_WEBUI_SILENT_FAILURE_RETRY", "1")


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
    emit_tool_progress = True

    def __init__(self, **kwargs):
        self.session_id = kwargs.get("session_id")
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.status_callback = kwargs.get("status_callback")
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
        if self.emit_tool_progress and self.tool_progress_callback is not None:
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


def test_an_echoed_transcript_is_not_retried(worker_scene, monkeypatch):
    """A result identical to the pre-turn context is not a retryable cut."""
    scene = worker_scene
    echoed = [_user_row(), {"role": "assistant", "content": "Earlier answer"}]
    scene.session.messages = [dict(row) for row in echoed]
    scene.session.context_messages = [dict(row) for row in echoed]
    _LiveToolProgressAgent.instances = []
    _LiveToolProgressAgent.result_messages = echoed
    _LiveToolProgressAgent.emit_tool_progress = False
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: _LiveToolProgressAgent)
    try:
        scene.run()
    finally:
        _LiveToolProgressAgent.emit_tool_progress = True

    assert [agent.runs for agent in _LiveToolProgressAgent.instances] == [1], (
        "re-sending an identical context cannot change the result"
    )


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
    assert "429 rate limit exceeded" in errors[0]["content"], (
        "the retry's own provider text must survive, not just its label"
    )


def test_a_failed_retry_keeps_the_earlier_display_transcript(worker_scene):
    """Settlement owns the writeback: the raw context rows must not replace it.

    Assigning the raw rows back to ``s.messages`` dropped every display-only row
    and saved the current prompt twice. The retry here returns only current
    rows while the session already holds an earlier turn.
    """
    scene = worker_scene
    earlier = [
        {"role": "user", "content": "Earlier question."},
        {"role": "assistant", "content": "Earlier answer."},
    ]
    scene.session.messages = [dict(row) for row in earlier]
    scene.session.context_messages = [dict(row) for row in earlier]
    scene.on_run = _silent_then(scene, {
        "error": "429 rate limit exceeded",
        "messages": [_materialized_user_row()],
    })

    scene.run()

    assert _runs(scene) == 2
    text = _session_text(scene)
    assert "Earlier question." in text, "the earlier display rows must survive"
    assert "Earlier answer." in text
    assert text.count("Do the task.") == 1, (
        "the current prompt must be saved exactly once"
    )



def test_gate_off_means_a_silent_turn_is_not_retried(worker_scene, monkeypatch):
    """Default posture: no opt-in, no whole-turn replay.

    The module-wide fixture enables the retry; this case removes the only place
    it can be enabled, so the worker must settle the silent turn exactly once
    and let the Agent's own empty-response ladder own recovery. Without this
    case a regression that hard-codes the retry on would pass every other test
    in this file.
    """
    monkeypatch.delenv("HERMES_WEBUI_SILENT_FAILURE_RETRY", raising=False)
    monkeypatch.setattr(
        streaming, "_silent_retry_enabled", lambda *a, **k: False,
    )

    scene = worker_scene
    scene.on_run = _silent_then(scene, None)

    scene.run()

    assert _runs(scene) == 1, "the gate is off: no second provider call"


# ── Blocker: an answer is owned by the turn, not by its text ─────────────────


def _history_with_an_earlier_yes():
    """History already holds an assistant ``Yes.`` from an earlier turn."""
    return [
        {"role": "user", "content": "Earlier question."},
        {"role": "assistant", "content": "Yes."},
        _materialized_user_row(),
    ]


def _realistic_yes_scene(scene, replies):
    """Realistic Agent: returns history + [user, reply]; ``None`` is a silent cut.

    No user row is pre-seeded and no transcript row is injected: the fake
    produces what the installed Agent produces, so the assertions below cannot
    be satisfied by the input the test itself supplied.
    """
    history = [
        {"role": "user", "content": "Earlier question."},
        {"role": "assistant", "content": "Yes."},
    ]
    scene.session.messages = [dict(row) for row in history]
    scene.session.context_messages = [dict(row) for row in history]

    def on_run():
        reply = replies[_runs(scene) - 1]
        rows = [dict(row) for row in history] + [
            {"role": "user", "content": "Do the task."},
        ]
        if reply is not None:
            rows.append({"role": "assistant", "content": reply})
        scene.result = {"messages": rows}

    return on_run


def _yes_answer_rows(scene):
    """Assistant rows whose whole text is the (repeated) answer ``Yes.``."""
    return [
        row for row in scene.session.messages
        if isinstance(row, dict)
        and row.get("role") == "assistant"
        and row.get("content") == "Yes."
    ]


def test_an_equal_text_answer_is_not_treated_as_a_silent_turn(worker_scene):
    """One attempt answers with a text history already used: it still answered.

    Settlement drops a reply whose text equals an earlier one (an existing
    master defect), which zeroes ``_assistant_added``. Reading that overwritten
    value made the gate replay a turn that did answer: an extra provider call,
    and — because the retry's answer was dropped again while ``_assistant_added``
    was forced True — a bare ``done`` with no answer and no card.
    """
    scene = worker_scene
    scene.on_run = _realistic_yes_scene(scene, ["Yes."])

    scene.run()

    assert _runs(scene) == 1, "a turn that answered must not be replayed"
    errors = _error_rows(scene)
    assert len(errors) == 1, "the dropped answer must still surface a card"
    assert "No response from provider" in errors[0]["content"]


def test_a_retry_whose_answer_is_dropped_keeps_the_error_card(worker_scene):
    """Silent first attempt, retry answers with the repeated text: no bare done."""
    scene = worker_scene
    scene.on_run = _realistic_yes_scene(scene, [None, "Yes."])

    scene.run()

    assert _runs(scene) == 2, "a silent turn earns exactly one retry"
    errors = _error_rows(scene)
    assert len(errors) == 1, (
        "a retry whose reply settlement dropped must not end on a bare done"
    )
    # Settlement drops a reply whose text repeats an earlier one (a pre-existing
    # master defect, out of scope here). The retry must not turn that into a
    # silent ``done``: the dropped answer keeps the visible card, exactly as an
    # un-retried attempt does on master.
    assert len(_yes_answer_rows(scene)) == 1, (
        "the settled transcript holds the earlier answer only; the dropped "
        "retry answer is what the error card is reporting"
    )


def test_retry_accepts_an_equal_text_answer_owned_by_the_current_turn(worker_scene):
    """A new answer may repeat an older one verbatim; it is still the answer.

    Realistic Agent shape: the fake returns ``conversation_history + [user,
    reply]`` — what the installed Agent returns — and nothing seeds the current
    turn into the transcript, so neither assertion below can be satisfied by
    input the test supplied. The earlier ``Yes.`` lives only in the DISPLAY
    (the context was compacted), so the retry's identical text is the only copy
    the model context has ever seen, and both copies must end up in the
    transcript: the old one for the reader, the new one as this turn's answer.
    """
    scene = worker_scene
    display = [
        {"role": "user", "content": "Earlier question."},
        {"role": "assistant", "content": "Yes."},
        {"role": "user", "content": "Second question."},
        {"role": "assistant", "content": "Second answer."},
    ]
    context = [dict(row) for row in display[2:]]
    scene.session.messages = [dict(row) for row in display]
    scene.session.context_messages = [dict(row) for row in context]

    def on_run():
        rows = [dict(row) for row in context] + [
            {"role": "user", "content": "Do the task."},
        ]
        if _runs(scene) == 2:
            rows.append({"role": "assistant", "content": "Yes."})
        scene.result = {"messages": rows}

    scene.on_run = on_run

    scene.run()

    assert _runs(scene) == 2, "the silent turn still earns exactly one retry"
    assert _error_rows(scene) == [], (
        "an equal-text answer owned by the current turn must settle, not error"
    )
    assert len(_yes_answer_rows(scene)) == 2, (
        "the earlier answer and the retry's identical answer are two turns and "
        "must both be in the transcript"
    )
    assert _session_text(scene).count("Do the task.") == 1, (
        "the current prompt must be saved exactly once"
    )


def test_retry_rejects_a_bare_replay_of_historical_rows(worker_scene):
    """The negative control: replaying only history is not a recovery."""
    scene = worker_scene
    history = _history_with_an_earlier_yes()
    scene.session.messages = [dict(row) for row in history]
    scene.session.context_messages = [dict(row) for row in history]
    scene.result = {"messages": [dict(row) for row in history]}

    scene.run()

    errors = _error_rows(scene)
    assert len(errors) == 1, "a replayed historical transcript must not settle"
    assert "No response from provider" in errors[0]["content"]


# ── Blocker: the retry's own failure owns the card ───────────────────────────


class _RaisingRetryAgent:
    """Synthetic agent whose call raises, as a provider failure does.

    The retry builds a *fresh* agent, so a per-instance counter cannot tell the
    first attempt from the retry. The counter is class-level: the first
    ``run_conversation`` returns the silent result, every one after raises.
    """

    instances = []
    result_messages = []
    raise_on_run = None
    calls = 0

    def __init__(self, **kwargs):
        self.session_id = kwargs.get("session_id")
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.status_callback = kwargs.get("status_callback")
        self.context_compressor = None
        self.session_prompt_tokens = self.session_completion_tokens = 0
        self.session_cache_read_tokens = self.session_cache_write_tokens = 0
        self.session_estimated_cost_usd = None
        self.reasoning_config = self.ephemeral_system_prompt = self._last_error = None
        self.pending = []
        self.pending_lock = threading.Lock()
        self.runs = 0
        _RaisingRetryAgent.instances.append(self)

    def run_conversation(self, **_kwargs):
        self.runs += 1
        _RaisingRetryAgent.calls += 1
        if _RaisingRetryAgent.calls >= 2 and _RaisingRetryAgent.raise_on_run is not None:
            raise _RaisingRetryAgent.raise_on_run
        return {"messages": [dict(row) for row in self.result_messages]}

    def interrupt(self, _message):
        return None

    def steer(self, _text):
        return True

    def _drain_pending_steer(self):
        return None


def _run_with_raising_retry(scene, monkeypatch, exc, silent_rows):
    _RaisingRetryAgent.instances = []
    _RaisingRetryAgent.calls = 0
    _RaisingRetryAgent.result_messages = silent_rows
    _RaisingRetryAgent.raise_on_run = exc
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: _RaisingRetryAgent)
    try:
        scene.run()
    finally:
        _RaisingRetryAgent.raise_on_run = None
    return _RaisingRetryAgent.instances


def test_raised_rate_limit_on_the_retry_is_classified_as_rate_limit(
    worker_scene, monkeypatch,
):
    """A retry that *raises* 429 must not be reported as a silent no-response."""
    scene = worker_scene
    agents = _run_with_raising_retry(
        scene, monkeypatch,
        RuntimeError("429 rate limit exceeded, please retry later"),
        [_user_row()],
    )

    assert len(agents) == 2, "the retry builds a fresh agent: two instances"
    assert _RaisingRetryAgent.calls == 2, "the retry must have run"
    errors = _error_rows(scene)
    assert len(errors) == 1
    body = errors[0]["content"]
    assert "No response from provider" not in body, (
        "a raised retry error is not a silent turn"
    )
    assert "rate limit" in body.lower(), (
        f"the retry's own cause must reach the card, got: {body[:200]}"
    )


def test_raised_quota_error_on_the_retry_is_classified_as_quota(
    worker_scene, monkeypatch,
):
    """A retry that raises ``402 insufficient_quota`` classifies as quota."""
    scene = worker_scene
    _run_with_raising_retry(
        scene, monkeypatch,
        RuntimeError("402 insufficient_quota: your balance is exhausted"),
        [_user_row()],
    )

    errors = _error_rows(scene)
    assert len(errors) == 1
    body = errors[0]["content"]
    assert "No response from provider" not in body
    assert "quota" in body.lower() or "balance" in body.lower() or "credit" in body.lower(), (
        f"quota wording expected, got: {body[:200]}"
    )


# ── Blocker: every form of current-turn activity counts ──────────────────────


class _ActivityAgent:
    """Synthetic agent whose first attempt shows activity but no final answer."""

    instances = []
    calls = 0
    result_messages = []
    reasoning_content = None
    fire_interim = False

    def __init__(self, session_id=None, tool_progress_callback=None,
                 interim_assistant_callback=None, **_kwargs):
        self.session_id = session_id
        self.tool_progress_callback = tool_progress_callback
        self.context_compressor = None
        self.session_prompt_tokens = self.session_completion_tokens = 0
        self.session_cache_read_tokens = self.session_cache_write_tokens = 0
        self.session_estimated_cost_usd = None
        self.reasoning_config = self.ephemeral_system_prompt = self._last_error = None
        self.pending = []
        self.pending_lock = threading.Lock()
        # Named explicitly so the worker's signature probe wires the callback,
        # exactly as it does for a real AIAgent.
        self.interim_assistant_callback = interim_assistant_callback
        _ActivityAgent.instances.append(self)

    def run_conversation(self, **_kwargs):
        _ActivityAgent.calls += 1
        first = _ActivityAgent.calls == 1
        if first and _ActivityAgent.fire_interim and self.interim_assistant_callback:
            self.interim_assistant_callback("Let me look into that.")
        result = {"messages": [dict(row) for row in _ActivityAgent.result_messages]}
        if first and _ActivityAgent.reasoning_content:
            result["reasoning_content"] = _ActivityAgent.reasoning_content
        return result

    def interrupt(self, _message):
        return None

    def steer(self, _text):
        return True

    def _drain_pending_steer(self):
        return None


def _run_activity_scene(scene, monkeypatch, *, reasoning=None, interim=False):
    _ActivityAgent.instances = []
    _ActivityAgent.calls = 0
    _ActivityAgent.result_messages = [_user_row()]
    _ActivityAgent.reasoning_content = reasoning
    _ActivityAgent.fire_interim = interim
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: _ActivityAgent)
    try:
        scene.run()
    finally:
        _ActivityAgent.reasoning_content = None
        _ActivityAgent.fire_interim = False
    return _ActivityAgent.calls


def test_result_only_reasoning_blocks_the_silent_retry(worker_scene, monkeypatch):
    """Reasoning returned on the result — not streamed — is still activity."""
    scene = worker_scene
    calls = _run_activity_scene(
        scene, monkeypatch,
        reasoning="I considered the request and decided to answer shortly.",
    )

    assert calls == 1, (
        "a turn that produced reasoning did work and must not be replayed"
    )


def test_an_interim_assistant_message_blocks_the_silent_retry(
    worker_scene, monkeypatch,
):
    """Visible interim output is work already shown to the user."""
    scene = worker_scene
    calls = _run_activity_scene(scene, monkeypatch, interim=True)

    assert calls == 1, (
        "a turn that emitted an interim assistant message must not be replayed"
    )


# ── Follow-up: a genuinely empty fresh turn is retryable ─────────────────────


def test_a_fresh_silent_turn_with_no_rows_is_retried(worker_scene):
    """An empty result in a fresh session is a cut, not an echoed context."""
    scene = worker_scene
    scene.session.messages = []
    scene.session.context_messages = []
    scene.result = {"messages": []}

    scene.run()

    assert _runs(scene) == 2, (
        "an empty fresh turn produced nothing to echo: it earns its retry"
    )


def test_a_fresh_silent_turn_with_no_messages_key_is_retried(worker_scene):
    """A result carrying neither messages nor an answer is still retryable."""
    scene = worker_scene
    scene.session.messages = []
    scene.session.context_messages = []
    scene.result = {}

    scene.run()

    assert _runs(scene) == 2, (
        "an empty result dict must not be read as an echoed transcript"
    )


# ── Follow-up: a failed retry keeps its partial result ───────────────────────


def test_a_failed_retry_keeps_its_partial_tool_rows(worker_scene):
    """A retry that returns a tool outcome *and* an error keeps the outcome.

    Dropping it loses work the turn really performed; the canonical settlement
    path is what makes it survive a reload.
    """
    scene = worker_scene
    scene.on_run = _silent_then(scene, {
        "error": "429 rate limit exceeded",
        "messages": [_user_row()] + _tool_rows(),
    })

    scene.run()

    assert _runs(scene) == 2, "the retry must have run"
    tool_rows = [row for row in scene.session.messages if row.get("role") == "tool"]
    assert len(tool_rows) == 1, (
        "the retry returned a tool outcome; discarding it loses real work"
    )
    assert tool_rows[0].get("content") == "done"


# ── The retry reads the session's own profile config ────────────────────────


def test_retry_opt_in_is_read_from_the_sessions_profile(worker_scene, monkeypatch):
    """Every other worker setting comes from the session's profile home.

    Reading the ambient config made the opt-in depend on the process default
    instead: a session whose profile has the retry off still retried whenever
    the ambient config (or another profile) had it on.
    """
    scene = worker_scene
    scene.on_run = _silent_then(scene, None)

    monkeypatch.delenv("HERMES_WEBUI_SILENT_FAILURE_RETRY", raising=False)
    monkeypatch.setattr(
        api_config, "get_config_for_profile_home",
        lambda *_a, **_k: {"webui_silent_failure_retry": False},
    )
    # The ambient config says yes; the session's profile says no. The profile wins.
    monkeypatch.setattr(
        streaming, "get_config",
        lambda *_a, **_k: {"webui_silent_failure_retry": True},
    )
    monkeypatch.setattr(
        api_config, "get_config",
        lambda *_a, **_k: {"webui_silent_failure_retry": True},
    )

    scene.run()

    assert _runs(scene) == 1, (
        "the session's profile has the retry off: the ambient config must not "
        "enable it for this turn"
    )


# ── Replacing the agent carries its usage and closes the old one ────────────


def test_a_replaced_agent_carries_usage_and_is_closed(worker_scene, monkeypatch):
    """A fresh agent starts at zero; the turn's real spend would be lost."""
    scene = worker_scene
    built = []

    def on_init():
        built.append(scene.agent)

    def on_run():
        first = _runs(scene) == 1
        scene.result = _silent_result() if first else None
        if first:
            # The attempt that went silent did spend tokens.
            scene.agent.session_prompt_tokens = 600
            scene.agent.session_estimated_cost_usd = 0.06
        else:
            # The replacement starts from the carried totals and adds its own.
            scene.agent.session_prompt_tokens += 200
            scene.agent.session_estimated_cost_usd += 0.02

    scene.on_init = on_init
    scene.on_run = on_run

    scene.run()

    assert _runs(scene) == 2, "the silent turn must have been retried"
    assert len(built) == 2, "the silent retry builds exactly one replacement"
    assert built[1].session_prompt_tokens == 800, (
        "the replacement must inherit the turn's spend and add its own"
    )
    assert built[1].session_estimated_cost_usd == pytest.approx(0.08)


# ── Finding B: the settled display transcript is authoritative ──────────────


def test_a_failed_retry_keeps_display_only_rows_when_context_was_compacted(worker_scene):
    """Display and context differ (compaction): the save must keep the display.

    ``_settle_result_messages`` already wrote the merged DISPLAY transcript and
    returned the raw, context-shaped rows. Writing those rows back dropped every
    display-only turn and (when the retry echoed the prompt) saved the current
    prompt twice.
    """
    scene = worker_scene
    display = [
        {"role": "user", "content": "First question."},
        {"role": "assistant", "content": "First answer."},
        {"role": "user", "content": "Second question."},
        {"role": "assistant", "content": "Second answer."},
    ]
    context = [dict(row) for row in display[2:]]
    scene.session.messages = [dict(row) for row in display]
    scene.session.context_messages = [dict(row) for row in context]

    def on_run():
        if _runs(scene) == 1:
            scene.result = {"messages": [
                {"role": "user", "content": "Do the task."},
            ]}
        else:
            scene.result = {
                "error": "429 rate limit exceeded",
                "messages": [dict(row) for row in context] + [
                    {"role": "user", "content": "Do the task."},
                ] + _tool_rows(),
            }

    scene.on_run = on_run

    scene.run()

    assert _runs(scene) == 2, "the silent turn must have been retried"
    text = _session_text(scene)
    for earlier in ("First question.", "First answer.",
                    "Second question.", "Second answer."):
        assert earlier in text, (
            "display-only history was lost when settlement returned raw "
            f"context rows ({earlier!r} missing)"
        )
    assert text.count("Do the task.") == 1, (
        "the current prompt must be saved exactly once"
    )
    tool_rows = [row for row in scene.session.messages if row.get("role") == "tool"]
    assert [row.get("content") for row in tool_rows] == ["done"], (
        "the retry's tool outcome must survive the failure"
    )


# ── A Stop that lands while the retry runs is a cancel, not a failure ───────


def test_stop_during_the_retry_is_saved_as_a_cancel(worker_scene):
    """The pre-retry cancel check cannot see a Stop that lands during the retry.

    Nothing looked at ``cancel_event`` between the retry returning (or raising)
    and settlement, so the turn was saved as the retry's provider error: an
    ``apperror`` card and a journal ``apperror`` terminal instead of a cancel.
    """
    scene = worker_scene
    # A real Stop signals this event from the request thread; the worker picks
    # the registered flag up at admission. Signal it here the same way so the
    # Stop lands while the retry is in flight without re-entering the worker's
    # own per-session lock (cancel_stream runs the whole cleanup, which would
    # deadlock against the worker holding that lock).
    stop = threading.Event()
    config.CANCEL_FLAGS["run"] = stop

    def on_run():
        if _runs(scene) == 1:
            scene.result = {"messages": [
                {"role": "user", "content": "Do the task."},
            ]}
            return
        # The retry is running when the user hits Stop; its own result is a 429.
        stop.set()
        scene.result = {
            "error": "429 rate limit exceeded",
            "messages": [{"role": "user", "content": "Do the task."}],
        }

    scene.on_run = on_run

    scene.run()

    assert _runs(scene) == 2, "the turn must have reached the retry"
    emitted = [event for event, _payload in list(scene.events.queue)]
    assert "cancel" in emitted, (
        "a Stop during the retry must end the turn as a cancel"
    )
    assert "apperror" not in emitted, (
        "the retry's 429 must not be reported as the outcome of a cancelled turn"
    )
    assert _error_rows(scene) == [], (
        "a cancelled turn must not persist the retry's provider error card"
    )


# ── A Stop that lands after the retry is a cancel, whatever it produced ────


def _stop_during_run(scene):
    """Install the per-stream Stop flag; the caller signals it."""

    stop = threading.Event()
    config.CANCEL_FLAGS["run"] = stop
    return stop


def test_stop_during_a_retry_that_answers_is_saved_as_a_cancel(worker_scene):
    """A retry that produced an answer the user stopped must not be persisted.

    The pre-retry check already ran when the user hit Stop, so the answer the
    retry returned is not the turn's outcome: settlement must be the cancel
    path (``cancel`` frame, cancelled turn), not a saved answer.
    """
    scene = worker_scene
    stop = _stop_during_run(scene)

    def on_run():
        if _runs(scene) == 1:
            scene.result = _silent_result()
            return
        stop.set()
        scene.result = {
            "messages": [
                _user_row(),
                {"role": "assistant", "content": "Answered while stopped."},
            ],
        }

    scene.on_run = on_run

    scene.run()

    assert _runs(scene) == 2, "the turn must have reached the retry"
    emitted = [event for event, _payload in list(scene.events.queue)]
    assert "cancel" in emitted, "a stopped retry must settle as a cancel"
    assert "apperror" not in emitted
    assert "done" not in emitted, (
        "the retry's answer must not be reported as this turn's result"
    )
    assert "Answered while stopped." not in _session_text(scene), (
        "the cancelled turn must not persist the retry's answer"
    )
    assert _error_rows(scene) == []


def test_stop_during_a_retry_that_raises_is_saved_as_a_cancel(worker_scene):
    """Both retry outcomes — returned and raised — settle as a cancel."""
    scene = worker_scene
    stop = _stop_during_run(scene)

    def on_run():
        if _runs(scene) == 1:
            scene.result = _silent_result()
            return
        stop.set()
        raise RuntimeError("429 rate limit exceeded")

    scene.on_run = on_run

    scene.run()

    assert _runs(scene) == 2, "the turn must have reached the retry"
    emitted = [event for event, _payload in list(scene.events.queue)]
    assert "cancel" in emitted
    assert "apperror" not in emitted, (
        "a raised retry error is not the outcome of a cancelled turn"
    )
    assert _error_rows(scene) == []


def test_stop_observed_before_the_retry_settles_as_a_cancel(
    worker_scene, monkeypatch,
):
    """A Stop seen at the failure gate is a cancel, not a second provider call.

    The flag is signalled from inside ``_classify_provider_error``, i.e. after
    the pre-settlement checks and before the gate's retry branch: no second
    provider call may be spent on a turn the user has already stopped.
    """
    scene = worker_scene
    stop = _stop_during_run(scene)
    classify = streaming._classify_provider_error

    def classify_and_stop(*args, **kwargs):
        stop.set()
        return classify(*args, **kwargs)

    monkeypatch.setattr(streaming, "_classify_provider_error", classify_and_stop)
    scene.result = _silent_result()

    scene.run()

    assert _runs(scene) == 1, (
        "a turn the user stopped must not be retried against the provider"
    )
    emitted = [event for event, _payload in list(scene.events.queue)]
    assert "cancel" in emitted
    assert "apperror" not in emitted


def test_stop_after_a_settled_answer_ends_with_a_cancel_frame(
    worker_scene, monkeypatch,
):
    """The same Stop on the normal path must not end with no terminal frame.

    ``put`` drops every event but ``cancel`` once the flag is set, so a Stop
    that lands after the pre-settlement checks used to leave the client with no
    terminal event at all.
    """
    scene = worker_scene
    stop = _stop_during_run(scene)
    classify = streaming._classify_provider_error

    def classify_and_stop(*args, **kwargs):
        stop.set()
        return classify(*args, **kwargs)

    monkeypatch.setattr(streaming, "_classify_provider_error", classify_and_stop)
    scene.result = {
        "messages": [
            _user_row(),
            {"role": "assistant", "content": "Finished."},
        ],
    }

    scene.run()

    emitted = [event for event, _payload in list(scene.events.queue)]
    assert "cancel" in emitted, (
        "a stopped turn must terminate with a cancel frame"
    )
    assert "done" not in emitted


# ── Compression inside the retry is migrated like the first attempt ────────


def test_retry_rotation_is_migrated_to_the_continuation_session(worker_scene):
    """The retry runs after the pre-turn compression migration.

    If it rotates ``agent.session_id``, the turn was still saved under the old
    id while the Agent had moved on, and no ``compressed`` event was emitted.
    """
    scene = worker_scene

    def on_run():
        if _runs(scene) == 1:
            scene.result = {"messages": [
                {"role": "user", "content": "Do the task."},
            ]}
            return
        scene.agent.session_id = "continuation"
        scene.result = {"messages": [
            {"role": "user", "content": "Do the task."},
            {"role": "assistant", "content": "Answered."},
        ]}

    scene.on_run = on_run

    scene.run()

    assert _runs(scene) == 2, "the silent turn must have been retried"
    assert scene.session.session_id == "continuation", (
        "the retry rotated the session: the turn must be saved on the "
        "continuation, not the stale parent"
    )
    emitted = list(scene.events.queue)
    names = [event for event, _payload in emitted]
    assert "compressed" in names, (
        "the client must be told the session id rotated during the retry"
    )
    compressed = next(payload for event, payload in emitted if event == "compressed")
    assert compressed.get("new_session_id") == "continuation"
    done = next(payload for event, payload in emitted if event == "done")
    assert done["session"]["session_id"] == "continuation", (
        "the done event must report the continuation session"
    )


class _RotatingHealAgent:
    """First attempt rotates the session id then raises a 401; retry answers.

    The credential self-heal builds a FRESH agent from the turn's kwargs, so a
    per-instance counter cannot tell the attempts apart: the counter is
    class-level, as in ``_RaisingRetryAgent``.
    """

    instances = []
    calls = 0
    answer = "Fresh retry answer."

    def __init__(self, **kwargs):
        self.session_id = kwargs.get("session_id")
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.status_callback = kwargs.get("status_callback")
        self.context_compressor = None
        self.session_prompt_tokens = self.session_completion_tokens = 0
        self.session_cache_read_tokens = self.session_cache_write_tokens = 0
        self.session_estimated_cost_usd = None
        self.reasoning_config = self.ephemeral_system_prompt = self._last_error = None
        self.pending = []
        self.pending_lock = threading.Lock()
        _RotatingHealAgent.instances.append(self)

    def run_conversation(self, **kwargs):
        _RotatingHealAgent.calls += 1
        if _RotatingHealAgent.calls == 1:
            # The first attempt already rotated through compression, then the
            # provider returned a 401 as a result (not as a raised exception).
            self.session_id = "continuation-one"
            return {"messages": list(kwargs.get("conversation_history") or []),
                    "error": "401 Unauthorized: credential expired"}
        history = list(kwargs.get("conversation_history") or [])
        return {
            "messages": history
            + [
                {"role": "user", "content": "Do the task."},
                {"role": "assistant", "content": _RotatingHealAgent.answer},
            ],
        }

    def interrupt(self, _message):
        return None

    def steer(self, _text):
        return True

    def _drain_pending_steer(self):
        return None


def test_the_401_self_heal_rebuilds_on_the_current_continuation_identity(
    worker_scene, monkeypatch,
):
    """The rebuilt agent must not be constructed on the turn's stale session id.

    The first attempt rotates ``original`` -> ``continuation-one`` and then
    returns a 401. The self-heal rebuilt the Agent from the turn's kwargs, which
    still named ``original``; the post-retry migration then read that stale
    constructor id as a SECOND rotation (``continuation-one -> original``),
    raised ``Failed to migrate compressed-session ownership`` and discarded the
    successful retry's answer (no answer saved, ``apperror`` terminal).
    """
    scene = worker_scene
    _RotatingHealAgent.instances = []
    _RotatingHealAgent.calls = 0
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: _RotatingHealAgent)

    # A resolved runtime is required for the credential heal to rebuild.
    monkeypatch.setattr(
        streaming, "_attempt_credential_self_heal",
        lambda *a, **kw: {"api_key": "fresh-key", "provider": "test-provider",
                          "base_url": "https://api.test/v1"},
    )

    scene.run()

    assert _RotatingHealAgent.calls == 2, "the 401 must have reached the retry"
    built = [agent.session_id for agent in _RotatingHealAgent.instances]
    assert "continuation-one" in built, (
        "the rebuilt agent must be constructed on the session's CURRENT "
        f"continuation identity, not the pre-turn one; built {built}"
    )
    assert scene.session.session_id == "continuation-one", (
        "the retry must not migrate the continuation back to the stale id"
    )
    saved = [str(row.get("content") or "") for row in scene.session.messages]
    assert saved.count(_RotatingHealAgent.answer) == 1, (
        f"the successful retry's answer must be saved exactly once, got {saved}"
    )
    emitted = [event for event, _payload in list(scene.events.queue)]
    assert "apperror" not in emitted, (
        "a successful retry must not end with a migration error card"
    )
