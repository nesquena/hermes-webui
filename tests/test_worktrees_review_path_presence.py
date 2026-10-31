"""Regressions for the maintainer's path-presence re-gate on #8162.

Two previous-green regressions, both caused by deciding ownership from the
candidate's *contents*:

1. ``_tracks_paths_in_parent()`` treated "every tracked path exists here" as
   positive ownership. A ``--separate-git-dir`` storage dir holding an operator
   note that reuses a tracked filename therefore owned the metadata as soon as
   the real checkout sat beyond the gitfile search bound, and the Agent created
   ``storage-only/.worktrees/hermes-*``.
2. The same full-path existence test *revoked* an ordinary primary's ownership
   when a tracked nested leaf was deleted (``src/second.txt`` gone, ``src/``
   still there), so a linked session nested under ``linked/.worktrees`` again.

Ownership now comes only from a gitfile claim on the metadata, or — for a plain
repository — from the recorded ``<tree>/.git`` layout, and that layout is only
trusted when the claim search finished without being truncated. These are real
``git init`` / ``git worktree add`` fixtures asserting the actual allocation.
"""

import os
import subprocess
from pathlib import Path

import pytest


def _git(*args, cwd=None, check=True):
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result


@pytest.fixture()
def git_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig-none"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return tmp_path


def _import():
    from api.worktrees import find_main_worktree_root

    return find_main_worktree_root


def _stub_agent_setup(monkeypatch, created_root):
    """Record the root WebUI hands down and place the tree where the Agent would.

    The Agent helper is exercised end-to-end elsewhere; the contract under test
    here is which root reaches it and where the session tree lands.
    """
    import api.worktrees as worktrees

    seen = {}

    def fake(repo_root):
        seen["repo_root"] = repo_root
        tree = Path(repo_root) / ".worktrees" / "hermes-test"
        tree.mkdir(parents=True, exist_ok=True)
        created_root.append(tree)
        return {"path": str(tree), "branch": "hermes-test", "repo_root": str(repo_root)}

    monkeypatch.setattr(worktrees, "_setup_agent_worktree", fake)
    return seen


def _allocate(monkeypatch, workspace):
    """Run the real ``create_worktree_for_workspace`` and report what it did."""
    created = []
    seen = _stub_agent_setup(monkeypatch, created)
    from api.worktrees import create_worktree_for_workspace

    info = create_worktree_for_workspace(workspace)
    return seen["repo_root"], Path(info["path"]).resolve(), created


# --- 1. Storage immediately beyond the search bound --------------------------
# The maintainer's literal scene. The checkout is one level below the gitfile
# search bound, the metadata is named ``.git`` inside a storage dir, and the
# storage dir holds unrelated content under a *tracked* filename. The bound does
# not decide ownership: on truncation the answer is unknown, never storage.


def _storage_beyond_bound(base, names=("tracked.txt",)):
    checkout = base.joinpath("elsewhere", "d0", "d1", "d2", "d3", "d4", "checkout")
    storage = base / "storage-only"
    checkout.mkdir(parents=True)
    storage.mkdir(parents=True)
    _git(
        "init",
        "-q",
        "-b",
        "main",
        f"--separate-git-dir={storage / '.git'}",
        cwd=checkout,
    )
    for name in names:
        (checkout / name).write_text(f"committed {name}\n", encoding="utf-8")
        _git("add", name, cwd=checkout)
    _git("commit", "-q", "-m", "init", cwd=checkout)
    linked = base / "linked"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=checkout)
    nested = linked / "nested"
    nested.mkdir()
    return checkout, storage, linked, nested


