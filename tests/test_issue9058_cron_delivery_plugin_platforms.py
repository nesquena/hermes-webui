"""Regression tests for #9058: the cron delivery picker must offer the
plugin-registry messaging platforms.

Current Hermes Agents moved telegram/discord/slack/feishu/... out of
``cron.scheduler_delivery._KNOWN_DELIVERY_PLATFORMS`` (now only
``{"webhook", "wecom_callback"}``) into the plugin platform registry. The Agent
validates ``deliver=<name>`` as "in the constant OR a registry entry with a
``cron_deliver_env_var``"; ``_handle_cron_delivery_options`` enumerated only the
constant, so the picker showed Local / Origin / Webhook / Wecom_callback.

These tests need no real Agent: the Agent modules the handler imports are
replaced with fakes in ``sys.modules``.
"""

from __future__ import annotations

import ast
import sys
import types
from pathlib import Path

import pytest

import api.routes as routes

REPO_ROOT = Path(__file__).resolve().parents[1]


def _entry(name, label="", env_var: str | None = ""):
    return types.SimpleNamespace(name=name, label=label, cron_deliver_env_var=env_var)


class _FakeRegistry:
    @staticmethod
    def current_scope_key():
        return "test-scope"

    def __init__(self, entries=None, exc=None):
        self._entries = list(entries or [])
        self._exc = exc

    def all_entries(self):
        if self._exc is not None:
            raise self._exc
        return list(self._entries)


