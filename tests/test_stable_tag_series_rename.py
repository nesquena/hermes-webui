"""Stable update check must survive a stable-series rename (creordate order).

The Agent repo renamed its stable series from ``v2026.9.x`` to ``v0.21.x``
(Oct 2026). With name-sort (``--sort=-v:refname``) ``v2026.9.24`` outranked
``v0.21.6`` (2026 > 0), so ``latest_tag`` resolved to an ANCESTOR of HEAD.
HEAD contains that ancestor, the release check bailed out, and the check fell
through to the branch-comparison fallback — advertising 500+ untagged master
commits to an install already sitting on the latest stable release.

Also, the ``v*`` stable glob matches build-metadata prereleases
(``v0.21.6+canary.*``) which must not be treated as published releases.
"""
from unittest.mock import patch

from api import updates


def _fake_git_two_series(args, cwd, timeout=10):
    if args == ['diff-index', '--quiet', 'HEAD', '--']:
        return '', True  # clean tree
    if args[:1] == ['--no-optional-locks']:
        return '', True  # dirty probe: clean tree
    if args == ['fetch', 'origin', '--tags', '--force']:
        return '', True
    if args == ['tag', '--list', 'v*', '--sort=-creatordate']:
        # creatordate order: canaries are newest, then the real release, then
        # the legacy series (which name-sort would have ranked FIRST).
        return (
            'v0.21.6+canary.20261010T070026Z\n'
            'v0.21.6+canary.20261009T070410Z\n'
            'v0.21.6\n'
            'v0.21.5+canary.20261008T070449Z\n'
            'v2026.9.24\n'
            'v2026.9.21\n'
        ), True
    if args == ['describe', '--tags', '--abbrev=0', '--match', 'v*']:
        return 'v0.21.6', True
    if args == ['describe', '--tags', '--always', '--match', 'v*']:
        return 'v0.21.6', True  # HEAD exactly on the release tag
    if args == ['merge-base', '--is-ancestor', 'v0.21.6', 'HEAD']:
        return '', True
    if args == ['merge-base', '--is-ancestor', 'HEAD', 'v0.21.6']:
        return '', True
    if args == ['rev-list', '--left-right', '--count', 'HEAD...v0.21.6']:
        return '0\t0', True  # HEAD exactly on the release tag
    if args == ['remote', 'get-url', 'origin']:
        return 'https://github.com/NousResearch/hermes-agent.git', True
    raise AssertionError(f'unexpected git args: {args!r}')


def test_stable_release_tags_filter_prereleases_and_keep_release_first(tmp_path):
    """Canary/build-metadata tags are not releases; latest = v0.21.6."""
    (tmp_path / '.git').mkdir()
    with patch.object(updates, '_run_git', side_effect=_fake_git_two_series):
        tags = updates._release_tags(tmp_path, 'stable')

    assert 'v0.21.6+canary.20261010T070026Z' not in tags
    assert 'v0.21.6' in tags
    assert tags[0] == 'v0.21.6'


def test_stable_check_up_to_date_after_series_rename(tmp_path):
    """HEAD on the newest release reports behind=0 — no branch fallback."""
    (tmp_path / '.git').mkdir()
    with patch.object(updates, '_run_git', side_effect=_fake_git_two_series):
        info = updates._check_repo(tmp_path, 'agent', 'stable')

    assert info is not None
    assert info['behind'] == 0
    assert info['current_version'] == 'v0.21.6'
    assert info['latest_version'] == 'v0.21.6'
