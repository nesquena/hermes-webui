"""#7770: WebUI must surface plugin-registered skills in /api/skills AND in the
slash-command autocomplete AND resolve them through /api/skills/content.

The agent's plugin manager (``hermes_cli.plugins.get_plugin_manager()``) is
the authoritative source of plugin-skill metadata. Before the fix the WebUI
listed only directory-installed skills, so ``obra/superpowers`` (and any
other enabled plugin) had skills invisible to the WebUI even though
``skill_view("superpowers:brainstorming")`` worked inside the agent.

The fix is fail-soft: when the plugin manager is unavailable (isolated WebUI
without Hermes Agent on ``sys.path``) the endpoint must still serve the
directory-based listing instead of 500-ing.

These tests are the load-bearing contract for the fix. They pin:

* listing: /api/skills includes plugin-registered skills with their qualified
  ``name`` and a ``source: "plugin"`` marker, alongside directory skills;
* category filter: ``category=plugin`` returns only plugin skills, a
  non-plugin category excludes them, no category returns the combined set;
* content lookup: ``/api/skills/content?name=plugin:skill`` AND the
  ``&file=...`` sub-path both resolve through the plugin manager when the
  directory scan misses;
* fail-soft: a plugin manager that raises on import does not break the
  directory listing, the /api/skills/content bare-name path, or the
  /api/skills/content file= path.
"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

# Pin #7770 coverage to environments where the hermes-agent module is
# importable: ``api/routes.py:_skills_list_from_dir`` and
# ``_list_plugin_skills_for_response`` both ``from agent.skill_utils import
# ...`` inside the production code path, and the conftest's
# ``pytest_collection_modifyitems`` skip list does not enumerate these new
# test names. Without this marker the tests run in CI shards and explode
# with ``ModuleNotFoundError: No module named 'tools.skills_tool'`` /
# ``agent.skill_utils`` on the first /api/skills code path — the same
# silent-cold-start pattern the maintainer flagged in their review of
# PR #7781. (CI: 15/24 test jobs FAILED on this exact class.)
from tests.conftest import requires_agent_modules

pytestmark = requires_agent_modules


# ── Fake plugin manager ────────────────────────────────────────────────────
#
# The agent's plugin manager exposes list_plugin_skill_metadata() and
# find_plugin_skill() — the two entry points the WebUI fix relies on. The
# fake mirrors the public contract: list returns dicts with ``name`` and
# ``description``; find returns a Path to a SKILL.md or None.


class _FakePluginManager:
    def __init__(self, skills=None, find_paths=None, raise_on_list=False,
                 raise_on_find=False, raise_on_discover=False):
        # skills: list[dict] of plugin-skill metadata
        # find_paths: dict[qualified_name -> Path]
        self._skills = list(skills or [])
        self._find_paths = dict(find_paths or {})
        self._raise_on_list = raise_on_list
        self._raise_on_find = raise_on_find
        self._raise_on_discover = raise_on_discover
        # Discover-call tracking mirrors the real ``PluginManager`` contract:
        # ``discover_and_load(force=False)`` must be invoked once before the
        # helper reads the in-memory ``_plugin_skills`` registry (#7770
        # round-2 — cold-start /api/skills regression). Tests pin the call
        # shape (force flag + count) so a future refactor that drops the
        # discover call is caught by the test that asserts on it.
        #
        # ``discover_calls`` is appended BEFORE any raise so a discover that
        # throws still records the attempt — the production helper must
        # call discover_and_load(force=False) before reading the registry
        # even when the call itself errors. Pin the attempt, not the success.
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

    def list_plugin_skills(self, plugin_name):
        """Mirror the real PluginManager: sorted bare names registered by
        *plugin_name* (the bare-content fallback calls this as its namespace
        existence check)."""
        prefix = f"{plugin_name}:"
        return sorted(
            name[len(prefix):]
            for name in self._find_paths
            if name.startswith(prefix)
        )


class _ColdRegistryPluginManager(_FakePluginManager):
    """Plugin manager whose skill registry is EMPTY until discovery runs.

    Mirrors a fresh WebUI process that has not yet listed /api/skills (or
    opened Settings → Plugins): the agent contract only populates the
    in-memory ``_plugin_skills`` map during ``discover_and_load``. The cold
    find paths move into the readable map inside discovery, so any lookup
    that skips discovery sees no skills at all — the 404 regression behind
    re-gate findings 2/3 (routes.py content fallback and standalone toggle).
    """

    def __init__(self, find_paths=None):
        super().__init__(find_paths={})
        self._cold_find_paths = dict(find_paths or {})
        self._discovered = False

    def discover_and_load(self, force: bool = False) -> None:
        super().discover_and_load(force)
        if not self._discovered:
            self._find_paths.update(self._cold_find_paths)
            self._discovered = True

    @property
    def discovered(self) -> bool:
        return self._discovered


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


# ── /api/skills listing tests ──────────────────────────────────────────────


class TestPluginSkillsInListing:
    @pytest.fixture
    def skills_dir(self, tmp_path):
        d = tmp_path / "skills"
        d.mkdir()
        _write_local_skill(d, name="local-alpha", description="Local alpha")
        _write_local_skill(d, name="local-beta", description="Local beta")
        return d

    def _capture_listing(self, skills_dir, *, manager=None, category=None):
        import api.routes as routes
        captured = {}
        def fake_j(handler, payload, status=200, extra_headers=None):
            captured["payload"] = payload
            captured["status"] = status
            return True
        handler = MagicMock()
        # Always stub the active-skills-dir lookup: tests must not read the
        # user's real ~/.hermes/skills tree. The plugin manager is patched
        # only when a manager is provided (some tests want the real
        # import-failure path).
        with ExitStack() as stack:
            stack.enter_context(patch("api.routes.j", side_effect=fake_j))
            stack.enter_context(patch("api.routes._active_skills_dir", return_value=skills_dir))
            stack.enter_context(patch("api.routes._active_skill_search_dirs", return_value=[skills_dir]))
            if manager is not None:
                stack.enter_context(
                    patch("api.routes._get_plugin_manager_for_visibility", return_value=manager)
                )
            qs = f"?category={category}" if category else ""
            handled = routes.handle_get(handler, urlparse(f"/api/skills{qs}"))
        assert handled is True
        assert captured.get("status") == 200
        return captured.get("payload", {})

    def test_listing_merges_plugin_skills_with_directory_skills(self, skills_dir, tmp_path):
        base = tmp_path / "plugin-skills"
        bs_md = _write_plugin_skill(base, qualified_name="superpowers:brainstorming",
                                    description="Brainstorm like a genius")
        wp_md = _write_plugin_skill(base, qualified_name="superpowers:writing-plans",
                                    description="Write a plan")
        manager = _FakePluginManager(
            skills=[
                {"name": "superpowers:brainstorming", "description": "Brainstorm like a genius", "category": "plugin"},
                {"name": "superpowers:writing-plans", "description": "Write a plan", "category": "plugin"},
            ],
            find_paths={"superpowers:brainstorming": bs_md, "superpowers:writing-plans": wp_md},
        )
        payload = self._capture_listing(skills_dir, manager=manager)
        names = sorted(s["name"] for s in payload["skills"])
        assert names == [
            "local-alpha",
            "local-beta",
            "superpowers:brainstorming",
            "superpowers:writing-plans",
        ], payload

    def test_plugin_skill_entries_carry_namespace_and_source_marker(self, skills_dir, tmp_path):
        bs_md = _write_plugin_skill(tmp_path, qualified_name="superpowers:brainstorming")
        manager = _FakePluginManager(
            skills=[{"name": "superpowers:brainstorming", "description": "Brainstorm"}],
            find_paths={"superpowers:brainstorming": bs_md},
        )
        payload = self._capture_listing(skills_dir, manager=manager)
        plugin_entries = [s for s in payload["skills"] if s.get("name") == "superpowers:brainstorming"]
        assert len(plugin_entries) == 1
        entry = plugin_entries[0]
        # The qualified name must survive intact (so the slash autocomplete can
        # round-trip it losslessly) and carry the metadata the picker needs to
        # group/label plugin skills distinctly.
        assert entry["name"] == "superpowers:brainstorming"
        assert entry["plugin"] == "superpowers"
        assert entry["source"] == "plugin"
        assert entry["category"] == "plugin"

    def test_category_plugin_filter_returns_only_plugin_skills(self, skills_dir, tmp_path):
        bs_md = _write_plugin_skill(tmp_path, qualified_name="obra:test")
        manager = _FakePluginManager(
            skills=[{"name": "obra:test", "description": "Test plugin skill"}],
            find_paths={"obra:test": bs_md},
        )
        payload = self._capture_listing(skills_dir, manager=manager, category="plugin")
        names = [s["name"] for s in payload["skills"]]
        assert names == ["obra:test"], payload

    def test_category_user_filter_excludes_plugin_skills(self, skills_dir, tmp_path):
        bs_md = _write_plugin_skill(tmp_path, qualified_name="obra:test")
        manager = _FakePluginManager(
            skills=[{"name": "obra:test", "description": "Test plugin skill"}],
            find_paths={"obra:test": bs_md},
        )
        payload = self._capture_listing(skills_dir, manager=manager, category="user")
        names = [s["name"] for s in payload["skills"]]
        # Plugin-skill entries always carry category=="plugin", so a non-plugin
        # category filter MUST exclude them (#7770). Local flat-tree skills
        # have category=None and are likewise excluded by the "user" filter —
        # the relevant invariant is that the plugin skill never leaks through
        # a non-plugin category request.
        assert "obra:test" not in names, names
        assert all(
            s.get("category") != "plugin" for s in payload["skills"]
        ), payload

    def test_listing_without_plugin_manager_still_serves_directory_skills(self, skills_dir):
        # No manager passed: _get_plugin_manager_for_visibility is NOT patched,
        # so any plugin-manager import failure must NOT break the listing.
        # Isolated WebUI deployments are exactly this case.
        import api.routes as routes
        captured = {}
        def fake_j(handler, payload, status=200, extra_headers=None):
            captured["payload"] = payload
            captured["status"] = status
            return True
        handler = MagicMock()
        # Force the plugin manager call to fail the same way an isolated WebUI
        # (no hermes_cli on sys.path) does.
        with patch("api.routes.j", side_effect=fake_j), \
             patch("api.routes._active_skills_dir", return_value=skills_dir), \
             patch("api.routes._active_skill_search_dirs", return_value=[skills_dir]), \
             patch("api.routes._get_plugin_manager_for_visibility",
                   side_effect=ImportError("hermes_cli not on sys.path")):
            handled = routes.handle_get(handler, urlparse("/api/skills"))
        assert handled is True
        assert captured["status"] == 200
        names = sorted(s["name"] for s in captured["payload"]["skills"])
        assert names == ["local-alpha", "local-beta"], captured

    def test_plugin_manager_list_failure_does_not_drop_directory_skills(self, skills_dir):
        # list_plugin_skill_metadata raises — the directory listing must still
        # ship and no 500 / partial payload.
        manager = _FakePluginManager(raise_on_list=True)
        import api.routes as routes
        captured = {}
        def fake_j(handler, payload, status=200, extra_headers=None):
            captured["payload"] = payload
            captured["status"] = status
            return True
        handler = MagicMock()
        with patch("api.routes.j", side_effect=fake_j), \
             patch("api.routes._active_skills_dir", return_value=skills_dir), \
             patch("api.routes._active_skill_search_dirs", return_value=[skills_dir]), \
             patch("api.routes._get_plugin_manager_for_visibility", return_value=manager):
            handled = routes.handle_get(handler, urlparse("/api/skills"))
        assert handled is True
        assert captured["status"] == 200
        names = sorted(s["name"] for s in captured["payload"]["skills"])
        assert names == ["local-alpha", "local-beta"], captured


# ── /api/skills/content tests ──────────────────────────────────────────────


class TestPluginSkillContentLookup:
    @pytest.fixture
    def skills_dir(self, tmp_path):
        d = tmp_path / "skills"
        d.mkdir()
        return d

    def _capture_content(self, *, manager, path, skills_dir):
        import api.routes as routes
        captured = {}
        def fake_j(handler, payload, status=200, extra_headers=None):
            captured["payload"] = payload
            captured["status"] = status
            return True
        def fake_bad(handler, msg, status):
            captured["payload"] = {"error": msg}
            captured["status"] = status
            return True
        handler = MagicMock()
        with patch("api.routes.j", side_effect=fake_j), \
             patch("api.routes.bad", side_effect=fake_bad), \
             patch("api.routes._get_plugin_manager_for_visibility", return_value=manager), \
             patch("api.routes._active_skills_dir", return_value=skills_dir), \
             patch("api.routes._active_skill_search_dirs", return_value=[skills_dir]):
            handled = routes.handle_get(handler, urlparse(path))
        return handled, captured

    def test_plugin_skill_file_lookup_resolves_through_plugin_manager(self, skills_dir, tmp_path):
        skill_md = _write_plugin_skill(tmp_path, qualified_name="superpowers:brainstorming")
        # Add a linked file the UI might fetch.
        (skill_md.parent / "extra.md").write_text("linked file content", encoding="utf-8")
        manager = _FakePluginManager(find_paths={"superpowers:brainstorming": skill_md})
        handled, captured = self._capture_content(
            manager=manager,
            path="/api/skills/content?name=superpowers:brainstorming&file=extra.md",
            skills_dir=skills_dir,
        )
        assert handled is True
        assert captured.get("status") == 200, captured
        assert captured["payload"]["content"] == "linked file content"
        assert captured["payload"]["path"] == "extra.md"

    def test_plugin_skill_file_lookup_discovers_before_find_on_cold_registry(self, skills_dir, tmp_path):
        """PR #7781 re-gate finding 2 (CORE): the plugin fallback used to call
        find_plugin_skill() before discovery, so a FRESH first
        /api/skills/content?name=reviewdemo:skill&file=extra.md returned 404 —
        the same request after an /api/skills listing (which discovers)
        returned the correct file. The fallback must run discovery
        (best-effort, force=False) inside the request-home scope before the
        lookup, and must still 404 for unknown names."""
        skill_md = _write_plugin_skill(tmp_path, qualified_name="reviewdemo:skill")
        (skill_md.parent / "extra.md").write_text("linked file content", encoding="utf-8")
        manager = _ColdRegistryPluginManager(find_paths={"reviewdemo:skill": skill_md})
        handled, captured = self._capture_content(
            manager=manager,
            path="/api/skills/content?name=reviewdemo:skill&file=extra.md",
            skills_dir=skills_dir,
        )
        assert handled is True
        assert captured.get("status") == 200, captured
        assert captured["payload"]["content"] == "linked file content"
        assert captured["payload"]["path"] == "extra.md"
        # Discovery ran exactly once, before the lookup, with force=False.
        assert manager.discover_calls == [False], manager.discover_calls
        assert manager.discovered is True

    def test_plugin_skill_file_lookup_404s_when_plugin_skill_unknown(self, skills_dir):
        manager = _FakePluginManager(find_paths={})
        handled, captured = self._capture_content(
            manager=manager,
            path="/api/skills/content?name=superpowers:nope&file=extra.md",
            skills_dir=skills_dir,
        )
        assert handled is True
        assert captured.get("status") == 404, captured

    def test_plugin_skill_file_lookup_survives_plugin_manager_failure(self, skills_dir, tmp_path):
        # A raising plugin manager must not 500 the file= path; the directory
        # branch already returned None and the plugin fallback must fall back
        # to a 404, not raise.
        manager = _FakePluginManager(raise_on_find=True)
        handled, captured = self._capture_content(
            manager=manager,
            path="/api/skills/content?name=superpowers:brainstorming&file=extra.md",
            skills_dir=skills_dir,
        )
        assert handled is True
        assert captured.get("status") == 404, captured

    def test_plugin_skill_file_lookup_404s_when_plugin_manager_unavailable(self, skills_dir):
        # Isolated WebUI: plugin manager import fails. The directory scan
        # misses the qualified name and the plugin fallback is unavailable —
        # 404, not 500.
        import api.routes as routes
        captured = {}
        def fake_bad(handler, msg, status):
            captured["payload"] = {"error": msg}
            captured["status"] = status
            return True
        handler = MagicMock()
        with patch("api.routes.bad", side_effect=fake_bad), \
             patch("api.routes._get_plugin_manager_for_visibility",
                   side_effect=ImportError("hermes_cli not on sys.path")), \
             patch("api.routes._active_skills_dir", return_value=skills_dir), \
             patch("api.routes._active_skill_search_dirs", return_value=[skills_dir]):
            handled = routes.handle_get(
                handler, urlparse("/api/skills/content?name=superpowers:x&file=extra.md")
            )
        assert handled is True
        assert captured.get("status") == 404, captured


# ── Helper unit tests (no IO) ─────────────────────────────────────────────


class TestListPluginSkillsForResponse:
    def _call(self, manager, disabled=None):
        import api.routes as routes
        # Isolate the disabled-set lookup from the machine's real profile
        # config: the entry shape under test must not depend on whatever
        # ~/.hermes/config.yaml happens to disable.
        with patch(
            "api.routes._get_disabled_skill_names_for_profile",
            return_value=set(disabled or ()),
        ):
            return routes._list_plugin_skills_for_response(manager=manager)

    def test_returns_qualified_entries_with_namespace_and_source_marker(self):
        manager = _FakePluginManager(skills=[
            {"name": "superpowers:brainstorming", "description": "Brainstorm"},
            {"name": "superpowers:writing-plans", "description": "Plan"},
        ])
        entries = self._call(manager)
        assert entries == [
            {
                "name": "superpowers:brainstorming",
                "description": "Brainstorm",
                "category": "plugin",
                "plugin": "superpowers",
                "source": "plugin",
                "disabled": False,
            },
            {
                "name": "superpowers:writing-plans",
                "description": "Plan",
                "category": "plugin",
                "plugin": "superpowers",
                "source": "plugin",
                "disabled": False,
            },
        ]

    def test_filters_entries_without_a_namespace_separator(self):
        # An un-qualified name would be ambiguous against a local skill and
        # must not leak into the picker.
        manager = _FakePluginManager(skills=[
            {"name": "nocolon", "description": "oops"},
            {"name": "ok:yes", "description": "fine"},
        ])
        names = [e["name"] for e in self._call(manager)]
        assert names == ["ok:yes"], names

    def test_returns_empty_list_when_manager_raises_on_list(self):
        manager = _FakePluginManager(raise_on_list=True)
        assert self._call(manager) == []

    def test_returns_empty_list_when_manager_is_unavailable(self):
        import api.routes as routes
        with patch("api.routes._get_plugin_manager_for_visibility",
                   side_effect=ImportError("hermes_cli not on sys.path")):
            assert routes._list_plugin_skills_for_response() == []


# ── Regression: cold-start discover (#7770 round-2 maintainer finding) ───────
#
# The agent's plugin manager populates ``_plugin_skills`` only during a
# discovery pass — a fresh WebUI process that has not yet driven an agent
# turn (or opened the Settings → Plugins tab) sits on an empty registry.
# ``_plugin_visibility_payload`` already calls
# ``manager.discover_and_load(force=False)`` before reading it; this class
# pins the same contract on the /api/skills code path so a future refactor
# cannot silently regress the cold-start behavior.


class TestPluginSkillDiscoveryTriggeredBeforeRegistryRead:
    def test_helper_invokes_discover_and_load_with_force_false(self):
        # The cold-start regression was that ``_list_plugin_skills_for_response``
        # obtained a manager and IMMEDIATELY read the in-memory registry, so
        # the first /api/skills on a fresh process returned ``[]`` and the
        # slash-command autocomplete silently dropped plugin skills. The
        # fix mirrors ``_plugin_visibility_payload`` (line 12885): call
        # ``discover_and_load(force=False)`` before reading the registry.
        manager = _FakePluginManager(skills=[
            {"name": "superpowers:brainstorming", "description": "Brainstorm"},
        ])
        import api.routes as routes
        entries = routes._list_plugin_skills_for_response(manager=manager)
        # The discover call MUST happen exactly once with force=False —
        # never force=True (that would evict the user-configured home
        # every call) and never zero times (the cold-start bug).
        assert manager.discover_calls == [False], manager.discover_calls
        # And the helper still returns the registry contents.
        assert [e["name"] for e in entries] == ["superpowers:brainstorming"]

    def test_helper_still_returns_skills_when_discover_raises(self):
        # Best-effort: a discovery failure (bad plugin manifest, locked
        # plugin home, partial scan) must NOT 500 /api/skills. The helper
        # should swallow the exception and continue with whatever the
        # registry currently holds — same fail-soft posture as the rest
        # of #7770.
        manager = _FakePluginManager(
            skills=[{"name": "superpowers:brainstorming", "description": "B"}],
            raise_on_discover=True,
        )
        import api.routes as routes
        entries = routes._list_plugin_skills_for_response(manager=manager)
        assert manager.discover_calls == [False], manager.discover_calls
        # The registry contents still surface even though discovery raised.
        assert [e["name"] for e in entries] == ["superpowers:brainstorming"]

    def test_listing_endpoint_invokes_discover_before_returning_payload(self):
        # Pin the contract at the public /api/skills boundary: a request
        # through the HTTP handler must trigger discover_and_load exactly
        # once with force=False, before the response is built. This is
        # the regression the maintainer explicitly called out: a cold-start
        # /api/skills call should never see an empty plugin-skill registry
        # when at least one plugin is installed and enabled.
        skills_dir = tmp_path_factory_safe_mkdir("skills-listing")
        _write_local_skill(skills_dir, name="local-alpha")
        bs_md = _write_plugin_skill(skills_dir, qualified_name="superpowers:brainstorming")
        manager = _FakePluginManager(
            skills=[{"name": "superpowers:brainstorming", "description": "B"}],
            find_paths={"superpowers:brainstorming": bs_md},
        )
        import api.routes as routes
        captured = {}
        def fake_j(handler, payload, status=200, extra_headers=None):
            captured["payload"] = payload
            captured["status"] = status
            return True
        handler = MagicMock()
        with ExitStack() as stack:
            stack.enter_context(patch("api.routes.j", side_effect=fake_j))
            stack.enter_context(patch("api.routes._active_skills_dir", return_value=skills_dir))
            stack.enter_context(patch("api.routes._active_skill_search_dirs", return_value=[skills_dir]))
            stack.enter_context(patch("api.routes._get_plugin_manager_for_visibility", return_value=manager))
            handled = routes.handle_get(handler, urlparse("/api/skills"))
        assert handled is True
        assert captured.get("status") == 200
        # The cold-start discover is what populates the plugin-skill
        # registry the helper reads. force=False is the correct flag —
        # force=True would evict the home on every /api/skills call.
        assert manager.discover_calls == [False], manager.discover_calls


def tmp_path_factory_safe_mkdir(name: str):
    """Tiny helper so the discover-triggered test above stays self-contained.

    Returns a fresh ``Path`` under /tmp without depending on a pytest
    fixture, keeping the regression test independent of the listing-class
    fixtures (the regression is about the helper + manager contract, not
    about directory fixtures).
    """
    import tempfile
    return Path(tempfile.mkdtemp(prefix=f"pr7781-{name}-"))


# ── Regression: plugin skills surface when local skills dir is absent ─────
#
# The first round of #7770 introduced the plugin-merge, but ``_skills_list_from_dir``
# short-circuited to ``{"skills": []}`` whenever the local ``skills/`` directory
# had never been created. The maintainer's round-2 review flagged this as
# suppressing the very plugin skills the fix was meant to surface. The fix:
# keep creating the directory for backward compatibility but fall through to
# the directory-scan + plugin-merge path so plugin-registered skills still
# show up on a fresh host. These tests pin both directions (skills present,
# skills absent) so a future early-return refactor is caught.


class TestPluginSkillsSurfaceWhenLocalDirMissing:
    def test_listing_returns_plugin_skills_when_local_dir_does_not_exist(self, tmp_path):
        # Local skills dir has NEVER been created — exactly the cold-host
        # shape the maintainer called out. The handler must still surface
        # plugin-registered skills (and create the local dir for
        # backward-compat writes).
        import api.routes as routes

        nonexistent_dir = tmp_path / "skills-never-created"
        assert not nonexistent_dir.exists()  # precondition
        bs_md = _write_plugin_skill(tmp_path, qualified_name="superpowers:brainstorming")
        manager = _FakePluginManager(
            skills=[{"name": "superpowers:brainstorming", "description": "B"}],
            find_paths={"superpowers:brainstorming": bs_md},
        )
        captured = {}
        def fake_j(handler, payload, status=200, extra_headers=None):
            captured["payload"] = payload
            captured["status"] = status
            return True
        handler = MagicMock()
        with ExitStack() as stack:
            stack.enter_context(patch("api.routes.j", side_effect=fake_j))
            stack.enter_context(patch("api.routes._active_skills_dir", return_value=nonexistent_dir))
            stack.enter_context(
                patch("api.routes._active_skill_search_dirs", return_value=[nonexistent_dir])
            )
            stack.enter_context(
                patch("api.routes._get_plugin_manager_for_visibility", return_value=manager)
            )
            handled = routes.handle_get(handler, urlparse("/api/skills"))
        assert handled is True
        assert captured.get("status") == 200, captured
        names = [s["name"] for s in captured["payload"]["skills"]]
        # The plugin skill must surface even though the local dir is absent
        # — this is the cold-host regression the maintainer called out.
        assert "superpowers:brainstorming" in names, names
        # Backward compat: the missing local dir is auto-created so a
        # user with zero local skills has a writable home for new ones.
        assert nonexistent_dir.exists(), (
            "local skills dir should be auto-created even when plugin "
            "skills are the only source, for backward-compat writes"
        )
        # And the discover_and_load(force=False) call still ran — the
        # cold-start guarantee applies regardless of whether the local
        # dir exists.
        assert manager.discover_calls == [False], manager.discover_calls

    def test_listing_returns_empty_skills_when_no_local_dir_and_no_plugin_manager(
        self, tmp_path,
    ):
        # Control: when both the local dir is absent AND the plugin
        # manager is unavailable (e.g. an isolated WebUI without
        # hermes-agent on sys.path), the listing must NOT 500. It returns
        # an empty ``skills`` list AND still creates the local dir for
        # backward compat. This pins the fail-soft contract the rest of
        # #7770 already enforces — the round-2 fix did not regress it.
        nonexistent_dir = tmp_path / "skills-never-created-2"
        assert not nonexistent_dir.exists()
        import api.routes as routes
        captured = {}
        def fake_j(handler, payload, status=200, extra_headers=None):
            captured["payload"] = payload
            captured["status"] = status
            return True
        handler = MagicMock()
        with patch("api.routes.j", side_effect=fake_j), \
             patch("api.routes._active_skills_dir", return_value=nonexistent_dir), \
             patch("api.routes._active_skill_search_dirs", return_value=[nonexistent_dir]), \
             patch("api.routes._get_plugin_manager_for_visibility",
                   side_effect=ImportError("hermes_cli not on sys.path")):
            handled = routes.handle_get(handler, urlparse("/api/skills"))
        assert handled is True
        assert captured.get("status") == 200, captured
        assert captured["payload"]["skills"] == []
        # Backward-compat: dir is created even when no plugin manager is
        # reachable, so a fresh host can still drop skills in via the UI.
        assert nonexistent_dir.exists()


# ── Round-3 review findings (PR #7781 re-gate) ─────────────────────────────


class _HomeRecordingPluginManager(_FakePluginManager):
    """Fake manager that records the agent-side Hermes home visible at each
    call, so a test can prove the request's active home was bound around
    the plugin-manager calls (finding 1) and restored afterwards."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seen_homes: list = []

    def _record(self):
        from hermes_constants import get_hermes_home

        self.seen_homes.append(Path(get_hermes_home()))

    def discover_and_load(self, force: bool = False) -> None:
        self._record()
        super().discover_and_load(force)

    def list_plugin_skill_metadata(self):
        self._record()
        return super().list_plugin_skill_metadata()

    def find_plugin_skill(self, qualified_name):
        self._record()
        return super().find_plugin_skill(qualified_name)

    def list_plugin_skills(self, plugin_name):
        self._record()
        return super().list_plugin_skills(plugin_name)


