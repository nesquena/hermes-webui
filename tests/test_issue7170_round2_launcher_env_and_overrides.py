"""Repro/regression coverage for the #7170 round-2 review findings.

Pinned against HEAD 98c49366b (nesquena-hermes CR 2026-10-13):

F1 [BRICK] ``api/gateway_chat.py`` ``_gateway_session_api_key`` only read the
   session profile's ``.env`` / terminal config. Launcher-provided keys
   (``HERMES_WEBUI_GATEWAY_API_KEY`` / ``API_SERVER_KEY`` from the process env,
   the documented docker-compose deployment shape) were missed, so the gateway
   worker sent an ANONYMOUS request and the Gateway rejected it with 401. The
   key environ must be built exactly like ``_gateway_session_base_url``:
   process env minus the loaded profile env keys, overlaid with the profile's
   filtered runtime env. The regeneration launch site must capture
   ``session_api_key``/``session_base_url`` the same way the normal
   /api/chat/start dispatch does.

F2 [SILENT] ``api/config.py`` ``_get_lmstudio_reasoning_probe_api_key`` fell
   back to ambient ``os.getenv()`` even for scoped (profile-snapshot) lookups,
   so profile B's LM Studio endpoint could receive profile A's key → 401 →
   coercion reported the wrong supported ladder (``xhigh`` instead of B's
   ``high``). Scoped lookups must consult only the captured owner-profile env
   (thread-local ``api.config._thread_ctx.env`` or an explicitly threaded
   ``environ``) with NO ambient fallback.

F3 [SILENT] ``api/config.py`` ``set_reasoning_effort`` wrote only the global
   ``agent.reasoning_effort`` key even when called with a model context. With a
   ``reasoning_overrides`` entry present, the composer chip read the override
   back and snapped to the old value. A model-context selection must write the
   corresponding override; Default (``""``) clears it.
"""

import threading
import types
from pathlib import Path

import pytest
import yaml

import api.config as config
import api.gateway_chat as gateway_chat
import api.models as models
import api.routes as routes
import api.session_ops as session_ops
import api.turn_journal as turn_journal
from api.config import STREAMS


@pytest.fixture(autouse=True)
def _clear_global_stream_registries():
    """Reset module-global stream/run/gateway state between tests."""
    STREAMS.clear()
    config.PRE_ADMISSION_CLAIMS.clear()
    config.ACTIVE_RUNS.clear()
    gateway_chat._STREAM_RUN_IDS.clear()
    gateway_chat._STREAM_RUN_LIFECYCLE.clear()
    gateway_chat._STREAM_ENDPOINTS.clear()
    yield
    STREAMS.clear()
    config.PRE_ADMISSION_CLAIMS.clear()
    config.ACTIVE_RUNS.clear()
    gateway_chat._STREAM_RUN_IDS.clear()
    gateway_chat._STREAM_RUN_LIFECYCLE.clear()
    gateway_chat._STREAM_ENDPOINTS.clear()


# ---------------------------------------------------------------------------
# F1 [BRICK]: launcher-env gateway key
# ---------------------------------------------------------------------------


