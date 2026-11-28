"""#9255 gate: sibling enablement is judged with Agent-compatible env expansion.

The sibling audit in :func:`_raise_on_native_alias_collision` (and the sibling
views of :func:`_unique_raw_provider_record` / :func:`_native_custom_provider_record`)
used to call :func:`_raw_provider_record_enabled` on the LITERAL ``enabled`` flag.
The Agent judges that flag after its own env expansion on the owning profile:
``${VAR}`` and ``${env:VAR}`` (inner whitespace included) read the profile
environment, and a sibling whose flag expands to a false word is skipped by the
actual installed Agent's resolver. WebUI leaving the reference literal treated
that sibling as enabled and refused a valid exact-key provider with
``AmbiguousCustomProviderError`` — a regression the gate reproduced on the
pre-fix head.

The repair reads the flag through :func:`_agent_provider_record_enabled`, the
SAME owning-profile, Agent-compatible expansion the built-in ambiguity guard
(:func:`_provider_name_ambiguity_in`) already used, so the two audits cannot
drift apart again.

These tests drive the real WebUI config loader/cache with a real YAML file on
disk (the "YAML-backed controls" the gate asks for) plus a stand-in for the
installed Agent's ``hermes_cli.config._expand_env_vars`` implemented to the same
contract (env refs read the environment; unset / non-env refs stay verbatim), so
CI without an installed Agent still exercises the exact expansion rule. Each
shape goes through the selector, the connection boundary and two runtime-bundle
constructors, asserting the exact record's endpoint/key survives.
"""

from __future__ import annotations

import copy
import os
import re as _re
import sys
import types

import pytest
import yaml

from api import config, routes, streaming

URL_EXACT = "https://exact-key.invalid/v1"
KEY_EXACT = "synthetic-exact-key"
URL_SIBLING = "https://sibling.invalid/v1"
KEY_SIBLING = "synthetic-sibling-key"
FLAG_ENV = "X_AI_SIBLING_ENABLED"


# ── Agent stand-ins (same contract as test_issue8690) ─────────────────────────


def _install_agent_expander(monkeypatch):
    """A stand-in for the installed Agent's ``hermes_cli.config._expand_env_vars``.

    Same contract as the Agent (``_env_expand_match``): ``${VAR}`` /
    ``${env:VAR}`` read the environment (inner whitespace trimmed), an unset or
    non-env (``${file:...}``) reference stays verbatim.
    """

    def expand(obj):
        def one(match):
            inner = match.group(1).strip()
            if inner.startswith("env:"):
                name = inner[len("env:"):].strip()
            elif _re.match(r"^[a-z][a-z0-9_-]*:", inner):
                name = None
            else:
                name = inner
            value = os.environ.get(name) if name else None
            return match.group(0) if value is None else value

        return _re.sub(r"\$\{([^}]+)\}", one, obj) if isinstance(obj, str) else obj

    module = sys.modules.get("hermes_cli.config") or types.ModuleType("hermes_cli.config")
    monkeypatch.setattr(module, "_expand_env_vars", expand, raising=False)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", module)
    _install_secret_scope(monkeypatch, {})


