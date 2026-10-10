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


@pytest.fixture(params=["key_env", "api_key_env"])
def status_profiles(request, monkeypatch, tmp_path):
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
                        "scope-local": {
                            "base_url": "http://127.0.0.1:8000/v1",
                            request.param: KEY,
                        }
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
