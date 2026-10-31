"""Regressions for the maintainer's re-gate 5676719421 on #8162.

Two source-assembly findings against head ``0708fd704``:

1. base selection — the mirror read only the cached ``refs/remotes/origin/HEAD``
   symref, so a repository where that cache is absent (bare clone, ``git init``
   plus ``remote add``, deleted ``set-head``) fell through to the selected local
   HEAD and reset a tree the installed helper had already based on the remote
   default. The installed ``_resolve_worktree_base`` asks the remote itself
   (``git remote show origin``) with a bounded, freshness-aware refresh.
2. tracked-path guard — ``_copy_included_entry`` returned on ``HEAD``
   membership, but the Agent copies an include *before* this alignment runs, so
   a tracked path could already hold the placement checkout's dirty bytes and
   the early return kept them.

Both fixtures use real git and allocate through ``create_worktree_for_workspace``
with a stand-in for ``cli._setup_worktree`` that reproduces the installed
helper's own behaviour: ``git worktree add`` from the placement root, files
copied from that root's ``.worktreeinclude``.
"""

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


def _head(root, rev="HEAD"):
    return _git("rev-parse", rev, cwd=root).stdout.strip()


@pytest.fixture()
def git_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig-none"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return tmp_path


def _stub_helper(monkeypatch, *, link_dirs=False):
    """``cli._setup_worktree`` stand-in: placement, base and includes from one root."""
    import api.worktrees as worktrees

    def fake(repo_root):
        root = Path(repo_root)
        tree = root / ".worktrees" / "hermes-test"
        tree.parent.mkdir(parents=True, exist_ok=True)
        _git("worktree", "add", "-q", "-b", "hermes-test", str(tree), cwd=root)
        manifest = root / ".worktreeinclude"
        if manifest.exists():
            for raw in manifest.read_text(encoding="utf-8").splitlines():
                entry = raw.strip()
                if not entry or entry.startswith("#"):
                    continue
                source = root / entry
                if source.is_file():
                    (tree / entry).parent.mkdir(parents=True, exist_ok=True)
                    (tree / entry).write_bytes(source.read_bytes())
                elif source.is_dir() and link_dirs and not (tree / entry).exists():
                    (tree / entry).parent.mkdir(parents=True, exist_ok=True)
                    (tree / entry).symlink_to(source.resolve())
        return {"path": str(tree), "branch": "hermes-test", "repo_root": str(root)}

    monkeypatch.setattr(worktrees, "_setup_agent_worktree", fake)


def _shared_head_checkouts(base, name="shared"):
    """Primary and linked checkout on the same commit, both with a tracked file."""
    primary = base / name / "primary"
    primary.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=primary)
    (primary / "tracked.txt").write_text("committed\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=primary)
    _git("commit", "-q", "-m", "commit tracked.txt", cwd=primary)
    linked = primary / "linked"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=primary)
    nested = linked / "nested"
    nested.mkdir()
    assert _head(primary) == _head(linked)
    return primary, linked, nested


# --- finding 1: the remote default is discovered, not only read from a cache ---


def _origin_with_unpublished_branches(base):
    """Bare origin on ``main``; primary has an unpublished commit, linked too."""
    origin = base / "origin.git"
    _git("init", "-q", "--bare", "-b", "main", str(origin))
    seed = base / "seed"
    seed.mkdir()
    _git("init", "-q", "-b", "main", cwd=seed)
    (seed / "shared.txt").write_text("shared\n", encoding="utf-8")
    _git("add", "shared.txt", cwd=seed)
    _git("commit", "-q", "-m", "init", cwd=seed)
    _git("remote", "add", "origin", str(origin), cwd=seed)
    _git("push", "-q", "-u", "origin", "main", cwd=seed)
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=origin)

    primary = base / "primary"
    _git("clone", "-q", str(origin), str(primary))
    _git("config", "user.name", "t", cwd=primary)
    _git("config", "user.email", "t@example.com", cwd=primary)
    (primary / "primary-only.txt").write_text("unpublished\n", encoding="utf-8")
    _git("add", "primary-only.txt", cwd=primary)
    _git("commit", "-q", "-m", "primary unpublished", cwd=primary)

    linked = primary / "linked"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=primary)
    (linked / "side-only.txt").write_text("unpublished on side\n", encoding="utf-8")
    _git("add", "side-only.txt", cwd=linked)
    _git("commit", "-q", "-m", "side unpublished", cwd=linked)
    nested = linked / "nested"
    nested.mkdir()
    return origin, primary, linked, nested


