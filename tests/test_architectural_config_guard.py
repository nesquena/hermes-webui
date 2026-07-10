"""Test the architectural guard in _save_yaml_config_file.

Verifies that ``${VAR}`` environment variable references in config.yaml survive
a read-modify-write cycle through a save path that reads expanded config.
"""

import pytest


@pytest.fixture(autouse=True)
def _prepare_config_path(tmp_path, monkeypatch):
    """Isolate config file access to a temp directory."""
    import api.config as config

    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(config, "reload_config", lambda: None)

    # Write a starting config with a ${VAR} reference
    env_var = "TICKTICK_MCP_KEY"
    monkeypatch.setenv(env_var, "tp_real_secret_value")
    monkeypatch.setenv(
        "UNRELATED_DASHBOARD_KEY",
        "dash_secret_value",
    )

    config_path.write_text(
        "\n".join([
            "model:",
            "  default: deepseek-v4-flash",
            "mcp_servers:",
            "  ticktick:",
            "    url: https://mcp.ticktick.com",
            "    headers:",
            "      Authorization: Bearer ${TICK...KEY}",
            "    timeout: 30",
            "",
        ]),
        encoding="utf-8",
    )

    # Wipe any in-memory config caches so the raw file is read fresh
    config._yaml_file_cache.clear()
    _orig_cfg_cache = config._cfg_cache
    config._cfg_cache = None

    yield config_path

    config._cfg_cache = _orig_cfg_cache