def _profile_home(tmp_path, name: str, *, env_lines: list[str] | None = None) -> Path:
    home = tmp_path / "profiles" / name
    home.mkdir(parents=True, exist_ok=True)
    if env_lines:
        home.joinpath(".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    return home


def _session_for_profile(profile: str = "b"):
    s = types.SimpleNamespace(profile=profile)
    return s


def test_gateway_session_api_key_reads_launcher_process_env_key(tmp_path, monkeypatch):
    """F1 RED: a Gateway key provided by the LAUNCHER (process env, docker
    ``HERMES_WEBUI_GATEWAY_API_KEY=${API_SERVER_KEY:-}``) is not claimed by any
    profile ``.env``, so ``_loaded_profile_env_keys`` does not contain it and
    the session-profile capture must return it — NOT the anonymous ``""`` that
    currently 401s the Gateway."""
    profile_b_home = _profile_home(
        tmp_path, "b", env_lines=['HERMES_WEBUI_GATEWAY_BASE_URL="http://profile-b.invalid:8642"']
    )
    import api.profiles as profiles_mod

    # Production marking: only profile-.env-loaded keys are listed. The
    # launcher key is not one of them.
    monkeypatch.setattr(profiles_mod, "_loaded_profile_env_keys", set())
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_API_KEY", "launcher-key")
    monkeypatch.setenv("API_SERVER_KEY", "launcher-key")
    monkeypatch.setattr(models, "_get_profile_home", lambda profile: profile_b_home)

    captured = gateway_chat._gateway_session_api_key(_session_for_profile("b"))
    assert captured == "launcher-key", (
        f"_gateway_session_api_key returned {captured!r}; the launcher-provided "
        "process-env key must be captured (F1). An empty capture sends an "
        "anonymous request that the keyed Gateway rejects with 401."
    )


def test_gateway_session_api_key_reads_launcher_api_server_key_alias(tmp_path, monkeypatch):
    """F1: the ``API_SERVER_KEY`` alias (the docker-compose source var) must
    also be picked up from the process env when the canonical name is unset."""
    profile_b_home = _profile_home(
        tmp_path, "b", env_lines=['HERMES_WEBUI_GATEWAY_BASE_URL="http://profile-b.invalid:8642"']
    )
    import api.profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "_loaded_profile_env_keys", set())
    monkeypatch.delenv("HERMES_WEBUI_GATEWAY_API_KEY", raising=False)
    monkeypatch.setenv("API_SERVER_KEY", "api-server-key")
    monkeypatch.setattr(models, "_get_profile_home", lambda profile: profile_b_home)

    captured = gateway_chat._gateway_session_api_key(_session_for_profile("b"))
    assert captured == "api-server-key", (
        f"_gateway_session_api_key returned {captured!r}; expected the "
        "API_SERVER_KEY alias from the process env."
    )


def test_gateway_session_api_key_strips_loaded_profile_key_for_anonymous_session(
    tmp_path, monkeypatch
):
    """F1 invariant: the anonymous-profile semantics are preserved. When the
    ambient process-env key IS a loaded profile key (production marks it in
    ``_loaded_profile_env_keys``), a session owned by a DIFFERENT keyless
    profile must still capture ``""`` — the loaded key is stripped, never
    borrowed across profiles."""
    profile_b_home = _profile_home(
        tmp_path, "b", env_lines=['HERMES_WEBUI_GATEWAY_BASE_URL="http://profile-b.invalid:8642"']
    )
    import api.profiles as profiles_mod

    # Profile A's .env was loaded into the process env at startup → marked.
    monkeypatch.setattr(
        profiles_mod,
        "_loaded_profile_env_keys",
        {"HERMES_WEBUI_GATEWAY_API_KEY", "HERMES_WEBUI_GATEWAY_BASE_URL"},
    )
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_API_KEY", "KEY_FOR_PROFILE_A")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://profile-a.invalid:8642")
    monkeypatch.setattr(models, "_get_profile_home", lambda profile: profile_b_home)

    captured = gateway_chat._gateway_session_api_key(_session_for_profile("b"))
    assert captured == "", (
        f"_gateway_session_api_key returned {captured!r}; the session profile has "
        "no gateway key, so the loaded profile A key must be stripped (anonymous "
        "profile semantics)."
    )


