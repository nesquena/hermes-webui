"""The real-checkout git guard (tests/_real_repo_git_guard.py, installed by conftest.py).

Every test here puts a fake ``git`` first on PATH, so a missing guard runs the
fake, never the real git, and the fake's log shows what got through. A refused
test drains the guard's record so the report hook doesn't fail it; the hook
itself is exercised in a pytester run below.
"""
import contextlib
import importlib
import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest import mock

import pytest

import api
import api.updates as updates
from tests import _real_repo_git_guard as guard
from tests.conftest import HERMES_AGENT, REPO_ROOT, TESTS_DIR

pytest_plugins = ("pytester",)
pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='the fake git and the launchers are POSIX')


@pytest.fixture
def fake_git(tmp_path, monkeypatch):
    """A `git` on PATH that logs its arguments and prints `abc123`."""
    log = tmp_path / 'git.log'
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    exe = bindir / 'git'
    exe.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$FAKE_GIT_LOG"\necho abc123\n', encoding='utf-8')
    exe.chmod(0o755)
    monkeypatch.setenv('FAKE_GIT_LOG', str(log))
    monkeypatch.setenv('PATH', f'{bindir}{os.pathsep}{os.environ.get("PATH", "")}')
    monkeypatch.setattr(updates, '_resolve_git_executable', lambda: str(exe))
    return SimpleNamespace(log=log, exe=exe)  # the fake's absolute path, for cases that spell the program out


def _lines(fake):
    return fake.log.read_text(encoding='utf-8').splitlines() if fake.log.exists() else []


@contextlib.contextmanager
def refused():
    """Expect a refusal, then drain its record so the teardown hook stays quiet."""
    before = len(guard.REFUSALS)
    with pytest.raises(guard.RealCheckoutGitRefused, match='real checkout'):
        yield
    assert len(guard.REFUSALS) == before + 1
    guard.REFUSALS.pop()


@pytest.mark.parametrize('argv', [
    ['git', 'reset', '--hard', 'HEAD'],
    ['git', '-c', 'core.askPass=', '--no-pager', 'fetch', 'origin'],  # after git's own options
    ['git', 'ls-remote', 'origin'],  # contacts the remote
    ['git', 'symbolic-ref', '--delete', 'refs/remotes/origin/HEAD'],
    ['git', 'log', '--output=CHANGELOG.md', '-1'],
    ['git', 'branch', 'scratch'],
    ['git', 'branch', '-v', 'scratch'],  # -v lists only without a name
    ['git', 'stash', 'push', '-m', 'x'],
    ['git', '--unknown-global', 'rev-parse', 'HEAD'],  # cannot tell: fail closed
    ['git', '-c', 'alias.st=!git reset --hard', 'st'],  # an alias can expand to anything
    ['sh', '-c', 'git reset --hard HEAD'],
    ['sh', '-c', 'cd /tmp && exec git reset --hard HEAD'],
    ['env', 'GIT_TERMINAL_PROMPT=0', '{git}', 'reset', '--hard', 'HEAD'],  # the fake, by absolute path
    ['git', 'remote', 'show', 'origin'],  # contacts the remote without -n
])
def test_mutating_or_opaque_git_on_real_checkout_is_refused(fake_git, argv):
    with refused():
        subprocess.run([a.format(git=fake_git.exe) for a in argv], cwd=REPO_ROOT)
    assert _lines(fake_git) == []


@pytest.mark.parametrize('line', [
    'git reset --hard HEAD', '{git} reset --hard HEAD', 'true && git clean -fd',
    'GIT_TERMINAL_PROMPT=0 git fetch origin', 'if git reset --hard HEAD; then echo ok; fi',
])
def test_shell_command_line_naming_git_is_refused(fake_git, line):
    with refused():
        subprocess.run(line.format(git=fake_git.exe), shell=True, cwd=REPO_ROOT)
    assert _lines(fake_git) == []