class TestArchitecturalConfigGuard:
    """``_save_yaml_config_file`` preserves ``${VAR}`` references when the
    caller provides a dirty_set declaring which keys were authored."""

    # ── Contract tests ────────────────────────────────────────────────────

    def test_missing_dirty_set_raises_type_error(self, _prepare_config_path):
        """Calling ``_save_yaml_config_file`` without ``dirty_set`` raises
        ``TypeError``."""
        import api.config as config

        config_path = _prepare_config_path
        expanded = config._load_yaml_config_file(config_path)

        with pytest.raises(TypeError, match="dirty_set is required"):
            config._save_yaml_config_file(config_path, expanded)

    # ── Basic dict preservation (same-value-literal case) ──────────────────

    def test_same_value_literal_authored_wins(self, tmp_path, monkeypatch):
        """When an authored value happens to equal the env expansion, the
        caller's literal is written — not the ``${VAR}`` reference."""
        import api.config as config

        monkeypatch.setenv("MY_KEY", "sk-real-secret")

        config_path = tmp_path / "config.yaml"
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
        monkeypatch.setattr(config, "reload_config", lambda: None)

        config_path.write_text(
            "\n".join([
                "model:",
                "  default: claude-opus-4",
                "  api_key: ${MY_KEY}",
            ]),
            encoding="utf-8",
        )

        config._yaml_file_cache.clear()
        _orig_cache = config._cfg_cache
        config._cfg_cache = None

        # Load expanded, write back the SAME literal that ${MY_KEY} expands
        # to, with dirty_set asserting authorship.
        expanded = config._load_yaml_config_file(config_path)
        expanded["model"]["api_key"] = "sk-real-secret"
        config._save_yaml_config_file(config_path, expanded,
            dirty_set={("model", "api_key")})

        config._cfg_cache = _orig_cache

        saved = config._load_yaml_config_file_raw(config_path, _copy=False)
        assert saved["model"]["api_key"] == "sk-real-secret", (
            f"Expected literal 'sk-real-secret', got {saved['model']['api_key']!r}"
        )

    def test_same_value_literal_not_authored_stays_raw(self, tmp_path, monkeypatch):
        """When an expanded value equals the env expansion but the caller
        did NOT dirty the key, the raw ``${VAR}`` reference is preserved."""
        import api.config as config

        monkeypatch.setenv("MY_KEY", "sk-real-secret")

        config_path = tmp_path / "config.yaml"
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
        monkeypatch.setattr(config, "reload_config", lambda: None)

        config_path.write_text(
            "\n".join([
                "model:",
                "  default: claude-opus-4",
                "  api_key: ${MY_KEY}",
            ]),
            encoding="utf-8",
        )

        config._yaml_file_cache.clear()
        _orig_cache = config._cfg_cache
        config._cfg_cache = None

        # Load expanded, pass through unchanged, but dirty only "model.default"
        expanded = config._load_yaml_config_file(config_path)
        expanded["model"]["default"] = "claude-opus-4"
        config._save_yaml_config_file(config_path, expanded,
            dirty_set={("model", "default")})

        config._cfg_cache = _orig_cache

        saved = config._load_yaml_config_file_raw(config_path, _copy=False)
        assert "${MY_KEY}" in str(saved["model"]["api_key"]), (
            f"Expected ${{MY_KEY}} in api_key, got {saved['model']['api_key']!r}"
        )

    # ── Scalar key authored (test_mutated_key_is_written analog) ───────────

    def test_scalar_key_in_dirty_is_written(self, _prepare_config_path):
        """A top-level scalar key the caller included in dirty_set is
        written with the caller's value."""
        import api.config as config

        config_path = _prepare_config_path
        expanded = config._load_yaml_config_file(config_path)
        expanded.setdefault("agent", {})["reasoning_effort"] = "high"

        config._save_yaml_config_file(config_path, expanded,
            dirty_set={("agent", "reasoning_effort")})

        saved = config._load_yaml_config_file_raw(config_path, _copy=False)
        agent = saved.get("agent", {})
        assert agent.get("reasoning_effort") == "high", (
            f"Expected reasoning_effort=high, got {agent.get('reasoning_effort')!r}"
        )

    # ── Environment rotation ──────────────────────────────────────────────

    def test_env_rotation_preserves_raw(self, tmp_path, monkeypatch):
        """When an env var changes between load and save, and the caller
        didn't dirty that key, the raw ``${VAR}`` reference is preserved
        (not the stale expanded value)."""
        import api.config as config

        monkeypatch.setenv("ROTATING_KEY", "old_value")

        config_path = tmp_path / "config.yaml"
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
        monkeypatch.setattr(config, "reload_config", lambda: None)

        config_path.write_text(
            "\n".join([
                "model:",
                "  api_key: ${ROTATING_KEY}",
            ]),
            encoding="utf-8",
        )

        config._yaml_file_cache.clear()
        _orig_cache = config._cfg_cache
        config._cfg_cache = None

        # Load when env is "old_value"
        expanded = config._load_yaml_config_file(config_path)
        assert expanded["model"]["api_key"] == "old_value"

        # Environment rotates before save
        monkeypatch.setenv("ROTATING_KEY", "new_value")

        # Save with only "model.default" dirtied (api_key NOT dirty)
        config._save_yaml_config_file(config_path, expanded,
            dirty_set={("model", "default")})

        config._cfg_cache = _orig_cache

        saved = config._load_yaml_config_file_raw(config_path, _copy=False)
        assert "${ROTATING_KEY}" in str(saved["model"]["api_key"]), (
            f"Expected ${{ROTATING_KEY}} in api_key, got {saved['model']['api_key']!r}"
        )

    # ── Nested dict, sub-path dirty, unrelated ${VAR} preserved ───────────

    def test_nested_dict_untouched_var_preserved(self, _prepare_config_path):
        """When the caller dirties a sub-path inside a nested dict, an
        unrelated ``${VAR}`` in the same dict section is preserved."""
        import api.config as config

        config_path = _prepare_config_path
        expanded = config._load_yaml_config_file(config_path)
        expanded.setdefault("agent", {})["reasoning_effort"] = "high"

        config._save_yaml_config_file(config_path, expanded,
            dirty_set={("agent", "reasoning_effort")})

        saved = config._load_yaml_config_file_raw(config_path, _copy=False)

        # The untouched ${VAR} in mcp_servers must survive
        mcp = saved.get("mcp_servers", {})
        ticktick = mcp.get("ticktick", {})
        headers = ticktick.get("headers", {})
        auth = headers.get("Authorization", "")
        assert "${TICK...KEY}" in auth, (
            f"Expected ${{TICK...KEY}} in Authorization header, got: {auth!r}"
        )

        # Plain-value keys in the same section survive
        assert ticktick.get("url") == "https://mcp.ticktick.com"
        assert ticktick.get("timeout") == 30

    # ── Nested dict, key itself dirtied ───────────────────────────────────

    def test_nested_dict_key_wholesale(self, _prepare_config_path):
        """When the caller dirties a dict key itself (not a sub-path), the
        entire sub-dict is written verbatim."""
        import api.config as config

        config_path = _prepare_config_path
        expanded = config._load_yaml_config_file(config_path)
        expanded.setdefault("agent", {})["reasoning_effort"] = "high"

        config._save_yaml_config_file(config_path, expanded,
            dirty_set={("agent",)})

        saved = config._load_yaml_config_file_raw(config_path, _copy=False)
        agent = saved.get("agent", {})
        assert agent.get("reasoning_effort") == "high", (
            f"Expected 'high', got {agent.get('reasoning_effort')!r}"
        )

    # ── Scalar list with deleted items — whole list dirtied ───────────────

    def test_scalar_list_whole_key_dirtied(self, tmp_path, monkeypatch):
        """When the caller replaces a scalar list and dirties the whole key,
        the caller's list is written verbatim."""
        import api.config as config

        monkeypatch.setenv("MODEL_A", "gpt-4o")
        monkeypatch.setenv("MODEL_B", "claude-opus-4")

        config_path = tmp_path / "config.yaml"
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
        monkeypatch.setattr(config, "reload_config", lambda: None)

        config_path.write_text(
            "\n".join([
                "models:",
                "  - ${MODEL_A}",
                "  - ${MODEL_B}",
            ]),
            encoding="utf-8",
        )

        config._yaml_file_cache.clear()
        _orig_cache = config._cfg_cache
        config._cfg_cache = None

        # Keep only the second, whole list is dirtied
        expanded = config._load_yaml_config_file(config_path)
        expanded["models"] = [expanded["models"][1]]
        config._save_yaml_config_file(config_path, expanded,
            dirty_set={("models",)})

        config._cfg_cache = _orig_cache

        saved = config._load_yaml_config_file_raw(config_path, _copy=False)
        models = saved.get("models", [])
        assert len(models) == 1
        # The whole list was authored, so ${MODEL_B} was not preserved
        # (the caller authored the replacement list verbatim)
        assert models[0] == "claude-opus-4"

    # ── Nested dict list with sub-path dirtied ────────────────────────────

    def test_nested_list_with_sub_path_dirty(self, tmp_path, monkeypatch):
        """When a nested dict section is dirtied, an unrelated ``${VAR}``
        in a separate top-level section is preserved."""
        import api.config as config

        monkeypatch.setenv("TOKEN_SECRET", "tp_secret_value")

        config_path = tmp_path / "config.yaml"
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
        monkeypatch.setattr(config, "reload_config", lambda: None)

        config_path.write_text(
            "\n".join([
                "mcp_servers:",
                "  my-server:",
                "    token: ${TOKEN_SECRET}",
                "    url: https://example.com",
                "agent:",
                "  reasoning_effort: medium",
            ]),
            encoding="utf-8",
        )

        config._yaml_file_cache.clear()
        _orig_cache = config._cfg_cache
        config._cfg_cache = None

        # Change agent.reasoning_effort — dirty only that path
        expanded = config._load_yaml_config_file(config_path)
        expanded["agent"]["reasoning_effort"] = "high"
        config._save_yaml_config_file(config_path, expanded,
            dirty_set={("agent", "reasoning_effort")})

        config._cfg_cache = _orig_cache

        saved = config._load_yaml_config_file_raw(config_path, _copy=False)

        # The changed key is written
        assert saved["agent"]["reasoning_effort"] == "high"

        # The untouched ${VAR} in an unrelated section is preserved
        assert "${TOKEN_SECRET}" in str(saved["mcp_servers"]["my-server"]["token"]), (
            f"Expected ${{TOKEN_SECRET}} in token, got "
            f"{saved['mcp_servers']['my-server']['token']!r}"
        )

        # Plain-value keys survive
        assert saved["mcp_servers"]["my-server"]["url"] == "https://example.com"


