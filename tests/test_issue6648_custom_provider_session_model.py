"""Regression tests for issue #6648.

Sessions created with a custom OpenAI-compatible provider persist the model as
``@custom:<slug>:<model>`` (or ``custom/<model>``). The Hermes runtime does not
recognize a provider named ``custom:<something>`` — it only knows the canonical
provider slug (e.g. ``sensenova-primary``). Without normalization every request
fails with "Unknown provider 'custom:...'" and exhausts the full fallback chain,
making WebUI responses 10-30x slower than Hermes Desktop.

The fix normalizes to ``<canonical-slug>/<bare_model>`` (single API call) when
the qualifier matches a NAMED ``custom_providers`` entry, and preserves the
current behavior otherwise.
"""

import io
import json


def test_custom_qualified_model_normalized_to_canonical_slug(monkeypatch):
    """@custom:sensenova-primary:deepseek-v4-flash -> sensenova-primary/deepseek-v4-flash."""
    import api.config as config
    import api.routes as routes

    def _fake_named_slug(provider, config_obj=None):
        return "custom:sensenova-primary" if provider == "custom:sensenova-primary" else ""

    monkeypatch.setattr(config, "_named_custom_provider_slug_for_provider", _fake_named_slug)

    profile_cfg = {"custom_providers": [{"name": "Sensenova Primary"}]}
    effective, provider, changed = routes._resolve_compatible_session_model_state(
        "@custom:sensenova-primary:deepseek-v4-flash",
        None,
        profile_config=profile_cfg,
    )

    assert changed is True
    assert effective == "sensenova-primary/deepseek-v4-flash", effective
    assert provider == "sensenova-primary", provider


def test_custom_qualified_model_routes_in_single_path(monkeypatch):
    """Provider must come back canonical (no 'custom:' prefix) so the runtime resolves it."""
    import api.config as config
    import api.routes as routes

    monkeypatch.setattr(
        config,
        "_named_custom_provider_slug_for_provider",
        lambda provider, config_obj=None: "custom:sensenova-primary"
        if provider == "custom:sensenova-primary"
        else "",
    )

    effective, provider, changed = routes._resolve_compatible_session_model_state(
        "@custom:sensenova-primary:deepseek-v4-flash",
        "custom:sensenova-primary",
        profile_config={"custom_providers": [{"name": "Sensenova Primary"}]},
    )

    assert changed is True
    assert provider == "sensenova-primary"
    assert effective == "sensenova-primary/deepseek-v4-flash"
    assert "custom:" not in provider
    assert "custom:" not in effective.split("/", 1)[0]


def test_slash_custom_form_normalized(monkeypatch):
    """Persisted 'custom/<model>' form with a custom:<slug> provider is also normalized."""
    import api.config as config
    import api.routes as routes

    monkeypatch.setattr(
        config,
        "_named_custom_provider_slug_for_provider",
        lambda provider, config_obj=None: "custom:sensenova-primary"
        if provider == "custom:sensenova-primary"
        else "",
    )

    effective, provider, changed = routes._resolve_compatible_session_model_state(
        "custom/deepseek-v4-flash",
        "custom:sensenova-primary",
        profile_config={"custom_providers": [{"name": "Sensenova Primary"}]},
    )

    assert changed is True
    assert effective == "sensenova-primary/deepseek-v4-flash", effective
    assert provider == "sensenova-primary"


def test_unmatched_qualifier_preserved(monkeypatch):
    """A custom: qualifier that matches NO named entry stays untouched (no rerouting)."""
    import api.config as config
    import api.routes as routes

    monkeypatch.setattr(
        config,
        "_named_custom_provider_slug_for_provider",
        lambda provider, config_obj=None: "",
    )

    result = routes._normalize_custom_qualified_session_model(
        "@custom:unknown-slug:gpt-5.4-mini",
        None,
    )
    assert result is None


def test_non_custom_model_untouched(monkeypatch):
    """Regular @provider:model and bare models never hit the custom normalizer path."""
    import api.config as config
    import api.routes as routes

    monkeypatch.setattr(
        config,
        "_named_custom_provider_slug_for_provider",
        lambda provider, config_obj=None: "",
    )

    assert routes._normalize_custom_qualified_session_model("@deepseek:deepseek-v4-pro", None) is None
    assert routes._normalize_custom_qualified_session_model("gpt-5.4", None) is None


