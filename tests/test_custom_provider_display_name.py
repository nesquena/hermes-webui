"""
Tests for named custom provider display in the model dropdown (issue #557).

When a custom_providers entry carries a `name` field (e.g. "Agent37"), the
web UI model picker should show that name as the group header rather than the
generic "Custom" label.
"""
import api.config as config


def _models_with_cfg(model_cfg=None, custom_providers=None, active_provider=None):
    """Temporarily patch config.cfg, call get_available_models(), restore."""
    old_cfg = dict(config.cfg)
    config.cfg.clear()
    if model_cfg:
        config.cfg["model"] = model_cfg
    if custom_providers is not None:
        config.cfg["custom_providers"] = custom_providers
    try:
        return config.get_available_models()
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)


# ── Named provider shows its name in the dropdown ─────────────────────────────

class TestNamedCustomProviderGroup:

    def test_named_provider_uses_name_as_group_header(self):
        """A custom_provider entry with name='Agent37' should produce
        a group whose 'provider' key is 'Agent37', not 'Custom'."""
        result = _models_with_cfg(
            model_cfg={"provider": "custom", "base_url": "https://agent37.example.com/v1"},
            custom_providers=[
                {"name": "Agent37", "model": "default", "base_url": "https://agent37.example.com/v1"}
            ],
        )
        group_names = [g["provider"] for g in result.get("groups", [])]
        assert "Agent37" in group_names, (
            f"Expected 'Agent37' in group names, got {group_names}"
        )

    def test_named_provider_does_not_produce_generic_custom(self):
        """When all custom_provider entries have names, no group called 'Custom'
        should appear alongside them."""
        result = _models_with_cfg(
            model_cfg={"provider": "custom", "base_url": "https://agent37.example.com/v1"},
            custom_providers=[
                {"name": "Agent37", "model": "default", "base_url": "https://agent37.example.com/v1"}
            ],
        )
        group_names = [g["provider"] for g in result.get("groups", [])]
        assert "Custom" not in group_names, (
            f"Expected no generic 'Custom' group when all entries are named, got {group_names}"
        )

    def test_named_provider_model_appears_in_its_group(self):
        """The model ID from the named entry should be inside the named group."""
        result = _models_with_cfg(
            model_cfg={"provider": "custom"},
            custom_providers=[
                {"name": "Agent37", "model": "my-llm", "base_url": "https://agent37.example.com/v1"}
            ],
        )
        agent37_group = next(
            (g for g in result.get("groups", []) if g["provider"] == "Agent37"), None
        )
        assert agent37_group is not None, "Expected an 'Agent37' group"
        model_ids = [m["id"] for m in agent37_group.get("models", [])]
        assert "my-llm" in model_ids, (
            f"Expected 'my-llm' in Agent37 group models, got {model_ids}"
        )

    def test_multiple_named_providers_each_get_their_own_group(self):
        """Two named custom providers should produce two distinct groups."""
        result = _models_with_cfg(
            model_cfg={"provider": "custom"},
            custom_providers=[
                {"name": "Agent37", "model": "fast-model"},
                {"name": "PrivateProxy", "model": "private-llm"},
            ],
        )
        group_names = [g["provider"] for g in result.get("groups", [])]
        assert "Agent37" in group_names, f"Expected 'Agent37' group, got {group_names}"
        assert "PrivateProxy" in group_names, f"Expected 'PrivateProxy' group, got {group_names}"
        assert "Custom" not in group_names, f"No generic 'Custom' group expected, got {group_names}"

    def test_multiple_models_in_same_named_provider(self):
        """Multiple entries with the same name should be collapsed into one group."""
        result = _models_with_cfg(
            model_cfg={"provider": "custom"},
            custom_providers=[
                {"name": "Agent37", "model": "model-a"},
                {"name": "Agent37", "model": "model-b"},
            ],
        )
        agent37_groups = [g for g in result.get("groups", []) if g["provider"] == "Agent37"]
        assert len(agent37_groups) == 1, (
            f"Expected exactly one 'Agent37' group, got {len(agent37_groups)}"
        )
        model_ids = [m["id"] for m in agent37_groups[0].get("models", [])]
        assert "model-a" in model_ids
        assert "model-b" in model_ids


# ── Unnamed entry still falls back to 'Custom' ─────────────────────────────────

class TestUnnamedCustomProviderFallback:

    def test_unnamed_entry_still_produces_custom_group(self):
        """A custom_provider entry without a name should still show as 'Custom'."""
        result = _models_with_cfg(
            model_cfg={"provider": "custom"},
            custom_providers=[
                {"model": "unnamed-model"}
            ],
        )
        group_names = [g["provider"] for g in result.get("groups", [])]
        assert "Custom" in group_names, (
            f"Expected generic 'Custom' group for unnamed entry, got {group_names}"
        )

    def test_mixed_named_and_unnamed_entries(self):
        """Named and unnamed entries should appear in their respective groups."""
        result = _models_with_cfg(
            model_cfg={"provider": "custom"},
            custom_providers=[
                {"name": "Agent37", "model": "named-model"},
                {"model": "unnamed-model"},
            ],
        )
        group_names = [g["provider"] for g in result.get("groups", [])]
        assert "Agent37" in group_names, f"Expected 'Agent37' group, got {group_names}"
        assert "Custom" in group_names, f"Expected 'Custom' group for unnamed entry, got {group_names}"
