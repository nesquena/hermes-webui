"""Behavioral regression test: settled turns must persist ``_turnUsage``.

The token/cache footer reads ``msg._turnUsage`` in ``static/ui.js``
(``hasTurnUsage = !!msg._turnUsage``). The live tab gets that value from the
SSE ``usage`` payload in the ``done`` handler (``static/messages.js``), but the
value is derived client-side and never persisted — so a reload or a second tab
loses the badge for every historical turn.

``_turnUsage`` is already listed in ``_SESSION_MESSAGE_DISPLAY_METADATA_KEYS``
(``api/models.py``): the reload path was ready to carry it, the producer never
wrote it. These tests drive the real ``_run_agent_streaming`` settle path with
a fake agent (harness borrowed from ``tests/test_issue1857_usage_overwrite.py``)
and assert on the *saved message values* — not on source text — so a regression
that drops the stamp, or writes session totals instead of per-turn deltas, is
caught.
"""

import queue
import sys
import types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

# Sentinel for sys.modules save/restore — distinguishes "key wasn't there" from None.
_MISSING = object()


class _FakeSession:
    """Minimal session double with pre-existing (turn-N-1) usage counters."""

    def __init__(self, session_id="turn_usage_persisted_on_settle"):
        self.session_id = session_id
        self.title = "Existing title"
        self.workspace = "/tmp"
        self.model = "gpt-5.4"
        self.model_provider = None
        self.profile = None
        self.personality = None
        self.messages = [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old answer"},
        ]
        self.context_messages = list(self.messages)
        # Turn N-1 already persisted these totals.
        self.input_tokens = 100
        self.output_tokens = 55
        self.estimated_cost = 0.02
        self.cache_read_tokens = 40
        self.cache_write_tokens = 20
        self.tool_calls = []
        self.gateway_routing = None
        self.gateway_routing_history = []
        self.active_stream_id = None
        self.pending_user_message = None
        self.pending_attachments = []
        self.pending_started_at = None
        self.pending_user_source = None
        self.context_length = 0
        self.threshold_tokens = 0
        self.last_prompt_tokens = 0
        self.llm_title_generated = True

    def save(self, *args, **kwargs):
        pass

    def compact(self):
        return {
            "session_id": self.session_id,
            "title": self.title,
            "workspace": self.workspace,
            "model": self.model,
            "created_at": 0,
            "updated_at": 0,
            "pinned": False,
            "archived": False,
            "project_id": None,
            "profile": self.profile,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost": self.estimated_cost,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "personality": self.personality,
        }


class _UsageAgent:
    """Agent double whose reported session totals advance by a known delta.

    ``totals`` is class-level so tests can retarget the agent's counters
    (e.g. an exactly-equal no-op turn) without re-running module injection.
    """

    totals = {
        "input": 133,   # delta vs the fake session's persisted 100
        "output": 80,   # delta 25
        "cost": 0.05,   # delta 0.03
        "cache_read": 90,   # delta 50
        "cache_write": 45,  # delta 25
    }

    def __init__(
        self,
        model=None,
        provider=None,
        base_url=None,
        api_key=None,
        platform=None,
        quiet_mode=False,
        enabled_toolsets=None,
        fallback_model=None,
        session_id=None,
        session_db=None,
        stream_delta_callback=None,
        reasoning_callback=None,
        tool_progress_callback=None,
        clarify_callback=None,
    ):
        self.session_id = session_id
        self.context_compressor = None
        self.session_prompt_tokens = self.totals["input"]
        self.session_completion_tokens = self.totals["output"]
        self.session_estimated_cost_usd = self.totals["cost"]
        self.session_cache_read_tokens = self.totals["cache_read"]
        self.session_cache_write_tokens = self.totals["cache_write"]
        self.reasoning_config = None
        self.ephemeral_system_prompt = None
        self._last_error = None

    def run_conversation(self, **kwargs):
        history = kwargs.get("conversation_history", [])
        return {
            "messages": history + [
                {"role": "user", "content": kwargs["persist_user_message"]},
                {"role": "assistant", "content": "new answer"},
            ]
        }

    def interrupt(self, _message):
        pass


