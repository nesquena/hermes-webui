"""Regression tests for docs/ ignore policy.

The rules under test are the committed ``.gitignore``. ``git check-ignore``
therefore runs in a throwaway repository that holds only a copy of that file,
not in the checkout itself: a linked worktree whose git dir is not visible
(a sandboxed suite run binds only the worktree, so git exits 128 with "not a
git repository") or a source export without ``.git`` must not fail the policy,
and the host's global/system git config or ``core.excludesFile`` must not add
or mask rules.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _isolated_git_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CEILING_DIRECTORIES"):
        env.pop(key, None)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    return env


@pytest.fixture(scope="module")
def policy_repo(tmp_path_factory) -> Path:
    repo = tmp_path_factory.mktemp("gitignore-policy")
    shutil.copyfile(ROOT / ".gitignore", repo / ".gitignore")
    subprocess.run(
        ["git", "init", "-q"],
        cwd=repo,
        env=_isolated_git_env(),
        check=True,
        capture_output=True,
        text=True,
    )
    return repo


@pytest.fixture
def check_ignore(policy_repo: Path):
    def _check(path: str) -> int:
        result = subprocess.run(
            ["git", "-c", f"core.excludesFile={os.devnull}", "check-ignore", "-q", path],
            cwd=policy_repo,
            env=_isolated_git_env(),
            capture_output=True,
            text=True,
        )
        # 0 = ignored, 1 = not ignored; anything else is a harness error.
        assert result.returncode in (0, 1), result.stderr
        return result.returncode

    return _check


def test_new_top_level_markdown_docs_are_trackable(check_ignore):
    """New docs/*.md files should be visible to Git, not silently ignored."""
    assert check_ignore("docs/example-new-guide.md") == 1


def test_root_agents_entrypoint_is_trackable(check_ignore):
    """AGENTS.md is the shared repo entrypoint; local overrides stay ignored."""
    assert check_ignore("AGENTS.md") == 1
    assert check_ignore("AGENTS.local.md") == 0


def test_docs_scratch_files_remain_ignored(check_ignore):
    """The broad docs/* ignore rule should still keep arbitrary scratch files out."""
    assert check_ignore("docs/local-scratch.tmp") == 0


def test_local_only_ai_context_files_remain_ignored_under_docs(check_ignore):
    """Local AI assistant context files must stay out of commits under docs/."""
    assert check_ignore("docs/AGENTS.md") == 0
    assert check_ignore("docs/CLAUDE.md") == 0
    assert check_ignore("docs/.cursorrules") == 0
    assert check_ignore("docs/.windsurfrules") == 0
