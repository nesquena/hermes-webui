"""Real-plugin regression tests for #7770 (re-gate items 1-4).

The fake-manager suite in ``test_issue7770_plugin_skill_discovery.py`` pins the
WebUI's own helpers, but it cannot prove that the WebUI agrees with the
*installed Agent*: a fake manager happily reports skills the real registry
does not have, and a fake frontmatter field says nothing about what the real
producer emits.

This suite therefore drives the real Agent plugin registry — real
``plugins/<name>/plugin.yaml`` + ``__init__.py`` whose ``register()`` calls
``ctx.register_skill``, loaded by the installed ``PluginManager`` — through the
real WebUI route handlers, with two profiles, so that:

* plugin discovery follows the *request* profile, not the root profile;
* a plugin skill disabled through either config form is reported disabled and
  its content is refused;
* a platform-restricted plugin skill is not advertised;
* a plugin skill with nested linked files and a ``${HERMES_SKILL_DIR}``
  template is served through the Agent's native viewer, not a thin reader.

The underscore-id picker case lives in
``test_issue7770_plugin_skill_slash_picker.py`` (it needs the real
``static/commands.js`` in a node vm).
"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from tests.conftest import requires_agent_modules

pytestmark = requires_agent_modules


def _write_plugin(
    home: Path,
    plugin_name: str,
    skills: dict,
    *,
    enabled: bool = True,
) -> None:
    """Create a real user-directory plugin under ``<home>/plugins/<name>``.

    *skills* maps a bare skill name to its frontmatter/body ``(frontmatter, body)``
    pair; the SKILL.md is written to ``<plugin>/skills/<name>/SKILL.md`` and
    registered through ``ctx.register_skill`` exactly like a shipped plugin.
    The frontmatter is written with the ``---`` fences the Agent's parser
    expects, so ``platforms:`` and friends are actually honoured.
    """
    plugin_dir = home / "plugins" / plugin_name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.yaml").write_text(
        f"name: {plugin_name}\nversion: 0.1.0\n", encoding="utf-8"
    )
    skill_names = list(skills)
    (plugin_dir / "__init__.py").write_text(
        "from pathlib import Path\n"
        "def register(ctx):\n"
        f"    for name in {skill_names!r}:\n"
        "        ctx.register_skill(name, Path(__file__).parent / 'skills' / name / 'SKILL.md')\n",
        encoding="utf-8",
    )
    for name, (frontmatter, body) in skills.items():
        skill_dir = plugin_dir / "skills" / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_dir.joinpath("SKILL.md").write_text(
            "---\n" + yaml.dump(frontmatter) + "---\n\n" + body, encoding="utf-8"
        )
    if enabled:
        config_path = home / "config.yaml"
        config = {}
        if config_path.exists():
            config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        config.setdefault("plugins", {})["enabled"] = [plugin_name]
        config_path.write_text(yaml.dump(config), encoding="utf-8")


def _skill(frontmatter: dict, body: str = "body\n") -> dict:
    return (frontmatter, body)


class RealPluginEnv:
    """A temp Hermes home with a root profile and a named ``auditor`` profile."""

    def __init__(self, tmp_path: Path):
        self.base = tmp_path / "hermes"
        self.root_home = self.base
        self.auditor_home = self.base / "profiles" / "auditor"
        self.root_home.mkdir(parents=True, exist_ok=True)
        self.auditor_home.mkdir(parents=True, exist_ok=True)

    def write_config(self, home: Path, config: dict) -> None:
        (home / "config.yaml").write_text(yaml.dump(config), encoding="utf-8")


@pytest.fixture
def real_env(tmp_path, monkeypatch):
    """Two real profiles wired into the WebUI's profile resolution."""
    from api import profiles as profiles_api
    from hermes_cli import plugins as agent_plugins

    env = RealPluginEnv(tmp_path)

    # The WebUI resolves named profiles under its base home, and the Agent
    # resolves the active home from HERMES_HOME / the context-local override.
    monkeypatch.setattr(profiles_api, "_DEFAULT_HERMES_HOME", env.base)
    monkeypatch.setenv("HERMES_HOME", str(env.base))
    monkeypatch.setenv("HERMES_BASE_HOME", str(env.base))
    agent_plugins._reset_plugin_managers_for_tests()
    try:
        yield env
    finally:
        profiles_api.clear_request_profile()
        agent_plugins._reset_plugin_managers_for_tests()