def test_storage_with_one_colliding_tracked_name_never_owns(git_env, monkeypatch):
    """Unrelated operator notes under a tracked filename are not ownership."""
    base = git_env / "presence-one"
    checkout, storage, linked, nested = _storage_beyond_bound(base)
    # The scene: only the tracked *filename* is reused, not its content.
    (storage / "tracked.txt").write_text("unrelated operator note\n", encoding="utf-8")

    committed = _git("rev-parse", "HEAD", cwd=checkout).stdout.strip()
    assert (checkout / "tracked.txt").read_text(encoding="utf-8") == "committed tracked.txt\n"

    find = _import()
    for workspace in (checkout, linked, nested):
        root = find(workspace)
        assert root != storage.resolve(), (
            f"storage must never be answered as a project root (input {workspace})"
        )
        assert root is None or root == checkout.resolve()

    root, allocated, created = _allocate(monkeypatch, nested)
    assert Path(root).resolve() != storage.resolve(), "the Agent must never be handed storage"
    assert not allocated.is_relative_to(storage.resolve())
    # The real checkout is within reach, so the tree lands under it.
    assert Path(root).resolve() == checkout.resolve()
    assert allocated.parent == (checkout / ".worktrees").resolve()
    assert created and created[0] == allocated
    # The fixture is untouched: HEAD and the operator note survive.
    assert _git("rev-parse", "HEAD", cwd=checkout).stdout.strip() == committed
    assert (storage / "tracked.txt").read_text(encoding="utf-8") == "unrelated operator note\n"


def test_storage_with_two_colliding_tracked_names_never_owns(git_env, monkeypatch):
    """Two unrelated colliding filenames: still a copy, never the checkout."""
    base = git_env / "presence-two"
    checkout, storage, linked, nested = _storage_beyond_bound(
        base, names=("tracked.txt", "second.txt")
    )
    for name in ("tracked.txt", "second.txt"):
        (storage / name).write_text(f"unrelated notes about {name}\n", encoding="utf-8")

    find = _import()
    for workspace in (linked, nested):
        root = find(workspace)
        assert root != storage.resolve()

    root, allocated, _ = _allocate(monkeypatch, nested)
    assert Path(root).resolve() == checkout.resolve()
    assert not allocated.is_relative_to(storage.resolve())
    assert allocated.parent == (checkout / ".worktrees").resolve()


def test_storage_with_an_identical_copy_and_no_index_refresh_never_owns(
    git_env, monkeypatch
):
    """One identical file plus an unrelated second — no index refresh at all.

    A storage dir that reproduces part of the committed tree byte for byte is
    still not the checkout: nothing about its contents or its stat records is
    ownership, so no ``status`` refresh is needed to get this right.
    """
    base = git_env / "presence-identical"
    checkout, storage, linked, nested = _storage_beyond_bound(
        base, names=("tracked.txt", "second.txt")
    )
    (storage / "tracked.txt").write_text(
        (checkout / "tracked.txt").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (storage / "second.txt").write_text("unrelated operator notes\n", encoding="utf-8")

    find = _import()
    assert find(nested) == checkout.resolve()

    root, allocated, _ = _allocate(monkeypatch, nested)
    assert Path(root).resolve() == checkout.resolve()
    assert not allocated.is_relative_to(storage.resolve())


# --- 2. An ordinary primary that deleted a tracked nested leaf ---------------
# Deleting a tracked file is uncommitted state, not a change of ownership: the
# primary still owns its repository and the session tree must land under it.


def _plain_primary_with_deleted_leaf(base, *, remove_dir):
    primary = base / "primary"
    (primary / "src").mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=primary)
    (primary / "tracked.txt").write_text("committed tracked.txt\n", encoding="utf-8")
    (primary / "src" / "second.txt").write_text("committed second\n", encoding="utf-8")
    _git("add", "tracked.txt", "src/second.txt", cwd=primary)
    _git("commit", "-q", "-m", "init", cwd=primary)
    linked = base / "linked"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=primary)
    nested = linked / "nested"
    nested.mkdir()
    # Delete only the nested leaf; ``src/`` stays (or goes, for the deeper case).
    (primary / "src" / "second.txt").unlink()
    if remove_dir:
        (primary / "src").rmdir()
    return primary, linked, nested


