"""Issue #8556: a diverged stable checkout must not claim "Up to date".

A WebUI install on the ``stable`` channel whose local branch sits OFF the
stable line -- HEAD neither contains the latest stable tag nor can fast-forward
to it -- used to hit the third suppression in ``_check_repo_release`` and get
the exact same "up to date" payload (``behind == 0``) as the two states that
really are up to date (HEAD at, past, or containing the latest tag). A
divergence is the opposite: every commit between HEAD and the release is
missing.

These tests build throwaway repositories in ``tmp_path`` (file remotes only, no
network, no fetch) and assert that the check and the apply decision agree:
``_check_repo_release`` defers to the branch comparison, the real commit gap is
reported, and ``_select_apply_compare_ref`` targets the same ref.
"""

import subprocess

import pytest

import api.updates as updates

pytestmark = pytest.mark.usefixtures("isolated_host_git_config")


def _git(repo, *args):
    subprocess.run(
        ['git', *args], cwd=str(repo), check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _init_repo(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    _git(repo, 'init', '-q', '-b', 'master')
    _git(repo, 'config', 'user.email', 't@t.co')
    _git(repo, 'config', 'user.name', 'Test')
    # A file remote that is never contacted: the tests only read local refs.
    _git(repo, 'remote', 'add', 'origin', 'https://example.invalid/hermes-webui.git')
    return repo


def _commit(repo, message):
    tracked = repo / 'APP'
    tracked.write_text(f'{message}\n', encoding='utf-8')
    _git(repo, 'add', 'APP')
    _git(repo, 'commit', '-q', '-m', message)


def _diverged_stable_repo(tmp_path):
    """Linear master with both tag families, plus a local branch off ``v0.52.2``.

    History::

        c1 (exp-v0.52.1) -> c2 (v0.52.2, exp-v0.52.2) -> c3 (exp-v0.52.3)
        -> c4 (exp-v0.52.4) -> c5 (v0.52.5, exp-v0.52.5)

    ``stable-local`` is checked out at ``v0.52.2`` with one local-only commit,
    so it does not contain ``v0.52.5`` and cannot fast-forward to it, while
    ``origin/master`` is 3 commits ahead of it.
    """
    repo = _init_repo(tmp_path)
    plan = [
        ('release c1', ['exp-v0.52.1']),
        ('release c2', ['v0.52.2', 'exp-v0.52.2']),
        ('release c3', ['exp-v0.52.3']),
        ('release c4', ['exp-v0.52.4']),
        ('release c5', ['v0.52.5', 'exp-v0.52.5']),
    ]
    for message, tags in plan:
        _commit(repo, message)
        for tag in tags:
            _git(repo, 'tag', tag)
    master_tip, _ = _run(repo, 'rev-parse', 'HEAD')
    _git(repo, 'update-ref', 'refs/remotes/origin/master', master_tip)

    _git(repo, 'checkout', '-q', '-b', 'stable-local', 'v0.52.2')
    _commit(repo, 'local hotfix not on the stable line')
    _git(repo, 'branch', '--set-upstream-to=origin/master', 'stable-local')
    return repo


def _run(repo, *args):
    out = subprocess.run(
        ['git', *args], cwd=str(repo), check=True,
        capture_output=True, text=True,
    )
    return out.stdout.strip(), out.returncode


def test_diverged_stable_check_defers_to_the_branch_comparison(tmp_path):
    """Head off the stable line must not be reported as up to date."""
    repo = _diverged_stable_repo(tmp_path)

    info = updates._check_repo_release(repo, 'webui', 'stable')

    assert info is None, (
        'a diverged stable checkout must fall through to the branch comparison, '
        f'not return an up-to-date payload: {info!r}'
    )
    branch_info = updates._check_repo_branch(repo, 'webui', fetch=False)
    assert branch_info['behind'] == 3, 'the real gap against origin/master is 3 commits'
    assert branch_info['branch'] == 'origin/master'


def test_diverged_stable_apply_targets_the_same_ref_as_the_check(tmp_path):
    repo = _diverged_stable_repo(tmp_path)

    compare_ref = updates._select_apply_compare_ref(repo, 'stable', 'webui')
    branch_info = updates._check_repo_branch(repo, 'webui', fetch=False)

    assert compare_ref is not None, 'apply must not report "no update" for a divergence'
    assert compare_ref == branch_info['branch'], (
        'the apply side must mirror the check side (#2653/#2846)'
    )


def test_stable_head_on_latest_tag_stays_up_to_date(tmp_path):
    """Control (a): HEAD exactly on the newest stable tag is up to date."""
    repo = _init_repo(tmp_path)
    _commit(repo, 'release c1')
    _git(repo, 'tag', 'v0.52.5')
    _git(repo, 'update-ref', 'refs/remotes/origin/master', 'HEAD')
    _git(repo, 'branch', '--set-upstream-to=origin/master', 'master')

    info = updates._check_repo_release(repo, 'webui', 'stable')

    assert info is not None
    assert info['behind'] == 0
    assert info['release_based'] is True
    assert info['latest_tag'] == 'v0.52.5'
    # Applying the tag HEAD already sits on is a no-op pull; the check reports
    # up-to-date either way.
    assert updates._select_apply_compare_ref(repo, 'stable', 'webui') == 'v0.52.5'


def test_stable_head_containing_latest_tag_stays_up_to_date(tmp_path):
    """Control (b): a positive tag gap where HEAD contains the newest tag.

    The legacy ``v2026.9.x`` tag is nearest to HEAD but ranks below the current
    ``v0.52.x`` series, so the tag-name gap is 1 while HEAD already contains
    ``v0.52.5``. That is condition 2: up to date on the promoted subset.
    """
    repo = _init_repo(tmp_path)
    _commit(repo, 'release v0.52.5')
    _git(repo, 'tag', 'v0.52.5')
    _commit(repo, 'legacy series tag')
    _git(repo, 'tag', 'v2026.9.24')
    _git(repo, 'update-ref', 'refs/remotes/origin/master', 'HEAD')
    _git(repo, 'branch', '--set-upstream-to=origin/master', 'master')

    info = updates._check_repo_release(repo, 'webui', 'stable')

    assert info is not None, 'HEAD contains the latest stable tag: up to date'
    assert info['behind'] == 0
    assert info['latest_tag'] == 'v0.52.5'
    assert updates._select_apply_compare_ref(repo, 'stable', 'webui') is None


def test_experimental_and_agent_keep_the_branch_fallthrough(tmp_path):
    """Control (c): the historical fallthrough is unchanged off stable/webui."""
    repo = _diverged_stable_repo(tmp_path)

    assert updates._check_repo_release(repo, 'webui', 'experimental') is None
    assert updates._select_apply_compare_ref(repo, 'experimental', 'webui') == 'origin/master'

    assert updates._check_repo_release(repo, 'agent', 'stable') is None
    assert updates._select_apply_compare_ref(repo, 'stable', 'agent') == 'origin/master'