class TestDiffConfigPaths:
    """``_diff_config_paths`` identifies leaf-level paths that changed."""

    def test_no_changes_returns_empty(self):
        from api.commands import _diff_config_paths

        cfg = {"model": {"provider": "openai", "default": "gpt-4o"}}
        assert _diff_config_paths(cfg, cfg) == set()

    def test_scalar_changed(self):
        from api.commands import _diff_config_paths

        old = {"model": {"default": "gpt-4o"}}
        new = {"model": {"default": "claude-opus-4"}}
        assert _diff_config_paths(old, new) == {("model", "default")}

    def test_nested_leaf_changed(self):
        from api.commands import _diff_config_paths

        old = {"model": {"openai_runtime": "auto", "api_key": "${KEY}"}}
        new = {"model": {"openai_runtime": "codex_app_server", "api_key": "${KEY}"}}
        # Only the changed leaf is dirty, not the whole model branch
        assert _diff_config_paths(old, new) == {("model", "openai_runtime")}

    def test_key_added(self):
        from api.commands import _diff_config_paths

        old = {"model": {"default": "gpt-4o"}}
        new = {"model": {"default": "gpt-4o", "provider": "openai"}}
        assert _diff_config_paths(old, new) == {("model", "provider")}

    def test_key_deleted(self):
        from api.commands import _diff_config_paths

        old = {"model": {"default": "gpt-4o", "provider": "openai"}}
        new = {"model": {"default": "gpt-4o"}}
        assert _diff_config_paths(old, new) == {("model", "provider")}

    def test_multiple_changes(self):
        from api.commands import _diff_config_paths

        old = {"a": 1, "b": {"c": 2, "d": 3}, "e": 4}
        new = {"a": 1, "b": {"c": 99, "d": 3}, "e": 5}
        result = _diff_config_paths(old, new)
        assert result == {("b", "c"), ("e",)}

    def test_both_empty_returns_empty(self):
        from api.commands import _diff_config_paths

        assert _diff_config_paths({}, {}) == set()