def test_provider_name_collisions_never_normalized(monkeypatch):
    """Non-custom qualifiers are never normalized, even when the lookup would
    resolve them to a custom slug (#6718 re-gate, CORE finding 1).

    Regression cases from the review:
      - ``@openai:gpt-5.5`` with a named ``openai`` provider must reach OpenAI
        as the bare ``gpt-5.5``, never ``openai/gpt-5.5``;
      - ``custom/x`` under an ``moa`` provider must not be rerouted to moa.
    """
    import api.config as config
    import api.routes as routes

    # The lookup resolves ANY qualifier to a custom slug — the gate must reject
    # non-``custom:`` qualifiers before the lookup is even consulted.
    monkeypatch.setattr(
        config,
        "_named_custom_provider_slug_for_provider",
        lambda provider, config_obj=None: "custom:" + str(provider).removeprefix("custom:"),
    )

    assert routes._normalize_custom_qualified_session_model("@openai:gpt-5.5", "openai") is None
    assert routes._normalize_custom_qualified_session_model("@moa:ensemble", "moa") is None
    assert routes._normalize_custom_qualified_session_model("custom/x", "moa") is None
    assert routes._normalize_custom_qualified_session_model("custom/x", None) is None


def test_session_profile_config_is_source_of_truth(monkeypatch):
    """The lookup must run against the session's OWN profile config — the
    ``config_obj`` threaded into the helper must be the session profile dict
    (#6718 re-gate, CORE finding 2)."""
    import api.config as config
    import api.routes as routes

    session_cfg = {
        "custom_providers": [
            {"name": "Sensenova Primary", "model": "deepseek-v4-flash"},
        ]
    }
    captured = {}

    def _fake_named_slug(provider, config_obj=None):
        captured["config_obj"] = config_obj
        if config_obj is session_cfg and provider == "custom:sensenova-primary":
            return "custom:sensenova-primary"
        return ""

    monkeypatch.setattr(config, "_named_custom_provider_slug_for_provider", _fake_named_slug)

    effective, provider, changed = routes._resolve_compatible_session_model_state(
        "@custom:sensenova-primary:deepseek-v4-flash",
        None,
        profile_config=session_cfg,
    )

    assert changed is True
    assert captured["config_obj"] is session_cfg
    assert effective == "sensenova-primary/deepseek-v4-flash", effective
    assert provider == "sensenova-primary", provider


def test_slug_missing_from_session_profile_not_leaked(monkeypatch):
    """A slug defined only in another profile (global cfg) must NOT remap a
    session whose own profile no longer defines it (#6718 re-gate, CORE
    finding 2: no global fallback when a profile config was supplied)."""
    import api.config as config
    import api.routes as routes

    # Global config still defines the slug...
    monkeypatch.setattr(
        config,
        "cfg",
        {"custom_providers": [{"name": "Sensenova Primary"}]},
    )

    # ...but the session's own profile does not. The REAL helper must not fall
    # back to the global cfg once a profile config dict was supplied.
    session_cfg = {"custom_providers": []}

    result = routes._normalize_custom_qualified_session_model(
        "@custom:sensenova-primary:deepseek-v4-flash",
        None,
        profile_config=session_cfg,
    )
    assert result is None


def test_configured_qualified_value_drives_normalization():
    """End-to-end through the REAL helper: the canonical slug comes from the
    profile config entry's qualified value (custom_providers), resolved under
    the session's own profile config."""
    import api.routes as routes

    session_cfg = {
        "custom_providers": [
            {"name": "Sensenova Primary", "model": "deepseek-v4-flash"},
        ]
    }

    effective, provider, changed = routes._resolve_compatible_session_model_state(
        "@custom:sensenova-primary:deepseek-v4-flash",
        "custom:sensenova-primary",
        profile_config=session_cfg,
    )

    assert changed is True
    assert effective == "sensenova-primary/deepseek-v4-flash", effective
    assert provider == "sensenova-primary", provider


