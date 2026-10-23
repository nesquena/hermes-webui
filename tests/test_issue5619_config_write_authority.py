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


class TestReadCompareStillResolves:
    """RAW load must not break comparisons against env-backed config values.

    The rule is "save raw, but READ what the rest of the system sees". A writer
    that compares a raw ``${VAR}`` placeholder against a resolved value silently
    diverges from the reader, which always expands. Greptile caught the first of
    these on PR #7854 (P1).
    """

    def test_skill_toggle_removal_matches_env_backed_entry(
        self, cfg_path, monkeypatch
    ):
        """Enabling a skill must remove a ``${VAR}`` entry it resolves to.

        ``skills.disabled: ["${DISABLED_SKILL}"]`` with ``DISABLED_SKILL=demo``
        means "demo" is disabled. Toggling "demo" ON must clear that entry —
        comparing the raw placeholder to the resolved name leaves it in place,
        so the API reports success while the skill stays disabled.
        """
        import api.config as config

        monkeypatch.setenv("DISABLED_SKILL", "demo")
        config._save_yaml_config_file(
            cfg_path,
            {
                "providers": {"openai": {"api_key": PLACEHOLDER}},
                "skills": {"disabled": ["${DISABLED_SKILL}", "other"]},
            },
        )

        import api.routes as routes

        monkeypatch.setattr(
            routes, "_find_skill_in_dirs", lambda _n, _d: (Path("/x"), "md")
        )
        monkeypatch.setattr(routes, "_active_skills_dir", lambda: Path("/x"))
        monkeypatch.setattr(routes, "_active_skill_search_dirs", lambda d: [d])
        monkeypatch.setattr(routes, "_active_profile_config_path", lambda: cfg_path)
        monkeypatch.setattr(routes, "reload_config", lambda *a, **k: None)
        monkeypatch.setattr(routes, "j", lambda _h, p: p)
        monkeypatch.setattr(
            routes, "bad", lambda _h, m, status=400: {"error": m, "status": status}
        )
        monkeypatch.setattr(routes, "_SKILLS_STATS_CACHE", {"clear": lambda: None})

        response = routes._handle_skill_toggle(
            None, {"name": "demo", "enabled": True}
        )
        assert response.get("ok") is True, response

        raw = config._load_yaml_config_file_raw(cfg_path)
        # "demo" resolves to ${DISABLED_SKILL}; enabling it must clear that slot.
        assert "${DISABLED_SKILL}" not in raw["skills"]["disabled"], (
            "enabling 'demo' left the env-backed ${DISABLED_SKILL} entry in "
            "skills.disabled — the reader expands it, so the skill is still "
            "disabled even though the API returned ok"
        )
        # The unrelated entry and the secret placeholder must be untouched.
        assert "other" in raw["skills"]["disabled"]
        _assert_placeholder_survives(cfg_path, "_handle_skill_toggle (env-backed)")

    def test_skill_toggle_add_dedupes_against_env_backed_entry(
        self, cfg_path, monkeypatch
    ):
        """Disabling a skill must not duplicate an env-backed entry for it."""
        import api.config as config

        import api.routes as routes

        monkeypatch.setenv("DISABLED_SKILL", "demo")
        config._save_yaml_config_file(
            cfg_path,
            {
                "providers": {"openai": {"api_key": PLACEHOLDER}},
                "skills": {"disabled": ["${DISABLED_SKILL}"]},
            },
        )

        monkeypatch.setattr(
            routes, "_find_skill_in_dirs", lambda _n, _d: (Path("/x"), "md")
        )
        monkeypatch.setattr(routes, "_active_skills_dir", lambda: Path("/x"))
        monkeypatch.setattr(routes, "_active_skill_search_dirs", lambda d: [d])
        monkeypatch.setattr(routes, "_active_profile_config_path", lambda: cfg_path)
        monkeypatch.setattr(routes, "reload_config", lambda *a, **k: None)
        monkeypatch.setattr(routes, "j", lambda _h, p: p)
        monkeypatch.setattr(
            routes, "bad", lambda _h, m, status=400: {"error": m, "status": status}
        )
        monkeypatch.setattr(routes, "_SKILLS_STATS_CACHE", {"clear": lambda: None})

        response = routes._handle_skill_toggle(
            None, {"name": "demo", "enabled": False}
        )
        assert response.get("ok") is True, response

        raw = config._load_yaml_config_file_raw(cfg_path)
        resolved = config._expand_env_vars(raw["skills"]["disabled"])
        assert resolved.count("demo") == 1, (
            f"expected exactly one 'demo' after expansion, got {resolved!r}"
        )
        _assert_placeholder_survives(cfg_path, "_handle_skill_toggle (dedupe)")

    def test_default_model_provider_compare_uses_resolved_value(
        self, cfg_path, monkeypatch
    ):
        """set_hermes_default_model must compare providers RESOLVED, not raw.

        ``model.provider: ${MODEL_PROVIDER}`` (=``openai``) is unchanged from the
        caller's perspective, so the write must NOT treat it as a provider switch
        and drop base_url. Reading raw makes ``previous_provider`` the literal
        ``${MODEL_PROVIDER}``, which never equals the resolved "openai".
        """
        import api.config as config

        monkeypatch.setenv("MODEL_PROVIDER", "openai")
        config._save_yaml_config_file(
            cfg_path,
            {
                "providers": {"openai": {"api_key": PLACEHOLDER}},
                "model": {
                    "default": "gpt-4o",
                    "provider": "${MODEL_PROVIDER}",
                    "base_url": "https://kept.example/v1",
                },
            },
        )

        result = config.set_hermes_default_model("gpt-4o", provider="openai")
        assert result.get("ok") is True, result

        raw = config._load_yaml_config_file_raw(cfg_path)
        # Provider did not actually change (both resolve to "openai"), so the
        # custom base_url must survive rather than being dropped by a bogus
        # provider-switch detection.
        assert raw["model"].get("base_url") == "https://kept.example/v1", (
            "base_url was dropped: previous_provider was read raw "
            f"(${'{'}MODEL_PROVIDER{'}'}) instead of resolved 'openai', so the "
            "writer wrongly concluded the provider changed"
        )
        # The provider indirection itself must survive too.
        assert raw["model"].get("provider") == "${MODEL_PROVIDER}", (
            "model.provider placeholder was baked to its resolved value: "
            f"got {raw['model'].get('provider')!r}"
        )
        _assert_placeholder_survives(cfg_path, "set_hermes_default_model (compare)")

    def test_aux_custom_provider_slug_matches_env_backed_name(
        self, cfg_path, monkeypatch
    ):
        """set_auxiliary_model must find a custom provider whose NAME is a ${VAR}.

        The UI sends the RESOLVED slug (``custom:my-server``, derived from an
        expanded read). Matching it against the raw ``${MY_PROVIDER_NAME}``
        entry yields a different slug, so the lookup misses and the slot falls
        back to the wrong endpoint — the raw-vs-resolved mismatch again.
        """
        import api.config as config

        monkeypatch.setenv("MY_PROVIDER_NAME", "My Server")
        config._save_yaml_config_file(
            cfg_path,
            {
                "providers": {"openai": {"api_key": PLACEHOLDER}},
                "custom_providers": [
                    {
                        "name": "${MY_PROVIDER_NAME}",
                        "base_url": "https://correct.example/v1",
                    }
                ],
            },
        )

        config.set_auxiliary_model(
            "vision",
            "custom:my-server",
            "some-model",
        )

        raw = config._load_yaml_config_file_raw(cfg_path)
        slot = raw["auxiliary"]["vision"]
        # The lookup must succeed and resolve the endpoint from the entry.
        assert slot.get("base_url") == "https://correct.example/v1", (
            "custom provider with an env-backed name was not matched: "
            f"got base_url={slot.get('base_url')!r}"
        )
        # The entry's own name indirection must still be on disk.
        assert raw["custom_providers"][0]["name"] == "${MY_PROVIDER_NAME}"
        _assert_placeholder_survives(cfg_path, "set_auxiliary_model (slug match)")


