"""Regression coverage for issue #7611: per-deployment instance
label distinguishes multi-instance browser tabs and desktop
windows. The label is installation-scoped, sourced from the env
var ``HERMES_WEBUI_INSTANCE_NAME`` or ``config.yaml``'s
``instance_name`` / ``webui.instance_name``, and exposed via
``GET /api/settings`` as ``settings["instance_label"]`` so the
WebUI can prefix the title without overloading the assistant
name or the profile identity.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def _load_routes():
    # Import lazily so test collection stays cheap; the module is
    # the same one already loaded by sibling tests, so this is a
    # cache hit after the first call.
    import api.routes as routes
    return routes


def _purge_settings_cache():
    """``/api/settings`` calls ``load_settings()`` which may cache.
    We don't need a fresh load for these tests — we just need
    ``_read_instance_label()`` to re-evaluate env / config on
    every call. The helper itself reads from the current
    process env and ``get_config()`` each invocation, so no
    module-level cache needs clearing."""
    pass


# ── _read_instance_label: env precedence ──────────────────────────────────────


def test_read_instance_label_prefers_env_var(monkeypatch):
    """The env var is the primary source — it lets operators
    distinguish Production / Staging / Dev without editing
    config.yaml. Order of precedence is env > config > empty,
    so the env value always wins when set."""
    import api.routes as routes
    monkeypatch.setenv("HERMES_WEBUI_INSTANCE_NAME", "Production")
    monkeypatch.setattr(
        "api.config.get_config",
        lambda: {"instance_name": "ConfigShouldBeIgnored"},
        raising=False,
    )
    assert routes._read_instance_label() == "Production", (
        "env var must take precedence over config.yaml so operators "
        "can override without editing the file"
    )


def test_read_instance_label_strips_whitespace(monkeypatch):
    """The env var is allowed to have surrounding whitespace from
    shell quoting or env-file editors; the helper trims so
    ``'   Production  '`` does not render as ``'   Production  •
    Hermes'`` in the tab title."""
    import api.routes as routes
    monkeypatch.setenv("HERMES_WEBUI_INSTANCE_NAME", "   Staging   ")
    assert routes._read_instance_label() == "Staging"


def test_read_instance_label_falls_back_to_config_top_level(monkeypatch):
    """When the env var is unset, the helper falls back to
    ``config.yaml``'s top-level ``instance_name`` key. This
    matches the configuration shape suggested in the issue body."""
    import api.routes as routes
    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)
    monkeypatch.setattr(
        "api.config.get_config",
        lambda: {"instance_name": "Dev"},
        raising=False,
    )
    assert routes._read_instance_label() == "Dev"


def test_read_instance_label_falls_back_to_config_nested(monkeypatch):
    """The helper also accepts ``webui.instance_name`` because
    some operators prefer a sectioned config layout. The
    nested key is only consulted when the top-level key is
    absent or empty."""
    import api.routes as routes
    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)
    monkeypatch.setattr(
        "api.config.get_config",
        lambda: {"webui": {"instance_name": "Staging"}},
        raising=False,
    )
    assert routes._read_instance_label() == "Staging"


def test_read_instance_label_returns_empty_when_unset(monkeypatch):
    """When neither env var nor config keys are set, the helper
    returns the empty string. The frontend treats the empty
    string as "no label" and leaves the default title
    untouched — this is the contract that keeps single-instance
    deployments behaviorally identical to before this PR."""
    import api.routes as routes
    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)
    monkeypatch.setattr(
        "api.config.get_config",
        lambda: {"instance_name": "", "webui": {}},
        raising=False,
    )
    assert routes._read_instance_label() == "", (
        "empty/unset must round-trip as empty so the frontend can "
        "branch on length without a separate sentinel"
    )


def test_read_instance_label_handles_config_exception(monkeypatch):
    """``api.config.get_config`` may raise on a malformed
    config.yaml. The helper must catch and fall back to the
    empty default, not propagate the exception to the GET
    handler (which would surface a 500 to the client)."""
    import api.routes as routes
    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)

    def _raise():
        raise RuntimeError("config.yaml is malformed")

    monkeypatch.setattr("api.config.get_config", _raise, raising=False)
    assert routes._read_instance_label() == "", (
        "a config read failure must degrade to the empty default; "
        "/api/settings should not 500 because of a broken config"
    )


