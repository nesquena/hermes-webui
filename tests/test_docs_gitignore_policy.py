"""Regression tests for docs/ ignore policy."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _git_check_ignore(path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "check-ignore", "-q", path],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_new_top_level_markdown_docs_are_trackable():
    """New docs/*.md files should be visible to Git, not silently ignored."""
    assert _git_check_ignore("docs/example-new-guide.md").returncode == 1


def test_docs_scratch_files_remain_ignored():
    """The broad docs/* ignore rule should still keep arbitrary scratch files out."""
    assert _git_check_ignore("docs/local-scratch.tmp").returncode == 0


def test_local_only_ai_context_files_remain_ignored_under_docs():
    """Local AI assistant context files must stay out of commits under docs/."""
    assert _git_check_ignore("docs/AGENTS.md").returncode == 0
    assert _git_check_ignore("docs/CLAUDE.md").returncode == 0
    assert _git_check_ignore("docs/.cursorrules").returncode == 0
    assert _git_check_ignore("docs/.windsurfrules").returncode == 0
