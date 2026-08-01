from __future__ import annotations

import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from api.worktree_gc_git import (
    KEEP_DIRTY,
    KEEP_IGNORED_FILES,
    KEEP_STALE_METADATA,
    KEEP_UNCERTAIN,
    KEEP_UNIQUE_COMMITS,
    REMOVE_ANCESTOR,
    REMOVE_PATCH_EQUIVALENT_KEEP_BRANCH,
    GitWorktreeDecision,
    classify_git_worktree,
)


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
    )


def _commit(repo: Path, filename: str, content: str, message: str) -> str:
    (repo / filename).write_text(content, encoding="utf-8")
    _git(repo, "add", "--", filename)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def make_remote_repo(tmp_path: Path) -> dict[str, Path | str]:
    remote = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    remote.mkdir()
    repo.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=master")
    _git(repo, "init", "--initial-branch=master")
    _git(repo, "config", "user.email", "gc-tests@example.invalid")
    _git(repo, "config", "user.name", "Worktree GC Tests")
    base_sha = _commit(repo, "base.txt", "base\n", "base")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "master")
    target_sha = _commit(repo, "shared.txt", "same patch\n", "target patch")
    _git(repo, "push", "origin", "master")
    return {
        "repo": repo,
        "remote": remote,
        "base_sha": base_sha,
        "target_sha": target_sha,
    }


def add_worktree(
    case: dict[str, Path | str],
    tmp_path: Path,
    branch: str,
    *,
    start: str | None = None,
) -> Path:
    repo = case["repo"]
    assert isinstance(repo, Path)
    worktree = tmp_path / branch.replace("/", "-")
    _git(
        repo,
        "worktree",
        "add",
        "-b",
        branch,
        str(worktree),
        start or str(case["base_sha"]),
    )
    _git(worktree, "config", "user.email", "gc-tests@example.invalid")
    _git(worktree, "config", "user.name", "Worktree GC Tests")
    return worktree


def test_decision_is_frozen_and_exposes_audit_fields(tmp_path):
    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, "gc/ancestor")

    decision = classify_git_worktree(
        worktree,
        "gc/ancestor",
        case["repo"],
    )

    assert isinstance(decision, GitWorktreeDecision)
    assert decision.path == str(worktree.resolve())
    assert decision.branch == "gc/ancestor"
    assert decision.repo_root == str(Path(case["repo"]).resolve())
    assert decision.target_ref == "origin/master"
    assert decision.verdict == REMOVE_ANCESTOR
    assert decision.eligible is True
    assert decision.exists is True
    assert decision.listed is True
    assert decision.dirty is False
    assert decision.untracked_count == 0
    assert decision.ancestor_of_target is True
    assert decision.cherry_unique_count is None
    assert decision.reasons
    with pytest.raises(FrozenInstanceError):
        decision.verdict = KEEP_UNCERTAIN  # type: ignore[misc]


def test_patch_equivalent_commit_removes_only_worktree(tmp_path):
    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, "gc/patch-equivalent")
    patch_sha = _commit(
        worktree,
        "shared.txt",
        "same patch\n",
        "equivalent patch under another sha",
    )
    assert patch_sha != case["target_sha"]

    decision = classify_git_worktree(
        worktree,
        "gc/patch-equivalent",
        case["repo"],
    )

    assert decision.verdict == REMOVE_PATCH_EQUIVALENT_KEEP_BRANCH
    assert decision.eligible is True
    assert decision.ancestor_of_target is False
    assert decision.cherry_unique_count == 0


def test_one_unique_cherry_commit_keeps_worktree_and_branch(tmp_path):
    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, "gc/unique")
    _commit(worktree, "unique.txt", "must survive\n", "unique work")

    decision = classify_git_worktree(
        worktree,
        "gc/unique",
        case["repo"],
    )

    assert decision.verdict == KEEP_UNIQUE_COMMITS
    assert decision.eligible is False
    assert decision.ancestor_of_target is False
    assert decision.cherry_unique_count == 1


def test_equivalent_and_unique_cherry_commits_still_keep_worktree(tmp_path):
    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, "gc/mixed-cherry")
    _commit(
        worktree,
        "shared.txt",
        "same patch\n",
        "equivalent patch under another sha",
    )
    _commit(worktree, "unique.txt", "must survive\n", "unique work")

    decision = classify_git_worktree(
        worktree,
        "gc/mixed-cherry",
        case["repo"],
    )

    assert decision.verdict == KEEP_UNIQUE_COMMITS
    assert decision.eligible is False
    assert decision.cherry_unique_count == 1


@pytest.mark.parametrize("untracked", [False, True], ids=["tracked", "untracked"])
def test_dirty_or_untracked_content_is_kept(tmp_path, untracked):
    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, f"gc/dirty-{untracked}")
    if untracked:
        (worktree / "untracked.txt").write_text("local\n", encoding="utf-8")
    else:
        (worktree / "base.txt").write_text("edited\n", encoding="utf-8")

    decision = classify_git_worktree(
        worktree,
        f"gc/dirty-{untracked}",
        case["repo"],
    )

    assert decision.verdict == KEEP_DIRTY
    assert decision.eligible is False
    assert decision.dirty is True
    assert decision.untracked_count == (1 if untracked else 0)
    assert decision.ignored_count == 0
    assert decision.ancestor_of_target is None


