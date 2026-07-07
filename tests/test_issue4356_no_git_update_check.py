"""Tests for issue #4356: update check returns "can't check" for non-git installs."""
from pathlib import Path
from unittest.mock import patch

import api.updates as updates


def test_check_repo_returns_no_git_sentinel_when_dot_git_absent(tmp_path):
    """_check_repo returns sentinel dict with no_git=True when .git is absent."""
    result = updates._check_repo(tmp_path, 'webui')

    assert result is not None
    assert isinstance(result, dict)
    assert result['name'] == 'webui'
    assert result['behind'] is None
    assert result['no_git'] is True


def test_check_repo_returns_no_git_sentinel_when_path_is_none():
    """_check_repo returns sentinel dict with no_git=True when path is None."""
    result = updates._check_repo(None, 'webui')

    assert result is not None
    assert isinstance(result, dict)
    assert result['name'] == 'webui'
    assert result['behind'] is None
    assert result['no_git'] is True


def test_check_repo_still_returns_dict_when_dot_git_exists(tmp_path):
    """_check_repo calls git operations when .git exists; no_git should not be True."""
    (tmp_path / '.git').mkdir()

    def fake_git(args, cwd, timeout=10):
        if args == ['diff-index', '--quiet', 'HEAD', '--']:
            return '', True  # clean tree
        if args == ['fetch', 'origin', '--tags', '--force']:
            return 'network unreachable', False
        if args == ['tag', '--list', 'v*', '--sort=-v:refname']:
            return '', True
        raise AssertionError(f'unexpected git args: {args!r}')

    with patch.object(updates, '_run_git', side_effect=fake_git):
        result = updates._check_repo(tmp_path, 'webui')

    assert result is not None
    assert isinstance(result, dict)
    # When .git exists, no_git should not be in result or should be False
    assert result.get('no_git') is not True


def test_format_update_target_status_excludes_no_git():
    """_formatUpdateTargetStatus returns null for no_git so it doesn't show as update available."""
    ui_path = Path(__file__).resolve().parent.parent / 'static' / 'ui.js'
    content = ui_path.read_text(encoding="utf-8")

    assert '_formatUpdateTargetStatus' in content
    assert 'info.no_git' in content


def test_panels_has_no_git_branch():
    """Update check consumer in static/panels.js has no_git handling.

    Guards the mixed-deployment fix (#4356): a no_git target must surface the
    can't-check indicator even when a sibling git target is up-to-date — so the
    consumer tracks noGitParts separately rather than only firing when EVERY
    target is no_git (which hid the indicator in mixed installs).
    """
    panels_path = Path(__file__).resolve().parent.parent / 'static' / 'panels.js'
    content = panels_path.read_text(encoding="utf-8")

    # Check for the no_git conditional in the update consumer
    assert 'no_git' in content
    assert 'settings_update_no_git' in content
    # Mixed-deployment: must use a per-target noGitParts accumulator, NOT the
    # fragile "every target is no_git" gate that hid the indicator when one
    # target was a git checkout.
    assert 'noGitParts' in content
    assert 'every(c=>c.no_git)' not in content


def test_i18n_no_git_key_all_locales():
    """settings_update_no_git key appears in all 14 locale blocks in static/i18n.js."""
    i18n_path = Path(__file__).resolve().parent.parent / 'static' / 'i18n.js'
    content = i18n_path.read_text(encoding="utf-8")

    # Count occurrences of the new key (should be exactly 13, one per locale)
    count = content.count("settings_update_no_git")
    assert count == 15, f"Expected exactly 15 settings_update_no_git keys, found {count}"