def _named_target_cases(tmp):
    """(cwd, argv) pairs where one of the named targets is the real checkout."""
    return [
        (REPO_ROOT, ['git', f'--git-dir={REPO_ROOT / ".git"}', f'--work-tree={tmp}', 'reset', '--hard']),
        (REPO_ROOT, ['git', f'--work-tree={tmp}', '--git-dir=.git', 'reset', '--hard']),  # relative to cwd
        (REPO_ROOT, ['git', f'--git-dir={tmp / ".git"}', '-C', 'api', 'reset', '--hard']),  # -C into the checkout
        (tmp, ['git', '-C', str(tmp), f'--work-tree={os.path.relpath(REPO_ROOT, tmp)}', 'reset', '--hard']),  # relative to -C
    ]


@pytest.mark.parametrize('argv', [
    ['git', '--git-dir={tmp}/.git', 'reset', '--hard'],  # the work tree is still cwd, the checkout
    ['git', '--git-dir={tmp}/.git', 'add', '.'],  # reads cwd's files into another index: not repository-only
    ['git', '--work-tree={tmp}', 'reset', '--hard'],  # the repository is still cwd's
    ['git', '--work-tree={tmp}', 'update-ref', 'refs/heads/m', 'HEAD'],  # the repository is still cwd's
    ['git', '--git-dir={tmp}/x.git', 'push', '{real}', 'HEAD:refs/heads/x'],  # the argument names the checkout
    ['git', '--git-dir={tmp}/x.git', 'config', '--file', '{real}/.git/config', 'core.bare', 'false'],
    ['git', '--git-dir={tmp}/x.git', 'config', '--file={real}/.git/config', 'core.bare', 'false'],  # attached value
])
def test_a_single_override_from_the_checkout_is_refused(fake_git, tmp_path, argv):
    with refused():
        subprocess.run([a.format(tmp=tmp_path, real=REPO_ROOT) for a in argv], cwd=tmp_path if '{real}' in ''.join(argv) else REPO_ROOT)
    assert _lines(fake_git) == []


@pytest.mark.parametrize('case', range(4))
def test_every_named_target_must_be_safe(fake_git, tmp_path, case):
    cwd, argv = _named_target_cases(tmp_path)[case]
    with refused():
        subprocess.run(argv, cwd=cwd)
    assert _lines(fake_git) == []


def test_a_list_run_through_the_shell_is_refused(fake_git):
    with refused():
        subprocess.run(['git reset --hard HEAD'], shell=True, cwd=REPO_ROOT)
    assert _lines(fake_git) == []


def test_executable_names_the_program(fake_git):
    with refused():
        subprocess.run(['vcs', 'reset', '--hard', 'HEAD'], executable=str(shutil.which('git') or 'git'), cwd=REPO_ROOT)
    assert _lines(fake_git) == []


def test_git_dir_in_the_child_environment_is_a_target(fake_git, tmp_path, monkeypatch):
    with refused():
        subprocess.run(['git', 'reset', '--hard', 'HEAD'], cwd=tmp_path, env={**os.environ, 'GIT_DIR': str(REPO_ROOT / '.git')})
    with refused():
        subprocess.run(['git', 'reset', '--hard', 'HEAD'], cwd=tmp_path, env={b'GIT_DIR': os.fsencode(REPO_ROOT / '.git')})
    monkeypatch.setenv('GIT_WORK_TREE', str(REPO_ROOT))
    with refused():
        subprocess.run(['git', 'clean', '-fd'], cwd=tmp_path)
    assert _lines(fake_git) == []


def test_clone_option_values_are_not_the_destination(fake_git, tmp_path):
    with refused():
        subprocess.run(['git', 'clone', '--depth', '1', 'https://example.invalid/x.git', str(REPO_ROOT / 'sub')], cwd=tmp_path)
    with refused():
        subprocess.run(['git', 'init', '--separate-git-dir', str(REPO_ROOT / '.git'), str(tmp_path / 'w')], cwd=tmp_path)
    assert _lines(fake_git) == []


def test_mutating_git_through_run_git_inside_checkout_is_refused(fake_git):
    with refused():
        updates._run_git(['clean', '-fd'], str(REPO_ROOT / 'api'))
    assert _lines(fake_git) == []


def test_no_cwd_means_the_process_cwd(fake_git, monkeypatch):
    monkeypatch.chdir(REPO_ROOT)
    with refused():
        subprocess.run(['git', 'reset', '--hard', 'HEAD'])
    assert _lines(fake_git) == []


