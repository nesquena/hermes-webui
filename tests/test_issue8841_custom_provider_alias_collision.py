"""#8841: two custom provider records claiming one alias fail closed.

#8690's guard (:func:`provider_name_ambiguity_verdict`) only refuses names that
collide with a BUILT-IN provider, so the custom-vs-custom case was outside it:
``providers['custom:x_ai']`` beside a second record whose display name is
``x_ai`` made :func:`_select_custom_provider_record` return the exact keyed
record without noticing the second claimant. The Agent keys one credential pool
by display name, so both records share ``pool:custom:<name>`` and a chat turn
could pair one record's endpoint with the other record's API key.

Selection now refuses, naming both records, exactly as #8690 does for built-in
collisions. Built-in-spelled claimants stay with the #8690 guard so an
already-terminal route is not refused twice. Synthetic keys, ``.invalid`` URLs,
no network, no Agent.
"""

from __future__ import annotations

import copy
import sys
import types

import pytest

from api import config

URL_A, KEY_A = "https://record-a.invalid/v1", "synthetic-key-a"
URL_B, KEY_B = "https://record-b.invalid/v1", "synthetic-key-b"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    # The raw-identity source reads the active profile's config.yaml; point it
    # at a per-test file nothing here creates.
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(tmp_path / "profile" / "config.yaml"))
    # No Agent in this environment: stub the modules api.config probes for.
    pkg = types.ModuleType("hermes_cli")
    pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", types.ModuleType("hermes_cli.runtime_provider"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", types.ModuleType("hermes_cli.config"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", types.ModuleType("agent.secret_scope"))
    monkeypatch.setattr(config._thread_ctx, "env", {}, raising=False)
    monkeypatch.setattr(config._thread_ctx, "block_process_env_fallback", True, raising=False)


def _bind(monkeypatch, data):
    monkeypatch.setattr(config, "cfg", data)
    monkeypatch.setattr(config, "get_config", lambda: data)
    monkeypatch.setattr(config, "get_config_snapshot", lambda: copy.deepcopy(data))


def _record_a():
    return {"base_url": URL_A, "api_key": KEY_A}


def _record_b(**extra):
    return {"base_url": URL_B, "api_key": KEY_B, **extra}


def _config(pid, providers, **model_extra):
    return {"model": {"provider": pid, "default": "synthetic-model", **model_extra}, "providers": providers}


def _issue_config(order):
    """The exact config from the issue's report, in both dict orders."""
    record_a = ("custom:x_ai", _record_a())
    record_b = ("myproxy", {"name": "x_ai", **_record_b()})
    providers = dict([record_a, record_b] if order == "keyed-first" else [record_b, record_a])
    return _config("custom:x_ai", providers)


def _select(pid, cfg_data):
    return config._select_custom_provider_record(pid, config._custom_provider_slug_key(pid), cfg_data)


def _refusal(cfg_data, pid="custom:x_ai"):
    with pytest.raises(config.AmbiguousCustomProviderError) as exc:
        _select(pid, cfg_data)
    return exc.value.message


# ── The reported case: both dict orders ───────────────────────────────────────


@pytest.mark.parametrize("order", ["keyed-first", "proxy-first"])
def test_issue_config_refuses_and_names_both_records(monkeypatch, order):
    cfg = _issue_config(order)
    _bind(monkeypatch, cfg)
    text = _refusal(cfg)
    assert "'custom:x_ai'" in text and "'myproxy'" in text
    # The actionable rename text reaches the user; no credential travels with it.
    assert KEY_A not in text and KEY_B not in text


def test_refusal_message_names_both_records_and_the_rename_fix(monkeypatch):
    cfg = _issue_config("keyed-first")
    _bind(monkeypatch, cfg)
    text = _refusal(cfg)
    assert text.startswith(
        "Custom provider records ['custom:x_ai', 'myproxy'] under providers: all name the "
        "provider slug 'x_ai'; an endpoint and API key could be resolved from different "
        "records. Rename one so each custom provider has a unique slug."
    )


def test_connection_resolution_refuses_the_turn(monkeypatch):
    """The credential lookup runs the same authority, so it fails closed too."""
    _bind(monkeypatch, _issue_config("proxy-first"))
    with pytest.raises(config.AmbiguousCustomProviderError) as exc:
        config.resolve_custom_provider_connection("custom:x_ai")
    assert "'custom:x_ai'" in exc.value.message and "'myproxy'" in exc.value.message


def test_model_resolution_refuses_the_turn(monkeypatch):
    """resolve_model_provider's point-of-return guard raises before returning."""
    _bind(monkeypatch, _issue_config("keyed-first"))
    with pytest.raises(config.AmbiguousCustomProviderError) as exc:
        config.resolve_model_provider("synthetic-model")
    assert "'custom:x_ai'" in exc.value.message and "'myproxy'" in exc.value.message


# ── Which shapes really collide ───────────────────────────────────────────────


@pytest.mark.parametrize("pid, providers, claimants", [
    # keyed record vs the other record's display name (the issue's case)
    ("custom:x_ai", {"custom:x_ai": _record_a(), "myproxy": {"name": "x_ai", **_record_b()}},
     ["custom:x_ai", "myproxy"]),
    # case-folded display name normalizes to the same slug
    ("custom:x_ai", {"custom:x_ai": _record_a(), "myproxy": {"name": "X_AI", **_record_b()}},
     ["custom:x_ai", "myproxy"]),
    # display name carrying the custom: prefix claims the keyed identity too
    ("custom:x_ai", {"custom:x_ai": _record_a(), "myproxy": {"name": "custom:x_ai", **_record_b()}},
     ["custom:x_ai", "myproxy"]),
    # display name vs display name (two raw records naming one identity)
    ("custom:x_ai", {"proxy-a": {"name": "x_ai", **_record_a()}, "proxy-b": {"name": "x_ai", **_record_b()}},
     ["proxy-a", "proxy-b"]),
    # an arbitrary name in place of x_ai, as the issue notes
    ("custom:foo", {"custom:foo": _record_a(), "myproxy": {"name": "foo", **_record_b()}},
     ["custom:foo", "myproxy"]),
], ids=[
    "keyed-vs-display-name", "case-folded-display-name", "custom-prefixed-display-name",
    "display-name-vs-display-name", "arbitrary-name",
])
def test_two_records_claiming_one_alias_are_refused(monkeypatch, pid, providers, claimants):
    cfg = _config(pid, providers)
    _bind(monkeypatch, cfg)
    text = _refusal(cfg, pid)
    for claimant in claimants:
        assert f"'{claimant}'" in text
    assert KEY_A not in text and KEY_B not in text


def test_both_keyed_and_named_spellings_of_one_slug_collide(monkeypatch):
    """A bare ``x_ai`` key beside ``custom:x_ai`` is the built-in-spelled shape
    the #8690 guard already refuses; selection itself must not double-refuse."""
    cfg = _config("custom:x_ai", {"custom:x_ai": _record_a(), "x_ai": _record_b()})
    _bind(monkeypatch, cfg)
    record, _source, _is_exact, status = _select("custom:x_ai", cfg)
    assert (record["base_url"], record["api_key"]) == (URL_A, KEY_A)
    verdict = config.provider_name_ambiguity_verdict("custom:x_ai", cfg)
    assert verdict is not None and verdict["reason"] == config.PROVIDER_NAME_AMBIGUOUS
    assert "the built-in 'x-ai'" in verdict["message"] and "'custom:x_ai'" in verdict["message"]


# ── Control cases: behavior that must not change ──────────────────────────────


def test_non_colliding_custom_records_still_route(monkeypatch):
    """Two custom records with distinct aliases keep their own endpoint+key."""
    cfg = _config("custom:x_ai", {"custom:x_ai": _record_a(), "custom:other": _record_b()})
    _bind(monkeypatch, cfg)
    record, _source, _is_exact, status = _select("custom:x_ai", cfg)
    assert (record["base_url"], record["api_key"]) == (URL_A, KEY_A)
    assert status == config.CUSTOM_SELECTION_KEYED
    other, _source2, _exact2, status2 = _select("custom:other", cfg)
    assert (other["base_url"], other["api_key"]) == (URL_B, KEY_B)
    assert status2 == config.CUSTOM_SELECTION_KEYED


def test_sole_record_claiming_the_alias_is_not_ambiguous(monkeypatch):
    """One claimant is the normal case, never an ambiguity (the sole-row rule)."""
    cfg = _config("custom:x_ai", {"custom:x_ai": _record_a()})
    _bind(monkeypatch, cfg)
    record, source, is_exact, status = _select("custom:x_ai", cfg)
    assert (record["base_url"], record["api_key"]) == (URL_A, KEY_A)
    assert (source, is_exact, status) == ("providers", False, config.CUSTOM_SELECTION_KEYED)


def test_builtin_collision_stays_with_the_8690_guard(monkeypatch):
    """#8690 owns custom:<builtin> vs providers.<builtin>: terminal verdict,
    not a second refusal shape from selection."""
    cfg = _config("custom:openai", {
        "openai": _record_a(),
        "custom:openai": _record_b(),
    })
    _bind(monkeypatch, cfg)
    record, _source, _is_exact, _status = _select("custom:openai", cfg)
    assert (record["base_url"], record["api_key"]) == (URL_B, KEY_B)
    verdict = config.provider_name_ambiguity_verdict("custom:openai", cfg)
    assert verdict is not None and verdict["reason"] == config.PROVIDER_NAME_AMBIGUOUS
    assert verdict["message"].startswith(
        "Two providers claim 'openai': the built-in 'openai' and 'custom:openai'."
    )


def test_underscore_and_hyphen_spellings_are_distinct_slugs(monkeypatch):
    """``x_ai`` and ``x-ai`` normalize to different slugs: not a collision, and
    each route keeps its own record."""
    cfg = _config("custom:x_ai", {
        "custom:x_ai": _record_a(),
        "myproxy": {"name": "x-ai", **_record_b()},
    })
    _bind(monkeypatch, cfg)
    record, *_rest = _select("custom:x_ai", cfg)
    assert (record["base_url"], record["api_key"]) == (URL_A, KEY_A)
    other, *_rest2 = _select("custom:x-ai", cfg)
    assert (other["base_url"], other["api_key"]) == (URL_B, KEY_B)


def test_a_disabled_record_claims_nothing(monkeypatch):
    """The Agent skips a disabled keyed record, so it is not a second claimant."""
    cfg = _config("custom:x_ai", {
        "custom:x_ai": _record_a(),
        "myproxy": {"name": "x_ai", **_record_b(), "enabled": False},
    })
    _bind(monkeypatch, cfg)
    record, *_rest = _select("custom:x_ai", cfg)
    assert (record["base_url"], record["api_key"]) == (URL_A, KEY_A)


def test_a_claimant_without_connection_fields_is_not_an_authority(monkeypatch):
    """A record that names the alias but declares nothing neither wins nor blocks."""
    cfg = _config("custom:x_ai", {
        "custom:x_ai": _record_a(),
        "myproxy": {"name": "x_ai", "models": ["synthetic-model"]},
    })
    _bind(monkeypatch, cfg)
    record, *_rest = _select("custom:x_ai", cfg)
    assert (record["base_url"], record["api_key"]) == (URL_A, KEY_A)


def test_a_record_naming_a_different_slug_is_not_a_claimant(monkeypatch):
    """An unrelated display name must not turn a clean config ambiguous."""
    cfg = _config("custom:x_ai", {
        "custom:x_ai": _record_a(),
        "myproxy": {"name": "other_proxy", **_record_b()},
    })
    _bind(monkeypatch, cfg)
    record, *_rest = _select("custom:x_ai", cfg)
    assert (record["base_url"], record["api_key"]) == (URL_A, KEY_A)