def test_colon_suffixed_model_id_normalized(monkeypatch):
    """#6718 (greptile-apps[bot]): a model id that itself contains ':' (e.g.
    ``@custom:sensenova-primary:model:free`` where the model is ``model:free``)
    must still normalize. ``_split_provider_qualified_model`` rsplit's on the
    LAST ':', so the naive qualifier ``custom:sensenova-primary:model`` matches
    no configured slug; the normalizer must walk back to the configured prefix
    and re-attach the eaten segments to the model."""
    import api.routes as routes

    session_cfg = {
        "custom_providers": [
            {"name": "Sensenova Primary", "model": "deepseek-v4-flash"},
        ]
    }

    effective, provider, changed = routes._resolve_compatible_session_model_state(
        "@custom:sensenova-primary:model:free",
        None,
        profile_config=session_cfg,
    )
    assert changed is True
    assert effective == "sensenova-primary/model:free", effective
    assert provider == "sensenova-primary", provider


def test_colon_suffixed_model_id_multi_segment(monkeypatch):
    """Multi-colon model id: ``@custom:sensenova-primary:a:b:c`` -> model ``a:b:c``."""
    import api.routes as routes

    session_cfg = {
        "custom_providers": [
            {"name": "Sensenova Primary", "model": "deepseek-v4-flash"},
        ]
    }

    effective, provider, changed = routes._resolve_compatible_session_model_state(
        "@custom:sensenova-primary:a:b:c",
        None,
        profile_config=session_cfg,
    )
    assert changed is True
    assert effective == "sensenova-primary/a:b:c", effective
    assert provider == "sensenova-primary", provider


def test_endpoint_derived_host_port_slug_not_walked_back():
    """Endpoint-derived ``custom:<host>:<port>`` slugs must stay unchanged —
    the colon walk-back must NOT eat ``8080:Qwen3`` into the model (#1776 form)."""
    import api.routes as routes

    session_cfg = {
        "custom_providers": [
            {"name": "Sensenova Primary", "model": "deepseek-v4-flash"},
        ]
    }

    assert (
        routes._normalize_custom_qualified_session_model(
            "@custom:10.8.71.41:8080:Qwen3",
            None,
            profile_config=session_cfg,
        )
        is None
    )


def test_configured_prefix_recovered_before_host_port_classification():
    """#6718 (greptile P1 re-gate): a configured named provider whose slug is a
    shorter prefix of the qualifier must be matched BEFORE the endpoint
    host:port classification.

    ``@custom:localhost:8080:Qwen3`` under a configured provider 'localhost'
    (slug ``custom:localhost``) rsplit's into qualifier ``custom:localhost:8080``
    and model ``Qwen3`` — the qualifier looks host:port-shaped, but the
    CONFIGURED prefix ``custom:localhost`` wins: the colon-bearing model
    ``8080:Qwen3`` is recovered and the session normalizes instead of staying
    on the unknown-provider fallback path. Genuine endpoint-derived slugs
    (``custom:<host>:<port>`` with NO configured prefix) stay unchanged
    (see test_endpoint_derived_host_port_slug_not_walked_back)."""
    import api.routes as routes

    session_cfg = {
        "custom_providers": [
            {"name": "localhost", "model": "Qwen3"},
        ]
    }

    assert (
        routes._normalize_custom_qualified_session_model(
            "@custom:localhost:8080:Qwen3",
            None,
            profile_config=session_cfg,
        )
        == ("localhost/8080:Qwen3", "localhost")
    )

    # The resolver entry point agrees and returns before any catalog call.
    effective, provider, changed = routes._resolve_compatible_session_model_state(
        "@custom:localhost:8080:Qwen3",
        None,
        profile_config=session_cfg,
    )
    assert changed is True
    assert effective == "localhost/8080:Qwen3", effective
    assert provider == "localhost", provider