def test_regeneration_launch_captures_session_api_key_and_base_url(tmp_path, monkeypatch):
    """F1: the regeneration launch site must capture ``session_api_key`` /
    ``session_base_url`` for the session-owning profile like the normal
    /api/chat/start dispatch — the gated worker currently falls back to the
    ambient env pair there."""
    profile_b_home = _profile_home(
        tmp_path,
        "b",
        env_lines=[
            'HERMES_WEBUI_GATEWAY_API_KEY="KEY_FOR_PROFILE_B"',
            'HERMES_WEBUI_GATEWAY_BASE_URL="http://profile-b.invalid:8642"',
        ],
    )
    import api.profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "_loaded_profile_env_keys", set())
    # Ambient process env holds profile A's pair (the WRONG pair for B).
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_API_KEY", "KEY_FOR_PROFILE_A")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://profile-a.invalid:8642")
    monkeypatch.setattr(models, "_get_profile_home", lambda profile: profile_b_home)

    seen: dict = {}
    recorded = threading.Event()

    def _gateway_worker(session_id, msg_text, model, workspace, stream_id, attachments=None, **kwargs):
        seen.update(kwargs)
        recorded.set()
        # Mimic the real worker's admission so the launch-phase claim and the
        # gateway-starting rows are retired (no residue for later tests).
        config.register_active_run(
            stream_id,
            session_id=session_id,
            started_at=1.0,
            phase="starting",
        )
        config.retire_pre_admission_claim_if_owned(stream_id, streams_lock_held=False)
        gateway_chat._finish_gateway_run_starting(stream_id, result="fallback")
        gateway_chat._clear_gateway_run_starting(stream_id)

    monkeypatch.setattr(routes, "_run_gateway_chat_streaming", _gateway_worker)
    monkeypatch.setattr(
        routes.uuid, "uuid4", lambda: types.SimpleNamespace(hex="stream-regen-capture")
    )
    monkeypatch.setattr(
        routes, "_prepare_chat_start_session_for_stream", lambda *a, **k: None
    )
    monkeypatch.setattr(routes, "set_last_workspace", lambda *a, **k: None)
    monkeypatch.setattr(
        routes, "publish_session_list_changed", lambda *a, **k: None, raising=False
    )
    monkeypatch.setattr(
        routes, "compression_recovery_payload_for_session", lambda s: None, raising=False
    )
    monkeypatch.setattr(routes, "clear_compression_recovery", lambda s: None, raising=False)
    monkeypatch.setattr(
        routes, "_is_hidden_empty_session", lambda s: False, raising=False
    )
    monkeypatch.setattr(turn_journal, "append_turn_journal_event", lambda *a, **k: {})
    turn = types.SimpleNamespace(
        revision=1, message_text="hello", attachments=[], source="webui"
    )
    monkeypatch.setattr(
        session_ops,
        "plan_regeneration",
        lambda s, expected_revision, lock_held: types.SimpleNamespace(turn=turn),
    )
    monkeypatch.setattr(session_ops, "snapshot_regeneration_state", lambda s: {})
    monkeypatch.setattr(
        session_ops,
        "apply_regeneration_plan",
        lambda s, plan, return_context_user=False: (True, None),
    )

    session = types.SimpleNamespace(
        session_id="regen-session",
        profile="b",
        model="claude-opus-4-5",
        workspace="test-workspace",
        title="t",
        pending_started_at=1.0,
        messages=[types.SimpleNamespace(role="user", text="hello")],
        active_stream_id=None,
        save=lambda: None,
    )

    response = routes._start_regeneration_stream_locked(
        session,
        turn=turn,
        workspace="test-workspace",
        model="claude-opus-4-5",
        model_provider="anthropic",
        normalized_model=False,
        diag=None,
        goal_related=False,
        source="webui",
        moa_config=None,
        backend_is_gateway=True,
    )
    assert response is not None and response.get("stream_id") == "stream-regen-capture"

    assert recorded.wait(5), "the gated regeneration worker never recorded its kwargs"
    assert seen.get("session_api_key") == "KEY_FOR_PROFILE_B", (
        f"regeneration worker received session_api_key={seen.get('session_api_key')!r}; "
        "the regeneration launch must capture the session-owning profile's key "
        "(F1) instead of falling back to the ambient env key "
        "(KEY_FOR_PROFILE_A)."
    )
    assert seen.get("session_base_url") == "http://profile-b.invalid:8642", (
        f"regeneration worker received session_base_url={seen.get('session_base_url')!r}; "
        "expected the session profile's gateway URL."
    )


# ---------------------------------------------------------------------------
# F2 [SILENT]: scoped LM Studio probe env isolation
# ---------------------------------------------------------------------------


class _ProbeRecorder:
    """Stand-in for ``_lmstudio_model_reasoning_options`` recording the key."""

    def __init__(self, options):
        self._options = list(options)
        self.calls: list[dict] = []

    def __call__(self, model, base_url, *, api_key=None, timeout=5.0):
        self.calls.append({"model": model, "base_url": base_url, "api_key": api_key})
        return list(self._options)


@pytest.fixture
def thread_env(monkeypatch):
    """Bind thread-local env state for the duration of one test.

    ``threading.local`` attributes cannot be monkeypatched (pytest snapshots
    the old value, which raises AttributeError when the attribute was never
    set on this thread), so set them directly and restore/remove them at
    teardown.
    """
    prev_env = getattr(config._thread_ctx, "env", None)
    prev_block = getattr(config._thread_ctx, "block_process_env_fallback", None)
    had_env = hasattr(config._thread_ctx, "env")
    had_block = hasattr(config._thread_ctx, "block_process_env_fallback")

    def _apply(env: dict, block_fallback: bool):
        config._thread_ctx.env = dict(env)
        config._thread_ctx.block_process_env_fallback = bool(block_fallback)

    yield _apply

    if had_env and prev_env is not None:
        config._thread_ctx.env = prev_env
    else:
        try:
            del config._thread_ctx.env
        except AttributeError:
            pass
    if had_block and prev_block is not None:
        config._thread_ctx.block_process_env_fallback = prev_block
    else:
        try:
            del config._thread_ctx.block_process_env_fallback
        except AttributeError:
            pass