def test_positional_cwd_is_seen(fake_git):
    with refused():
        subprocess.Popen(['git', 'reset', '--hard', 'HEAD'], -1, None, None, None, None, None, True, False, str(REPO_ROOT))
    assert _lines(fake_git) == []


def test_the_updaters_own_agent_root_is_guarded(fake_git, tmp_path):
    root = tmp_path / 'agent-root'
    root.mkdir()
    guard.install(root)  # as conftest does with api.updates._AGENT_DIR
    try:
        with refused():
            subprocess.run(['git', 'fetch', 'origin'], cwd=root)
    finally:
        guard._ROOTS[:] = [r for r in guard._ROOTS if r != root.resolve()]
    assert _lines(fake_git) == []


def test_mutating_git_on_agent_checkout_is_refused(fake_git):
    if HERMES_AGENT is None:
        pytest.skip('no hermes-agent checkout discovered')
    with refused():
        subprocess.run(['git', 'reset', '--hard', 'HEAD'], cwd=HERMES_AGENT)
    assert _lines(fake_git) == []


def test_reimported_updates_module_is_still_guarded(fake_git):
    saved_module, saved_attr = sys.modules.pop('api.updates'), api.__dict__.pop('updates', None)
    try:
        fresh = importlib.import_module('api.updates')
        assert fresh is not saved_module
        fresh._resolve_git_executable = updates._resolve_git_executable
        with refused():
            fresh._run_git(['reset', '--hard', 'HEAD'], REPO_ROOT)
    finally:
        sys.modules['api.updates'] = saved_module
        if saved_attr is not None:
            api.updates = saved_attr
    # importing api.updates probes the version with read-only git; only the reset must be missing
    assert 'reset --hard HEAD' not in _lines(fake_git)


def test_refusal_on_a_worker_thread_is_recorded_for_teardown(fake_git):
    outcome = {}

    def work():
        try:
            updates._run_git(['reset', '--hard', 'HEAD'], REPO_ROOT)
        except guard.RealCheckoutGitRefused as exc:
            outcome['error'] = exc

    t = threading.Thread(target=work)
    t.start()
    t.join(10)
    assert isinstance(outcome.get('error'), guard.RealCheckoutGitRefused)
    assert 'real checkout' in guard.REFUSALS.pop()
    assert _lines(fake_git) == []


def test_read_only_git_on_real_checkout_passes_through(fake_git):
    done = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=REPO_ROOT, capture_output=True, text=True)
    assert done.stdout == 'abc123\n'
    assert updates._run_git(['tag', '--list', 'v*', '--sort=-creatordate'], REPO_ROOT) == ('abc123', True)
    assert updates._run_git(['config', '--get-all', 'remote.origin.fetch'], REPO_ROOT) == ('abc123', True)
    assert updates._run_git(['symbolic-ref', 'refs/remotes/origin/HEAD'], REPO_ROOT) == ('abc123', True)
    subprocess.run(['git', '--version'], cwd=REPO_ROOT, check=True)
    subprocess.run(['git', 'remote', '-v'], cwd=REPO_ROOT, check=True)
    subprocess.run(['git', 'remote', 'show', '-n', 'origin'], cwd=REPO_ROOT, check=True)
    subprocess.run(['git', 'branch', '--show-current'], cwd=REPO_ROOT, check=True)
    subprocess.run(['git', 'stash', 'list'], cwd=REPO_ROOT, check=True)
    subprocess.run(['git', 'branch'], cwd=REPO_ROOT, check=True)
    subprocess.run(['git', 'tag', '--sort=-creatordate'], cwd=REPO_ROOT, check=True)
    subprocess.run(['git', 'var', 'GIT_SHELL_PATH'], cwd=REPO_ROOT, check=True)
    subprocess.run(['git', 'worktree', 'list', '--porcelain'], cwd=REPO_ROOT, check=True)
    subprocess.run(['git', 'tag', '--points-at', 'HEAD'], cwd=REPO_ROOT, check=True)
    subprocess.run(['git', 'ls-remote', '--get-url', 'origin'], cwd=REPO_ROOT, check=True)
    assert _lines(fake_git) == [
        'rev-parse --short HEAD',
        'tag --list v* --sort=-creatordate',
        'config --get-all remote.origin.fetch',
        'symbolic-ref refs/remotes/origin/HEAD',
        '--version',
        'remote -v',
        'remote show -n origin',
        'branch --show-current',
        'stash list',
        'branch',
        'tag --sort=-creatordate',
        'var GIT_SHELL_PATH',
        'worktree list --porcelain',
        'tag --points-at HEAD',
        'ls-remote --get-url origin',
    ]


