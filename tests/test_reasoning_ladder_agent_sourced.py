"""The reasoning-effort ladder is a UNION of the WebUI's historic levels and
whatever the installed Hermes Agent adds on top, ranked in a fixed canonical
order; above-``max`` levels are offered/accepted only on Agents whose route
rule (``agent.reasoning_effort.route_supported_efforts``) proves their wire
transports clamp; and ceiling rules cap everything ABOVE the documented top,
not just the literal 'max'.

Bug being fixed: the WebUI hardcoded its own six-level ladder in
api/config.py and shipped eight static dropdown rows in the page markup, so
when Hermes Agent released 'ultra' above 'max' (the level the TUI's
`hermes model` picker offers via hermes_constants.VALID_REASONING_EFFORTS),
the composer dropdown still topped out at Max.

The ladder must be agent-FORWARD but also release-agnostic: released agent
tuples differ in BOTH set and order (v2026.3.28 exported
("xhigh","high","medium","low","minimal") DESCENDING with no 'max';
April-July 2026 agents topped out at 'xhigh'; v2026.7.7 added 'max';
v2026.7.20 added 'ultra' but its transports forwarded it verbatim until the
clamp era). So api/config.py ranks the six levels the WebUI always accepted
first, then appends agent extras only when the route rule is importable.
None of the tests below may assume the ladder — or the route-rule vintage —
of whatever Agent happens to be installed on the CI host: they monkeypatch
both or derive from them.
"""

import pytest

from api import config as cfg

# A deterministic 7-level ladder standing in for "current agent" on any host.
_LADDER = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")

# Historical agent tuples, verbatim.
_AGENT_3_28_DESCENDING = ("xhigh", "high", "medium", "low", "minimal")
_AGENT_APRIL_JULY_NO_MAX = ("minimal", "low", "medium", "high", "xhigh")
_AGENT_7_7_WITH_MAX = ("minimal", "low", "medium", "high", "xhigh", "max")
_AGENT_7_20_WITH_ULTRA = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")

# Real runtime-config reader, captured before the autouse fixture stubs it.
_REAL_RUNTIME_READER = cfg.openai_runtime_api_mode


def _faithful_route_rule(provider, model, api_mode=None):
    """Mirror of the current Agent's route rule for deterministic tests:
    'ultra' only on openai-codex + codex_app_server + a model whose codex
    ladder reaches 'max' (gpt-5.6 in these fixtures)."""
    if str(provider) == "openai-codex":
        supported = (
            ("low", "medium", "high", "xhigh", "max")
            if "5.6" in str(model) or "6" in str(model)
            else ("low", "medium", "high", "xhigh")
        )
        if api_mode == "codex_app_server" and "max" in supported:
            return (*supported, "ultra")
        return supported
    return ("minimal", "low", "medium", "high", "xhigh", "max")


@pytest.fixture(autouse=True)
def _deterministic_ladder(monkeypatch):
    """Pin the module ladder, the route-rule feature, and the ambient runtime
    read so no test depends on the installed Agent or the host profile."""
    monkeypatch.setattr(cfg, "VALID_REASONING_EFFORTS", _LADDER)
    monkeypatch.setattr(
        cfg, "_agent_route_supported_efforts", lambda: _faithful_route_rule
    )
    monkeypatch.setattr(cfg, "openai_runtime_api_mode", lambda profile_home=None: None)


def _runtime_yaml(monkeypatch, tmp_path, value):
    """Point the ambient config read at a real YAML file carrying
    model.openai_runtime (value None = the key is absent) and restore the real
    runtime reader."""
    body = "model:\n"
    if value is not None:
        body += f"  openai_runtime: {value}\n"
    path = tmp_path / f"config-{value}.yaml"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setattr(cfg, "_get_config_path", lambda: path)
    monkeypatch.setattr(cfg, "openai_runtime_api_mode", _REAL_RUNTIME_READER)


# ── _rank_valid_efforts: union + canonical rank (release-agnostic ladder) ───

def test_rank_descending_old_agent_tuple_is_reordered_and_max_kept():
    # v2026.3.28: descending AND missing 'max'. The WebUI must not lose 'max'
    # (sessions stored under master keep running) and must not inherit the
    # descending order into ceiling math.
    ranked = cfg._rank_valid_efforts(_AGENT_3_28_DESCENDING)
    assert ranked == cfg._WEBUI_BASE_EFFORTS
    assert ranked.index("xhigh") < ranked.index("max")


def test_rank_no_max_agent_tuple_still_yields_max():
    # April-July 2026 agents: 'max' is WebUI-accepted history, not agent-gated.
    assert cfg._rank_valid_efforts(_AGENT_APRIL_JULY_NO_MAX) == cfg._WEBUI_BASE_EFFORTS


