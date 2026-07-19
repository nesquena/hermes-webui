"""Regression coverage for issue #5619 profile config cross-contamination."""

from __future__ import annotations

import copy
import json
import os
import sys
import threading
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml


def _write_config(home, *, provider: str, mcp_server: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    home.joinpath("config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {"provider": provider, "default": f"{provider}-model"},
                "providers": {provider: {"api_key": f"{provider}-key"}},
                "mcp_servers": {mcp_server: {"command": f"run-{mcp_server}"}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _handler():
    handler = MagicMock()
    handler.path = "/api/mcp/servers"
    handler.command = "GET"
    return handler


def _payload(handler) -> dict:
    body = handler.wfile.write.call_args[0][0]
    return json.loads(body.decode("utf-8"))


def _server_handler(path: str, *, command: str = "GET"):
    from server import Handler

    handler = Handler.__new__(Handler)
    handler.path = path
    handler.command = command
    handler.headers = {"Cookie": "hermes_profile=ghost"}
    handler.wfile = MagicMock()
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler._safe_webui_print = MagicMock()
    return handler


def _sent_headers(handler, name: str) -> list[str]:
    return [
        args[1]
        for args, _kwargs in handler.send_header.call_args_list
        if args and args[0] == name
    ]


@pytest.fixture
def profile_config_harness(tmp_path, monkeypatch):
    from api import config, routes

    default_home = tmp_path / "default"
    work_home = tmp_path / "profiles" / "work"
    _write_config(default_home, provider="default-provider", mcp_server="default-mcp")
    _write_config(work_home, provider="work-provider", mcp_server="work-mcp")
    shared_mtime = 1_700_000_000
    os.utime(default_home / "config.yaml", (shared_mtime, shared_mtime))
    os.utime(work_home / "config.yaml", (shared_mtime, shared_mtime))

    request_scope = threading.local()

    def active_home():
        return work_home if getattr(request_scope, "profile", "default") == "work" else default_home

    def active_config_path():
        return active_home() / "config.yaml"

    original_cache = copy.deepcopy(config._cfg_cache)
    original_mtime = config._cfg_mtime
    original_path = config._cfg_path
    original_fingerprint = config._cfg_fingerprint
    original_cfg_rebound = config.cfg is not config._cfg_cache
    original_cfg = copy.deepcopy(config.cfg)

    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.setattr(config, "_get_config_path", active_config_path)
    monkeypatch.setattr(routes, "_get_config_path", active_config_path)
    monkeypatch.setattr(routes, "get_active_hermes_home", active_home)
    request_scope.profile = "default"
    config.reload_config()

    yield SimpleNamespace(
        config=config,
        routes=routes,
        request_scope=request_scope,
        default_home=default_home,
        work_home=work_home,
    )

    with config._cfg_lock:
        config._cfg_cache.clear()
        config._cfg_cache.update(original_cache)
        config._cfg_mtime = original_mtime
        config._cfg_path = original_path
        config._cfg_fingerprint = original_fingerprint
        config.cfg = copy.deepcopy(original_cfg) if original_cfg_rebound else config._cfg_cache


def _reload_default_in_other_thread(harness) -> None:
    errors = []

    def reload_default():
        try:
            harness.request_scope.profile = "default"
            harness.config.reload_config()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=reload_default)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not errors


def test_mcp_list_keeps_work_profile_snapshot_during_default_reload(
    profile_config_harness, monkeypatch
):
    harness = profile_config_harness
    routes = harness.routes
    harness.request_scope.profile = "work"
    original_get = routes.get_config_for_profile_home

    def get_then_reload(profile_home):
        config_data = original_get(profile_home)
        _reload_default_in_other_thread(harness)
        return config_data

    monkeypatch.setattr(routes, "get_config_for_profile_home", get_then_reload)
    monkeypatch.setattr(routes, "_mcp_runtime_status_by_name", lambda: {})

    handler = _handler()
    routes._handle_mcp_servers_list(handler)

    assert [row["name"] for row in _payload(handler)["servers"]] == ["work-mcp"]


def test_provider_catalog_keeps_work_profile_snapshot_during_default_reload(
    profile_config_harness, monkeypatch
):
    from api import providers

    harness = profile_config_harness
    harness.request_scope.profile = "work"
    original_get = providers.get_config

    def get_then_reload():
        config_data = original_get()
        _reload_default_in_other_thread(harness)
        return config_data

    monkeypatch.setattr(providers, "get_config", get_then_reload)

    result = providers.get_providers()
    provider_ids = {row["id"] for row in result["providers"]}

    assert result["active_provider"] == "work-provider"
    assert "work-provider" in provider_ids
    assert "default-provider" not in provider_ids


def test_model_picker_keeps_work_profile_snapshot_during_default_reload(
    profile_config_harness, monkeypatch
):
    harness = profile_config_harness
    config = harness.config
    harness.request_scope.profile = "work"
    original_get = config.get_config_snapshot
    original_get_providers_cfg = config._get_providers_cfg
    provider_cfg_global_reads = []

    def get_then_reload():
        config_data = original_get()
        _reload_default_in_other_thread(harness)
        return config_data

    def track_provider_cfg_reads(*, config_data=None):
        if config_data is None:
            provider_cfg_global_reads.append(copy.deepcopy(config.cfg.get("providers", {})))
        return original_get_providers_cfg(config_data=config_data)

    monkeypatch.setattr(config, "get_config_snapshot", get_then_reload)
    monkeypatch.setattr(config, "_get_providers_cfg", track_provider_cfg_reads)
    monkeypatch.setattr(config, "_available_models_cache", None)
    monkeypatch.setattr(config, "_available_models_cache_ts", 0.0)
    monkeypatch.setattr(config, "_available_models_cache_source_fingerprint", None)
    monkeypatch.setattr(config, "_models_cache_provenance", None)
    monkeypatch.setattr(config, "_cache_build_in_progress", False)
    monkeypatch.setattr(config, "_load_models_cache_from_disk", lambda *, config_data=None: None)
    monkeypatch.setattr(config, "_load_stale_models_cache_from_disk", lambda *, config_data=None: None)
    for env_name in ("HERMES_MODEL", "OPENAI_MODEL", "LLM_MODEL"):
        monkeypatch.delenv(env_name, raising=False)

    result = config.get_available_models(prefer_cache=True)

    assert result["active_provider"] == "work-provider"
    assert result["default_model"] == "work-provider-model"
    assert provider_cfg_global_reads == []


def test_static_model_catalog_uses_snapshot_for_provider_model_allowlist(
    monkeypatch, tmp_path
):
    from api import config

    for env_name in ("HERMES_MODEL", "OPENAI_MODEL", "LLM_MODEL"):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: tmp_path / "auth.json")
    monkeypatch.setattr(
        config,
        "cfg",
        {
            "model": {"provider": "work-provider", "default": "global-model"},
            "providers": {"work-provider": {"models": {"global-model": {}}}},
        },
        raising=False,
    )

    snapshot = {
        "model": {"provider": "work-provider", "default": "snapshot-model"},
        "providers": {"work-provider": {"models": {"snapshot-model": {}}}},
    }

    result = config._static_models_catalog_without_live_probes(config_data=snapshot)
    work_group = next(
        group for group in result["groups"] if group.get("provider_id") == "work-provider"
    )
    model_ids = {model["id"] for model in work_group["models"]}

    assert "snapshot-model" in model_ids
    assert "global-model" not in model_ids


def test_settings_default_model_uses_one_profile_snapshot(
    profile_config_harness, monkeypatch
):
    harness = profile_config_harness
    config = harness.config
    harness.request_scope.profile = "work"
    original_get = config.get_config_snapshot

    def get_then_reload():
        config_data = original_get()
        _reload_default_in_other_thread(harness)
        return config_data

    monkeypatch.setattr(config, "get_config_snapshot", get_then_reload)
    monkeypatch.setattr(config, "_read_raw_settings_file", lambda: {})
    for env_name in ("HERMES_MODEL", "OPENAI_MODEL", "LLM_MODEL"):
        monkeypatch.delenv(env_name, raising=False)

    settings = config.load_settings()

    assert settings["default_model"] == "work-provider-model"
    assert settings["default_model_provider"] == "work-provider"


def test_mcp_write_holds_profile_config_lock_through_save(
    profile_config_harness, monkeypatch
):
    harness = profile_config_harness
    routes = harness.routes
    entered_save = threading.Event()
    release_save = threading.Event()
    reload_attempted = threading.Event()
    reload_finished = threading.Event()
    errors = []
    original_save = routes._save_yaml_config_file

    def paused_save(path, config_data):
        if path == harness.work_home / "config.yaml":
            entered_save.set()
            assert release_save.wait(timeout=5)
        return original_save(path, config_data)

    monkeypatch.setattr(routes, "_save_yaml_config_file", paused_save)

    def update_work():
        try:
            harness.request_scope.profile = "work"
            handler = _handler()
            handler.command = "PUT"
            routes._handle_mcp_server_update(
                handler,
                "new-work-mcp",
                {"command": "run-new-work-mcp"},
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def reload_default():
        try:
            assert entered_save.wait(timeout=5)
            harness.request_scope.profile = "default"
            reload_attempted.set()
            harness.config.reload_config()
            reload_finished.set()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    update_thread = threading.Thread(target=update_work)
    reload_thread = threading.Thread(target=reload_default)
    update_thread.start()
    reload_thread.start()

    assert entered_save.wait(timeout=5)
    assert reload_attempted.wait(timeout=5)
    assert not reload_finished.wait(timeout=0.1)
    release_save.set()
    update_thread.join(timeout=5)
    reload_thread.join(timeout=5)

    assert not update_thread.is_alive()
    assert not reload_thread.is_alive()
    assert not errors

    work_config = yaml.safe_load(
        harness.work_home.joinpath("config.yaml").read_text(encoding="utf-8")
    )
    default_config = yaml.safe_load(
        harness.default_home.joinpath("config.yaml").read_text(encoding="utf-8")
    )
    assert work_config["model"]["provider"] == "work-provider"
    assert "new-work-mcp" in work_config["mcp_servers"]
    assert default_config["model"]["provider"] == "default-provider"
    assert "new-work-mcp" not in default_config["mcp_servers"]


def test_profile_snapshot_expands_env_from_request_profile_raw_yaml(
    profile_config_harness, monkeypatch
):
    harness = profile_config_harness
    config = harness.config
    harness.work_home.joinpath("config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {"provider": "work-provider", "default": "work-model"},
                "providers": {"work-provider": {"api_key": "${PROFILE_TOKEN}"}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    harness.work_home.joinpath(".env").write_text("PROFILE_TOKEN=work-secret\n", encoding="utf-8")
    monkeypatch.setenv("PROFILE_TOKEN", "process-secret")
    harness.request_scope.profile = "work"
    snapshot = config.get_config_snapshot()

    assert snapshot["providers"]["work-provider"]["api_key"] == "work-secret"


def test_named_profile_snapshot_does_not_expand_missing_env_from_process(
    profile_config_harness, monkeypatch
):
    harness = profile_config_harness
    config = harness.config
    harness.work_home.joinpath("config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {"provider": "work-provider", "default": "work-model"},
                "providers": {"work-provider": {"api_key": "${PROFILE_TOKEN}"}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PROFILE_TOKEN", "process-secret")
    harness.request_scope.profile = "work"

    snapshot = config.get_config_snapshot()
    explicit = config.get_config_for_profile_home(harness.work_home)

    assert snapshot["providers"]["work-provider"]["api_key"] == "${PROFILE_TOKEN}"
    assert explicit["providers"]["work-provider"]["api_key"] == "${PROFILE_TOKEN}"


def test_model_resolution_uses_one_profile_snapshot_after_other_profile_reload(
    profile_config_harness,
):
    harness = profile_config_harness
    config = harness.config
    harness.default_home.joinpath("config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {
                    "provider": "lmstudio",
                    "default": "local-model",
                    "base_url": "http://default-lmstudio.test/v1",
                },
                "providers": {
                    "lmstudio": {"base_url": "http://default-lmstudio.test/v1"}
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    harness.work_home.joinpath("config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {
                    "provider": "lmstudio",
                    "default": "local-model",
                    "base_url": "http://work-lmstudio.test/v1",
                },
                "providers": {
                    "lmstudio": {"base_url": "http://work-lmstudio.test/v1"}
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    harness.request_scope.profile = "work"
    snapshot = config.get_config_snapshot()

    _reload_default_in_other_thread(harness)
    model_for_resolution = config.model_with_provider_context(
        "local-model",
        "lmstudio",
        config_data=snapshot,
    )
    _, provider, base_url = config.resolve_model_provider(
        model_for_resolution,
        config_data=snapshot,
    )

    assert provider == "lmstudio"
    assert base_url == "http://work-lmstudio.test/v1"


def test_streaming_runtime_resolution_stays_inside_profile_scope():
    from pathlib import Path

    from api import streaming

    source = Path(streaming.__file__).read_text(encoding="utf-8")
    start = source.index(
        'with profiles_api.profile_scope_for_detached_worker(\n'
        '                _resolved_profile_name, "model/runtime resolution"'
    )
    end = source.index("            # Read per-profile config at call time", start)
    block = source[start:end]

    assert "resolve_runtime_provider_with_anthropic_env_lock" in block
    assert "_resolve_custom_provider_runtime_overrides" in block
    assert "config_data=_model_resolution_cfg" in block


def test_credential_self_heal_resolves_with_named_profile_env(
    profile_config_harness, monkeypatch
):
    from api import config, oauth, profiles, streaming

    harness = profile_config_harness
    harness.work_home.joinpath(".env").write_text("PROFILE_TOKEN=work-secret\n", encoding="utf-8")
    monkeypatch.setenv("PROFILE_TOKEN", "process-secret")
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda name: harness.work_home if name == "work" else harness.default_home,
    )
    monkeypatch.setattr(oauth, "read_auth_json", lambda: {"auth": True})
    monkeypatch.setattr(
        oauth,
        "resolve_runtime_provider_with_anthropic_env_lock",
        lambda resolver, **kwargs: resolver(**kwargs),
    )
    captured = {}

    def resolve_runtime_provider(**kwargs):
        captured["thread_env"] = dict(getattr(config._thread_ctx, "env", {}) or {})
        captured["block_process_env_fallback"] = bool(
            getattr(config._thread_ctx, "block_process_env_fallback", False)
        )
        return {
            "provider": kwargs.get("requested"),
            "api_key": captured["thread_env"].get("PROFILE_TOKEN"),
        }

    hermes_cli_mod = types.ModuleType("hermes_cli")
    runtime_mod = types.ModuleType("hermes_cli.runtime_provider")
    runtime_mod.resolve_runtime_provider = resolve_runtime_provider
    hermes_cli_mod.runtime_provider = runtime_mod
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_mod)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", runtime_mod)

    result = streaming._attempt_credential_self_heal(
        "custom:work",
        "session-work",
        None,
        target_model="work-model",
        profile_name="work",
    )

    assert result["api_key"] == "work-secret"
    assert captured["thread_env"]["PROFILE_TOKEN"] == "work-secret"
    assert captured["block_process_env_fallback"] is True


def test_mcp_write_ignores_hermes_config_path_and_preserves_raw_placeholders(
    profile_config_harness, monkeypatch
):
    harness = profile_config_harness
    routes = harness.routes
    harness.work_home.joinpath("config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {"provider": "work-provider", "default": "work-model"},
                "providers": {"work-provider": {"api_key": "${PROFILE_TOKEN}"}},
                "mcp_servers": {},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(harness.default_home / "config.yaml"))
    monkeypatch.setenv("PROFILE_TOKEN", "process-secret")
    harness.request_scope.profile = "work"

    handler = _handler()
    handler.command = "PUT"
    routes._handle_mcp_server_update(
        handler,
        "raw-work-mcp",
        {"command": "run-raw-work-mcp"},
    )

    work_config = yaml.safe_load(
        harness.work_home.joinpath("config.yaml").read_text(encoding="utf-8")
    )
    default_config = yaml.safe_load(
        harness.default_home.joinpath("config.yaml").read_text(encoding="utf-8")
    )
    assert work_config["providers"]["work-provider"]["api_key"] == "${PROFILE_TOKEN}"
    assert "raw-work-mcp" in work_config["mcp_servers"]
    assert "raw-work-mcp" not in default_config["mcp_servers"]


def test_mcp_tools_inventory_filters_runtime_and_registry_to_active_profile(
    profile_config_harness, monkeypatch
):
    harness = profile_config_harness
    routes = harness.routes
    _write_config(harness.default_home, provider="default-provider", mcp_server="shared-mcp")
    _write_config(harness.work_home, provider="work-provider", mcp_server="shared-mcp")
    harness.request_scope.profile = "work"
    work_key = str(harness.work_home.resolve())
    default_key = str(harness.default_home.resolve())
    monkeypatch.setattr(
        routes,
        "_mcp_runtime_status_by_name",
        lambda: {
            (work_key, "shared-mcp"): {
                "name": "shared-mcp",
                "profile_home": work_key,
                "tools": [{"name": "work_tool"}],
            },
            (default_key, "shared-mcp"): {
                "name": "shared-mcp",
                "profile_home": default_key,
                "tools": [{"name": "default_tool"}],
            },
            ("", "shared-mcp"): {
                "name": "shared-mcp",
                "tools": [{"name": "unowned_tool"}],
            },
        },
    )

    handler = _handler()
    routes._handle_mcp_tools_list(handler)
    tools = _payload(handler)["tools"]

    assert [tool["name"] for tool in tools] == ["work_tool"]

    fake_registry_mod = types.ModuleType("tools.registry")

    class _Registry:
        def get_toolset_for_tool(self, name):
            return {
                "work_registry_tool": "mcp-shared-mcp",
                "default_registry_tool": "mcp-shared-mcp",
                "unowned_registry_tool": "mcp-shared-mcp",
            }[name]

        def get_profile_home_for_tool(self, name):
            return {
                "work_registry_tool": work_key,
                "default_registry_tool": default_key,
                "unowned_registry_tool": "",
            }[name]

        def get_schema(self, name):
            return {"name": name}

        def get_all_tool_names(self):
            return [
                "work_registry_tool",
                "default_registry_tool",
                "unowned_registry_tool",
            ]

    fake_registry_mod.registry = _Registry()
    monkeypatch.setitem(sys.modules, "tools.registry", fake_registry_mod)
    monkeypatch.setattr(routes, "_mcp_runtime_status_by_name", lambda: {})

    handler = _handler()
    routes._handle_mcp_tools_list(handler)
    tools = _payload(handler)["tools"]

    assert [tool["name"] for tool in tools] == ["work_registry_tool"]


def test_strict_profile_cookie_rejects_invalid_and_unknown_profiles(
    monkeypatch, tmp_path
):
    from api import profiles
    from api.helpers import InvalidProfileCookie, get_profile_cookie

    invalid_handler = SimpleNamespace(headers={"Cookie": "hermes_profile=../../etc"})
    with pytest.raises(InvalidProfileCookie):
        get_profile_cookie(invalid_handler, reject_invalid=True)

    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: name == "default")
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda name: tmp_path / "profiles" / name,
    )
    unknown_handler = SimpleNamespace(headers={"Cookie": "hermes_profile=ghost"})
    with pytest.raises(InvalidProfileCookie):
        get_profile_cookie(unknown_handler, reject_invalid=True)

    malformed_handler = SimpleNamespace(headers={"Cookie": "theme=dark; hermes_profile"})
    with pytest.raises(InvalidProfileCookie):
        get_profile_cookie(malformed_handler, reject_invalid=True)

    empty_handler = SimpleNamespace(headers={"Cookie": "theme=dark; hermes_profile="})
    with pytest.raises(InvalidProfileCookie):
        get_profile_cookie(empty_handler, reject_invalid=True)


def test_invalid_profile_cookie_clears_cookie_and_recovers_login(monkeypatch):
    import server
    from api.helpers import InvalidProfileCookie

    handler = _server_handler("/login?next=/")
    monkeypatch.setattr(
        server,
        "get_profile_cookie",
        lambda _handler, *, reject_invalid=False: (_ for _ in ()).throw(
            InvalidProfileCookie("Invalid or unknown active profile cookie")
        ),
    )
    monkeypatch.setattr(server, "check_auth", MagicMock())
    monkeypatch.setattr(server, "handle_get", MagicMock())

    server.Handler.do_GET(handler)

    handler.send_response.assert_called_once_with(303)
    assert _sent_headers(handler, "Location") == ["/login?next=/"]
    clear_headers = _sent_headers(handler, "Set-Cookie")
    assert len(clear_headers) == 1
    assert clear_headers[0].startswith("hermes_profile=")
    assert "Max-Age=0" in clear_headers[0]
    server.check_auth.assert_not_called()
    server.handle_get.assert_not_called()


def test_invalid_profile_cookie_clears_cookie_and_blocks_api_dispatch(monkeypatch):
    import server
    from api.helpers import InvalidProfileCookie

    handler = _server_handler("/api/models")
    monkeypatch.setattr(
        server,
        "get_profile_cookie",
        lambda _handler, *, reject_invalid=False: (_ for _ in ()).throw(
            InvalidProfileCookie("Invalid or unknown active profile cookie")
        ),
    )
    monkeypatch.setattr(server, "check_auth", MagicMock())
    monkeypatch.setattr(server, "handle_get", MagicMock())

    server.Handler.do_GET(handler)

    handler.send_response.assert_called_once_with(400)
    clear_headers = _sent_headers(handler, "Set-Cookie")
    assert len(clear_headers) == 1
    assert "Max-Age=0" in clear_headers[0]
    payload = json.loads(handler.wfile.write.call_args[0][0].decode("utf-8"))
    assert payload["profile_cookie_reset"] is True
    server.check_auth.assert_not_called()
    server.handle_get.assert_not_called()


def test_delete_profile_rejects_request_active_profile(monkeypatch):
    from api import profiles

    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: False)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: name == "default")
    profiles.set_request_profile("work")
    try:
        with pytest.raises(RuntimeError, match="Switch to another profile first"):
            profiles.delete_profile_api("work")
    finally:
        profiles.clear_request_profile()