class TestPluginManagerProfileHomeBinding:
    """Finding 1 (CORE): ``get_plugin_manager()`` keys its manager by the
    agent-side Hermes home, not by the WebUI's thread-local active profile.
    Every plugin-manager call in the request path must run inside the
    request's active-home context, or one WebUI process serving several
    profiles leaks profile A's plugin skills into profile B's responses.
    """

    def test_listing_binds_request_active_home_around_manager_calls(self, tmp_path):
        from hermes_constants import get_hermes_home, set_hermes_home_override

        import api.routes as routes

        profile_a_home = (tmp_path / "profile-a").resolve()
        profile_b_home = (tmp_path / "profile-b").resolve()
        profile_a_home.mkdir()
        profile_b_home.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        _write_local_skill(skills_dir, name="local-alpha")
        manager = _HomeRecordingPluginManager(
            skills=[{"name": "demo:skill", "description": "profile-B plugin skill"}],
        )
        captured = {}

        def fake_j(handler, payload, status=200, extra_headers=None):
            captured["payload"] = payload
            captured["status"] = status
            return True

        handler = MagicMock()
        # The ambient agent-side home is profile A (e.g. a previous request
        # or agent turn on another profile); the request's active WebUI
        # profile is B. Capture the ambient value first so the post-request
        # assertion proves the override was RESET (not left dangling).
        ambient_before = Path(get_hermes_home())
        token = set_hermes_home_override(profile_a_home)
        try:
            with patch("api.routes.j", side_effect=fake_j), \
                 patch("api.routes._active_skills_dir", return_value=skills_dir), \
                 patch("api.routes._active_skill_search_dirs", return_value=[skills_dir]), \
                 patch("api.routes._get_plugin_manager_for_visibility", return_value=manager), \
                 patch("api.profiles.get_active_hermes_home", return_value=str(profile_b_home)):
                handled = routes.handle_get(handler, urlparse("/api/skills"))
        finally:
            from hermes_constants import reset_hermes_home_override

            reset_hermes_home_override(token)
        assert handled is True
        assert captured.get("status") == 200
        names = sorted(s["name"] for s in captured["payload"]["skills"])
        assert names == ["demo:skill", "local-alpha"], names
        # Both manager calls (discover + list) saw profile B's home, not
        # the ambient profile A home.
        assert manager.seen_homes, manager.seen_homes
        assert all(h == profile_b_home for h in manager.seen_homes), manager.seen_homes
        # The override is reset after the request so the ambient home (and
        # any later request on another profile) is unaffected.
        assert Path(get_hermes_home()) == ambient_before

    def test_file_content_lookup_binds_request_active_home(self, tmp_path):
        from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override

        import api.routes as routes

        profile_a_home = (tmp_path / "profile-a").resolve()
        profile_b_home = (tmp_path / "profile-b").resolve()
        profile_a_home.mkdir()
        profile_b_home.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        skill_md = _write_plugin_skill(tmp_path, qualified_name="demo:skill")
        (skill_md.parent / "extra.md").write_text("linked", encoding="utf-8")
        manager = _HomeRecordingPluginManager(find_paths={"demo:skill": skill_md})
        captured = {}

        def fake_j(handler, payload, status=200, extra_headers=None):
            captured["payload"] = payload
            captured["status"] = status
            return True

        def fake_bad(handler, msg, status):
            captured["payload"] = {"error": msg}
            captured["status"] = status
            return True

        handler = MagicMock()
        ambient_before = Path(get_hermes_home())
        token = set_hermes_home_override(profile_a_home)
        try:
            with patch("api.routes.j", side_effect=fake_j), \
                 patch("api.routes.bad", side_effect=fake_bad), \
                 patch("api.routes._get_plugin_manager_for_visibility", return_value=manager), \
                 patch("api.routes._active_skills_dir", return_value=skills_dir), \
                 patch("api.routes._active_skill_search_dirs", return_value=[skills_dir]), \
                 patch("api.profiles.get_active_hermes_home", return_value=str(profile_b_home)):
                handled = routes.handle_get(
                    handler,
                    urlparse("/api/skills/content?name=demo:skill&file=extra.md"),
                )
        finally:
            reset_hermes_home_override(token)
        assert handled is True
        assert captured.get("status") == 200, captured
        assert captured["payload"]["content"] == "linked"
        # Both manager calls (the re-gate's pre-lookup discover AND the
        # find) must see profile B's home, not the ambient profile A home.
        assert manager.seen_homes == [profile_b_home, profile_b_home], manager.seen_homes
        assert Path(get_hermes_home()) == ambient_before

    def test_bare_content_lookup_binds_request_active_home(self, tmp_path):
        from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override

        import api.routes as routes

        profile_a_home = (tmp_path / "profile-a").resolve()
        profile_b_home = (tmp_path / "profile-b").resolve()
        profile_a_home.mkdir()
        profile_b_home.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        skill_md = _write_plugin_skill(tmp_path, qualified_name="demo:skill")
        manager = _HomeRecordingPluginManager(find_paths={"demo:skill": skill_md})
        captured = {}

        def fake_j(handler, payload, status=200, extra_headers=None):
            captured["payload"] = payload
            captured["status"] = status
            return True

        handler = MagicMock()
        ambient_before = Path(get_hermes_home())
        token = set_hermes_home_override(profile_a_home)
        try:
            with patch("api.routes.j", side_effect=fake_j), \
                 patch("api.routes._active_skills_dir", return_value=skills_dir), \
                 patch("api.routes._active_skill_search_dirs", return_value=[skills_dir]), \
                 patch("api.routes._find_skill_in_dirs", return_value=(None, None)), \
                 patch("hermes_cli.plugins.discover_plugins"), \
                 patch("hermes_cli.plugins.get_plugin_manager", return_value=manager), \
                 patch("tools.skills_tool.skill_view",
                       return_value='{"success": true, "content": "demo skill body"}'), \
                 patch("api.profiles.get_active_hermes_home", return_value=str(profile_b_home)):
                handled = routes.handle_get(
                    handler, urlparse("/api/skills/content?name=demo:skill")
                )
        finally:
            reset_hermes_home_override(token)
        assert handled is True
        assert captured.get("status") == 200, captured
        assert captured["payload"].get("content") == "demo skill body", captured
        # The bare-content fallback resolves the plugin skill through the
        # manager (find) and the namespace check (list_plugin_skills) —
        # both must see profile B's home, and the agent's own skill_view
        # call runs inside the same binding.
        assert manager.seen_homes, manager.seen_homes
        assert all(h == profile_b_home for h in manager.seen_homes), manager.seen_homes
        assert Path(get_hermes_home()) == ambient_before


