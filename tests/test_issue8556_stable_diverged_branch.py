"""Issue #8556: a diverged stable checkout is reported against the stable channel.

A WebUI install on the ``stable`` channel whose local branch sits OFF the
stable line -- HEAD neither contains the latest stable tag nor can fast-forward
to it -- used to hit the third suppression in ``_check_repo_release`` and get
the exact same "up to date" payload (``behind == 0``) as the two states that
really are up to date (HEAD at, past, or containing the latest tag). A
divergence is the opposite: every commit between HEAD and the release is
missing.

The first repair made that case fall through to the branch comparison, which
reported the real gap against ``origin/master`` -- but it also re-pointed the
apply side at the branch upstream, so a *force* update on the stable channel
reset the checkout onto the experimental tip (review 2026-11-28T04:26:04Z).

The contract these tests pin:

* the check side reports the release-based payload: ``release_based: True``,
  ``latest_sha``/``latest_tag``/``branch`` = the latest stable tag, and a
  positive ``behind`` (release gap);
* the apply side targets that same stable tag -- the ``None`` path stays
  reserved for the two genuinely up-to-date states;
* a real force update of a stable install resets to the stable tag and never to
  an experimental-only tip on ``origin/master``;
* the on/past/contains-tag behaviour, the experimental channel and the AGENT
  repo keep their historical branch fallthrough;
* the decisions hold for a diverged branch with its own upstream and for a
  shallow tag checkout without ``origin/master``.

These tests build throwaway repositories in ``tmp_path`` (file remotes only, no
network) and use production code for ref selection, ancestry checks and the
reset.
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


def _init_repo(tmp_path, name='repo'):
    repo = tmp_path / name
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


def _run(repo, *args):
    out = subprocess.run(
        ['git', *args], cwd=str(repo), check=True,
        capture_output=True, text=True,
    )
    return out.stdout.strip(), out.returncode


def _rev(repo, ref):
    sha, _ = _run(repo, 'rev-parse', f'{ref}^{{commit}}')
    return sha


def _has_ref(repo, ref):
    proc = subprocess.run(
        ['git', 'rev-parse', '--verify', ref], cwd=str(repo),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0


def _linear_master_with_both_tag_families(repo):
    """Build c1 (exp-v0.52.1) -> c2 (v0.52.2, exp-v0.52.2) -> c3 (exp-v0.52.3)

    -> c4 (exp-v0.52.4) -> c5 (v0.52.5, exp-v0.52.5) and return master's tip.
    """
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
    return _rev(repo, 'HEAD')


def _diverged_stable_repo(tmp_path):
    """Linear master with both tag families, plus a local branch off ``v0.52.2``.

    ``stable-local`` is checked out at ``v0.52.2`` with one local-only commit,
    so it does not contain ``v0.52.5`` and cannot fast-forward to it, while
    ``origin/master`` is 3 commits ahead of it.
    """
    repo = _init_repo(tmp_path)
    master_tip = _linear_master_with_both_tag_families(repo)
    _git(repo, 'update-ref', 'refs/remotes/origin/master', master_tip)

    _git(repo, 'checkout', '-q', '-b', 'stable-local', 'v0.52.2')
    _commit(repo, 'local hotfix not on the stable line')
    _git(repo, 'branch', '--set-upstream-to=origin/master', 'stable-local')
    return repo


def _diverged_stable_repo_with_experimental_tip(tmp_path):
    """Same divergence, plus an experimental-only release on ``origin/master``.

    This is the review schedule: the stable channel must not carry the install
    onto ``exp-v0.52.6``.
    """
    repo = _diverged_stable_repo(tmp_path)
    _git(repo, 'checkout', '-q', 'master')
    _commit(repo, 'release c6 (experimental only)')
    _git(repo, 'tag', 'exp-v0.52.6')
    exp_tip = _rev(repo, 'HEAD')
    _git(repo, 'update-ref', 'refs/remotes/origin/master', exp_tip)
    _git(repo, 'checkout', '-q', 'stable-local')
    return repo, exp_tip


def _install_force_update_doubles(monkeypatch, repo, restarts):
    """Replace only the network fetch and the restart scheduling."""
    real_run_git = updates._run_git

    def _run_git_no_fetch(args, cwd, timeout=10):
        if args and args[0] == 'fetch':
            return '', True
        return real_run_git(args, cwd, timeout=timeout)

    monkeypatch.setattr(updates, '_run_git', _run_git_no_fetch)
    monkeypatch.setattr(updates, 'REPO_ROOT', repo)
    monkeypatch.setattr(updates, '_schedule_restart', lambda *a, **k: restarts.append(1))


def test_diverged_stable_check_reports_the_release_payload(tmp_path):
    """Head off the stable line reports the release, not the branch firehose."""
    repo = _diverged_stable_repo(tmp_path)

    info = updates._check_repo_release(repo, 'webui', 'stable')

    assert info is not None, (
        'a diverged stable checkout must be reported, not suppressed: '
        f'{info!r}'
    )
    assert info['release_based'] is True, (
        'the stable channel must stay release-based on a divergence: '
        f'{info!r}'
    )
    assert info['latest_sha'] == 'v0.52.5', (
        f'the advertised release must be the latest stable tag: {info!r}'
    )
    assert info['latest_tag'] == 'v0.52.5'
    assert info['branch'] == 'v0.52.5', (
        'the payload must not point the install at origin/master: '
        f'{info!r}'
    )
    assert info['current_sha'] == 'v0.52.2', (
        f'the installed release is the nearest stable tag behind HEAD: {info!r}'
    )
    assert info['behind'] == 1, (
        f'one release separates v0.52.2 from the stable tip v0.52.5: {info!r}'
    )
    assert info['channel'] == 'stable'


def test_diverged_stable_apply_targets_the_stable_tag(tmp_path):
    repo = _diverged_stable_repo(tmp_path)

    compare_ref = updates._select_apply_compare_ref(repo, 'stable', 'webui')

    assert compare_ref == 'v0.52.5', (
        'apply must target the release the check advertises, not the branch '
        f'upstream: {compare_ref!r}'
    )


def test_force_update_on_a_diverged_stable_checkout_stays_on_the_stable_tag(tmp_path, monkeypatch):
    """The review schedule: a real force update must not reach exp-v0.52.6."""
    repo, exp_tip = _diverged_stable_repo_with_experimental_tip(tmp_path)
    restarts = []
    _install_force_update_doubles(monkeypatch, repo, restarts)

    result = updates._apply_force_update_inner('webui', 'stable')

    assert result.get('ok') is True, f'force update must succeed: {result!r}'
    stable_sha = _rev(repo, 'v0.52.5')
    head_sha = _rev(repo, 'HEAD')
    assert head_sha == stable_sha, (
        'the checkout must end on the latest stable tag; '
        f'HEAD={head_sha} stable={stable_sha} exp={exp_tip}'
    )
    assert head_sha != exp_tip, (
        'the stable channel must never advance the install to the '
        'experimental-only tip'
    )
    assert 'v0.52.5' in result.get('message', ''), (
        f'the response must name the ref it applied: {result!r}'
    )
    assert len(restarts) == 1, f'the restart is scheduled exactly once: {restarts!r}'


def test_diverged_branch_with_its_own_upstream_targets_the_stable_tag(tmp_path):
    """A local branch tracking its own remote branch is still a stable install."""
    repo = _diverged_stable_repo(tmp_path)
    _git(repo, 'update-ref', 'refs/remotes/origin/stable-local', 'v0.52.2')
    _git(repo, 'branch', '--set-upstream-to=origin/stable-local', 'stable-local')

    info = updates._check_repo_release(repo, 'webui', 'stable')
    compare_ref = updates._select_apply_compare_ref(repo, 'stable', 'webui')

    assert info is not None and info['release_based'] is True, (
        f'the release payload must not depend on the tracked branch: {info!r}'
    )
    assert info['latest_sha'] == 'v0.52.5'
    assert compare_ref == 'v0.52.5', (
        'the tracked branch must not replace the release target: '
        f'{compare_ref!r}'
    )


def test_shallow_tag_checkout_without_origin_master_targets_the_stable_tag(tmp_path):
    """A shallow tag checkout has no origin/master to fall back to."""
    source = _init_repo(tmp_path, name='source')
    _linear_master_with_both_tag_families(source)

    clone = tmp_path / 'clone'
    # --no-local forces the transport so --depth applies to a filesystem
    # source as well; a single-branch tag clone has no origin/master.
    subprocess.run(
        ['git', 'clone', '-q', '--no-local', '--depth', '1',
         '--branch', 'v0.52.2', str(source), str(clone)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _git(clone, 'config', 'user.email', 't@t.co')
    _git(clone, 'config', 'user.name', 'Test')
    _commit(clone, 'local hotfix on the tag checkout')
    # Bring only the newer stable tag, keeping the clone shallow.
    _git(clone, 'fetch', '-q', '--depth', '1', 'origin', 'tag', 'v0.52.5')

    assert not _has_ref(clone, 'refs/remotes/origin/master'), (
        'this arm must not have an origin/master to fall back to'
    )

    info = updates._check_repo_release(clone, 'webui', 'stable')
    compare_ref = updates._select_apply_compare_ref(clone, 'stable', 'webui')

    assert info is not None and info['release_based'] is True, (
        f'a shallow tag checkout still reports the release payload: {info!r}'
    )
    assert info['latest_sha'] == 'v0.52.5'
    assert compare_ref == 'v0.52.5', (
        f'the apply side must target the release tag: {compare_ref!r}'
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
