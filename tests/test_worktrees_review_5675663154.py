"""Regressions for the maintainer's re-gate 5675663154 on #8162.

Three findings against the head that installed the selected commit in the new
tree, all about what "our copy" means once committed content is in the tree:

1. include cleanup could delete or replace the selected commit's own tree —
   a manifest line is not proof the Agent copied the path;
2. directory includes still read primary: the destination is a symlink, so a
   follow-the-symlink containment check rejected it and left the link in place;
3. equal commit ids were read as "the Agent fell back to local HEAD", resetting
   a tree the Agent had correctly based on the selected branch's upstream.

The fixtures use real git. The stand-ins reproduce the installed helper's own
behaviour — ``git worktree add`` from the placement root, files copied from its
``.worktreeinclude``, directories symlinked back to it — because the defects are
in what WebUI's alignment step does *after* that.
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


def _primary_with_linked_checkout(base, name="repo"):
    """Primary on ``main`` with a linked checkout on ``side`` nested inside it."""
    primary = base / name / "primary"
    primary.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=primary)
    (primary / "shared.txt").write_text("shared\n", encoding="utf-8")
    _git("add", "shared.txt", cwd=primary)
    _git("commit", "-q", "-m", "init", cwd=primary)
    linked = primary / "linked"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=primary)
    nested = linked / "nested"
    nested.mkdir()
    return primary, linked, nested


# --- finding 1: a manifest line does not authorise deleting committed content ---


def test_manifest_entry_naming_a_committed_file_keeps_the_commit(git_env, monkeypatch):
    """Same HEAD on both checkouts, primary names a tracked file: nothing is deleted."""
    primary, linked, nested = _primary_with_linked_checkout(git_env, name="committed-file")
    (linked / "tracked.txt").write_text("committed on side\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=linked)
    _git("commit", "-q", "-m", "side commits tracked.txt", cwd=linked)
    (primary / ".worktreeinclude").write_text("tracked.txt\n", encoding="utf-8")

    _stub_helper(monkeypatch)
    from api.worktrees import create_worktree_for_workspace

    tree = Path(create_worktree_for_workspace(nested)["path"]).resolve()

    assert (tree / "tracked.txt").exists(), (
        "the selected commit's tracked file must not be deleted by include cleanup"
    )
    assert (tree / "tracked.txt").read_text(encoding="utf-8") == "committed on side\n"
    status = _git("status", "--porcelain", cwd=tree).stdout
    assert "tracked.txt" not in status, f"worktree must be clean, got: {status!r}"


def test_primary_local_include_never_replaces_a_committed_config(git_env, monkeypatch):
    """The selected commit carries ``local-config.txt``; primary's copy must not win."""
    primary, linked, nested = _primary_with_linked_checkout(git_env, name="committed-config")
    (linked / "local-config.txt").write_text("committed on side\n", encoding="utf-8")
    _git("add", "local-config.txt", cwd=linked)
    _git("commit", "-q", "-m", "side commits a config", cwd=linked)
    (primary / ".worktreeinclude").write_text("local-config.txt\n", encoding="utf-8")
    (primary / "local-config.txt").write_text("primary working copy\n", encoding="utf-8")

    _stub_helper(monkeypatch)
    from api.worktrees import create_worktree_for_workspace

    tree = Path(create_worktree_for_workspace(nested)["path"]).resolve()

    assert (tree / "local-config.txt").read_text(encoding="utf-8") == "committed on side\n", (
        "a committed config may not be overwritten by the primary's include"
    )


def test_untracked_primary_only_include_is_still_dropped(git_env, monkeypatch):
    """Control: an include that is NOT committed content is still cleaned up."""
    primary, linked, nested = _primary_with_linked_checkout(git_env, name="drop-untracked")
    (primary / ".worktreeinclude").write_text("local-config.txt\n", encoding="utf-8")
    (primary / "local-config.txt").write_text("primary setting\n", encoding="utf-8")

    _stub_helper(monkeypatch)
    from api.worktrees import create_worktree_for_workspace

    tree = Path(create_worktree_for_workspace(nested)["path"]).resolve()

    assert not (tree / "local-config.txt").exists()


# --- finding 2: directory includes are read from the selected checkout ---------


def test_directory_include_links_the_selected_checkout(git_env, monkeypatch):
    """A directory in both manifests must resolve into selected, not primary."""
    primary, linked, nested = _primary_with_linked_checkout(git_env, name="dir-both")
    for root, body in ((primary, "primary value\n"), (linked, "selected value\n")):
        (root / "conf").mkdir()
        (root / "conf" / "app.conf").write_text(body, encoding="utf-8")
        (root / ".worktreeinclude").write_text("conf\n", encoding="utf-8")

    _stub_helper(monkeypatch, link_dirs=True)
    from api.worktrees import create_worktree_for_workspace

    tree = Path(create_worktree_for_workspace(nested)["path"]).resolve()
    landed = tree / "conf"

    assert landed.exists(), "the include must still be present"
    assert (landed / "app.conf").read_text(encoding="utf-8") == "selected value\n", (
        f"directory include must come from the selected checkout, got {landed.resolve()}"
    )


