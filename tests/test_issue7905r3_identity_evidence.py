"""#7905 round 3 — one identity-evidence rule for the router and for the GET refresh.

Two cues from the maintainer:

* gate certificate 2026-11-16T23:13:23Z — ``GET /api/session`` resolves an
  unchanged routed generic-custom selection with the session's stored provider
  evidence, then compares it with the stored identity through the older,
  evidence-free parser.  The unchanged selection therefore looks like a model
  change and the 256k metadata fallback replaces a larger persisted window
  (and rescales the threshold).
* follow-up comment 2026-11-16T23:20:26Z — the generic fallback decides "this
  name is already a configured identity" from checks that skip DISABLED records
  and records without connection fields, so a bare ``providers: {omni: ...}``
  or ``providers: {proxy: {name: omni}}`` identity falls through to the generic
  endpoint instead of staying on the named lane (which the route then refuses,
  as on master).

The rows below are the maintainer's own table.  ``providers.custom`` is the
generic record throughout, and every probe runs a ``custom`` session asking for
``@custom:omni:latest``.
"""
from __future__ import annotations

import copy
import sys
import types
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

import api.config as config
from api import routes
from api.config import (
    CustomProviderRouteError,
    _configured_custom_provider_identity,
    canonical_model_provider_lane,
    resolve_model_provider,
)

GENERIC_URL = "https://generic.example/v1"
OMNI_URL = "https://omni.example/v1"

# id -> (extra providers entries, expected lane, expected base_url when known)
ROWS = [
    (
        "1-none-peels-to-generic-7904",
        {},
        ("omni:latest", "custom"),
        GENERIC_URL,
    ),
    (
        "2-omni-with-connection",
        {"omni": {"base_url": OMNI_URL, "api_key": "fixture-omni"}},
        ("latest", "custom:omni"),
        OMNI_URL,
    ),
    (
        "3-exact-key-disabled",
        {"custom:omni": {"enabled": False, "base_url": OMNI_URL}},
        ("latest", "custom:omni"),
        OMNI_URL,
    ),
    (
        "4-omni-models-only",
        {"omni": {"models": ["latest"]}},
        ("latest", "custom:omni"),
        None,
    ),
    (
        "5-omni-disabled",
        # The identity owns the NAME (so the lane is named) but declares no usable
        # endpoint for a switched-off record: base_url stays None exactly as on
        # master, and the route chokepoint refuses. Widening _get_provider_cfg to
        # read the bare key here would be a routing change nobody asked for.
        {"omni": {"enabled": False, "base_url": OMNI_URL}},
        ("latest", "custom:omni"),
        None,
    ),
    (
        "6-proxy-name-only",
        {"proxy": {"name": "omni"}},
        ("latest", "custom:omni"),
        None,
    ),
]

ROW_IDS = [row[0] for row in ROWS]


def _cfg(extra_providers):
    providers = {"custom": {"base_url": GENERIC_URL, "api_key": "fixture-generic"}}
    providers.update(copy.deepcopy(extra_providers))
    return {"model": {"provider": "deepseek", "default": "deepseek-chat"}, "providers": providers}


def _use(cfg, monkeypatch):
    """Install ``cfg`` as both the ambient config object and the loader result."""
    cfg = copy.deepcopy(cfg)
    monkeypatch.setattr(config, "cfg", cfg)
    monkeypatch.setattr(config, "get_config", lambda *a, **k: cfg)
    return cfg


@pytest.mark.parametrize("extra,expected_lane,expected_base_url", [row[1:] for row in ROWS], ids=ROW_IDS)
def test_every_identity_row_keeps_the_named_lane(extra, expected_lane, expected_base_url, monkeypatch):
    """A record that NAMES the identity is configured, enabled or not.

    The lane is what both real entry points resolve: the sync ``/api/chat``
    handler (``api/routes.py`` ``_handle_chat_start``) and the streaming start
    (``api/streaming.py``) both call
    ``resolve_model_provider(model_with_provider_context(stored_model,
    stored_provider), session_provider=stored_provider)``.
    """
    cfg = _use(_cfg(extra), monkeypatch)

    assert _configured_custom_provider_identity("omni", cfg) is (
        expected_lane[1] == "custom:omni"
    )
    assert canonical_model_provider_lane("@custom:omni:latest", "custom") == expected_lane
    resolved_model, resolved_provider, resolved_base_url = resolve_model_provider(
        "@custom:omni:latest", session_provider="custom"
    )
    assert (resolved_model, resolved_provider) == expected_lane
    assert resolved_base_url == expected_base_url


