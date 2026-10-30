"""#7770: WebUI must surface plugin-registered skills in /api/skills and resolve
them through /api/skills/content.

The agent's plugin manager (``hermes_cli.plugins.get_plugin_manager()``) is the
authoritative source of plugin-skill metadata. Before the fix the WebUI listed
only directory-installed skills, so ``obra/superpowers`` (and any other enabled
plugin) had skills invisible to the WebUI even though
``skill_view("superpowers:brainstorming")`` worked inside the agent.

The fix is fail-soft: when the plugin manager is unavailable (isolated WebUI
without Hermes Agent on ``sys.path``) the endpoints must still serve the
directory-based listing instead of 500-ing.

These tests pin:

* listing: plugin-registered skills appear with their qualified ``name`` and a
  ``source: "plugin"`` marker, alongside directory skills;
* category filter: ``category=plugin`` returns only plugin skills, a non-plugin
  category excludes them, no category returns the combined set;
* cold start: the plugin registry is only populated after discovery, so the
  helper must call ``discover_and_load(force=False)`` before reading it;
* content lookup: a plugin-qualified name resolves through the plugin manager
  when the directory scan misses;
* fail-soft: a plugin manager that raises does not break the directory listing.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ``_skills_list_from_dir`` imports ``agent.skill_utils`` and
# ``tools.skills_tool`` on the listing path, so these tests only make sense
# where the hermes-agent Python modules are importable. The marker already
# exists in tests/conftest.py; without it these explode with ModuleNotFoundError
# in the CI shards that lack the agent (the failure class flagged on PR #7781).
from tests.conftest import requires_agent_modules

pytestmark = requires_agent_modules


class _FakePluginManager:
    """Mirror of the agent PluginManager surface the WebUI relies on."""

    def __init__(
        self,
        skills=None,
        find_paths=None,
        raise_on_list=False,
        raise_on_find=False,
        raise_on_discover=False,
    ):
        self._skills = list(skills or [])
        self._find_paths = dict(find_paths or {})
        self._raise_on_list = raise_on_list
        self._raise_on_find = raise_on_find
        self._raise_on_discover = raise_on_discover
        # The agent only populates its plugin-skill registry during discovery,
        # so a cold-started WebUI sees an empty registry until discover runs.
        # Tests assert on this to pin the cold-start regression.
        self.discover_calls: list[bool] = []

    def discover_and_load(self, force: bool = False) -> None:
        self.discover_calls.append(force)
        if self._raise_on_discover:
            raise RuntimeError("simulated discover_and_load failure")

    def list_plugin_skill_metadata(self):
        if self._raise_on_list:
            raise RuntimeError("simulated plugin-manager failure")
        return list(self._skills)

    def find_plugin_skill(self, qualified_name):
        if self._raise_on_find:
            raise RuntimeError("simulated find_plugin_skill failure")
        return self._find_paths.get(qualified_name)


def _write_local_skill(skills_dir: Path, *, name, description="local desc") -> Path:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nLocal body\n",
        encoding="utf-8",
    )
    return skill_dir


def _write_plugin_skill(base: Path, *, qualified_name, description="plugin desc") -> Path:
    namespace, bare = qualified_name.split(":", 1)
    skill_dir = base / namespace / bare
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        f"---\nname: {qualified_name}\ndescription: {description}\n---\n\nPlugin body\n",
        encoding="utf-8",
    )
    return skill_md


def _listing(skills_dir, *, manager, category=None):
    """Call _skills_list_from_dir with the plugin manager pinned."""
    from api import routes

    with patch.object(routes, "_get_disabled_skill_names_for_profile", return_value=set()):
        with patch.object(routes, "_active_skill_search_dirs", return_value=[skills_dir]):
            with patch.object(routes, "_get_plugin_manager_for_visibility", return_value=manager):
                return routes._skills_list_from_dir(skills_dir, category=category)


class TestPluginSkillsInListing:
    @pytest.fixture
    def skills_dir(self, tmp_path):
        d = tmp_path / "skills"
        d.mkdir()
        _write_local_skill(d, name="local-alpha", description="Local alpha")
        _write_local_skill(d, name="local-beta", description="Local beta")
        return d

    def test_plugin_skills_appear_alongside_directory_skills(self, skills_dir):
        manager = _FakePluginManager(
            skills=[
                {"name": "superpowers:brainstorming", "description": "Brainstorm first"},
                {"name": "superpowers:writing-plans", "description": "Plan the work"},
            ]
        )

        result = _listing(skills_dir, manager=manager)

        skills = {s["name"]: s for s in result["skills"]}
        assert "local-alpha" in skills
        assert "local-beta" in skills
        # Qualified name preserved verbatim so the picker can round-trip it.
        assert "superpowers:brainstorming" in skills
        assert "superpowers:writing-plans" in skills

        entry = skills["superpowers:brainstorming"]
        assert entry["description"] == "Brainstorm first"
        assert entry["category"] == "plugin"
        assert entry["source"] == "plugin"
        assert entry["plugin"] == "superpowers"
        assert entry["disabled"] is False
        # Directory skills must NOT be marked as plugin-sourced.
        assert "source" not in skills["local-alpha"]

    def test_listing_triggers_cold_start_discovery(self, skills_dir):
        """A freshly-restarted WebUI has an empty plugin-skill registry until
        discovery runs, so the listing must discover before it reads."""
        manager = _FakePluginManager(
            skills=[{"name": "superpowers:brainstorming", "description": "d"}]
        )

        _listing(skills_dir, manager=manager)

        assert manager.discover_calls == [False]

    def test_directory_skill_wins_name_collision_with_plugin(self, skills_dir):
        """A local skill must not be shadowed (or duplicated) by a plugin skill
        that reports the identical name."""
        manager = _FakePluginManager(
            skills=[
                {"name": "local-alpha", "description": "plugin copy"},
                {"name": "superpowers:brainstorming", "description": "d"},
            ]
        )

        result = _listing(skills_dir, manager=manager)

        names = [s["name"] for s in result["skills"]]
        assert names.count("local-alpha") == 1
        assert skills_entry(result, "local-alpha")["description"] == "Local alpha"

    def test_unqualified_plugin_name_is_filtered(self, skills_dir):
        """Only the qualified ``plugin:skill`` form is unambiguous enough to put
        in the picker; a bare name could shadow a directory skill."""
        manager = _FakePluginManager(
            skills=[
                {"name": "brainstorming", "description": "unqualified"},
                {"name": ":leading", "description": "empty namespace"},
                {"name": "trailing:", "description": "empty skill"},
                {"name": "superpowers:ok", "description": "valid"},
            ]
        )

        result = _listing(skills_dir, manager=manager)

        names = {s["name"] for s in result["skills"]}
        assert "superpowers:ok" in names
        assert "brainstorming" not in names
        assert ":leading" not in names
        assert "trailing:" not in names

    def test_malformed_plugin_metadata_is_skipped(self, skills_dir):
        manager = _FakePluginManager(
            skills=[
                None,
                "not-a-dict",
                {},
                {"description": "no name"},
                {"name": "superpowers:ok", "description": "valid"},
            ]
        )

        result = _listing(skills_dir, manager=manager)

        names = {s["name"] for s in result["skills"]}
        assert "superpowers:ok" in names

    def test_non_list_metadata_is_ignored(self, skills_dir):
        manager = _FakePluginManager()
        manager.list_plugin_skill_metadata = lambda: {"name": "superpowers:ok"}

        result = _listing(skills_dir, manager=manager)

        names = {s["name"] for s in result["skills"]}
        assert "superpowers:ok" not in names
        assert "local-alpha" in names


def skills_entry(result, name):
    return {s["name"]: s for s in result["skills"]}[name]


class TestCategoryFilter:
    @pytest.fixture
    def skills_dir(self, tmp_path):
        d = tmp_path / "skills"
        d.mkdir()
        # Nested one level so _skill_category_from_path yields the group name;
        # a flat skill under the local root is uncategorized by design.
        (d / "group" / "local-alpha").mkdir(parents=True)
        (d / "group" / "local-alpha" / "SKILL.md").write_text(
            "---\nname: local-alpha\ndescription: Local alpha\n---\n\nLocal body\n",
            encoding="utf-8",
        )
        return d

    def test_category_plugin_returns_only_plugin_skills(self, skills_dir):
        manager = _FakePluginManager(
            skills=[
                {"name": "superpowers:brainstorming", "description": "d"},
                {"name": "other:skill", "description": "d"},
            ]
        )

        result = _listing(skills_dir, manager=manager, category="plugin")

        assert {s["name"] for s in result["skills"]} == {
            "superpowers:brainstorming",
            "other:skill",
        }

    def test_non_plugin_category_excludes_plugin_skills(self, skills_dir):
        manager = _FakePluginManager(
            skills=[{"name": "superpowers:brainstorming", "description": "d"}]
        )

        result = _listing(skills_dir, manager=manager, category="group")

        assert {s["name"] for s in result["skills"]} == {"local-alpha"}

    def test_no_category_returns_combined_set(self, skills_dir):
        manager = _FakePluginManager(
            skills=[{"name": "superpowers:brainstorming", "description": "d"}]
        )

        result = _listing(skills_dir, manager=manager, category=None)

        assert {s["name"] for s in result["skills"]} == {
            "local-alpha",
            "superpowers:brainstorming",
        }

    def test_categories_include_plugin(self, skills_dir):
        manager = _FakePluginManager(
            skills=[{"name": "superpowers:brainstorming", "description": "d"}]
        )

        result = _listing(skills_dir, manager=manager)

        assert "plugin" in result["categories"]


class TestFailSoft:
    @pytest.fixture
    def skills_dir(self, tmp_path):
        d = tmp_path / "skills"
        d.mkdir()
        _write_local_skill(d, name="local-alpha", description="Local alpha")
        return d

    def test_listing_failure_keeps_directory_skills(self, skills_dir):
        manager = _FakePluginManager(raise_on_list=True)

        result = _listing(skills_dir, manager=manager)

        assert {s["name"] for s in result["skills"]} == {"local-alpha"}

    def test_discover_failure_still_lists_registry(self, skills_dir):
        """discover_and_load throwing must not suppress an already-populated
        registry (the retry path), nor crash the listing."""
        manager = _FakePluginManager(
            skills=[{"name": "superpowers:brainstorming", "description": "d"}],
            raise_on_discover=True,
        )

        result = _listing(skills_dir, manager=manager)

        assert "superpowers:brainstorming" in {s["name"] for s in result["skills"]}

    def test_manager_acquisition_failure_keeps_directory_skills(self, skills_dir):
        from api import routes

        with patch.object(routes, "_get_disabled_skill_names_for_profile", return_value=set()):
            with patch.object(routes, "_active_skill_search_dirs", return_value=[skills_dir]):
                with patch.object(
                    routes,
                    "_get_plugin_manager_for_visibility",
                    side_effect=RuntimeError("no plugin subsystem"),
                ):
                    result = routes._skills_list_from_dir(skills_dir)

        assert {s["name"] for s in result["skills"]} == {"local-alpha"}

    def test_missing_local_dir_does_not_hide_plugin_skills(self, tmp_path):
        """A user who never created a local skills/ directory still gets plugin
        skills (#7770 reproduces against a fresh profile)."""

        missing = tmp_path / "does-not-exist"
        manager = _FakePluginManager(
            skills=[{"name": "superpowers:brainstorming", "description": "d"}]
        )

        result = _listing(missing, manager=manager)

        assert "superpowers:brainstorming" in {s["name"] for s in result["skills"]}
        assert result["success"] is True
        # The directory is still created for backward compatibility.
        assert missing.exists()

    def test_missing_local_dir_and_no_skills_keeps_created_notice(self, tmp_path):
        """The pre-#7770 "directory created" notice must survive so the Settings
        panel is not silently broken on a first-ever call."""

        missing = tmp_path / "does-not-exist"

        result = _listing(missing, manager=_FakePluginManager())

        assert result["skills"] == []
        assert "created at" in result["message"]


class TestSkillContentLookup:
    def _resolve(self, name, *, manager, skills_dir, file=None, native_view=None):
        """Drive the /api/skills/content route with plugins pinned.

        ``native_view`` is the JSON payload the Agent's native ``skill_view``
        returns for a plugin skill. The route delegates plugin content to that
        native viewer (contract parity with the Agent, #7770), and the native
        viewer resolves against the *real* registry — so a fake manager's path
        is unknown to it and the delegate has to be pinned for these tests.
        """
        import api.routes as routes

        captured = {}

        def fake_j(handler, payload, status=200, extra_headers=None):
            captured["payload"] = payload
            captured["status"] = status
            return True

        from urllib.parse import urlparse

        query = f"?name={name}"
        if file:
            query += f"&file={file}"
        parsed = urlparse("/api/skills/content" + query)

        handler = MagicMock()
        with ExitStack() as stack:
            stack.enter_context(patch.object(routes, "j", side_effect=fake_j))
            stack.enter_context(patch.object(routes, "bad", side_effect=fake_j))
            stack.enter_context(patch.object(routes, "_active_skills_dir", return_value=skills_dir))
            stack.enter_context(
                patch.object(routes, "_active_skill_search_dirs", return_value=[skills_dir])
            )
            stack.enter_context(
                patch.object(routes, "_get_plugin_manager_for_visibility", return_value=manager)
            )
            if native_view is not None:
                stack.enter_context(
                    patch(
                        "tools.skills_tool.skill_view",
                        return_value=json.dumps(native_view),
                    )
                )
            handled = routes.handle_get(handler, parsed)
        assert handled is True, "route did not handle /api/skills/content"
        return captured

    def test_plugin_skill_content_resolves_through_plugin_manager(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        skill_md = _write_plugin_skill(
            tmp_path / "plugins", qualified_name="superpowers:brainstorming"
        )
        manager = _FakePluginManager(find_paths={"superpowers:brainstorming": skill_md})

        captured = self._resolve(
            "superpowers:brainstorming",
            manager=manager,
            skills_dir=skills_dir,
            native_view={
                "success": True,
                "name": "superpowers:brainstorming",
                "content": "Brainstorm body",
            },
        )

        assert captured["status"] == 200, captured["payload"]
        assert "Brainstorm" in captured["payload"]["content"] or captured["payload"]["content"]

    def test_plugin_skill_content_is_delegated_to_the_native_viewer(self, tmp_path):
        """The route must serve plugin content through the Agent's native
        skill_view, not the directory thin-reader.

        The native viewer is the only source of the qualified name, the
        expanded ``${HERMES_SKILL_DIR}``, nested linked files and the bundle
        banner (#7770). A directory-style payload would drop all of them.
        """
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        skill_md = _write_plugin_skill(
            tmp_path / "plugins", qualified_name="superpowers:brainstorming"
        )
        manager = _FakePluginManager(find_paths={"superpowers:brainstorming": skill_md})

        captured = self._resolve(
            "superpowers:brainstorming",
            manager=manager,
            skills_dir=skills_dir,
            native_view={
                "success": True,
                "name": "superpowers:brainstorming",
                "content": "body with ${HERMES_SKILL_DIR} expanded",
                "linked_files": {
                    "references": ["references/top.md", "references/nested/guide.md"],
                    "scripts": ["scripts/run.sh"],
                },
                "bundle": "watchplugin bundle banner",
            },
        )

        payload = captured["payload"]
        assert payload["name"] == "superpowers:brainstorming", payload
        assert "${HERMES_SKILL_DIR}" in payload["content"], payload
        # Nested linked files survive — the directory reader only found top.md.
        assert "references/nested/guide.md" in payload["linked_files"]["references"], payload
        assert payload.get("bundle") == "watchplugin bundle banner", payload

    def test_plugin_linked_file_resolves_through_plugin_manager(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        skill_md = _write_plugin_skill(
            tmp_path / "plugins", qualified_name="superpowers:brainstorming"
        )
        (skill_md.parent / "references").mkdir()
        (skill_md.parent / "references" / "guide.md").write_text(
            "linked guide body", encoding="utf-8"
        )
        manager = _FakePluginManager(find_paths={"superpowers:brainstorming": skill_md})

        captured = self._resolve(
            "superpowers:brainstorming",
            manager=manager,
            skills_dir=skills_dir,
            file="references/guide.md",
        )

        assert captured["status"] == 200, captured["payload"]
        assert "linked guide body" in captured["payload"]["content"]

    def test_find_failure_returns_not_found_payload_not_500(self, tmp_path):
        """The bare-name path answers a miss with the not-found payload, not a
        500, so a plugin subsystem that raises stays a clean 'no such skill'."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        manager = _FakePluginManager(raise_on_find=True)

        captured = self._resolve("superpowers:missing", manager=manager, skills_dir=skills_dir)

        assert captured["payload"]["success"] is False
        assert "superpowers:missing" in captured["payload"]["error"]

    def test_plugin_linked_file_failure_returns_404_not_500(self, tmp_path):
        """A linked-file miss is a 404 (the pre-existing contract for the file
        sub-path), and must stay one even when the plugin manager raises."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        manager = _FakePluginManager(raise_on_find=True)

        captured = self._resolve(
            "superpowers:missing", manager=manager, skills_dir=skills_dir, file="references/x.md"
        )

        assert captured["status"] == 404, captured["payload"]

class TestSkillToggleAcceptsPluginSkills:
    """The Skills panel renders one toggle per /api/skills row (#7770).

    Adding plugin rows therefore hands the frontend a control that 404s unless
    _handle_skill_toggle can resolve a plugin-qualified name, so the toggle path
    is pinned here end-to-end rather than only being listed.
    """

    def _toggle(self, name, enabled, *, manager, skills_dir, config_path):
        import api.routes as routes

        captured = {}

        def fake_j(handler, payload, status=200, extra_headers=None):
            captured["payload"] = payload
            captured["status"] = status
            return True

        handler = MagicMock()
        with ExitStack() as stack:
            stack.enter_context(patch.object(routes, "j", side_effect=fake_j))
            stack.enter_context(patch.object(routes, "bad", side_effect=fake_j))
            stack.enter_context(patch.object(routes, "_active_skills_dir", return_value=skills_dir))
            stack.enter_context(
                patch.object(routes, "_active_skill_search_dirs", return_value=[skills_dir])
            )
            stack.enter_context(
                patch.object(routes, "_get_plugin_manager_for_visibility", return_value=manager)
            )
            stack.enter_context(
                patch.object(routes, "_active_profile_config_path", return_value=config_path)
            )
            stack.enter_context(patch.object(routes, "reload_config", lambda: None))
            routes._handle_skill_toggle(handler, {"name": name, "enabled": enabled})
        return captured

    def test_toggle_plugin_skill_persists_qualified_name(self, tmp_path):
        from api.config import _load_yaml_config_file

        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        config_path = tmp_path / "config.yaml"
        config_path.write_text("skills: {}\n", encoding="utf-8")
        skill_md = _write_plugin_skill(
            tmp_path / "plugins", qualified_name="superpowers:brainstorming"
        )
        manager = _FakePluginManager(find_paths={"superpowers:brainstorming": skill_md})

        captured = self._toggle(
            "superpowers:brainstorming",
            False,
            manager=manager,
            skills_dir=skills_dir,
            config_path=config_path,
        )

        assert captured["status"] == 200, captured["payload"]
        assert captured["payload"]["ok"] is True
        disabled = _load_yaml_config_file(config_path)["skills"]["disabled"]
        assert "superpowers:brainstorming" in disabled

    def test_toggle_reenabling_plugin_skill_removes_it(self, tmp_path):
        from api.config import _load_yaml_config_file

        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "skills:\n  disabled:\n    - superpowers:brainstorming\n", encoding="utf-8"
        )
        skill_md = _write_plugin_skill(
            tmp_path / "plugins", qualified_name="superpowers:brainstorming"
        )
        manager = _FakePluginManager(find_paths={"superpowers:brainstorming": skill_md})

        captured = self._toggle(
            "superpowers:brainstorming",
            True,
            manager=manager,
            skills_dir=skills_dir,
            config_path=config_path,
        )

        assert captured["status"] == 200, captured["payload"]
        disabled = _load_yaml_config_file(config_path)["skills"]["disabled"]
        assert "superpowers:brainstorming" not in disabled

    def test_toggle_unknown_plugin_skill_still_404s(self, tmp_path):
        """The fallback must not turn a genuinely missing skill into a success."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        config_path = tmp_path / "config.yaml"
        config_path.write_text("skills: {}\n", encoding="utf-8")
        manager = _FakePluginManager(find_paths={})

        captured = self._toggle(
            "superpowers:nonexistent",
            False,
            manager=manager,
            skills_dir=skills_dir,
            config_path=config_path,
        )

        assert captured["status"] == 404, captured["payload"]


class TestPluginSkillsAreReadOnly:
    """A plugin-provided skill is owned by the plugin package (#7770).

    The Skills panel's Edit control would write only ``SKILL.md`` into the
    profile's local ``skills/`` tree, leaving an incomplete copy that shadows
    the plugin's real skill and drops its linked files; Delete could only ever
    404 while the plugin kept serving the skill. The API refuses both writes,
    so the broken state is unreachable even by a direct request.
    """

    def _write(self, action, *, manager, skills_dir, body):
        import api.routes as routes

        captured = {}

        def fake_j(_handler, payload, status=200, extra_headers=None):
            captured["payload"] = payload
            captured["status"] = status
            return True

        with ExitStack() as stack:
            stack.enter_context(patch.object(routes, "j", side_effect=fake_j))
            stack.enter_context(patch.object(routes, "bad", side_effect=fake_j))
            stack.enter_context(patch.object(routes, "_active_skills_dir", return_value=skills_dir))
            stack.enter_context(
                patch.object(routes, "_active_skill_search_dirs", return_value=[skills_dir])
            )
            stack.enter_context(
                patch.object(routes, "_get_plugin_manager_for_visibility", return_value=manager)
            )
            if action == "save":
                routes._handle_skill_save(MagicMock(), body)
            else:
                routes._handle_skill_delete(MagicMock(), body)
        return captured

    def test_saving_a_plugin_skill_is_refused(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        skill_md = _write_plugin_skill(
            tmp_path / "plugins", qualified_name="superpowers:brainstorming"
        )
        manager = _FakePluginManager(find_paths={"superpowers:brainstorming": skill_md})

        captured = self._write(
            "save",
            manager=manager,
            skills_dir=skills_dir,
            body={"name": "superpowers:brainstorming", "content": "---\nname: x\n---\n"},
        )

        assert captured["status"] == 409, captured["payload"]
        # No partial local copy was written next to the plugin's real skill.
        assert not (skills_dir / "superpowers:brainstorming").exists()
        assert not (skills_dir / "superpowers").exists()

    def test_deleting_a_plugin_skill_is_refused(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        skill_md = _write_plugin_skill(
            tmp_path / "plugins", qualified_name="superpowers:brainstorming"
        )
        manager = _FakePluginManager(find_paths={"superpowers:brainstorming": skill_md})

        captured = self._write(
            "delete",
            manager=manager,
            skills_dir=skills_dir,
            body={"name": "superpowers:brainstorming"},
        )

        assert captured["status"] == 409, captured["payload"]

    def test_a_directory_skill_with_a_colon_is_still_editable(self, tmp_path):
        """Only registered plugin skills are read-only.

        A local skill whose name merely contains a colon is not owned by a
        plugin and must keep working exactly as before.
        """
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        _write_local_skill(skills_dir, name="weird:name")
        manager = _FakePluginManager(find_paths={})

        captured = self._write(
            "save",
            manager=manager,
            skills_dir=skills_dir,
            body={"name": "weird:name", "content": "---\nname: weird:name\n---\n\nbody\n"},
        )

        assert captured["status"] == 200, captured["payload"]
