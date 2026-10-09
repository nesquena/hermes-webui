"""MCP config edits use a pinned raw-file transaction, not the shared runtime cache.

Extracted scope from #6114. No real MCP server, provider or profile credentials.
"""

from __future__ import annotations

import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from api import config, routes

_ORIGINAL_CONFIG_PATH = config._get_config_path


@pytest.fixture
def store(tmp_path, monkeypatch):
    paths = {key: tmp_path / key / "config.yaml" for key in ("work", "default")}
    for key, path in paths.items():
        path.parent.mkdir()
        path.write_text(
            yaml.safe_dump(
                {
                    "profile_marker": key,
                    "providers": {"example": {"api_key": "${PORTFOLIO_TEST_TOKEN}"}},
                    "mcp_servers": {
                        "shared": {
                            "command": key + "-server",
                            "env": {"TOKEN": "${PORTFOLIO_TEST_TOKEN}"},
                        },
                        "remove": {"command": key + "-remove"},
                    },
                }
            ),
            encoding="utf-8",
        )
    scope = threading.local()
    scope.profile = "work"

    def path_for_request():
        return paths[getattr(scope, "profile", "work")]

    monkeypatch.setattr(config, "_get_config_path", path_for_request)
    monkeypatch.setattr(routes, "_get_config_path", path_for_request)
    cache = {}
    monkeypatch.setattr(config, "_cfg_cache", cache)
    monkeypatch.setattr(config, "cfg", cache)
    for name in ("_cfg_path", "_cfg_fingerprint"):
        monkeypatch.setattr(config, name, None)
    monkeypatch.setattr(config, "_cfg_mtime", 0.0)
    monkeypatch.setattr(config, "_yaml_file_cache", {})
    monkeypatch.setenv("PORTFOLIO_TEST_TOKEN", "synthetic-expanded-value")
    config.reload_config()

    def call(operation, name=None):
        handler = MagicMock()
        handler.path = "/api/mcp/servers/shared"
        handler.command = {"update": "PUT", "delete": "DELETE", "toggle": "PATCH"}[
            operation
        ]
        if operation == "update":
            routes._handle_mcp_server_update(
                handler, name or "new", {"command": "new-server"}
            )
        elif operation == "delete":
            routes._handle_mcp_server_delete(handler, name or "remove")
        else:
            routes._handle_mcp_server_toggle(
                handler, name or "shared", {"enabled": False}
            )
        return handler

    return SimpleNamespace(
        paths=paths,
        scope=scope,
        call=call,
        read=lambda key: yaml.safe_load(paths[key].read_text()),
    )


@pytest.mark.parametrize("operation", ["update", "toggle", "delete"])
def test_write_preserves_raw_placeholders_and_other_profile(store, operation):
    before_other = store.paths["default"].read_bytes()
    handler = store.call(operation)
    handler.send_response.assert_called_once_with(200)
    saved = store.read("work")
    assert saved["profile_marker"] == "work"
    assert saved["providers"]["example"]["api_key"] == "${PORTFOLIO_TEST_TOKEN}"
    assert saved["mcp_servers"]["shared"]["env"]["TOKEN"] == "${PORTFOLIO_TEST_TOKEN}"
    assert store.paths["default"].read_bytes() == before_other
    if operation == "update":
        assert saved["mcp_servers"]["new"]["command"] == "new-server"
    elif operation == "toggle":
        assert saved["mcp_servers"]["shared"]["enabled"] is False
    else:
        assert "remove" not in saved["mcp_servers"]


@pytest.mark.parametrize("operation", ["update", "toggle", "delete"])
def test_failed_disk_save_does_not_mutate_runtime_cache(store, monkeypatch, operation):
    before_cache = copy.deepcopy(config.get_config())
    before_file = store.paths["work"].read_bytes()

    def refuse(*args, **kwargs):
        raise OSError("synthetic storage failure")

    monkeypatch.setattr(routes, "_save_yaml_config_file", refuse)
    with pytest.raises(OSError, match="synthetic storage failure"):
        store.call(operation)
    assert config._cfg_cache == before_cache
    assert store.paths["work"].read_bytes() == before_file


@pytest.mark.parametrize("operation", ["update", "toggle", "delete"])
def test_write_excludes_other_profile_reload_until_commit(
    store, monkeypatch, operation
):
    entered, release, reload_started = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    original_save = routes._save_yaml_config_file
    locked_at_commit = []

    def paused_save(path, data):
        locked_at_commit.append(config._cfg_lock.locked())
        entered.set()
        assert release.wait(5)
        return original_save(path, data)

    monkeypatch.setattr(routes, "_save_yaml_config_file", paused_save)

    def write():
        store.scope.profile = "work"
        return store.call(operation)

    def reload_default():
        store.scope.profile = "default"
        reload_started.set()
        config.reload_config()

    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(write)
        try:
            assert entered.wait(5)
            reloading = pool.submit(reload_default)
            assert reload_started.wait(5)
        finally:
            release.set()
        writing.result(timeout=10)
        reloading.result(timeout=10)
    assert locked_at_commit == [True], "read-modify-save must use the config lock"
    assert store.read("work")["profile_marker"] == "work"
    assert store.read("default")["profile_marker"] == "default"