def test_git_aimed_elsewhere_is_allowed(fake_git, tmp_path, monkeypatch):
    assert updates._run_git(['reset', '--hard', 'HEAD'], tmp_path) == ('abc123', True)
    # a relative --git-dir resolves against the directory -C leaves git in, as git does
    subprocess.run(['git', '--git-dir=sub/.git', '-C', str(tmp_path), 'reset', '--hard'], cwd=REPO_ROOT, check=True)
    subprocess.run(['git', 'clone', '--depth', '1', 'https://example.invalid/x.git', str(tmp_path / 'c2')], cwd=REPO_ROOT, check=True)
    subprocess.run(['git', '-C', str(tmp_path), 'clean', '-fd'], cwd=REPO_ROOT, check=True)
    subprocess.run(['git', f'--work-tree={tmp_path}', f'--git-dir={tmp_path / ".git"}', 'reset', '--hard'], cwd=tmp_path, check=True)
    monkeypatch.chdir(REPO_ROOT)
    subprocess.run(['git', 'init', '-b', 'main', str(tmp_path / 'fresh')], check=True)
    subprocess.run(['git', 'clone', 'https://example.invalid/x.git', str(tmp_path / 'copy')], check=True)
    subprocess.run(['git', 'daemon', '--export-all', f'--base-path={tmp_path}', str(tmp_path)], check=True)
    # a write inside ANOTHER repository, named by --git-dir, never touches cwd's work tree or index
    subprocess.run(['git', '--git-dir', str(tmp_path / 'upstream.git'), 'symbolic-ref', 'HEAD', 'refs/heads/master'], check=True)
    subprocess.run(['git', f'--git-dir={tmp_path / "x.git"}', 'update-ref', 'refs/heads/m', 'HEAD'], cwd=REPO_ROOT, check=True)
    assert _lines(fake_git) == [
        'reset --hard HEAD',
        f'--git-dir=sub/.git -C {tmp_path} reset --hard',
        f'clone --depth 1 https://example.invalid/x.git {tmp_path / "c2"}',
        f'-C {tmp_path} clean -fd',
        f'--work-tree={tmp_path} --git-dir={tmp_path / ".git"} reset --hard',
        f'init -b main {tmp_path / "fresh"}',
        f'clone https://example.invalid/x.git {tmp_path / "copy"}',
        f'daemon --export-all --base-path={tmp_path} {tmp_path}',
        f'--git-dir {tmp_path / "upstream.git"} symbolic-ref HEAD refs/heads/master',
        f'--git-dir={tmp_path / "x.git"} update-ref refs/heads/m HEAD',
    ]


def test_a_shell_script_that_only_mentions_git_is_allowed(fake_git, monkeypatch, tmp_path):
    monkeypatch.chdir(REPO_ROOT)
    script = 'set -eu\n# only a git checkout gets here\n[ -e "$1/.git" ] && echo "-- $1 is a git checkout"\ntrue'
    subprocess.run(['bash', '-c', script, 'probe', str(REPO_ROOT)], check=True)
    subprocess.run('FOO=/srv/git true', shell=True, cwd=REPO_ROOT, check=True)  # a value ending in git
    (tmp_path / 'git').mkdir()  # a directory named git is not the program
    subprocess.run(['bash', '-c', 'true', 'probe', str(tmp_path / 'git'), '--out=/srv/git'], cwd=REPO_ROOT, check=True)
    assert _lines(fake_git) == []


def test_a_reexecuted_conftest_shares_the_one_guard(fake_git):
    spec = importlib.util.spec_from_file_location('_guard_conftest_alias', TESTS_DIR / 'conftest.py')
    alias = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(alias)
    assert subprocess.Popen is guard.GuardedPopen
    assert guard.GuardedPopen.__mro__[1] is guard.REAL_POPEN
    with refused():
        subprocess.run(['git', 'reset', '--hard', 'HEAD'], cwd=REPO_ROOT)
    assert _lines(fake_git) == []


