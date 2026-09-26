"""Regression matrix for the #5619 config WRITE-target authority rule.

Issue #5619 (nesquena/hermes-webui): "Web UI config discrepancy with
multi-profiles". The maintainer's 2026-09-22 comment asks for
"the write target ... one explicit end-to-end rule and regression matrix".

THE RULE: a config writer must transact on RAW, un-env-expanded YAML.

Why this matters:
  ``_load_yaml_config_file()`` = raw parse + ``_expand_env_vars()``. A writer
  that reads through it and then calls ``_save_yaml_config_file()`` persists
  the *expanded* structure, which does two bad things:

  1. Bakes the literal secret onto disk. ``api_key: ${OPENAI_API_KEY}``
      becomes ``api_key: sk-...`` in plaintext config.yaml, forever, and the
      indirection is destroyed so a later env rotation no longer takes effect.
  2. Cross-profile leak: the expansion resolves against whichever profile
      env is thread-local-active, so a writer under profile A can persist
      profile B's secret into A's config.yaml.

  ``set_max_tokens()`` already transacts on ``_load_yaml_config_file_raw()``
  and is covered by ``tests/test_issue2929_settings_max_tokens.py``. This
  matrix extends that contract to every other writer.

SCOPE NOTE: the three MCP write handlers (``_handle_mcp_server_delete``
/ ``_toggle`` / ``_update``) are deliberately NOT asserted here — PR #6114
owns that lane and the maintainer asked for no parallel fix alongside it.
"""

from pathlib import Path

import pytest

SECRET = "sk-should-never-be-written-to-disk-9f3a"
PLACEHOLDER = "${OPENAI_API_KEY}"


def _seed(config_path: Path) -> None:
    """Write a config.yaml holding an un-expanded secret placeholder."""
    config_path.write_text(
        "providers:\n"
        "  openai:\n"
        f"    api_key: {PLACEHOLDER}\n"
        "model:\n"
        "  default: gpt-4o\n"
        "  provider: openai\n"
        "display:\n"
        "  show_reasoning: false\n"
        "skills:\n"
        "  disabled:\n"
        "    - some-skill\n"
        "dashboard:\n"
        "  kanban:\n"
        "    lane_by_profile: false\n"
        "webui:\n"
        "  dashboard:\n"
        "    enabled: false\n",
        encoding="utf-8",
    )


def _assert_placeholder_survives(config_path: Path, writer_name: str) -> None:
    """The raw bytes on disk must still hold the placeholder, never the secret."""
    text = config_path.read_text(encoding="utf-8")
    assert PLACEHOLDER in text, (
        f"{writer_name} baked the env-expanded secret into config.yaml: "
        f"the {PLACEHOLDER} placeholder was replaced. Config writes must "
        "transact on raw un-expanded YAML (#5619)."
    )
    assert SECRET not in text, (
        f"{writer_name} wrote the literal secret value into config.yaml"
    )


@pytest.fixture
def cfg_path(monkeypatch, tmp_path):
    """Isolated config.yaml, seeded with the secret placeholder."""
    import api.config as config

    config_path = tmp_path / "config.yaml"
    _seed(config_path)
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
    # Writers reload_config() at the end; keep that on the isolated path too.
    monkeypatch.setattr(config, "reload_config", lambda *a, **k: None)
    yield config_path
    _seed(config_path)