def test_scoped_lmstudio_probe_does_not_fall_back_to_ambient_env_key(monkeypatch, thread_env):
    """F2 RED: a scoped coercion (profile snapshot handed in) probes profile
    B's LM Studio endpoint. The ambient process env holds profile A's
    ``LM_API_KEY``; the probe must NOT receive it — the scoped lookup has no
    ambient fallback. With the leaked key the endpoint 401s, the probe returns
    nothing and coercion preserves ``xhigh``; keyless, B's ladder is returned
    and ``xhigh`` degrades to ``high``."""
    probe_url = "http://profile-b-host:1234/v1"
    profile_b_cfg = {
        "model": {"provider": "lmstudio", "default": "b-model", "base_url": probe_url},
        "providers": {"lmstudio": {"base_url": probe_url}},  # NO api_key anywhere
    }
    recorder = _ProbeRecorder(["low", "high"])  # B's endpoint ladder
    monkeypatch.setattr(config, "_lmstudio_model_reasoning_options", recorder)
    monkeypatch.setenv("LM_API_KEY", "KEY_FOR_PROFILE_A")
    monkeypatch.setenv("LMSTUDIO_API_KEY", "KEY_FOR_PROFILE_A")

    # Detached-worker-like thread state: no captured owner env.
    thread_env({}, block_fallback=False)

    coerced = config.coerce_reasoning_effort_for_model(
        "xhigh",
        "b-model",
        provider_id="lmstudio",
        base_url=probe_url,
        config_data=profile_b_cfg,
    )

    assert recorder.calls, "the LM Studio probe was never invoked"
    last = recorder.calls[-1]
    assert last["api_key"] is None, (
        f"scoped probe carried api_key={last['api_key']!r} — profile A's ambient "
        "LM_API_KEY leaked into profile B's endpoint (F2). Scoped lookups must "
        "not fall back to the ambient env."
    )
    assert coerced == "high", (
        f"coercion returned {coerced!r}; B's ladder supports high, so xhigh must "
        "degrade to high, not be preserved because the leaked key 401'd the probe."
    )


def test_scoped_lmstudio_probe_uses_thread_captured_owner_env_key(monkeypatch, thread_env):
    """F2: on a profile-scoped request thread the captured owner env
    (``api.config._thread_ctx.env``) is the only env the probe may read — B's
    own key is used, never the ambient A key."""
    probe_url = "http://profile-b-host:1234/v1"
    profile_b_cfg = {
        "model": {"provider": "lmstudio", "default": "b-model", "base_url": probe_url},
        "providers": {"lmstudio": {"base_url": probe_url}},  # NO config key
    }
    recorder = _ProbeRecorder(["low", "high"])
    monkeypatch.setattr(config, "_lmstudio_model_reasoning_options", recorder)
    monkeypatch.setenv("LM_API_KEY", "KEY_FOR_PROFILE_A")
    monkeypatch.setenv("LMSTUDIO_API_KEY", "KEY_FOR_PROFILE_A")

    # Request-thread-like: profile B's env captured thread-locally, process
    # fallback blocked.
    thread_env({"LM_API_KEY": "KEY_FOR_PROFILE_B"}, block_fallback=True)

    coerced = config.coerce_reasoning_effort_for_model(
        "xhigh",
        "b-model",
        provider_id="lmstudio",
        base_url=probe_url,
        config_data=profile_b_cfg,
    )

    assert recorder.calls, "the LM Studio probe was never invoked"
    last = recorder.calls[-1]
    assert last["api_key"] == "KEY_FOR_PROFILE_B", (
        f"scoped probe carried api_key={last['api_key']!r}; expected the "
        "thread-captured owner env key KEY_FOR_PROFILE_B, never the ambient "
        "KEY_FOR_PROFILE_A."
    )
    assert coerced == "high"


