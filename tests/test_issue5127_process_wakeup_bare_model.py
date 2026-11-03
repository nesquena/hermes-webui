"""Tests for issue #5127 — process-wakeup bare custom-provider model repair.

When ``_resolve_compatible_session_model_state`` takes the #1855 fast path
(both model and model_provider set), a bare runtime model that matches the
suffix of a slash-qualified ``profile_default_model`` must repair to the full
qualified ID for named custom providers.
"""
from unittest.mock import patch

import pytest


class TestIssue5127CustomProviderBareSuffixRepair:
    def test_profile_scoped_qualified_model_survives_cold_reload(self, monkeypatch):
        """A valid named-custom session must not revert to the profile default."""
        import api.config as config
        from api.routes import _resolve_compatible_session_model_state

        profile_cfg = {
            "custom_providers": [
                {
                    "name": "local-lab",
                    "model": "local-chat-35b-q6",
                    "models": ["local-chat-35b-q6"],
                }
            ]
        }
        monkeypatch.setattr(config, "cfg", {"model": {"provider": "openai-codex"}})
        monkeypatch.setattr(
            "api.routes.get_available_models",
            lambda **_kwargs: {
                "active_provider": "openai-codex",
                "default_model": "gpt-5.6-luna",
                "groups": [
                    {
                        "provider": "OpenAI Codex",
                        "provider_id": "openai-codex",
                        "models": [{"id": "gpt-5.6-luna", "label": "Luna"}],
                    }
                ],
            },
        )

        result = _resolve_compatible_session_model_state(
            "@custom:local-lab:local-chat-35b-q6",
            "custom:local-lab",
            profile_config=profile_cfg,
            prefer_cached_catalog=True,
        )

        assert result == (
            "@custom:local-lab:local-chat-35b-q6",
            "custom:local-lab",
            False,
        )

    def test_repairs_removed_qualified_model_to_same_custom_provider_default(self):
        """A renamed local model must not remain routable only because its provider exists."""
        from api.routes import _resolve_compatible_session_model_state

        profile_cfg = {
            "custom_providers": [
                {
                    "name": "local-lab",
                    "model": "local-chat-35b-q6",
                    "models": ["local-chat-35b-q6"],
                }
            ]
        }

        with patch("api.routes.get_available_models") as mock_catalog:
            result = _resolve_compatible_session_model_state(
                "@custom:local-lab:local-chat-35b-q4",
                "custom:local-lab",
                profile_config=profile_cfg,
                explicit_model_pick=False,
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 0
        assert result == (
            "@custom:local-lab:local-chat-35b-q6",
            "custom:local-lab",
            True,
        )

    @pytest.mark.parametrize(
        "entry_extra",
        [
            {"models": {"saved-a": {}}, "models_discovered": True},
            {"models": {"saved-a": {"context_length": 128000}}},
        ],
        ids=["discovered-snapshot", "dict-metadata"],
    )
    def test_unpinned_models_mapping_does_not_repair_live_pick(self, entry_extra):
        """A discovery snapshot or metadata dict cannot prove a model was removed."""
        from api.routes import _resolve_compatible_session_model_state

        profile_cfg = {
            "custom_providers": [
                {"name": "local-lab", "model": "saved-a", **entry_extra}
            ]
        }

        with patch("api.routes.get_available_models") as mock_catalog:
            result = _resolve_compatible_session_model_state(
                "@custom:local-lab:new-live-c",
                "custom:local-lab",
                profile_config=profile_cfg,
                explicit_model_pick=False,
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 0
        assert result == ("@custom:local-lab:new-live-c", "custom:local-lab", False)

    def test_explicit_pick_is_not_replaced_as_stale(self, monkeypatch):
        """A fresh explicit pick stays put; the endpoint reports a bad model."""
        import api.config as config
        from api.routes import _resolve_compatible_session_model_state

        profile_cfg = {
            "custom_providers": [
                {
                    "name": "lab",
                    "base_url": "http://lab.invalid/v1",
                    "model": "saved-a",
                    "models": ["saved-a"],
                }
            ]
        }
        monkeypatch.setattr(config, "cfg", profile_cfg)

        with patch("api.routes.get_available_models") as mock_catalog:
            model, provider, normalized = _resolve_compatible_session_model_state(
                "@custom:lab:live-b",
                "custom:lab",
                profile_config=profile_cfg,
                explicit_model_pick=True,
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 0
        assert (model, provider, normalized) == ("@custom:lab:live-b", "custom:lab", False)
        assert config.resolve_model_provider(model, explicitly_picked=True)[0] == "live-b"

    @pytest.mark.parametrize("explicit", [False, True])
    def test_namespaced_upstream_id_is_kept_verbatim(self, monkeypatch, explicit):
        """``@safe:`` in a configured ID is part of the upstream ID, not a wrapper."""
        import api.config as config
        from api.routes import _resolve_compatible_session_model_state

        profile_cfg = {
            "custom_providers": [
                {
                    "name": "Lab",
                    "base_url": "http://lab.invalid/v1",
                    "model": "@safe:gpt-4o-mini",
                    "models": ["@safe:gpt-4o-mini"],
                }
            ]
        }
        monkeypatch.setattr(config, "cfg", profile_cfg)

        with patch("api.routes.get_available_models") as mock_catalog:
            model, provider, normalized = _resolve_compatible_session_model_state(
                "@custom:lab:@safe:gpt-4o-mini",
                "custom:lab",
                profile_config=profile_cfg,
                explicit_model_pick=explicit,
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 0
        assert (model, provider, normalized) == (
            "@custom:lab:@safe:gpt-4o-mini",
            "custom:lab",
            False,
        )
        assert (
            config.resolve_model_provider(model, explicitly_picked=explicit)[0]
            == "@safe:gpt-4o-mini"
        )

    def test_removed_model_repairs_to_namespaced_default_verbatim(self, monkeypatch):
        """Repair to the default keeps its real ``@namespace:`` prefix."""
        import api.config as config
        from api.routes import _resolve_compatible_session_model_state

        profile_cfg = {
            "custom_providers": [
                {
                    "name": "Lab",
                    "base_url": "http://lab.invalid/v1",
                    "model": "@safe:gpt-4o-mini",
                    "models": ["@safe:gpt-4o-mini"],
                }
            ]
        }
        monkeypatch.setattr(config, "cfg", profile_cfg)

        model, provider, normalized = _resolve_compatible_session_model_state(
            "@custom:lab:retired-model",
            "custom:lab",
            profile_config=profile_cfg,
            prefer_cached_catalog=True,
        )

        assert (model, provider, normalized) == (
            "@custom:lab:@safe:gpt-4o-mini",
            "custom:lab",
            True,
        )
        assert config.resolve_model_provider(model)[0] == "@safe:gpt-4o-mini"

    def test_own_provider_wrapper_in_allowlist_is_unwrapped(self):
        """A pinned ``@custom:<slug>:x`` spelling still counts as ``x``."""
        from api.routes import _repair_removed_qualified_custom_provider_model

        profile_cfg = {
            "custom_providers": [
                {
                    "name": "lab",
                    "model": "@custom:lab:chat-a",
                    "models": ["@custom:lab:chat-a"],
                }
            ]
        }

        assert (
            _repair_removed_qualified_custom_provider_model(
                "@custom:lab:chat-a", "custom:lab", config_obj=profile_cfg
            )
            is None
        )
        assert (
            _repair_removed_qualified_custom_provider_model(
                "@custom:lab:chat-old", "custom:lab", config_obj=profile_cfg
            )
            == "@custom:lab:chat-a"
        )

    def test_unresolved_env_reference_does_not_drive_repair(self, monkeypatch):
        """Raw profile YAML may hold ``${VAR}``; that is not the real model ID."""
        import api.config as config
        from api.routes import _resolve_compatible_session_model_state

        profile_cfg = {
            "custom_providers": [
                {
                    "name": "lab",
                    "base_url": "http://lab.invalid/v1",
                    "model": "${LAB_MODEL}",
                    "models": ["${LAB_MODEL}"],
                }
            ]
        }
        monkeypatch.setattr(config, "cfg", profile_cfg)

        with patch("api.routes.get_available_models") as mock_catalog:
            model, provider, normalized = _resolve_compatible_session_model_state(
                "@custom:lab:saved-a",
                "custom:lab",
                profile_config=profile_cfg,
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 0
        assert (model, provider, normalized) == ("@custom:lab:saved-a", "custom:lab", False)
        assert config.resolve_model_provider(model)[0] == "saved-a"

    def test_unresolved_env_default_falls_back_to_pinned_list(self, monkeypatch):
        """A concrete pinned list still proves removal when only the default is ``${VAR}``."""
        import api.config as config
        from api.routes import _resolve_compatible_session_model_state

        profile_cfg = {
            "custom_providers": [
                {
                    "name": "lab",
                    "base_url": "http://lab.invalid/v1",
                    "model": "${LAB_MODEL}",
                    "models": ["chat-new"],
                }
            ]
        }
        monkeypatch.setattr(config, "cfg", profile_cfg)

        model, provider, normalized = _resolve_compatible_session_model_state(
            "@custom:lab:chat-old",
            "custom:lab",
            profile_config=profile_cfg,
            prefer_cached_catalog=True,
        )

        assert (model, provider, normalized) == ("@custom:lab:chat-new", "custom:lab", True)
        assert config.resolve_model_provider(model)[0] == "chat-new"

    @staticmethod
    def _bind_profile_env(monkeypatch, tmp_path, env):
        import api.profiles as profiles

        monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda _name: tmp_path)
        monkeypatch.setattr(profiles, "get_profile_runtime_env", lambda _home: dict(env))

    def test_env_default_resolved_by_session_profile_is_kept(self, monkeypatch, tmp_path):
        """``model: ${LAB_MODEL}`` is still the configured default once the profile env expands it."""
        import api.config as config
        from api.routes import _resolve_compatible_session_model_state

        self._bind_profile_env(monkeypatch, tmp_path, {"LAB_MODEL": "live-b"})
        profile_cfg = {
            "custom_providers": [
                {
                    "name": "lab",
                    "base_url": "http://lab.invalid/v1",
                    "model": "${LAB_MODEL}",
                    "models": ["saved-a"],
                }
            ]
        }
        monkeypatch.setattr(config, "cfg", profile_cfg)

        kept = _resolve_compatible_session_model_state(
            "@custom:lab:live-b",
            "custom:lab",
            profile_config=profile_cfg,
            prefer_cached_catalog=True,
            profile="lab-profile",
        )
        assert kept == ("@custom:lab:live-b", "custom:lab", False)
        assert config.resolve_model_provider(kept[0])[0] == "live-b"

        repaired = _resolve_compatible_session_model_state(
            "@custom:lab:removed-x",
            "custom:lab",
            profile_config=profile_cfg,
            prefer_cached_catalog=True,
            profile="lab-profile",
        )
        assert repaired == ("@custom:lab:saved-a", "custom:lab", True)
        assert config.resolve_model_provider(repaired[0])[0] == "saved-a"

    def test_env_default_never_expands_from_server_env_for_named_profile(
        self, monkeypatch, tmp_path
    ):
        """Fail-closed: a value only the server process defines doesn't count for a named profile."""
        import api.config as config
        from api.routes import _resolve_compatible_session_model_state

        self._bind_profile_env(monkeypatch, tmp_path, {})
        monkeypatch.setenv("LAB_MODEL", "live-b")
        profile_cfg = {
            "custom_providers": [
                {
                    "name": "lab",
                    "base_url": "http://lab.invalid/v1",
                    "model": "${LAB_MODEL}",
                    "models": ["saved-a"],
                }
            ]
        }
        monkeypatch.setattr(config, "cfg", profile_cfg)

        model, provider, normalized = _resolve_compatible_session_model_state(
            "@custom:lab:live-b",
            "custom:lab",
            profile_config=profile_cfg,
            prefer_cached_catalog=True,
            profile="lab-profile",
        )

        assert (model, provider, normalized) == ("@custom:lab:saved-a", "custom:lab", True)
        assert config.resolve_model_provider(model)[0] == "saved-a"

    def test_unresolved_env_default_with_profile_falls_back_to_pinned_list(
        self, monkeypatch, tmp_path
    ):
        """No value in the profile env either: keep today's pinned-list fallback."""
        import api.config as config
        from api.routes import _resolve_compatible_session_model_state

        self._bind_profile_env(monkeypatch, tmp_path, {})
        monkeypatch.delenv("LAB_MODEL", raising=False)
        profile_cfg = {
            "custom_providers": [
                {
                    "name": "lab",
                    "base_url": "http://lab.invalid/v1",
                    "model": "${LAB_MODEL}",
                    "models": ["saved-a"],
                }
            ]
        }
        monkeypatch.setattr(config, "cfg", profile_cfg)

        model, provider, normalized = _resolve_compatible_session_model_state(
            "@custom:lab:removed-x",
            "custom:lab",
            profile_config=profile_cfg,
            prefer_cached_catalog=True,
            profile="lab-profile",
        )

        assert (model, provider, normalized) == ("@custom:lab:saved-a", "custom:lab", True)
        assert config.resolve_model_provider(model)[0] == "saved-a"

    def test_root_session_env_default_ignores_switched_process_env(
        self, monkeypatch, tmp_path
    ):
        """A ``default`` session expands with the root ``.env``, not the switched-to profile's."""
        import api.config as config
        import api.profiles as profiles
        from api.routes import _resolve_compatible_session_model_state

        root_home = tmp_path / "root"
        work_home = tmp_path / "work"
        root_home.mkdir()
        work_home.mkdir()
        (root_home / ".env").write_text("LAB_MODEL=live-b\n", encoding="utf-8")
        (work_home / ".env").write_text("LAB_MODEL=server-only\n", encoding="utf-8")
        monkeypatch.setattr(
            profiles,
            "get_hermes_home_for_profile",
            lambda name: root_home if name in ("", "default") else work_home,
        )
        # After switch_profile("work") the process env holds work's .env.
        monkeypatch.setenv("LAB_MODEL", "server-only")
        profile_cfg = {
            "custom_providers": [
                {
                    "name": "lab",
                    "base_url": "http://lab.invalid/v1",
                    "model": "${LAB_MODEL}",
                    "models": ["saved-a"],
                }
            ]
        }
        monkeypatch.setattr(config, "cfg", profile_cfg)

        kept = _resolve_compatible_session_model_state(
            "@custom:lab:live-b",
            "custom:lab",
            profile_config=profile_cfg,
            prefer_cached_catalog=True,
            profile="default",
        )

        assert kept == ("@custom:lab:live-b", "custom:lab", False)
        assert config.resolve_model_provider(kept[0])[0] == "live-b"

    def test_renamed_root_alias_keeps_launcher_value(self, monkeypatch, tmp_path):
        """A renamed root alias (#1612) resolves like 'default': the launcher snapshot is layered under root's .env."""
        import api.config as config
        import api.profiles as profiles
        from api.routes import _resolve_compatible_session_model_state

        root_home = tmp_path / "root"
        root_home.mkdir()
        monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda _name: root_home)
        monkeypatch.setattr(profiles, "_is_root_profile", lambda name: name in ("default", "main"))
        monkeypatch.setattr(profiles, "_INITIAL_PROCESS_ENV", {"LAUNCH_ONLY_MODEL": "launch-x"})
        monkeypatch.delenv("LAUNCH_ONLY_MODEL", raising=False)
        profile_cfg = {
            "custom_providers": [
                {
                    "name": "lab2",
                    "base_url": "http://lab2.invalid/v1",
                    "model": "${LAUNCH_ONLY_MODEL}",
                    "models": ["saved-c"],
                }
            ]
        }
        monkeypatch.setattr(config, "cfg", profile_cfg)

        kept = _resolve_compatible_session_model_state(
            "@custom:lab2:launch-x",
            "custom:lab2",
            profile_config=profile_cfg,
            prefer_cached_catalog=True,
            profile="main",
        )

        assert kept == ("@custom:lab2:launch-x", "custom:lab2", False)

    def test_root_session_env_default_keeps_launcher_value(self, monkeypatch, tmp_path):
        """A launcher-supplied value the root ``.env`` doesn't repeat still counts for root."""
        import api.config as config
        import api.profiles as profiles
        from api.routes import _resolve_compatible_session_model_state

        root_home = tmp_path / "root"
        root_home.mkdir()
        monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda _name: root_home)
        monkeypatch.setattr(profiles, "_INITIAL_PROCESS_ENV", {"LAB_MODEL": "live-b"})
        # Switched to another profile since: its .env value is live, root's isn't.
        monkeypatch.setenv("LAB_MODEL", "server-only")
        profile_cfg = {
            "custom_providers": [
                {
                    "name": "lab",
                    "base_url": "http://lab.invalid/v1",
                    "model": "${LAB_MODEL}",
                    "models": ["saved-a"],
                }
            ]
        }
        monkeypatch.setattr(config, "cfg", profile_cfg)

        kept = _resolve_compatible_session_model_state(
            "@custom:lab:live-b",
            "custom:lab",
            profile_config=profile_cfg,
            prefer_cached_catalog=True,
            profile="default",
        )

        assert kept == ("@custom:lab:live-b", "custom:lab", False)
        assert config.resolve_model_provider(kept[0])[0] == "live-b"

    def test_named_profile_env_ignores_launcher_snapshot(self, monkeypatch, tmp_path):
        """The launcher snapshot is root-only: a named profile stays fail-closed."""
        import api.profiles as profiles
        from api.config import _thread_local_env_value

        monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda _name: tmp_path)
        monkeypatch.setattr(profiles, "get_profile_runtime_env", lambda _home: {})
        monkeypatch.setattr(profiles, "_INITIAL_PROCESS_ENV", {"LAB_MODEL": "live-b"})

        with profiles.profile_env_for_thread_readonly("work", include_root=True):
            assert _thread_local_env_value("LAB_MODEL") == ""

    def test_thread_readonly_env_still_skips_root_by_default(self, monkeypatch, tmp_path):
        """Other callers keep the root no-op unless they opt in with ``include_root``."""
        import api.profiles as profiles
        from api.config import _thread_local_env_value

        monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda _name: tmp_path)
        monkeypatch.setattr(profiles, "get_profile_runtime_env", lambda _home: {"LAB_MODEL": "live-b"})
        monkeypatch.setenv("LAB_MODEL", "server-only")

        with profiles.profile_env_for_thread_readonly("default"):
            assert _thread_local_env_value("LAB_MODEL") == "server-only"
        with profiles.profile_env_for_thread_readonly("default", include_root=True):
            assert _thread_local_env_value("LAB_MODEL") == "live-b"
        assert _thread_local_env_value("LAB_MODEL") == "server-only"

    @pytest.mark.parametrize(
        "serialized",
        [
            '[{"id": "saved-a"}, {"id": "live-b"}]',
            "[{'id': 'saved-a'}, {'model': 'live-b'}]",
        ],
    )
    def test_serialized_mapping_allowlist_keeps_member_ids(self, monkeypatch, serialized):
        """A serialized list of mappings resolves through id/model/name, not str()."""
        import api.config as config
        from api.routes import _resolve_compatible_session_model_state

        profile_cfg = {
            "custom_providers": [
                {
                    "name": "lab",
                    "base_url": "http://lab.invalid/v1",
                    "model": "saved-a",
                    "models": serialized,
                }
            ]
        }
        monkeypatch.setattr(config, "cfg", profile_cfg)

        kept = _resolve_compatible_session_model_state(
            "@custom:lab:live-b",
            "custom:lab",
            profile_config=profile_cfg,
            prefer_cached_catalog=True,
        )
        assert kept == ("@custom:lab:live-b", "custom:lab", False)
        assert config.resolve_model_provider(kept[0])[0] == "live-b"

        repaired = _resolve_compatible_session_model_state(
            "@custom:lab:removed-x",
            "custom:lab",
            profile_config=profile_cfg,
            prefer_cached_catalog=True,
        )
        assert repaired == ("@custom:lab:saved-a", "custom:lab", True)
        assert config.resolve_model_provider(repaired[0])[0] == "saved-a"

    def test_configured_default_outside_pinned_list_is_kept(self, monkeypatch):
        """The singular ``model`` is still a picker entry, so it isn't stale."""
        import api.config as config
        from api.routes import _resolve_compatible_session_model_state

        profile_cfg = {
            "custom_providers": [
                {
                    "name": "lab",
                    "base_url": "http://lab.invalid/v1",
                    "model": "live-b",
                    "models": ["saved-a"],
                }
            ]
        }
        monkeypatch.setattr(config, "cfg", profile_cfg)

        model, provider, normalized = _resolve_compatible_session_model_state(
            "@custom:lab:live-b",
            "custom:lab",
            profile_config=profile_cfg,
            prefer_cached_catalog=True,
        )

        assert (model, provider, normalized) == ("@custom:lab:live-b", "custom:lab", False)
        assert config.resolve_model_provider(model)[0] == "live-b"

    @pytest.mark.parametrize(
        "sentinel", ["__discovered_model_catalog__", "__explicit_model_allowlist__"]
    )
    def test_legacy_sentinel_keys_are_not_model_ids(self, monkeypatch, sentinel):
        """Pre-fix sentinels inside a pinned mapping never become the repair target."""
        import api.config as config
        from api.routes import _resolve_compatible_session_model_state

        profile_cfg = {
            "custom_providers": [
                {
                    "name": "lab",
                    "base_url": "http://lab.invalid/v1",
                    "discover_models": False,
                    "model": "gone-default",
                    "models": {sentinel: True, "real-a": {}},
                }
            ]
        }
        monkeypatch.setattr(config, "cfg", profile_cfg)

        model, provider, normalized = _resolve_compatible_session_model_state(
            "@custom:lab:removed-x",
            "custom:lab",
            profile_config=profile_cfg,
            prefer_cached_catalog=True,
        )

        assert (model, provider, normalized) == ("@custom:lab:real-a", "custom:lab", True)
        assert config.resolve_model_provider(model)[0] == "real-a"

    def test_no_profile_config_never_repairs_from_shared_cfg(self, monkeypatch):
        """The shared cfg may belong to another profile, so it can't drive a rewrite."""
        import api.config as config
        from api.routes import _repair_removed_qualified_custom_provider_model

        monkeypatch.setattr(
            config,
            "cfg",
            {
                "custom_providers": [
                    {"name": "local-lab", "model": "chat-new", "models": ["chat-new"]}
                ]
            },
        )

        assert (
            _repair_removed_qualified_custom_provider_model(
                "@custom:local-lab:chat-old",
                "custom:local-lab",
                config_obj=None,
            )
            is None
        )

    def test_no_profile_session_keeps_valid_pick_from_active_config(self, monkeypatch):
        """Without a profile config, a valid pick stays on its provider (no rewrite)."""
        import api.config as config
        from api.routes import _resolve_compatible_session_model_state

        monkeypatch.setattr(
            config,
            "cfg",
            {
                "model": {"provider": "openai-codex"},
                "custom_providers": [
                    {
                        "name": "local-lab",
                        "model": "local-chat-35b-q6",
                        "models": ["local-chat-35b-q6"],
                    }
                ],
            },
        )

        with patch("api.routes.get_available_models") as mock_catalog:
            result = _resolve_compatible_session_model_state(
                "@custom:local-lab:local-chat-35b-q6",
                "custom:local-lab",
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 0
        assert result == (
            "@custom:local-lab:local-chat-35b-q6",
            "custom:local-lab",
            False,
        )

    def test_repair_ignores_undecodable_serialized_allowlist(self):
        """A serialized list that fails to parse can't prove a model was removed."""
        from api.routes import _repair_removed_qualified_custom_provider_model

        profile_cfg = {
            "custom_providers": [
                {"name": "local-lab", "model": "chat-a", "models": '["chat-a", "chat-b"'}
            ]
        }

        assert (
            _repair_removed_qualified_custom_provider_model(
                "@custom:local-lab:chat-a",
                "custom:local-lab",
                config_obj=profile_cfg,
            )
            is None
        )

    def test_repair_honours_single_model_string_allowlist(self):
        """A plain ``models: chat-new`` string is a one-model allowlist."""
        from api.routes import _repair_removed_qualified_custom_provider_model

        profile_cfg = {
            "custom_providers": [
                {"name": "local-lab", "model": "chat-new", "models": "chat-new"}
            ]
        }

        assert (
            _repair_removed_qualified_custom_provider_model(
                "@custom:local-lab:chat-old",
                "custom:local-lab",
                config_obj=profile_cfg,
            )
            == "@custom:local-lab:chat-new"
        )

    def test_repair_replacement_stays_inside_pinned_allowlist(self):
        """A singular default outside the pinned list is not a valid replacement."""
        from api.routes import _repair_removed_qualified_custom_provider_model

        profile_cfg = {
            "custom_providers": [
                {"name": "local-lab", "model": "chat-default", "models": ["chat-a"]}
            ]
        }

        assert (
            _repair_removed_qualified_custom_provider_model(
                "@custom:local-lab:chat-old",
                "custom:local-lab",
                config_obj=profile_cfg,
            )
            == "@custom:local-lab:chat-a"
        )

    def test_repair_fails_closed_on_colliding_slugs(self):
        """Two entries normalising to one slug must not pick a winner."""
        from api.routes import _repair_removed_qualified_custom_provider_model

        profile_cfg = {
            "custom_providers": [
                {"name": "Local Lab", "model": "chat-a", "models": ["chat-a"]},
                {"name": "local-lab", "model": "chat-old", "models": ["chat-old"]},
            ]
        }

        assert (
            _repair_removed_qualified_custom_provider_model(
                "@custom:local-lab:chat-old",
                "custom:local-lab",
                config_obj=profile_cfg,
            )
            is None
        )

    def test_fast_path_rejects_longer_parsed_qualifier(self):
        """The parsed qualifier must equal the session provider to skip the catalog."""
        from api.routes import _resolve_compatible_session_model_state

        profile_cfg = {
            "custom_providers": [
                {"name": "host.example", "model": "m", "models": ["m"]}
            ]
        }

        with patch("api.routes.get_available_models", return_value={"groups": []}) as mock_catalog:
            _resolve_compatible_session_model_state(
                "@custom:host.example:8080:m",
                "custom:host.example",
                profile_config=profile_cfg,
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 1

    def test_removed_custom_provider_is_not_kept_by_unrelated_entry(self, monkeypatch):
        """Another configured custom provider must not keep a removed one routable."""
        import api.config as config
        from api.routes import _resolve_compatible_session_model_state

        profile_cfg = {
            "custom_providers": [
                {"name": "other-lab", "model": "other-chat", "models": ["other-chat"]}
            ]
        }
        monkeypatch.setattr(config, "cfg", {"model": {"provider": "openai-codex"}})
        monkeypatch.setattr(
            "api.routes.get_available_models",
            lambda **_kwargs: {
                "active_provider": "openai-codex",
                "default_model": "gpt-5.6-luna",
                "groups": [
                    {
                        "provider": "OpenAI Codex",
                        "provider_id": "openai-codex",
                        "models": [{"id": "gpt-5.6-luna", "label": "Luna"}],
                    }
                ],
            },
        )

        result = _resolve_compatible_session_model_state(
            "@custom:local-lab:local-chat-35b-q6",
            "custom:local-lab",
            profile_config=profile_cfg,
            prefer_cached_catalog=True,
        )

        assert result != (
            "@custom:local-lab:local-chat-35b-q6",
            "custom:local-lab",
            False,
        )

    def test_fast_path_repairs_bare_suffix_to_profile_default(self):
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            result = _resolve_compatible_session_model_state(
                "grok-composer-2.5-fast",
                "custom:my-proxy",
                profile_provider="custom:my-proxy",
                profile_default_model="x-ai/grok-composer-2.5-fast",
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 0
        assert result == ("x-ai/grok-composer-2.5-fast", "custom:my-proxy", True)

    def test_fast_path_skips_repair_when_profile_provider_mismatches(self):
        """Regression: custom:other-proxy must not inherit my-proxy's qualified default."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            result = _resolve_compatible_session_model_state(
                "grok-composer-2.5-fast",
                "custom:other-proxy",
                profile_provider="custom:my-proxy",
                profile_default_model="x-ai/grok-composer-2.5-fast",
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 0
        assert result == ("grok-composer-2.5-fast", "custom:other-proxy", False)

    def test_fast_path_repairs_generic_custom_provider(self):
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            result = _resolve_compatible_session_model_state(
                "some-model",
                "custom",
                profile_default_model="vendor/some-model",
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 0
        assert result == ("vendor/some-model", "custom", True)

    def test_fast_path_does_not_rewrite_unrelated_bare_model(self):
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            result = _resolve_compatible_session_model_state(
                "other-model",
                "custom:my-proxy",
                profile_default_model="x-ai/grok-composer-2.5-fast",
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 0
        assert result == ("other-model", "custom:my-proxy", False)

    def test_fast_path_keeps_qualified_model_unchanged(self):
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            result = _resolve_compatible_session_model_state(
                "x-ai/grok-composer-2.5-fast",
                "custom:my-proxy",
                profile_default_model="x-ai/grok-composer-2.5-fast",
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 0
        assert result == ("x-ai/grok-composer-2.5-fast", "custom:my-proxy", False)

    def test_slow_path_repairs_bare_suffix_to_profile_default_for_custom_provider(self):
        """Regression #5225: async continuations may arrive without usable model_provider."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "openrouter",
                "default_model": "openai/gpt-5.5",
                "groups": [],
            }
            result = _resolve_compatible_session_model_state(
                "grok-composer-2.5-fast",
                None,
                profile_provider="custom:my-proxy",
                profile_default_model="x-ai/grok-composer-2.5-fast",
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 1
        assert result == ("x-ai/grok-composer-2.5-fast", "custom:my-proxy", True)

    def test_fast_path_repairs_non_default_bare_model_of_custom_provider(self):
        """Regression: non-default bare model must be dynamically repaired if listed under the custom provider."""
        from api.routes import _resolve_compatible_session_model_state

        _mock_cfg = {
            "custom_providers": [
                {
                    "name": "my-proxy",
                    "base_url": "https://llm-proxy.ext.ben.io/v1",
                    "models": {
                        "x-ai/grok-composer-2.5-fast": {},
                        "umans/umans-glm-5.2": {},
                    }
                }
            ]
        }

        with patch("api.routes.get_available_models") as mock_catalog, \
             patch("api.config.cfg", _mock_cfg):
            result = _resolve_compatible_session_model_state(
                "grok-composer-2.5-fast",
                "custom:my-proxy",
                profile_provider="custom:my-proxy",
                profile_default_model="umans/umans-glm-5.2",
                profile_config=_mock_cfg,
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 0
        assert result == ("x-ai/grok-composer-2.5-fast", "custom:my-proxy", True)

    def test_slow_path_repairs_non_default_bare_model_of_custom_provider(self):
        """Regression: slow-path must also dynamically repair non-default bare model if listed under custom provider."""
        from api.routes import _resolve_compatible_session_model_state

        _mock_cfg = {
            "custom_providers": [
                {
                    "name": "my-proxy",
                    "base_url": "https://llm-proxy.ext.ben.io/v1",
                    "models": {
                        "x-ai/grok-composer-2.5-fast": {},
                        "umans/umans-glm-5.2": {},
                    }
                }
            ]
        }

        with patch("api.routes.get_available_models") as mock_catalog, \
             patch("api.config.cfg", _mock_cfg):
            mock_catalog.return_value = {
                "active_provider": "openrouter",
                "default_model": "openai/gpt-5.5",
                "groups": [],
            }
            result = _resolve_compatible_session_model_state(
                "grok-composer-2.5-fast",
                None,
                profile_provider="custom:my-proxy",
                profile_default_model="umans/umans-glm-5.2",
                profile_config=_mock_cfg,
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 1
        assert result == ("x-ai/grok-composer-2.5-fast", "custom:my-proxy", True)

    def test_slow_path_does_not_rewrite_unrelated_bare_model_for_custom_provider(self):
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "openrouter",
                "default_model": "openai/gpt-5.5",
                "groups": [],
            }
            result = _resolve_compatible_session_model_state(
                "other-model",
                None,
                profile_provider="custom:my-proxy",
                profile_default_model="x-ai/grok-composer-2.5-fast",
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 1
        assert result == ("other-model", "custom:my-proxy", False)

    def test_openai_codex_stale_openai_slash_still_uses_slow_path(self):
        """Regression: openai/... under openai-codex must not take bare fast return."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "openai-codex",
                "default_model": "gpt-5.5",
                "groups": [],
            }
            result = _resolve_compatible_session_model_state(
                "openai/gpt-5.4-mini",
                "openai-codex",
                profile_default_model="gpt-5.5",
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count >= 1
        assert result[0] == "gpt-5.5"
        assert result[2] is True


class TestRepairBareCustomProviderModel:
    def test_uses_config_order_when_suffixes_collide(self):
        from api.routes import _repair_bare_custom_provider_model

        custom_cfg = [
            {
                "name": "llm-proxy",
                "model": "org-a/shared-suffix",
                "models": {"org-b/shared-suffix": {}},
            }
        ]
        with patch("api.config.cfg", {"custom_providers": custom_cfg}):
            assert _repair_bare_custom_provider_model(
                "shared-suffix", "custom:llm-proxy",
                config_obj={"custom_providers": custom_cfg},
            ) == "org-a/shared-suffix"

    def test_repairs_display_name_provider_via_slug(self):
        from api.routes import _repair_bare_custom_provider_model

        cfg = {
            "custom_providers": [
                {
                    "name": "My Proxy",
                    "models": {"x-ai/grok-composer-2.5-fast": {}},
                }
            ]
        }
        assert _repair_bare_custom_provider_model(
            "grok-composer-2.5-fast",
            "custom:my-proxy",
            config_obj=cfg,
        ) == "x-ai/grok-composer-2.5-fast"

    def test_repairs_list_form_models_catalog(self):
        from api.routes import _repair_bare_custom_provider_model

        cfg = {
            "custom_providers": [
                {
                    "name": "team",
                    "models": [
                        "profile-list/shared",
                        {"id": "profile-list/other"},
                    ],
                }
            ]
        }
        assert _repair_bare_custom_provider_model(
            "shared",
            "custom:team",
            config_obj=cfg,
        ) == "profile-list/shared"

    def test_uses_get_config_when_config_obj_is_none(self):
        from unittest.mock import patch
        from api.routes import _repair_bare_custom_provider_model

        cfg = {
            "custom_providers": [
                {
                    "name": "team",
                    "models": {"vendor/shared": {}},
                }
            ]
        }
        with patch("api.config.get_config") as mock_get_config:
            mock_get_config.return_value = cfg
            result = _repair_bare_custom_provider_model(
                "shared",
                "custom:team",
                config_obj=None,
            )
        assert result == "vendor/shared"
        mock_get_config.assert_called_once_with()

    def test_malformed_custom_provider_name_fails_closed(self):
        from api.routes import _repair_bare_custom_provider_model

        bad_cfg = {
            "custom_providers": [
                {"name": None, "models": {"vendor/ok-model": {}}},
                {"name": "good-proxy", "models": {"vendor/ok-model": {}}},
            ]
        }
        assert _repair_bare_custom_provider_model(
            "ok-model",
            "custom:good-proxy",
            config_obj=bad_cfg,
        ) == "vendor/ok-model"

    def test_profile_config_scoped_not_global_cfg(self):
        from api.routes import _repair_bare_custom_provider_model

        profile_cfg = {
            "custom_providers": [
                {
                    "name": "team",
                    "models": {"profile-vendor/shared": {}},
                }
            ]
        }
        global_cfg = {
            "custom_providers": [
                {
                    "name": "team",
                    "models": {"wrong-vendor/shared": {}},
                }
            ]
        }
        with patch("api.config.cfg", global_cfg):
            assert _repair_bare_custom_provider_model(
                "shared",
                "custom:team",
                config_obj=profile_cfg,
            ) == "profile-vendor/shared"


class TestReadProfileModelConfigWithExplicitProvider:
    def test_returns_profile_default_when_session_has_model_provider(self, tmp_path, monkeypatch):
        from api.routes import _read_profile_model_config

        profile_home = tmp_path / "prof"
        profile_home.mkdir()
        (profile_home / "config.yaml").write_text(
            "model:\n  provider: custom:my-proxy\n  default: x-ai/grok-composer-2.5-fast\n",
            encoding="utf-8",
        )

        class _Session:
            profile = "testprof"

        monkeypatch.setattr(
            "api.profiles.get_hermes_home_for_profile",
            lambda _p: str(profile_home),
        )

        provider, default, _ = _read_profile_model_config(_Session(), "custom:my-proxy")
        assert provider is None
        assert default == "x-ai/grok-composer-2.5-fast"

    def test_returns_none_default_when_session_provider_differs_from_profile(
        self, tmp_path, monkeypatch,
    ):
        from api.routes import _read_profile_model_config

        profile_home = tmp_path / "prof"
        profile_home.mkdir()
        (profile_home / "config.yaml").write_text(
            "model:\n  provider: custom:my-proxy\n  default: x-ai/grok-composer-2.5-fast\n",
            encoding="utf-8",
        )

        class _Session:
            profile = "testprof"

        monkeypatch.setattr(
            "api.profiles.get_hermes_home_for_profile",
            lambda _p: str(profile_home),
        )

        provider, default, _ = _read_profile_model_config(_Session(), "custom:other-proxy")
        assert provider is None
        assert default is None