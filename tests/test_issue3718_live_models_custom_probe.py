"""Handler-level regression tests for /api/models/live custom-provider probing.

These tests invoke the real ``routes._handle_live_models`` and inspect the
emitted ``models`` payload. Earlier revisions of this file re-implemented the
filtering loop locally, which meant they passed unchanged even with the
production filter deleted (a vacuous oracle). Every case here must fail if the
allowlist gating in ``api/routes.py`` regresses.

Covered shapes:
  * native ``models`` list                     → live catalog filtered
  * JSON-array-string ``models``               → live catalog filtered (#7120 class)
  * Python-literal-string ``models``           → live catalog filtered
  * singular ``model`` only, no ``models``     → live catalog NOT gated
  * malformed serialized ``models``            → safe, treated as scalar name
  * allowlisted model absent from live catalog → appended
  * live probe failure                         → falls back to config ids
"""

import io
import json
import sys
import types
from urllib.parse import urlparse

import pytest


def _install_provider_model_ids(monkeypatch, fn):
    """Stub hermes_cli.models.provider_model_ids (returns [] for custom:*)."""
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    models = types.ModuleType("hermes_cli.models")
    models.provider_model_ids = fn
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", models)


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


def _install_live_catalog(monkeypatch, routes, catalog, *, fail=False):
    """Stub the upstream /v1/models probe used by the custom-provider branch."""
    import urllib.request

    requested: list[str] = []

    def _fake_urlopen(req, timeout=None):
        requested.append(getattr(req, "full_url", str(req)))
        if fail:
            raise OSError("probe failed")
        body = {"data": [{"id": mid} for mid in catalog]}
        return _FakeResponse(json.dumps(body).encode())

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    return requested


def _run_live_models(monkeypatch, routes, custom_provider, *, catalog, fail=False):
    """Invoke the real handler for provider=custom:test-gateway."""
    import api.config as config
    import api.profiles as profiles

    routes._clear_live_models_cache()
    monkeypatch.setattr(
        routes, "j", lambda _handler, payload, status=200, extra_headers=None: payload
    )
    monkeypatch.setattr(config, "_resolve_provider_alias", lambda provider: provider)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")

    config_data = {
        "model": {"provider": "custom:test-gateway"},
        "custom_providers": [custom_provider],
    }
    monkeypatch.setattr(config, "get_config", lambda: config_data)
    monkeypatch.setattr(routes, "cfg", config_data, raising=False)

    # provider_model_ids() returns [] for custom:* → drives the custom branch.
    _install_provider_model_ids(monkeypatch, lambda provider: [])
    requested = _install_live_catalog(monkeypatch, routes, catalog, fail=fail)

    parsed = urlparse("/api/models/live?provider=custom:test-gateway")
    payload = routes._handle_live_models(object(), parsed)
    return payload, requested


def _ids(payload):
    return [m["id"] for m in payload.get("models", [])]


_LIVE_CATALOG = [
    "chat-a",
    "chat-b",
    "image-gen-1",   # non-chat noise the picker must not surface
    "audio-tts-1",
]

_BASE_PROVIDER = {
    "name": "Test Gateway",
    "base_url": "https://gateway.example/v1",
    "api_key": "sk-test",
}


@pytest.mark.parametrize(
    "models_value",
    [
        pytest.param(["chat-a", "chat-b"], id="native-list"),
        pytest.param('["chat-a","chat-b"]', id="json-array-string"),
        pytest.param("['chat-a', 'chat-b']", id="python-literal-string"),
        pytest.param({"chat-a": {}, "chat-b": {}}, id="mapping-metadata"),
        pytest.param(
            [{"id": "chat-a"}, {"model": "chat-b"}], id="list-of-dicts"
        ),
    ],
)
def test_allowlist_filters_live_catalog(monkeypatch, models_value):
    """An explicit plural allowlist must trim the live catalog to itself.

    The JSON-array-string and Python-literal cases are the regression: those
    shapes are what ``hermes config set`` / JSON-mode editor saves persist, and
    a dict/list-only parser drops them to ``[]`` → read as "no allowlist" → the
    full upstream catalog floods the picker.
    """
    import api.routes as routes

    provider = dict(_BASE_PROVIDER, models=models_value)
    payload, requested = _run_live_models(
        monkeypatch, routes, provider, catalog=_LIVE_CATALOG
    )

    assert requested, "live /v1/models probe was never attempted"
    assert _ids(payload) == ["chat-a", "chat-b"]
    assert "image-gen-1" not in _ids(payload)
    assert "audio-tts-1" not in _ids(payload)


