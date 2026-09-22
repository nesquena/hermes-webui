"""The LOCAL fallback footer notice must be gated on real Agent fallback
provenance, with the provider identity carried end-to-end.

Gate blockers (nesquena-hermes, 2026-09-19):

1. ``_local_model_switch()`` compared raw/bare model strings, so a normal
   model-ID normalization manufactured a fallback warning: requesting
   ``claude-sonnet-4.6`` and being served the Agent-normalized
   ``claude-sonnet-4-6`` returned "switched" on an ordinary turn.
2. Provider identity was not carried through the production local-fallback
   path: no Python/message projection stamped ``_requestedProvider`` /
   ``_usedProvider``, so a real fallback between two providers serving the
   same bare model was suppressed.

The fixed contract, exercised here through the production ``_run_agent_streaming``
composition (FakeAgent standing in for AIAgent, exactly like
``test_issue1857_usage_overwrite.py``):

* the notice verdict requires the Agent's ``_provider_fallback_active``
  provenance AND a different constructor-normalized ``(provider, model)``
  identity — never a spelling variant alone;
* ``_usedProvider`` is always stamped; ``_requestedModel``/``_requestedProvider``
  are stamped only on a proven fallback switch (persistence + SSE usage);
* provider ids are canonicalized through the Agent's own alias tables
  (``ollama``→``custom``, ``openai``→``openrouter``,
  ``kimi-coding``→``kimi-for-coding``) on BOTH sides before comparing, so the
  same provider under two spellings can never read as a cross-provider switch.
"""

import queue
import sys
import types
from unittest import mock

# Sentinel for sys.modules save/restore — distinguishes "key wasn't there" from None.
_MISSING = object()


def _make_agent(*, requested_model, requested_provider, served_model, served_provider,
                fallback_active, primary_runtime="requested"):
    """FakeAgent: constructed on the requested identity; run_conversation swaps
    to the served identity and sets the Agent's real fallback-active flag."""

    class FakeAgent:
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
            self.session_prompt_tokens = 10
            self.session_completion_tokens = 5
            self.session_estimated_cost_usd = 0.01
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = None
            # Constructor state == the requested identity, like AIAgent.
            self.model = requested_model
            self.provider = requested_provider
            self._provider_fallback_active = False
            # AIAgent snapshots the primary runtime AFTER the constructor
            # normalized model/provider; restore_primary_runtime returns to it.
            if primary_runtime == "requested":
                self._primary_runtime = {
                    "model": requested_model,
                    "provider": requested_provider,
                }
            else:
                self._primary_runtime = primary_runtime

        def run_conversation(self, **kwargs):
            # try_activate_fallback swaps client/model/provider in place and
            # sets the fallback-active provenance flag.
            self.model = served_model
            self.provider = served_provider
            self._provider_fallback_active = fallback_active
            history = kwargs.get("conversation_history", [])
            return {
                "messages": history + [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "served answer"},
                ]
            }

        def interrupt(self, _message):
            pass

    return FakeAgent