def test_rank_empty_agent_ladder_is_the_base():
    assert cfg._rank_valid_efforts(()) == cfg._WEBUI_BASE_EFFORTS
    assert cfg._rank_valid_efforts(None) == cfg._WEBUI_BASE_EFFORTS


def test_rank_agent_extras_append_in_agent_order():
    assert cfg._rank_valid_efforts(_AGENT_7_7_WITH_MAX) == cfg._WEBUI_BASE_EFFORTS
    assert cfg._rank_valid_efforts(_AGENT_7_20_WITH_ULTRA) == _LADDER
    # Future level released above ultra joins automatically, in agent order.
    assert cfg._rank_valid_efforts((*_AGENT_7_20_WITH_ULTRA, "hyper")) == (*_LADDER, "hyper")


def test_rank_strips_extras_on_pre_clamp_agent(monkeypatch):
    # v2026.7.20..~2026-08-19: 'ultra' in the ladder, no route rule, no wire
    # clamp — the level must be neither offered nor accepted.
    monkeypatch.setattr(cfg, "_agent_route_supported_efforts", lambda: None)
    assert cfg._rank_valid_efforts(_AGENT_7_20_WITH_ULTRA) == cfg._WEBUI_BASE_EFFORTS


def test_parse_accepts_max_on_an_old_agent_without_max():
    # Regression for the downgrade the union prevents: with an April-July
    # agent installed, a stored 'max' must still parse, not become Default.
    monkey_ladder = cfg._rank_valid_efforts(_AGENT_APRIL_JULY_NO_MAX)
    assert "max" in monkey_ladder
    assert cfg.parse_reasoning_effort("max") == {"enabled": True, "effort": "max"}


# ── _efforts_above: the generic ceiling helper (ranked, never positional-in-agent) ──

def test_efforts_above_xhigh_includes_max_and_ultra():
    above = cfg._efforts_above("xhigh")
    assert "max" in above and "ultra" in above
    assert "xhigh" not in above and "high" not in above


def test_efforts_above_max_includes_only_rungs_above_max():
    above = cfg._efforts_above("max")
    assert "ultra" in above
    assert "max" not in above and "xhigh" not in above


def test_efforts_above_is_not_inverted_by_a_descending_agent_tuple(monkeypatch):
    # On the v2026.3.28 descending tuple, "everything after xhigh" in AGENT
    # order would be the WEAK levels — ranking must make this frozenset {max}.
    monkeypatch.setattr(
        cfg, "VALID_REASONING_EFFORTS", cfg._rank_valid_efforts(_AGENT_3_28_DESCENDING)
    )
    assert cfg._efforts_above("xhigh") == frozenset({"max"})


def test_efforts_above_unknown_ceiling_is_empty():
    assert cfg._efforts_above("nonexistent-level") == frozenset()


# ── Provider ceilings cap 'ultra' exactly like 'max' ─────────────────────────

def test_gemini_ceiling_strips_ultra_and_max_keeps_xhigh():
    efforts = cfg._filter_reasoning_efforts_for_provider(
        list(cfg.VALID_REASONING_EFFORTS), "gemini-4-argon", "gemini"
    )
    assert "xhigh" in efforts
    assert "max" not in efforts, "existing ceiling: Gemini tops out at xhigh"
    assert "ultra" not in efforts, (
        "the ceiling is 'tops out at xhigh' — a level above max must be "
        f"stripped with it; got {efforts!r}"
    )


def test_pre_gpt56_ceiling_strips_ultra_and_max():
    efforts = cfg._filter_reasoning_efforts_for_provider(
        list(cfg.VALID_REASONING_EFFORTS), "gpt-5.5", "openai"
    )
    assert "xhigh" in efforts and "max" not in efforts and "ultra" not in efforts


def test_legacy_claude_ceiling_strips_ultra_and_max():
    efforts = cfg._filter_reasoning_efforts_for_provider(
        list(cfg.VALID_REASONING_EFFORTS), "claude-sonnet-4-5", "anthropic"
    )
    assert "xhigh" in efforts and "max" not in efforts and "ultra" not in efforts


def test_uncapped_reasoning_lane_reaches_ultra():
    # A provider with no ceiling rule keeps the full ladder — this is the
    # user-facing fix: reasoning-capable routes now advertise the agent's
    # current ceiling instead of the stale hardcoded 'max'.
    efforts = cfg._filter_reasoning_efforts_for_provider(
        list(cfg.VALID_REASONING_EFFORTS), "qwen3.8-flash", "deepseek"
    )
    assert efforts == list(cfg.VALID_REASONING_EFFORTS)
    assert "ultra" in efforts


def test_zai_effort_tier_caps_at_max_not_ultra():
    # Z.AI documents its ladder top as 'max' (GLM-5.2+); 'ultra' is a
    # Hermes-internal rung above it and must not be advertised as a native
    # Z.AI wire level.
    efforts = cfg._filter_reasoning_efforts_for_provider(
        list(cfg.VALID_REASONING_EFFORTS), "glm-5.3", "zai"
    )
    assert "max" in efforts
    assert "ultra" not in efforts, f"zai tier caps at max; got {efforts!r}"