def _install_secret_scope(monkeypatch, mapping):
    """``agent.secret_scope.current_secret_scope``: a bound profile scope."""
    scope = types.ModuleType("agent.secret_scope")
    scope.current_secret_scope = lambda: mapping
    monkeypatch.setitem(sys.modules, "agent.secret_scope", scope)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(tmp_path / "profile" / "config.yaml"))
    pkg = types.ModuleType("hermes_cli")
    pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", types.ModuleType("hermes_cli.runtime_provider"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", types.ModuleType("hermes_cli.config"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", types.ModuleType("agent.secret_scope"))
    monkeypatch.setattr(config._thread_ctx, "env", {}, raising=False)
    # The WebUI loader expands plain ``${VAR}`` references against the process
    # env (it does not know the ``env:`` prefix), so the loader path must be
    # allowed to do that — the gate's "real YAML through the loader" matrix
    # depends on plain-env refs actually expanding here.
    monkeypatch.setattr(config._thread_ctx, "block_process_env_fallback", False, raising=False)
    _install_agent_expander(monkeypatch)
    monkeypatch.setenv(FLAG_ENV, "false")
    # The requested provider's ambient conventional key must not exist: it
    # belongs to the exact record, never to the sibling.
    monkeypatch.delenv("CUSTOM_X_AI_API_KEY", raising=False)


def _doc(flag, *, with_sibling: bool = True):
    """The real YAML document for one ``enabled`` value.

    ``flag`` is the Python value to serialize: a bool, or a string holding the
    literal word / env reference. ``yaml.safe_dump`` emits it as a proper YAML
    scalar, so a reference is never mis-parsed as a flow mapping.
    """
    data = {
        "model": {"provider": "custom:x_ai", "default": "synthetic-model"},
        "providers": {
            "custom:x_ai": {"base_url": URL_EXACT, "api_key": KEY_EXACT},
        },
    }
    if with_sibling:
        data["providers"]["myproxy"] = {
            "name": "x_ai",
            "enabled": flag,
            "base_url": URL_SIBLING,
            "api_key": KEY_SIBLING,
        }
    return data


def _load_through_webui(monkeypatch, tmp_path, data):
    """Write real YAML, load it through the WebUI loader (which env-expands
    plain ``${VAR}`` refs in its cache view), and bind the module cache.

    All three bindings go through ``monkeypatch`` so teardown restores the
    module's real view — a raw assignment here leaked into later files' config
    (test_issue5220's reload-stampede assertions).
    """
    path = tmp_path / "profile" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    loaded = config._load_yaml_config_file(path)
    monkeypatch.setattr(config, "cfg", loaded)
    monkeypatch.setattr(config, "get_config", lambda: loaded)
    monkeypatch.setattr(config, "get_config_snapshot", lambda: copy.deepcopy(loaded))
    return loaded


def _runtime():
    """The runtime dict a send path would resolve for the exact record."""
    return {
        "provider": "custom", "api_mode": "chat_completions",
        "base_url": URL_EXACT, "api_key": KEY_EXACT, "credential_pool": None,
    }


def _assert_exact_preserved(monkeypatch, tmp_path, flag):
    """Selector, connection and both runtime bundles keep the exact endpoint/key."""
    data = _load_through_webui(monkeypatch, tmp_path, _doc(flag))
    pid = "custom:x_ai"
    slug = config._custom_provider_slug_key(pid)

    record, _source, _exact, _status = config._select_custom_provider_record(pid, slug, data)
    assert record is not None, "the exact-key provider must still own the route"
    assert record.get("base_url") == URL_EXACT
    assert record.get("api_key") == KEY_EXACT

    api_key, base_url = config.resolve_custom_provider_connection(pid)
    assert base_url == URL_EXACT and api_key == KEY_EXACT

    bundle = streaming._resolve_runtime_connection_bundle(
        pid, KEY_EXACT, URL_EXACT, _runtime(), custom_provider_lookup=pid,
    )
    final = config.raise_for_custom_provider_route(bundle)
    assert final["base_url"] == URL_EXACT and final["api_key"] == KEY_EXACT

    routes_bundle = routes._resolve_agent_connection_bundle(
        pid, KEY_EXACT, URL_EXACT, _runtime(),
    )
    routes_final = config.raise_for_custom_provider_route(routes_bundle)
    assert routes_final["base_url"] == URL_EXACT and routes_final["api_key"] == KEY_EXACT


# ── The gate matrix: every disabled spelling keeps the exact record ───────────


@pytest.mark.parametrize("flag", [
    False,                                    # literal boolean false
    "false",                                  # literal string false
    "${X_AI_SIBLING_ENABLED}",                # plain environment reference
    "${env:X_AI_SIBLING_ENABLED}",            # env-prefixed reference
    "${env: X_AI_SIBLING_ENABLED}",           # …with inner whitespace
    "${ env:X_AI_SIBLING_ENABLED}",           # …with outer whitespace
    "${ env: X_AI_SIBLING_ENABLED }",         # …both
], ids=[
    "literal-bool-false", "literal-string-false", "plain-env",
    "env-ref", "env-ref-inner-ws", "env-ref-outer-ws", "env-ref-both-ws",
])
def test_disabled_sibling_never_refuses_the_exact_provider(monkeypatch, tmp_path, flag):
    """A sibling the actual Agent skips (its flag expands to false) claims nothing."""
    _assert_exact_preserved(monkeypatch, tmp_path, flag)


# ── Controls: the collision goal still holds when the sibling is enabled ──────


@pytest.mark.parametrize("flag", [
    True,                                     # literal boolean true
    "true",                                   # literal string true
    "${X_AI_SIBLING_ENABLED}",                # plain env, resolved true
    "${env:X_AI_SIBLING_ENABLED}",            # env-prefixed, resolved true
    "${env: X_AI_SIBLING_ENABLED}",           # env-prefixed + inner whitespace, true
], ids=["literal-bool-true", "literal-string-true", "plain-env-true",
        "env-ref-true", "env-ref-ws-true"])
def test_enabled_sibling_still_refuses_the_turn(monkeypatch, tmp_path, flag):
    """A sibling whose flag reads enabled is a real second claimant: refused."""
    if isinstance(flag, str) and "${" in flag:
        monkeypatch.setenv(FLAG_ENV, "true")
    data = _load_through_webui(monkeypatch, tmp_path, _doc(flag))
    pid = "custom:x_ai"
    slug = config._custom_provider_slug_key(pid)
    with pytest.raises(config.AmbiguousCustomProviderError) as exc:
        config._select_custom_provider_record(pid, slug, data)
    assert "'custom:x_ai'" in exc.value.message and "'myproxy'" in exc.value.message


def test_unset_env_reference_stays_unknown_and_keeps_claiming(monkeypatch, tmp_path):
    """An unset reference stays verbatim for the Agent, and the unknown flag is
    fail-closed: the sibling keeps claiming (the built-in guard's reading), so
    the collision is still refused."""
    monkeypatch.delenv(FLAG_ENV, raising=False)
    data = _load_through_webui(monkeypatch, tmp_path, _doc("${env:X_AI_SIBLING_ENABLED}"))
    pid = "custom:x_ai"
    slug = config._custom_provider_slug_key(pid)
    with pytest.raises(config.AmbiguousCustomProviderError) as exc:
        config._select_custom_provider_record(pid, slug, data)
    assert "'custom:x_ai'" in exc.value.message and "'myproxy'" in exc.value.message


def test_no_agent_expander_keeps_the_templated_sibling_claiming(monkeypatch, tmp_path):
    """Without the Agent's expander the flag's value is unknown, so the sibling
    keeps claiming (fail closed) — never a silent acceptance of the route."""
    monkeypatch.setenv(FLAG_ENV, "false")
    # Drop the stand-in: the stub has no ``_expand_env_vars`` -> ImportError path.
    monkeypatch.setitem(sys.modules, "hermes_cli.config", types.ModuleType("hermes_cli.config"))
    data = _load_through_webui(monkeypatch, tmp_path, _doc("${env:X_AI_SIBLING_ENABLED}"))
    pid = "custom:x_ai"
    slug = config._custom_provider_slug_key(pid)
    with pytest.raises(config.AmbiguousCustomProviderError) as exc:
        config._select_custom_provider_record(pid, slug, data)
    assert "'custom:x_ai'" in exc.value.message and "'myproxy'" in exc.value.message


@pytest.mark.parametrize("flag", [
    False,
    "${X_AI_SIBLING_ENABLED}",
    "${env:X_AI_SIBLING_ENABLED}",
    "${ env: X_AI_SIBLING_ENABLED }",
], ids=["literal", "plain-env", "env-ref", "env-ref-ws"])
def test_no_sibling_control_stays_clean(monkeypatch, tmp_path, flag):
    """Without the sibling there is never a collision, whatever the flag spelling."""
    data = _load_through_webui(monkeypatch, tmp_path, _doc(flag, with_sibling=False))
    pid = "custom:x_ai"
    slug = config._custom_provider_slug_key(pid)
    record, *_rest = config._select_custom_provider_record(pid, slug, data)
    assert record is not None and record.get("base_url") == URL_EXACT
    assert record.get("api_key") == KEY_EXACT


@pytest.mark.parametrize("flag", [
    False,
    "${env:X_AI_SIBLING_ENABLED}",
], ids=["literal", "env-ref"])
def test_an_unrelated_sibling_is_never_a_claimant(monkeypatch, tmp_path, flag):
    """Control: a sibling naming a DIFFERENT slug is untouched by the flag rule."""
    data = _doc(flag)
    data["providers"]["myproxy"]["name"] = "other_slug"
    data = _load_through_webui(monkeypatch, tmp_path, data)
    pid = "custom:x_ai"
    slug = config._custom_provider_slug_key(pid)
    record, *_rest = config._select_custom_provider_record(pid, slug, data)
    assert record is not None and record.get("base_url") == URL_EXACT


# ── The other two sibling scans share the same rule ───────────────────────────


def test_env_disabled_unique_raw_owner_does_not_turn_the_scan_ambiguous(monkeypatch, tmp_path):
    """Without the exact canonical key, the raw scan is the one that finds owners.

    A sibling the Agent skips must not be counted as a second owner there
    either: the literal reading raised ``AmbiguousCustomProviderError`` for a
    config whose only visible owner is the plain ``x_ai`` row.
    """
    data = {
        "model": {"provider": "custom:x_ai"},
        "providers": {
            "x_ai": {"base_url": URL_EXACT, "api_key": KEY_EXACT},
            "myproxy": {
                "name": "x_ai", "enabled": "${env:X_AI_SIBLING_ENABLED}",
                "base_url": URL_SIBLING, "api_key": KEY_SIBLING,
            },
        },
    }
    loaded = _load_through_webui(monkeypatch, tmp_path, data)
    record, _source, _exact, _status = config._select_custom_provider_record(
        "custom:x_ai", config._custom_provider_slug_key("custom:x_ai"), loaded,
    )
    assert record is not None and record.get("base_url") == URL_EXACT
    assert record.get("api_key") == KEY_EXACT


def test_env_disabled_native_alias_is_not_an_authority(monkeypatch, tmp_path):
    """A disabled native-spelled record must not be selected as the route owner.

    The literal reading returned the disabled row's own endpoint/credential —
    using a record the actual Agent never sees.
    """
    data = {
        "model": {"provider": "custom:x_ai"},
        "providers": {
            "custom:x_ai_alias": {
                "name": "x_ai", "enabled": "${env:X_AI_SIBLING_ENABLED}",
                "base_url": URL_SIBLING, "api_key": KEY_SIBLING,
            },
        },
    }
    loaded = _load_through_webui(monkeypatch, tmp_path, data)
    record, _source, _exact, status = config._select_custom_provider_record(
        "custom:x_ai", config._custom_provider_slug_key("custom:x_ai"), loaded,
    )
    assert record is None
    assert status == config.CUSTOM_SELECTION_MISSING