def test_missing_origin_head_still_uses_the_remote_default(git_env, monkeypatch):
    """The uncached case: no ``origin/HEAD`` symref, base is still ``origin/main``."""
    origin, primary, linked, nested = _origin_with_unpublished_branches(git_env)
    expected = _git("rev-parse", "origin/main", cwd=primary).stdout.strip()
    assert expected != _head(linked), "fixture: side must be ahead of origin/main"
    _git("symbolic-ref", "--delete", "refs/remotes/origin/HEAD", cwd=primary)
    assert _git("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", cwd=primary,
                check=False).returncode != 0

    _stub_helper(monkeypatch)
    from api.worktrees import create_worktree_for_workspace

    tree = Path(create_worktree_for_workspace(nested)["path"]).resolve()

    assert _head(tree) == expected, (
        "a missing origin/HEAD cache must fall back to the remote's own default, "
        "not to the selected local HEAD"
    )
    assert not (tree / "side-only.txt").exists()
    assert not (tree / "primary-only.txt").exists()


def test_cached_origin_head_uses_the_remote_default(git_env, monkeypatch):
    """Positive control for the same fixture: with the cache present, base is ``origin/main``."""
    origin, primary, linked, nested = _origin_with_unpublished_branches(git_env)
    expected = _git("rev-parse", "origin/main", cwd=primary).stdout.strip()
    _git("update-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", cwd=primary)

    _stub_helper(monkeypatch)
    from api.worktrees import create_worktree_for_workspace

    tree = Path(create_worktree_for_workspace(nested)["path"]).resolve()

    assert _head(tree) == expected
    assert not (tree / "side-only.txt").exists()


# --- finding 2: HEAD membership is not proof the right bytes are in the tree ---


def test_dirty_primary_include_does_not_leave_dirty_bytes(git_env, monkeypatch):
    """Same HEAD, both manifests name a tracked file, only primary is dirty."""
    primary, linked, nested = _shared_head_checkouts(git_env, name="dirty-both")
    (primary / ".worktreeinclude").write_text("tracked.txt\n", encoding="utf-8")
    (linked / ".worktreeinclude").write_text("tracked.txt\n", encoding="utf-8")
    (primary / "tracked.txt").write_text("primary uncommitted bytes\n", encoding="utf-8")

    _stub_helper(monkeypatch)
    from api.worktrees import create_worktree_for_workspace

    tree = Path(create_worktree_for_workspace(nested)["path"]).resolve()

    assert (tree / "tracked.txt").read_text(encoding="utf-8") == "committed\n", (
        "the selected base's committed bytes must win over the primary's dirty copy"
    )
    status = _git("status", "--porcelain", cwd=tree).stdout
    assert "tracked.txt" not in status, f"new session tree must be clean, got: {status!r}"


def test_dirty_primary_only_include_still_restores_committed_bytes(git_env, monkeypatch):
    """The drop path has the same defect: tracked content is restored, not kept dirty."""
    primary, linked, nested = _shared_head_checkouts(git_env, name="dirty-primary-only")
    (primary / ".worktreeinclude").write_text("tracked.txt\n", encoding="utf-8")
    (primary / "tracked.txt").write_text("primary uncommitted bytes\n", encoding="utf-8")

    _stub_helper(monkeypatch)
    from api.worktrees import create_worktree_for_workspace

    tree = Path(create_worktree_for_workspace(nested)["path"]).resolve()

    assert (tree / "tracked.txt").read_text(encoding="utf-8") == "committed\n"
    status = _git("status", "--porcelain", cwd=tree).stdout
    assert "tracked.txt" not in status, f"new session tree must be clean, got: {status!r}"


def test_untracked_primary_only_include_is_still_dropped(git_env, monkeypatch):
    """Control: an include that is not committed content is still removed."""
    primary, linked, nested = _shared_head_checkouts(git_env, name="untracked-control")
    (primary / ".worktreeinclude").write_text("local.conf\n", encoding="utf-8")
    (primary / "local.conf").write_text("primary setting\n", encoding="utf-8")

    _stub_helper(monkeypatch)
    from api.worktrees import create_worktree_for_workspace

    tree = Path(create_worktree_for_workspace(nested)["path"]).resolve()

    assert not (tree / "local.conf").exists()
