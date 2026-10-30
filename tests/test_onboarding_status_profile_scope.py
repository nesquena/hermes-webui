"""Request-profile credential ownership through the onboarding status route."""

import io
import json
import os
import sys
from contextvars import ContextVar
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlparse

import pytest
import yaml

from api import config, onboarding, profiles, routes


KEY = "ONBOARDING_SCOPE_TEST_KEY"
PROVIDER = "custom:scope-local"


@pytest.fixture(params=["key_env", "api_key_env", "api_key_envref"])
def status_profiles(request, monkeypatch, tmp_path):
    provider_record = {"base_url": "http://127.0.0.1:8000/v1"}
    if request.param == "api_key_envref":
        # ``api_key: ${VAR}`` must expand in the request profile's env scope,
        # never against the process-pinned get_config() cache.
        provider_record["api_key"] = "${" + KEY + "}"
    else:
        provider_record[request.param] = KEY
    return _setup_status_profiles(monkeypatch, tmp_path, provider_record)


@pytest.fixture
def shape_profiles(monkeypatch, tmp_path):
    """Same server setup; each test writes its own record shape."""
    return _setup_status_profiles(
        monkeypatch, tmp_path, {"base_url": "http://127.0.0.1:8000/v1", "key_env": KEY}
    )


def _setup_status_profiles(monkeypatch, tmp_path, provider_record):
    root = tmp_path / "home"
    homes = {
        "default": root,
        "work": root / "profiles/work",
        "empty": root / "profiles/empty",
    }
    for home in homes.values():
        home.mkdir(parents=True, exist_ok=True)
        (home / ".env").write_text("", encoding="utf-8")
        (home / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "model": {"provider": PROVIDER, "default": "local-model"},
                    "providers": {
                        "scope-local": provider_record,
                    },
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_BASE_HOME", str(root))
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_ISOLATED_PROFILE", raising=False)
    monkeypatch.delenv(KEY, raising=False)
    monkeypatch.delenv("CUSTOM_SCOPE_LOCAL_API_KEY", raising=False)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setattr(profiles, "_active_profile", "default")
    monkeypatch.setattr(profiles, "_root_profile_name_cache", {"default"})
    monkeypatch.setattr(profiles, "_root_profile_name_cache_loaded", True)
    cache = {}
    monkeypatch.setattr(config, "_cfg_cache", cache)
    monkeypatch.setattr(config, "cfg", cache)
    monkeypatch.setattr(config, "_cfg_path", None)
    monkeypatch.setattr(config, "_cfg_mtime", 0.0)
    monkeypatch.setattr(config, "_cfg_fingerprint", None)

    # Keep the real config loader, status composition, and named resolver.
    # These peripheral services are independent of credential ownership.
    monkeypatch.setattr(onboarding, "_HERMES_FOUND", True)
    monkeypatch.setattr(onboarding, "verify_hermes_imports", lambda: (True, [], {}))
    monkeypatch.setattr(onboarding, "load_workspaces", lambda: [])
    monkeypatch.setattr(onboarding, "get_last_workspace", lambda: None)
    monkeypatch.setattr(onboarding, "is_auth_enabled", lambda: False)
    monkeypatch.setattr(config, "_custom_record_pool_runtime", lambda *_: None)

    observed = SimpleNamespace(homes=homes, keys=[], stages=[])

    def observe(stage):
        observed.stages.append((stage, config._thread_local_env_value(KEY)))
        # A transient process-env mirror would also violate this read-only path.
        assert os.environ.get(KEY) == observed.ambient
        assert os.environ["HERMES_HOME"] == str(root)

    def settings():
        observe("settings")  # First step, before get_config() / expansion.
        return {"onboarding_completed": True}

    def models():
        observe("models")  # Later composition stays in the same scope too.
        return {}

    resolve = onboarding.resolve_custom_provider_bundle

    def observed_resolve(*args, **kwargs):
        bundle = resolve(*args, **kwargs)
        observed.keys.append(bundle["api_key"])
        observe("resolver")
        return bundle

    monkeypatch.setattr(onboarding, "load_settings", settings)
    monkeypatch.setattr(onboarding, "get_available_models", models)
    monkeypatch.setattr(onboarding, "resolve_custom_provider_bundle", observed_resolve)
    return observed


def request_status(observed, profile, *, broken_response=False):
    """Use the server's request identity and real dispatch/JSON response path."""
    previous_profile = getattr(profiles._tls, "profile", None)
    previous_env = dict(getattr(config._thread_ctx, "env", {}))
    previous_block = getattr(config._thread_ctx, "block_process_env_fallback", False)
    observed.ambient = os.environ.get(KEY)
    handler = SimpleNamespace(
        headers={},
        wfile=io.BytesIO(),
        path="/api/onboarding/status",
        send_response=Mock(),
        send_header=Mock(),
        end_headers=Mock(),
    )
    if broken_response:
        handler.send_response.side_effect = RuntimeError("response failed")
    # server.py:do_GET establishes this identity from the profile cookie.
    profiles.set_request_profile(profile)
    try:
        routes.handle_get(handler, urlparse(handler.path))
        handler.send_response.assert_called_once_with(200)
        return json.loads(handler.wfile.getvalue())
    finally:
        try:
            assert dict(getattr(config._thread_ctx, "env", {})) == previous_env
            assert (
                getattr(config._thread_ctx, "block_process_env_fallback", False)
                == previous_block
            )
            assert os.environ.get(KEY) == observed.ambient
            assert os.environ["HERMES_HOME"] == str(observed.homes["default"])
        finally:
            if previous_profile is None:
                profiles.clear_request_profile()
            else:
                profiles.set_request_profile(previous_profile)


@pytest.mark.parametrize("placement", ["profile_only", "ambient_only", "conflicting"])
def test_status_uses_requested_profile_credentials(
    status_profiles, monkeypatch, placement
):
    if placement != "ambient_only":
        (status_profiles.homes["work"] / ".env").write_text(
            f"{KEY}=profile-placeholder\n",
            encoding="utf-8",
        )
    if placement != "profile_only":
        monkeypatch.setenv(KEY, "ambient-placeholder")
    expected = None if placement == "ambient_only" else "profile-placeholder"

    payload = request_status(status_profiles, "work")

    assert payload["system"]["provider_ready"] is bool(expected)
    assert payload["system"]["chat_ready"] is bool(expected)
    assert payload["system"]["config_path"] == str(
        status_profiles.homes["work"] / "config.yaml"
    )
    assert payload["system"]["env_path"] == str(status_profiles.homes["work"] / ".env")
    assert status_profiles.keys == [expected]
    assert status_profiles.stages == [
        ("settings", expected or ""),
        ("resolver", expected or ""),
        ("models", expected or ""),
    ]


@pytest.mark.parametrize("profile", [None, "default"])
@pytest.mark.parametrize("has_key", [False, True])
def test_status_preserves_root_process_credentials(
    status_profiles, monkeypatch, profile, has_key
):
    if has_key:
        monkeypatch.setenv(KEY, "root-placeholder")
    payload = request_status(status_profiles, profile)
    assert payload["system"]["chat_ready"] is has_key
    assert payload["system"]["config_path"] == str(
        status_profiles.homes["default"] / "config.yaml"
    )
    assert status_profiles.keys == ["root-placeholder" if has_key else None]


def test_status_scope_does_not_leak_between_requests(status_profiles, monkeypatch):
    monkeypatch.setenv(KEY, "root-placeholder")
    (status_profiles.homes["work"] / ".env").write_text(
        f"{KEY}=profile-placeholder\n",
        encoding="utf-8",
    )
    for profile, ready in [
        ("work", True),
        ("empty", False),
        (None, True),
        ("work", True),
    ]:
        assert request_status(status_profiles, profile)["system"]["chat_ready"] is ready
    assert status_profiles.keys == [
        "profile-placeholder",
        None,
        "root-placeholder",
        "profile-placeholder",
    ]


@pytest.mark.parametrize(
    "profile,host_in",
    [("work", "env_file"), ("default", "process_env"), (None, "process_env")],
)
def test_status_expands_custom_base_url_envref(
    status_profiles, monkeypatch, profile, host_in
):
    """``base_url: ${VAR}`` expands in the reader's scope, not once globally."""
    HOST = "ONBOARDING_SCOPE_TEST_BASE_HOST"
    monkeypatch.delenv(HOST, raising=False)
    for home in status_profiles.homes.values():
        cfg_path = home / "config.yaml"
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        data["providers"]["scope-local"]["base_url"] = "${" + HOST + "}"
        cfg_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    if host_in == "env_file":
        expected = "http://127.0.0.1:9100/v1"
        (status_profiles.homes["work"] / ".env").write_text(
            f"{HOST}={expected}\n",
            encoding="utf-8",
        )
    else:
        expected = "http://127.0.0.1:9200/v1"
        monkeypatch.setenv(HOST, expected)

    payload = request_status(status_profiles, profile)

    assert payload["system"]["current_base_url"] == expected


SHAPES = ["raw", "keyed", "legacy"]


def _write_record_shape(homes, shape, record):
    """Write ``record`` as one of the three named-record shapes in every home."""
    if shape == "raw":
        extra = {"providers": {"scope-local": record}}
    elif shape == "keyed":
        extra = {"providers": {PROVIDER: record}}
    else:
        extra = {"custom_providers": [{"name": "scope-local", **record}]}
    for home in homes.values():
        data = {"model": {"provider": PROVIDER, "default": "local-model"}, **extra}
        (home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def _bundle_in_profile(profile):
    """``resolve_custom_provider_bundle`` under the request profile's readonly scope."""
    previous_profile = getattr(profiles._tls, "profile", None)
    profiles.set_request_profile(profile)
    try:
        with profiles.profile_env_for_active_request_readonly():
            return config.resolve_custom_provider_bundle(PROVIDER)
    finally:
        if previous_profile is None:
            profiles.clear_request_profile()
        else:
            profiles.set_request_profile(previous_profile)


@pytest.fixture
def token_builder(monkeypatch):
    """Stand-in for the Agent's ``key_cmd`` token source.

    Records every command handed to the builder and returns a provider that
    fails if anything ever invokes it: a status read or a routing decision must
    build a provider for a literal ``key_cmd`` without executing it, and must
    never hand an unresolved ``${CMD}`` to the builder at all.
    """
    calls = []
    module = ModuleType("agent.command_token_source")

    def build_command_token_provider(key_cmd, provider_label="custom"):
        calls.append(key_cmd)
        if not str(key_cmd or "").strip():
            return None

        def provider():
            raise AssertionError("key_cmd must not run during status or routing")

        return provider

    module.build_command_token_provider = build_command_token_provider
    monkeypatch.setitem(sys.modules, "agent.command_token_source", module)
    return calls


def _chat_merge(profile):
    """The streaming worker's connection seam, as chat would resolve it."""
    from api import streaming

    ambient = os.environ.get(KEY)
    bundle = streaming._resolve_runtime_connection_bundle(
        PROVIDER, None, None, {}, profile_name=profile
    )
    assert os.environ.get(KEY) == ambient
    return bundle


@pytest.mark.parametrize("shape", SHAPES)
def test_blank_envref_credential_is_unresolved_not_keyless(shape_profiles, shape):
    """``api_key: ${KEY}`` blank in the profile is a declared credential that
    did not resolve: not ready, and chat fails closed instead of sending the
    keyless placeholder."""
    _write_record_shape(
        shape_profiles.homes,
        shape,
        {"base_url": "http://127.0.0.1:8000/v1", "api_key": "${" + KEY + "}"},
    )
    (shape_profiles.homes["work"] / ".env").write_text(f'{KEY}=" "\n', encoding="utf-8")

    payload = request_status(shape_profiles, "work")

    assert payload["system"]["provider_ready"] is False
    assert payload["system"]["chat_ready"] is False
    assert (
        payload["system"]["provider_note_key"]
        == "onboarding_notice_provider_auth_required"
    )
    assert shape_profiles.keys == [None]

    bundle = _bundle_in_profile("work")
    assert bundle["api_key"] is None
    assert bundle["keyless"] is False

    merged = _chat_merge("work")
    assert merged["api_key"] != config.KEYLESS_CUSTOM_API_KEY
    verdict = config.custom_provider_route_error(merged)
    assert verdict and verdict["reason"] == config.CUSTOM_ROUTE_NO_CREDENTIAL


@pytest.mark.parametrize("host_in", ["undefined", "process_env"])
@pytest.mark.parametrize("shape", SHAPES)
def test_unresolved_base_url_envref_is_no_endpoint(
    shape_profiles, monkeypatch, shape, host_in
):
    """``base_url: ${HOST}`` that does not resolve in the profile is not an
    endpoint: not ready with the base-URL prompt, and chat reports the missing
    endpoint instead of handing the literal to the Agent."""
    HOST = "ONBOARDING_SCOPE_TEST_BASE_HOST"
    monkeypatch.delenv(HOST, raising=False)
    if host_in == "process_env":
        monkeypatch.setenv(HOST, "http://127.0.0.1:9200/v1")
    _write_record_shape(
        shape_profiles.homes, shape, {"base_url": "${" + HOST + "}", "key_env": KEY}
    )
    (shape_profiles.homes["work"] / ".env").write_text(
        f"{KEY}=work-placeholder\n", encoding="utf-8"
    )

    payload = request_status(shape_profiles, "work")

    assert payload["system"]["provider_ready"] is False
    assert payload["system"]["chat_ready"] is False
    assert payload["system"]["current_base_url"] is None
    assert (
        payload["system"]["provider_note_key"]
        == "onboarding_notice_custom_base_url_required"
    )

    merged = _chat_merge("work")
    assert "${" not in str(merged.get("base_url") or "")
    verdict = config.custom_provider_route_error(merged)
    assert verdict and verdict["reason"] == config.CUSTOM_ROUTE_NO_ENDPOINT


@pytest.mark.parametrize("shape", SHAPES)
def test_literal_record_still_routes_with_profile_key(shape_profiles, shape):
    """Control: a literal endpoint with a profile key stays ready and routable."""
    _write_record_shape(
        shape_profiles.homes,
        shape,
        {"base_url": "http://127.0.0.1:8000/v1", "key_env": KEY},
    )
    (shape_profiles.homes["work"] / ".env").write_text(
        f"{KEY}=work-placeholder\n", encoding="utf-8"
    )

    payload = request_status(shape_profiles, "work")
    assert payload["system"]["chat_ready"] is True

    merged = _chat_merge("work")
    assert config.custom_provider_route_error(merged) is None
    assert merged["api_key"] == "work-placeholder"
    assert merged["base_url"] == "http://127.0.0.1:8000/v1"


@pytest.mark.parametrize("profile", ["work", "default"])
@pytest.mark.parametrize("shape", SHAPES)
def test_pooled_literal_envref_is_unresolved_not_a_credential(
    shape_profiles, monkeypatch, shape, profile
):
    """The installed pool can hand ``api_key: ${KEY}`` back untouched when
    ``KEY`` is unset. That literal is not a credential: the pool result and its
    pool object are discarded, the declaration survives, and both status and
    chat end with the unresolved-credential verdict."""
    _write_record_shape(
        shape_profiles.homes,
        shape,
        {"base_url": "http://127.0.0.1:8000/v1", "api_key": "${" + KEY + "}"},
    )
    pool = object()
    consulted = []

    def literal_pool(base_url, record):
        consulted.append(base_url)
        if not base_url:
            return None
        return {"api_key": "${" + KEY + "}", "credential_pool": pool}

    monkeypatch.setattr(config, "_custom_record_pool_runtime", literal_pool)

    payload = request_status(shape_profiles, profile)

    assert payload["system"]["provider_ready"] is False
    assert payload["system"]["chat_ready"] is False
    assert (
        payload["system"]["provider_note_key"]
        == "onboarding_notice_provider_auth_required"
    )
    assert shape_profiles.keys == [None]
    assert consulted, "the pool must be consulted and rejected, not bypassed"

    bundle = _bundle_in_profile(profile)
    assert bundle["api_key"] is None
    assert bundle["keyless"] is False
    assert "credential_pool" not in bundle["owned"]

    merged = _chat_merge(profile)
    assert merged.get("api_key") not in (
        "${" + KEY + "}",
        config.KEYLESS_CUSTOM_API_KEY,
    )
    assert merged.get("credential_pool") is not pool
    verdict = config.custom_provider_route_error(merged)
    assert verdict and verdict["reason"] == config.CUSTOM_ROUTE_NO_CREDENTIAL


@pytest.mark.parametrize("shape", SHAPES)
def test_pooled_literal_on_undeclared_record_is_not_keyless(
    shape_profiles, monkeypatch, shape
):
    """A pool row can exist for an endpoint whose record declares no credential
    field of its own. The pool answering is still a declaration: when its
    answer is an unresolved literal the route is unresolved, never keyless."""
    _write_record_shape(
        shape_profiles.homes, shape, {"base_url": "http://127.0.0.1:8000/v1"}
    )
    pool = object()
    monkeypatch.setattr(
        config,
        "_custom_record_pool_runtime",
        lambda base_url, record: (
            {"api_key": "${" + KEY + "}", "credential_pool": pool} if base_url else None
        ),
    )

    payload = request_status(shape_profiles, "work")

    assert payload["system"]["chat_ready"] is False
    bundle = _bundle_in_profile("work")
    assert bundle["api_key"] is None
    assert bundle["keyless"] is False
    assert "credential_pool" not in bundle["owned"]

    merged = _chat_merge("work")
    assert merged.get("api_key") not in (
        "${" + KEY + "}",
        config.KEYLESS_CUSTOM_API_KEY,
    )
    verdict = config.custom_provider_route_error(merged)
    assert verdict and verdict["reason"] == config.CUSTOM_ROUTE_NO_CREDENTIAL


KEY_CMD = "ONBOARDING_SCOPE_TEST_KEY_CMD"


@pytest.mark.parametrize("cmd_in", ["blank_in_profile", "unset"])
@pytest.mark.parametrize("shape", SHAPES)
def test_unresolved_key_cmd_envref_is_unresolved_not_keyless(
    shape_profiles, monkeypatch, token_builder, shape, cmd_in
):
    """``key_cmd: ${CMD}`` that expands blank or stays unresolved is a declared
    credential source with nothing behind it: not keyless, no placeholder, no
    token provider, and the literal never reaches the Agent's command source."""
    monkeypatch.delenv(KEY_CMD, raising=False)
    _write_record_shape(
        shape_profiles.homes,
        shape,
        {"base_url": "http://127.0.0.1:8000/v1", "key_cmd": "${" + KEY_CMD + "}"},
    )
    if cmd_in == "blank_in_profile":
        (shape_profiles.homes["work"] / ".env").write_text(
            f'{KEY_CMD}=" "\n', encoding="utf-8"
        )

    payload = request_status(shape_profiles, "work")

    assert payload["system"]["provider_ready"] is False
    assert payload["system"]["chat_ready"] is False
    assert (
        payload["system"]["provider_note_key"]
        == "onboarding_notice_provider_auth_required"
    )
    assert shape_profiles.keys == [None]

    bundle = _bundle_in_profile("work")
    assert bundle["api_key"] is None
    assert bundle["keyless"] is False

    merged = _chat_merge("work")
    assert not callable(merged.get("api_key"))
    assert merged.get("api_key") != config.KEYLESS_CUSTOM_API_KEY
    verdict = config.custom_provider_route_error(merged)
    assert verdict and verdict["reason"] == config.CUSTOM_ROUTE_NO_CREDENTIAL
    assert token_builder == []


@pytest.mark.parametrize("shape", SHAPES)
def test_literal_key_cmd_still_builds_a_token_provider(
    shape_profiles, token_builder, shape
):
    """Control: a literal ``key_cmd`` is a usable credential source. Status and
    routing build its per-request provider and never execute it."""
    command = "print-token --provider local"
    _write_record_shape(
        shape_profiles.homes,
        shape,
        {"base_url": "http://127.0.0.1:8000/v1", "key_cmd": command},
    )

    payload = request_status(shape_profiles, "work")

    assert payload["system"]["chat_ready"] is True
    assert callable(shape_profiles.keys[0])

    merged = _chat_merge("work")
    assert config.custom_provider_route_error(merged) is None
    assert callable(merged["api_key"])
    assert token_builder and set(token_builder) == {command}


@pytest.mark.parametrize("shape", SHAPES)
def test_undeclared_credential_stays_keyless(shape_profiles, shape):
    """Control: a record that declares no credential source at all is the only
    thing that may be keyless, and chat then uses the placeholder."""
    _write_record_shape(
        shape_profiles.homes, shape, {"base_url": "http://127.0.0.1:8000/v1"}
    )

    payload = request_status(shape_profiles, "work")

    assert payload["system"]["chat_ready"] is True
    bundle = _bundle_in_profile("work")
    assert bundle["api_key"] is None
    assert bundle["keyless"] is True

    merged = _chat_merge("work")
    assert config.custom_provider_route_error(merged) is None
    assert merged["api_key"] == config.KEYLESS_CUSTOM_API_KEY


@pytest.mark.parametrize(
    "failure_at", ["get_config", "get_available_models", "response"]
)
def test_status_restores_scope_after_exception(
    status_profiles, monkeypatch, failure_at
):
    monkeypatch.setenv(KEY, "ambient-placeholder")
    (status_profiles.homes["work"] / ".env").write_text(
        f"{KEY}=profile-placeholder\n",
        encoding="utf-8",
    )
    seen = []

    def fail():
        seen.append(config._thread_local_env_value(KEY))
        raise RuntimeError("composition failed")

    if failure_at != "response":
        monkeypatch.setattr(onboarding, failure_at, fail)
    with pytest.raises(RuntimeError, match="failed"):
        request_status(
            status_profiles, "work", broken_response=failure_at == "response"
        )
    if failure_at == "response":
        assert status_profiles.keys == ["profile-placeholder"]
    else:
        assert seen == ["profile-placeholder"]


@pytest.mark.parametrize(
    "availability", ["available", "unavailable", "becomes_available"]
)
def test_status_imports_before_profile_binding(
    status_profiles, monkeypatch, tmp_path, availability
):
    """Execute a real module import with an inert, context-observing Agent fixture."""
    override = ContextVar("onboarding_test_home", default=None)
    constants = ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: Path(
        override.get() or os.environ["HERMES_HOME"]
    )
    constants.get_hermes_home_override = override.get
    constants.set_hermes_home_override = override.set
    constants.reset_hermes_home_override = override.reset
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    probe = ModuleType("onboarding_import_probe")
    probe.calls = []
    probe.available = availability == "available"
    monkeypatch.setitem(sys.modules, "onboarding_import_probe", probe)
    monkeypatch.delitem(sys.modules, "run_agent", raising=False)
    (tmp_path / "run_agent.py").write_text(
        "from hermes_constants import get_hermes_home, get_hermes_home_override\n"
        "import onboarding_import_probe as probe\n"
        "probe.calls.append((get_hermes_home(), get_hermes_home_override()))\n"
        "if not probe.available:\n"
        "    raise ImportError('fixture agent unavailable')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    # Exercise the actual import checker, including failed-import retries.
    checker = Mock(wraps=config.verify_hermes_imports)
    monkeypatch.setattr(onboarding, "verify_hermes_imports", checker)
    scoped_models = onboarding.get_available_models

    def models():
        assert override.get() == status_profiles.homes["work"]
        return scoped_models()

    monkeypatch.setattr(onboarding, "get_available_models", models)
    before_env = dict(os.environ)
    try:
        for attempt in range(2):
            if attempt and availability == "becomes_available":
                probe.available = True
            payload = request_status(status_profiles, "work")
            assert payload["system"]["imports_ok"] is probe.available
            assert override.get() is None
            # Compare without rendering any inherited environment values.
            assert bool(dict(os.environ) == before_env)
        assert checker.call_count == 2  # Once per request, never again in scope.
        expected_imports = 1 if availability == "available" else 2
        assert (
            probe.calls == [(status_profiles.homes["default"], None)] * expected_imports
        )
    finally:
        sys.modules.pop("run_agent", None)