# ── Wire survival: the Agent's own rule + real runtime config ────────────────

def test_default_codex_without_opt_in_sends_max(tmp_path, monkeypatch):
    # model.openai_runtime absent or 'auto' resolves to codex_responses; the
    # default Codex setup must NOT keep 'ultra' (gate repro: chip said
    # Ultra High while the route sends max).
    for value in (None, "auto"):
        _runtime_yaml(monkeypatch, tmp_path, value)
        assert cfg._route_wire_accepts_ultra("gpt-5.6", "openai-codex") is False
        assert cfg.coerce_reasoning_effort_for_model(
            "ultra", model_id="gpt-5.6", provider_id="openai-codex"
        ) == "max"


def test_explicit_app_server_opt_in_keeps_ultra(tmp_path, monkeypatch):
    _runtime_yaml(monkeypatch, tmp_path, "codex_app_server")
    assert cfg._route_wire_accepts_ultra("gpt-5.6", "openai-codex") is True
    monkeypatch.setattr(
        cfg, "resolve_model_reasoning_efforts", lambda *a, **k: list(_LADDER)
    )
    assert cfg.coerce_reasoning_effort_for_model(
        "ultra", model_id="gpt-5.6", provider_id="openai-codex"
    ) == "ultra"
    # Only for models whose ladder reaches 'max' — the Agent rule decides.
    assert cfg._route_wire_accepts_ultra("gpt-5.5", "openai-codex") is False


def test_custom_providers_never_get_ultra(tmp_path, monkeypatch):
    # All three custom shapes (prefixed-native, bare-native, legacy
    # custom_providers[]) may carry api_mode: codex_app_server, yet the Agent
    # rule gives ANY custom route the OpenAI-compat wire: max is the ceiling.
    # The prefixed form is what sessions actually persist.
    _runtime_yaml(monkeypatch, tmp_path, "codex_app_server")
    monkeypatch.setattr(
        cfg,
        "_load_yaml_config_file",
        lambda path: {
            "model": {"openai_runtime": "codex_app_server"},
            "providers": {"reviewer-codex": {"api_mode": "codex_app_server"}},
            "custom_providers": [{"name": "mygw", "api_mode": "codex_app_server"}],
        },
    )
    for provider in ("custom:reviewer-codex", "reviewer-codex", "custom:mygw"):
        assert cfg._route_wire_accepts_ultra("gpt-5.6", provider) is False, provider


def test_ultra_sends_max_on_documented_max_routes(monkeypatch):
    # Release-gate must-fix: the WebUI must not rely on the installed Agent
    # clamping 'ultra' (v2026.7.20..~2026-08-19 forward it verbatim and an
    # OpenAI-compatible wire 400s). Uncapped non-codex route => send 'max'.
    monkeypatch.setattr(
        cfg, "resolve_model_reasoning_efforts", lambda *a, **k: list(_LADDER)
    )
    assert cfg.coerce_reasoning_effort_for_model(
        "ultra", model_id="qwen3.8-flash", provider_id="deepseek"
    ) == "max"


def test_pre_clamp_agent_rejects_and_coerces_ultra(monkeypatch):
    # Older-Agent case demanded by the gate: a ladder WITH 'ultra' but no
    # route rule — ultra is neither offered nor accepted, and a stored one
    # coerces to 'max' (not Default).
    monkeypatch.setattr(cfg, "_agent_route_supported_efforts", lambda: None)
    monkeypatch.setattr(cfg, "VALID_REASONING_EFFORTS", cfg._WEBUI_BASE_EFFORTS)
    assert cfg.parse_reasoning_effort("ultra") is None
    assert cfg.coerce_reasoning_effort_for_model(
        "ultra", model_id="qwen3.8-flash", provider_id="deepseek"
    ) == "max"
    # Advertised list: metadata that already knows 'ultra' must not offer it.
    monkeypatch.setattr(
        cfg,
        "_resolve_model_reasoning_efforts_impl",
        lambda *a, **k: list(_LADDER),
    )
    assert cfg.resolve_model_reasoning_efforts(
        "qwen3.8-flash", provider_id="deepseek"
    ) == list(cfg._WEBUI_BASE_EFFORTS)


# ── Resolved-runtime threading (review round-4 must-fix) ─────────────────────

