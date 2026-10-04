"""Regression tests for docs/ ignore policy."""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _git_check_ignore(path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "check-ignore", "-q", path],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def _require_git_work_tree() -> None:
    """Skip when ROOT is not a usable git work tree.

    Under a detached worktree whose ``.git`` file points at a gitdir that isn't
    reachable (e.g. a bubblewrap sandbox that only bind-mounts the worktree, or a
    checkout without the parent repo's ``.git`` metadata), ``git`` exits 128 with
    ``fatal: not a git repository``.  ``git check-ignore`` then returns 128
    instead of the 0/1 these tests assert on, so the ignore policy simply cannot
    be evaluated here.  That is a missing-prerequisite condition, not a policy
    regression — skip rather than fail.
    """
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        pytest.skip(
            "not inside a usable git work tree; git check-ignore is unavailable "
            f"(git rev-parse rc={probe.returncode}, stderr={probe.stderr.strip()!r})"
        )


def test_new_top_level_markdown_docs_are_trackable():
    """New docs/*.md files should be visible to Git, not silently ignored."""
    _require_git_work_tree()
    assert _git_check_ignore("docs/example-new-guide.md").returncode == 1


def test_root_agents_entrypoint_is_trackable():
    """AGENTS.md is the shared repo entrypoint; local overrides stay ignored."""
    _require_git_work_tree()
    assert _git_check_ignore("AGENTS.md").returncode == 1
    assert _git_check_ignore("AGENTS.local.md").returncode == 0


def test_docs_scratch_files_remain_ignored():
    """The broad docs/* ignore rule should still keep arbitrary scratch files out."""
    _require_git_work_tree()
    assert _git_check_ignore("docs/local-scratch.tmp").returncode == 0


def test_local_only_ai_context_files_remain_ignored_under_docs():
    """Local AI assistant context files must stay out of commits under docs/."""
    _require_git_work_tree()
    assert _git_check_ignore("docs/AGENTS.md").returncode == 0
    assert _git_check_ignore("docs/CLAUDE.md").returncode == 0
    assert _git_check_ignore("docs/.cursorrules").returncode == 0
    assert _git_check_ignore("docs/.windsurfrules").returncode == 0
