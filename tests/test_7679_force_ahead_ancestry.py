"""Regression tests for the #7679 force-update backend findings.

Three properties are pinned here, each with a real disposable git repository
(no mocked git calls), so a regression in the ancestry handling fails for the
right reason:

  1. A checkout that is AHEAD of the channel's upstream branch (the user made
     local commits on top of it) is a valid dirty-at-or-past-latest install.
     The force-clean affordance is offered for it, so the cleanup must
     succeed: the tracked local edits and untracked files are discarded while
     the user's own commits survive. Before this fix the reset targeted the
     upstream ref and the rewind guard refused, so the offered action ALWAYS
     failed (Greptile P1, 10-14).

  2. A checkout that is ahead of a channel RELEASE TAG is the switch-back case:
     resetting onto that tag would downgrade the code, so the force path must
     still refuse with ``refused_rewind`` (Codex CORE #3).

  3. When the ancestry between HEAD and the selected ref cannot be
     established (git fails), the force path must FAIL CLOSED. The legacy
     boolean helpers mapped "probe failed" and "not an ancestor" onto the same
     False, so a git failure read as "diverged" and reached ``git clean -fd``
     with the dirty-only gate never consulted \u2014 deleting untracked work that
     no check had reported as dirty (Greptile P1, 10-06).
"""

from pathlib import Path
import subprocess

import pytest

from api import updates