def test_primary_only_directory_include_link_is_removed_not_followed(git_env, monkeypatch):
    """A primary-only directory link is dropped, and primary's source survives."""
    primary, linked, nested = _primary_with_linked_checkout(git_env, name="dir-primary-only")
    (primary / "pconly").mkdir()
    (primary / "pconly" / "keep.txt").write_text("keep me\n", encoding="utf-8")
    (primary / ".worktreeinclude").write_text("pconly\n", encoding="utf-8")

    _stub_helper(monkeypatch, link_dirs=True)
    from api.worktrees import create_worktree_for_workspace

    tree = Path(create_worktree_for_workspace(nested)["path"]).resolve()

    assert not (tree / "pconly").exists() and not (tree / "pconly").is_symlink(), (
        "the include link must be removed"
    )
    assert (primary / "pconly" / "keep.txt").read_text(encoding="utf-8") == "keep me\n", (
        "removing the link must never follow it into the primary checkout"
    )


def test_committed_directory_is_not_replaced_by_a_directory_include(git_env, monkeypatch):
    """A tracked directory in the selected commit survives the include handling."""
    primary, linked, nested = _primary_with_linked_checkout(git_env, name="dir-committed")
    (linked / "src").mkdir()
    (linked / "src" / "app.py").write_text("committed\n", encoding="utf-8")
    _git("add", "src", cwd=linked)
    _git("commit", "-q", "-m", "side commits src", cwd=linked)
    (primary / "src").mkdir()
    (primary / "src" / "app.py").write_text("primary dirty\n", encoding="utf-8")
    (primary / ".worktreeinclude").write_text("src\n", encoding="utf-8")

    _stub_helper(monkeypatch, link_dirs=True)
    from api.worktrees import create_worktree_for_workspace

    tree = Path(create_worktree_for_workspace(nested)["path"]).resolve()

    assert not (tree / "src").is_symlink(), "a committed directory may not become a link"
    assert (tree / "src" / "app.py").read_text(encoding="utf-8") == "committed\n"


# --- finding 3: equal commit ids are not evidence of a local-HEAD fallback -----


def _clone_with_linked_branch(base, branch="side"):
    """Bare origin + primary clone on ``main`` + linked checkout on *branch*."""
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

    linked = primary / "linked"
    _git("worktree", "add", "-q", "-b", branch, str(linked), cwd=primary)
    _git("branch", "--set-upstream-to=origin/main", branch, cwd=primary)
    nested = linked / "nested"
    nested.mkdir()
    return origin, primary, linked, nested


def test_same_commit_id_does_not_reset_a_tree_off_the_selected_upstream(git_env, monkeypatch):
    """Selected tracks ``origin/main`` and has an unpublished commit: keep the remote tip."""
    origin, primary, linked, nested = _clone_with_linked_branch(git_env)
    (linked / "unpublished.txt").write_text("local only\n", encoding="utf-8")
    _git("add", "unpublished.txt", cwd=linked)
    _git("commit", "-q", "-m", "unpublished on side", cwd=linked)
    assert _head(linked) != _head(primary), "fixture: side must be ahead of origin/main"

    _stub_helper(monkeypatch)
    from api.worktrees import create_worktree_for_workspace

    tree = Path(create_worktree_for_workspace(nested)["path"]).resolve()

    assert _head(tree) == _head(primary), (
        "equal commit ids must not be read as a local-HEAD fallback"
    )
    assert not (tree / "unpublished.txt").exists()


def test_selected_branch_tracking_another_upstream_is_followed(git_env, monkeypatch):
    """A distinct selected upstream wins over primary's base."""
    origin, primary, linked, nested = _clone_with_linked_branch(git_env, branch="side")
    _git("checkout", "-q", "-b", "release", "origin/main", cwd=primary)
    (primary / "release.txt").write_text("release work\n", encoding="utf-8")
    _git("add", "release.txt", cwd=primary)
    _git("commit", "-q", "-m", "release work", cwd=primary)
    _git("push", "-q", "-u", "origin", "release", cwd=primary)
    _git("checkout", "-q", "main", cwd=primary)
    _git("branch", "--set-upstream-to=origin/release", "side", cwd=primary)

    _stub_helper(monkeypatch)
    from api.worktrees import create_worktree_for_workspace

    tree = Path(create_worktree_for_workspace(nested)["path"]).resolve()

    from api.worktrees import _resolve_base_commit

    expected = _resolve_base_commit(linked)
    assert expected == _git("rev-parse", "origin/release", cwd=primary).stdout.strip()
    assert _head(tree) == expected, "the tree must follow the selected branch's upstream"
    assert (tree / "release.txt").exists()


def test_no_remote_still_falls_back_to_the_selected_head(git_env, monkeypatch):
    """Control: with no upstream and no origin, local HEAD is the base."""
    primary, linked, nested = _primary_with_linked_checkout(git_env, name="no-remote")
    (linked / "feature.txt").write_text("only on side\n", encoding="utf-8")
    _git("add", "feature.txt", cwd=linked)
    _git("commit", "-q", "-m", "side work", cwd=linked)

    _stub_helper(monkeypatch)
    from api.worktrees import create_worktree_for_workspace

    tree = Path(create_worktree_for_workspace(nested)["path"]).resolve()

    assert _head(tree) == _head(linked)
    assert (tree / "feature.txt").exists()
