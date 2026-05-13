"""Regression tests for #2177 — resolve_model_provider() strips nvidia/ prefix on NVIDIA NIM.

The bug: when ``config_provider == "nvidia"`` and the model id is
``nvidia/nemotron-3-super-120b-a12b``, the resolver hit the
``prefix == config_provider`` strip BEFORE the ``_PORTAL_PROVIDERS`` guard and
returned the bare name ``nemotron-3-super-120b-a12b``. NVIDIA NIM's inference
endpoint then 404'd because it requires the full namespaced model id.

Same bug class as #854 / #894 for the Nous portal. The fix moves the
``_PORTAL_PROVIDERS`` guard to run BEFORE the prefix-strip so portal providers
always preserve the full ``provider/model`` path regardless of whether the
prefix happens to equal the config_provider name.
"""
import api.config as config


def _resolve(model_id: str, provider: str):
    """Resolve a model under a synthesized config_provider, isolated from disk state."""
    old = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg["model"] = {"provider": provider}
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0
    try:
        return config.resolve_model_provider(model_id)
    finally:
        config.cfg.clear()
        config.cfg.update(old)
        config._cfg_mtime = old_mtime


class TestNvidiaPortalPrefixPreservation:
    """NVIDIA NIM requires the full ``namespace/model`` id — prefix must never be stripped."""

    def test_nvidia_self_namespace_prefix_preserved(self):
        """``nvidia/nemotron-...`` under nvidia provider must keep the nvidia/ prefix.

        This is the exact case in #2177 — prefix == config_provider would otherwise
        trigger the strip branch and emit a bare ``nemotron-...`` id that NIM 404s.
        """
        model, provider, _ = _resolve("nvidia/nemotron-3-super-120b-a12b", "nvidia")
        assert model == "nvidia/nemotron-3-super-120b-a12b", (
            f"prefix was stripped: {model!r} — NVIDIA NIM requires the full namespaced id"
        )
        assert provider == "nvidia"

    def test_nvidia_cross_namespace_qwen_prefix_preserved(self):
        """``qwen/...`` under nvidia provider keeps the qwen/ prefix (regression pin).

        Cross-namespace already worked because ``prefix != config_provider`` — pinning
        the test so a later refactor doesn't regress it.
        """
        model, provider, _ = _resolve("qwen/qwen2.5-coder-32b-instruct", "nvidia")
        assert model == "qwen/qwen2.5-coder-32b-instruct"
        assert provider == "nvidia"

    def test_nvidia_cross_namespace_meta_prefix_preserved(self):
        """``meta/llama-...`` under nvidia provider keeps the meta/ prefix."""
        model, provider, _ = _resolve("meta/llama-3.1-70b-instruct", "nvidia")
        assert model == "meta/llama-3.1-70b-instruct"
        assert provider == "nvidia"

    def test_nvidia_static_models_all_resolve_with_prefix_intact(self):
        """Every static nvidia model in ``_PROVIDER_MODELS`` must resolve to itself.

        Pins the contract that the static dropdown list and the resolver agree on
        the wire format — same invariant ``test_nous_portal_routing.py`` enforces
        for the Nous portal.
        """
        nvidia_models = config._PROVIDER_MODELS.get("nvidia", [])
        assert nvidia_models, "nvidia must have at least one static model"
        for entry in nvidia_models:
            mid = entry["id"]
            resolved, provider, _ = _resolve(mid, "nvidia")
            assert resolved == mid, (
                f"static model {mid!r} resolved to {resolved!r} — "
                f"portal must preserve full namespaced id"
            )
            assert provider == "nvidia"


class TestPortalGuardOrdering:
    """The ``_PORTAL_PROVIDERS`` guard must run BEFORE the prefix-strip branch.

    Otherwise, any portal user whose model id starts with the literal provider
    name (nvidia/nvidia-..., nous/nous-..., etc.) hits the strip branch and
    breaks at the upstream API. This is structural — pinning the ordering so a
    future refactor cannot quietly reintroduce the bug.
    """

    def test_hypothetical_nous_self_prefix_preserved(self):
        """``nous/<model>`` under nous provider must keep the prefix.

        Latent bug shape — there's no shipped ``nous/<model>`` id today, but if
        Nous ever serves a self-prefixed model the resolver must handle it the
        same way it handles nvidia/.
        """
        model, provider, _ = _resolve("nous/hermes-3-something", "nous")
        assert model == "nous/hermes-3-something", (
            f"prefix stripped: {model!r} — portal guard must preserve full id"
        )
        assert provider == "nous"

    def test_anthropic_self_prefix_still_strips_for_anthropic(self):
        """Non-portal providers (anthropic) keep the existing strip behavior.

        ``anthropic/claude-...`` under the anthropic provider should still
        resolve to bare ``claude-...`` — anthropic's API doesn't want the
        ``anthropic/`` prefix. Pinning so the fix doesn't over-correct.
        """
        model, provider, _ = _resolve("anthropic/claude-opus-4.6", "anthropic")
        assert model == "claude-opus-4.6", (
            f"anthropic strip-prefix path broken: got {model!r}, expected 'claude-opus-4.6'"
        )
        assert provider == "anthropic"