def test_read_instance_label_rejects_non_string_config_values(monkeypatch):
    """A non-string ``instance_name`` (int, dict, list) must
    not be coerced; the helper skips it and tries the next
    candidate. A future operator who accidentally sets
    ``instance_name: 42`` should not see ``'42 • Hermes'`` in
    their tab title."""
    import api.routes as routes
    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)
    monkeypatch.setattr(
        "api.config.get_config",
        lambda: {"instance_name": 42, "webui": {"instance_name": ["bad", "shape"]}},
        raising=False,
    )
    assert routes._read_instance_label() == "", (
        "non-string candidates must be skipped so a malformed "
        "config value cannot become a tab-title fragment"
    )


# ── frontend wiring ──────────────────────────────────────────────────────────


def test_instance_label_round_trips_through_window_global():
    """The boot path must read ``s.instance_label`` from the
    ``/api/settings`` response and pin it on
    ``window._instanceLabel`` so the synchronous ``applyBotName``
    and the later ``syncTopbar`` calls can both prefix the
    title without re-reading the settings payload."""
    src = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    assert "window._instanceLabel=(s.instance_label||'').trim();" in src, (
        "boot.js must read s.instance_label from /api/settings and "
        "expose it on window._instanceLabel"
    )


def test_apply_bot_name_prefixes_with_instance_label():
    """``applyBotName`` must consume ``window._instanceLabel``
    when writing ``document.title`` for the no-session branch
    so the empty-state tab title reflects the deployment."""
    src = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    body = _extract_function_body(src, "applyBotName")
    assert "window._instanceLabel" in body, (
        "applyBotName must consult window._instanceLabel"
    )
    assert "_instanceLabel+' \\u2022 '+name" in body or (
        "_instanceLabel+' • '+name"
    ) in body, (
        "applyBotName must prefix the document.title with "
        "'<label> • <name>' so the empty-state tab is distinguishable"
    )
    # Default behavior is preserved when the label is empty: the
    # helper returns the bare name with no prefix.
    assert "_titledName" in body, (
        "the helper must compute a titled-name once so both the "
        "empty and non-empty cases share the same code path"
    )


def test_sync_topbar_prefixes_with_instance_label():
    """``syncTopbar`` must consume the same label for both the
    no-session and with-session title paths so the tab title
    stays distinguishable while a session is open."""
    src = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    body = _extract_function_body(src, "syncTopbar")
    assert "window._instanceLabel" in body, (
        "syncTopbar must consult window._instanceLabel"
    )
    # Both title assignments (no-session and with-session) must
    # use the same titled-name helper so the prefix is consistent.
    assert body.count("_titledName(") >= 2, (
        "syncTopbar must use the _titledName helper for every "
        "document.title assignment; found fewer than two calls"
    )


def test_instance_label_is_not_writable_from_post_settings(monkeypatch):
    """The label is installation-scoped and must not be
    editable from the WebUI settings UI. The POST handler at
    ``/api/settings`` accepts ``bot_name`` and other user-tunable
    fields, but the ``instance_label`` field must not be
    consumed from the request body. A user submitting
    ``{instance_label: 'CustomLabel'}`` must not have it
    applied; only the env var and config.yaml control the
    value."""
    src = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    # The GET path reads the label from the helper; the POST
    # path must not interpret it. The simplest check: the
    # docstring for bot_name handling explicitly mentions the
    # field name, but instance_label must not appear in any
    # 'body[...]' reads inside the POST handler.
    # Concretely: find the POST /api/settings block and ensure
    # it does not branch on "instance_label" in body.
    assert '"instance_label" in body' not in src, (
        "instance_label must not be a writable body field on "
        "/api/settings POST — the value is installation-scoped"
    )
    # And the GET path must surface it from the helper.
    assert "settings[\"instance_label\"] = _read_instance_label()" in src, (
        "/api/settings GET must surface instance_label so the "
        "frontend can prefix the title"
    )


# ── helpers ─────────────────────────────────────────────────────────────────


def _extract_function_body(src: str, name: str) -> str:
    """Inline copy of the same helper used in the issue #7352
    test file — keeps this file self-contained and avoids
    pulling in a private test-utility module."""
    marker = f"function {name}("
    start = src.find(marker)
    assert start != -1, f"{name} not found"
    paren = src.find("(", start)
    assert paren != -1
    depth = 0
    for idx in range(paren, len(src)):
        ch = src[idx]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                brace = src.find("{", idx)
                break
    else:
        raise AssertionError(f"{name} params did not terminate")
    assert brace != -1
    depth = 0
    for idx in range(brace, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[brace + 1 : idx]
    raise AssertionError(f"{name} body did not terminate")