@pytest.mark.parametrize("extra", [ROWS[4][1], ROWS[5][1]], ids=["5-omni-disabled", "6-proxy-name-only"])
def test_a_named_but_unusable_identity_is_refused_not_rerouted(extra, monkeypatch):
    """The sync ``/api/chat`` verdict: a 400 route error, never the generic endpoint.

    ``_handle_chat_start`` hands the resolved pair to
    ``_resolve_agent_connection_bundle`` and turns its
    ``CustomProviderRouteError`` into a controlled 400.  Before this round the
    two shapes below resolved to the generic lane, so that chokepoint never saw
    a named identity to refuse.
    """
    _use(_cfg(extra), monkeypatch)
    _model, provider, base_url = resolve_model_provider(
        "@custom:omni:latest", session_provider="custom"
    )
    assert provider == "custom:omni"
    with pytest.raises(CustomProviderRouteError):
        routes._resolve_agent_connection_bundle(provider, "", base_url, None)


def test_the_generic_identity_itself_still_peels(monkeypatch):
    """Positive control for the #7904 fix: nothing owns ``omni`` -> generic lane."""
    _use(_cfg({}), monkeypatch)
    _model, provider, base_url = resolve_model_provider(
        "@custom:omni:latest", session_provider="custom"
    )
    assert (provider, base_url) == ("custom", GENERIC_URL)
    # And an unconfigured slug is not "configured" just because other rows exist.
    assert _configured_custom_provider_identity("ghost") is False


# ── B: GET /api/session context refresh ─────────────────────────────────────


def _fake_metadata(calls, value):
    def fake_get_model_context_length(model, base_url="", api_key="", **kwargs):
        calls.append((model, base_url))
        return value

    mod = types.ModuleType("agent.model_metadata")
    mod.get_model_context_length = fake_get_model_context_length
    return mod


def _stub_session(model, provider, context_length, threshold_tokens):
    s = MagicMock()
    s.session_id = "t7905"
    s.title = "t7905"
    s.model = model
    s.model_provider = provider
    s.last_used_model = None
    s.gateway_routing = None
    s.profile = None
    s.context_length = context_length
    s.threshold_tokens = threshold_tokens
    s.last_prompt_tokens = 0
    s.messages = []
    s.tool_calls = []
    s.pending_attachments = []
    s.pending_user_message = None
    s.pending_started_at = None
    s.active_stream_id = None
    s.anchor_activity_scenes = None
    s.compact.return_value = {
        "session_id": "t7905",
        "title": "t7905",
        "model": model,
        "input_tokens": 0,
        "output_tokens": 0,
        "context_length": context_length,
        "threshold_tokens": threshold_tokens,
        "last_prompt_tokens": 0,
    }
    return s


def _get(cfg, monkeypatch, *, model, provider, persisted, threshold, fallback):
    _use(cfg, monkeypatch)
    calls, captured = [], {}
    session = _stub_session(model, provider, persisted, threshold)
    with patch("api.routes.get_session", return_value=session), patch(
        "api.routes.j", side_effect=lambda h, data, status=200: captured.update(payload=data, status=status)
    ), patch.dict(sys.modules, {"agent.model_metadata": _fake_metadata(calls, fallback)}):
        routes.handle_get(
            MagicMock(), urlparse("/api/session?session_id=t7905&messages=0&resolve_model=1")
        )
    assert captured.get("status", 200) == 200, captured
    # ``j(handler, {"session": redact})`` — the window lives under "session".
    return captured["payload"]["session"], session, calls


def test_get_keeps_the_larger_window_for_an_unchanged_routed_generic_model(monkeypatch):
    """Unchanged selection + 256k fallback must not clobber a 1M window."""
    payload, session, _calls = _get(
        _cfg({}),
        monkeypatch,
        model="@custom:qwen3-30b-a3b:latest",
        provider="custom",
        persisted=1_000_000,
        threshold=800_000,
        fallback=256_000,
    )
    assert payload["context_length"] == 1_000_000
    assert payload["threshold_tokens"] == 800_000
    assert session.context_length == 1_000_000
    assert session.threshold_tokens == 800_000


def test_get_still_refreshes_a_genuinely_changed_selection(monkeypatch):
    """Control: a genuine model change keeps the 256k refresh and rescales the ratio.

    ``effective_model`` is the display resolver's verdict; pinning it to another
    model is the controlled way to say "the selection is no longer the stored
    one" without touching the lookup or the refresh guard.
    """
    with patch(
        "api.routes._resolve_effective_session_model_for_display", return_value="other-model"
    ), patch(
        "api.routes._resolve_effective_session_model_provider_for_display",
        return_value="custom",
    ):
        payload, session, _calls = _get(
            _cfg({}),
            monkeypatch,
            model="@custom:qwen3-30b-a3b:latest",
            provider="custom",
            persisted=1_000_000,
            threshold=800_000,
            fallback=256_000,
        )
    assert payload["context_length"] == 256_000
    assert payload["threshold_tokens"] == 204_800
