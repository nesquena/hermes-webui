"""Regressions for the maintainer's re-gate 5675116837 on #8162.

Two blocking regressions on that head, both from one `repo_root` deciding the
placement *and* the source of the new tree:

1. a session started in a linked checkout was built from the primary's HEAD,
   so the selected branch's committed work was missing;
2. `.worktreeinclude` was read from the primary, so a session inherited the
   wrong local settings.

Plus the reversed (alias-above) containment shape and the docs drift.

The fixtures use real git. `_setup_agent_worktree` is replaced by a stand-in
that behaves like the installed helper: it creates `<root>/.worktrees/<name>`,
branches it from *root*, and copies *root*'s `.worktreeinclude`. Everything the
assertions check happens after that, in WebUI's own alignment step.
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


@pytest.fixture()
def git_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig-none"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return tmp_path


def _stub_installed_helper(monkeypatch):
    """Stand-in for ``cli._setup_worktree``: placement, base and includes from one root."""
    import api.worktrees as worktrees

    created = []

    def fake(repo_root):
        root = Path(repo_root)
        tree = root / ".worktrees" / "hermes-test"
        tree.parent.mkdir(parents=True, exist_ok=True)
        _git("worktree", "add", "-q", "-b", "hermes-test", str(tree), cwd=root)
        manifest = root / ".worktreeinclude"
        if manifest.exists():
            for line in manifest.read_text(encoding="utf-8").splitlines():
                entry = line.strip()
                if not entry or entry.startswith("#"):
                    continue
                source = root / entry
                if source.is_file():
                    (tree / entry).parent.mkdir(parents=True, exist_ok=True)
                    (tree / entry).write_text(
                        source.read_text(encoding="utf-8"), encoding="utf-8"
                    )
        created.append(tree)
        return {"path": str(tree), "branch": "hermes-test", "repo_root": str(root)}

    monkeypatch.setattr(worktrees, "_setup_agent_worktree", fake)
    return created


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


# --- must fix 1: the new tree is built from the selected checkout ---------------


def test_session_in_a_linked_checkout_keeps_that_branchs_commits(git_env, monkeypatch):
    """No remotes, linked checkout on ``side``: the tree is based on ``side``'s HEAD."""
    primary, linked, nested = _primary_with_linked_checkout(git_env)
    (linked / "feature.txt").write_text("only on side\n", encoding="utf-8")
    _git("add", "feature.txt", cwd=linked)
    _git("commit", "-q", "-m", "side work", cwd=linked)

    created = _stub_installed_helper(monkeypatch)
    from api.worktrees import create_worktree_for_workspace

    info = create_worktree_for_workspace(nested)

    tree = Path(info["path"]).resolve()
    assert tree.parent == (primary / ".worktrees").resolve(), "placed under the primary"
    assert created and tree == created[0].resolve()
    assert (tree / "feature.txt").exists(), (
        "the selected checkout's committed work must be in the new tree"
    )
    assert (tree / "feature.txt").read_text(encoding="utf-8") == "only on side\n"
    assert (tree / "shared.txt").exists(), "the shared history comes along"


def test_session_in_the_primary_itself_is_unchanged(git_env, monkeypatch):
    """Control: a session started in the primary is not rewritten."""
    primary, linked, nested = _primary_with_linked_checkout(git_env, name="control")
    (linked / "feature.txt").write_text("only on side\n", encoding="utf-8")
    _git("add", "feature.txt", cwd=linked)
    _git("commit", "-q", "-m", "side work", cwd=linked)

    _stub_installed_helper(monkeypatch)
    from api.worktrees import create_worktree_for_workspace

    info = create_worktree_for_workspace(primary)

    tree = Path(info["path"]).resolve()
    assert tree.parent == (primary / ".worktrees").resolve()
    assert not (tree / "feature.txt").exists(), "primary's HEAD does not carry it"


# --- must fix 2: `.worktreeinclude` comes from the selected checkout ------------


def _write_manifest(root, entries, files):
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8")
    (root / ".worktreeinclude").write_text(
        "\n".join(entries) + "\n", encoding="utf-8"
    )


