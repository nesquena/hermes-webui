from __future__ import annotations

import os
import time
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
    settle_index_clock,
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
    settle_index_clock(worktree)

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
    settle_index_clock(worktree)

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
    assert decision.dirty is True  # stat evidence disagrees with the index...
    assert decision.index_masked_count == 1  # ...and the index lies about it.
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


# ---------------------------------------------------------------------------
# Round-2 gate blockers (2026-09-19): no repository program may execute, and
# no state outside the report may be overwritten.
# ---------------------------------------------------------------------------


def test_classification_never_executes_repository_programs(tmp_path):
    """Round-2 blocker 1: clean filters and diff drivers must never run.

    The control half proves the repository configuration is live: ordinary
    porcelain Git executes it.  The audit must classify the same worktree
    without executing anything the repository configured.
    """
    case = make_remote_repo(tmp_path)
    repo = case["repo"]
    assert isinstance(repo, Path)
    marker = tmp_path / "filter-executed.marker"
    driver_marker = tmp_path / "diff-driver-executed.marker"
    (repo / ".gitattributes").write_text(
        "*.txt filter=gatemark diff=gatemark\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-m", "repository-controlled attributes")
    _git(repo, "push", "origin", "master")
    _git(
        repo,
        "config",
        "filter.gatemark.clean",
        f"touch {marker}; cat",
    )
    _git(
        repo,
        "config",
        "diff.gatemark.command",
        f"touch {driver_marker}; true",
    )
    worktree = add_worktree(
        case,
        tmp_path,
        "gc/hostile-config",
        start="origin/master",
    )

    decision = classify_git_worktree(worktree, "gc/hostile-config", repo)

    assert decision.verdict == REMOVE_ANCESTOR
    assert decision.eligible is True
    assert not marker.exists(), "classification executed a clean filter"
    assert not driver_marker.exists(), "classification executed a diff driver"

    # Control: the same repository executes its configured programs under
    # ordinary porcelain Git, proving the harness would catch execution.
    (worktree / "base.txt").write_text("modified\n", encoding="utf-8")
    _git(worktree, "status", "--porcelain")
    _git(worktree, "diff", "--", "base.txt")
    assert marker.exists(), "control probe: clean filter did not run"
    assert driver_marker.exists(), "control probe: diff driver did not run"


def test_masked_index_lowercase_and_combined_tags_fail_closed(tmp_path):
    """Round-2: masked tags beyond ``h``/``S`` must be caught at the bit level."""
    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, "gc/masked-combined")
    # skip-worktree + assume-unchanged combined shows as lowercase ``s`` in
    # ls-files -v, which a tag-letter parser can miss entirely.
    _git(
        worktree,
        "update-index",
        "--skip-worktree",
        "--assume-unchanged",
        "base.txt",
    )

    decision = classify_git_worktree(
        worktree,
        "gc/masked-combined",
        case["repo"],
    )

    assert decision.verdict == KEEP_UNCERTAIN
    assert decision.eligible is False
    assert decision.index_masked_count == 1
    assert decision.reasons == ("index_masked_entries_present",)


def test_tracked_mutation_after_first_scan_blocks_eligibility(
    tmp_path,
    monkeypatch,
):
    """Round-2: eligibility must not survive a mutation after the clean read."""
    import api.worktree_gc_git as gc_git

    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, "gc/toctou-content")
    repo = case["repo"]
    assert isinstance(repo, Path)

    real_run_git = gc_git._run_git
    mutated = {"done": False}

    def mutating_run_git(args, cwd, *, timeout=gc_git.GIT_TIMEOUT):
        if (
            not mutated["done"]
            and args[:2] == ["merge-base", "--is-ancestor"]
        ):
            mutated["done"] = True
            # The clean scan already ran; mutate a tracked file now.
            (worktree / "base.txt").write_text(
                "mutated after the scan\n",
                encoding="utf-8",
            )
        return real_run_git(args, cwd, timeout=timeout)

    monkeypatch.setattr(gc_git, "_run_git", mutating_run_git)

    decision = classify_git_worktree(worktree, "gc/toctou-content", repo)

    assert mutated["done"] is True
    assert decision.verdict == KEEP_UNCERTAIN
    assert decision.eligible is False
    assert "pin_revalidation_failed" in decision.reasons


def test_racy_index_entry_is_dirty_without_content_hashing(tmp_path):
    """Racy entries fail closed: hashing them would invoke clean filters."""
    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, "gc/racy")
    # Deterministic raciness: index timestamp at or below the entry mtime.
    import tests.test_worktree_gc_git_classification as helpers

    index = helpers._index_path(worktree)
    past = time.time() - 3600
    os.utime(index, (past, past))

    decision = classify_git_worktree(worktree, "gc/racy", case["repo"])

    assert decision.verdict == "KEEP_DIRTY"
    assert decision.eligible is False
    assert decision.dirty is True