def test_unscoped_lmstudio_probe_keeps_ambient_env_fallback(monkeypatch, thread_env):
    """F2 invariant: unscoped lookups (no profile snapshot) keep the historical
    ambient ``os.environ`` fallback — the docker-compose / single-profile
    deployments still configure LM Studio purely via env."""
    probe_url = "http://lmstudio-host:1234/v1"
    recorder = _ProbeRecorder(["low", "medium", "high"])
    monkeypatch.setattr(config, "_lmstudio_model_reasoning_options", recorder)
    # The active profile's config declares the endpoint (module-global cfg,
    # the historical unscoped source); the key lives in the ambient env only.
    monkeypatch.setattr(
        config,
        "cfg",
        {
            "model": {"provider": "lmstudio", "default": "b-model", "base_url": probe_url},
            "providers": {"lmstudio": {"base_url": probe_url}},
        },
    )
    monkeypatch.setenv("LM_API_KEY", "ambient-lm-key")
    thread_env({}, block_fallback=False)

    efforts = config.resolve_model_reasoning_efforts(
        "b-model", provider_id="lmstudio", base_url=probe_url
    )
    assert efforts == ["low", "medium", "high"]
    assert recorder.calls[-1]["api_key"] == "ambient-lm-key", (
        "unscoped lookup must keep the historical ambient env fallback."
    )


# ---------------------------------------------------------------------------
# F3 [SILENT]: model-context reasoning selection must write the override
# ---------------------------------------------------------------------------


