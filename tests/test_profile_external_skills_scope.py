"""Agent-free contract tests for profile-scoped external skill roots.

``tests/test_profile_external_skills_api.py`` drives the real Hermes Agent; CI has
no agent checkout, so this file models the agent surface around the lookup
(context-local home override, routed-profile predicate, process-env expansion) and
pins the WebUI side of the contract: external roots come from the request
profile's own files whatever that surface reports, one rejected entry skips only
itself, and an unreadable skills config withholds external roots.
"""

import os
import sys
import types
from pathlib import Path

import yaml
import pytest


class FakeAgent:
    """Minimal model of the hermes-agent surface used for external skill roots."""

    def __init__(self, *, routed=None, expose_routing=True, process_home=None,
                 expose_home_override=True):
        self.override = None
        self.process_home = process_home
        self._routed = routed
        self._expose_routing = expose_routing
        self._expose_home_override = expose_home_override
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

        if agent._expose_home_override:
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
            config = Path(agent.home()) / 'config.yaml'
            if config.exists():
                entries = yaml.safe_load(config.read_text())['skills']['external_dirs']
                return [Path(os.path.expandvars(entry)) for entry in entries]
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
    expose_home_override=True,
):
    agent = FakeAgent(
        routed=routed,
        expose_routing=expose_routing,
        process_home=process_home,
        expose_home_override=expose_home_override,
    )
    agent.external_by_home = {str(k): v for k, v in external_dirs_by_home.items()}
    for home, entries in external_dirs_by_home.items():
        Path(home).mkdir(parents=True, exist_ok=True)
        (Path(home) / 'config.yaml').write_text(yaml.safe_dump({
            'skills': {'external_dirs': [str(entry) for entry in entries]},
        }))
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
    # The lookup never installs an Agent home override.
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


@pytest.mark.parametrize('agent_shape', [
    {'routed': False},  # routed-profile check disagrees (mirrored streaming turn)
    {'expose_routing': False},  # Agent without the routed-profile predicate
    {'expose_routing': False, 'expose_home_override': False},  # no home override
], ids=['routing-disagrees', 'no-routed-predicate', 'no-home-override'])
def test_agent_routing_state_does_not_withhold_external_roots(monkeypatch, tmp_path, agent_shape):
    """The resolver reads the request profile's files, so Agent routing is not consulted."""
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
        **agent_shape,
    )

    dirs, scope = routes._active_skill_search_dirs_scoped(active_home / "skills")

    assert dirs == [active_home / "skills", shared_skills]
    assert scope == "profile"
    assert agent.override is None


def test_home_variable_expansion_is_profile_local(monkeypatch, tmp_path):
    from api import profiles, routes

    root = tmp_path / 'root'
    named = tmp_path / 'profiles' / 'named'
    for home in (root, named):
        (home / 'skills').mkdir(parents=True)
        (home / 'shared').mkdir()
    monkeypatch.setenv('HERMES_HOME', str(root))
    _install_fake_agent(monkeypatch, external_dirs_by_home={
        root: ['${HERMES_HOME}/shared'], named: ['${HERMES_HOME}/shared'],
    }, process_home=root)
    for name, home in [('named', named), ('default', root), ('named', named)]:
        _patch_profile(monkeypatch, profiles, name, home, root)
        dirs, scope = routes._active_skill_search_dirs_scoped(home / 'skills')
        assert scope == 'profile'
        assert dirs == [home / 'skills', home / 'shared']


def test_profile_path_expansion_uses_own_env_and_observes_edits(monkeypatch, tmp_path):
    from api import profiles, routes
    from api.paths import HOME

    root = tmp_path / 'root'
    home = tmp_path / 'named'
    (home / 'skills').mkdir(parents=True)
    for name in ('shared', 'updated', 'relative'):
        (home / name).mkdir()
    monkeypatch.setenv('SKILL_ROOT', str(root))
    _patch_profile(monkeypatch, profiles, 'named', home, root)
    _install_fake_agent(monkeypatch, external_dirs_by_home={home: [
        '${SKILL_ROOT}', 'relative', '$HERMES_HOME/relative', '~', '$HOME',
    ]}, process_home=root)
    (home / '.env').write_text(f'SKILL_ROOT={home / "shared"}\nHERMES_HOME={root}\n')
    dirs, scope = routes._active_skill_search_dirs_scoped(home / 'skills')
    assert scope == 'profile'
    assert dirs == [home / 'skills', home / 'shared', home / 'relative', HOME.resolve()]
    (home / '.env').write_text(f'SKILL_ROOT={home / "updated"}\n')
    assert routes._active_skill_search_dirs(home / 'skills')[1] == home / 'updated'
    (home / 'config.yaml').write_text('skills:\n  external_dirs: [relative]\n')
    assert routes._active_skill_search_dirs(home / 'skills') == [
        home / 'skills', home / 'relative',
    ]
    # A variable known only to the live process must never contribute a root.
    (home / 'config.yaml').write_text('skills:\n  external_dirs: ["$SKILL_ROOT"]\n')
    (home / '.env').write_text('')
    assert routes._active_skill_search_dirs_scoped(home / 'skills') == (
        [home / 'skills'], 'profile',
    )


