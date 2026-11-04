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
    ['git', 'config', '--', 'core.guardprobe', '--get'],  # after `--` the read option is the value written
    ['git', 'tag', '--', '--list'],  # `--list` is a tag name here, not a listing
    ['git', 'stash', '--', 'list'],  # `stash push -- list`
    ['git', 'stash', '-m', 'list'],  # `stash push -m list`
    ['git', 'stash', '--message', 'show'],
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


def test_config_keys_and_values_are_not_places(fake_git, tmp_path):
    """A temp repo's config may name the checkout's own interpreter or a path inside it (the
    supported runner's Python is <checkout>/.venv/bin/python); only --file names a place."""
    inside = str(REPO_ROOT / '.venv' / 'bin' / 'python')
    subprocess.run(['git', 'config', 'core.fsmonitor', f'{inside} hook.py'], cwd=tmp_path, check=True)
    subprocess.run(['git', 'config', 'core.sshCommand', inside], cwd=tmp_path, check=True)
    subprocess.run(['git', 'config', 'user.name', str(REPO_ROOT)], cwd=tmp_path, check=True)
    subprocess.run(['git', 'config', '--get', str(REPO_ROOT / 'tests')], cwd=tmp_path, check=True)
    assert _lines(fake_git) == [
        f'config core.fsmonitor {inside} hook.py',
        f'config core.sshCommand {inside}',
        f'config user.name {REPO_ROOT}',
        f'config --get {REPO_ROOT / "tests"}',
    ]
    config = str(REPO_ROOT / '.git' / 'config')
    for spelling in (['-f', config], ['--file', config], ['--fil', config], ['--fil=' + config], ['-f' + config],
                     ['-zf', config], ['-zf' + config]):
        with refused():
            subprocess.run(['git', 'config', *spelling, 'core.bare', 'false'], cwd=tmp_path)
    assert len(_lines(fake_git)) == 4


@pytest.mark.parametrize('key', ['GIT_CONFIG', 'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM', 'GIT_COMMON_DIR'])
def test_the_config_files_git_writes_from_the_environment_are_targets(fake_git, tmp_path, key):
    inside = str(REPO_ROOT / '.git' / ('config' if key != 'GIT_COMMON_DIR' else ''))
    with refused():
        subprocess.run(['git', 'config', 'core.bare', 'false'], cwd=tmp_path, env={**os.environ, key: inside})
    subprocess.run(['git', 'config', 'core.bare', 'false'], cwd=tmp_path, env={**os.environ, key: str(tmp_path / 'cfg')}, check=True)
    assert _lines(fake_git) == ['config core.bare false']


def test_an_unreadable_cwd_only_matters_to_git(fake_git, tmp_path):
    with refused():
        subprocess.run(['git', 'reset', '--hard', 'HEAD'], cwd=5)
    with pytest.raises(TypeError):
        subprocess.run(['ls'], cwd=5)
    assert _lines(fake_git) == []


def test_a_bytes_cwd_is_read_like_a_str(fake_git, tmp_path):
    with refused():
        subprocess.run(['git', 'reset', '--hard', 'HEAD'], cwd=os.fsencode(REPO_ROOT))
    subprocess.run(['git', 'reset', '--hard', 'HEAD'], cwd=os.fsencode(tmp_path), check=True)
    assert _lines(fake_git) == ['reset --hard HEAD']


def test_a_basetemp_inside_the_checkout_exempts_nothing(fake_git, monkeypatch):
    """A plain directory under a basetemp inside the checkout is discovered by git as the
    checkout, so it stays guarded; that layout is not one the suite supports."""
    basetemp = REPO_ROOT / '.pytest-basetemp'
    monkeypatch.setattr(guard, '_EXEMPT', [])
    guard.pytest_configure(SimpleNamespace(getoption=lambda name, default=None: str(basetemp)))
    assert guard._EXEMPT == [basetemp]
    plain = basetemp / 'test_x0' / 'plain'
    assert guard.is_real_checkout(plain)
    assert guard.is_real_checkout(basetemp)
    assert guard.is_real_checkout(REPO_ROOT)
    for argv, cwd in (
        (['git', 'reset', '--hard', 'HEAD'], plain),
        (['git', 'fetch', 'origin'], plain),
        (['git', 'init'], plain),
        (['git', 'clean', '-fdx'], basetemp),
    ):
        assert guard.refusal(argv, cwd=str(cwd)) is not None, (argv, cwd)
    guard.pytest_configure(SimpleNamespace(getoption=lambda name, default=None: None))
    assert guard._EXEMPT == []


