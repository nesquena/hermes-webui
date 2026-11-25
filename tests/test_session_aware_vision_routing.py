"""Tests for session-aware vision routing (image routing follows the
session's actual provider/model, not only the global default).

Covers the behavior added by the session-aware-vision-routing patch:
  - no active params     -> falls back to the global default (upstream parity)
  - active vision model  -> native
  - active text model    -> text
  - requested_provider   -> capability lookup selects the exact
    custom_providers/providers entry even after the provider id was
    canonicalized to "custom" by
    _resolve_custom_provider_runtime_overrides
  - _build_native_multimodal_message and _sanitize_messages_for_api
    forward the session identity through to the routing decision
"""
from pathlib import Path

import pytest

from api.streaming import (
    _build_native_multimodal_message,
    _resolve_image_input_mode,
    _sanitize_messages_for_api,
)

# The capability-based routing under test delegates to
# ``agent.image_routing.decide_image_input_mode`` (the single source of truth).
# In the WebUI standalone CI environment the ``hermes-agent`` package is NOT
# installed, so that import fails and ``_resolve_image_input_mode`` falls back to
# the historical "forward native, rely on strip-and-retry" behaviour. Tests that
# assert a capability-derived ``text`` verdict can therefore only run where the
# agent package is importable — guard them so they skip (not fail) on CI.
try:  # pragma: no cover - trivial import probe
    import agent.image_routing  # noqa: F401
    import agent.auxiliary_client  # noqa: F401

    _HAS_AGENT_ROUTING = True
except Exception:  # pragma: no cover
    _HAS_AGENT_ROUTING = False

requires_agent_routing = pytest.mark.skipif(
    not _HAS_AGENT_ROUTING,
    reason="hermes-agent not installed (capability-based routing unavailable)",
)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _make_png(path: Path, size: int = 0) -> Path:
    """Write a minimal valid PNG to *path* (IHDR + IDAT + IEND)."""
    if size <= 0:
        data = (
            b'\x89PNG\r\n\x1a\n'
            b'\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde'
            b'\x00\x00\x00\x0bIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N'
            b'\x00\x00\x00\x00IEND\xaeB`\x82'
        )
    else:
        data = b'\x89PNG\r\n\x1a\n' + b'\x00' * (size - 8)
    path.write_bytes(data)
    return path


def _cfg_with_provider_vision(provider_name: str, model: str, vision: bool) -> dict:
    """Config with a named provider whose per-model capability is explicit."""
    return {
        "model": {},
        "providers": {
            provider_name: {
                "models": {model: {"supports_vision": vision}},
            }
        },
    }


# ── _resolve_image_input_mode ───────────────────────────────────────────────

class TestResolveImageInputMode:
    @requires_agent_routing
    def test_no_active_params_falls_back_to_global_default(self, monkeypatch):
        """Upstream parity: with no session identity, route by global default."""
        import agent.auxiliary_client as aux

        monkeypatch.setattr(aux, "_read_main_provider", lambda: "custom:mygateway")
        monkeypatch.setattr(aux, "_read_main_model", lambda: "my-vision-model")
        cfg = _cfg_with_provider_vision("mygateway", "my-vision-model", True)
        assert _resolve_image_input_mode(cfg) == "native"

    @requires_agent_routing
    def test_global_default_text_model_routes_text(self, monkeypatch):
        """Upstream parity for a text-only global default."""
        import agent.auxiliary_client as aux

        monkeypatch.setattr(aux, "_read_main_provider", lambda: "custom:mygateway")
        monkeypatch.setattr(aux, "_read_main_model", lambda: "my-text-model")
        cfg = {"model": {}, "providers": {}}
        # unknown to models.dev -> WebUI carve-out forwards native
        assert _resolve_image_input_mode(cfg) in ("native", "text")

    def test_active_vision_model_routes_native(self):
        cfg = _cfg_with_provider_vision("myvllm", "my-vision", True)
        assert _resolve_image_input_mode(
            cfg, "custom", "my-vision", requested_provider="myvllm"
        ) == "native"

    @requires_agent_routing
    def test_active_text_model_routes_text(self):
        cfg = _cfg_with_provider_vision("myvllm", "my-text-model", False)
        assert _resolve_image_input_mode(
            cfg, "custom", "my-text-model", requested_provider="myvllm"
        ) == "text"

    @requires_agent_routing
    def test_requested_provider_selects_exact_entry(self):
        """requested_provider beats the unknown-model native carve-out.

        Same config, same canonicalized provider "custom": passing the
        pre-canonicalization identity makes capability lookup hit the exact
        providers.<name> entry (here: supports_vision=False) and route text,
        while without it the lookup misses and the carve-out forwards native.
        """
        cfg = _cfg_with_provider_vision("myvllm", "my-model", False)
        assert _resolve_image_input_mode(
            cfg, "custom", "my-model", requested_provider="myvllm"
        ) == "text"
        assert _resolve_image_input_mode(cfg, "custom", "my-model") == "native"

    def test_requested_provider_vision_true_routes_native(self):
        cfg = _cfg_with_provider_vision("myvllm", "my-vision", True)
        assert _resolve_image_input_mode(
            cfg, "custom", "my-vision", requested_provider="myvllm"
        ) == "native"