@pytest.mark.parametrize("raw", ["[broken", "- list\n- not-config\n", "42\n"])
def test_invalid_existing_yaml_is_not_overwritten(store, raw):
    store.paths["work"].write_text(raw, encoding="utf-8")
    before = store.paths["work"].read_bytes()
    with pytest.raises((ValueError, yaml.YAMLError)):
        store.call("update")
    assert store.paths["work"].read_bytes() == before


def test_config_path_is_resolved_only_once(store, monkeypatch):
    calls = []
    original = routes._get_config_path

    def resolve():
        calls.append(1)
        return original()

    monkeypatch.setattr(routes, "_get_config_path", resolve)
    store.call("update")
    assert calls == [1]


def test_missing_config_can_be_created(store):
    store.paths["work"].unlink()
    handler = store.call("update")
    handler.send_response.assert_called_once_with(200)
    assert store.read("work") == {"mcp_servers": {"new": {"command": "new-server"}}}


def test_masked_credentials_preserve_unexpanded_original(store):
    handler = MagicMock()
    routes._handle_mcp_server_update(
        handler,
        "shared",
        {
            "command": "changed-server",
            "env": {"TOKEN": routes._MASKED_PLACEHOLDER},
        },
    )
    handler.send_response.assert_called_once_with(200)
    assert (
        store.read("work")["mcp_servers"]["shared"]["env"]["TOKEN"]
        == "${PORTFOLIO_TEST_TOKEN}"
    )


@pytest.mark.parametrize("operation", ["delete", "toggle"])
def test_not_found_does_not_write_or_hold_lock_during_response(store, operation):
    before = store.paths["work"].read_bytes()
    handler = MagicMock()

    def respond(status):
        assert not config._cfg_lock.locked(), (
            "HTTP response must be outside config lock"
        )

    handler.send_response.side_effect = respond
    if operation == "delete":
        routes._handle_mcp_server_delete(handler, "missing")
    else:
        routes._handle_mcp_server_toggle(handler, "missing", {"enabled": False})
    handler.send_response.assert_called_once_with(404)
    assert store.paths["work"].read_bytes() == before


def test_two_same_profile_edits_are_both_retained(store):
    start = threading.Barrier(2)

    def edit(name):
        store.scope.profile = "work"
        start.wait(timeout=5)
        return store.call("update", name)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(edit, name) for name in ("first", "second")]
        for future in futures:
            future.result(timeout=10).send_response.assert_called_once_with(200)
    assert {"first", "second"} <= store.read("work")["mcp_servers"].keys()


@pytest.mark.parametrize("operation", ["update", "toggle", "delete"])
def test_read_failure_does_not_overwrite_file_or_cache(store, monkeypatch, operation):
    from pathlib import Path

    original = Path.read_text
    before_file = store.paths["work"].read_bytes()
    before_cache = copy.deepcopy(config._cfg_cache)

    def unreadable(path, *args, **kwargs):
        if path == store.paths["work"]:
            raise PermissionError("synthetic denied read")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(PermissionError):
        store.call(operation)
    assert store.paths["work"].read_bytes() == before_file
    assert config._cfg_cache == before_cache


@pytest.mark.parametrize("operation", ["update", "toggle", "delete"])
def test_reload_and_success_response_happen_outside_lock(store, monkeypatch, operation):
    original_reload = routes.reload_config

    def reload():
        assert not config._cfg_lock.locked()
        original_reload()

    monkeypatch.setattr(routes, "reload_config", reload)
    original_j = routes.j

    def respond(*args, **kwargs):
        assert not config._cfg_lock.locked()
        return original_j(*args, **kwargs)

    monkeypatch.setattr(routes, "j", respond)
    store.call(operation).send_response.assert_called_once_with(200)


def test_operator_config_override_is_still_authoritative(store, monkeypatch):
    # Exercise the original resolver, not a test substitute. An operator override
    # deliberately selects another file; this narrow fix must not change policy.
    # The fixture replaced both module bindings. Resolve the unpatched function
    # from the test's module-level snapshot recorded below.
    monkeypatch.setattr(config, "_get_config_path", _ORIGINAL_CONFIG_PATH)
    monkeypatch.setattr(routes, "_get_config_path", _ORIGINAL_CONFIG_PATH)
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(store.paths["default"]))
    before = store.paths["work"].read_bytes()
    store.call("update")
    assert store.paths["work"].read_bytes() == before
    assert "new" in store.read("default")["mcp_servers"]


def test_toggle_does_not_mutate_aliased_server(store):
    shared = {"command": "shared-command"}
    store.paths["work"].write_text(
        yaml.safe_dump(
            {
                "mcp_servers": {"first": shared, "second": shared},
                "server_template": shared,
            }
        )
    )
    store.call("toggle", "first").send_response.assert_called_once_with(200)
    saved = store.read("work")
    assert saved["mcp_servers"]["first"]["enabled"] is False
    assert saved["mcp_servers"]["second"] == {"command": "shared-command"}
    assert saved["server_template"] == {"command": "shared-command"}


@pytest.mark.parametrize("operation", ["update", "toggle", "delete"])
def test_mcp_container_alias_does_not_modify_unrelated_section(store, operation):
    shared = {
        "shared": {"command": "shared-command"},
        "remove": {"command": "remove-command"},
    }
    original = copy.deepcopy(shared)
    store.paths["work"].write_text(
        yaml.safe_dump({"mcp_servers": shared, "unrelated": shared})
    )
    store.call(operation).send_response.assert_called_once_with(200)
    saved = store.read("work")
    assert saved["unrelated"] == original
    assert saved["mcp_servers"] != original
