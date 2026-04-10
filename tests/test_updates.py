from pathlib import Path
from unittest.mock import MagicMock, patch

from api import updates


def test_run_git_returns_stderr_on_failure(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()

    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout='',
            stderr="fatal: 'origin/master' does not appear to be a git repository\n",
        )
        out, ok = updates._run_git(['pull', '--ff-only', 'origin/master'], repo)

    assert ok is False
    assert "does not appear to be a git repository" in out


def test_apply_update_splits_remote_and_branch_for_pull(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / '.git').mkdir()
    monkeypatch.setattr(updates, 'REPO_ROOT', repo)

    calls = []

    def fake_run_git(args, cwd, timeout=10):
        calls.append(args)
        if args == ['rev-parse', '--abbrev-ref', '@{upstream}']:
            return 'origin/feature/update-fix', True
        if args == ['status', '--porcelain']:
            return '', True
        if args == ['pull', '--ff-only', 'origin', 'feature/update-fix']:
            return 'Already up to date.', True
        return '', True

    monkeypatch.setattr(updates, '_run_git', fake_run_git)

    res = updates._apply_update_inner('webui')

    assert res['ok'] is True
    assert ['pull', '--ff-only', 'origin', 'feature/update-fix'] in calls
    assert ['pull', '--ff-only', 'origin/feature/update-fix'] not in calls