class TestSkillToggleEditTheReadersNames:
    """The toggle must edit the NAMES THE READER SEES, not the raw text.

    Two silent defects from the 2026-09-29 review: a scalar ``${VAR}`` that
    expands to a serialized list was saved back as one list item, after which
    the reader no longer parses it as a list and silently enables the sibling
    names; and the writer compared the env-expanded value literally while the
    reader strips, so ``" demo "`` never matched ``demo`` and the API
    returned ``ok`` while the skill stayed disabled.
    """

    def _toggle(self, monkeypatch, cfg_path, name, enabled):
        """Drive _handle_skill_toggle at the isolated cfg_path; assert ok."""
        import api.routes as routes

        monkeypatch.setattr(
            routes, "_find_skill_in_dirs", lambda _n, _d: (Path("/x"), "md")
        )
        monkeypatch.setattr(routes, "_active_skills_dir", lambda: Path("/x"))
        monkeypatch.setattr(routes, "_active_skill_search_dirs", lambda d: [d])
        monkeypatch.setattr(routes, "_active_profile_config_path", lambda: cfg_path)
        monkeypatch.setattr(routes, "reload_config", lambda *a, **k: None)
        monkeypatch.setattr(routes, "j", lambda _h, p: p)
        monkeypatch.setattr(
            routes, "bad", lambda _h, m, status=400: {"error": m, "status": status}
        )
        monkeypatch.setattr(routes, "_SKILLS_STATS_CACHE", {"clear": lambda: None})
        response = routes._handle_skill_toggle(None, {"name": name, "enabled": enabled})
        assert response.get("ok") is True, response
        return routes

    def test_scalar_placeholder_expanding_to_list_enables_only_target(
        self, cfg_path, monkeypatch, caplog
    ):
        """One scalar ``${VAR}`` holding a list must not enable its siblings.

        ``skills.disabled: ${DISABLED}`` with ``DISABLED='["demo","other"]'``
        reads as both disabled. Enabling ``demo`` must leave ``other``
        disabled: the placeholder cannot survive a partial edit, so the
        remaining resolved name is written explicitly (and logged), rather
        than saved as a list item the reader no longer parses as a list --
        which silently enables ``other``.
        """
        import api.config as config

        monkeypatch.setenv("DISABLED", '["demo","other"]')
        config._save_yaml_config_file(
            cfg_path,
            {
                "providers": {"openai": {"api_key": PLACEHOLDER}},
                "skills": {"disabled": "${DISABLED}"},
            },
        )

        with caplog.at_level("INFO"):
            routes = self._toggle(monkeypatch, cfg_path, "demo", True)

        raw = config._load_yaml_config_file_raw(cfg_path)
        assert config._expand_env_vars(raw["skills"]["disabled"]) == ["other"], (
            "enabling 'demo' must drop it and keep 'other' disabled; the raw "
            f"value is {raw['skills']['disabled']!r}"
        )
        # The reader's own view: demo out, sibling still in.
        assert routes._get_disabled_skill_names_for_profile() == {"other"}
        # Rewriting a placeholder that cannot survive is stated in a log line.
        assert any("placeholder cannot survive" in r.message for r in caplog.records)
        _assert_placeholder_survives(cfg_path, "_handle_skill_toggle (scalar list)")

    def test_platform_disabled_webui_gets_the_same_rule(self, cfg_path, monkeypatch):
        """``skills.platform_disabled.webui`` is held to the same rule.

        The WebUI reader consults this list first, so the identical scalar
        ``${VAR}`` edit must materialize the remaining name here too -- and a
        repeat disable must be a byte-for-byte no-op (idempotency control).
        """
        import api.config as config

        monkeypatch.setenv("DISABLED", '["demo","other"]')
        config._save_yaml_config_file(
            cfg_path,
            {
                "providers": {"openai": {"api_key": PLACEHOLDER}},
                "skills": {
                    "disabled": [],
                    "platform_disabled": {"webui": "${DISABLED}"},
                },
            },
        )

        routes = self._toggle(monkeypatch, cfg_path, "demo", True)

        raw = config._load_yaml_config_file_raw(cfg_path)
        assert config._expand_env_vars(
            raw["skills"]["platform_disabled"]["webui"]
        ) == ["other"], (
            "enabling 'demo' must not silently enable 'other' via the webui "
            f"list; raw is {raw['skills']['platform_disabled']['webui']!r}"
        )
        assert routes._get_disabled_skill_names_for_profile() == {"other"}
        _assert_placeholder_survives(cfg_path, "_handle_skill_toggle (webui list)")

        # Disable control: demo comes back exactly once...
        self._toggle(monkeypatch, cfg_path, "demo", False)
        raw = config._load_yaml_config_file_raw(cfg_path)
        webui = config._expand_env_vars(raw["skills"]["platform_disabled"]["webui"])
        assert webui == ["other", "demo"], f"got {webui!r}"

        # ...and a second disable changes nothing on disk.
        before = cfg_path.read_text(encoding="utf-8")
        self._toggle(monkeypatch, cfg_path, "demo", False)
        assert cfg_path.read_text(encoding="utf-8") == before, (
            "disabling an already-disabled skill rewrote config.yaml; the "
            "toggle must be idempotent when the raw value already reads right"
        )
        assert (
            config._expand_env_vars(
                config._load_yaml_config_file_raw(cfg_path)["skills"][
                    "platform_disabled"
                ]["webui"]
            ).count("demo")
            == 1
        )

    def test_env_value_whitespace_matches_the_reader(self, cfg_path, monkeypatch):
        """The reader strips env-expanded names, so the writer must too.

        ``DISABLED_SKILL=" demo "`` resolves to a padded name the reader
        strips to ``demo``. Enabling ``demo`` must remove the entry --
        comparing the raw expansion keeps it in place, so the API returned
        success while the skill stayed disabled.
        """
        import api.config as config

        monkeypatch.setenv("DISABLED_SKILL", " demo ")
        config._save_yaml_config_file(
            cfg_path,
            {
                "providers": {"openai": {"api_key": PLACEHOLDER}},
                "skills": {"disabled": ["${DISABLED_SKILL}"]},
            },
        )

        routes = self._toggle(monkeypatch, cfg_path, "demo", True)

        raw = config._load_yaml_config_file_raw(cfg_path)
        assert raw["skills"]["disabled"] == [], (
            "enabling 'demo' left the padded ${DISABLED_SKILL} entry behind: "
            f"{raw['skills']['disabled']!r}"
        )
        assert "demo" not in routes._get_disabled_skill_names_for_profile(), (
            "the API returned ok but the reader still reports demo disabled"
        )
        _assert_placeholder_survives(cfg_path, "_handle_skill_toggle (whitespace)")