def test_a_basetemp_outside_the_checkout_exempts_nothing(tmp_path, monkeypatch):
    """The supported runner passes an explicit --basetemp outside the checkout; a fake
    checkout a test installs under it must still be guarded, and the real one too."""
    monkeypatch.setattr(guard, '_EXEMPT', [tmp_path.parent.resolve()])
    monkeypatch.setattr(guard, '_ROOTS', list(guard._ROOTS))
    fake = tmp_path / 'fake'
    fake.mkdir()
    guard.install(fake)
    assert guard.is_real_checkout(fake / 'src')
    assert guard.is_real_checkout(REPO_ROOT)
    assert not guard.is_real_checkout(tmp_path)


def test_a_fake_root_under_a_basetemp_inside_the_checkout_is_guarded(monkeypatch):
    basetemp = REPO_ROOT / '.pytest-basetemp'
    monkeypatch.setattr(guard, '_EXEMPT', [basetemp])
    monkeypatch.setattr(guard, '_ROOTS', list(guard._ROOTS))
    guard.install(basetemp / 'test_x0' / 'main')
    assert guard.is_real_checkout(basetemp / 'test_x0' / 'main' / 'src')
    assert guard.is_real_checkout(basetemp / 'test_x0' / 'other')  # still inside the real checkout
    assert guard.is_real_checkout(REPO_ROOT)


def test_a_linked_worktree_of_the_checkout_made_during_a_test_is_guarded(fake_git, tmp_path, monkeypatch):
    """Its `.git` file points into the checkout's repository, so its refs and config are the checkout's."""
    linked = tmp_path / 'wt'
    linked.mkdir()
    (linked / '.git').write_text(f'gitdir: {REPO_ROOT}/.git/worktrees/wt-test\n', encoding='utf-8')
    for exempt in ([], [tmp_path.parent.resolve()]):
        monkeypatch.setattr(guard, '_EXEMPT', exempt)
        assert guard.is_real_checkout(linked)
        assert guard.is_real_checkout(linked / 'sub')
        with refused():
            subprocess.run(['git', 'tag', 'scratch'], cwd=linked)
    other = tmp_path / 'other-wt'
    other.mkdir()
    (other / '.git').write_text(f'gitdir: {tmp_path}/other/.git/worktrees/w\n', encoding='utf-8')
    assert not guard.is_real_checkout(other)
    subprocess.run(['git', 'tag', 'scratch'], cwd=other, check=True)
    assert _lines(fake_git) == ['tag scratch']


def test_install_guards_the_main_checkout_of_a_linked_worktree(tmp_path, monkeypatch):
    main, linked = tmp_path / 'main', tmp_path / 'wt'
    (main / '.git' / 'worktrees' / 'wt').mkdir(parents=True)
    linked.mkdir()
    (linked / '.git').write_text(f'gitdir: {main}/.git/worktrees/wt\n', encoding='utf-8')
    monkeypatch.setattr(guard, '_ROOTS', [])
    guard.install(linked)
    assert guard._ROOTS == sorted({linked.resolve(), main.resolve()})
    assert guard.is_real_checkout(main / 'src')


def test_executable_names_the_program(fake_git):
    with refused():
        subprocess.run(['vcs', 'reset', '--hard', 'HEAD'], executable=str(shutil.which('git') or 'git'), cwd=REPO_ROOT)
    assert _lines(fake_git) == []


def test_git_dir_in_the_child_environment_is_a_target(fake_git, tmp_path, monkeypatch):
    with refused():
        subprocess.run(['git', 'reset', '--hard', 'HEAD'], cwd=tmp_path, env={**os.environ, 'GIT_DIR': str(REPO_ROOT / '.git')})
    with refused():
        subprocess.run(['git', 'reset', '--hard', 'HEAD'], cwd=tmp_path, env={**os.environb, b'GIT_DIR': os.fsencode(REPO_ROOT / '.git')})
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
    subprocess.run(['git', 'config', '--get', '--', 'core.guardprobe'], cwd=REPO_ROOT, check=True)
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
        'config --get -- core.guardprobe',
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