@pytest.mark.parametrize('replacement', [
    '$MISSING_ROOT', '${MISSING_ROOT}', '${SKILL_ROOT}', 'skills$v2',
])
def test_expansion_is_single_pass_and_keeps_unknown_tokens_literal(monkeypatch, tmp_path, replacement):
    """Match os.path.expandvars: a `$` in a replacement value stays literal text."""
    from api import profiles, routes

    root = tmp_path / 'root'
    home = tmp_path / 'named'
    (home / 'skills').mkdir(parents=True)
    (home / replacement).mkdir()
    _patch_profile(monkeypatch, profiles, 'named', home, root)
    _install_fake_agent(monkeypatch, external_dirs_by_home={home: ['$SKILL_ROOT']},
                        process_home=root)
    (home / '.env').write_text(f'SKILL_ROOT={replacement}\n')
    assert routes._active_skill_search_dirs_scoped(home / 'skills') == (
        [home / 'skills', home / replacement], 'profile',
    )


@pytest.mark.parametrize('rejected', [
    '$MISSING_ROOT', '${MISSING_ROOT}', '$CHAINED_ROOT', '~nosuchuser7842/skills',
    'loop', '/nul\x00byte', 'skills', '${HERMES_HOME}/skills',
], ids=['bare-var', 'braced-var', 'replacement-token', 'unknown-user-home', 'symlink-loop',
        'nul-byte', 'local-skills-dir', 'local-skills-var'])
def test_one_rejected_entry_keeps_the_other_external_roots(monkeypatch, tmp_path, rejected):
    """Skip an entry that resolves to no directory, as the Agent does, and keep the rest."""
    from api import profiles, routes

    root = tmp_path / 'root'
    home = tmp_path / 'named'
    first, second = tmp_path / 'first', tmp_path / 'second'
    for directory in (home / 'skills', first, second):
        directory.mkdir(parents=True)
    _patch_profile(monkeypatch, profiles, 'named', home, root)
    _install_fake_agent(monkeypatch, external_dirs_by_home={
        home: [str(first), rejected, str(second), str(first)],
    }, process_home=root)
    (home / '.env').write_text('CHAINED_ROOT=$MISSING_ROOT\n')
    # A relative entry resolves under the profile home; make it a symlink loop.
    (home / 'loop').symlink_to(home / 'loop')

    assert routes._active_skill_search_dirs_scoped(home / 'skills') == (
        [home / 'skills', first, second], 'profile',
    )


@pytest.mark.parametrize('profile', ['translation', 'default'])
def test_profiles_can_share_skills_through_external_dirs(monkeypatch, tmp_path, profile):
    """Agent-supported sharing: a profile may list another profile's skills tree."""
    from api import profiles, routes

    root = tmp_path / 'hermes'
    translation = root / 'profiles' / 'translation'
    other = root / 'profiles' / 'other'
    for directory in (root / 'skills', translation / 'skills', other / 'skills'):
        directory.mkdir(parents=True)
    home = translation if profile == 'translation' else root
    monkeypatch.setattr(profiles, '_DEFAULT_HERMES_HOME', root)
    _patch_profile(monkeypatch, profiles, profile, home, root)
    shared = [root / 'skills', other / 'skills'] if profile == 'translation' else [
        translation / 'skills', other / 'skills',
    ]
    _install_fake_agent(monkeypatch, external_dirs_by_home={home: shared}, process_home=root)

    assert routes._active_skill_search_dirs_scoped(home / 'skills') == (
        [home / 'skills', *shared], 'profile',
    )


@pytest.mark.skipif(not Path('/root').is_dir(), reason='needs an account home to name')
def test_other_user_home_expands_as_the_agent_does(monkeypatch, tmp_path):
    from api import profiles, routes

    root = tmp_path / 'root'
    home = tmp_path / 'named'
    (home / 'skills').mkdir(parents=True)
    _patch_profile(monkeypatch, profiles, 'named', home, root)
    _install_fake_agent(monkeypatch, external_dirs_by_home={home: ['~root']},
                        process_home=root)

    assert routes._active_skill_search_dirs_scoped(home / 'skills') == (
        [home / 'skills', Path(os.path.expanduser('~root')).resolve()], 'profile',
    )