def test_sha256_repository_classifies_end_to_end(tmp_path):
    """Round-2: SHA-256 object format must not be rejected by 40-hex checks."""
    remote = tmp_path / "origin256.git"
    repo = tmp_path / "repo256"
    remote.mkdir()
    repo.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=master", "--object-format=sha256")
    _git(repo, "init", "--initial-branch=master", "--object-format=sha256")
    _git(repo, "config", "user.email", "gc-tests@example.invalid")
    _git(repo, "config", "user.name", "Worktree GC Tests")
    _commit(repo, "base.txt", "base\n", "base")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "master")
    worktree = tmp_path / "wt256"
    _git(repo, "worktree", "add", "-b", "gc/sha256", str(worktree), "master")
    settle_index_clock(worktree)

    head = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    assert len(head) == 64

    decision = classify_git_worktree(
        worktree,
        "gc/sha256",
        repo,
        target_ref="master",
    )

    assert decision.verdict == REMOVE_ANCESTOR
    assert decision.eligible is True
    assert decision.ancestor_of_target is True


def test_newline_worktree_path_is_classified_not_rejected(tmp_path):
    """Round-2: NUL-delimited worktree listing must survive newline paths."""
    case = make_remote_repo(tmp_path)
    repo = case["repo"]
    assert isinstance(repo, Path)
    worktree = tmp_path / "weird" / "wt\nnewline"
    worktree.parent.mkdir()
    _git(
        repo,
        "worktree",
        "add",
        "-b",
        "gc/newline-path",
        str(worktree),
        str(case["base_sha"]),
    )
    settle_index_clock(worktree)

    decision = classify_git_worktree(worktree, "gc/newline-path", repo)

    assert decision.listed is True
    assert decision.verdict == REMOVE_ANCESTOR
    assert decision.eligible is True


def test_oversized_git_output_fails_closed_instead_of_consuming_memory(
    tmp_path,
    monkeypatch,
):
    """Round-2: every Git output is hard-bounded, not just a few probes."""
    import api.worktree_gc_git as gc_git

    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, "gc/oversized")
    monkeypatch.setattr(gc_git, "_GIT_OUTPUT_LIMIT", 8)

    decision = classify_git_worktree(worktree, "gc/oversized", case["repo"])

    assert decision.verdict == KEEP_UNCERTAIN
    assert decision.eligible is False
    assert "git_output_oversized" in decision.reasons


def test_dirty_path_stored_only_in_split_index_fails_closed(tmp_path):
    """Round-3: a shared-index-only path must never disappear from the scan."""
    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, "gc/split-index-dirty")
    _git(worktree, "update-index", "--split-index")
    settle_index_clock(worktree)
    (worktree / "base.txt").write_text("hidden local edit\n", encoding="utf-8")

    assert _git(worktree, "status", "--porcelain").stdout.strip() == "M base.txt"

    decision = classify_git_worktree(
        worktree,
        "gc/split-index-dirty",
        case["repo"],
    )

    assert decision.verdict == KEEP_UNCERTAIN
    assert decision.eligible is False
    assert decision.reasons == ("split_index_present",)


def test_clean_split_index_fails_closed(tmp_path):
    """Round-3: split-index composition is unsupported even when currently clean."""
    case = make_remote_repo(tmp_path)
    worktree = add_worktree(case, tmp_path, "gc/split-index-clean")
    _git(worktree, "update-index", "--split-index")
    settle_index_clock(worktree)

    decision = classify_git_worktree(
        worktree,
        "gc/split-index-clean",
        case["repo"],
    )

    assert decision.verdict == KEEP_UNCERTAIN
    assert decision.eligible is False
    assert decision.reasons == ("split_index_present",)


def test_locked_worktree_is_never_eligible(tmp_path):
    """Round-3: an operator lock is preservation authority, not decoration."""
    case = make_remote_repo(tmp_path)
    repo = case["repo"]
    assert isinstance(repo, Path)
    worktree = add_worktree(case, tmp_path, "gc/locked")
    _git(repo, "worktree", "lock", "--reason", "operator preservation", str(worktree))

    decision = classify_git_worktree(worktree, "gc/locked", repo)

    assert decision.verdict == KEEP_UNCERTAIN
    assert decision.eligible is False
    assert decision.reasons == ("worktree_locked",)


def test_lock_added_after_initial_scan_invalidates_eligibility(tmp_path, monkeypatch):
    """Round-3: final pin validation must observe a concurrent operator lock."""
    import api.worktree_gc_git as gc_git

    case = make_remote_repo(tmp_path)
    repo = case["repo"]
    assert isinstance(repo, Path)
    worktree = add_worktree(case, tmp_path, "gc/lock-toctou")
    real_run_git = gc_git._run_git
    locked = {"done": False}

    def locking_run_git(args, cwd, *, timeout=gc_git.GIT_TIMEOUT):
        if not locked["done"] and args[:2] == ["merge-base", "--is-ancestor"]:
            locked["done"] = True
            _git(repo, "worktree", "lock", "--reason", "concurrent hold", str(worktree))
        return real_run_git(args, cwd, timeout=timeout)

    monkeypatch.setattr(gc_git, "_run_git", locking_run_git)

    decision = classify_git_worktree(worktree, "gc/lock-toctou", repo)

    assert locked["done"] is True
    assert decision.verdict == KEEP_UNCERTAIN
    assert decision.eligible is False
    assert "pin_revalidation_failed" in decision.reasons
