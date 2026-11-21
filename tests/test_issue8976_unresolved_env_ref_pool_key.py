"""Regression tests for #8976: a pool credential that is an unresolved ``${VAR}``.

A named profile reads only its own ``.env`` (#3961), so a ``${VAR}`` reference
that profile's ``.env`` does not define survives the Agent's credential-pool
lookup as the literal string. WebUI's ``_custom_record_pool_runtime`` used to
hand that literal straight to the connection settings, where it reached the
provider as the API key and surfaced as a confusing provider-side 401.

``api.profiles.expand_profile_config``'s own contract says a caller that
forwards a value must treat an unresolved ``${VAR}`` as absent
(:func:`api.config._has_env_ref`), so the runtime is dropped instead and the
existing "declares a credential source that produced no key" refusal names the
real cause.
"""

from __future__ import annotations

import sys
import types

import pytest

import api.config as config

_URL = "https://pooled.example.test/v1"


def _pool_stub(monkeypatch, runtime):
    """Point ``hermes_cli.runtime_provider``'s pool lookup at a test double.

    The runtime module is not importable in the WebUI test environment, and
    ``_custom_record_pool_runtime`` is documented as best-effort against a
    stubbed runtime module, so the lookup is installed directly in
    ``sys.modules`` — the same agent-free shape the repo's other
    credential-pool tests use.
    """
    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_module._try_resolve_from_custom_pool = lambda *a, **k: runtime
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.__path__ = []
    fake_hermes_cli.runtime_provider = fake_runtime_module
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime_module)


def _record(**fields):
    """A minimal ``custom_providers[]`` row that owns ``_URL``."""
    record = {"name": "pooled", "base_url": _URL}
    record.update(fields)
    return record


# ── the #8976 case: an unresolved reference is not a credential ──────────────


def test_pool_runtime_with_unresolved_env_ref_api_key_yields_no_runtime(monkeypatch):
    """A literal ``${VAR}`` api_key from the pool must not be forwarded."""
    _pool_stub(monkeypatch, {"api_key": "${MY_KEY}", "credential_pool": object()})

    assert config._custom_record_pool_runtime(_URL, _record()) is None


@pytest.mark.parametrize(
    "value",
    ["prefix-${MY_KEY}", "${MY_KEY}-suffix", "pre-${MY_KEY}-post", "${A}${B}"],
)
def test_env_ref_beside_other_characters_is_still_unresolved(monkeypatch, value):
    """``${...}`` anywhere in the value marks it unresolved, not only a bare ref."""
    _pool_stub(monkeypatch, {"api_key": value})

    assert config._custom_record_pool_runtime(_URL, _record()) is None, value


def test_unresolved_pool_key_falls_through_to_the_no_credential_refusal(monkeypatch):
    """End-to-end: the existing refusal applies instead of a literal-key 401."""
    cfg = {"custom_providers": [_record(api_key="${MY_KEY}")]}
    monkeypatch.setattr(config, "get_config", lambda *a, **k: cfg)
    monkeypatch.setattr(config, "cfg", cfg)
    _pool_stub(monkeypatch, {"api_key": "${MY_KEY}", "credential_pool": object()})

    bundle = config.merge_custom_provider_runtime_bundle("custom:pooled", None, None, None)

    assert "${MY_KEY}" not in str(bundle)
    assert bundle["api_key"] is None
    assert bundle["base_url"] == _URL
    verdict = config.custom_provider_route_error(bundle)
    assert verdict is not None, bundle
    assert verdict["reason"] == config.CUSTOM_ROUTE_NO_CREDENTIAL
    assert "produced no API key" in verdict["message"], verdict


# ── control cases: everything else keeps today's behaviour ───────────────────


def test_pool_runtime_with_a_real_api_key_is_forwarded_unchanged(monkeypatch):
    """A resolved pool credential still flows through untouched."""
    runtime = {"api_key": "***", "credential_pool": object()}
    _pool_stub(monkeypatch, runtime)

    assert config._custom_record_pool_runtime(_URL, _record()) is runtime


@pytest.mark.parametrize("api_key", ["", None])
def test_pool_runtime_with_an_empty_or_absent_api_key_is_forwarded_unchanged(
    monkeypatch, api_key
):
    """Empty/missing credentials were already forwarded as-is; keep them so."""
    runtime = {"api_key": api_key, "credential_pool": object()}
    _pool_stub(monkeypatch, runtime)

    assert config._custom_record_pool_runtime(_URL, _record()) is runtime


def test_pool_runtime_without_an_api_key_field_is_forwarded_unchanged(monkeypatch):
    """A runtime that names no api_key at all is unaffected."""
    runtime = {"credential_pool": object()}
    _pool_stub(monkeypatch, runtime)

    assert config._custom_record_pool_runtime(_URL, _record()) is runtime


@pytest.mark.parametrize(
    "value",
    [
        "$MY_KEY",
        "{MY_KEY}",
        "MY_KEY",
        "key$with$dollars",
        "${MY_KEY",
        "sk-!@#$%^&*()_+-=[]{}|;:,.<>?/~`",
    ],
)
def test_dollar_shapes_that_are_not_env_refs_are_forwarded_unchanged(monkeypatch, value):
    """Only ``${...}`` is a reference; ``$VAR`` / ``{VAR}`` are ordinary text."""
    runtime = {"api_key": value, "credential_pool": object()}
    _pool_stub(monkeypatch, runtime)

    assert config._custom_record_pool_runtime(_URL, _record()) is runtime, value


@pytest.mark.parametrize(
    "api_key",
    [
        "***",
        "key-with-\"double\"-quotes",
        "key-with-'single'-quotes",
        "键-非ASCII-中文",
        "ключ-кириллица",
        "emoji-🔑-key",
        "key with spaces",
        "key\twith\ttabs",
        "  padded-key  ",
        config.KEYLESS_CUSTOM_API_KEY,
        "x" * 512,
    ],
)
def test_resolvable_pool_api_keys_forward_byte_for_byte(monkeypatch, api_key):
    """Differential control: a real credential's runtime is not altered at all."""
    runtime = {"api_key": api_key, "credential_pool": object(), "extra": [1, 2, 3]}
    _pool_stub(monkeypatch, runtime)

    result = config._custom_record_pool_runtime(_URL, _record())

    assert result is runtime
    assert result == runtime
    assert list(result) == ["api_key", "credential_pool", "extra"]