def _listing(home: Path, profile: str | None = None, disabled: set | None = None):
    """Run the real /api/skills listing for *profile*.

    ``disabled`` replaces the profile's effective disabled set. It defaults to
    ``None`` — meaning "read the real profile config" — so a caller that
    patches the disabled set is not silently overridden by this helper.
    """
    from api import profiles as profiles_api
    from api import routes

    if profile:
        profiles_api.set_request_profile(profile)

    with ExitStack() as stack:
        stack.enter_context(patch.object(routes, "_active_skills_dir", return_value=home / "skills"))
        stack.enter_context(
            patch.object(routes, "_active_skill_search_dirs", return_value=[home / "skills"])
        )
        if disabled is not None:
            stack.enter_context(
                patch.object(
                    routes, "_get_disabled_skill_names_for_profile", return_value=set(disabled)
                )
            )
        result = routes._skills_list_from_dir(home / "skills")
    return {skill["name"]: skill for skill in result["skills"]}


def _content(home: Path, name: str, profile: str | None = None):
    """Run the real /api/skills/content route for *name*."""
    from urllib.parse import urlparse

    from api import profiles as profiles_api
    from api import routes

    if profile:
        profiles_api.set_request_profile(profile)

    captured = {}

    def fake_j(_handler, payload, status=200, extra_headers=None):
        captured["payload"] = payload
        captured["status"] = status
        return True

    parsed = urlparse(f"/api/skills/content?name={name}")
    with ExitStack() as stack:
        stack.enter_context(patch.object(routes, "j", side_effect=fake_j))
        stack.enter_context(patch.object(routes, "bad", side_effect=fake_j))
        stack.enter_context(patch.object(routes, "_active_skills_dir", return_value=home / "skills"))
        stack.enter_context(
            patch.object(routes, "_active_skill_search_dirs", return_value=[home / "skills"])
        )
        routes.handle_get(MagicMock(), parsed)
    return captured


def test_plugin_discovery_follows_the_request_profile(real_env):
    """The named profile sees its own plugin skill, not the root profile's.

    An unscoped read would report the root profile's registry to every
    profile — and let a named profile toggle a root-only skill into its own
    config (re-gate item 1).
    """
    _write_plugin(
        real_env.root_home,
        "rootplugin",
        {"helper": _skill({"name": "helper", "description": "root helper"})},
    )
    _write_plugin(
        real_env.auditor_home,
        "privateplugin",
        {"helper": _skill({"name": "helper", "description": "auditor helper"})},
    )

    root_rows = _listing(real_env.root_home)
    auditor_rows = _listing(real_env.auditor_home, profile="auditor")

    assert "rootplugin:helper" in root_rows, root_rows
    assert "privateplugin:helper" not in root_rows, root_rows
    assert "privateplugin:helper" in auditor_rows, auditor_rows
    # The auditor must not see the root profile's plugin skill.
    assert "rootplugin:helper" not in auditor_rows, auditor_rows


def test_plugin_skill_content_follows_the_request_profile(real_env):
    """Content for another profile's plugin skill is not served."""
    _write_plugin(
        real_env.root_home,
        "rootplugin",
        {"helper": _skill({"name": "helper", "description": "root helper"})},
    )
    _write_plugin(
        real_env.auditor_home,
        "privateplugin",
        {"helper": _skill({"name": "helper", "description": "auditor helper"})},
    )

    own = _content(real_env.auditor_home, "privateplugin:helper", profile="auditor")
    assert own["status"] == 200, own
    assert "auditor helper" in own["payload"].get("description", ""), own

    foreign = _content(real_env.auditor_home, "rootplugin:helper", profile="auditor")
    assert foreign["payload"].get("success") is not True, foreign