def test_unreadable_external_dir_keeps_the_other_external_roots(monkeypatch, tmp_path):
    """An entry the server cannot stat is skipped like a missing one."""
    from api import profiles, routes

    root = tmp_path / 'root'
    home = tmp_path / 'named'
    shared = tmp_path / 'shared'
    locked = tmp_path / 'locked'
    for directory in (home / 'skills', shared, locked / 'inner'):
        directory.mkdir(parents=True)
    _patch_profile(monkeypatch, profiles, 'named', home, root)
    _install_fake_agent(monkeypatch, external_dirs_by_home={
        home: [str(locked / 'inner'), str(shared)],
    }, process_home=root)
    original_is_dir = Path.is_dir

    def is_dir(path):
        if path == locked / 'inner':
            raise PermissionError(13, 'Permission denied', str(path))
        return original_is_dir(path)

    monkeypatch.setattr(Path, 'is_dir', is_dir)

    assert routes._active_skill_search_dirs_scoped(home / 'skills') == (
        [home / 'skills', shared], 'profile',
    )


@pytest.mark.parametrize('config_text', [
    'skills:\n', 'skills:\n  external_dirs:\n', '{}\n', '',
], ids=['skills-null', 'dirs-null', 'empty-mapping', 'empty-file'])
def test_empty_skills_config_lists_no_external_roots(monkeypatch, tmp_path, config_text):
    """An empty or bare ``skills:`` section is a complete, empty external set."""
    from api import profiles, routes

    root = tmp_path / 'root'
    home = tmp_path / 'named'
    (home / 'skills').mkdir(parents=True)
    _patch_profile(monkeypatch, profiles, 'named', home, root)
    _install_fake_agent(monkeypatch, external_dirs_by_home={}, process_home=root)
    (home / 'config.yaml').write_text(config_text)

    assert routes._active_skill_search_dirs_scoped(home / 'skills') == (
        [home / 'skills'], 'profile',
    )


@pytest.mark.parametrize('config_text, listed', [
    ('skills: [unclosed\n', []),
    ('- not\n- a mapping\n', []),
    ('skills: shared\n', []),
    ('skills:\n  external_dirs: {path: /x}\n', []),
    ('\ufeffskills:\n  external_dirs: [shared]\n', ['shared']),
    ('skills:\n  external_dirs: [2024, shared]\n', ['2024', 'shared']),
    ('# caf\xe9\nskills:\n  external_dirs: [shared]\n', []),
], ids=['invalid-yaml', 'top-level-list', 'skills-not-mapping', 'dirs-not-list',
        'utf8-bom', 'non-string-entry', 'not-utf8'])
def test_malformed_skills_config_is_read_as_the_agent_reads_it(monkeypatch, tmp_path,
                                                               config_text, listed):
    """Follow the Agent's config reader: malformed lists none, entries are str()'d."""
    from api import profiles, routes

    root = tmp_path / 'root'
    home = tmp_path / 'named'
    for directory in (home / 'skills', home / 'shared', home / '2024'):
        directory.mkdir(parents=True)
    _patch_profile(monkeypatch, profiles, 'named', home, root)
    _install_fake_agent(monkeypatch, external_dirs_by_home={}, process_home=root)
    # Latin-1 keeps a non-UTF-8 byte in the not-utf8 row; ASCII rows are identical.
    encoding = 'latin-1' if '\xe9' in config_text else 'utf-8'
    (home / 'config.yaml').write_text(config_text, encoding=encoding)

    assert routes._active_skill_search_dirs_scoped(home / 'skills') == (
        [home / 'skills', *(home / name for name in listed)], 'profile',
    )


@pytest.mark.skipif(hasattr(os, 'geteuid') and os.geteuid() == 0,
                    reason='root reads files regardless of mode')
def test_unreadable_config_reports_unavailable(monkeypatch, tmp_path):
    """A config that cannot be read is retried later, not cached as an empty list."""
    from api import profiles, routes

    root = tmp_path / 'root'
    home = tmp_path / 'named'
    (home / 'skills').mkdir(parents=True)
    (home / 'shared').mkdir()
    _patch_profile(monkeypatch, profiles, 'named', home, root)
    _install_fake_agent(monkeypatch, external_dirs_by_home={home: ['shared']},
                        process_home=root)
    (home / 'config.yaml').chmod(0)
    try:
        assert routes._active_skill_search_dirs_scoped(home / 'skills') == (
            [home / 'skills'], 'unavailable',
        )
    finally:
        (home / 'config.yaml').chmod(0o600)
