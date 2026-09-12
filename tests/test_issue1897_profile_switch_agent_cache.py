"""Regression coverage for #1897 — same-session profile switch identity bleed."""

from __future__ import annotations

import os
import queue
import sys
import types
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
STREAMING_PY = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")


def _compute_agent_cache_signature_source() -> str:
    """Return the source of the `_compute_agent_cache_signature()` helper.

    The signature blob used to be inlined in the streaming send path; it now
    lives in this helper so the initial send and both self-heal retry paths
    derive the signature from the same final runtime bundle.
    """
    start = STREAMING_PY.index("def _compute_agent_cache_signature(")
    end = STREAMING_PY.index("\ndef ", start)
    return STREAMING_PY[start:end]


def _signature_block() -> str:
    """Return the `_json.dumps([...])` field list the signature hashes."""
    helper = _compute_agent_cache_signature_source()
    sig_start = helper.index("_sig_blob = _json.dumps")
    sig_end = helper.index("], sort_keys=True)", sig_start)
    return helper[sig_start:sig_end]


def _production_signature_calls() -> list[tuple[int, str]]:
    """Return `(offset, source)` for every production signature call site.

    Paren-balanced so the whole multi-line keyword-argument list is captured,
    and every live call site is returned so a retry path cannot silently drop a
    field that the initial send still passes. Commented-out occurrences are
    skipped -- a disabled retry call must read as missing, not as present.
    """
    marker = "_agent_sig = _compute_agent_cache_signature("
    calls: list[tuple[int, str]] = []
    pos = STREAMING_PY.find(marker)
    while pos != -1:
        line_start = STREAMING_PY.rfind("\n", 0, pos) + 1
        if STREAMING_PY[line_start:pos].strip():
            # Commented-out (or otherwise non-statement) occurrence: it no
            # longer runs, so it must not count as a live call site.
            pos = STREAMING_PY.find(marker, pos + 1)
            continue
        depth = 0
        for idx in range(pos + len(marker) - 1, len(STREAMING_PY)):
            char = STREAMING_PY[idx]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    calls.append((pos, STREAMING_PY[pos:idx + 1]))
                    break
        else:
            raise AssertionError("unterminated _compute_agent_cache_signature( call")
        pos = STREAMING_PY.find(marker, pos + 1)
    return calls


# Lifecycle anchors for the streaming send path. Pinning one signature call to
# each region keeps the oracle honest: a retry call that silently disappears
# fails the region check instead of hiding behind the calls that remain.
INITIAL_SEND_MARKER = "# ── Agent cache: reuse across messages in the same session ──"
RETURNED_ERROR_SELF_HEAL_MARKER = (
    "logger.info('[webui] self-heal: retrying stream after credential refresh')"
)
RAISED_EXCEPTION_SELF_HEAL_MARKER = (
    "logger.info('[webui] self-heal (except path): "
    "retrying stream after credential refresh')"
)

# Each region ends at the cache write that stores the signature it just
# computed. Bounding the region at the write -- rather than at the next
# lifecycle anchor or at EOF -- is what proves the signature is recomputed
# *before* it is cached: a call moved below its own write lands outside the
# region and fails, instead of passing while the write consumes a stale
# `_agent_sig`.
INITIAL_SEND_CACHE_WRITE_MARKER = (
    "SESSION_AGENT_CACHE[session_id] = (agent, _agent_sig)"
)
RETURNED_ERROR_SELF_HEAL_CACHE_WRITE_MARKER = (
    "_SAC[session_id] = (agent, _agent_sig)"
)
RAISED_EXCEPTION_SELF_HEAL_CACHE_WRITE_MARKER = (
    "_SAC2[session_id] = (_heal_agent, _agent_sig)"
)


def _marker_offset(marker: str) -> int:
    """Return the single offset of a lifecycle marker in streaming.py."""
    offset = STREAMING_PY.index(marker)
    assert STREAMING_PY.find(marker, offset + 1) == -1, (
        "lifecycle marker is no longer unique in streaming.py:\n" + marker
    )
    return offset