def test_singular_model_only_does_not_gate_live_catalog(monkeypatch):
    """``model:`` is default metadata, not an allowlist — keep full discovery."""
    import api.routes as routes

    provider = dict(_BASE_PROVIDER, model="chat-a")
    payload, _ = _run_live_models(
        monkeypatch, routes, provider, catalog=_LIVE_CATALOG
    )

    assert _ids(payload) == _LIVE_CATALOG


def test_malformed_serialized_models_is_safe(monkeypatch):
    """A broken JSON-array string must not crash or silently drop the gate.

    ``_parse_config_string_list()`` treats an undecodable value as one literal
    name, so the allowlist stays non-empty and the filter still runs.
    """
    import api.routes as routes

    provider = dict(_BASE_PROVIDER, models='["chat-a", "chat-b"')  # missing ]
    payload, _ = _run_live_models(
        monkeypatch, routes, provider, catalog=_LIVE_CATALOG
    )

    # Treated as a single (unmatched) name → appended, catalog not flooded.
    assert "image-gen-1" not in _ids(payload)
    assert "audio-tts-1" not in _ids(payload)


def test_allowlisted_model_missing_from_live_is_appended(monkeypatch):
    """A declared model the upstream didn't return must still be selectable."""
    import api.routes as routes

    provider = dict(_BASE_PROVIDER, models=["chat-a", "chat-offline"])
    payload, _ = _run_live_models(
        monkeypatch, routes, provider, catalog=_LIVE_CATALOG
    )

    assert _ids(payload) == ["chat-a", "chat-offline"]


def test_contradictory_singular_model_outside_plural_allowlist_is_ignored(monkeypatch):
    """A singular default outside the plural allowlist must not leak into the picker.

    Paired test: whether the live probe succeeds or fails, only the explicit
    plural allowlist is exposed.
    """
    import api.routes as routes

    provider = dict(_BASE_PROVIDER, model="chat-default", models=["chat-a"])

    # 1. Successful live fetch -> only plural allowlist
    payload_success, _ = _run_live_models(
        monkeypatch, routes, provider, catalog=_LIVE_CATALOG, fail=False
    )
    assert _ids(payload_success) == ["chat-a"]

    # 2. Failed live fetch -> still only plural allowlist (not chat-default)
    payload_fail, _ = _run_live_models(
        monkeypatch, routes, provider, catalog=_LIVE_CATALOG, fail=True
    )
    assert _ids(payload_fail) == ["chat-a"]


def test_probe_failure_without_allowlist_falls_back_to_singular_model(monkeypatch):
    """When no plural allowlist is configured and probe fails, singular model is used."""
    import api.routes as routes

    provider = dict(_BASE_PROVIDER, model="chat-default")
    payload, _ = _run_live_models(
        monkeypatch, routes, provider, catalog=_LIVE_CATALOG, fail=True
    )
    assert _ids(payload) == ["chat-default"]


@pytest.mark.parametrize(
    "models_value",
    [
        pytest.param([], id="native-empty-list"),
        pytest.param("[]", id="serialized-empty-list"),
        pytest.param({}, id="empty-mapping"),
        pytest.param("   ", id="blank-scalar"),
    ],
)
def test_empty_allowlist_is_treated_as_not_configured(monkeypatch, models_value):
    """An empty ``models`` value must NOT be read as "allow nothing".

    Pins a deliberate semantic decision. Gating the filter on *declared-ness*
    rather than on a non-empty allowlist would make ``models: []`` emit an
    EMPTY picker, leaving the provider unselectable — a harder failure than
    surfacing a few extra models, and unrecoverable from the UI because
    ``custom_providers`` has no WebUI write path (hand-edited config.yaml only).
    There is also no deny-all use case: a provider the user wants hidden is
    removed from ``custom_providers`` outright.

    If someone later "fixes" the empty case into a deny-all gate, this test
    fails and forces the trade-off to be re-argued rather than silently shipped.
    """
    import api.routes as routes

    provider = dict(_BASE_PROVIDER, models=models_value)
    payload, _ = _run_live_models(
        monkeypatch, routes, provider, catalog=_LIVE_CATALOG
    )

    assert _ids(payload) == _LIVE_CATALOG, (
        "empty allowlist must fall through to full live discovery, "
        "never collapse the picker to zero models"
    )
    assert _ids(payload), "picker must never be emptied by an empty allowlist"