def test_bundle_api_mode_beats_ambient_opt_out(tmp_path, monkeypatch):
    # Session profile opted into the app-server runtime (its bundle says so)
    # while the AMBIENT profile has no opt-in. The effort that reaches the
    # AIAgent must be 'ultra' — ambient config must not veto the real runtime.
    _runtime_yaml(monkeypatch, tmp_path, None)
    monkeypatch.setattr(
        cfg, "resolve_model_reasoning_efforts", lambda *a, **k: list(_LADDER)
    )
    assert cfg._route_wire_accepts_ultra("gpt-5.6", "openai-codex", "codex_app_server") is True
    assert cfg.resolve_session_reasoning_effort(
        {}, session_effort="ultra", model_id="gpt-5.6", provider_id="openai-codex",
        api_mode="codex_app_server",
    ) == "ultra"


def test_bundle_api_mode_beats_ambient_opt_in(tmp_path, monkeypatch):
    # The reverse mismatch: ambient opted in, but THIS session's runtime
    # resolved to the plain Responses API — 'ultra' must coerce to 'max' and
    # never hit the wire. Without an explicit bundle the ambient read still
    # answers as the fallback.
    _runtime_yaml(monkeypatch, tmp_path, "codex_app_server")
    monkeypatch.setattr(
        cfg, "resolve_model_reasoning_efforts", lambda *a, **k: list(_LADDER)
    )
    assert cfg._route_wire_accepts_ultra("gpt-5.6", "openai-codex", "codex_responses") is False
    assert cfg.resolve_session_reasoning_effort(
        {}, session_effort="ultra", model_id="gpt-5.6", provider_id="openai-codex",
        api_mode="codex_responses",
    ) == "max"
    assert cfg.resolve_session_reasoning_effort(
        {}, session_effort="ultra", model_id="gpt-5.6", provider_id="openai-codex",
    ) == "ultra"


def test_openai_runtime_api_mode_reads_the_named_profile(tmp_path, monkeypatch):
    # The session-profile reader used by the reasoning status path: the opt-in
    # lives in the SESSION's profile, not the ambient one. A consulted profile
    # without the opt-in answers CONCRETELY (codex_responses — what absent/
    # auto/unrecognised values resolve to), never the None that means "no
    # authority, ask ambient".
    monkeypatch.setattr(
        cfg,
        "get_config_for_profile_home",
        lambda home, **kw: (
            {"model": {"openai_runtime": "codex_app_server"}}
            if "optin" in str(home)
            else ({"model": {"openai_runtime": "banana"}} if "weird" in str(home) else {})
        ),
    )
    monkeypatch.setattr(cfg, "openai_runtime_api_mode", _REAL_RUNTIME_READER)
    assert cfg.openai_runtime_api_mode(tmp_path / "optin") == "codex_app_server"
    assert cfg.openai_runtime_api_mode(tmp_path / "plain") == "codex_responses"
    # Unrecognised values behave like auto (plain Responses API), like the
    # Agent's runtime_provider reference.
    assert cfg.openai_runtime_api_mode(tmp_path / "weird") == "codex_responses"


def test_composed_named_profile_optout_beats_ambient_opt_in(tmp_path, monkeypatch):
    # Review round-5 composed case (real isolated reader + real status path):
    # ambient opts INTO the app-server runtime while the session's named
    # profile keeps absent/auto. Status must answer max + annotation — the
    # same value the turn resolver gets from the runtime bundle — instead of
    # re-reading the ambient opt-in through the old None ambiguity.
    _runtime_yaml(monkeypatch, tmp_path, "codex_app_server")
    named = tmp_path / "profile-auto"
    named.mkdir()
    (named / "config.yaml").write_text(
        "model:\n  openai_runtime: auto\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        cfg, "resolve_model_reasoning_efforts", lambda *a, **k: list(_LADDER)
    )
    profile_mode = cfg.openai_runtime_api_mode(named)
    assert profile_mode == "codex_responses", (
        "a consulted opt-out profile must be concrete, not the None that "
        "means 'no authority' (it used to trigger the ambient re-read)"
    )
    st = cfg.get_reasoning_status(
        model_id="gpt-5.6", provider_id="openai-codex",
        effort_override="ultra", api_mode=profile_mode,
    )
    assert st["reasoning_effort"] == "max"
    assert st["reasoning_effort_display"] == "ultra (sends max on this route)"
    # Genuine ambient callers (no session, no bundle) keep answering for the
    # default profile — the fallback still sees its opt-in.
    ambient_st = cfg.get_reasoning_status(
        model_id="gpt-5.6", provider_id="openai-codex", effort_override="ultra",
    )
    assert ambient_st["reasoning_effort"] == "ultra"


def test_composed_named_profile_optin_beats_ambient_optout(tmp_path, monkeypatch):
    # The reverse control: ambient absent/auto, the session's named profile
    # opts in — status must answer ultra from the profile, matching the turn
    # resolver's app-server bundle.
    _runtime_yaml(monkeypatch, tmp_path, None)
    named = tmp_path / "profile-optin"
    named.mkdir()
    (named / "config.yaml").write_text(
        "model:\n  openai_runtime: codex_app_server\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        cfg, "resolve_model_reasoning_efforts", lambda *a, **k: list(_LADDER)
    )
    profile_mode = cfg.openai_runtime_api_mode(named)
    assert profile_mode == "codex_app_server"
    st = cfg.get_reasoning_status(
        model_id="gpt-5.6", provider_id="openai-codex",
        effort_override="ultra", api_mode=profile_mode,
    )
    assert st["reasoning_effort"] == "ultra"
    assert st["reasoning_effort_display"] == "ultra"