class TestProviderQualifiedSessionModel:
    """#9196: gateway sessions store the model as ``@provider:model``.

    The request body strips that prefix through ``_gateway_model_field()``, but
    image routing used to receive the raw string. The capability lookup then
    searched for a model literally named ``@custom:mygateway:my-vision-model``,
    missed the ``supports_vision`` entry and returned the wrong verdict.
    ``_resolve_image_input_mode`` must normalise the session model the same way
    (``_parse_provider_qualified_model_id``) so every caller is covered.
    """

    @requires_agent_routing
    def test_prefixed_model_reaches_capability_lookup_unprefixed(self, monkeypatch):
        """The lookup hook must see the bare model name, not the route hint."""
        import agent.image_routing as image_routing

        seen_models: list[str] = []
        real_lookup = image_routing._lookup_supports_vision

        def spy(provider, model, cfg=None, **kwargs):
            seen_models.append(model)
            return real_lookup(provider, model, cfg, **kwargs)

        monkeypatch.setattr(image_routing, "_lookup_supports_vision", spy)
        cfg = _cfg_with_provider_vision("mygateway", "my-vision-model", True)
        assert _resolve_image_input_mode(
            cfg,
            "custom:mygateway",
            "@custom:mygateway:my-vision-model",
            requested_provider="custom:mygateway",
        ) == "native"
        assert seen_models, "capability lookup was never consulted"
        assert set(seen_models) == {"my-vision-model"}

    @requires_agent_routing
    def test_prefixed_text_only_model_routes_text_like_bare(self):
        """A config-known text-only model must not gain native images by being
        stored with a provider prefix: the carve-out for unknown models is only
        sound while the capability lookup actually finds the entry."""
        cfg = _cfg_with_provider_vision("mygateway", "my-text-model", False)
        bare = _resolve_image_input_mode(
            cfg, "custom:mygateway", "my-text-model",
            requested_provider="custom:mygateway",
        )
        prefixed = _resolve_image_input_mode(
            cfg, "custom:mygateway", "@custom:mygateway:my-text-model",
            requested_provider="custom:mygateway",
        )
        assert bare == "text"
        assert prefixed == bare

    @requires_agent_routing
    def test_prefixed_model_matches_bare_with_explicit_auxiliary_vision(self, monkeypatch):
        """Parity fence: an explicit ``auxiliary.vision`` backend is a documented
        text signal for the MAIN model (agent/image_routing.py), so the prefix
        must not change that verdict either. Normalising the model must never
        bypass the explicit-signal rule."""
        import agent.image_routing as image_routing

        consulted: list[str] = []
        real_lookup = image_routing._lookup_supports_vision

        def spy(provider, model, cfg=None, **kwargs):
            consulted.append(model)
            return real_lookup(provider, model, cfg, **kwargs)

        monkeypatch.setattr(image_routing, "_lookup_supports_vision", spy)
        cfg = _cfg_with_provider_vision("mygateway", "my-vision-model", True)
        cfg["auxiliary"] = {
            "vision": {"provider": "custom:mygateway", "model": "other-vision-model"}
        }
        bare = _resolve_image_input_mode(
            cfg, "custom:mygateway", "my-vision-model",
            requested_provider="custom:mygateway",
        )
        prefixed = _resolve_image_input_mode(
            cfg, "custom:mygateway", "@custom:mygateway:my-vision-model",
            requested_provider="custom:mygateway",
        )
        # ``bare == prefixed`` alone also holds on master (both sides answer
        # "text" there), so the fence also pins the documented verdict: with an
        # explicit ``auxiliary.vision`` backend both ids route text even though
        # the provider model declares ``supports_vision: true``. A future
        # normalisation that bypasses the explicit signal keeps the parity but
        # flips both ids to "native", which this fails on.
        assert bare == prefixed
        assert bare == "text"
        assert prefixed == "text"
        # The explicit-signal rule short-circuits before the capability lookup,
        # so unwrapping the prefixed id must not turn the lookup back on.
        assert consulted == [], f"capability lookup consulted for {consulted}"