def _run_turn(agent_cls, *, resolved_model, resolved_provider, extra_modules=None):
    """Drive the production streaming worker with a FakeAgent and return
    (session, sse_events). Mirrors test_issue1857_usage_overwrite.py."""
    import api.streaming as streaming

    class FakeSession:
        def __init__(self):
            self.session_id = "local_fallback_provenance"
            self.title = "Existing title"
            self.workspace = "/tmp"
            self.model = resolved_model
            self.model_provider = resolved_provider
            self.profile = None
            self.personality = None
            self.messages = [
                {"role": "user", "content": "old"},
                {"role": "assistant", "content": "old answer"},
            ]
            self.context_messages = list(self.messages)
            self.input_tokens = 0
            self.output_tokens = 0
            self.estimated_cost = 0.0
            self.cache_read_tokens = 0
            self.cache_write_tokens = 0
            self.tool_calls = []
            self.gateway_routing = None
            self.gateway_routing_history = []
            self.active_stream_id = None
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None
            self.context_length = 0
            self.threshold_tokens = 0
            self.last_prompt_tokens = 0
            self.llm_title_generated = True

        def save(self, *args, **kwargs):
            pass

        def compact(self):
            return {"session_id": self.session_id}

    fake_session = FakeSession()
    fake_stream_id = "stream_local_fallback_provenance"
    fake_session.active_stream_id = fake_stream_id
    fake_queue = queue.Queue()

    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_module.resolve_runtime_provider = mock.Mock(
        return_value={
            "provider": resolved_provider,
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

    # Manual save/restore (not mock.patch.dict): see test_issue1857 for why
    # patch.dict's key eviction breaks lazily-imported downstream modules.
    injected = {
        "hermes_cli": fake_hermes_cli,
        "hermes_cli.runtime_provider": fake_runtime_module,
        "hermes_state": fake_hermes_state,
    }
    for name, module in (extra_modules or {}).items():
        injected[name] = module
        if name.startswith("hermes_cli."):
            setattr(fake_hermes_cli, name.split(".", 1)[1], module)
    saved = {k: sys.modules.get(k, _MISSING) for k in injected}
    sys.modules.update(injected)
    try:
        with mock.patch.object(streaming, "get_session", return_value=fake_session), \
             mock.patch.object(streaming, "_get_ai_agent", return_value=agent_cls), \
             mock.patch.object(
                 streaming, "resolve_model_provider",
                 return_value=(resolved_model, resolved_provider, None)), \
             mock.patch("api.config.get_config", return_value={}), \
             mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
            streaming.STREAMS[fake_stream_id] = fake_queue
            streaming._run_agent_streaming(
                session_id=fake_session.session_id,
                msg_text="new turn",
                model=resolved_model,
                workspace="/tmp",
                stream_id=fake_stream_id,
            )
    finally:
        for k, prev in saved.items():
            if prev is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = prev
        streaming.STREAMS.pop(fake_stream_id, None)
    return fake_session, list(fake_queue.queue)


def _done_usage(events):
    for event, payload in events:
        if event == "done" and isinstance(payload, dict) and payload.get("usage"):
            return payload["usage"]
    raise AssertionError(f"no done/usage event in {events!r}")


def test_proven_cross_provider_fallback_stamps_full_requested_and_used_identity():
    """A REAL local fallback (Agent provenance + different normalized identity)
    must stamp both provider halves, in persistence and in the SSE usage payload."""
    from api.streaming import _normalized_runtime_provider_id as fold

    agent_cls = _make_agent(
        requested_model="qwen3.8-max", requested_provider="alibaba",
        served_model="deepseek-v4-flash-0731", served_provider="ollama",
        fallback_active=True,
    )
    session, events = _run_turn(
        agent_cls, resolved_model="qwen3.8-max", resolved_provider="alibaba"
    )
    last = session.messages[-1]
    assert last["role"] == "assistant"
    assert last["_usedModel"] == "deepseek-v4-flash-0731"
    # Stamped provider ids are the canonical fold (ollama → custom when the
    # Agent alias table is importable; the WebUI id otherwise) — the point is
    # that the served half differs from the requested half under ONE namespace.
    assert last["_usedProvider"] == fold("ollama")
    assert last["_usedProvider"] != last["_requestedProvider"]
    assert last["_requestedModel"] == "qwen3.8-max"
    assert last["_requestedProvider"] == "alibaba"
    usage = _done_usage(events)
    assert usage["used_model"] == "deepseek-v4-flash-0731"
    assert usage["used_provider"] == fold("ollama")
    assert usage["requested_model"] == "qwen3.8-max"
    assert usage["requested_provider"] == "alibaba"


def test_normal_model_id_normalization_without_fallback_provenance_stays_silent():
    """Gate blocker #1, production-composed: ``claude-sonnet-4.6`` requested,
    the Agent-normalized ``claude-sonnet-4-6`` served, NO fallback provenance —
    the served model is stamped (#6068) but no switch verdict is emitted."""
    agent_cls = _make_agent(
        requested_model="claude-sonnet-4.6", requested_provider="anthropic",
        served_model="claude-sonnet-4-6", served_provider="anthropic",
        fallback_active=False,
    )
    session, events = _run_turn(
        agent_cls, resolved_model="claude-sonnet-4.6", resolved_provider="anthropic"
    )
    last = session.messages[-1]
    assert last["_usedModel"] == "claude-sonnet-4-6"
    assert last["_usedProvider"] == "anthropic"
    assert "_requestedModel" not in last
    assert "_requestedProvider" not in last
    usage = _done_usage(events)
    assert usage["used_model"] == "claude-sonnet-4-6"
    assert usage["used_provider"] == "anthropic"
    assert "requested_model" not in usage
    assert "requested_provider" not in usage


def test_same_bare_model_served_by_a_different_provider_is_a_switch():
    """Gate blocker #2, production-composed: the fallback serves the SAME bare
    model from a DIFFERENT provider — the old model-only comparison suppressed
    exactly this real switch."""
    agent_cls = _make_agent(
        requested_model="claude-sonnet-4-6", requested_provider="openrouter",
        served_model="claude-sonnet-4-6", served_provider="anthropic",
        fallback_active=True,
    )
    session, events = _run_turn(
        agent_cls, resolved_model="claude-sonnet-4-6", resolved_provider="openrouter"
    )
    last = session.messages[-1]
    assert last["_usedModel"] == "claude-sonnet-4-6"
    assert last["_usedProvider"] == "anthropic"
    assert last["_requestedModel"] == "claude-sonnet-4-6"
    assert last["_requestedProvider"] == "openrouter"
    usage = _done_usage(events)
    assert usage["used_provider"] == "anthropic"
    assert usage["requested_provider"] == "openrouter"
    assert usage["requested_model"] == "claude-sonnet-4-6"


def test_same_provider_under_two_agent_alias_spellings_is_not_a_switch():
    """The fallback entry spells the primary's provider through another alias
    (``kimi-coding`` vs the Agent-canonical ``kimi-for-coding``): the identity
    is unchanged after canonicalization, so even WITH fallback provenance no
    switch is reported. Uses the real ``hermes_cli.providers`` alias group."""
    fake_providers = types.ModuleType("hermes_cli.providers")
    fake_providers.ALIASES = {
        # Mirror of the Agent's hermes_cli.providers._ALIAS_GROUPS entry:
        # "kimi-for-coding": ("kimi", "kimi-coding", "kimi-coding-cn", "moonshot")
        "kimi": "kimi-for-coding",
        "kimi-coding": "kimi-for-coding",
        "kimi-coding-cn": "kimi-for-coding",
        "moonshot": "kimi-for-coding",
    }
    fake_providers.normalize_provider = lambda name: fake_providers.ALIASES.get(
        name.strip().lower(), name.strip().lower()
    )
    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.normalize_provider = lambda provider: (
        (provider or "openrouter").strip().lower()
    )
    agent_cls = _make_agent(
        requested_model="k3-256k", requested_provider="kimi-coding",
        served_model="k3-256k", served_provider="kimi-for-coding",
        fallback_active=True,
    )
    session, events = _run_turn(
        agent_cls,
        resolved_model="k3-256k",
        resolved_provider="kimi-coding",
        extra_modules={
            "hermes_cli.providers": fake_providers,
            "hermes_cli.models": fake_models,
        },
    )
    last = session.messages[-1]
    assert last["_usedModel"] == "k3-256k"
    # The stamped provider half is the canonical fold, identical on both sides.
    assert last["_usedProvider"] == "kimi-for-coding"
    assert "_requestedModel" not in last
    assert "_requestedProvider" not in last
    usage = _done_usage(events)
    assert usage["used_provider"] == "kimi-for-coding"
    assert "requested_model" not in usage
    assert "requested_provider" not in usage


def test_fallback_provenance_flag_alone_without_identity_change_stays_silent():
    """Provenance is necessary but not sufficient: a fallback that landed on
    the SAME normalized identity is not a visible switch."""
    agent_cls = _make_agent(
        requested_model="llama3:8b", requested_provider="custom",
        served_model="llama3:8b", served_provider="custom",
        fallback_active=True,
    )
    session, events = _run_turn(
        agent_cls, resolved_model="llama3:8b", resolved_provider="custom"
    )
    last = session.messages[-1]
    assert last["_usedModel"] == "llama3:8b"
    assert "_requestedModel" not in last
    usage = _done_usage(events)
    assert "requested_model" not in usage


def test_provider_alias_fold_matches_the_agent_tables():
    """Helper-level: both sides of the comparison are folded through the
    Agent's own alias tables (when importable) plus the WebUI table, so
    spelling variants of one provider converge to one canonical id."""
    import api.streaming as streaming

    fold = streaming._normalized_runtime_provider_id
    assert fold("") == ""
    assert fold(None) == ""
    assert fold("@Anthropic") == "anthropic"
    assert fold("custom:kimi-coding") == "custom:kimi-coding"
    try:
        import hermes_cli.providers  # noqa: F401
        import hermes_cli.models  # noqa: F401
    except Exception:
        # Agent tree not importable (CI): only the WebUI table applies, on
        # both sides, and the comparison stays fail-closed on unknowns.
        return
    assert fold("ollama") == fold("custom")
    assert fold("openai") == fold("openrouter")
    assert fold("kimi-coding") == fold("kimi-for-coding") == fold("moonshot")
    assert fold("x-ai") == fold("xai")
    assert fold("claude") == fold("anthropic")
    # Idempotent fixed point.
    assert fold(fold("kimi-coding")) == fold("kimi-coding")


def test_fallback_switch_requires_agent_provenance_and_identity_change():
    """Helper-level contract of the notice gate: provenance AND a different
    normalized (provider, model) identity — never a spelling variant alone."""
    from api.streaming import _agent_fallback_provenance, _local_fallback_switch

    # Stand in for the Agent's constructor-time model normalizer (dots →
    # hyphens for Anthropic) when the real hermes_cli tree is not importable
    # in this test environment — the same way _run_turn stubs runtime_provider.
    try:
        from hermes_cli.model_normalize import normalize_model_for_provider  # noqa: F401
        extra = {}
    except Exception:
        fake_normalize = types.ModuleType("hermes_cli.model_normalize")

        def _normalize_model_for_provider(model_input, target_provider):
            name = (model_input or "").strip()
            if str(target_provider or "").strip().lower() == "anthropic" and "/" not in name:
                return name.replace(".", "-")
            return name

        fake_normalize.normalize_model_for_provider = _normalize_model_for_provider
        extra = {"hermes_cli.model_normalize": fake_normalize}
        if "hermes_cli" not in sys.modules:
            fake_cli = types.ModuleType("hermes_cli")
            fake_cli.model_normalize = fake_normalize
            extra["hermes_cli"] = fake_cli

    saved = {k: sys.modules.get(k, _MISSING) for k in extra}
    sys.modules.update(extra)
    try:
        class Agent:
            pass

        agent = Agent()
        agent._provider_fallback_active = False
        agent._primary_runtime = {"model": "claude-sonnet-4-6", "provider": "anthropic"}
        # No provenance: even genuinely different ids must not raise the notice.
        assert _agent_fallback_provenance(agent) is False
        assert _local_fallback_switch(
            agent, "claude-sonnet-4.6", "anthropic", "deepseek-flash", "deepseek"
        ) is False
        # Provenance + spelling variant of the served identity: silent.
        agent._provider_fallback_active = True
        assert _agent_fallback_provenance(agent) is True
        assert _local_fallback_switch(
            agent, "claude-sonnet-4.6", "anthropic", "claude-sonnet-4-6", "anthropic"
        ) is False
        # Provenance + same bare model from another provider: a real switch.
        assert _local_fallback_switch(
            agent, "claude-sonnet-4-6", "openrouter", "claude-sonnet-4-6", "anthropic"
        ) is True
        # Provenance + genuinely different model: a real switch.
        assert _local_fallback_switch(
            agent, "qwen3.8-max", "alibaba", "deepseek-v4-flash-0731", "ollama"
        ) is True
        # _fallback_activated alone (init-time fallback / user model switch
        # bookkeeping) is NOT runtime fallback provenance.
        other = Agent()
        other._provider_fallback_active = False
        other._fallback_activated = True
        assert _agent_fallback_provenance(other) is False
    finally:
        for k, prev in saved.items():
            if prev is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = prev


def test_live_stream_recopies_provider_identity_onto_the_assistant_message():
    """The SSE counterpart: attachLiveStream must copy used_provider and
    requested_provider onto the live assistant message, or the footer notice
    would only appear after a reload."""
    from pathlib import Path

    messages_js = (Path(__file__).resolve().parents[1] / "static" / "messages.js").read_text(
        encoding="utf-8"
    )
    assert "lastAsst._usedProvider=d.usage.used_provider" in messages_js
    assert "lastAsst._requestedModel=d.usage.requested_model" in messages_js
    assert "lastAsst._requestedProvider=d.usage.requested_provider" in messages_js


def test_provider_identity_keys_survive_the_save_reload_projection():
    """The stamped provider halves must be display-metadata keys, or the
    footer notice would vanish on reload (sidecar/state.db merge)."""
    from pathlib import Path

    models_py = (Path(__file__).resolve().parents[1] / "api" / "models.py").read_text(
        encoding="utf-8"
    )
    allowlist = models_py.split("_SESSION_MESSAGE_DISPLAY_METADATA_KEYS", 1)[1].split("\n)", 1)[0]
    for key in ('"_usedProvider"', '"_requestedModel"', '"_requestedProvider"', '"_usedModel"'):
        assert key in allowlist, f"{key} missing from the display-metadata allowlist"


def test_requested_provider_is_captured_before_use_in_every_scope():
    """Same NameError guard as the requested-model test: the provider halves
    must be assigned before every read in the streaming worker."""
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "api" / "streaming.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(src)
    for name in ("_requested_provider_for_switch", "_used_provider", "_local_fallback_switched"):
        checked = 0
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            stores, loads = [], []
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and sub.id == name:
                    (stores if isinstance(sub.ctx, ast.Store) else loads).append(sub.lineno)
            if not loads:
                continue
            checked += 1
            assert stores, f"{name} used without assignment in {node.name}()"
            assert min(stores) < min(loads), (
                f"{name} is read before it is assigned in {node.name}()"
            )
        assert checked, f"no function reads {name}"