class TestPluginSkillDisabledAndPlatformFilters:
    """Finding 4 (SILENT): disabled and platform-incompatible plugin skills
    came back as enabled. The agent's own listing (tools/skills_tool.py
    ``skills_list``) omits platform-incompatible rows and marks disabled
    ones from the active profile's set; /api/skills must mirror that."""

    @pytest.fixture
    def skills_dir(self, tmp_path):
        d = tmp_path / "skills"
        d.mkdir()
        return d

    def _capture_listing(self, skills_dir, *, manager, category=None, disabled=()):
        import api.routes as routes
        captured = {}

        def fake_j(handler, payload, status=200, extra_headers=None):
            captured["payload"] = payload
            captured["status"] = status
            return True

        handler = MagicMock()
        with patch("api.routes.j", side_effect=fake_j), \
             patch("api.routes._active_skills_dir", return_value=skills_dir), \
             patch("api.routes._active_skill_search_dirs", return_value=[skills_dir]), \
             patch("api.routes._get_plugin_manager_for_visibility", return_value=manager), \
             patch("api.routes._get_disabled_skill_names_for_profile",
                   return_value=set(disabled)):
            qs = f"?category={category}" if category else ""
            handled = routes.handle_get(handler, urlparse(f"/api/skills{qs}"))
        assert handled is True
        assert captured.get("status") == 200
        return captured.get("payload", {})

    def test_platform_incompatible_rows_are_omitted(self, skills_dir):
        # A skill restricted to a foreign OS must not be listed at all —
        # the agent never offers it either.
        import sys

        foreign_os = "linux" if sys.platform != "linux" else "win32"
        manager = _FakePluginManager(skills=[
            {"name": "demo:foreign", "description": "other OS",
             "frontmatter": {"platforms": [foreign_os]}},
            {"name": "demo:any", "description": "all platforms"},
        ])
        payload = self._capture_listing(skills_dir, manager=manager)
        names = [s["name"] for s in payload["skills"]]
        assert "demo:foreign" not in names, names
        assert "demo:any" in names, names

    def test_disabled_rows_are_marked_from_active_profile_set(self, skills_dir):
        manager = _FakePluginManager(skills=[
            {"name": "demo:on", "description": "enabled"},
            {"name": "demo:off", "description": "disabled in active profile"},
        ])
        payload = self._capture_listing(
            skills_dir, manager=manager, disabled={"demo:off"}
        )
        by_name = {s["name"]: s for s in payload["skills"]}
        assert by_name["demo:on"]["disabled"] is False, by_name
        assert by_name["demo:off"]["disabled"] is True, by_name

    def test_helper_marks_disabled_and_skips_incompatible(self):
        import sys

        foreign_os = "linux" if sys.platform != "linux" else "win32"
        manager = _FakePluginManager(skills=[
            {"name": "demo:on", "description": "enabled"},
            {"name": "demo:off", "description": "disabled"},
            {"name": "demo:foreign", "description": "other OS",
             "frontmatter": {"platforms": [foreign_os]}},
        ])
        entries = TestListPluginSkillsForResponse()._call(
            manager, disabled={"demo:off"}
        )
        by_name = {e["name"]: e for e in entries}
        assert set(by_name) == {"demo:on", "demo:off"}, by_name
        assert by_name["demo:on"]["disabled"] is False
        assert by_name["demo:off"]["disabled"] is True