def test_status_path_answers_for_the_session_runtime(monkeypatch):
    # /api/reasoning must answer with the runtime the caller resolved (the
    # session's), not the ambient one: ambient opted out (autouse stub),
    # explicit app-server mode => ultra survives; responses => coerced +
    # annotated.
    monkeypatch.setattr(
        cfg, "resolve_model_reasoning_efforts", lambda *a, **k: list(_LADDER)
    )
    monkeypatch.setattr(cfg, "_load_yaml_config_file", lambda path: {})
    st = cfg.get_reasoning_status(
        model_id="gpt-5.6", provider_id="openai-codex",
        effort_override="ultra", api_mode="codex_app_server",
    )
    assert st["reasoning_effort"] == "ultra"
    assert st["reasoning_effort_display"] == "ultra"
    st = cfg.get_reasoning_status(
        model_id="gpt-5.6", provider_id="openai-codex",
        effort_override="ultra", api_mode="codex_responses",
    )
    assert st["reasoning_effort"] == "max"
    assert st["reasoning_effort_display"] == "ultra (sends max on this route)"


def test_chat_and_status_call_sites_pass_the_resolved_runtime():
    # Contract pins: the chat path hands its `_runtime_bundle['api_mode']`
    # (the exact value going into AIAgent kwargs) to the resolver, and when
    # the runtime RESOLVER failed (round-7 should-fix 3) it falls back to the
    # SESSION profile's reader — never a bare ambient read on the worker. The
    # reasoning status handlers read the opt-in from the SESSION's profile,
    # and so does the Gateway worker (round-7 must-fix 2).
    import pathlib

    here = pathlib.Path(cfg.__file__).parent
    streaming = (here / "streaming.py").read_text(encoding="utf-8")
    routes = (here / "routes.py").read_text(encoding="utf-8")
    gateway = (here / "gateway_chat.py").read_text(encoding="utf-8")
    assert "api_mode=_runtime_bundle.get('api_mode')" in streaming
    assert "api_mode=_runtime_bundle.get('api_mode') or None" not in streaming
    assert "or openai_runtime_api_mode(_profile_home or None)" in streaming
    assert 'effort_kwargs["api_mode"] = openai_runtime_api_mode(_reasoning_home)' in routes
    assert routes.count("api_mode=openai_runtime_api_mode(") >= 1
    assert "session_runtime = _gateway_profile_runtime(_session_profile_home)" in gateway
    assert "api_mode=session_runtime" in gateway


# ── Composed regressions through the REAL GET/POST handlers (review round 6:
# coverage only — the reader+status tests above stay; these join them to the
# handlers) ───────────────────────────────────────────────────────────────────


class _FakeHandler:
    """Minimal BaseHTTPRequestHandler stand-in (same shape as
    tests/test_issue7738_stale_session_overwrite.py::_FakeHandler)."""

    def __init__(self, body: bytes = b"{}"):
        import io

        self.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
        }
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):  # noqa: D401 — protocol stub
        self.status = status

    def send_header(self, key, value):  # noqa: D401 — protocol stub
        pass

    def end_headers(self):  # noqa: D401 — protocol stub
        pass