def _signature_calls_by_region(
    calls: list[tuple[int, str]],
) -> dict[str, tuple[int, str]]:
    """Map each lifecycle region to the single signature call inside it.

    The regions are the initial send, the returned-error self-heal retry and
    the raised-exception self-heal retry; each runs from its lifecycle anchor
    to the cache write that consumes `_agent_sig`. Requiring exactly one call
    inside those bounds means neither a dropped retry call (masked by its
    surviving siblings) nor a call recomputed after its own cache write (which
    would leave the write storing a stale signature) can read as correct.
    """
    initial = _marker_offset(INITIAL_SEND_MARKER)
    initial_cache_write = _marker_offset(INITIAL_SEND_CACHE_WRITE_MARKER)
    returned_error = _marker_offset(RETURNED_ERROR_SELF_HEAL_MARKER)
    returned_error_cache_write = _marker_offset(
        RETURNED_ERROR_SELF_HEAL_CACHE_WRITE_MARKER
    )
    raised_exception = _marker_offset(RAISED_EXCEPTION_SELF_HEAL_MARKER)
    raised_exception_cache_write = _marker_offset(
        RAISED_EXCEPTION_SELF_HEAL_CACHE_WRITE_MARKER
    )
    assert (
        initial
        < initial_cache_write
        < returned_error
        < returned_error_cache_write
        < raised_exception
        < raised_exception_cache_write
    ), (
        "streaming.py lifecycle regions are out of order; these anchors no "
        "longer describe the send path"
    )

    bounds = {
        "initial send": (initial, initial_cache_write),
        "returned-error self-heal": (returned_error, returned_error_cache_write),
        "raised-exception self-heal": (
            raised_exception,
            raised_exception_cache_write,
        ),
    }
    by_region: dict[str, tuple[int, str]] = {}
    for region, (start, end) in bounds.items():
        found = [(offset, call) for offset, call in calls if start < offset < end]
        assert len(found) == 1, (
            "expected exactly one _compute_agent_cache_signature() call in the "
            f"{region} region of streaming.py -- between its lifecycle anchor "
            f"and the cache write that stores `_agent_sig` -- found "
            f"{len(found)}"
        )
        by_region[region] = found[0]
    return by_region


def test_same_session_profile_switch_rebuilds_agent_under_new_soul_home(tmp_path, monkeypatch):
    """Switching profiles in one WebUI session must not reuse old SOUL.md.

    The fake AIAgent mirrors the real failure mode: it reads SOUL.md from
    HERMES_HOME at construction time and keeps that value in a cached system
    prompt. Two consecutive turns on the same profile should reuse the agent;
    changing only ``session.profile`` should create a fresh agent whose cached
    prompt comes from the new synthetic profile home.
    """
    sys.path.insert(0, str(REPO))
    from api import config as cfg
    from api import oauth
    from api import profiles
    from api import streaming

    default_home = tmp_path / "hermes-home"
    profile_a_home = default_home / "profiles" / "alpha"
    profile_b_home = default_home / "profiles" / "beta"
    profile_a_home.mkdir(parents=True)
    profile_b_home.mkdir(parents=True)
    (profile_a_home / "SOUL.md").write_text(
        "PROFILE_ALPHA_SYNTHETIC_SOUL",
        encoding="utf-8",
    )
    (profile_b_home / "SOUL.md").write_text(
        "PROFILE_BETA_SYNTHETIC_SOUL",
        encoding="utf-8",
    )

    class FakeSession:
        def __init__(self):
            self.session_id = "issue1897-same-session"
            self.title = "Pinned test title"
            self.workspace = str(tmp_path)
            self.model = "test-model"
            self.model_provider = None
            self.profile = "alpha"
            self.personality = None
            self.messages = []
            self.context_messages = []
            self.tool_calls = []
            self.input_tokens = 0
            self.output_tokens = 0
            self.estimated_cost = None
            self.context_length = 0
            self.threshold_tokens = 0
            self.last_prompt_tokens = 0
            self.active_stream_id = None
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None
            self.llm_title_generated = True

        def save(self, *args, **kwargs):
            return None

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
                "personality": self.personality,
            }

    constructed_agents = []
    prompts_used_for_runs = []
    homes_seen_during_runs = []

    class SoulCachingAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id")
            self.model = kwargs.get("model")
            self.provider = kwargs.get("provider")
            self.base_url = kwargs.get("base_url")
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.ephemeral_system_prompt = None
            self._last_error = None
            self.stream_delta_callback = kwargs.get("stream_delta_callback")
            self.tool_progress_callback = kwargs.get("tool_progress_callback")
            self.reasoning_callback = kwargs.get("reasoning_callback")
            self.clarify_callback = kwargs.get("clarify_callback")
            home = Path(os.environ["HERMES_HOME"])
            self.constructed_home = str(home)
            self._cached_system_prompt = (home / "SOUL.md").read_text(encoding="utf-8")
            constructed_agents.append(self)

        def run_conversation(self, **kwargs):
            prompts_used_for_runs.append(self._cached_system_prompt)
            homes_seen_during_runs.append(os.environ.get("HERMES_HOME"))
            history = list(kwargs.get("conversation_history") or [])
            return {
                "messages": history
                + [
                    {"role": "user", "content": kwargs.get("persist_user_message", "")},
                    {
                        "role": "assistant",
                        "content": f"reply from {self._cached_system_prompt}",
                    },
                ]
            }

        def interrupt(self, _message):
            return None

    fake_session = FakeSession()
    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_module.resolve_runtime_provider = lambda requested=None: {
        "provider": requested or "test-provider",
        "api_key": "synthetic-key",
        "base_url": None,
    }
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.runtime_provider = fake_runtime_module
    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = lambda: None

    def home_for_profile(profile_name):
        return {"alpha": profile_a_home, "beta": profile_b_home}[profile_name]

    monkeypatch.setattr(streaming, "get_session", lambda _sid: fake_session)
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: SoulCachingAgent)
    monkeypatch.setattr(
        streaming,
        "resolve_model_provider",
        lambda _model, **_kw: ("test-model", "test-provider", None),
    )
    monkeypatch.setattr(streaming, "_maybe_schedule_title_refresh", lambda *args, **kwargs: None)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", home_for_profile)
    monkeypatch.setattr(profiles, "get_profile_runtime_env", lambda _home: {})
    monkeypatch.setattr(
        oauth,
        "resolve_runtime_provider_with_anthropic_env_lock",
        lambda _resolver, requested=None: {
            "provider": requested or "test-provider",
            "api_key": "synthetic-key",
            "base_url": None,
        },
    )
    monkeypatch.setattr("api.config.get_config", lambda: {})
    monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda _cfg: [])
    monkeypatch.setattr("api.config.load_settings", lambda: {})
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime_module)
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)

    with cfg.SESSION_AGENT_CACHE_LOCK:
        cfg.SESSION_AGENT_CACHE.clear()
    streaming.STREAMS.clear()
    streaming.CANCEL_FLAGS.clear()
    streaming.AGENT_INSTANCES.clear()
    streaming.STREAM_PARTIAL_TEXT.clear()
    streaming.STREAM_REASONING_TEXT.clear()
    streaming.STREAM_LIVE_TOOL_CALLS.clear()

    def run_turn(profile_name: str, stream_id: str, text: str):
        fake_session.profile = profile_name
        fake_session.active_stream_id = stream_id
        streaming.STREAMS[stream_id] = queue.Queue()
        streaming._run_agent_streaming(
            session_id=fake_session.session_id,
            msg_text=text,
            model="test-model",
            model_provider="test-provider",
            workspace=str(tmp_path),
            stream_id=stream_id,
        )

    run_turn("alpha", "issue1897-stream-1", "first turn")
    run_turn("alpha", "issue1897-stream-2", "same profile second turn")
    assert len(constructed_agents) == 1, "same-profile turns should reuse the cached agent"

    run_turn("beta", "issue1897-stream-3", "profile switched turn")

    assert prompts_used_for_runs == [
        "PROFILE_ALPHA_SYNTHETIC_SOUL",
        "PROFILE_ALPHA_SYNTHETIC_SOUL",
        "PROFILE_BETA_SYNTHETIC_SOUL",
    ]
    assert [agent.constructed_home for agent in constructed_agents] == [
        str(profile_a_home),
        str(profile_b_home),
    ]
    assert homes_seen_during_runs == [
        str(profile_a_home),
        str(profile_a_home),
        str(profile_b_home),
    ]
    with cfg.SESSION_AGENT_CACHE_LOCK:
        assert cfg.SESSION_AGENT_CACHE[fake_session.session_id][0] is constructed_agents[-1]


