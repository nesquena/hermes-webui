"""Agent-free contract tests for profile-scoped external skill roots.

``tests/test_profile_external_skills_api.py`` drives the real Hermes Agent; CI has
no agent checkout, so this file models the small agent surface the WebUI relies on
(context-local home override, routed-profile predicate, home key) and pins the WebUI
side of the contract: which profile is bound, that external roots come from it, the
fail-closed path, and the legacy fallback. The shape mirrors
``tests/test_mcp_runtime_profile_scope_unit.py``.
"""

import os
import sys
import types
from pathlib import Path


class FakeAgent:
    """Minimal model of the hermes-agent surface used for external skill roots."""

    def __init__(self, *, routed=None, expose_routing=True, process_home=None):
        self.override = None
        self.process_home = process_home
        self._routed = routed
        self._expose_routing = expose_routing
        self.external_by_home = {}

    def home(self):
        return self.override or os.environ.get("HERMES_HOME", "")

    def routed(self):
        if self._routed is not None:
            return self._routed
        if self.override is None:
            return False
        anchor = self.process_home or os.environ.get("HERMES_HOME", "")
        return self.key(self.override) != self.key(anchor)

    @staticmethod
    def key(path):
        return str(Path(path).resolve())

    def build_modules(self):
        agent = self

        hc = types.ModuleType("hermes_constants")

        def set_hermes_home_override(home):
            previous = agent.override
            agent.override = None if home is None else str(home)
            return previous

        def reset_hermes_home_override(token):
            agent.override = token

        hc.set_hermes_home_override = set_hermes_home_override
        hc.reset_hermes_home_override = reset_hermes_home_override
        hc.hermes_home_key = lambda path=None: agent.key(
            path if path is not None else agent.home()
        )

        agent_pkg = types.ModuleType("agent")
        agent_pkg.__path__ = []

        secret = types.ModuleType("agent.secret_scope")
        if agent._expose_routing:
            secret.serves_routed_profile = agent.routed

        skill_utils = types.ModuleType("agent.skill_utils")

        def get_external_skills_dirs():
            return list(agent.external_by_home.get(str(agent.home()), []))

        skill_utils.get_external_skills_dirs = get_external_skills_dirs
        agent_pkg.skill_utils = skill_utils

        return {
            "hermes_constants": hc,
            "agent": agent_pkg,
            "agent.secret_scope": secret,
            "agent.skill_utils": skill_utils,
        }


def _install_fake_agent(
    monkeypatch,
    *,
    external_dirs_by_home,
    process_home,
    routed=None,
    expose_routing=True,
):
    agent = FakeAgent(
        routed=routed, expose_routing=expose_routing, process_home=process_home
    )
    agent.external_by_home = {str(k): v for k, v in external_dirs_by_home.items()}
    for name, module in agent.build_modules().items():
        monkeypatch.setitem(sys.modules, name, module)
    return agent


def _patch_profile(monkeypatch, profiles, name, home, process_home):
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: name)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda _name: home)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: home)
    monkeypatch.setattr(profiles, "get_process_profile_home", lambda: process_home)


def test_external_roots_resolve_from_request_profile(monkeypatch, tmp_path):
    """A named profile's external roots must not resolve from the process home."""
    from api import profiles, routes

    default_home = tmp_path / "default"
    active_home = tmp_path / "profiles" / "translation"
    shared_skills = tmp_path / "shared-skills"
    (active_home / "skills").mkdir(parents=True)
    shared_skills.mkdir()

    monkeypatch.setenv("HERMES_HOME", str(default_home))
    _patch_profile(monkeypatch, profiles, "translation", active_home, default_home)
    agent = _install_fake_agent(
        monkeypatch,
        external_dirs_by_home={active_home: [shared_skills]},
        process_home=default_home,
    )

    dirs, scope = routes._active_skill_search_dirs_scoped(active_home / "skills")

    assert shared_skills in dirs
    assert scope == "profile"
    # The read-only scope is released on exit, not leaked to the next request.
    assert agent.override is None


def test_root_profile_binds_its_own_home_not_a_mirrored_turn(monkeypatch, tmp_path):
    """The root profile must not follow a streaming turn's mirrored HERMES_HOME."""
    from api import profiles, routes

    default_home = tmp_path / "default"
    other_home = tmp_path / "profiles" / "translation"
    root_shared = tmp_path / "root-shared"
    other_shared = tmp_path / "other-shared"
    (default_home / "skills").mkdir(parents=True)
    root_shared.mkdir()
    other_shared.mkdir()

    # A streaming turn has mirrored another profile's home into os.environ.
    monkeypatch.setenv("HERMES_HOME", str(other_home))
    _patch_profile(monkeypatch, profiles, "default", default_home, default_home)
    _install_fake_agent(
        monkeypatch,
        external_dirs_by_home={default_home: [root_shared], other_home: [other_shared]},
        process_home=default_home,
    )

    dirs, scope = routes._active_skill_search_dirs_scoped(default_home / "skills")

    assert root_shared in dirs
    assert other_shared not in dirs
    assert scope == "profile"


def test_routing_disagreement_withholds_external_roots(monkeypatch, tmp_path):
    """Fail closed: an unconfirmed scope must not contribute external roots."""
    from api import profiles, routes

    default_home = tmp_path / "default"
    active_home = tmp_path / "profiles" / "translation"
    shared_skills = tmp_path / "shared-skills"
    (active_home / "skills").mkdir(parents=True)
    shared_skills.mkdir()

    monkeypatch.setenv("HERMES_HOME", str(default_home))
    _patch_profile(monkeypatch, profiles, "translation", active_home, default_home)
    agent = _install_fake_agent(
        monkeypatch,
        external_dirs_by_home={active_home: [shared_skills]},
        process_home=default_home,
        routed=False,
    )

    dirs, scope = routes._active_skill_search_dirs_scoped(active_home / "skills")

    assert shared_skills not in dirs
    assert scope == "unavailable"
    assert agent.override is None


def test_agent_without_routed_predicate_reports_legacy_scope(monkeypatch, tmp_path):
    """An agent that predates routed-profile routing keeps its process-wide lookup."""
    from api import profiles, routes

    default_home = tmp_path / "default"
    active_home = tmp_path / "profiles" / "translation"
    shared_skills = tmp_path / "shared-skills"
    (active_home / "skills").mkdir(parents=True)
    shared_skills.mkdir()

    monkeypatch.setenv("HERMES_HOME", str(default_home))
    _patch_profile(monkeypatch, profiles, "translation", active_home, default_home)
    _install_fake_agent(
        monkeypatch,
        external_dirs_by_home={active_home: [shared_skills]},
        process_home=default_home,
        expose_routing=False,
    )

    dirs, scope = routes._active_skill_search_dirs_scoped(active_home / "skills")

    assert shared_skills in dirs
    assert scope == "legacy_process"
