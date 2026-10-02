# Copyright 2025 the Hermes WebUI contributors
# SPDX-License-Identifier: MIT

"""Regression tests for GitHub issue #7955.

Symptom: with ``model.provider: ollama`` (or any provider the alias tables
collapse to ``custom``) plus a ``model.base_url``, a session persisted on the
generic ``custom`` provider failed to start:

    Custom provider 'custom:qwen3.8' is not configured

Root cause: the WebUI aliases the active provider to ``custom`` for local
endpoints, and the session remembers the picked row as ``custom``. In
``model_with_provider_context()`` the ``provider == config_provider`` guard
then compares the aliased ``custom`` against the raw configured ``ollama`` and
does not match, so a colon-tagged local model id (``qwen3.8:27b``) fell
through to the final ``@custom:<model>`` emission, and
``resolve_model_provider()`` read the first colon segment (``qwen3.8``) as a
named-provider slug.

Fix: emit the CONFIGURED provider as the hint (``@ollama:<model>``). A bare
model would fix the slug parse but lose the session's endpoint: the bare id
runs through the custom_providers[] / providers: ownership scans, so another
configured endpoint listing the same id would take the request (maintainer
review on #7966 / #7967). The duplicate-id tests below pin that the
configured endpoint stays authoritative for untagged and tagged ids, through
both config shapes.

This is the inverse-precondition sibling of the custom-colon-parse family
(#7904/#7240/#6648): those require the default provider to NOT be on a custom
route; #7955 requires it to BE one.
"""

import pytest

import api.config as cfg_mod
from api.config import model_with_provider_context, resolve_model_provider

OLLAMA_URL = 'http://127.0.0.1:11434/v1'
LOCAL_URL = 'http://127.0.0.1:1234/v1'
LAB_URL = 'http://10.0.0.8:8000/v1'
LAB_MODELS = ['mistral-7b', 'qwen3.8:27b']


@pytest.fixture
def ollama_default(monkeypatch):
    """``model.provider: ollama`` + base_url, with the agent's alias mapping.

    Production precondition: the agent alias table maps ``ollama`` to
    ``custom`` for local endpoints. Patch the resolver to that mapping so the
    tests hold with or without the agent on ``sys.path`` (CI).
    """
    monkeypatch.setattr(
        cfg_mod, '_resolve_provider_alias',
        lambda name: 'custom' if str(name or '').strip().lower() == 'ollama' else name,
    )
    monkeypatch.setitem(cfg_mod.cfg, 'model', {'provider': 'ollama', 'base_url': OLLAMA_URL})
    monkeypatch.delitem(cfg_mod.cfg, 'custom_providers', raising=False)
    monkeypatch.delitem(cfg_mod.cfg, 'providers', raising=False)


def _add_lab(monkeypatch, shape):
    """Add a second endpoint ``lab`` that ALSO lists the Ollama session's ids."""
    if shape == 'custom_providers':
        monkeypatch.setitem(cfg_mod.cfg, 'custom_providers', [
            {'name': 'lab', 'base_url': LAB_URL, 'models': list(LAB_MODELS)},
        ])
    else:
        monkeypatch.setitem(cfg_mod.cfg, 'providers', {
            'lab': {'base_url': LAB_URL, 'models': list(LAB_MODELS)},
        })


def test_ollama_alias_custom_session_routes_to_configured_endpoint(ollama_default):
    """#7955: colon-tagged id on a ``custom`` session under an Ollama default."""
    wrapped = model_with_provider_context('qwen3.8:27b', 'custom')
    assert wrapped == '@ollama:qwen3.8:27b', (
        f'Expected the configured-provider hint, got {wrapped!r}. A synthetic '
        '@custom: qualifier would make resolve_model_provider() parse the '
        'version tag as a provider slug.'
    )
    assert resolve_model_provider(wrapped) == ('qwen3.8:27b', 'ollama', OLLAMA_URL)