def test_cache_signature_includes_profile_home():
    block = _signature_block()
    assert "profile_home" in block, (
        "SESSION_AGENT_CACHE signature is missing `profile_home`. Without this, "
        "same-session profile switches reuse the cached agent built under the "
        "previous profile's HERMES_HOME, leaking the old SOUL.md into new turns."
    )

    calls = _production_signature_calls()
    assert len(calls) == 3, (
        "expected exactly three _compute_agent_cache_signature() call sites "
        "(initial send plus both self-heal retries), found "
        f"{len(calls)}"
    )
    for region, (_offset, call) in _signature_calls_by_region(calls).items():
        assert "profile_home=_profile_home" in call, (
            f"the {region} signature call site must pass the resolved profile home, "
            "or a retry re-caches the agent under a signature that ignores the active profile:\n" + call
        )


def test_profile_home_resolved_before_cache_signature():
    profile_home_assignment = STREAMING_PY.index("_profile_home = str(_profile_home_path)")

    calls = _production_signature_calls()
    assert len(calls) == 3, (
        "expected exactly three _compute_agent_cache_signature() call sites "
        "(initial send plus both self-heal retries), found "
        f"{len(calls)}"
    )
    for region, (offset, call) in _signature_calls_by_region(calls).items():
        assert profile_home_assignment < offset, (
            "`_profile_home` must be resolved before the cache signature is "
            f"computed, otherwise the {region} signature hashes a stale/unbound "
            "home."
        )
        assert "profile_home=_profile_home" in call, call
        assert "max_iterations_cfg=_max_iterations_cfg" in call, call
        assert "max_tokens_cfg=_max_tokens_cfg" in call, call


def test_signature_uses_profile_home_with_fallback():
    block = _signature_block()
    assert "profile_home or ''" in block, (
        "Signature should use `profile_home or ''` so empty-home deployments get "
        "a stable cache key rather than unnecessary cache churn."
    )

    for _offset, call in _production_signature_calls():
        assert "profile_home=_profile_home" in call, call
