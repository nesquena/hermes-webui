"""Tests for self-update diagnostics (api/updates.py)."""

from unittest.mock import MagicMock, patch

import api.updates as updates


def test_run_git_returns_stderr_on_failure(tmp_path):
    """When a git command fails, _run_git should return stderr (not empty string)."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="fatal: 'origin/master' does not appear to be a git repository\n",
        )
        out, ok = updates._run_git(["pull", "--ff-only", "origin/master"], tmp_path)

    assert ok is False
    assert "does not appear to be a git repository" in out


def test_run_git_returns_stdout_when_no_stderr(tmp_path):
    """If stderr is empty on failure, fall back to stdout."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=128,
            stdout="Already up to date.",
            stderr="",
        )
        out, ok = updates._run_git(["pull"], tmp_path)

    assert ok is False
    assert "Already up to date" in out


def test_run_git_returns_exit_code_when_no_output(tmp_path):
    """If both stdout and stderr are empty, report the exit code."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="",
        )
        out, ok = updates._run_git(["status"], tmp_path)

    assert ok is False
    assert "status 1" in out


def test_split_remote_ref_splits_tracking_ref():
    """_split_remote_ref should correctly split origin/branch."""
    assert updates._split_remote_ref("origin/master") == ("origin", "master")
    assert updates._split_remote_ref("origin/feature/foo") == ("origin", "feature/foo")
    assert updates._split_remote_ref("master") == (None, "master")