@pytest.mark.parametrize('shape', ['custom_providers', 'providers'])
@pytest.mark.parametrize('model', ['mistral-7b', 'qwen3.8:27b'])
def test_duplicate_id_keeps_configured_endpoint(ollama_default, monkeypatch, shape, model):
    """Another endpoint listing the same id must not take a ``custom``-lane
    Ollama request, for an untagged and a tagged id, through
    ``custom_providers[]`` and through ``providers:``."""
    _add_lab(monkeypatch, shape)
    wrapped = model_with_provider_context(model, 'custom')
    resolved = resolve_model_provider(wrapped)
    assert resolved == (model, 'ollama', OLLAMA_URL), (
        f'{model!r} picked from the Ollama lane resolved to {resolved!r}; '
        f'it must not move to the duplicate {shape} entry at {LAB_URL}.'
    )


def test_ollama_only_model_round_trip(ollama_default, monkeypatch):
    """Control: an id only Ollama offers resolves to Ollama with a duplicate
    endpoint configured beside it."""
    _add_lab(monkeypatch, 'custom_providers')
    wrapped = model_with_provider_context('llama3', 'custom')
    assert resolve_model_provider(wrapped) == ('llama3', 'ollama', OLLAMA_URL)


def test_named_custom_session_under_ollama_default(ollama_default, monkeypatch):
    """Control: a session that picked the named ``custom:lab`` row still
    reaches ``lab``. The #7955 change is scoped to bare ``custom``."""
    _add_lab(monkeypatch, 'custom_providers')
    wrapped = model_with_provider_context('mistral-7b', 'custom:lab')
    assert wrapped == '@custom:lab:mistral-7b'
    assert resolve_model_provider(wrapped) == ('mistral-7b', 'custom:lab', LAB_URL)


@pytest.fixture
def local_default(monkeypatch):
    """Legacy ``model.provider: local`` + base_url, on the WebUI's own alias
    table, unpatched."""
    monkeypatch.setitem(cfg_mod.cfg, 'model', {'provider': 'local', 'base_url': LOCAL_URL})
    monkeypatch.delitem(cfg_mod.cfg, 'custom_providers', raising=False)
    monkeypatch.delitem(cfg_mod.cfg, 'providers', raising=False)


def test_local_alias_custom_session_routes_to_configured_endpoint(local_default):
    """Legacy ``local`` takes the configured-provider hint, and
    resolve_model_provider() heals it to ``custom`` (#1384) while keeping the
    local base_url."""
    wrapped = model_with_provider_context('llama-3.4:8b', 'custom')
    assert wrapped == '@local:llama-3.4:8b'
    assert resolve_model_provider(wrapped) == ('llama-3.4:8b', 'custom', LOCAL_URL)


@pytest.mark.parametrize('shape', ['custom_providers', 'providers'])
@pytest.mark.parametrize('model', ['mistral-7b', 'qwen3.8:27b'])
def test_local_duplicate_id_keeps_configured_endpoint(local_default, monkeypatch, shape, model):
    """Another endpoint listing the same id must not take a ``custom``-lane
    legacy ``local`` request, and ``local`` itself must never be returned."""
    _add_lab(monkeypatch, shape)
    wrapped = model_with_provider_context(model, 'custom')
    resolved = resolve_model_provider(wrapped)
    assert resolved == (model, 'custom', LOCAL_URL), (
        f'{model!r} picked from the legacy local lane resolved to {resolved!r}; '
        f'it must not move to the duplicate {shape} entry at {LAB_URL}.'
    )


def test_non_custom_alias_keeps_named_hint(monkeypatch):
    """Control: a named custom session under a NON-custom-alias configured
    provider keeps minting the ``@custom:<slug>:`` hint."""
    monkeypatch.setitem(cfg_mod.cfg, 'model', {'provider': 'anthropic'})
    monkeypatch.setitem(cfg_mod.cfg, 'custom_providers', [{
        'name': 'ds2api',
        'base_url': 'http://ds2api:5001/v1/',
        'models': {'my-private-model': {}},
    }])
    wrapped = model_with_provider_context('my-private-model', 'custom:ds2api')
    assert wrapped == '@custom:ds2api:my-private-model'
    model, provider, base_url = resolve_model_provider(wrapped)
    assert provider == 'custom:ds2api'
    assert base_url == 'http://ds2api:5001/v1/'


def test_same_provider_bare_passthrough_unchanged(ollama_default):
    """Existing contract: a session already on the configured provider keeps
    the id bare."""
    assert model_with_provider_context('deepseek-r1:14b', 'ollama') == 'deepseek-r1:14b'