def _write_reasoning_cfg(cfgfile: Path) -> None:
    cfgfile.write_text(
        yaml.safe_dump(
            {
                "model": {"default": "claude-opus-4-5", "provider": "anthropic"},
                "agent": {
                    "reasoning_effort": "medium",
                    "reasoning_overrides": {"claude-opus-4-5": "low"},
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _patch_reasoning_scope(tmp_path, monkeypatch, cfgfile: Path):
    monkeypatch.setattr(config, "_get_config_path", lambda: cfgfile)
    monkeypatch.setattr(config, "reload_config", lambda: None)
    monkeypatch.setattr(
        config,
        "resolve_model_reasoning_efforts",
        lambda model_id, provider_id=None, base_url=None, config_data=None: [
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        ],
    )


def test_set_reasoning_effort_model_context_writes_override(tmp_path, monkeypatch):
    """F3 RED: with a ``reasoning_overrides`` entry present, a model-context
    selection must write the OVERRIDE (so the chip reads its own selection
    back) — before the fix only the global key was written and the chip
    snapped to the stale override value."""
    cfgfile = tmp_path / "config.yaml"
    _write_reasoning_cfg(cfgfile)
    _patch_reasoning_scope(tmp_path, monkeypatch, cfgfile)

    status = config.set_reasoning_effort(
        "high", model_id="claude-opus-4-5", provider_id="anthropic"
    )

    assert status["reasoning_effort"] == "high", (
        f"set_reasoning_effort returned reasoning_effort={status['reasoning_effort']!r}; "
        "a model-context selection wrote only the global key, so the chip read "
        "the override (low) back and snapped (F3)."
    )
    on_disk = yaml.safe_load(cfgfile.read_text(encoding="utf-8"))
    agent_cfg = on_disk.get("agent") or {}
    assert (agent_cfg.get("reasoning_overrides") or {}).get("claude-opus-4-5") == "high", (
        "the selection must be persisted as the per-model override."
    )


def test_set_reasoning_effort_default_clears_model_override(tmp_path, monkeypatch):
    """F3: Default (``""``) in a model context clears the override (and the
    global key, the historical empty-effort contract) so the provider default
    applies again — the chip must not snap back to the stale override."""
    cfgfile = tmp_path / "config.yaml"
    _write_reasoning_cfg(cfgfile)
    _patch_reasoning_scope(tmp_path, monkeypatch, cfgfile)

    status = config.set_reasoning_effort(
        "", model_id="claude-opus-4-5", provider_id="anthropic"
    )

    assert status["reasoning_effort"] == "", (
        f"Default returned reasoning_effort={status['reasoning_effort']!r}; "
        "clearing the model override must let the provider default apply."
    )
    on_disk = yaml.safe_load(cfgfile.read_text(encoding="utf-8"))
    agent_cfg = on_disk.get("agent") or {}
    assert "claude-opus-4-5" not in (agent_cfg.get("reasoning_overrides") or {}), (
        "Default must remove the per-model override entry."
    )
    assert "reasoning_effort" not in agent_cfg, (
        "Default must clear the global reasoning_effort key (historical "
        "empty-effort contract)."
    )


def test_set_reasoning_effort_without_model_still_writes_global(tmp_path, monkeypatch):
    """F3 invariant: an unscoped selection (no model context) keeps writing the
    global ``agent.reasoning_effort`` key — and does not invent an override."""
    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text(
        yaml.safe_dump(
            {
                "model": {"default": "claude-opus-4-5", "provider": "anthropic"},
                "agent": {"reasoning_effort": "medium"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _patch_reasoning_scope(tmp_path, monkeypatch, cfgfile)

    status = config.set_reasoning_effort("xhigh")

    assert status["reasoning_effort"] == "xhigh"
    on_disk = yaml.safe_load(cfgfile.read_text(encoding="utf-8"))
    agent_cfg = on_disk.get("agent") or {}
    assert agent_cfg.get("reasoning_effort") == "xhigh"
    assert not (agent_cfg.get("reasoning_overrides") or {}), (
        "an unscoped selection must not create per-model overrides."
    )


# ---------------------------------------------------------------------------
# droopanu (real user, 2026-10-20): the write key and the read matchers
# disagreed on spelling.
# ---------------------------------------------------------------------------


def _write_cfg_with(cfgfile: Path, overrides: dict, effort: str = "high") -> None:
    cfgfile.write_text(
        yaml.safe_dump(
            {
                "model": {"default": "gpt-5.4", "provider": "openai"},
                "agent": {
                    "reasoning_effort": effort,
                    "reasoning_overrides": overrides,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_default_clears_every_spelling_the_reader_accepts(tmp_path, monkeypatch):
    """Default for ``gpt-5.4`` must not leave ``openai/gpt-5.4`` behind.

    The read side matches the bare id, its lowercase form, the ``@provider:id``
    tail and every slash-qualified form, so a config carrying
    ``openai/gpt-5.4: low`` still answered for ``gpt-5.4``. Picking ``high``
    for the bare model and then ``Default`` removed only the bare key, and the
    effective effort silently fell back to ``low``.
    """
    cfgfile = tmp_path / "config.yaml"
    _write_cfg_with(cfgfile, {"openai/gpt-5.4": "low"})
    _patch_reasoning_scope(tmp_path, monkeypatch, cfgfile)

    config.set_reasoning_effort("", model_id="gpt-5.4", provider_id="openai")

    on_disk = yaml.safe_load(cfgfile.read_text(encoding="utf-8"))
    overrides = (on_disk.get("agent") or {}).get("reasoning_overrides") or {}
    assert not overrides, (
        f"Default must clear every spelling the reader accepts, got {overrides!r}"
    )


def test_a_new_level_supersedes_the_qualified_spelling(tmp_path, monkeypatch):
    """Picking a level for the bare model must beat the qualified entry.

    With ``openai/gpt-5.4: low`` stored, choosing ``high`` for ``gpt-5.4``
    must not leave the stale qualified entry able to win the read.
    """
    cfgfile = tmp_path / "config.yaml"
    _write_cfg_with(cfgfile, {"openai/gpt-5.4": "low"})
    _patch_reasoning_scope(tmp_path, monkeypatch, cfgfile)

    config.set_reasoning_effort("high", model_id="gpt-5.4", provider_id="openai")

    on_disk = yaml.safe_load(cfgfile.read_text(encoding="utf-8"))
    overrides = (on_disk.get("agent") or {}).get("reasoning_overrides") or {}
    assert overrides.get("gpt-5.4") == "high", (
        f"the bare key must carry the new level, got {overrides!r}"
    )
    assert overrides.get("openai/gpt-5.4") != "low", (
        "the stale qualified entry must not survive with its old value"
    )
    # ...and the read side must agree with what was written.
    assert (
        config.configured_reasoning_effort_for_model(
            on_disk, model_id="gpt-5.4", provider_id="openai"
        )
        == "high"
    ), "the effective effort must be the level the user just picked"


def test_unrelated_override_entries_survive(tmp_path, monkeypatch):
    """Clearing one model must not touch another model's override."""
    cfgfile = tmp_path / "config.yaml"
    _write_cfg_with(cfgfile, {"gpt-5.4": "low", "claude-opus-4-5": "medium"})
    _patch_reasoning_scope(tmp_path, monkeypatch, cfgfile)

    config.set_reasoning_effort("", model_id="gpt-5.4", provider_id="openai")

    on_disk = yaml.safe_load(cfgfile.read_text(encoding="utf-8"))
    overrides = (on_disk.get("agent") or {}).get("reasoning_overrides") or {}
    assert overrides == {"claude-opus-4-5": "medium"}, (
        f"unrelated override entries must survive, got {overrides!r}"
    )