def test_non_git_commands_are_not_touched(fake_git):
    done = subprocess.run([sys.executable, '-c', 'print(1)'], cwd=REPO_ROOT, capture_output=True, text=True)
    assert done.stdout == '1\n'
    assert subprocess.run(pathlib.Path(shutil.which('true')), cwd=REPO_ROOT).returncode == 0  # a single PathLike
    assert _lines(fake_git) == []


def test_patched_subprocess_run_still_wins(fake_git):
    with mock.patch('subprocess.run') as run:
        updates._run_git(['reset', '--hard', 'HEAD'], REPO_ROOT)
    run.assert_called_once()
    assert _lines(fake_git) == []


def test_allow_real_repo_git_opts_out(fake_git, allow_real_repo_git):
    assert updates._run_git(['reset', '--hard', 'HEAD'], REPO_ROOT) == ('abc123', True)
    assert _lines(fake_git) == ['reset --hard HEAD']


_INNER_CONFTEST = """
import sys
sys.path.insert(0, {repo_root!r})
from tests import _real_repo_git_guard as guard
guard.install({root!r})
pytest_runtest_logstart = guard.pytest_runtest_logstart
pytest_runtest_makereport = guard.pytest_runtest_makereport
"""

_INNER_TESTS = """
import os
import subprocess

import pytest

from tests import _real_repo_git_guard as guard

ROOT = {root!r}

try:  # at import time: fails nothing by itself, must not be blamed on the first test
    subprocess.run(['git', 'stash', 'push'], cwd=ROOT)
except guard.RealCheckoutGitRefused:
    pass


def test_first_is_innocent():
    pass


def test_swallowed_refusal(monkeypatch):
    monkeypatch.setenv('GUARD_MARK', '1')
    try:
        subprocess.run(['git', 'reset', '--hard', 'HEAD'], cwd=ROOT)
    except guard.RealCheckoutGitRefused:
        pass


def test_after_it_sees_restored_state():
    assert 'GUARD_MARK' not in os.environ


def test_propagated_refusal():
    subprocess.run(['git', 'reset', '--hard', 'HEAD'], cwd=ROOT)


@pytest.fixture
def refusing_finalizer():
    yield
    try:
        subprocess.run(['git', 'clean', '-fd'], cwd=ROOT)
    except guard.RealCheckoutGitRefused:
        pass


def test_refusal_in_a_finalizer(refusing_finalizer):
    pass


@pytest.mark.xfail(reason='an xfail mark must not hide a refusal')
def test_xfail_marked_refusal():
    try:
        subprocess.run(['git', 'reset', '--hard', 'HEAD'], cwd=ROOT)
    except guard.RealCheckoutGitRefused:
        pass


def test_last_one_is_innocent():
    pass
"""


def test_a_refusal_fails_its_own_test_after_its_finalizers_ran(pytester):
    """The report hook: the offender fails (once), its monkeypatches are undone before
    the next test runs, a refusal inside a finalizer errors that test only, and the
    neighbours pass."""
    root = pytester.path / 'guarded-repo'
    root.mkdir()
    pytester.makeconftest(_INNER_CONFTEST.format(repo_root=str(REPO_ROOT), root=str(root)))
    pytester.makepyfile(test_inner=_INNER_TESTS.format(root=str(root)))
    result = pytester.runpytest_subprocess('-p', 'no:cacheprovider', '-q', '-rfE')
    result.assert_outcomes(passed=4, failed=3, errors=1)  # the finalizer case passes its call phase
    summary = [line for line in result.outlines if line.startswith(('FAILED', 'ERROR'))]
    assert [line.split(' - ')[0] for line in summary] == [
        'FAILED test_inner.py::test_swallowed_refusal',
        'FAILED test_inner.py::test_propagated_refusal',
        'FAILED test_inner.py::test_xfail_marked_refusal',
        'ERROR test_inner.py::test_refusal_in_a_finalizer',
    ], summary
    # the three reports carry the guard's message (the -q summary lines truncate it)
    assert result.stdout.str().count('hermes test repo guard') >= 4
