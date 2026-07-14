"""End-to-end tests for update channels (stable vs experimental).

These use REAL git repositories in tmp_path (not mocked _run_git) so they
exercise the actual git commands the channel logic depends on — the tag globs,
`describe --match`, and the ancestor/descendant merge-base checks. This is the
durable regression proof for the two-tag-channel design:

  stable       -> 'v*'      promoted, soaked releases (default)
  experimental -> 'exp-v*'  every batch (opt-in testers)

Both tag families live on the SAME linear master line; a channel is only a tag
glob. The critical property under test: a STABLE user with an unpromoted
`exp-v*` tag ahead of them reports up-to-date, NOT the master firehose.
"""
import subprocess

import pytest

import api.updates as updates


def _git(repo, *args):
    subprocess.run(
        ['git', *args], cwd=str(repo), check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def channel_repo(tmp_path):
    """A linear repo: 5 batch commits, each tagged exp-v0.52.N; stable promoted
    at batch 2 (v0.52.2) and batch 5 (v0.52.5). HEAD ends on batch 5."""
    repo = tmp_path / 'repo'
    repo.mkdir()
    _git(repo, 'init', '-q')
    _git(repo, 'config', 'user.email', 't@t.co')
    _git(repo, 'config', 'user.name', 'Test')
    _git(repo, 'remote', 'add', 'origin', 'https://github.com/nesquena/hermes-webui.git')
    for i in range(1, 6):
        _git(repo, 'commit', '-q', '--allow-empty', '-m', f'batch{i}')
        _git(repo, 'tag', f'exp-v0.52.{i}')
    # Promote batch 2 and batch 5 to stable (same commit, same version number).
    _git(repo, 'tag', 'v0.52.2', 'HEAD~3')
    _git(repo, 'tag', 'v0.52.5', 'HEAD')
    return repo


# ── Tag-glob separation ──────────────────────────────────────────────────────

def test_stable_glob_excludes_exp_tags(channel_repo):
    tags = updates._release_tags(channel_repo, 'stable')
    assert tags == ['v0.52.5', 'v0.52.2']
    assert all(not t.startswith('exp-') for t in tags)


def test_experimental_glob_lists_only_exp_tags(channel_repo):
    tags = updates._release_tags(channel_repo, 'experimental')
    assert tags == [f'exp-v0.52.{i}' for i in (5, 4, 3, 2, 1)]


def test_default_channel_is_stable(channel_repo):
    # No explicit channel == stable glob.
    assert updates._release_tags(channel_repo) == updates._release_tags(channel_repo, 'stable')


def test_unknown_channel_falls_back_to_stable(channel_repo):
    assert updates._normalize_channel('bogus') == 'stable'
    assert updates._normalize_channel(None) == 'stable'
    assert updates._release_tags(channel_repo, 'bogus') == updates._release_tags(channel_repo, 'stable')


# ── current_release_tag is channel-scoped (Codex CORE #1) ────────────────────

def test_current_release_tag_matches_channel(channel_repo):
    # HEAD is tagged BOTH v0.52.5 and exp-v0.52.5. describe must pick the
    # channel-matched tag, not whichever git prefers lexically.
    assert updates._current_release_tag(channel_repo, 'stable') == 'v0.52.5'
    assert updates._current_release_tag(channel_repo, 'experimental') == 'exp-v0.52.5'


# ── The core property: stable never sees the firehose (Codex CORE #2) ────────

def test_stable_user_behind_a_promoted_tag_sees_the_update(channel_repo):
    """Stable user sitting on v0.52.2 must be offered v0.52.5 (a real gap)."""
    _git(channel_repo, 'checkout', '-q', 'v0.52.2')
    info = updates._check_repo_release(channel_repo, 'webui', 'stable')
    assert info is not None
    assert info['behind'] == 1
    assert info['current_version'] == 'v0.52.2'
    assert info['latest_version'] == 'v0.52.5'
    assert updates._select_apply_compare_ref(channel_repo, 'stable', 'webui') == 'v0.52.5'


def test_stable_user_on_latest_stable_with_unpromoted_exp_ahead_is_up_to_date(channel_repo):
    """THE key guarantee: a stable webui user on v0.52.5 with an unpromoted
    exp-v0.52.6 ahead of them reports UP-TO-DATE, never the master firehose."""
    _git(channel_repo, 'commit', '-q', '--allow-empty', '-m', 'batch6')
    _git(channel_repo, 'tag', 'exp-v0.52.6')  # experimental-only, NOT promoted
    _git(channel_repo, 'checkout', '-q', 'v0.52.5')

    info = updates._check_repo_release(channel_repo, 'webui', 'stable')
    assert info is not None
    assert info['behind'] == 0, 'stable must be up-to-date, not offered the firehose'
    # Apply side agrees: it resolves to the current stable tag (a no-op ff),
    # NEVER to origin/master. Both "current tag" and None mean up-to-date; the
    # firehose ref (origin/*) must never appear.
    apply_ref = updates._select_apply_compare_ref(channel_repo, 'stable', 'webui')
    assert apply_ref in ('v0.52.5', None)
    assert apply_ref is None or not apply_ref.startswith('origin/')


def test_stable_user_PAST_latest_stable_on_untagged_commit_is_up_to_date(channel_repo):
    """A stable webui user whose HEAD moved PAST the latest stable tag onto an
    unpromoted (untagged-on-stable) commit must report up-to-date, NOT fall
    through to origin/master. This is the exact firehose-suppression path."""
    _git(channel_repo, 'commit', '-q', '--allow-empty', '-m', 'past-stable')
    _git(channel_repo, 'tag', 'exp-v0.52.6')  # exp-only; no stable tag here
    # HEAD is now one commit PAST v0.52.5 with no stable tag on it.
    info = updates._check_repo_release(channel_repo, 'webui', 'stable')
    assert info is not None
    assert info['behind'] == 0, 'stable past-tag must be up-to-date, not firehose'
    assert updates._select_apply_compare_ref(channel_repo, 'stable', 'webui') is None


def test_experimental_user_sees_unpromoted_exp_commit(channel_repo):
    """Same HEAD (v0.52.5), experimental channel, sees exp-v0.52.6 as behind=1."""
    _git(channel_repo, 'commit', '-q', '--allow-empty', '-m', 'batch6')
    _git(channel_repo, 'tag', 'exp-v0.52.6')
    _git(channel_repo, 'checkout', '-q', 'v0.52.5')

    info = updates._check_repo_release(channel_repo, 'webui', 'experimental')
    assert info is not None
    assert info['behind'] == 1
    assert info['current_version'] == 'exp-v0.52.5'
    assert info['latest_version'] == 'exp-v0.52.6'


# ── Agent repo keeps historical fall-through (channel is webui-only) ─────────

def test_agent_repo_falls_through_to_branch_even_on_stable(channel_repo):
    """The agent repo legitimately tracks master past its tags — the stable
    channel's fall-through suppression is webui-only, so the agent still
    branch-compares when HEAD is past the latest tag."""
    # Add commits past the latest stable tag WITHOUT promoting them, and set an
    # upstream so the branch path is reachable.
    _git(channel_repo, 'commit', '-q', '--allow-empty', '-m', 'past1')
    _git(channel_repo, 'commit', '-q', '--allow-empty', '-m', 'past2')
    # name='agent' → suppression OFF → past-tag HEAD returns None (fall through).
    result = updates._check_repo_release(channel_repo, 'agent', 'stable')
    assert result is None, 'agent must fall through to branch check when past its tag'


def test_agent_resolution_identical_under_both_webui_channels(tmp_path, monkeypatch):
    """Codex gate: the update CHANNEL is WebUI-only. An Agent repo that tags only
    plain v* must resolve release/apply IDENTICALLY whether the user's WebUI
    channel is 'stable' or 'experimental' — the webui channel must never leak
    into the agent check (which would make the agent ignore its v* tags and fall
    back to origin/master)."""
    agent = tmp_path / 'agent'
    agent.mkdir()
    _git(agent, 'init', '-q')
    _git(agent, 'config', 'user.email', 't@t.co')
    _git(agent, 'config', 'user.name', 'Test')
    _git(agent, 'remote', 'add', 'origin', 'https://github.com/nesquena/hermes-agent.git')
    _git(agent, 'commit', '-q', '--allow-empty', '-m', 'a1')
    _git(agent, 'tag', 'v1.0.0')
    _git(agent, 'commit', '-q', '--allow-empty', '-m', 'a2')
    _git(agent, 'tag', 'v1.0.3')            # agent has ONLY plain v* tags, no exp-v*
    _git(agent, 'checkout', '-q', 'v1.0.0')  # behind by one release

    # check_for_updates threads the user's WebUI channel; the agent leg must
    # ignore it. Compare agent payloads under stable vs experimental WebUI.
    monkeypatch.setattr(updates, 'REPO_ROOT', tmp_path / 'nonexistent-webui')
    monkeypatch.setattr(updates, '_AGENT_DIR', agent)

    def fresh_cache():
        monkeypatch.setitem(updates._update_cache, 'checked_at', 0)

    fresh_cache()
    stable = updates.check_for_updates(force=True, include_agent=True, channel='stable')['agent']
    fresh_cache()
    experimental = updates.check_for_updates(force=True, include_agent=True, channel='experimental')['agent']

    # Agent must resolve its v1.0.3 release identically on both — never None /
    # origin/master (which is what leaking 'experimental' into the agent caused).
    assert stable.get('latest_version') == 'v1.0.3'
    assert experimental.get('latest_version') == 'v1.0.3'
    assert stable.get('behind') == experimental.get('behind') == 1
    # Apply-ref selection for the agent is channel-independent because the apply
    # wrappers force DEFAULT_UPDATE_CHANNEL for target=='agent'. Verified at the
    # raw layer with the default (stable) channel — the agent's v* tag resolves.
    assert updates._select_apply_compare_ref(agent, 'stable', 'agent') == 'v1.0.3'


# ── Force-update rewind guard (Codex CORE #3) ────────────────────────────────

def test_force_update_refuses_rewind_when_ref_is_ancestor(channel_repo, monkeypatch):
    """The rewind guard: apply_force_update must refuse to reset --hard onto a
    ref that is a strict ANCESTOR of HEAD (a downgrade). HEAD is on v0.52.5;
    we force to a ref resolving to the older v0.52.2 (an ancestor)."""
    monkeypatch.setattr(updates, 'REPO_ROOT', channel_repo)
    monkeypatch.setattr(
        updates, '_restart_blocker_snapshot',
        lambda: {'restart_blocked': False, 'active_streams': 0, 'active_runs': 0},
    )
    # Force the compare ref to the older stable tag (a strict ancestor of HEAD).
    monkeypatch.setattr(
        updates, '_select_apply_compare_ref',
        lambda path, channel='stable', target=None: 'v0.52.2',
    )
    real_run_git = updates._run_git

    def no_fetch(args, cwd, timeout=10):
        if args[:2] == ['fetch', 'origin']:
            return '', True
        return real_run_git(args, cwd, timeout=timeout)

    monkeypatch.setattr(updates, '_run_git', no_fetch)
    result = updates.apply_force_update('webui', channel='stable')
    assert result.get('refused_rewind') is True, result
    assert result['ok'] is False
    # HEAD must not have moved (no rewind actually happened).
    head, ok = updates._run_git(['rev-parse', 'HEAD'], channel_repo)
    v525, _ = updates._run_git(['rev-parse', 'v0.52.5'], channel_repo)
    assert head == v525, 'force-update must not have rewound HEAD'


# ── Cache is keyed by channel (Codex SILENT #5) ──────────────────────────────

def test_update_cache_scoped_by_channel(channel_repo, monkeypatch):
    calls = []

    def fake_check_repo(path, name, channel='stable'):
        calls.append((name, channel))
        return {'name': name, 'behind': 0, 'channel': channel}

    monkeypatch.setattr(updates, 'REPO_ROOT', channel_repo)
    monkeypatch.setattr(updates, '_AGENT_DIR', channel_repo)
    monkeypatch.setattr(updates, '_check_repo', fake_check_repo)
    monkeypatch.setitem(updates._update_cache, 'checked_at', 0)
    monkeypatch.setitem(updates._update_cache, 'channel', 'stable')

    stable = updates.check_for_updates(force=True, include_agent=False, channel='stable')
    # A non-forced call on a DIFFERENT channel must not serve the stable cache.
    experimental = updates.check_for_updates(force=False, include_agent=False, channel='experimental')
    assert stable['channel'] == 'stable'
    assert experimental['channel'] == 'experimental'
    # Both channels triggered a real check (no stale cross-channel cache hit).
    assert ('webui', 'stable') in calls
    assert ('webui', 'experimental') in calls


# ── Lock-recovery preserves the channel (Codex round-2 SILENT) ───────────────

def test_clear_lock_retry_preserves_experimental_channel(tmp_path, monkeypatch):
    """apply_clear_lock re-runs the normal update once the lock is gone. It must
    pass the configured channel through — otherwise an experimental WebUI
    lock-recovery retry silently falls back to stable (_apply_update_inner
    defaults to stable)."""
    (tmp_path / '.git').mkdir()
    monkeypatch.setattr(updates, 'REPO_ROOT', tmp_path)
    monkeypatch.setattr(
        updates, '_restart_blocker_snapshot',
        lambda: {'restart_blocked': False, 'active_streams': 0, 'active_runs': 0},
    )
    # No lock present → clear-lock takes the "re-run normal update" branch.
    monkeypatch.setattr(
        updates, '_inventory_locks',
        lambda path: {'well_known_lock_present': False,
                      'well_known_lock_path': str(tmp_path / '.git/index.lock'),
                      'other_locks': []},
    )
    # User's configured channel is experimental.
    monkeypatch.setattr(updates, '_read_update_channel', lambda: 'experimental')
    seen = {}

    def fake_inner(target, channel='stable'):
        seen['channel'] = channel
        return {'ok': True, 'target': target, 'channel': channel}

    monkeypatch.setattr(updates, '_apply_update_inner', fake_inner)
    result = updates.apply_clear_lock('webui')
    assert seen.get('channel') == 'experimental', (
        'clear-lock retry must preserve the experimental channel, not default to stable'
    )
    assert result['ok'] is True
    assert result['lock_recovery']['action'] == 'no-lock-found'


# ── Stable-tagged install opting into Experimental (#5862) ───────────────────
#
# b3nw's report: a plain stable install pinned on v0.52.0 that flips to the
# Experimental channel. Unlike `channel_repo` (which tags stable and exp on the
# SAME commits), here the stable tag PREDATES every exp-v* tag — so from HEAD ==
# v0.52.0 there is NO exp-v* tag reachable behind HEAD. Both channel-scoped git
# lookups then degrade: the chip `describe --always` leaks a bare SHA, and the
# banner `describe --abbrev=0` fatals -> current_version=None -> "unknown".

@pytest.fixture
def stable_pinned_repo(tmp_path):
    """v0.52.0 tagged first (stable only), then 3 exp-v* batches AHEAD on master.

    HEAD is checked out ON v0.52.0, so no exp-v* tag is reachable behind HEAD.
    """
    repo = tmp_path / 'stable_pinned'
    repo.mkdir()
    _git(repo, 'init', '-q')
    _git(repo, 'config', 'user.email', 't@t.co')
    _git(repo, 'config', 'user.name', 'Test')
    _git(repo, 'remote', 'add', 'origin', 'https://github.com/nesquena/hermes-webui.git')
    _git(repo, 'commit', '-q', '--allow-empty', '-m', 'v0.52.0 release')
    _git(repo, 'tag', 'v0.52.0')  # stable tag, NO exp tag on this commit
    for i in range(1, 4):
        _git(repo, 'commit', '-q', '--allow-empty', '-m', f'exp batch {i}')
        _git(repo, 'tag', f'exp-v0.52.{i}')
    _git(repo, 'checkout', '-q', 'v0.52.0')  # pin HEAD on the stable release
    return repo


def test_stable_pinned_current_release_tag_is_none_on_experimental(stable_pinned_repo):
    """Precondition: the channel-scoped current-tag lookup genuinely returns None
    (no exp-v* reachable behind a v0.52.0 HEAD). This is the trigger for #5862."""
    assert updates._current_release_tag(stable_pinned_repo, 'experimental') is None
    # Stable still resolves the real tag.
    assert updates._current_release_tag(stable_pinned_repo, 'stable') == 'v0.52.0'


def test_stable_pinned_experimental_reports_installed_version_not_unknown(stable_pinned_repo, monkeypatch):
    """#5862: current_version must be the neutral installed tag (v0.52.0), NOT
    None (which the UI renders as 'unknown'), and the count must be the real
    number of exp releases ahead (3), NOT the bogus _release_gap fallback of 1."""
    monkeypatch.setattr(updates, 'WEBUI_VERSION', 'v0.52.0')
    info = updates._check_repo_release(stable_pinned_repo, 'webui', 'experimental')
    assert info is not None
    assert info['current_version'] == 'v0.52.0', 'must not be None/"unknown"'
    assert info['current_sha'] == 'v0.52.0'
    assert info['latest_version'] == 'exp-v0.52.3'
    assert info['behind'] == 3, 'all 3 exp releases are ahead, not a bogus 1'


def test_stable_pinned_stable_channel_still_up_to_date(stable_pinned_repo, monkeypatch):
    """Sanity: the SAME install on the stable channel is up-to-date on v0.52.0
    (the exp-v* tags ahead must not be offered as stable updates)."""
    monkeypatch.setattr(updates, 'WEBUI_VERSION', 'v0.52.0')
    info = updates._check_repo_release(stable_pinned_repo, 'webui', 'stable')
    assert info is not None
    assert info['behind'] == 0
    assert info['current_version'] == 'v0.52.0'


def test_channel_version_badge_no_bare_sha_on_experimental(stable_pinned_repo, monkeypatch):
    """#5862 chip: channel_version_badge must fall back to the neutral installed
    version, never a bare git SHA, when no channel tag is reachable."""
    monkeypatch.setattr(updates, 'REPO_ROOT', stable_pinned_repo)
    monkeypatch.setattr(updates, 'WEBUI_VERSION', 'v0.52.0')
    badge = updates.channel_version_badge('experimental')
    assert badge == 'v0.52.0', f'expected installed version, got bare SHA-ish {badge!r}'
    # Stable channel resolves its own reachable tag directly.
    assert updates.channel_version_badge('stable') == 'v0.52.0'


def test_count_channel_tags_ahead(stable_pinned_repo):
    """The ahead-count helper: 3 exp-v* tags sit ahead of a v0.52.0 HEAD.

    This helper is only ever CALLED when no channel tag is reachable on/behind
    HEAD (current_tag is None) — exactly the experimental case here, where no
    exp-v* tag sits on the v0.52.0 commit, so `--contains HEAD` counts precisely
    the 3 tags strictly ahead.
    """
    _git(stable_pinned_repo, 'checkout', '-q', 'v0.52.0')
    assert updates._count_channel_tags_ahead(stable_pinned_repo, 'experimental') == 3


def test_stable_pinned_experimental_agent_repo_does_not_inject_webui_version(stable_pinned_repo, monkeypatch):
    """#5864: the installed-version fallback is WebUI-only. _check_repo_release is
    shared with the Agent repo, where WEBUI_VERSION (v0.52.0) is not a valid ref —
    it must NOT be injected as the Agent's current_version/current_sha (that would
    show the WebUI version as the Agent's and emit a broken Agent compare link)."""
    monkeypatch.setattr(updates, 'WEBUI_VERSION', 'v0.52.0')
    info = updates._check_repo_release(stable_pinned_repo, 'agent', 'experimental')
    assert info is not None
    # Agent must NOT carry the WebUI version.
    assert info['current_version'] != 'v0.52.0'
    assert info['current_sha'] != 'v0.52.0'


def test_stable_pinned_experimental_current_sha_is_verified_ref_only(stable_pinned_repo, monkeypatch):
    """#5864: current_sha must be a git-VERIFIED ref, never a raw WEBUI_VERSION
    that can be dirty/`-N-g<sha>`/bare-SHA/`unknown`. When HEAD is exactly on a
    tag, current_sha resolves to that tag; when WEBUI_VERSION is a non-ref dirty
    string, the compare ref falls back to None (no broken /compare link) while the
    displayed version still shows the real installed string."""
    # HEAD is exactly on v0.52.0 → verified tag resolves for the compare link.
    monkeypatch.setattr(updates, 'WEBUI_VERSION', 'v0.52.0-dirty-deadbeef')
    info = updates._check_repo_release(stable_pinned_repo, 'webui', 'experimental')
    assert info is not None
    # current_version shows the real (dirty) installed string...
    assert info['current_version'] == 'v0.52.0-dirty-deadbeef'
    # ...but current_sha is the git-verified exact tag, NOT the dirty display string.
    assert info['current_sha'] == 'v0.52.0'
    assert 'dirty' not in (info['current_sha'] or '')
