"""Real-Git regressions for find_main_worktree_root() (review P2 on #8162).

The reviewer's finding: the previous implementation treated any git *metadata*
directory as a checkout. `--git-common-dir` is not always `<main>/.git`, so
`git init --separate-git-dir=…/metadata` and a submodule (`super/.git/modules/child`)
both returned the metadata path instead of the working tree, and session trees were
created beneath it.

These tests build real repositories on disk — no mocks of git — and assert the
resolved root is the actual checkout, not the metadata dir.
"""

import subprocess

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
    """Isolated git identity so commits work on a bare CI runner."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig-none"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return tmp_path


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=path)
    (path / "README.md").write_text("x\n", encoding="utf-8")
    _git("add", "README.md", cwd=path)
    _git("commit", "-q", "-m", "init", cwd=path)
    return path


def _import():
    from api.worktrees import find_main_worktree_root

    return find_main_worktree_root


def test_regular_repo_returns_its_root(git_env):
    repo = _init_repo(git_env / "regular")
    find = _import()
    assert find(repo) == repo.resolve()


def test_regular_linked_worktree_unnests_to_main(git_env):
    repo = _init_repo(git_env / "regular-linked")
    linked = git_env / "regular-linked-wt"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=repo)
    find = _import()
    assert find(linked) == repo.resolve()


def test_separate_git_dir_returns_checkout_not_metadata(git_env):
    """The reviewer's primary case: metadata dir must not be mistaken for a checkout."""
    checkout = git_env / "sep-checkout"
    metadata = git_env / "sep-metadata"
    checkout.mkdir()
    _git("init", "-q", "-b", "main", f"--separate-git-dir={metadata}", cwd=checkout)
    (checkout / "README.md").write_text("x\n", encoding="utf-8")
    _git("add", "README.md", cwd=checkout)
    _git("commit", "-q", "-m", "init", cwd=checkout)

    find = _import()
    root = find(checkout)
    assert root is not None
    assert root == checkout.resolve(), f"expected the checkout, got {root}"
    assert root != metadata.resolve()


def test_separate_git_dir_linked_worktree(git_env):
    checkout = git_env / "sep2-checkout"
    metadata = git_env / "sep2-metadata"
    checkout.mkdir()
    _git("init", "-q", "-b", "main", f"--separate-git-dir={metadata}", cwd=checkout)
    (checkout / "README.md").write_text("x\n", encoding="utf-8")
    _git("add", "README.md", cwd=checkout)
    _git("commit", "-q", "-m", "init", cwd=checkout)

    linked = git_env / "sep2-linked"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=checkout)

    find = _import()
    assert find(linked) == checkout.resolve()
    assert find(checkout) == checkout.resolve()


def test_submodule_returns_submodule_checkout_not_modules_dir(git_env):
    super_repo = _init_repo(git_env / "super")
    child = _init_repo(git_env / "child-src")

    _git(
        "-c", "protocol.file.allow=always",
        "submodule", "add", "-q", str(child), "child",
        cwd=super_repo,
    )
    _git("commit", "-q", "-m", "add submodule", cwd=super_repo)

    sub_checkout = super_repo / "child"
    modules_dir = super_repo / ".git" / "modules" / "child"
    assert modules_dir.is_dir(), "fixture: submodule metadata dir missing"

    find = _import()
    root = find(sub_checkout)
    assert root is not None
    assert root == sub_checkout.resolve(), f"expected the submodule checkout, got {root}"
    assert root != modules_dir.resolve()


def test_submodule_linked_worktree(git_env):
    super_repo = _init_repo(git_env / "super2")
    child = _init_repo(git_env / "child2-src")
    _git(
        "-c", "protocol.file.allow=always",
        "submodule", "add", "-q", str(child), "child",
        cwd=super_repo,
    )
    _git("commit", "-q", "-m", "add submodule", cwd=super_repo)

    sub_checkout = super_repo / "child"
    linked = git_env / "child2-linked"
    _git("worktree", "add", "-q", "-b", "subside", str(linked), cwd=sub_checkout)

    find = _import()
    assert find(linked) == sub_checkout.resolve()


def test_nested_workspace_inside_regular_repo(git_env):
    repo = _init_repo(git_env / "nested-repo")
    nested = repo / "a" / "b" / "c"
    nested.mkdir(parents=True)
    find = _import()
    assert find(nested) == repo.resolve()


def test_non_git_directory_returns_none(git_env):
    plain = git_env / "not-a-repo"
    plain.mkdir()
    find = _import()
    assert find(plain) is None


def test_separate_git_dir_inside_unrelated_repo_is_not_mistaken_for_it(git_env):
    """P1 on #8162: the fallback must verify the parent OWNS the common dir.

    A checkout can keep ``--separate-git-dir`` inside another repository's tree
    while the checkout itself lives elsewhere. The parent is a valid checkout,
    so a parent-only check returns it and starts the session in the wrong
    project. The parent does not own this common dir, so the answer is None.
    """
    host = _init_repo(git_env / "host-repo")
    checkout = git_env / "elsewhere" / "checkout"
    checkout.mkdir(parents=True)
    metadata = host / "nested-metadata"
    _git("init", "-q", "-b", "main", f"--separate-git-dir={metadata}", cwd=checkout)

    find = _import()
    assert find(checkout) is None