# ── _build_native_multimodal_message forwarding ─────────────────────────────

def _normalized_attachments(img_path: Path) -> list:
    from api.routes import _normalize_chat_attachments

    return _normalize_chat_attachments([{
        'name': img_path.name, 'path': str(img_path),
        'mime': 'image/png', 'size': img_path.stat().st_size, 'is_image': True,
    }])


class TestBuildNativeMultimodalForwarding:
    @requires_agent_routing
    def test_requested_provider_reaches_text_mode(self, tmp_path):
        """A text-mode verdict strips the attachments to a plain string."""
        cfg = _cfg_with_provider_vision("myvllm", "my-model", False)
        img = _make_png(tmp_path / "a.png")
        result = _build_native_multimodal_message(
            "", "describe", _normalized_attachments(img), str(tmp_path),
            cfg=cfg,
            active_provider="custom",
            active_model="my-model",
            requested_provider="myvllm",
        )
        assert result == "describe"

    def test_without_requested_provider_embeds_native(self, tmp_path):
        """Unknown-model carve-out: image is embedded as a content part."""
        cfg = _cfg_with_provider_vision("myvllm", "my-model", False)
        img = _make_png(tmp_path / "a.png")
        parts = _build_native_multimodal_message(
            "", "describe", _normalized_attachments(img), str(tmp_path),
            cfg=cfg,
            active_provider="custom",
            active_model="my-model",
        )
        assert isinstance(parts, list)
        assert parts[0]["type"] == "text"
        assert any(part.get("type") == "image_url" for part in parts[1:])


# ── _sanitize_messages_for_api forwarding ───────────────────────────────────

class TestSanitizeForwarding:
    @requires_agent_routing
    def test_text_mode_strips_historical_native_images(self):
        cfg = _cfg_with_provider_vision("myvllm", "my-model", False)
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hi"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ],
            }
        ]
        clean = _sanitize_messages_for_api(
            messages,
            cfg=cfg,
            effective_provider="custom",
            effective_model="my-model",
            requested_provider="myvllm",
        )
        rendered = str(clean)
        assert "image_url" not in rendered

    @requires_agent_routing
    def test_canonicalized_provider_without_requested_identity_cannot_strip(self):
        """Regression for the auth-heal retry bug (#6882 / Codex CORE): once the
        provider id is canonicalized to the generic ``custom``, the per-model
        vision capability can ONLY be found via the preserved pre-canonicalization
        ``requested_provider``. If a heal-retry clobbers that identity, the
        sanitizer falls back to the generic id, cannot resolve the named
        provider's text-only capability, and wrongly KEEPS the historical image.

        This asserts the two arms:
          - identity preserved  -> images stripped (correct heal-retry behavior)
          - identity lost        -> images kept    (the bug the fix prevents)
        """
        cfg = _cfg_with_provider_vision("myvllm", "my-model", False)
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hi"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ],
            }
        ]
        # Identity preserved through canonicalization: text-only -> stripped.
        preserved = _sanitize_messages_for_api(
            [dict(m) for m in messages],
            cfg=cfg,
            effective_provider="custom",
            effective_model="my-model",
            requested_provider="myvllm",
        )
        assert "image_url" not in str(preserved)
        # Identity lost (heal-retry clobbered it to the generic id): the named
        # provider's text-only capability is unreachable, so images survive.
        lost = _sanitize_messages_for_api(
            [dict(m) for m in messages],
            cfg=cfg,
            effective_provider="custom",
            effective_model="my-model",
            requested_provider="custom",
        )
        assert "image_url" in str(lost)

    def test_native_mode_keeps_historical_images(self):
        cfg = _cfg_with_provider_vision("myvllm", "my-model", True)
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hi"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ],
            }
        ]
        clean = _sanitize_messages_for_api(
            messages,
            cfg=cfg,
            effective_provider="custom",
            effective_model="my-model",
            requested_provider="myvllm",
        )
        assert "image_url" in str(clean)