class TestConfigPyWriters:
    """api/config.py writers must not expand-then-save."""

    def test_set_reasoning_display_preserves_placeholder(self, cfg_path):
        import api.config as config

        config.set_reasoning_display(True)
        _assert_placeholder_survives(cfg_path, "set_reasoning_display")
        # ...and the write itself must still land.
        assert config._load_yaml_config_file_raw(cfg_path)["display"][
            "show_reasoning"
        ] is True

    def test_set_reasoning_effort_preserves_placeholder(self, cfg_path):
        import api.config as config

        config.set_reasoning_effort("high")
        _assert_placeholder_survives(cfg_path, "set_reasoning_effort")
        assert (
            config._load_yaml_config_file_raw(cfg_path)["agent"]["reasoning_effort"]
            == "high"
        )

    def test_set_hermes_default_model_preserves_placeholder(self, cfg_path):
        import api.config as config

        config.set_hermes_default_model("gpt-4o-mini", "openai")
        _assert_placeholder_survives(cfg_path, "set_hermes_default_model")
        raw = config._load_yaml_config_file_raw(cfg_path)
        assert raw["model"]["default"] == "gpt-4o-mini"

    def test_set_auxiliary_model_preserves_placeholder(self, cfg_path):
        import api.config as config

        config.set_auxiliary_model("vision", "openai", "gpt-4o-mini")
        _assert_placeholder_survives(cfg_path, "set_auxiliary_model")
        raw = config._load_yaml_config_file_raw(cfg_path)
        assert raw["auxiliary"]["vision"]["model"] == "gpt-4o-mini"


class TestOtherModuleWriters:
    """Writers outside api/config.py are held to the same rule."""

    def test_kanban_update_config_payload_preserves_placeholder(
        self, cfg_path, monkeypatch
    ):
        from api import kanban_bridge

        # The response payload pulls board metadata from hermes_cli, which is
        # unrelated to the write rule under test.
        import api.config as config_mod

        monkeypatch.setattr(
            kanban_bridge, "_config_payload", lambda *a, **k: {}
        )
        monkeypatch.setattr(config_mod, "reload_config", lambda *a, **k: None)
        kanban_bridge._update_config_payload({"lane_by_profile": True})
        _assert_placeholder_survives(cfg_path, "kanban_bridge._update_config_payload")
        raw = config_mod._load_yaml_config_file_raw(cfg_path)
        assert raw["dashboard"]["kanban"]["lane_by_profile"] is True

    def test_dashboard_probe_save_preserves_placeholder(self, cfg_path, monkeypatch):
        from api import dashboard_probe
        import api.config as config_mod

        monkeypatch.setattr(config_mod, "reload_config", lambda *a, **k: None)
        dashboard_probe.save_dashboard_config({"enabled": "always", "url": ""})
        _assert_placeholder_survives(cfg_path, "dashboard_probe.save_dashboard_config")
        raw = config_mod._load_yaml_config_file_raw(cfg_path)
        assert raw["webui"]["dashboard"]["enabled"] == "always"

    def test_skill_toggle_preserves_placeholder(self, cfg_path, monkeypatch):
        import api.routes as routes

        # Pretend the skill exists on disk so the writer is reached.
        monkeypatch.setattr(routes, "_find_skill_in_dirs", lambda *a, **k: ("x", "x"))
        monkeypatch.setattr(routes, "_active_skills_dir", lambda: Path("/tmp"))
        monkeypatch.setattr(routes, "_active_skill_search_dirs", lambda d: [d])
        monkeypatch.setattr(routes, "_active_profile_config_path", lambda: cfg_path)
        monkeypatch.setattr(routes, "reload_config", lambda *a, **k: None)
        monkeypatch.setattr(routes, "j", lambda _handler, payload: payload)
        monkeypatch.setattr(
            routes,
            "bad",
            lambda _handler, message, status=400: {"error": message, "status": status},
        )
        monkeypatch.setattr(routes, "_SKILLS_STATS_CACHE", {"clear": lambda: None})

        # enabled=False ADDS the skill to skills.disabled, which exercises the
        # full read-modify-write transaction on the profile's config.yaml.
        response = routes._handle_skill_toggle(
            None, {"name": "demo", "enabled": False}
        )
        assert response.get("ok") is True, response
        _assert_placeholder_survives(cfg_path, "_handle_skill_toggle")
        raw = routes._load_yaml_config_file_raw(cfg_path)
        assert "demo" in raw["skills"]["disabled"]