def _git(repo, *args):
    subprocess.run(
        ['git', *args],
        cwd=str(repo),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _write(repo: Path, name: str, content: str):
    (repo / name).write_text(content, encoding='utf-8')


def _remote_repo(tmp_path):
    origin = tmp_path / 'origin.git'
    origin.mkdir()
    _git(origin, 'init', '--bare', '-q', str(origin))
    return origin


def _branch_repo(tmp_path):
    """A repo tracking origin/main with NO release tags at all.

    With no tags the apply side falls through to the branch comparison, so the
    selected ref is the upstream branch \u2014 the shape the "local commits ahead
    of upstream" finding is about.
    """
    repo = tmp_path / 'repo'
    repo.mkdir()
    _git(repo, 'init', '-q', '-b', 'main')
    _git(repo, 'config', 'user.email', 't@t.co')
    _git(repo, 'config', 'user.name', 'Test')
    _write(repo, 'tracked.txt', 'base\n')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-q', '-m', 'base')
    origin = _remote_repo(tmp_path)
    _git(repo, 'remote', 'add', 'origin', str(origin))
    _git(repo, 'push', '-q', '-u', 'origin', 'main')
    return repo


@pytest.fixture
def branch_repo(tmp_path):
    return _branch_repo(tmp_path)


def _prepare_force(monkeypatch, repo):
    """Point the force path at *repo* and neutralise fetch/restart side effects."""
    monkeypatch.setattr(updates, 'REPO_ROOT', repo)
    monkeypatch.setattr(updates, '_AGENT_DIR', repo)
    monkeypatch.setattr(
        updates, '_restart_blocker_snapshot',
        lambda: {'restart_blocked': False, 'active_streams': 0, 'active_runs': 0},
    )
    real_run_git = updates._run_git

    def no_fetch(args, cwd, timeout=10):
        if args[:2] == ['fetch', 'origin']:
            return '', True
        return real_run_git(args, cwd, timeout=timeout)

    monkeypatch.setattr(updates, '_run_git', no_fetch)
    monkeypatch.setattr(updates, '_ensure_gateway_restart_for_agent_update',
                        lambda: (True, {'restarted': True}))
    monkeypatch.setitem(updates._update_cache, 'checked_at', 0)


# \u2500\u2500 Ahead of the upstream branch: clean against HEAD, keep local commits \u2500\u2500


def test_ahead_of_upstream_branch_cleans_local_edits_and_keeps_local_commits(
    branch_repo, monkeypatch
):
    """HEAD is ahead of origin/main (a local commit) and the tree is dirty.

    The offered cleanup must succeed and must NOT drop the local commit.
    """
    _prepare_force(monkeypatch, branch_repo)
    # One local commit on top of the pushed upstream state.
    _write(branch_repo, 'local.txt', 'local work\n')
    _git(branch_repo, 'add', '-A')
    _git(branch_repo, 'commit', '-q', '-m', 'local commit')
    local_sha, _ = updates._run_git(['rev-parse', 'HEAD'], branch_repo)
    # A tracked edit on top of that commit, plus an untracked file.
    _write(branch_repo, 'tracked.txt', 'edited\n')
    _write(branch_repo, 'untracked.txt', 'scratch\n')

    result = updates.apply_force_update('webui', channel='experimental')

    assert result.get('ok') is True, result
    assert result.get('refused_rewind') is not True, result
    # The tracked edit is gone and the untracked file was cleaned ...
    assert (branch_repo / 'tracked.txt').read_text(encoding='utf-8') == 'base\n'
    assert not (branch_repo / 'untracked.txt').exists()
    # ... but the user's own commit survives: HEAD did not move.
    head_after, _ = updates._run_git(['rev-parse', 'HEAD'], branch_repo)
    assert head_after == local_sha


def test_ahead_of_upstream_branch_refuses_when_nothing_is_dirty(
    branch_repo, monkeypatch
):
    """No dirty signal \u2192 the force path must not run ``git clean -fd``."""
    _prepare_force(monkeypatch, branch_repo)
    _write(branch_repo, 'local.txt', 'local work\n')
    _git(branch_repo, 'add', '-A')
    _git(branch_repo, 'commit', '-q', '-m', 'local commit')
    local_sha, _ = updates._run_git(['rev-parse', 'HEAD'], branch_repo)
    _write(branch_repo, 'untracked.txt', 'scratch\n')

    result = updates.apply_force_update('webui', channel='experimental')

    assert result.get('ok') is True, result
    assert result.get('up_to_date') is True, result
    # The untracked file was never part of a dirty probe, so it survives.
    assert (branch_repo / 'untracked.txt').exists()
    head_after, _ = updates._run_git(['rev-parse', 'HEAD'], branch_repo)
    assert head_after == local_sha


# \u2500\u2500 Ahead of a release tag: the rewind guard still refuses \u2500\u2500


def test_ahead_of_a_release_tag_still_refuses_to_rewind(tmp_path, monkeypatch):
    """Switching back to a slower channel must not downgrade the checkout."""
    repo = tmp_path / 'repo'
    repo.mkdir()
    _git(repo, 'init', '-q', '-b', 'main')
    _git(repo, 'config', 'user.email', 't@t.co')
    _git(repo, 'config', 'user.name', 'Test')
    _write(repo, 'tracked.txt', 'base\n')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-q', '-m', 'base')
    _git(repo, 'tag', 'v0.52.2')
    # HEAD moves past the stable release tag.
    _write(repo, 'later.txt', 'later\n')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-q', '-m', 'past the stable tag')
    head_sha, _ = updates._run_git(['rev-parse', 'HEAD'], repo)

    _prepare_force(monkeypatch, repo)
    monkeypatch.setattr(
        updates, '_select_apply_compare_ref',
        lambda path, channel='stable', target=None: 'v0.52.2',
    )
    _write(repo, 'tracked.txt', 'edited\n')

    result = updates.apply_force_update('webui', channel='stable')

    assert result.get('refused_rewind') is True, result
    assert result.get('ok') is False, result
    # HEAD must not have moved (no rewind actually happened).
    head_after, _ = updates._run_git(['rev-parse', 'HEAD'], repo)
    assert head_after == head_sha


# \u2500\u2500 Unknown ancestry: fail closed \u2500\u2500


def test_unknown_ancestry_fails_closed_instead_of_cleaning(
    branch_repo, monkeypatch
):
    """A failed ancestry probe must not reach ``git clean -fd``."""
    _prepare_force(monkeypatch, branch_repo)
    _write(branch_repo, 'tracked.txt', 'edited\n')
    _write(branch_repo, 'untracked.txt', 'scratch\n')
    real_run_git = updates._run_git

    def fail_ancestry(args, cwd, timeout=10):
        # A real git failure (not "not an ancestor"): stderr text, so the
        # tri-state verdict can tell it apart from a legitimate exit-1.
        if args[:2] == ['merge-base', '--is-ancestor']:
            return 'fatal: bad object', False
        return real_run_git(args, cwd, timeout=timeout)

    monkeypatch.setattr(updates, '_run_git', fail_ancestry)

    result = updates.apply_force_update('webui', channel='experimental')

    assert result.get('ok') is False, result
    assert 'refus' in str(result.get('message', '')).lower(), result
    # The untracked file was never reported dirty, so it must still be there.
    assert (branch_repo / 'untracked.txt').exists()
    # The tracked edit was not discarded either.
    assert (branch_repo / 'tracked.txt').read_text(encoding='utf-8') == 'edited\n'


def test_fast_forward_probe_failure_fails_closed(branch_repo, monkeypatch):
    """A failed FAST-FORWARD probe must fail closed even when the
    contains-probe succeeded.

    ``_head_contains_ref`` and ``_can_fast_forward_to`` are two independent
    probes. If only the second one fails, the "is this ref a descendant of
    HEAD" question is unanswered. Reading that as "no" would make the
    force path treat a same-commit checkout as "HEAD is ahead of the ref" and
    clean against HEAD without ever consulting the dirty gate (Greptile P1,
    10-06).
    """
    _prepare_force(monkeypatch, branch_repo)
    _write(branch_repo, 'tracked.txt', 'edited\n')
    _write(branch_repo, 'untracked.txt', 'scratch\n')
    real_run_git = updates._run_git

    def fail_fast_forward(args, cwd, timeout=10):
        # The fast-forward probe is `merge-base --is-ancestor HEAD <ref>`;
        # the contains-probe is `<ref> HEAD`. Fail only the former.
        if args[:2] == ['merge-base', '--is-ancestor'] and args[2] == 'HEAD':
            return 'fatal: bad object', False
        return real_run_git(args, cwd, timeout=timeout)

    monkeypatch.setattr(updates, '_run_git', fail_fast_forward)

    result = updates.apply_force_update('webui', channel='experimental')

    assert result.get('ok') is False, result
    assert 'refus' in str(result.get('message', '')).lower(), result
    # Nothing was discarded.
    assert (branch_repo / 'untracked.txt').exists()
    assert (branch_repo / 'tracked.txt').read_text(encoding='utf-8') == 'edited\n'


def test_ancestry_seam_reports_unknown_when_the_probe_fails(
    branch_repo, monkeypatch
):
    """The tri-state seam itself: a git failure is 'unknown', not False.

    ``_head_contains_ref`` / ``_can_fast_forward_to`` answer True (yes),
    False (no) or None (the probe itself failed). Collapsing the third case
    onto False is what made a failed probe read as "diverged" and reach
    ``git clean -fd`` with the dirty gate never consulted (Greptile P1,
    10-06).
    """
    real_run_git = updates._run_git

    def fail_ancestry(args, cwd, timeout=10):
        if args[:2] == ['merge-base', '--is-ancestor']:
            return 'fatal: bad object', False
        return real_run_git(args, cwd, timeout=timeout)

    monkeypatch.setattr(updates, '_run_git', fail_ancestry)
    assert updates._head_contains_ref(branch_repo, 'HEAD') is None
    assert updates._can_fast_forward_to(branch_repo, 'HEAD') is None

    # A legitimate "no" (exit 1, empty output) is NOT a failure.
    def not_ancestor(args, cwd, timeout=10):
        if args[:2] == ['merge-base', '--is-ancestor']:
            return '', False
        return real_run_git(args, cwd, timeout=timeout)

    monkeypatch.setattr(updates, '_run_git', not_ancestor)
    assert updates._head_contains_ref(branch_repo, 'HEAD') is False
    assert updates._can_fast_forward_to(branch_repo, 'HEAD') is False