@pytest.mark.parametrize(
    "ignored_name",
    [
        ".env",
        "ignored directory/private.txt",
        "ignored name with spaces.txt",
        "ignored-newline-directory/ignored-name-with-\n-newline.txt",
    ],
)
def test_ignored_files_are_kept_without_reading_contents(tmp_path, ignored_name):
    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, "gc/ignored")
    ignored = worktree / ignored_name
    ignored.parent.mkdir(parents=True, exist_ok=True)
    ignored.write_text("must remain private\n", encoding="utf-8")
    (worktree / ".gitignore").write_text(
        f"/{ignored_name.split('/', 1)[0]}\n",
        encoding="utf-8",
    )
    _git(worktree, "add", ".gitignore")
    _git(worktree, "commit", "-m", "ignore private file")

    decision = classify_git_worktree(
        worktree,
        "gc/ignored",
        case["repo"],
    )

    assert decision.verdict == KEEP_IGNORED_FILES
    assert decision.eligible is False
    assert decision.dirty is False
    assert decision.untracked_count == 0
    assert decision.ignored_count == 1
    assert decision.reasons == ("ignored_files_present",)
    assert "must remain private" not in repr(decision)


def test_no_ignored_files_reports_zero_and_remains_eligible(tmp_path):
    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, "gc/no-ignored")

    decision = classify_git_worktree(
        worktree,
        "gc/no-ignored",
        case["repo"],
    )

    assert decision.verdict == REMOVE_ANCESTOR
    assert decision.eligible is True
    assert decision.ignored_count == 0


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected_reason"),
    [
        (b"unterminated", 0, "ignored_files_unparseable"),
        (b"", 1, "ignored_files_failed"),
    ],
)
def test_unverifiable_ignored_file_probe_fails_closed(
    tmp_path,
    monkeypatch,
    stdout,
    returncode,
    expected_reason,
):
    import api.worktree_gc_git as gc_git

    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, "gc/ignored-uncertain")
    real_run_git = gc_git._run_git

    def corrupt_ignored_probe(args, cwd, *, timeout=gc_git.GIT_TIMEOUT):
        if args == [
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
        ]:
            return subprocess.CompletedProcess(
                ["git", *args],
                returncode,
                stdout=stdout,
                stderr=b"private detail",
            )
        return real_run_git(args, cwd, timeout=timeout)

    monkeypatch.setattr(gc_git, "_run_git", corrupt_ignored_probe)

    decision = classify_git_worktree(
        worktree,
        "gc/ignored-uncertain",
        case["repo"],
    )

    assert decision.verdict == KEEP_UNCERTAIN
    assert decision.eligible is False
    assert decision.ignored_count is None
    assert decision.reasons == (expected_reason,)
    assert "private detail" not in repr(decision)


def test_ignored_file_probe_timeout_fails_closed(tmp_path, monkeypatch):
    import api.worktree_gc_git as gc_git

    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, "gc/ignored-timeout")
    real_run_git = gc_git._run_git

    def timeout_ignored_probe(args, cwd, *, timeout=gc_git.GIT_TIMEOUT):
        if args == [
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
        ]:
            raise gc_git._GitInvocationError("git_timeout")
        return real_run_git(args, cwd, timeout=timeout)

    monkeypatch.setattr(gc_git, "_run_git", timeout_ignored_probe)

    decision = classify_git_worktree(
        worktree,
        "gc/ignored-timeout",
        case["repo"],
    )

    assert decision.verdict == KEEP_UNCERTAIN
    assert decision.ignored_count is None
    assert decision.reasons == ("git_timeout",)


def test_ignored_file_probe_output_limit_fails_closed(tmp_path, monkeypatch):
    import api.worktree_gc_git as gc_git

    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, "gc/ignored-output-limit")
    real_run_git = gc_git._run_git

    def oversized_ignored_probe(args, cwd, *, timeout=gc_git.GIT_TIMEOUT):
        if args == list(gc_git._IGNORED_FILES_ARGS):
            return subprocess.CompletedProcess(
                ["git", *args],
                0,
                stdout=b"x" * (gc_git.IGNORED_OUTPUT_LIMIT + 1),
                stderr=b"",
            )
        return real_run_git(args, cwd, timeout=timeout)

    monkeypatch.setattr(gc_git, "_run_git", oversized_ignored_probe)

    decision = classify_git_worktree(
        worktree,
        "gc/ignored-output-limit",
        case["repo"],
    )

    assert decision.verdict == KEEP_UNCERTAIN
    assert decision.ignored_count is None
    assert decision.reasons == ("ignored_files_unparseable",)


