from __future__ import annotations

from pathlib import Path

from api.worktree_gc_git import (
    KEEP_UNIQUE_MERGE_COMMITS,
    KEEP_UNCERTAIN,
    REMOVE_ANCESTOR,
    REMOVE_PATCH_EQUIVALENT_KEEP_BRANCH,
    classify_git_worktree,
)
from tests.test_worktree_gc_git_classification import (
    _git,
    _commit,
    add_worktree,
    make_remote_repo,
)


def test_inherited_git_trace_never_creates_a_file_outside_the_report(tmp_path, monkeypatch):
    """Blocker 1: ambient GIT_TRACE must not leak into the audit subprocess."""
    import api.worktree_gc_git as gc_git

    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, "gc/trace-leak")
    trace_file = tmp_path / "outside" / "trace.log"

    monkeypatch.setenv("GIT_TRACE", str(trace_file))
    monkeypatch.setenv("GIT_TRACE_PACK_ACCESS", str(trace_file))
    env = gc_git._clean_git_env()

    assert "GIT_TRACE" not in env
    assert "GIT_TRACE_PACK_ACCESS" not in env
    for key in env:
        assert not key.upper().startswith("GIT_") or key in {
            "GIT_TERMINAL_PROMPT",
            "GIT_NO_REPLACE_OBJECTS",
            "GIT_OPTIONAL_LOCKS",
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_SYSTEM",
        }, f"inherited {key} survived cleaning"

    decision = classify_git_worktree(worktree, "gc/trace-leak", case["repo"])

    assert decision.verdict == REMOVE_ANCESTOR
    assert decision.eligible is True
    assert not trace_file.exists()


def test_clean_git_env_strips_every_inherited_git_variable(tmp_path, monkeypatch):
    """Blocker 1: even unknown/odd-cased GIT_* variables must be removed."""
    import api.worktree_gc_git as gc_git

    hostile = {
        "GIT_TRACE": "/tmp/evil-trace",
        "GIT_TRACE2_EVENT": "/tmp/evil-trace2",
        "GIT_CONFIG_PARAMETERS": "'core.hooksPath=/tmp/evil-hooks'",
        "GIT_EDITOR": "touch /tmp/evil-editor",
        "GIT_NOTES_DISPLAY_REF": "refs/notes/evil",
        "GIT_CEILING_DIRECTORIES": "/tmp",
    }
    for key, value in hostile.items():
        monkeypatch.setenv(key, value)

    env = gc_git._clean_git_env()

    for key in hostile:
        assert key not in env
    # Only the fixed, audit-required values may be re-added.
    git_keys = {key for key in env if key.upper().startswith("GIT_")}
    assert git_keys <= {
        "GIT_TERMINAL_PROMPT",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_OPTIONAL_LOCKS",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
    }


def test_branch_exclusive_merge_resolution_is_not_patch_equivalent(tmp_path):
    """Blocker 2: git cherry omits a branch-exclusive merge commit."""
    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, "gc/merge-unique")
    # Reproduce the certified defect shape: every regular commit is
    # patch-equivalent to the target, and the branch-exclusive merge commit
    # carries a resolution that only exists on the branch (hidden unique
    # content).  git cherry only reports "-" entries and omits the merge.
    base_sha = case["base_sha"]
    _git(worktree, "reset", "-q", "--hard", str(base_sha))
    # Same patch content as the target commit: patch-equivalent.
    _commit(worktree, "shared.txt", "same patch\n", "branch-side duplicate")
    _git(worktree, "checkout", "-q", "-b", "side-work", str(base_sha))
    _commit(worktree, "shared.txt", "same patch\n", "other-side duplicate")
    _git(worktree, "checkout", "-q", "gc/merge-unique")
    _git(worktree, "merge", "--no-ff", "-q", "side-work", "-m", "merge duplicates")
    # Hide unique content inside the merge commit itself.
    (worktree / "unique-resolution.txt").write_text(
        "only in merge\n",
        encoding="utf-8",
    )
    _git(worktree, "add", "unique-resolution.txt")
    _git(worktree, "commit", "-q", "--amend", "--no-edit")

    decision = classify_git_worktree(
        worktree,
        "gc/merge-unique",
        case["repo"],
    )

    # The merge commit resolution is branch-exclusive: never eligible unless
    # the resulting tree is proven equal to the target tree.
    assert decision.verdict == KEEP_UNIQUE_MERGE_COMMITS
    assert decision.eligible is False
    assert decision.branch_exclusive_merge_count == 1
    assert decision.reasons == ("branch_exclusive_merge_present",)


def test_branch_exclusive_merge_with_proven_equal_tree_stays_eligible(tmp_path):
    """Blocker 2 mitigation: a merge whose tree equals the target tree is safe."""
    case = make_remote_repo(tmp_path)
    repo = case["repo"]
    assert isinstance(repo, Path)
    worktree = add_worktree(case, tmp_path, "gc/merge-equal-tree")
    base_sha = case["base_sha"]
    _git(worktree, "reset", "-q", "--hard", str(base_sha))
    _commit(worktree, "shared.txt", "same patch\n", "branch duplicate")
    _git(worktree, "checkout", "-q", "-b", "side-work", str(base_sha))
    _commit(worktree, "shared.txt", "same patch\n", "side duplicate")
    _git(worktree, "checkout", "-q", "gc/merge-equal-tree")
    # Both sides carry the same patch: the merge resolution is identical to
    # the target tree even though the merge commit itself is branch-exclusive.
    _git(worktree, "merge", "--no-ff", "-q", "side-work", "-m", "merge same tree")

    decision = classify_git_worktree(
        worktree,
        "gc/merge-equal-tree",
        repo,
    )

    assert decision.verdict == REMOVE_PATCH_EQUIVALENT_KEEP_BRANCH
    assert decision.eligible is True
    assert decision.branch_exclusive_merge_count == 1