def test_profile_config_none_fails_closed_against_global(monkeypatch):
    """A slug configured only in the module-global config must NOT normalize
    when ``profile_config=None`` — the ``None`` state is reachable from
    /api/session/new and chat/start when the profile YAML is absent/unreadable,
    and the helper chain must fail closed instead of falling back to
    ``api.config.cfg`` (#6718 re-gate, CORE 2 follow-up).

    The REAL helper is used end-to-end (no slug-lookup monkeypatch): the global
    config defines the slug, so any global fallback would resolve it.
    """
    import api.config as config
    import api.routes as routes

    # Only the module-global config defines the slug.
    monkeypatch.setattr(
        config,
        "cfg",
        {"custom_providers": [{"name": "Sensenova Primary"}]},
    )
    # Keep the resolver out of the real (network-backed) catalog — the
    # assertion here is about fail-closed normalization, not catalog resolution.
    monkeypatch.setattr(
        routes,
        "get_available_models",
        lambda *a, **k: {"default_model": "gpt-5.4"},
    )

    # Direct helper call with profile_config=None → fail closed.
    assert (
        routes._normalize_custom_qualified_session_model(
            "@custom:sensenova-primary:deepseek-v4-flash",
            None,
            profile_config=None,
        )
        is None
    )

    # The resolver entry point without a profile config also fails closed.
    effective, provider, changed = routes._resolve_compatible_session_model_state(
        "@custom:sensenova-primary:deepseek-v4-flash",
        None,
        profile_config=None,
    )
    assert changed is False
    assert effective == "@custom:sensenova-primary:deepseek-v4-flash", effective


def test_session_new_request_threads_profile_config(monkeypatch):
    """``_session_model_state_from_request`` (the /api/session/new and
    /api/session/update entry point) must thread the session profile config
    into the normalizer — the lookup sees the session's own config dict
    (#6718 re-gate, CORE 2 follow-up)."""
    import api.config as config
    import api.routes as routes

    session_cfg = {
        "custom_providers": [
            {"name": "Sensenova Primary", "model": "deepseek-v4-flash"},
        ]
    }
    captured = {}

    def _fake_named_slug(provider, config_obj=None):
        captured["config_obj"] = config_obj
        if config_obj is session_cfg and provider == "custom:sensenova-primary":
            return "custom:sensenova-primary"
        return ""

    monkeypatch.setattr(config, "_named_custom_provider_slug_for_provider", _fake_named_slug)

    model, provider = routes._session_model_state_from_request(
        "@custom:sensenova-primary:deepseek-v4-flash",
        None,
        profile_config=session_cfg,
    )

    assert captured.get("config_obj") is session_cfg
    assert model == "sensenova-primary/deepseek-v4-flash", model
    assert provider == "sensenova-primary", provider


def test_session_new_request_e2e_profile_config():
    """End-to-end through the REAL helper: a named-custom model on /api/session/new
    normalizes to the canonical slug under the session's own profile config."""
    import api.routes as routes

    session_cfg = {
        "custom_providers": [
            {"name": "Sensenova Primary", "model": "deepseek-v4-flash"},
        ]
    }

    model, provider = routes._session_model_state_from_request(
        "@custom:sensenova-primary:deepseek-v4-flash",
        None,
        profile_config=session_cfg,
    )

    assert model == "sensenova-primary/deepseek-v4-flash", model
    assert provider == "sensenova-primary", provider


# ── #6718 review 3 regressions ────────────────────────────────────────────
# (CORE) /api/session/new must resolve ONE authoritative session profile
# identity and use it for BOTH the profile-config load and new_session().
# (SILENT) invalid persisted session.profile names must fail closed instead
# of loading the root config.