def _composed_handler_env(tmp_path, monkeypatch, *, ambient, profile_runtime):
    """Wire the real /api/reasoning handlers to a real isolated session store,
    a real ambient YAML (``ambient``) and a named 'review' profile home with a
    real YAML (``profile_runtime``). Session lookup/permissions and profile
    writes are adapters; status/coercion/labels and the profile reader are the
    production chain. Returns (routes, captured, session_dir, sid, raw_persisted)."""
    import json as _json
    from types import SimpleNamespace  # noqa: F401 — documents parsed shape

    import api.models as models
    import api.profiles as profiles
    import api.routes as routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file)
    models.SESSIONS.clear()

    # Ambient (default profile) config through the real YAML loader.
    _runtime_yaml(monkeypatch, tmp_path, ambient)
    # api.routes binds config helpers BY VALUE at import time; if the module
    # was first imported while the autouse stub was installed, routes holds the
    # stub forever. Re-point the handler's runtime reader at the real one so
    # the test exercises production composition, not a harness binding.
    monkeypatch.setattr(routes, "openai_runtime_api_mode", _REAL_RUNTIME_READER)

    # Named session-profile home with its own real config.yaml. The profile →
    # home mapping is an ADAPTER (the profiles registry is process-global), but
    # "default"/None MUST resolve to the real default home, not the named one:
    # _profile_home_config_source() calls get_hermes_home_for_profile("default")
    # to compute root_home and short-circuits to ambient when target == root_home.
    real_home_for_profile = profiles.get_hermes_home_for_profile
    real_default_home = real_home_for_profile("default")
    named = tmp_path / "profile-review"
    named.mkdir()
    (named / "config.yaml").write_text(
        f"model:\n  openai_runtime: {profile_runtime}\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda profile=None: (
            named if profile == "review" else real_default_home
        ),
    )

    sid = "ultra-runtime-session"
    models.Session(
        session_id=sid, title="t", profile="review", reasoning_effort="ultra"
    ).save()

    captured: dict = {}

    def _j(handler, payload, status=200, extra_headers=None, **_kw):
        captured["payload"] = payload
        captured["status"] = status
        return True

    monkeypatch.setattr(routes, "j", _j)
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    # Adapters: response writer, CSRF, per-request body, profile-default
    # write side effect, and cross-profile visibility (the handler-side
    # visibility guard runs before routing).
    monkeypatch.setattr(
        routes, "write_reasoning_effort", lambda effort, *_a, **_k: effort
    )
    monkeypatch.setattr(
        routes, "_session_visible_to_active_profile", lambda *_a, **_k: True
    )

    def raw_persisted():
        return _json.loads(
            (session_dir / f"{sid}.json").read_text(encoding="utf-8")
        )["reasoning_effort"]

    return routes, captured, session_dir, sid, raw_persisted


def _drive_get(routes, sid):
    from types import SimpleNamespace

    handler = _FakeHandler()
    ok = routes.handle_get(
        handler,
        SimpleNamespace(
            path="/api/reasoning",
            query=f"model=gpt-5.6&provider=openai-codex&session_id={sid}",
        ),
    )
    assert ok is True
    return handler


def _drive_post(routes, sid, monkeypatch):
    from types import SimpleNamespace

    body = {
        "effort": "ultra",
        "session_id": sid,
        "model": "gpt-5.6",
        "provider": "openai-codex",
    }
    monkeypatch.setattr(routes, "read_body", lambda handler: body)
    handler = _FakeHandler(b"{}")
    ok = routes.handle_post(handler, SimpleNamespace(path="/api/reasoning"))
    assert ok is True
    return handler


class TestComposedHandlerRegressions:
    """The reviewer's composition: real isolated YAML, the real GET/POST
    handlers, real status/coercion/labels and the profile-runtime reader —
    asserting effective effort, display label and the RAW persisted choice."""


    def test_get_named_optout_over_ambient_app_server(self, tmp_path, monkeypatch):
        routes, captured, _dir, sid, raw = _composed_handler_env(
            tmp_path, monkeypatch, ambient="codex_app_server", profile_runtime="auto"
        )
        _drive_get(routes, sid)
        assert captured["status"] == 200, captured
        assert captured["payload"]["reasoning_effort"] == "max"
        assert captured["payload"]["reasoning_effort_display"] == (
            "ultra (sends max on this route)"
        )
        assert raw() == "ultra", "the raw choice must stay persisted, not be rewritten"

    def test_post_named_optout_over_ambient_app_server(self, tmp_path, monkeypatch):
        routes, captured, _dir, sid, raw = _composed_handler_env(
            tmp_path, monkeypatch, ambient="codex_app_server", profile_runtime="auto"
        )
        _drive_post(routes, sid, monkeypatch)
        assert captured["status"] == 200, captured
        assert captured["payload"]["reasoning_effort"] == "max"
        assert captured["payload"]["reasoning_effort_display"] == (
            "ultra (sends max on this route)"
        )
        assert raw() == "ultra"

    def test_get_named_optin_over_ambient_optout(self, tmp_path, monkeypatch):
        routes, captured, _dir, sid, raw = _composed_handler_env(
            tmp_path, monkeypatch, ambient=None, profile_runtime="codex_app_server"
        )
        _drive_get(routes, sid)
        assert captured["status"] == 200, captured
        assert captured["payload"]["reasoning_effort"] == "ultra"
        assert captured["payload"]["reasoning_effort_display"] == "ultra"
        assert raw() == "ultra"

    def test_post_named_optin_over_ambient_optout(self, tmp_path, monkeypatch):
        routes, captured, _dir, sid, raw = _composed_handler_env(
            tmp_path, monkeypatch, ambient=None, profile_runtime="codex_app_server"
        )
        _drive_post(routes, sid, monkeypatch)
        assert captured["status"] == 200, captured
        assert captured["payload"]["reasoning_effort"] == "ultra"
        assert captured["payload"]["reasoning_effort_display"] == "ultra"
        assert raw() == "ultra"


# ── Every rung ABOVE max gets the route check (review round 7, must-fix 1) ───

_FUTURE_LADDER = _LADDER + ("hyper",)