class TestCategoryPluginBucketReservedForPluginManager:
    """Greptile finding (P2): plugin entries share the ``plugin`` category
    value a directory skill can acquire from a top-level ``plugin/``
    directory, so ``?category=plugin`` must discriminate on the source
    marker — otherwise the plugin-only bucket mixes directory skills in."""

    @pytest.fixture
    def skills_dir(self, tmp_path):
        d = tmp_path / "skills"
        d.mkdir()
        return d

    def _capture_listing(self, skills_dir, *, manager, category=None):
        import api.routes as routes
        captured = {}

        def fake_j(handler, payload, status=200, extra_headers=None):
            captured["payload"] = payload
            captured["status"] = status
            return True

        handler = MagicMock()
        with patch("api.routes.j", side_effect=fake_j), \
             patch("api.routes._active_skills_dir", return_value=skills_dir), \
             patch("api.routes._active_skill_search_dirs", return_value=[skills_dir]), \
             patch("api.routes._get_plugin_manager_for_visibility", return_value=manager):
            qs = f"?category={category}" if category else ""
            handled = routes.handle_get(handler, urlparse(f"/api/skills{qs}"))
        assert handled is True
        assert captured.get("status") == 200
        return captured.get("payload", {})

    def test_category_plugin_excludes_directory_skills_in_plugin_dir(self, skills_dir):
        # A directory skill at <skills>/plugin/<name>/SKILL.md> carries
        # category "plugin" from its path; it must not land in the
        # plugin-manager bucket.
        dir_skill = skills_dir / "plugin" / "dir-skill"
        dir_skill.mkdir(parents=True)
        (dir_skill / "SKILL.md").write_text(
            "---\nname: dir-skill\ndescription: directory skill in plugin/ category\n---\n\nbody\n",
            encoding="utf-8",
        )
        manager = _FakePluginManager(
            skills=[{"name": "demo:real", "description": "plugin-manager skill"}],
        )
        payload = self._capture_listing(skills_dir, manager=manager, category="plugin")
        names = [s["name"] for s in payload["skills"]]
        assert names == ["demo:real"], names
        # The unfiltered listing still shows both — only the bucket filter
        # discriminates.
        payload_all = self._capture_listing(skills_dir, manager=manager)
        assert sorted(s["name"] for s in payload_all["skills"]) == [
            "demo:real", "dir-skill"
        ], payload_all

    def test_non_plugin_category_still_excludes_plugin_entries(self, skills_dir):
        manager = _FakePluginManager(
            skills=[{"name": "demo:real", "description": "plugin-manager skill"}],
        )
        payload = self._capture_listing(skills_dir, manager=manager, category="user")
        assert payload["skills"] == [], payload