class _RoutePostHandler:
    """Minimal fake handler for driving ``routes.handle_post`` in-process."""

    def __init__(self, body: dict):
        raw = json.dumps(body).encode("utf-8")
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.client_address = ("127.0.0.1", 12345)
        self.status = None
        self.response_headers = {}

    def send_response(self, code: int):
        self.status = code

    def send_header(self, key: str, value: str):
        self.response_headers[key] = value

    def end_headers(self):
        pass

    def log_message(self, *_args, **_kwargs):
        pass

    def payload(self) -> dict:
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def test_session_new_omitted_profile_uses_active_profile_config(monkeypatch, tmp_path):
    """#6718 review 3 (CORE): POST /api/session/new with ``body.profile``
    omitted must resolve the session model against the ACTIVE profile's config
    — not the default/root config.

    Previously the config lookup loaded the root config (profile=None →
    default home) while ``new_session()`` stamped the session with the active
    NAMED profile: two different identities in one request, so a root-only
    ``@custom:other-profile-only:...`` slug remapped inside another profile's
    session.
    """
    from urllib.parse import urlparse

    import api.profiles as profiles_mod
    import api.routes as routes_mod

    root_home = tmp_path / "root"
    work_home = tmp_path / "profiles" / "work"
    root_home.mkdir(parents=True)
    work_home.mkdir(parents=True)
    (root_home / "config.yaml").write_text(
        "custom_providers:\n  - name: Other Profile Only\n    model: gpt-5.5\n"
    )
    (work_home / "config.yaml").write_text(
        "custom_providers:\n  - name: Sensenova Primary\n    model: deepseek-v4-flash\n"
    )

    # The named profile home resolves for 'work', the root home for anything
    # else (None/empty/root aliases → root) — exactly the resolver behavior
    # the review reproduced the leak through.
    monkeypatch.setattr(
        profiles_mod,
        "get_hermes_home_for_profile",
        lambda name: root_home if not name or str(name).strip().lower() in ("default", "root") else work_home,
    )
    # The handler resolves the omitted-profile identity through
    # api.profiles.get_active_profile_name — the SAME function new_session()
    # falls back to (one authoritative identity).
    monkeypatch.setattr(profiles_mod, "get_active_profile_name", lambda: "work")
    monkeypatch.setattr(routes_mod, "_get_active_profile_name", lambda: "work")
    # Keep the post-normalization catalog resolution off the network.
    monkeypatch.setattr(
        routes_mod,
        "get_available_models",
        lambda *a, **k: {"default_model": "gpt-5.4"},
    )

    handler = _RoutePostHandler({"model": "@custom:other-profile-only:gpt-5.5"})
    routes_mod.handle_post(handler, urlparse("/api/session/new"))

    assert handler.status == 200, handler.payload()
    session = handler.payload()["session"]
    assert session["profile"] == "work", session["profile"]
    # The root-only slug must NOT be applied inside the 'work' profile's
    # session: 'work' config has no matching named entry, so the persisted
    # form is kept (fail closed) instead of being remapped to the root slug.
    assert session["model"] == "@custom:other-profile-only:gpt-5.5", session["model"]
    assert session["model"] != "other-profile-only/gpt-5.5", session["model"]


def test_session_new_omitted_profile_matches_active_profile_slug(monkeypatch, tmp_path):
    """#6718 review 3 (CORE) positive control: with body.profile omitted, a
    model whose qualifier IS configured in the active named profile's config
    still normalizes — proving the active identity drives the config load."""
    from urllib.parse import urlparse

    import api.profiles as profiles_mod
    import api.routes as routes_mod

    root_home = tmp_path / "root"
    work_home = tmp_path / "profiles" / "work"
    root_home.mkdir(parents=True)
    work_home.mkdir(parents=True)
    # The slug lives ONLY in the active profile's config, not the root config.
    (root_home / "config.yaml").write_text("custom_providers: []\n")
    (work_home / "config.yaml").write_text(
        "custom_providers:\n  - name: Sensenova Primary\n    model: deepseek-v4-flash\n"
    )

    monkeypatch.setattr(
        profiles_mod,
        "get_hermes_home_for_profile",
        lambda name: root_home if not name or str(name).strip().lower() in ("default", "root") else work_home,
    )
    monkeypatch.setattr(profiles_mod, "get_active_profile_name", lambda: "work")
    monkeypatch.setattr(routes_mod, "_get_active_profile_name", lambda: "work")
    monkeypatch.setattr(
        routes_mod,
        "get_available_models",
        lambda *a, **k: {"default_model": "gpt-5.4"},
    )

    handler = _RoutePostHandler({"model": "@custom:sensenova-primary:deepseek-v4-flash"})
    routes_mod.handle_post(handler, urlparse("/api/session/new"))

    assert handler.status == 200, handler.payload()
    session = handler.payload()["session"]
    assert session["profile"] == "work", session["profile"]
    assert session["model"] == "sensenova-primary/deepseek-v4-flash", session["model"]