def _run_settle_turn(session):
    """Drive one full _run_agent_streaming settle; return (events, session).

    NOTE: we deliberately avoid mock.patch.dict(sys.modules, ...) here — the
    same reason as tests/test_issue1857_usage_overwrite.py: patch.dict's exit
    evicts lazily-imported submodules other tests rely on. Manual save/restore
    only touches the keys we explicitly inject.

    Each caller must pass a session with a UNIQUE ``session_id``: the settle
    path reuses a cached agent per session id (``SESSION_AGENT_CACHE``), and a
    reused agent would carry a previous test's counters into the next one.
    """
    import api.streaming as streaming

    fake_stream_id = f"stream_{session.session_id}"
    session.active_stream_id = fake_stream_id
    fake_queue = queue.Queue()

    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_module.resolve_runtime_provider = mock.Mock(
        return_value={
            "provider": "openai",
            "base_url": None,
            "api_key": "sk-test",
            "api_mode": "chat_completions",
            "command": None,
            "args": [],
            "credential_pool": None,
        }
    )
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.runtime_provider = fake_runtime_module
    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = mock.Mock(return_value=None)

    _injected = {
        "hermes_cli": fake_hermes_cli,
        "hermes_cli.runtime_provider": fake_runtime_module,
        "hermes_state": fake_hermes_state,
    }
    _saved = {k: sys.modules.get(k, _MISSING) for k in _injected}
    sys.modules.update(_injected)
    try:
        with mock.patch.object(streaming, "get_session", return_value=session), \
             mock.patch.object(streaming, "_get_ai_agent", return_value=_UsageAgent), \
             mock.patch.object(streaming, "resolve_model_provider", return_value=("gpt-5.4", "openai", None)), \
             mock.patch("api.config.get_config", return_value={}), \
             mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
            streaming.STREAMS[fake_stream_id] = fake_queue
            streaming._run_agent_streaming(
                session_id=session.session_id,
                msg_text="new turn",
                model="gpt-5.4",
                workspace="/tmp",
                stream_id=fake_stream_id,
            )
    finally:
        for k, prev in _saved.items():
            if prev is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = prev

    return list(fake_queue.queue), session


def _last_assistant_message(session):
    for msg in reversed(session.messages):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            return msg
    return None


def test_settle_block_persists_turn_usage_delta_on_assistant_message():
    """The saved assistant message must carry _turnUsage with per-turn deltas."""
    session = _FakeSession(session_id="turn_usage_delta_write")
    _run_settle_turn(session)

    msg = _last_assistant_message(session)
    assert msg is not None, "no assistant message after settle"
    usage = msg.get("_turnUsage")
    assert usage is not None, (
        "the settle block must stamp _turnUsage on the final assistant message "
        "so the usage badge survives a session reload"
    )
    # Deltas: agent totals (133/80/0.05/90/45) minus persisted (100/55/0.02/40/20).
    assert usage["input_tokens"] == 33
    assert usage["output_tokens"] == 25
    assert usage["cache_read_tokens"] == 50
    assert usage["cache_write_tokens"] == 25
    assert abs(usage["estimated_cost"] - 0.03) < 1e-9
    assert isinstance(usage["cache_hit_percent"], (int, float))


def test_turn_usage_uses_per_turn_deltas_not_session_totals():
    """A write of cumulative session totals must not pass as _turnUsage."""
    session = _FakeSession(session_id="turn_usage_not_session_totals")
    _run_settle_turn(session)

    usage = _last_assistant_message(session).get("_turnUsage") or {}
    # If the write used the agent's cumulative totals instead of the delta,
    # these would read 133 / 80 / 90 / 45 — the assertion only passes when the
    # per-turn delta semantics are preserved.
    assert usage.get("input_tokens") != 133
    assert usage.get("output_tokens") != 80
    assert usage.get("cache_read_tokens") != 90
    assert usage.get("cache_write_tokens") != 45


def test_turn_usage_only_stamped_when_turn_consumed_tokens():
    """A zero-delta turn must not stamp an all-zero snapshot over a real one."""
    session = _FakeSession(session_id="turn_usage_zero_delta_guard")
    prev_totals = dict(_UsageAgent.totals)
    try:
        # Agent reports exactly what the session already persisted → all deltas 0.
        _UsageAgent.totals = {
            "input": 100,
            "output": 55,
            "cost": 0.02,
            "cache_read": 40,
            "cache_write": 20,
        }
        _run_settle_turn(session)
    finally:
        _UsageAgent.totals = prev_totals

    msg = _last_assistant_message(session)
    assert msg is not None
    assert "_turnUsage" not in msg, (
        "a zero-delta turn must not stamp _turnUsage (same guard as the #503 "
        "client path: only write when the turn actually consumed tokens)"
    )


def test_turn_usage_stays_in_the_persisted_display_metadata_allowlist():
    """The key must keep surviving the sidecar/state.db merge on reload."""
    src = (ROOT / "api" / "models.py").read_text(encoding="utf-8")

    start = src.index("_SESSION_MESSAGE_DISPLAY_METADATA_KEYS = (")
    block = src[start:src.index(")", start)]
    assert '"_turnUsage"' in block