
"""Regressions for the maintainer's re-gate 5565346417 on #8162."""

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


# --- Maintainer re-gate 5565346417: content-independent ownership ---------------
# Four objective failures on the previous head, all traced to the *content*
# discriminator (`diff-files` divergence) and to its stat dependence:
#   1. an ordinary primary whose every tracked file was edited lost ownership;
#   2. a same-content rewrite or a bare mtime `touch` also lost it, because
#      `diff-files` was read without refreshing the index;
#   3. an OSError/timeout of that optional query escaped `create_worktree_*`;
#   4. a partial copy in a separate storage dir, after a real `status` refresh
#      changed the index stat records, *gained* ownership.
# The owned behaviour is that content is never evidence: ownership comes from the
# gitfile claim that a storage dir cannot make, or — for a plain repository — from
# the recorded `<tree>/.git` layout once the claim search has finished. No index
# query, stat record or per-path existence test takes part. These tests assert
# allocation, not just the resolver.


def _ordinary_primary_single(base):
    """Primary tracking exactly one file, plus a linked worktree nested in it."""
    primary = base / "primary"
    primary.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=primary)
    (primary / "only.txt").write_text("committed\n", encoding="utf-8")
    _git("add", "only.txt", cwd=primary)
    _git("commit", "-q", "-m", "init", cwd=primary)
    linked = primary / "linked"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=primary)
    nested = linked / "nested"
    nested.mkdir()
    return primary, linked, nested


def test_modified_single_file_primary_still_owns_the_repository(git_env, monkeypatch):
    """Vector 1: one tracked file, then edited. It is still the primary."""
    primary, linked, nested = _ordinary_primary_single(git_env / "modified-single")
    (primary / "only.txt").write_text("modified content\n", encoding="utf-8")

    find = _import()
    assert find(linked) == primary.resolve()
    assert find(nested) == primary.resolve()

    created = []
    seen = _stub_agent_setup(monkeypatch, created)
    from api.worktrees import create_worktree_for_workspace

    info = create_worktree_for_workspace(nested)
    assert Path(seen["repo_root"]).resolve() == primary.resolve()
    allocated = Path(info["path"]).resolve()
    assert allocated.parent == (primary / ".worktrees").resolve()
    assert not allocated.is_relative_to(linked.resolve())


@pytest.mark.parametrize("variant", ["rewrite-same-content", "touch-mtime", "edit-every-file"])
def test_uncommitted_state_never_revokes_ownership(git_env, monkeypatch, variant):
    """Vectors 1+2: content-identical or stat-only change keeps the primary.

    `touch` and a same-content rewrite change no tracked byte; editing the only
    tracked file changes bytes but no ownership. Neither may be read as a
    difference, so neither may send the tree into the linked checkout.
    """
    import os

    primary, linked, nested = _ordinary_primary_single(git_env / f"state-{variant}")
    target = primary / "only.txt"
    if variant == "rewrite-same-content":
        target.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    elif variant == "touch-mtime":
        st = target.stat()
        os.utime(target, (st.st_atime + 10_000, st.st_mtime + 10_000))
    else:
        target.write_text("entirely different\n", encoding="utf-8")

    created = []
    seen = _stub_agent_setup(monkeypatch, created)
    from api.worktrees import create_worktree_for_workspace

    info = create_worktree_for_workspace(nested)
    assert Path(seen["repo_root"]).resolve() == primary.resolve(), variant
    assert not Path(info["path"]).resolve().is_relative_to(linked.resolve()), variant


def _two_file_primary(base):
    """Primary tracking two top-level files, with a linked checkout outside it."""
    primary = base / "primary"
    primary.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=primary)
    for name in ("a.txt", "b.txt"):
        (primary / name).write_text(f"committed {name}\n", encoding="utf-8")
        _git("add", name, cwd=primary)
    _git("commit", "-q", "-m", "init", cwd=primary)
    linked = base / "linked"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=primary)
    nested = linked / "nested"
    nested.mkdir()
    return primary, linked, nested


def test_every_tracked_file_edited_still_owns(git_env, monkeypatch):
    """Vector 1: editing *all* tracked files does not revoke ownership."""
    primary, linked, nested = _two_file_primary(git_env / "edit-all")
    for name in ("a.txt", "b.txt"):
        (primary / name).write_text(f"edited {name}\n", encoding="utf-8")

    created = []
    seen = _stub_agent_setup(monkeypatch, created)
    from api.worktrees import create_worktree_for_workspace

    info = create_worktree_for_workspace(nested)
    assert Path(seen["repo_root"]).resolve() == primary.resolve()
    assert Path(info["path"]).resolve().is_relative_to(primary.resolve())
    assert not Path(info["path"]).resolve().is_relative_to(linked.resolve())