def test_assume_unchanged_masked_entry_fails_closed(tmp_path):
    """Blocker 3: assume-unchanged must not hide modifications."""
    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, "gc/assume-unchanged")
    (worktree / "base.txt").write_text("locally modified\n", encoding="utf-8")
    _git(worktree, "update-index", "--assume-unchanged", "base.txt")

    decision = classify_git_worktree(
        worktree,
        "gc/assume-unchanged",
        case["repo"],
    )

    assert decision.verdict == KEEP_UNCERTAIN
    assert decision.eligible is False
    assert decision.dirty is False  # status is genuinely clean...
    assert decision.index_masked_count == 1  # ...but the index lies.
    assert decision.reasons == ("index_masked_entries_present",)


def test_skip_worktree_masked_entry_fails_closed(tmp_path):
    """Blocker 3: skip-worktree must not hide modifications."""
    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, "gc/skip-worktree")
    (worktree / "base.txt").write_text("locally modified\n", encoding="utf-8")
    _git(worktree, "update-index", "--skip-worktree", "base.txt")

    decision = classify_git_worktree(
        worktree,
        "gc/skip-worktree",
        case["repo"],
    )

    assert decision.verdict == KEEP_UNCERTAIN
    assert decision.eligible is False
    assert decision.index_masked_count == 1
    assert decision.reasons == ("index_masked_entries_present",)


def test_submodule_gitlink_fails_closed_as_uncertain(tmp_path):
    """Blocker 4: gitlink contents are invisible to top-level probes."""
    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, "gc/with-submodule")

    # Register a gitlink without cloning anything: the top-level status and
    # ignored-file probes stay clean and blind to the submodule content.
    gitlink_oid = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    _git(
        worktree,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{gitlink_oid},submod",
    )
    _git(worktree, "commit", "-q", "-m", "add gitlink")

    decision = classify_git_worktree(
        worktree,
        "gc/with-submodule",
        case["repo"],
    )

    assert decision.verdict == KEEP_UNCERTAIN
    assert decision.eligible is False
    assert decision.submodule_count == 1
    assert decision.reasons == ("submodules_present",)


def test_moved_target_ref_between_status_and_decision_blocks_eligibility(
    tmp_path,
    monkeypatch,
):
    """Blocker 5: a candidate ref moved after the clean read must fail closed."""
    import api.worktree_gc_git as gc_git

    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, "gc/toctou")
    repo = case["repo"]
    assert isinstance(repo, Path)

    real_run_git = gc_git._run_git
    moved = {"done": False}

    def moving_run_git(args, cwd, *, timeout=gc_git.GIT_TIMEOUT):
        # After the status read and branch pin, silently move the branch ref
        # so the clean evidence no longer matches the published decision.
        if (
            not moved["done"]
            and args[:2] == ["merge-base", "--is-ancestor"]
        ):
            moved["done"] = True
            _git(
                repo,
                "update-ref",
                "refs/heads/gc/toctou",
                str(case["target_sha"]),
            )
        return real_run_git(args, cwd, timeout=timeout)

    monkeypatch.setattr(gc_git, "_run_git", moving_run_git)

    decision = classify_git_worktree(worktree, "gc/toctou", repo)

    assert decision.verdict == KEEP_UNCERTAIN
    assert decision.eligible is False
    assert "pin_revalidation_failed" in decision.reasons


def test_stable_refs_still_publish_eligibility_after_revalidation(tmp_path):
    """Blocker 5 baseline: unchanged pins must not block eligibility."""
    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, "gc/toctou-stable")

    decision = classify_git_worktree(
        worktree,
        "gc/toctou-stable",
        case["repo"],
    )

    assert decision.verdict == REMOVE_ANCESTOR
    assert decision.eligible is True


def test_worktree_head_move_between_status_and_decision_blocks(tmp_path, monkeypatch):
    """Blocker 5: worktree HEAD moved after the status read must fail closed."""
    import api.worktree_gc_git as gc_git

    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, "gc/toctou-head")
    repo = case["repo"]
    assert isinstance(repo, Path)

    real_run_git = gc_git._run_git
    moved = {"done": False}

    def moving_run_git(args, cwd, *, timeout=gc_git.GIT_TIMEOUT):
        if (
            not moved["done"]
            and args[:2] == ["merge-base", "--is-ancestor"]
        ):
            moved["done"] = True
            _git(worktree, "reset", "-q", "--hard", str(case["target_sha"]))
        return real_run_git(args, cwd, timeout=timeout)

    monkeypatch.setattr(gc_git, "_run_git", moving_run_git)

    decision = classify_git_worktree(worktree, "gc/toctou-head", repo)

    assert decision.verdict == KEEP_UNCERTAIN
    assert decision.eligible is False
    assert "pin_revalidation_failed" in decision.reasons