def test_disabled_plugin_skill_is_listed_disabled_and_content_refused(real_env):
    """``skills.disabled`` disables the plugin skill end to end.

    The row must be marked disabled (so the pickers stop offering it) and its
    content must be refused — the Agent refuses a disabled skill, so the WebUI
    must not serve it (re-gate item 2).
    """
    _write_plugin(
        real_env.root_home,
        "watchplugin",
        {"helper": _skill({"name": "helper", "description": "watched helper"})},
    )
    real_env.write_config(
        real_env.root_home,
        {"plugins": {"enabled": ["watchplugin"]}, "skills": {"disabled": ["watchplugin:helper"]}},
    )

    from api import routes

    with patch.object(
        routes, "_get_disabled_skill_names_for_profile", return_value={"watchplugin:helper"}
    ):
        rows = _listing(real_env.root_home)
        content = _content(real_env.root_home, "watchplugin:helper")

    assert "watchplugin:helper" in rows, rows
    assert rows["watchplugin:helper"]["disabled"] is True, rows
    assert content["payload"].get("success") is not True, content
    assert "disabled" in str(content["payload"].get("error", "")).lower(), content


def test_platform_disabled_plugin_skill_is_listed_disabled(real_env):
    """``skills.platform_disabled.webui`` disables the plugin skill too."""
    _write_plugin(
        real_env.root_home,
        "watchplugin",
        {"helper": _skill({"name": "helper", "description": "watched helper"})},
    )
    real_env.write_config(
        real_env.root_home,
        {
            "plugins": {"enabled": ["watchplugin"]},
            "skills": {"platform_disabled": {"webui": ["watchplugin:helper"]}},
        },
    )

    from api import routes

    with patch.object(
        routes, "_get_disabled_skill_names_for_profile", return_value={"watchplugin:helper"}
    ):
        rows = _listing(real_env.root_home)

    assert "watchplugin:helper" in rows, rows
    assert rows["watchplugin:helper"]["disabled"] is True, rows


def test_platform_restricted_plugin_skill_is_not_advertised(real_env):
    """A ``platforms: [windows]`` plugin skill is refused on Linux.

    The native Agent does not list it; the WebUI must not advertise it either
    (re-gate item 3).
    """
    import sys

    if sys.platform.startswith("win"):
        pytest.skip("the restriction is only observable off Windows")

    _write_plugin(
        real_env.root_home,
        "watchplugin",
        {
            "winonly": _skill(
                {"name": "winonly", "description": "windows only", "platforms": ["windows"]}
            ),
            "helper": _skill({"name": "helper", "description": "everywhere"}),
        },
    )

    rows = _listing(real_env.root_home)

    assert "watchplugin:helper" in rows, rows
    assert "watchplugin:winonly" not in rows, rows


def test_templated_plugin_skill_is_served_by_the_native_viewer(real_env):
    """A plugin skill with a template and nested linked files keeps both.

    The native ``skill_view`` expands ``${HERMES_SKILL_DIR}`` and reports
    nested linked files; the directory thin-reader dropped both (re-gate
    item 4).
    """
    _write_plugin(
        real_env.root_home,
        "watchplugin",
        {
            "templated": _skill(
                {"name": "templated", "description": "templated skill"},
                "dir is ${HERMES_SKILL_DIR}\n",
            )
        },
    )
    plugin_skill_dir = real_env.root_home / "plugins" / "watchplugin" / "skills" / "templated"
    (plugin_skill_dir / "references" / "nested").mkdir(parents=True, exist_ok=True)
    (plugin_skill_dir / "references" / "top.md").write_text("top\n", encoding="utf-8")
    (plugin_skill_dir / "references" / "nested" / "guide.md").write_text("nested\n", encoding="utf-8")
    (plugin_skill_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (plugin_skill_dir / "scripts" / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")

    content = _content(real_env.root_home, "watchplugin:templated")

    payload = content["payload"]
    assert content["status"] == 200, payload
    assert payload.get("name") == "watchplugin:templated", payload
    assert "${HERMES_SKILL_DIR}" not in payload.get("content", ""), payload
    assert "templated" in payload.get("content", ""), payload

    linked = payload.get("linked_files") or {}
    flat = [item for group in linked.values() for item in group]
    assert "references/top.md" in flat, linked
    assert "references/nested/guide.md" in flat, linked
    assert "scripts/run.sh" in flat, linked