def _module(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def _install(
    monkeypatch,
    *,
    constant: frozenset | None = frozenset({"webhook", "wecom_callback"}),
    registry=None,
    registry_module_missing=False,
    plugins_module_missing=False,
    per_home_plugin_managers=True,
    discover=None,
    legacy_constant=None,
):
    """Replace the Agent modules the handler imports with fakes."""
    if constant is None:
        monkeypatch.setitem(sys.modules, "cron.scheduler_delivery", None)
    else:
        monkeypatch.setitem(
            sys.modules,
            "cron.scheduler_delivery",
            _module("cron.scheduler_delivery", _KNOWN_DELIVERY_PLATFORMS=frozenset(constant)),
        )
    if legacy_constant is None:
        monkeypatch.setitem(sys.modules, "cron.scheduler", None)
    else:
        monkeypatch.setitem(
            sys.modules,
            "cron.scheduler",
            _module("cron.scheduler", _KNOWN_DELIVERY_PLATFORMS=frozenset(legacy_constant)),
        )

    calls = {"discover": 0}

    def _discover_plugins(*_a, **_k):
        calls["discover"] += 1
        if discover is not None:
            discover()

    if plugins_module_missing:
        monkeypatch.setitem(sys.modules, "hermes_cli.plugins", None)
    else:
        plugin_attrs = {"discover_plugins": _discover_plugins}
        if per_home_plugin_managers:
            # Agents from 2026-08-12 key plugin managers by profile home.
            plugin_attrs["_plugin_managers_by_home"] = {}
        monkeypatch.setitem(
            sys.modules,
            "hermes_cli.plugins",
            _module("hermes_cli.plugins", **plugin_attrs),
        )

    if registry_module_missing:
        monkeypatch.setitem(sys.modules, "gateway.platform_registry", None)
    else:
        monkeypatch.setitem(
            sys.modules,
            "gateway.platform_registry",
            _module("gateway.platform_registry", platform_registry=registry or _FakeRegistry()),
        )
    return calls


def _call(monkeypatch):
    captured = {}

    def _j(_handler, payload, status=200, *_a, **_k):
        captured["payload"] = payload
        captured["status"] = status
        return True

    monkeypatch.setattr(routes, "j", _j)
    routes._handle_cron_delivery_options(object())
    assert captured.get("status") == 200, captured
    return captured["payload"]["platforms"]


def _values(platforms):
    return [p["value"] for p in platforms]


_PLUGIN_ENTRIES = [
    _entry("telegram", "Telegram", "TELEGRAM_HOME_CHANNEL"),
    _entry("discord", "Discord", "DISCORD_HOME_CHANNEL"),
    _entry("slack", "Slack", "SLACK_HOME_CHANNEL"),
    _entry("feishu", "Feishu / Lark", "FEISHU_HOME_CHANNEL"),
    # Registered platform without a cron home-channel env var: not deliverable.
    _entry("raft", "Raft", ""),
    # Also in the constant; registry has no env var for it but a better label.
    _entry("wecom_callback", "WeCom Callback (self-built apps)", ""),
]


def test_plugin_platforms_with_cron_env_var_are_offered(monkeypatch):
    calls = _install(monkeypatch, registry=_FakeRegistry(_PLUGIN_ENTRIES))
    platforms = _call(monkeypatch)
    values = _values(platforms)

    for expected in ("telegram", "discord", "slack", "feishu", "webhook", "wecom_callback"):
        assert expected in values, values
    assert calls["discover"] == 1, "discover_plugins() must run before the registry is read"

    labels = {p["value"]: p["label"] for p in platforms}
    assert labels["feishu"] == "Feishu / Lark"  # registry display label
    assert labels["telegram"] == "Telegram"
    assert labels["webhook"] == "Webhook"  # constant-only row: name.capitalize()
    assert labels["wecom_callback"] == "WeCom Callback (self-built apps)"


def test_registry_entries_without_cron_env_var_are_not_offered(monkeypatch):
    _install(
        monkeypatch,
        registry=_FakeRegistry([_entry("raft", "Raft", ""), _entry("nocron", "No Cron", None)]),
    )
    values = _values(_call(monkeypatch))
    assert "raft" not in values
    assert "nocron" not in values
    assert values == ["local", "origin", "webhook", "wecom_callback"]


def test_local_and_origin_first_then_sorted_and_deduplicated(monkeypatch):
    _install(
        monkeypatch,
        constant=frozenset({"webhook", "wecom_callback", "telegram"}),
        registry=_FakeRegistry(
            [
                _entry("telegram", "Telegram", "TELEGRAM_HOME_CHANNEL"),
                _entry("telegram", "Telegram", "TELEGRAM_HOME_CHANNEL"),
                _entry("whatsapp", "WhatsApp", "WHATSAPP_HOME_CHANNEL"),
                _entry("bluebubbles", "BlueBubbles (iMessage)", "BLUEBUBBLES_HOME_CHANNEL"),
                # A registry row must never displace the two built-in rows.
                _entry("local", "Plugin Local", "LOCAL_HOME_CHANNEL"),
                _entry("origin", "Plugin Origin", "ORIGIN_HOME_CHANNEL"),
            ]
        ),
    )
    platforms = _call(monkeypatch)
    values = _values(platforms)

    assert values == [
        "local",
        "origin",
        "bluebubbles",
        "telegram",
        "webhook",
        "wecom_callback",
        "whatsapp",
    ]
    assert len(values) == len(set(values))
    assert platforms[0]["label"] == "Local (save output only)"
    assert platforms[1]["label"] == "Origin (reply to creator)"


def test_registry_import_failure_degrades_to_constant_only(monkeypatch):
    _install(monkeypatch, registry_module_missing=True)
    assert _values(_call(monkeypatch)) == ["local", "origin", "webhook", "wecom_callback"]


def test_older_agent_without_plugins_module_keeps_legacy_behaviour(monkeypatch):
    """Pre-registry Agents: constant in ``cron.scheduler`` holds every platform."""
    legacy = frozenset({"telegram", "discord", "slack", "feishu"})
    _install(
        monkeypatch,
        constant=None,
        legacy_constant=legacy,
        plugins_module_missing=True,
        registry_module_missing=True,
    )
    platforms = _call(monkeypatch)
    assert _values(platforms) == ["local", "origin", "discord", "feishu", "slack", "telegram"]
    assert {p["value"]: p["label"] for p in platforms}["telegram"] == "Telegram"


def test_agent_without_per_home_plugin_managers_keeps_constant_only(monkeypatch):
    """Agents before 2026-08-12 share one process-wide plugin manager (and some run
    discovery under the cron env lock): never enumerate the registry there."""
    calls = _install(
        monkeypatch,
        registry=_FakeRegistry(_PLUGIN_ENTRIES),
        per_home_plugin_managers=False,
    )
    assert _values(_call(monkeypatch)) == ["local", "origin", "webhook", "wecom_callback"]
    assert calls["discover"] == 0, "discover_plugins() must not run on a process-wide plugin manager"


def test_registry_enumeration_raising_degrades_to_constant_only(monkeypatch):
    _install(monkeypatch, registry=_FakeRegistry(exc=RuntimeError("deferred loader exploded")))
    assert _values(_call(monkeypatch)) == ["local", "origin", "webhook", "wecom_callback"]


def test_discover_plugins_raising_degrades_to_constant_only(monkeypatch):
    def _boom():
        raise RuntimeError("plugin discovery failed")

    _install(monkeypatch, registry=_FakeRegistry(_PLUGIN_ENTRIES), discover=_boom)
    assert _values(_call(monkeypatch)) == ["local", "origin", "webhook", "wecom_callback"]


def test_partial_enumeration_failure_discards_partial_results(monkeypatch):
    class _HalfwayRegistry:
        @staticmethod
        def current_scope_key():
            return "test-scope"

        def all_entries(self):
            yield _entry("telegram", "Telegram", "TELEGRAM_HOME_CHANNEL")
            raise RuntimeError("second deferred loader exploded")

    _install(monkeypatch, registry=_HalfwayRegistry())
    platforms = _call(monkeypatch)
    assert _values(platforms) == ["local", "origin", "webhook", "wecom_callback"]
    assert {p["value"]: p["label"] for p in platforms}["wecom_callback"] == "Wecom_callback"


def test_no_agent_at_all_returns_local_and_origin(monkeypatch):
    _install(
        monkeypatch,
        constant=None,
        plugins_module_missing=True,
        registry_module_missing=True,
    )
    assert _values(_call(monkeypatch)) == ["local", "origin"]


@pytest.mark.parametrize("module", ["gateway.platform_registry", "hermes_cli.plugins"])
def test_registry_modules_are_not_imported_at_module_top_level(module):
    """Agent imports stay inside the handler so an Agent-free WebUI still boots."""
    tree = ast.parse((REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            assert node.module != module, f"top-level import of {module} in api/routes.py"
        elif isinstance(node, ast.Import):
            assert module not in {alias.name for alias in node.names}


def test_per_home_manager_with_unscoped_platform_registry_is_not_discovered(monkeypatch):
    """Agent 4e1b2e436c alone: per-home plugin managers but a global platform registry."""
    registry = types.SimpleNamespace(all_entries=lambda: list(_PLUGIN_ENTRIES))
    calls = _install(monkeypatch, registry=registry)
    assert _values(_call(monkeypatch)) == ["local", "origin", "webhook", "wecom_callback"]
    assert calls["discover"] == 0


def test_cron_picker_does_not_cache_delivery_options_across_profiles():
    """The delivery list is per profile now; a page-global cache would keep the first
    profile's list after an in-page profile switch."""
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    assert "_cronDeliveryOptionsCache" not in src
    start = src.index("async function _populateCronDeliverOptions(")
    body = src[start:src.index("\nasync function ", start + 10)]
    assert "api('/api/crons/delivery-options')" in body
    assert "requestedProfile" in body and "S.activeProfile" in body
    assert "return _populateCronDeliverOptions(selectedValue, isEdit)" in body