class TestFutureRungAboveMax:
    """A level a future Agent adds above `ultra` must never be forwarded
    verbatim just because the WebUI's sourced ladder offers it: the Agent's
    clamp_effort passes levels outside its own EFFORT_LADDER through
    UNCHANGED, so the route rule — not the literal name 'ultra' — gates it."""

    @staticmethod
    def _env(monkeypatch):
        monkeypatch.setattr(cfg, "VALID_REASONING_EFFORTS", _FUTURE_LADDER)
        # The future capability catalog KNOWS hyper (models.dev would advertise
        # it) — so without the route check it would be forwarded verbatim.
        monkeypatch.setattr(
            cfg, "resolve_model_reasoning_efforts",
            lambda *a, **k: _FUTURE_LADDER,
        )

    def test_hyper_never_reaches_non_codex_wires(self, monkeypatch):
        self._env(monkeypatch)
        for model, provider in (
            ("deepseek-chat", "deepseek"),
            ("llama-4-scout", "openrouter"),
            ("some-model", "customprovider"),
        ):
            assert cfg.coerce_reasoning_effort_for_model(
                "hyper", model, provider_id=provider
            ) == "max", f"hyper must step down to max on {provider}"

    def test_hyper_steps_to_the_highest_accepted_rung(self, monkeypatch):
        # Codex app-server's rule stops at ultra => hyper lands on ultra, not max.
        self._env(monkeypatch)
        assert cfg.coerce_reasoning_effort_for_model(
            "hyper", "gpt-5.6", provider_id="openai-codex",
            api_mode="codex_app_server",
        ) == "ultra"

    def test_hyper_still_runs_the_provider_ceilings_after_stepping(self, monkeypatch):
        self._env(monkeypatch)
        assert cfg.coerce_reasoning_effort_for_model(
            "hyper", "gemini-4-argon", provider_id="gemini"
        ) == "xhigh"

    def test_hyper_on_pre_clamp_agent_sends_max(self, monkeypatch):
        self._env(monkeypatch)
        monkeypatch.setattr(cfg, "_agent_route_supported_efforts", lambda: None)
        assert cfg.coerce_reasoning_effort_for_model(
            "hyper", "gpt-5.6", provider_id="openai-codex",
            api_mode="codex_app_server",
        ) == "max"

    def test_hyper_label_names_the_rung_actually_sent(self, monkeypatch):
        import unittest.mock as mock

        self._env(monkeypatch)
        with mock.patch.object(cfg, "_load_yaml_config_file", lambda path: {}):
            st = cfg.get_reasoning_status(
                model_id="deepseek-chat", provider_id="deepseek",
                effort_override="hyper",
            )
        assert st["reasoning_effort"] == "max"
        assert st["reasoning_effort_display"] == "hyper (sends max on this route)"


def test_legacy_codex_id_sends_and_labels_xhigh(monkeypatch):
    # Round-7 should-fix 4: codex-mini-latest runs the CODEX legacy ladder,
    # which tops out at xhigh (no max, no ultra — even under the app-server
    # runtime, which only adds ultra where the model reaches max). Stored
    # 'ultra' must therefore step all the way down to xhigh and the label
    # must name xhigh, not the max the old ultra-only rewrite claimed.
    import unittest.mock as mock

    monkeypatch.setattr(
        cfg, "resolve_model_reasoning_efforts",
        lambda *a, **k: ("minimal", "low", "medium", "high", "xhigh", "max"),
    )
    with mock.patch.object(cfg, "_load_yaml_config_file", lambda path: {}):
        st = cfg.get_reasoning_status(
            model_id="codex-mini-latest", provider_id="openai-codex",
            effort_override="ultra", api_mode="codex_app_server",
        )
    assert st["reasoning_effort"] == "xhigh"
    assert st["reasoning_effort_display"] == "ultra (sends xhigh on this route)"


# ── Gateway worker follows the session profile (review round 7, must-fix 2) ──

