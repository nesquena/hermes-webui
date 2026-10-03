"""Launch-env compatibility through the production Skills route (Agent adapter)."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


@pytest.mark.parametrize('case', [
    'root', 'root-alias', 'root-dotenv', 'named', 'named-dotenv', 'pinned-named',
])
def test_skills_launch_environment_is_root_only_and_stable(tmp_path, case):
    root = tmp_path / 'root'
    named = root / 'profiles' / 'named'
    launch = tmp_path / 'launch-skills'
    replacement = tmp_path / 'profile-skills'
    mirrored = tmp_path / 'mirrored-skills'
    for directory, name in (
        (root / 'skills', 'local-skill'), (named / 'skills', 'local-skill'),
        (launch, 'team-skill'), (replacement, 'profile-skill'),
        (mirrored, 'foreign-skill'),
    ):
        skill = directory / name
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: Test skill\n---\nBody\n')
    for home in (root, named):
        (home / 'config.yaml').write_text('skills:\n  external_dirs: ["$REL7842_SKILL_ROOT"]\n')
    home = root if case.startswith('root') else named
    if case.endswith('dotenv'):
        (home / '.env').write_text(f'REL7842_SKILL_ROOT={replacement}\n')
    env = dict(os.environ, HERMES_HOME=str(named if case == 'pinned-named' else root),
               HERMES_BASE_HOME=str(root), HERMES_WEBUI_STATE_DIR=str(tmp_path / 'state'),
               HERMES_WEBUI_ISOLATED_PROFILE='1' if case == 'pinned-named' else '0',
               HERMES_WEBUI_TEST_NETWORK_BLOCK='1', REL7842_SKILL_ROOT=str(launch))
    script = textwrap.dedent('''
        import os, sys, types
        from pathlib import Path
        from unittest.mock import MagicMock
        from urllib.parse import urlparse
        # The early startup module must capture the launch env before profile
        # initialization or a stream mirrors a different environment into it.
        import api.paths
        root, home, mirrored = map(Path, sys.argv[1:4])
        case = sys.argv[4]
        os.environ['REL7842_SKILL_ROOT'] = str(mirrored)
        from api import profiles, routes, yaml_compat
        os.environ['REL7842_SKILL_ROOT'] = str(mirrored)
        os.environ['HERMES_HOME'] = str(root / 'profiles' / 'named')
        sys.path.insert(0, str(Path.cwd() / 'tests'))
        from test_profile_external_skills_scope import FakeAgent
        process_home = home if case == 'pinned-named' else root
        agent = FakeAgent(process_home=process_home)
        modules = agent.build_modules()
        modules['agent.skill_utils'].iter_skill_index_files = lambda root, pattern: root.rglob(pattern)
        tools = types.ModuleType('tools')
        tools.__path__ = []
        skills = types.ModuleType('tools.skills_tool')
        skills.MAX_DESCRIPTION_LENGTH = 512
        skills._EXCLUDED_SKILL_DIRS = set()
        skills._parse_frontmatter = lambda text: (yaml_compat.safe_load(text.split('---')[1]), '')
        skills._sort_skills = lambda values: values
        skills.skill_matches_platform = lambda frontmatter: True
        modules.update({'tools': tools, 'tools.skills_tool': skills})
        sys.modules.update(modules)
        profiles.get_active_profile_name = lambda: (
            'renamed-root' if case == 'root-alias' else 'default' if home == root else 'named'
        )
        profiles.get_hermes_home_for_profile = lambda name: home
        profiles.get_active_hermes_home = lambda: home
        profiles.get_process_profile_home = lambda: process_home
        payloads = []
        routes.j = lambda handler, payload, **kwargs: payloads.append(payload)
        routes.handle_get(MagicMock(), urlparse('/api/skills'))
        payload = payloads[-1]
        expected = ['local-skill']
        if case.endswith('dotenv'):
            expected.append('profile-skill')
        elif case.startswith('root'):
            expected.append('team-skill')
        assert sorted(s['name'] for s in payload['skills']) == sorted(expected), payload
        assert payload['runtime_scope'] == ('unavailable' if case in ('named', 'pinned-named') else 'profile'), payload
        assert agent.override is None
        assert os.environ['REL7842_SKILL_ROOT'] == str(mirrored)
        # A variable introduced only after startup remains unconfirmed, even
        # for root. Neither later stream values nor a late resolver import own it.
        os.environ['REL7842_LATE_ROOT'] = str(mirrored)
        (home / 'config.yaml').write_text('skills:\\n  external_dirs: ["$REL7842_LATE_ROOT"]\\n')
        routes.handle_get(MagicMock(), urlparse('/api/skills'))
        assert payloads[-1]['runtime_scope'] == 'unavailable', payloads[-1]
        assert [s['name'] for s in payloads[-1]['skills']] == ['local-skill'], payloads[-1]
    ''')
    proc = subprocess.run(
        [sys.executable, '-c', script, str(root), str(home), str(mirrored), case],
        cwd=Path(__file__).resolve().parents[1], env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