def test_tracked_file_count_does_not_decide_ownership(git_env, monkeypatch):
    """Vector 1: file counts describe different domains and are not ownership."""
    primary, linked, nested = _two_file_primary(git_env / "counts")
    # Two modified files under one directory: one path component, two paths.
    (primary / "src").mkdir()
    for name in ("x.py", "y.py"):
        (primary / "src" / name).write_text(f"committed {name}\n", encoding="utf-8")
        _git("add", f"src/{name}", cwd=primary)
    _git("commit", "-q", "-m", "src", cwd=primary)
    for name in ("x.py", "y.py"):
        (primary / "src" / name).write_text(f"edited {name}\n", encoding="utf-8")

    created = []
    seen = _stub_agent_setup(monkeypatch, created)
    from api.worktrees import create_worktree_for_workspace

    info = create_worktree_for_workspace(nested)
    assert Path(seen["repo_root"]).resolve() == primary.resolve()
    assert not Path(info["path"]).resolve().is_relative_to(linked.resolve())


@pytest.mark.parametrize("failure", ["oserror", "timeout", "returncode"])
def test_failed_ownership_query_never_aborts_creation(git_env, monkeypatch, failure):
    """Vector 3: a failing optional query must not raise before allocation.

    The removed `status` fault injection proved nothing about the replacement
    query; this injects on whatever query the ownership rule actually runs, and
    asserts the creation still completes with the ordinary primary.
    """
    import api.worktrees as worktrees

    primary, linked, nested = _two_file_primary(git_env / f"ownfail-{failure}")
    (primary / "a.txt").write_text("edited\n", encoding="utf-8")
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and any(
            token in str(c) for c in cmd for token in ("diff-files", "ls-files", "status")
        ):
            if failure == "oserror":
                raise OSError("injected: query unavailable")
            if failure == "timeout":
                raise subprocess.TimeoutExpired(cmd="git", timeout=2)
            return subprocess.CompletedProcess(
                args=list(cmd), returncode=128, stdout="", stderr="boom"
            )
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(worktrees.subprocess, "run", fake_run)

    created = []
    seen = _stub_agent_setup(monkeypatch, created)
    from api.worktrees import create_worktree_for_workspace

    info = create_worktree_for_workspace(nested)  # must not raise
    root = Path(seen["repo_root"]).resolve()
    # Unknown ownership keeps the caller's own checkout: nesting at worst.
    assert root in (primary.resolve(), linked.resolve()), failure
    assert Path(info["path"]).resolve().is_relative_to(root)


def _depth6_partial_copy(base):
    """The maintainer's depth-6 fixture: checkout 6 levels down, storage aside."""
    checkout = base.joinpath("elsewhere", "d0", "d1", "d2", "d3", "checkout")
    storage = base / "storage-only"
    checkout.mkdir(parents=True)
    storage.mkdir(parents=True)
    _git("init", "-q", "-b", "main", f"--separate-git-dir={storage / '.git'}", cwd=checkout)
    for name in ("tracked.txt", "second.txt"):
        (checkout / name).write_text(f"committed {name}\n", encoding="utf-8")
        _git("add", name, cwd=checkout)
    _git("commit", "-q", "-m", "init", cwd=checkout)
    linked = base / "linked"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=checkout)
    nested = linked / "nested"
    nested.mkdir()
    return checkout, storage, linked, nested


def test_depth6_partial_copy_storage_never_gets_the_tree(git_env, monkeypatch):
    """Vector 4: an identical copy of *one* file plus a colliding note.

    A real `status --porcelain` in storage runs first, exactly as the maintainer
    does: it refreshes the matching file's stat record, so a subsequent
    `diff-files` reports only the differing path and an earlier ratio accepted
    storage. Ownership must not depend on that; the checkout's gitfile claims
    the metadata and storage cannot.
    """
    checkout, storage, linked, nested = _depth6_partial_copy(git_env / "partial")
    (storage / "tracked.txt").write_text(
        (checkout / "tracked.txt").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (storage / "second.txt").write_text("operator notes\n", encoding="utf-8")
    # The real refresh the maintainer performs before observing the bug.
    _git("--git-dir", str(storage / ".git"), "--work-tree", str(storage),
         "status", "--porcelain", cwd=storage)

    find = _import()
    assert find(nested) != storage.resolve(), "storage must never be the root"
    assert find(nested) == checkout.resolve()

    created = []
    seen = _stub_agent_setup(monkeypatch, created)
    from api.worktrees import create_worktree_for_workspace

    info = create_worktree_for_workspace(nested)
    root = Path(seen["repo_root"]).resolve()
    assert root == checkout.resolve()
    assert root != storage.resolve()
    assert Path(info["path"]).resolve().is_relative_to(checkout.resolve())
    assert not Path(info["path"]).resolve().is_relative_to(storage.resolve())


def test_depth6_layout_requires_no_copy_at_all(git_env, monkeypatch):
    """Vector 4 negative: empty storage must not be crowned either."""
    checkout, storage, linked, nested = _depth6_partial_copy(git_env / "partial-empty")
    created = []
    seen = _stub_agent_setup(monkeypatch, created)
    from api.worktrees import create_worktree_for_workspace

    info = create_worktree_for_workspace(nested)
    assert Path(seen["repo_root"]).resolve() == checkout.resolve()
    assert not Path(info["path"]).resolve().is_relative_to(storage.resolve())