def test_gateway_request_helper_threads_the_session_runtime(monkeypatch):
    from api.gateway_chat import _gateway_reasoning_effort_for_request

    monkeypatch.setattr(
        cfg, "resolve_model_reasoning_efforts",
        lambda *a, **k: ("minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
    )
    # Opted-out session (codex_responses): ultra is not a wire level there.
    assert _gateway_reasoning_effort_for_request(
        {}, session_effort="ultra", model="gpt-5.6",
        model_provider="openai-codex", api_mode="codex_responses",
    ) == "max"
    # Opted-in session: ultra survives on the app-server runtime.
    assert _gateway_reasoning_effort_for_request(
        {}, session_effort="ultra", model="gpt-5.6",
        model_provider="openai-codex", api_mode="codex_app_server",
    ) == "ultra"


def test_gateway_profile_runtime_reads_the_named_profile_yaml(tmp_path, monkeypatch):
    from api.gateway_chat import _gateway_profile_runtime

    # Ambient (default profile) opts OUT; the named profile opts IN through
    # its own real YAML — the worker answer must follow the profile file.
    _runtime_yaml(monkeypatch, tmp_path, None)
    optin = tmp_path / "profile-gw-optin"
    optin.mkdir()
    (optin / "config.yaml").write_text(
        "model:\n  openai_runtime: codex_app_server\n", encoding="utf-8"
    )
    assert _gateway_profile_runtime(optin) == "codex_app_server"
    # A missing YAML is still a CONCRETE consultation (round-5 semantics):
    # codex_responses, not an ambient re-read of the default profile.
    assert _gateway_profile_runtime(tmp_path / "does-not-exist") == "codex_responses"


# ── Honest labels (chip / toast / /reasoning) ────────────────────────────────

def test_honest_label_annotates_above_wire_levels():
    # Stored ultra + route that sends max => the CLI-parity annotation
    # (from the Agent's effort_display_label, with a literal fallback).
    assert cfg._effort_honest_label("ultra", "max", "qwen3.8-flash", "deepseek") == (
        "ultra (sends max on this route)"
    )
    # Stored == effective: plain level, no annotation.
    assert cfg._effort_honest_label("max", "max", "gpt-5.6", "openai") == "max"
    assert cfg._effort_honest_label("", "", None, None) == ""
    # Levels at or below the wire ceiling never annotate.
    assert cfg._effort_honest_label("high", "xhigh", "m", "p") == "xhigh"


def test_label_names_the_level_actually_sent_under_webui_ceilings():
    # Review round-4 should-fix: the Agent's label only knows the transport's
    # own clamp (Codex vs OpenAI-compat => 'max'); the WebUI's per-model
    # ceilings can clamp LOWER (Gemini xhigh). The annotation must name the
    # level we really send — "ultra (sends xhigh on this route)" — never a
    # bare/closer level the wire also does not receive.
    assert cfg._effort_honest_label("ultra", "xhigh", "gemini-4-argon", "gemini") == (
        "ultra (sends xhigh on this route)"
    )
    # End-to-end through the status: capped model stores ultra => effective is
    # xhigh and the display agrees (no "Ultra High" claim, no bare xhigh).
    import unittest.mock as mock

    with mock.patch.object(cfg, "_load_yaml_config_file", lambda path: {}):
        st = cfg.get_reasoning_status(
            model_id="gemini-4-argon", provider_id="gemini",
            effort_override="ultra",
        )
    assert st["reasoning_effort"] == "xhigh"
    assert st["reasoning_effort_display"] == "ultra (sends xhigh on this route)"


# ── Coercion agrees with the widened ladder ─────────────────────────────────

def test_ultra_degrades_on_capped_models():
    # Ceiling excludes ultra (and max) => descend to xhigh, never escalate.
    for model, provider in (
        ("gemini-4-argon", "gemini"),
        ("gpt-5.5", "openai"),
        ("claude-sonnet-4-5", "anthropic"),
    ):
        assert cfg.coerce_reasoning_effort_for_model(
            "ultra", model_id=model, provider_id=provider
        ) == "xhigh", f"ultra must degrade to the capped ceiling on {model}"


def test_ultra_degrades_on_unknown_provider():
    # #3505 default-deny applies to everything above the universally-safe
    # xhigh ceiling — the wire-normalised 'max' included.
    assert cfg.coerce_reasoning_effort_for_model(
        "ultra", model_id="some-unknown-model", provider_id="customprovider"
    ) == "xhigh"


def test_low_level_survives_on_a_descending_ladder_host(monkeypatch):
    # The bug the descending tuple would cause: index math treating WEAK
    # levels as "above xhigh". A stored 'low' must stay 'low'.
    monkeypatch.setattr(
        cfg, "VALID_REASONING_EFFORTS", cfg._rank_valid_efforts(_AGENT_3_28_DESCENDING)
    )
    assert cfg.coerce_reasoning_effort_for_model(
        "low", model_id="gpt-5.5", provider_id="openai-codex"
    ) == "low"


# ── Persistence accepts the agent ceiling ────────────────────────────────────

def test_set_reasoning_effort_accepts_ultra():
    import unittest.mock as mock

    saved = {}

    def fake_save(path, data):
        saved.update(data.get("agent", {}) or {})

    with mock.patch("api.config._save_yaml_config_file", side_effect=fake_save), \
         mock.patch("api.config.reload_config"):
        cfg.set_reasoning_effort("ultra")
    assert saved.get("reasoning_effort") == "ultra"


def test_set_reasoning_effort_still_rejects_garbage():
    import unittest.mock as mock

    with mock.patch("api.config._save_yaml_config_file"), \
         mock.patch("api.config.reload_config"):
        with pytest.raises(ValueError):
            cfg.set_reasoning_effort("banana")