def test_include_manifest_is_read_from_the_selected_checkout(git_env, monkeypatch):
    """Differing manifests: the selected checkout's settings land in the new tree."""
    primary, linked, nested = _primary_with_linked_checkout(git_env, name="includes")
    _write_manifest(
        primary,
        ["local-config.txt", "primary-only.txt"],
        {"local-config.txt": "primary setting\n", "primary-only.txt": "primary\n"},
    )
    _write_manifest(
        linked,
        ["local-config.txt"],
        {"local-config.txt": "selected setting\n"},
    )

    _stub_installed_helper(monkeypatch)
    from api.worktrees import create_worktree_for_workspace

    info = create_worktree_for_workspace(nested)

    tree = Path(info["path"]).resolve()
    assert (tree / "local-config.txt").read_text(encoding="utf-8") == "selected setting\n", (
        "the include must come from the checkout the session started in"
    )
    assert not (tree / "primary-only.txt").exists(), (
        "an include only the primary lists must not be injected into the session tree"
    )


def test_include_from_the_primary_alone_is_not_injected(git_env, monkeypatch):
    """The selected checkout has no manifest: nothing is copied at all."""
    primary, linked, nested = _primary_with_linked_checkout(git_env, name="no-include")
    _write_manifest(
        primary, ["local-config.txt"], {"local-config.txt": "primary setting\n"}
    )

    _stub_installed_helper(monkeypatch)
    from api.worktrees import create_worktree_for_workspace

    info = create_worktree_for_workspace(nested)

    assert not (Path(info["path"]).resolve() / "local-config.txt").exists()


# --- should fix 3: the mirrored (alias-above) containment shape ------------------


def test_alias_above_the_checkout_does_not_win_ownership(git_env):
    """``x/.git -> x/primary/.git``: a session in ``primary`` stays in ``primary``."""
    from api.worktrees import find_main_worktree_root

    base = git_env / "alias-above"
    primary = base / "x" / "primary"
    primary.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=primary)
    (primary / "tracked.txt").write_text("committed\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=primary)
    _git("commit", "-q", "-m", "init", cwd=primary)

    alias = base / "x"
    (alias / ".git").write_text("gitdir: primary/.git\n", encoding="utf-8")

    assert find_main_worktree_root(primary) == primary.resolve()
    assert find_main_worktree_root(primary) != alias.resolve()


def test_alias_above_allocation_lands_under_the_checkout(git_env, monkeypatch):
    """The allocation, not just the resolver: ``primary/.worktrees``, never ``x``."""
    base = git_env / "alias-above-alloc"
    primary = base / "x" / "primary"
    primary.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=primary)
    (primary / "tracked.txt").write_text("committed\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=primary)
    _git("commit", "-q", "-m", "init", cwd=primary)
    alias = base / "x"
    (alias / ".git").write_text("gitdir: primary/.git\n", encoding="utf-8")

    _stub_installed_helper(monkeypatch)
    from api.worktrees import create_worktree_for_workspace

    info = create_worktree_for_workspace(primary)

    tree = Path(info["path"]).resolve()
    assert tree.parent == (primary / ".worktrees").resolve()
    assert not (alias / ".worktrees").exists(), (
        "no session tree may be created beside the alias"
    )


def test_storage_inside_the_checkout_still_resolves_to_the_checkout(git_env, monkeypatch):
    """Control: ``x/.git -> x/meta/.git`` is a real checkout, and still wins."""
    from api.worktrees import find_main_worktree_root

    base = git_env / "storage-inside"
    checkout = base / "x"
    checkout.mkdir(parents=True)
    storage = checkout / "meta"
    storage.mkdir()
    _git("init", "-q", "-b", "main", f"--separate-git-dir={storage / '.git'}", cwd=checkout)
    (checkout / "tracked.txt").write_text("committed\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=checkout)
    _git("commit", "-q", "-m", "init", cwd=checkout)

    assert find_main_worktree_root(checkout) == checkout.resolve()