def test_load_profile_config_dict_fails_closed_on_invalid_persisted_profile(monkeypatch, tmp_path):
    """#6718 review 3 (SILENT): a session whose PERSISTED profile name is
    invalid must NOT load the root config.

    ``get_hermes_home_for_profile`` resolves names that do not match the
    profile-id shape to the ROOT home, so pre-fix an invalid stored
    ``session.profile`` loaded the root config — letting a root-only
    custom-provider slug remap inside that session. The identity is validated
    before resolution; anything that is neither a root alias nor a valid named
    profile yields None (fail closed), never the root fallback.
    """
    import api.profiles as profiles_mod
    import api.routes as routes_mod

    root_home = tmp_path / "root"
    root_home.mkdir(parents=True)
    root_cfg_text = (
        "custom_providers:\n  - name: Root Only\n    model: gpt-5.5\n"
    )
    (root_home / "config.yaml").write_text(root_cfg_text)

    # ANY name resolves to the root home — the resolver fallback the guard
    # must defeat: pre-fix this made the root config load for the invalid
    # stored profile name.
    monkeypatch.setattr(profiles_mod, "get_hermes_home_for_profile", lambda name: root_home)

    class _InvalidProfileSession:
        profile = "../../evil"

    assert routes_mod._load_profile_config_dict(_InvalidProfileSession()) is None

    class _TraversalProfileSession:
        profile = "..%2f..%2fetc"

    assert routes_mod._load_profile_config_dict(_TraversalProfileSession()) is None

    # A valid named profile still loads its own config (positive control)...
    class _NamedProfileSession:
        profile = "work"

    assert routes_mod._load_profile_config_dict(_NamedProfileSession()) == {
        "custom_providers": [{"name": "Root Only", "model": "gpt-5.5"}]
    }

    # ...and a root alias is still the root profile's OWN config (legitimate,
    # not a cross-profile leak).
    class _RootAliasSession:
        profile = "default"

    assert routes_mod._load_profile_config_dict(_RootAliasSession()) == {
        "custom_providers": [{"name": "Root Only", "model": "gpt-5.5"}]
    }


def test_read_profile_model_config_fails_closed_on_invalid_persisted_profile(monkeypatch, tmp_path):
    """#6718 review 3 (SILENT): the display/start config loader must ALSO fail
    closed on an invalid persisted ``session.profile``.

    ``_read_profile_model_config`` feeds its ``profile_config`` result into the
    custom-provider slug normalizer on the GET /api/session display path,
    chat/start wakeup and goal paths. ``get_hermes_home_for_profile`` maps
    invalid names to the ROOT home, so pre-fix an invalid stored profile name
    loaded the ROOT config — letting a root-only custom-provider slug remap
    inside that session. The identity is validated before resolution;
    anything that is neither a root alias nor a valid named profile yields
    (None, None, None) (fail closed), never the root fallback.
    """
    import api.profiles as profiles_mod
    import api.routes as routes_mod

    root_home = tmp_path / "root"
    root_home.mkdir(parents=True)
    (root_home / "config.yaml").write_text(
        "custom_providers:\n  - name: Root Only\n    model: gpt-5.5\n"
    )

    # ANY name resolves to the root home — the resolver fallback the guard
    # must defeat: pre-fix this made the root config load for the invalid
    # stored profile name.
    monkeypatch.setattr(profiles_mod, "get_hermes_home_for_profile", lambda name: root_home)

    class _InvalidProfileSession:
        profile = "../../evil"

    assert routes_mod._read_profile_model_config(_InvalidProfileSession(), None) == (None, None, None)

    class _TraversalProfileSession:
        profile = "..%2f..%2fetc"

    assert routes_mod._read_profile_model_config(_TraversalProfileSession(), None) == (None, None, None)

    # A valid named profile still loads its own config (positive control)...
    class _NamedProfileSession:
        profile = "work"

    _pp, _pd, _pcfg = routes_mod._read_profile_model_config(_NamedProfileSession(), None)
    assert _pcfg == {"custom_providers": [{"name": "Root Only", "model": "gpt-5.5"}]}

    # ...and a root alias is still the root profile's OWN config (legitimate,
    # not a cross-profile leak).
    class _RootAliasSession:
        profile = "default"

    _pp, _pd, _pcfg = routes_mod._read_profile_model_config(_RootAliasSession(), None)
    assert _pcfg == {"custom_providers": [{"name": "Root Only", "model": "gpt-5.5"}]}