@pytest.mark.parametrize("remove_dir", [False, True], ids=["src-kept", "src-removed"])
def test_deleted_nested_tracked_leaf_keeps_primary_ownership(
    git_env, monkeypatch, remove_dir
):
    """From either entry point the primary is still the primary."""
    primary, linked, nested = _plain_primary_with_deleted_leaf(
        git_env / f"deleted-leaf-{remove_dir}", remove_dir=remove_dir
    )
    assert (primary / "tracked.txt").exists(), "the created tree is checked"

    find = _import()
    for workspace in (linked, nested):
        assert find(workspace) == primary.resolve(), f"input {workspace}"

    for workspace in (linked, nested):
        root, allocated, created = _allocate(monkeypatch, workspace)
        assert Path(root).resolve() == primary.resolve()
        assert allocated.parent == (primary / ".worktrees").resolve()
        assert not allocated.is_relative_to(linked.resolve())
        assert created and created[0] == allocated

    # Still the same repository, and the deletion is still the only change.
    assert _git("rev-parse", "HEAD", cwd=primary).returncode == 0
    status = _git("status", "--porcelain", cwd=primary).stdout
    assert "src/second.txt" in status


# --- 3. The claim bound: allocation immediately inside and outside ----------
# The bound no longer grants ownership: inside it the real checkout is found;
# outside it the answer is unknown (never the storage dir).


def test_allocation_immediately_inside_and_outside_the_claim_bound(git_env, monkeypatch):
    import api.worktrees as worktrees

    cap = worktrees._CHECKOUT_LISTING_DEPTH
    inside = tuple(f"lvl{i}" for i in range(cap - 1))  # checkout sits at level cap
    outside = tuple(f"lvl{i}" for i in range(cap))  # one level past the cap

    for label, levels in (("inside", inside), ("outside", outside)):
        base = git_env / f"bound-{label}"
        checkout = base.joinpath(*levels, "checkout")
        storage = base / "storage"
        checkout.mkdir(parents=True)
        storage.mkdir(parents=True)
        _git(
            "init",
            "-q",
            "-b",
            "main",
            f"--separate-git-dir={storage / '.git'}",
            cwd=checkout,
        )
        (checkout / "tracked.txt").write_text("committed\n", encoding="utf-8")
        _git("add", "tracked.txt", cwd=checkout)
        _git("commit", "-q", "-m", "init", cwd=checkout)
        linked = base / "linked"
        _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=checkout)
        # Storage collides with the tracked filename in both arms.
        (storage / "tracked.txt").write_text("unrelated operator note\n", encoding="utf-8")

        find = _import()
        root = find(linked)
        assert root != storage.resolve(), f"{label}: storage must never be answered"
        if label == "inside":
            assert root == checkout.resolve(), f"{label}: the checkout is within reach"
        else:
            assert root is None, f"{label}: a truncated search is unknown ownership"

        repo_root, allocated, _ = _allocate(monkeypatch, linked)
        assert Path(repo_root).resolve() != storage.resolve(), label
        assert not allocated.is_relative_to(storage.resolve()), label
        assert (checkout / "tracked.txt").exists(), label


# --- 4. Retained traversal bound -------------------------------------------
# The claim search does not walk into other checkouts, so a repository full of
# session trees costs one listing per tree, not a full recursive walk.


def test_claim_search_does_not_descend_into_session_trees(git_env, monkeypatch):
    base = git_env / "session-trees"
    primary = base / "primary"
    primary.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=primary)
    (primary / "tracked.txt").write_text("committed\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=primary)
    _git("commit", "-q", "-m", "init", cwd=primary)
    linked = base / "linked"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=primary)
    # Five real session worktrees, each with a deep subtree of its own.
    for index in range(5):
        tree = primary / ".worktrees" / f"hermes-{index:x}"
        _git("worktree", "add", "-q", "-b", f"sess{index}", str(tree), cwd=primary)
        tree.joinpath(*(f"d{level}" for level in range(10))).mkdir(parents=True)

    import api.worktrees as worktrees

    real_scandir = os.scandir
    visited: list[str] = []

    def counting_scandir(path="."):
        visited.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(worktrees.os, "scandir", counting_scandir)

    assert worktrees.find_main_worktree_root(linked) == primary.resolve()
    # One listing per session tree at most, plus the shallow repo levels.
    assert len(visited) < 20, f"{len(visited)} listings: the search descended into a checkout"
    assert not any("hermes-0" in path for path in visited), visited[:10]