def test_path_branch_mismatch_fails_closed(tmp_path):
    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, "gc/actual")
    repo = case["repo"]
    assert isinstance(repo, Path)
    _git(repo, "branch", "gc/claimed", str(case["base_sha"]))

    decision = classify_git_worktree(
        worktree,
        "gc/claimed",
        repo,
    )

    assert decision.verdict == KEEP_UNCERTAIN
    assert decision.eligible is False
    assert decision.exists is True
    assert decision.listed is True
    assert "worktree_branch_mismatch" in decision.reasons


def test_missing_local_branch_fails_closed(tmp_path):
    case = make_remote_repo(tmp_path)
    absent = tmp_path / "missing-branch-worktree"

    decision = classify_git_worktree(
        absent,
        "gc/not-created",
        case["repo"],
    )

    assert decision.verdict == KEEP_UNCERTAIN
    assert decision.eligible is False
    assert "branch_missing" in decision.reasons


def test_missing_target_fails_closed(tmp_path):
    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, "gc/missing-target")

    decision = classify_git_worktree(
        worktree,
        "gc/missing-target",
        case["repo"],
        target_ref="origin/not-there",
    )

    assert decision.verdict == KEEP_UNCERTAIN
    assert decision.eligible is False
    assert decision.ancestor_of_target is None
    assert "target_ref_missing" in decision.reasons


def test_missing_path_still_listed_is_stale_metadata_audit_only(tmp_path):
    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, "gc/stale")
    holding = tmp_path / "moved-aside"
    worktree.rename(holding)

    decision = classify_git_worktree(
        worktree,
        "gc/stale",
        case["repo"],
    )

    assert decision.verdict == KEEP_STALE_METADATA
    assert decision.eligible is False
    assert decision.exists is False
    assert decision.listed is True
    assert decision.dirty is None
    assert decision.untracked_count is None


def test_empty_path_inputs_are_rejected_before_running_git(tmp_path, monkeypatch):
    import api.worktree_gc_git as gc_git

    monkeypatch.setattr(
        gc_git,
        "_run_git",
        lambda *args, **kwargs: pytest.fail("invalid empty paths must not run git"),
    )

    missing_path = classify_git_worktree("", "gc/branch", tmp_path)
    missing_repo = classify_git_worktree(tmp_path / "worktree", "gc/branch", "")

    assert missing_path.verdict == KEEP_UNCERTAIN
    assert missing_path.path == ""
    assert missing_path.exists is None
    assert missing_path.reasons == ("path_input_invalid",)
    assert missing_repo.verdict == KEEP_UNCERTAIN
    assert missing_repo.repo_root == ""
    assert missing_repo.exists is None
    assert missing_repo.reasons == ("repo_root_input_invalid",)


def test_absent_unlisted_path_is_uncertain_not_a_prune_instruction(tmp_path):
    case = make_remote_repo(tmp_path)
    repo = case["repo"]
    assert isinstance(repo, Path)
    _git(repo, "branch", "gc/absent", str(case["base_sha"]))
    absent = tmp_path / "never-listed"

    decision = classify_git_worktree(
        absent,
        "gc/absent",
        repo,
    )

    assert decision.verdict == KEEP_UNCERTAIN
    assert decision.eligible is False
    assert decision.exists is False
    assert decision.listed is False


def test_non_interpretable_porcelain_status_fails_closed(tmp_path, monkeypatch):
    import api.worktree_gc_git as gc_git

    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, "gc/bad-status")
    real_run_git = gc_git._run_git

    def corrupt_status(args, cwd, *, timeout=gc_git.GIT_TIMEOUT):
        if args == ["status", "--porcelain=v1", "-z", "--untracked-files=all"]:
            return subprocess.CompletedProcess(
                ["git", *args],
                0,
                stdout=b"?? unterminated",
                stderr=b"",
            )
        return real_run_git(args, cwd, timeout=timeout)

    monkeypatch.setattr(gc_git, "_run_git", corrupt_status)

    decision = classify_git_worktree(
        worktree,
        "gc/bad-status",
        case["repo"],
    )

    assert decision.verdict == KEEP_UNCERTAIN
    assert decision.eligible is False
    assert decision.dirty is None
    assert decision.untracked_count is None
    assert "status_unparseable" in decision.reasons


def test_status_timeout_fails_closed(tmp_path, monkeypatch):
    import api.worktree_gc_git as gc_git

    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, "gc/status-timeout")
    real_run_git = gc_git._run_git

    def timeout_status(args, cwd, *, timeout=gc_git.GIT_TIMEOUT):
        if args == ["status", "--porcelain=v1", "-z", "--untracked-files=all"]:
            raise gc_git._GitInvocationError("git_timeout")
        return real_run_git(args, cwd, timeout=timeout)

    monkeypatch.setattr(gc_git, "_run_git", timeout_status)

    decision = classify_git_worktree(
        worktree,
        "gc/status-timeout",
        case["repo"],
    )

    assert decision.verdict == KEEP_UNCERTAIN
    assert decision.eligible is False
    assert decision.dirty is None
    assert "git_timeout" in decision.reasons
