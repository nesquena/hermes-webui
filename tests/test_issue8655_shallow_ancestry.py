"""Regression tests for issue #8556 / #8655.

Shallow/incomplete history must not authorize a stable rewind.
Tests use real git graphs over file:// clones (no fakes for ancestry).
"""

import subprocess
import pytest
import api.updates as updates


def _git(repo, *args):
    res = subprocess.run(
        ['git', *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


@pytest.fixture
def origin_repo(tmp_path):
    """Upstream repo with:
    c1 -> c2 @ v1.0.0 -> c3 @ v1.0.1 -> c4 unpromoted on master
    pushed to bare origin.git.
    """
    upstream = tmp_path / 'upstream'
    upstream.mkdir()
    _git(upstream, 'init', '-q', '-b', 'master')
    _git(upstream, 'config', 'user.email', 't@t.co')
    _git(upstream, 'config', 'user.name', 'Test')

    tracked = upstream / 'tracked.txt'
    tracked.write_text('c1\n', encoding='utf-8')
    _git(upstream, 'add', 'tracked.txt')
    _git(upstream, 'commit', '-q', '-m', 'c1')

    tracked.write_text('c2\n', encoding='utf-8')
    _git(upstream, 'commit', '-q', '-am', 'c2')
    _git(upstream, 'tag', 'v1.0.0')

    tracked.write_text('c3\n', encoding='utf-8')
    _git(upstream, 'commit', '-q', '-am', 'c3')
    _git(upstream, 'tag', 'v1.0.1')

    tracked.write_text('c4 unpromoted\n', encoding='utf-8')
    _git(upstream, 'commit', '-q', '-am', 'c4 unpromoted')
    c4 = _git(upstream, 'rev-parse', 'HEAD')

    origin = tmp_path / 'origin.git'
    subprocess.run(
        ['git', 'init', '--bare', '-q', '--initial-branch=master', str(origin)],
        check=True,
        capture_output=True,
    )
    _git(upstream, 'push', '-q', str(origin), 'master')
    _git(upstream, 'push', '-q', str(origin), '--tags')

    origin_url = f'file://{origin.resolve().as_posix()}'
    return origin_url, c4


def test_shallow_clone_does_not_downgrade(tmp_path, origin_repo, monkeypatch):
    """A --depth 1 clone must not report both divergent and repair_required,
    must never branch-compare on stable WebUI, and force path must not rewind.
    """
    origin_url, c4 = origin_repo
    repo = tmp_path / 'shallow_clone'
    subprocess.run(
        ['git', 'clone', '-q', '--depth', '1', origin_url, str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, 'config', 'user.email', 't@t.co')
    _git(repo, 'config', 'user.name', 'Test')

    # 1. _check_repo(repo, 'webui', 'stable') must not carry both divergent and repair_required
    info = updates._check_repo(repo, 'webui', 'stable')
    assert not (info and info.get('divergent') and info.get('repair_required')), (
        f"Shallow clone must not carry both divergent and repair_required: {info}"
    )

    # 2. _select_apply_compare_ref(repo, 'stable', 'webui') must never be a branch ref
    ref = updates._select_apply_compare_ref(repo, 'stable', 'webui')
    if ref is not None:
        assert not ref.startswith('origin/'), f"Must not be a branch ref: {ref}"
        assert not ref.startswith('refs/heads/'), f"Must not be a branch ref: {ref}"

    # 3. Force path must leave HEAD at the unpromoted commit or return up_to_date
    monkeypatch.setattr(updates, 'REPO_ROOT', repo)
    monkeypatch.setattr(updates, '_schedule_restart', lambda: None)
    head_before = _git(repo, 'rev-parse', 'HEAD')
    assert head_before == c4

    force_res = updates._apply_force_update_inner('webui', 'stable')
    head_after = _git(repo, 'rev-parse', 'HEAD')

    assert head_after == c4, f"HEAD was rewound from {c4} to {head_after}"
    if force_res.get('up_to_date'):
        assert force_res['ok'] is True
    else:
        assert head_after == c4


def test_unknown_ancestry_refuses_force(tmp_path, origin_repo, monkeypatch):
    """Full clone with a local tag pointing to an unrelated orphan commit
    must report ancestry_unknown (without repair_required) and refuse force update.
    """
    origin_url, c4 = origin_repo
    repo = tmp_path / 'full_clone'
    subprocess.run(
        ['git', 'clone', '-q', origin_url, str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, 'config', 'user.email', 't@t.co')
    _git(repo, 'config', 'user.name', 'Test')

    # Create an orphan commit tagged v1.0.2 locally
    _git(repo, 'checkout', '--orphan', 'orphan-branch')
    (repo / 'orphan.txt').write_text('orphan content\n', encoding='utf-8')
    _git(repo, 'add', 'orphan.txt')
    _git(repo, 'commit', '-q', '-m', 'orphan commit')
    _git(repo, 'tag', 'v1.0.2')

    # Return to master (HEAD at c4)
    _git(repo, 'checkout', '-q', 'master')
    head_before = _git(repo, 'rev-parse', 'HEAD')
    assert head_before == c4

    # _check_repo payload carries ancestry_unknown and no repair_required
    info = updates._check_repo(repo, 'webui', 'stable')
    assert info is not None
    assert info.get('ancestry_unknown') is True
    assert not info.get('repair_required')
    assert not info.get('divergent')
    assert info.get('latest_tag') == 'v1.0.2'

    # Force path returns ok is False, refused_rewind is True, ancestry_unknown is True
    monkeypatch.setattr(updates, 'REPO_ROOT', repo)
    monkeypatch.setattr(updates, '_schedule_restart', lambda: None)

    res = updates._apply_force_update_inner('webui', 'stable')
    assert res.get('ok') is False
    assert res.get('refused_rewind') is True
    assert res.get('ancestry_unknown') is True

    head_after = _git(repo, 'rev-parse', 'HEAD')
    assert head_after == head_before


def test_proven_divergence_still_repairs(tmp_path, origin_repo, monkeypatch):
    """Control: local branch off v1.0.0 with one local commit and full history
    must report divergent + repair_required, and force update lands on v1.0.1.
    """
    origin_url, c4 = origin_repo
    repo = tmp_path / 'divergent_clone'
    subprocess.run(
        ['git', 'clone', '-q', origin_url, str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, 'config', 'user.email', 't@t.co')
    _git(repo, 'config', 'user.name', 'Test')

    _git(repo, 'checkout', '-q', '-b', 'diverged-branch', 'v1.0.0')
    (repo / 'local.txt').write_text('local divergence\n', encoding='utf-8')
    _git(repo, 'add', 'local.txt')
    _git(repo, 'commit', '-q', '-m', 'local commit off v1.0.0')

    info = updates._check_repo(repo, 'webui', 'stable')
    assert info is not None
    assert info.get('divergent') is True
    assert info.get('repair_required') is True
    assert info.get('latest_tag') == 'v1.0.1'

    monkeypatch.setattr(updates, 'REPO_ROOT', repo)
    monkeypatch.setattr(updates, '_schedule_restart', lambda: None)

    res = updates._apply_force_update_inner('webui', 'stable')
    assert res.get('ok') is True

    head_after = _git(repo, 'rev-parse', 'HEAD')
    v101_sha = _git(repo, 'rev-parse', 'v1.0.1^{commit}')
    assert head_after == v101_sha


def test_full_history_at_and_past_tag_still_up_to_date(tmp_path, origin_repo):
    """Control: checkout at v1.0.1 and at c4 (past tag) report behind == 0."""
    origin_url, c4 = origin_repo
    repo = tmp_path / 'full_clone'
    subprocess.run(
        ['git', 'clone', '-q', origin_url, str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, 'config', 'user.email', 't@t.co')
    _git(repo, 'config', 'user.name', 'Test')

    # At v1.0.1
    _git(repo, 'checkout', '-q', 'v1.0.1')
    info_at = updates._check_repo(repo, 'webui', 'stable')
    assert info_at is not None
    assert info_at.get('behind') == 0

    # Past v1.0.1 (HEAD at c4 on master)
    _git(repo, 'checkout', '-q', 'master')
    info_past = updates._check_repo(repo, 'webui', 'stable')
    assert info_past is not None
    assert info_past.get('behind') == 0


def test_experimental_channel_still_branch_compares(tmp_path, origin_repo):
    """Control: experimental channel branch compares, no release_based."""
    origin_url, c4 = origin_repo
    repo = tmp_path / 'full_clone'
    subprocess.run(
        ['git', 'clone', '-q', origin_url, str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, 'config', 'user.email', 't@t.co')
    _git(repo, 'config', 'user.name', 'Test')

    _git(repo, 'checkout', '-q', '-b', 'diverged-branch', 'v1.0.0')
    (repo / 'local.txt').write_text('local divergence\n', encoding='utf-8')
    _git(repo, 'add', 'local.txt')
    _git(repo, 'commit', '-q', '-m', 'local commit')
    _git(repo, 'branch', '--set-upstream-to=origin/master', 'diverged-branch')

    info = updates._check_repo(repo, 'webui', 'experimental')
    assert info is not None
    assert info.get('release_based') is None
    assert info.get('branch') == 'origin/master'

    assert updates._select_apply_compare_ref(repo, 'experimental', 'webui') == 'origin/master'
    assert updates._select_apply_compare_ref(repo, 'stable', 'agent') == 'origin/master'


@pytest.fixture
def two_parent_origin(tmp_path):
    """Upstream repo with a two-parent merge:
    B(v1.0.0) -- S(v1.0.1) -- [intermediate] -- M (installed)
     \\________________________ T ______________/
    pushed to bare origin.git.
    """
    upstream = tmp_path / 'upstream'
    upstream.mkdir()
    _git(upstream, 'init', '-q', '-b', 'master')
    _git(upstream, 'config', 'user.email', 't@t.co')
    _git(upstream, 'config', 'user.name', 'Test')

    (upstream / 'f.txt').write_text('B\n', encoding='utf-8')
    _git(upstream, 'add', 'f.txt')
    _git(upstream, 'commit', '-q', '-m', 'B')
    _git(upstream, 'tag', 'v1.0.0')

    _git(upstream, 'checkout', '-q', '-b', 'side')
    (upstream / 'side.txt').write_text('T\n', encoding='utf-8')
    _git(upstream, 'add', 'side.txt')
    _git(upstream, 'commit', '-q', '-m', 'T')

    _git(upstream, 'checkout', '-q', 'master')
    (upstream / 'f.txt').write_text('S\n', encoding='utf-8')
    _git(upstream, 'commit', '-q', '-am', 'S')
    _git(upstream, 'tag', 'v1.0.1')

    # Intermediate commits on master past S so bounded deepening does not expose S
    tree = _git(upstream, 'write-tree')
    parent = _git(upstream, 'rev-parse', 'HEAD')
    for i in range(280):
        res = subprocess.run(
            ['git', 'commit-tree', tree, '-p', parent, '-m', f'c{i}'],
            cwd=str(upstream),
            capture_output=True,
            text=True,
            check=True,
        )
        parent = res.stdout.strip()
    _git(upstream, 'update-ref', 'refs/heads/master', parent)

    _git(upstream, 'merge', '-q', '--no-ff', '-m', 'M', 'side')
    m_sha = _git(upstream, 'rev-parse', 'HEAD')

    origin = tmp_path / 'origin.git'
    subprocess.run(
        ['git', 'init', '--bare', '-q', '--initial-branch=master', str(origin)],
        check=True,
        capture_output=True,
    )
    _git(upstream, 'push', '-q', str(origin), 'master')
    _git(upstream, 'push', '-q', str(origin), '--tags')

    origin_url = f'file://{origin.resolve().as_posix()}'
    return origin_url, m_sha


def test_two_parent_shallow_merge_does_not_rewind(tmp_path, two_parent_origin, monkeypatch):
    """Review P1 regression:
    Clone --depth 3 of the two-parent origin, fetch tags.
    Merge base HEAD v1.0.1 resolves to B through side branch T, but
    ancestry_relation must not be ANCESTRY_DIVERGENT. Payload carries
    ancestry_unknown (no repair_required), and force update refuses rewind.
    """
    origin_url, m_sha = two_parent_origin
    repo = tmp_path / 'shallow_clone'
    subprocess.run(
        ['git', 'clone', '-q', '--depth', '3', origin_url, str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, 'config', 'user.email', 't@t.co')
    _git(repo, 'config', 'user.name', 'Test')
    _git(repo, 'fetch', '-q', '--tags')

    assert updates._repository_is_shallow(repo) is True
    assert updates._head_contains_ref(repo, 'v1.0.1') is False
    assert updates._can_fast_forward_to(repo, 'v1.0.1') is False
    assert updates._has_reachable_merge_base(repo, 'v1.0.1') is True

    relation = updates._ancestry_relation(repo, 'v1.0.1')
    assert relation is not updates.ANCESTRY_DIVERGENT
    assert relation == updates.ANCESTRY_UNKNOWN

    info = updates._check_repo(repo, 'webui', 'stable')
    assert info is not None
    assert info.get('ancestry_unknown') is True
    assert not info.get('repair_required')
    assert not info.get('divergent')

    monkeypatch.setattr(updates, 'REPO_ROOT', repo)
    monkeypatch.setattr(updates, '_schedule_restart', lambda: None)
    head_before = _git(repo, 'rev-parse', 'HEAD')
    assert head_before == m_sha

    force_res = updates._apply_force_update_inner('webui', 'stable')
    assert force_res.get('ok') is False
    assert force_res.get('refused_rewind') is True
    assert force_res.get('ancestry_unknown') is True

    head_after = _git(repo, 'rev-parse', 'HEAD')
    assert head_after == head_before


def test_incomplete_after_bounded_deepening_is_unknown(tmp_path, two_parent_origin, monkeypatch):
    """Bounded deepening case:
    Promoted tag v1.0.1 is more than 64 commits behind M on the main path,
    so a single fetch --deepen=64 cannot expose it. The repository remains
    shallow after _ancestry_relation ran, relation is ANCESTRY_UNKNOWN (not DIVERGENT),
    and force update refuses with HEAD preserved.
    """
    origin_url, m_sha = two_parent_origin
    repo = tmp_path / 'shallow_clone'
    subprocess.run(
        ['git', 'clone', '-q', '--depth', '3', origin_url, str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, 'config', 'user.email', 't@t.co')
    _git(repo, 'config', 'user.name', 'Test')
    _git(repo, 'fetch', '-q', '--tags')

    relation = updates._ancestry_relation(repo, 'v1.0.1')
    assert updates._repository_is_shallow(repo) is True
    assert relation == updates.ANCESTRY_UNKNOWN
    assert relation is not updates.ANCESTRY_DIVERGENT

    monkeypatch.setattr(updates, 'REPO_ROOT', repo)
    monkeypatch.setattr(updates, '_schedule_restart', lambda: None)
    head_before = _git(repo, 'rev-parse', 'HEAD')
    assert head_before == m_sha

    force_res = updates._apply_force_update_inner('webui', 'stable')
    assert force_res.get('ok') is False
    assert force_res.get('refused_rewind') is True
    assert force_res.get('ancestry_unknown') is True

    head_after = _git(repo, 'rev-parse', 'HEAD')
    assert head_after == head_before


def test_two_parent_full_history_is_ancestor(tmp_path, two_parent_origin, monkeypatch):
    """Control: full clone of the same two-parent origin.
    History is complete, so v1.0.1 is proven an ancestor of HEAD (ANCESTRY_ANCESTOR),
    force refuses rewind, and HEAD is preserved.
    """
    origin_url, m_sha = two_parent_origin
    repo = tmp_path / 'full_clone'
    subprocess.run(
        ['git', 'clone', '-q', origin_url, str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, 'config', 'user.email', 't@t.co')
    _git(repo, 'config', 'user.name', 'Test')

    relation = updates._ancestry_relation(repo, 'v1.0.1')
    assert relation == updates.ANCESTRY_ANCESTOR

    monkeypatch.setattr(updates, 'REPO_ROOT', repo)
    monkeypatch.setattr(updates, '_schedule_restart', lambda: None)
    head_before = _git(repo, 'rev-parse', 'HEAD')
    assert head_before == m_sha

    force_res = updates._apply_force_update_inner('webui', 'stable')
    assert force_res.get('up_to_date') or force_res.get('refused_rewind')
    head_after = _git(repo, 'rev-parse', 'HEAD')
    assert head_after == head_before


def test_unreadable_shallow_status_refuses_force(tmp_path, two_parent_origin, monkeypatch):
    """Failure-path regression: unreadable shallow query must not declare divergence.

    A TimeoutExpired raised for rev-parse --is-shallow-repository must collapse to
    HISTORY_UNKNOWN, ANCESTRY_UNKNOWN (not ANCESTRY_DIVERGENT), report ancestry_unknown
    without repair_required, refuse force update without rewinding HEAD, and schedule
    no restart.
    """
    origin_url, m_sha = two_parent_origin
    repo = tmp_path / 'shallow_clone'
    subprocess.run(
        ['git', 'clone', '-q', '--depth', '3', origin_url, str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, 'config', 'user.email', 't@t.co')
    _git(repo, 'config', 'user.name', 'Test')
    _git(repo, 'fetch', '-q', '--tags')

    orig_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if any('--is-shallow-repository' in str(arg) for arg in cmd):
            raise subprocess.TimeoutExpired(cmd, timeout=kwargs.get('timeout', 10))
        return orig_run(cmd, *args, **kwargs)

    monkeypatch.setattr(updates.subprocess, 'run', fake_run)

    assert updates._history_completeness(repo) == updates.HISTORY_UNKNOWN

    relation = updates._ancestry_relation(repo, 'v1.0.1')
    assert relation is not updates.ANCESTRY_DIVERGENT
    assert relation == updates.ANCESTRY_UNKNOWN

    info = updates._check_repo(repo, 'webui', 'stable')
    assert info is not None
    assert info.get('ancestry_unknown') is True
    assert not info.get('repair_required')
    assert not info.get('divergent')

    restart_calls = []
    monkeypatch.setattr(updates, 'REPO_ROOT', repo)
    monkeypatch.setattr(updates, '_schedule_restart', lambda: restart_calls.append(1))

    head_before = _git(repo, 'rev-parse', 'HEAD')
    assert head_before == m_sha

    force_res = updates._apply_force_update_inner('webui', 'stable')
    assert force_res.get('ok') is False
    assert force_res.get('refused_rewind') is True
    assert force_res.get('ancestry_unknown') is True

    head_after = _git(repo, 'rev-parse', 'HEAD')
    assert head_after == head_before
    assert restart_calls == []


def test_unrecognized_shallow_status_is_unknown(tmp_path, two_parent_origin, monkeypatch):
    """Control for value half: non-boolean output must not prove complete history.

    A 0 exit with unrecognized output (e.g. 'banana') must map to HISTORY_UNKNOWN,
    ANCESTRY_UNKNOWN (not ANCESTRY_DIVERGENT), report ancestry_unknown without
    repair_required, and refuse force update with HEAD preserved.
    """
    origin_url, m_sha = two_parent_origin
    repo = tmp_path / 'shallow_clone'
    subprocess.run(
        ['git', 'clone', '-q', '--depth', '3', origin_url, str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, 'config', 'user.email', 't@t.co')
    _git(repo, 'config', 'user.name', 'Test')
    _git(repo, 'fetch', '-q', '--tags')

    orig_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if any('--is-shallow-repository' in str(arg) for arg in cmd):
            return subprocess.CompletedProcess(cmd, 0, stdout='banana\n', stderr='')
        return orig_run(cmd, *args, **kwargs)

    monkeypatch.setattr(updates.subprocess, 'run', fake_run)

    assert updates._history_completeness(repo) == updates.HISTORY_UNKNOWN

    relation = updates._ancestry_relation(repo, 'v1.0.1')
    assert relation is not updates.ANCESTRY_DIVERGENT
    assert relation == updates.ANCESTRY_UNKNOWN

    info = updates._check_repo(repo, 'webui', 'stable')
    assert info is not None
    assert info.get('ancestry_unknown') is True
    assert not info.get('repair_required')
    assert not info.get('divergent')

    restart_calls = []
    monkeypatch.setattr(updates, 'REPO_ROOT', repo)
    monkeypatch.setattr(updates, '_schedule_restart', lambda: restart_calls.append(1))

    head_before = _git(repo, 'rev-parse', 'HEAD')
    assert head_before == m_sha

    force_res = updates._apply_force_update_inner('webui', 'stable')
    assert force_res.get('ok') is False
    assert force_res.get('refused_rewind') is True
    assert force_res.get('ancestry_unknown') is True

    head_after = _git(repo, 'rev-parse', 'HEAD')
    assert head_after == head_before
    assert restart_calls == []


