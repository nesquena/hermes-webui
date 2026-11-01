"""A recovery that compresses a second time must still save its answer.

The 401 self-heal rebuilds the Agent on the session's current continuation. If
that rebuilt Agent compresses again, the turn has rotated twice in one request
(`original -> continuation-one -> continuation-two`). The second publication has
no durable `continuation-one.json` to snapshot: the turn only saves once, under
its final id. The migration therefore has to persist the intermediate
continuation from memory instead of failing its pre-image read and raising with
the retry's real answer discarded.

Both retry settings are covered: the rotation happens on the auth path, which
runs whether or not the silent-failure retry is enabled.
"""

import threading

import pytest

from api import models
from api import streaming
from tests.test_steer_worker_boundaries import worker_scene as worker_scene


class _TwoHopHealAgent:
    """First attempt rotates and returns 401; the rebuilt Agent rotates again."""

    instances = []
    # Constructor inputs, captured when construction happens. The live
    # `session_id` is mutated by compression later, so it is not provenance.
    constructed_session_ids = []
    calls = 0
    answer = "Fresh retry answer."

    def __init__(self, session_id=None, **kwargs):
        self.session_id = session_id
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.status_callback = kwargs.get("status_callback")
        self.context_compressor = None
        self.session_prompt_tokens = self.session_completion_tokens = 0
        self.session_cache_read_tokens = self.session_cache_write_tokens = 0
        self.session_estimated_cost_usd = None
        self.reasoning_config = self.ephemeral_system_prompt = self._last_error = None
        self.pending = []
        self.pending_lock = threading.Lock()
        _TwoHopHealAgent.instances.append(self)
        _TwoHopHealAgent.constructed_session_ids.append(session_id)

    def run_conversation(self, **kwargs):
        _TwoHopHealAgent.calls += 1
        history = list(kwargs.get("conversation_history") or [])
        if _TwoHopHealAgent.calls == 1:
            # Compression rotated the session, then the provider returned 401.
            self.session_id = "continuation-one"
            return {"messages": history, "error": "401 Unauthorized: credential expired"}
        # The rebuilt Agent compresses again and answers.
        self.session_id = "continuation-two"
        return {
            "messages": history
            + [
                {"role": "user", "content": "Do the task."},
                {"role": "assistant", "content": _TwoHopHealAgent.answer},
            ],
        }

    def interrupt(self, _message):
        return None

    def steer(self, _text):
        return True

    def _drain_pending_steer(self):
        return None


@pytest.mark.parametrize("retry_enabled", ["0", "1"])
def test_two_hop_compression_keeps_the_recovered_answer(
    worker_scene, monkeypatch, retry_enabled,
):
    monkeypatch.setenv("HERMES_WEBUI_SILENT_FAILURE_RETRY", retry_enabled)
    scene = worker_scene
    _TwoHopHealAgent.instances = []
    _TwoHopHealAgent.constructed_session_ids = []
    _TwoHopHealAgent.calls = 0
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: _TwoHopHealAgent)
    monkeypatch.setattr(
        streaming, "_attempt_credential_self_heal",
        lambda *a, **kw: {
            "api_key": "fresh-key",
            "provider": "test-provider",
            "base_url": "https://api.test/v1",
        },
    )

    scene.run()

    assert _TwoHopHealAgent.calls == 2, "the 401 must have reached the retry"
    built = [str(sid) for sid in _TwoHopHealAgent.constructed_session_ids]
    assert "continuation-one" in built, (
        f"the rebuilt agent must be constructed on the current continuation, built {built}"
    )
    assert scene.session.session_id == "continuation-two", (
        "the second rotation must become the session the turn is saved under"
    )
    saved = [str(row.get("content") or "") for row in scene.session.messages]
    assert saved.count(_TwoHopHealAgent.answer) == 1, (
        f"the recovered answer must be saved exactly once, got {saved}"
    )

    reloaded = models.Session.load("continuation-two")
    assert reloaded is not None, "no continuation-two sidecar was written"
    cold = [str(row.get("content") or "") for row in reloaded.messages]
    assert cold.count(_TwoHopHealAgent.answer) == 1, (
        f"cold load of the final continuation lost the answer, got {cold}"
    )

    emitted = list(scene.events.queue)
    names = [event for event, _payload in emitted]
    assert "apperror" not in names, (
        "a recovered turn must not end on a migration error card"
    )
    compressed = next(payload for event, payload in emitted if event == "compressed")
    assert compressed.get("new_session_id") == "continuation-two"
    assert compressed.get("old_session_id") == "original", (
        "the client's stream still originates at the id it opened the stream on"
    )
    done = next(payload for event, payload in emitted if event == "done")
    assert done["session"]["session_id"] == "continuation-two"
