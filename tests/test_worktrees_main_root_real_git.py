"""Real-Git regressions for find_main_worktree_root() (review P2 on #8162).

The reviewer's finding: the previous implementation treated any git *metadata*
directory as a checkout. `--git-common-dir` is not always `<main>/.git`, so
`git init --separate-git-dir=…/metadata` and a submodule (`super/.git/modules/child`)
both returned the metadata path instead of the working tree, and session trees were
created beneath it.

These tests build real repositories on disk — no mocks of git — and assert the
resolved root is the actual checkout, not the metadata dir.
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
    """P1 on #8162: the metadata's parent must not be taken for the checkout.

    A checkout can keep ``--separate-git-dir`` inside another repository's tree
    while the checkout itself lives elsewhere. The parent is a valid checkout,
    so a parent-only check returns it and starts the session in the wrong
    project. The checkout owns its own metadata, so the correct answer is the
    checkout — never ``host``.
    """
    host = _init_repo(git_env / "host-repo")
    checkout = git_env / "elsewhere" / "checkout"
    checkout.mkdir(parents=True)
    metadata = host / "nested-metadata"
    _git("init", "-q", "-b", "main", f"--separate-git-dir={metadata}", cwd=checkout)

    find = _import()
    root = find(checkout)
    assert root == checkout.resolve(), f"expected the checkout, got {root}"
    assert root != host.resolve()


# --- Reviewer follow-up: separate metadata literally named ".git" -------------
#
# ``git init --separate-git-dir=<storage>/.git`` makes the common dir's *basename*
# ``.git`` while its parent is storage, not a checkout. A basename-only ownership
# assumption returned ``storage`` and created session trees beneath it. These
# cases assert the real allocation: which root reaches the Agent helper and where
# the tree lands.


def _separate_dotgit_fixture(base):
    """A checkout whose metadata is ``<base>/storage-only/.git``."""
    checkout = base / "actual-checkout"
    storage = base / "storage-only"
    checkout.mkdir(parents=True, exist_ok=True)
    storage.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", f"--separate-git-dir={storage / '.git'}", cwd=checkout)
    (checkout / "README.md").write_text("x\n", encoding="utf-8")
    _git("add", "README.md", cwd=checkout)
    _git("commit", "-q", "-m", "init", cwd=checkout)
    return checkout, storage


def _stub_agent_setup(monkeypatch, created_root):
    """Replace the Agent helper with one that records its argument.

    The real helper is exercised by the maintainer's replay; here the contract
    under test is *which root WebUI passes down* and where the tree is placed.
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


def test_separate_git_dir_named_dotgit_direct_allocation(git_env, monkeypatch):
    """Direct input: the Agent must get the checkout, never the storage dir."""
    checkout, storage = _separate_dotgit_fixture(git_env)
    created = []
    seen = _stub_agent_setup(monkeypatch, created)

    from api.worktrees import create_worktree_for_workspace

    info = create_worktree_for_workspace(checkout)

    assert Path(seen["repo_root"]).resolve() == checkout.resolve()
    assert Path(info["repo_root"]).resolve() == checkout.resolve()
    assert Path(info["path"]).resolve().is_relative_to(checkout.resolve())
    assert not Path(info["path"]).resolve().is_relative_to(storage.resolve())
    assert (checkout / "README.md").exists()


def test_separate_git_dir_named_dotgit_linked_allocation(git_env, monkeypatch):
    """Linked input: still the checkout that owns the metadata."""
    checkout, storage = _separate_dotgit_fixture(git_env)
    linked = git_env / "linked-wt"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=checkout)

    created = []
    seen = _stub_agent_setup(monkeypatch, created)

    from api.worktrees import create_worktree_for_workspace

    info = create_worktree_for_workspace(linked)

    assert Path(seen["repo_root"]).resolve() == checkout.resolve()
    assert Path(info["repo_root"]).resolve() == checkout.resolve()
    assert not Path(info["path"]).resolve().is_relative_to(storage.resolve())


def test_separate_git_dir_named_dotgit_resolver_direct(git_env):
    """The resolver alone answers the checkout for both inputs."""
    checkout, storage = _separate_dotgit_fixture(git_env)
    linked = git_env / "linked-wt2"
    _git("worktree", "add", "-q", "-b", "side2", str(linked), cwd=checkout)

    find = _import()
    assert find(checkout) == checkout.resolve()
    assert find(linked) == checkout.resolve()
    assert find(checkout) != storage.resolve()


def test_distant_primary_with_linked_input_never_returns_storage(git_env, monkeypatch):
    """Reviewer's residual layout: the primary checkout is outside the search.

    ``base/elsewhere/deep/actual-checkout`` holds the gitfile, the metadata is
    ``base/storage-only/.git``, and the session enters ``base/linked-wt/nested``.
    Scanning the metadata's children cannot reach the checkout, so the resolver
    must not fall back to ``storage-only`` (git reports it as a working tree
    because of the basename). The contract: either the real checkout, or the
    explicit conservative ``None`` that lets the caller keep the current
    checkout — never storage.
    """
    base = git_env / "distant"
    checkout = base / "elsewhere" / "deep" / "actual-checkout"
    storage = base / "storage-only"
    checkout.mkdir(parents=True)
    storage.mkdir(parents=True)
    _git("init", "-q", "-b", "main", f"--separate-git-dir={storage / '.git'}", cwd=checkout)
    (checkout / "sentinel.txt").write_text("keep me\n", encoding="utf-8")
    _git("add", "sentinel.txt", cwd=checkout)
    _git("commit", "-q", "-m", "init", cwd=checkout)

    linked = base / "linked-wt"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=checkout)
    nested = linked / "nested"
    nested.mkdir()

    find = _import()
    for workspace in (checkout, linked, nested):
        root = find(workspace)
        assert root != storage.resolve(), (
            f"storage must never be returned as a project root (input {workspace})"
        )
        assert root is None or root == checkout.resolve(), (
            f"expected the checkout or the conservative None, got {root}"
        )


def test_distant_primary_allocation_never_lands_in_storage(git_env, monkeypatch):
    """The allocation control: whatever the resolver answers, not storage."""
    base = git_env / "distant-alloc"
    checkout = base / "elsewhere" / "deep" / "checkout-two"
    storage = base / "storage-two"
    checkout.mkdir(parents=True)
    storage.mkdir(parents=True)
    _git("init", "-q", "-b", "main", f"--separate-git-dir={storage / '.git'}", cwd=checkout)
    (checkout / "tracked.txt").write_text("x\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=checkout)
    _git("commit", "-q", "-m", "init", cwd=checkout)
    linked = base / "linked-two"
    _git("worktree", "add", "-q", "-b", "side2", str(linked), cwd=checkout)

    created = []
    seen = _stub_agent_setup(monkeypatch, created)

    from api.worktrees import create_worktree_for_workspace

    info = create_worktree_for_workspace(linked)

    root = Path(seen["repo_root"]).resolve()
    assert root != storage.resolve(), "the Agent must never be handed storage"
    assert not Path(info["path"]).resolve().is_relative_to(storage.resolve())


def test_distant_primary_with_storage_notes_never_returns_storage(git_env, monkeypatch):
    """Unrelated content beside the metadata is not checkout ownership.

    Same distant-primary layout, plus ``storage-only/README.txt``: operator
    notes next to the metadata must not turn the metadata's parent into a
    checkout. The resolver may answer the real checkout or the conservative
    ``None``, never storage; allocation then keeps the current checkout and the
    committed sentinel in the real checkout survives untouched.
    """
    base = git_env / "distant-notes"
    checkout = base / "elsewhere" / "deep" / "actual-checkout"
    storage = base / "storage-only"
    checkout.mkdir(parents=True)
    storage.mkdir(parents=True)
    _git("init", "-q", "-b", "main", f"--separate-git-dir={storage / '.git'}", cwd=checkout)
    sentinel = checkout / "sentinel.txt"
    sentinel.write_text("keep me\n", encoding="utf-8")
    _git("add", "sentinel.txt", cwd=checkout)
    _git("commit", "-q", "-m", "init", cwd=checkout)

    linked = base / "linked-wt"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=checkout)
    nested = linked / "nested"
    nested.mkdir()
    (storage / "README.txt").write_text("operator notes\n", encoding="utf-8")

    find = _import()
    assert find(checkout) == checkout.resolve()
    for workspace in (linked, nested):
        root = find(workspace)
        assert root != storage.resolve(), (
            f"storage must never be returned as a project root (input {workspace})"
        )
        assert root is None or root == checkout.resolve()

    created = []
    seen = _stub_agent_setup(monkeypatch, created)

    from api.worktrees import create_worktree_for_workspace

    info = create_worktree_for_workspace(nested)

    # Either the real checkout is recovered (preferred) or the conservative
    # current-checkout fallback applies. Never the metadata's parent.
    repo_root = Path(seen["repo_root"]).resolve()
    assert repo_root in (checkout.resolve(), linked.resolve())
    assert repo_root != storage.resolve()
    assert Path(info["repo_root"]).resolve() == repo_root
    allocated = Path(info["path"]).resolve()
    assert allocated.parent == (repo_root / ".worktrees").resolve()
    assert not allocated.is_relative_to(storage.resolve())
    assert created and created[0].resolve() == allocated
    assert sentinel.read_text(encoding="utf-8") == "keep me\n"


def test_empty_primary_with_linked_checkout_resolves_and_allocates_there(
    git_env, monkeypatch
):
    """Positive control: a valid primary whose only commit is empty.

    An ordinary repository with just an empty commit legitimately holds nothing
    but ``.git``; the ownership check must not mistake that for storage. A
    linked checkout outside it must unnest to the primary and allocate there.
    """
    base = git_env / "empty-primary"
    primary = base / "primary"
    primary.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=primary)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=primary)

    linked = base / "linked-empty"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=primary)

    find = _import()
    assert find(primary) == primary.resolve()
    assert find(linked) == primary.resolve()

    created = []
    seen = _stub_agent_setup(monkeypatch, created)

    from api.worktrees import create_worktree_for_workspace

    info = create_worktree_for_workspace(linked)

    assert Path(seen["repo_root"]).resolve() == primary.resolve()
    assert Path(info["repo_root"]).resolve() == primary.resolve()
    allocated = Path(info["path"]).resolve()
    assert allocated.parent == (primary / ".worktrees").resolve()
    assert not allocated.is_relative_to(linked.resolve())
    assert created and created[0].resolve() == allocated


def test_storage_dir_holding_a_tracked_basename_is_still_not_a_checkout(git_env):
    """Reviewer's README case, tightened: the stray file *collides* with a path.

    The storage dir holds an operator note whose basename matches a tracked
    path in the repository. Content is not ownership evidence in either
    direction, so the resolver must still refuse the metadata's parent and let
    the caller keep the checkout it already has.
    """
    base = git_env / "collide"
    checkout = base / "elsewhere" / "deep" / "actual-checkout"
    storage = base / "storage-only"
    checkout.mkdir(parents=True)
    storage.mkdir(parents=True)
    _git("init", "-q", "-b", "main", f"--separate-git-dir={storage / '.git'}", cwd=checkout)
    (checkout / "README.md").write_text("real\n", encoding="utf-8")
    _git("add", "README.md", cwd=checkout)
    _git("commit", "-q", "-m", "init", cwd=checkout)

    linked = base / "linked-wt"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=checkout)

    # The operator note reuses the tracked basename.
    (storage / "README.md").write_text("operator notes\n", encoding="utf-8")

    find = _import()
    assert find(checkout) == checkout.resolve()
    for workspace in (linked, linked / "nested"):
        if not workspace.exists():
            workspace.mkdir()
        root = find(workspace)
        assert root != storage.resolve(), "storage must never be answered"
        assert root is None or root == checkout.resolve()


def test_storage_dir_with_only_an_unrelated_README_is_not_a_checkout(git_env, monkeypatch):
    """The reviewer's literal fixture: unrelated content beside the metadata.

    Asserts the whole allocation path, not only the resolver: the Agent must
    never be handed storage and the tree must never land under it.
    """
    base = git_env / "readme-beside"
    checkout = base / "elsewhere" / "deep" / "actual-checkout"
    storage = base / "storage-only"
    checkout.mkdir(parents=True)
    storage.mkdir(parents=True)
    _git("init", "-q", "-b", "main", f"--separate-git-dir={storage / '.git'}", cwd=checkout)
    (checkout / "sentinel.txt").write_text("keep me\n", encoding="utf-8")
    _git("add", "sentinel.txt", cwd=checkout)
    _git("commit", "-q", "-m", "init", cwd=checkout)

    linked = base / "linked-wt"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=checkout)

    (storage / "README.txt").write_text("operator notes\n", encoding="utf-8")

    find = _import()
    root = find(linked)
    assert root != storage.resolve()
    assert root is None or root == checkout.resolve()

    created = []
    seen = _stub_agent_setup(monkeypatch, created)

    from api.worktrees import create_worktree_for_workspace

    info = create_worktree_for_workspace(linked)

    assert Path(seen["repo_root"]).resolve() != storage.resolve()
    assert not Path(info["path"]).resolve().is_relative_to(storage.resolve())
    assert (checkout / "sentinel.txt").exists()


# --- Reviewer re-gate (a3c42e9b): depth cutoff and bounded traversal ----------
#
# Two objective blockers on the previous head:
#   1. moving the owning checkout one level deeper still reached the depth
#      filter and let ``storage-only`` win;
#   2. ``rglob`` descended the whole subtree before filtering, so a deep
#      unrelated sibling tree was walked to completion.


def _deeper_primary_fixture(base, levels=("elsewhere", "deep", "more")):
    """Checkout at ``base/<levels>/actual-checkout``, metadata in ``base/storage-only``."""
    checkout = base.joinpath(*levels, "actual-checkout")
    storage = base / "storage-only"
    checkout.mkdir(parents=True)
    storage.mkdir(parents=True)
    _git("init", "-q", "-b", "main", f"--separate-git-dir={storage / '.git'}", cwd=checkout)
    (checkout / "sentinel.txt").write_text("keep me\n", encoding="utf-8")
    _git("add", "sentinel.txt", cwd=checkout)
    _git("commit", "-q", "-m", "init", cwd=checkout)
    linked = base / "linked-wt"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=checkout)
    nested = linked / "nested"
    nested.mkdir()
    return checkout, storage, linked, nested


def test_deeper_primary_linked_input_never_returns_storage(git_env):
    """Blocker 1: the owning checkout one level deeper must not crown storage.

    The contract is the reviewer's: answer the real checkout, or the explicit
    conservative ``None`` — never the metadata's parent.
    """
    base = git_env / "deeper"
    checkout, storage, linked, nested = _deeper_primary_fixture(base)

    find = _import()
    for workspace in (checkout, linked, nested):
        root = find(workspace)
        assert root != storage.resolve(), (
            f"storage must never be returned as a project root (input {workspace})"
        )
        assert root is None or root == checkout.resolve(), (
            f"expected the checkout or the conservative None, got {root}"
        )


def test_deeper_primary_allocation_lands_on_the_checkout(git_env, monkeypatch):
    """Blocker 1, allocation: the Agent never receives storage."""
    base = git_env / "deeper-alloc"
    checkout, storage, linked, nested = _deeper_primary_fixture(base)

    created = []
    seen = _stub_agent_setup(monkeypatch, created)

    from api.worktrees import create_worktree_for_workspace

    info = create_worktree_for_workspace(nested)

    repo_root = Path(seen["repo_root"]).resolve()
    assert repo_root != storage.resolve()
    assert repo_root in (checkout.resolve(), linked.resolve())
    allocated = Path(info["path"]).resolve()
    assert not allocated.is_relative_to(storage.resolve())
    assert (checkout / "sentinel.txt").read_text(encoding="utf-8") == "keep me\n"


def test_search_does_not_walk_a_deep_unrelated_sibling_tree(git_env, monkeypatch):
    """Blocker 2: traversal is bounded, not filtered after the fact.

    A 15-level sibling tree with no ``.git`` must not be walked to completion.
    Every real ``os.scandir`` call is delegated to and counted, with the paths
    it is asked to open recorded.
    """
    base = git_env / "bounded"
    checkout, storage, linked, nested = _deeper_primary_fixture(base)

    # An unrelated sibling tree far deeper than any supported layout.
    deep_sibling = base / "unrelated"
    cursor = deep_sibling
    for i in range(15):
        cursor = cursor / f"level{i}"
    cursor.mkdir(parents=True)

    import api.worktrees as worktrees

    real_scandir = os.scandir
    visited: list[str] = []

    def counting_scandir(path="."):
        visited.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(worktrees.os, "scandir", counting_scandir)

    root = worktrees.find_main_worktree_root(nested)
    assert root != storage.resolve()
    assert root is None or root == checkout.resolve()

    # Descent into the unrelated tree stops at the depth cap: it must be
    # entered shallowly at most, never walked to its bottom (15 levels).
    entered_unrelated = [
        path for path in visited if str(deep_sibling) in path
    ]
    assert len(entered_unrelated) <= _max_listing_depth() + 1, (
        "the search descended the unrelated sibling tree too far: "
        f"{entered_unrelated[:5]}"
    )
    assert not any("level5" in path or "level10" in path for path in entered_unrelated), (
        f"the search walked deep into an unrelated tree: {entered_unrelated[:5]}"
    )
    # And the whole search stays a small, bounded number of directory listings.
    assert len(visited) < 40, f"{len(visited)} directory listings is not bounded"


def _max_listing_depth() -> int:
    import api.worktrees as worktrees

    return worktrees._CHECKOUT_LISTING_DEPTH


# --- Recognised-topology finding: an unclaimed search claims nothing ---------
# The reviewer's residual case: ``unsuccessful gitfile discovery`` used to fall
# through to a *shape* fallback (empty index, a filename that happens to collide)
# and hand the Agent the metadata's parent. Ownership now has to be recorded, and
# the allocation controls below assert where the tree actually lands.


def _reviewer_layout(base, levels=("elsewhere", "deep", "more", "further")):
    """The reviewer's exact fixture: depth-5 checkout, storage with a colliding file."""
    checkout = base.joinpath(*levels, "actual-checkout")
    storage = base / "storage-only"
    checkout.mkdir(parents=True)
    storage.mkdir(parents=True)
    _git("init", "-q", "-b", "main", f"--separate-git-dir={storage / '.git'}", cwd=checkout)
    (checkout / "tracked.txt").write_text("real\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=checkout)
    _git("commit", "-q", "-m", "init", cwd=checkout)
    # Unrelated operator notes that only share the tracked *filename*.
    (storage / "tracked.txt").write_text("unrelated operator notes\n", encoding="utf-8")
    linked = base / "linked-wt"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=checkout)
    nested = linked / "nested"
    nested.mkdir()
    return checkout, storage, linked, nested


def test_colliding_filename_is_not_checkout_ownership(git_env, monkeypatch):
    """A recorded claim wins; a name collision never does."""
    checkout, storage, linked, nested = _reviewer_layout(git_env / "collision")
    find = _import()
    for workspace in (linked, nested):
        root = find(workspace)
        assert root != storage.resolve(), "storage must never be answered"
        assert root is None or root == checkout.resolve()

    created = []
    seen = _stub_agent_setup(monkeypatch, created)
    from api.worktrees import create_worktree_for_workspace

    info = create_worktree_for_workspace(nested)
    repo_root = Path(seen["repo_root"]).resolve()
    assert repo_root != storage.resolve()
    assert not Path(info["path"]).resolve().is_relative_to(storage.resolve())


def test_tracked_file_whose_content_differs_is_not_ownership(git_env):
    """A storage note that reuses a tracked name but not its content.

    This is the maintainer's collision fixture at depth 6, where the bounded
    search cannot reach the primary. Name presence alone must not claim the
    storage dir: the parent's tree has to *be* the committed tree, and an
    operator note only shares the name.
    """
    base = git_env / "collision-content"
    checkout = base.joinpath(
        "elsewhere", "l0", "l1", "l2", "l3", "actual-checkout"
    )
    storage = base / "storage-only"
    checkout.mkdir(parents=True)
    storage.mkdir(parents=True)
    _git("init", "-q", "-b", "main", f"--separate-git-dir={storage / '.git'}", cwd=checkout)
    for name in ("tracked.txt", "second.txt"):
        (checkout / name).write_text(f"committed {name}\n", encoding="utf-8")
        _git("add", name, cwd=checkout)
    _git("commit", "-q", "-m", "init", cwd=checkout)
    # Operator notes that reuse both tracked names with different content.
    for name in ("tracked.txt", "second.txt"):
        (storage / name).write_text(f"unrelated notes about {name}\n", encoding="utf-8")
    linked = base / "linked-wt"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=checkout)

    find = _import()
    for workspace in (linked, linked / "nested" if (linked / "nested").mkdir() is None else linked):
        assert find(workspace) != storage.resolve()


def test_plain_single_file_checkout_still_resolves(git_env):
    """A plain checkout tracking one file keeps resolving to itself.

    The positive the ownership rule must not lose: names present *and* matching
    the committed content is affirmative, at any repository size.
    """
    primary = git_env / "single" / "primary"
    primary.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=primary)
    (primary / "a.txt").write_text("one file\n", encoding="utf-8")
    _git("add", "a.txt", cwd=primary)
    _git("commit", "-q", "-m", "init", cwd=primary)
    linked = git_env / "single" / "linked"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=primary)

    find = _import()
    assert find(primary) == primary.resolve()
    assert find(linked) == primary.resolve()


def test_inside_and_outside_the_search_boundary(git_env):
    """Allocation controls immediately inside and outside the listing bound."""
    import api.worktrees as worktrees

    depth = worktrees._CHECKOUT_LISTING_DEPTH
    inside = tuple(f"lvl{i}" for i in range(depth - 1))
    outside = tuple(f"lvl{i}" for i in range(depth + 1))
    find = _import()

    for label, levels in (("inside", inside), ("outside", outside)):
        base = git_env / f"boundary-{label}"
        checkout = base.joinpath(*levels, "checkout")
        storage = base / "storage"
        checkout.mkdir(parents=True)
        storage.mkdir(parents=True)
        _git("init", "-q", "-b", "main", f"--separate-git-dir={storage / '.git'}", cwd=checkout)
        (checkout / "sentinel.txt").write_text("keep me\n", encoding="utf-8")
        _git("add", "sentinel.txt", cwd=checkout)
        _git("commit", "-q", "-m", "init", cwd=checkout)
        linked = base / "linked"
        _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=checkout)

        assert find(checkout) == checkout.resolve(), f"{label}: the checkout is its own root"
        root = find(linked)
        assert root != storage.resolve(), f"{label}: storage must never be answered"
        # Inside the bound the primary is recovered; outside it the caller keeps
        # its own checkout (the documented ``None`` fallback).
        if label == "inside":
            assert root == checkout.resolve(), "inside the bound the primary is found"
        else:
            assert root in (None, checkout.resolve())


# --- Scope-aware claim search: a skipped directory is not a complete search ---
#
# The reviewer's residual: the claim search treated every stop as a finished
# search, so ``<base>/storage/.git`` was promoted to a checkout whenever the
# real checkout sat behind one — under ``node_modules``, inside another
# repository, or one level above the level the ancestor walk stopped at. A skip
# is only sound when it lies *inside* the plain candidate itself; anything the
# search skipped outside the candidate leaves ownership unknown, and a gitfile
# found by the one-level probe or the ancestor probe is answered positively.


def _separate_git_dir_checkout(base, checkout, metadata):
    """``git init --separate-git-dir=<metadata>/.git <checkout>`` with one commit."""
    checkout.mkdir(parents=True)
    metadata.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", f"--separate-git-dir={metadata / '.git'}", cwd=checkout)
    (checkout / "tracked.txt").write_text("committed\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=checkout)
    _git("commit", "-q", "-m", "init", cwd=checkout)
    return checkout


@pytest.mark.parametrize("skipped", ["node_modules", ".venv", "site-packages"])
def test_checkout_under_a_skipped_directory_resolves_not_storage(
    git_env, monkeypatch, skipped
):
    """A checkout inside a skipped dir is found, never rounded up to storage.

    The metadata is ``<base>/storage/.git``; the real checkout is
    ``<base>/<skipped>/pkg/checkout``. Skipping ``<skipped>`` is a cost
    decision, not evidence, so the probe looks inside it: the claim is found and
    the real checkout is answered. The metadata's parent is never crowned, and
    the allocation control keeps the caller's own checkout when the claim is out
    of reach.
    """
    base = git_env / f"skipped-{skipped.strip('.')}"
    storage = base / "storage"
    checkout = _separate_git_dir_checkout(
        base, base / skipped / "pkg" / "checkout", storage
    )
    linked = base / "linked"
    _git("worktree", "add", "-q", "-b", "linked", str(linked), cwd=checkout)

    find = _import()
    for workspace in (linked, checkout):
        root = find(workspace)
        assert root != storage.resolve(), f"{workspace}: storage must never be answered"

    assert find(linked) == checkout.resolve(), "a skip must not hide the real checkout"

    created = []
    seen = _stub_agent_setup(monkeypatch, created)
    from api.worktrees import create_worktree_for_workspace

    info = create_worktree_for_workspace(linked)
    repo_root = Path(seen["repo_root"]).resolve()
    assert repo_root != storage.resolve()
    assert not Path(info["path"]).resolve().is_relative_to(storage.resolve())


def test_checkout_zero_colliding_content_still_resolves_not_storage(git_env):
    """Same layout with nothing in common between storage and the checkout.

    Pins the rule to the *claim*, not to any content coincidence: the checkout
    is answered because its gitfile names this metadata, and the metadata's
    parent is never crowned.
    """
    base = git_env / "skipped-empty"
    storage = base / "storage"
    checkout = _separate_git_dir_checkout(
        base, base / "node_modules" / "pkg" / "checkout", storage
    )
    linked = base / "linked"
    _git("worktree", "add", "-q", "-b", "linked", str(linked), cwd=checkout)

    assert list(storage.iterdir()) == [storage / ".git"]
    find = _import()
    assert find(linked) == checkout.resolve()
    assert find(linked) != storage.resolve()


def test_checkout_nested_in_another_repository_is_answered(git_env):
    """A checkout one level inside another repo still claims the metadata.

    ``<base>/vendor/otherrepo/checkout`` is a working tree of the separate
    metadata. The search does not descend into ``otherrepo``, so it probes one
    level: the claim is positive evidence and wins over the plain layout.
    """
    base = git_env / "nested-repo"
    other = base / "vendor" / "otherrepo"
    other.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=other)
    (other / "README.md").write_text("other\n", encoding="utf-8")
    _git("add", "README.md", cwd=other)
    _git("commit", "-q", "-m", "init", cwd=other)

    storage = base / "storage"
    checkout = _separate_git_dir_checkout(base, other / "checkout", storage)
    linked = checkout / "linked-wt"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=checkout)

    find = _import()
    assert find(linked) == checkout.resolve()
    assert find(linked) != storage.resolve()


def test_primary_above_the_metadata_grandparent_is_answered(git_env, monkeypatch):
    """The checkout may sit above the directory holding the metadata.

    ``work/sub/storage/.git`` with the checkout at ``work/primary``: the old
    ancestor bound stopped at ``work/sub``, so the search never saw the claim
    and crowned storage. The ancestor probe reaches ``work``.
    """
    base = git_env / "above-bound"
    storage = base / "sub" / "storage"
    storage.mkdir(parents=True)
    checkout = base / "primary"
    checkout.mkdir(parents=True)
    _git("init", "-q", "-b", "main", f"--separate-git-dir={storage / '.git'}", cwd=checkout)
    (checkout / "tracked.txt").write_text("committed\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=checkout)
    _git("commit", "-q", "-m", "init", cwd=checkout)
    linked = base / "linked"
    _git("worktree", "add", "-q", "-b", "linked", str(linked), cwd=checkout)

    find = _import()
    assert find(linked) == checkout.resolve()
    assert find(linked) != storage.resolve()

    created = []
    seen = _stub_agent_setup(monkeypatch, created)
    from api.worktrees import create_worktree_for_workspace

    info = create_worktree_for_workspace(linked)
    assert Path(seen["repo_root"]).resolve() == checkout.resolve()
    assert Path(info["path"]).resolve().is_relative_to(checkout.resolve())


def test_plain_repo_keeps_resolving_beside_skips_and_other_repos(git_env):
    """The positive the scope rule must not lose.

    An ordinary repository resolves even when its own ``node_modules`` and a
    sibling checkout of another repository sit in the searched levels: those
    stops are inside the candidate, or answered as complete by the probe.
    """
    base = git_env / "plain-neighbourhood"
    sibling = _init_repo(base / "sibling")
    primary = base / "primary"
    primary.mkdir()
    _git("init", "-q", "-b", "main", cwd=primary)
    (primary / "tracked.txt").write_text("committed\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=primary)
    _git("commit", "-q", "-m", "init", cwd=primary)
    (primary / "node_modules" / "dep").mkdir(parents=True)
    (primary / ".tox").mkdir()
    linked = base / "linked"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=primary)
    # The same skips in the *session* tree, which is where a workspace really
    # lands when the UI restores a session inside its own dependencies.
    (linked / "node_modules" / "dep").mkdir(parents=True)

    assert sibling.exists()
    find = _import()
    assert find(primary) == primary.resolve()
    assert find(linked) == primary.resolve()
    assert find(linked / "node_modules" / "dep") == primary.resolve()


def test_plain_repo_deep_under_home_like_nesting_resolves(git_env):
    """A plain repository many levels down still resolves its own root.

    The ancestor probe walks to the filesystem root, so nesting depth is not
    what makes ownership unknown.
    """
    primary = _init_repo(git_env / "a" / "b" / "c" / "d" / "e" / "f" / "g" / "primary")
    linked = git_env / "linked"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=primary)

    find = _import()
    assert find(linked) == primary.resolve()


@pytest.mark.parametrize("skipped", ["node_modules", ".venv"])
def test_checkout_inside_the_candidate_own_dependency_tree_is_the_real_checkout(
    git_env, monkeypatch, skipped
):
    """A claim parked in the *candidate's own* dependency tree still wins.

    Re-gate case 1, both candidate-owned skip directories: the metadata is
    ``<storage>/.git`` and the real checkout is
    ``<storage>/<skipped>/pkg/checkout``. The skip is inside the plain
    candidate, so treating it as sound crowns storage; treating it as a gap
    makes the layout unknown. Neither is right -- the claim is simply there, at
    the depth a checkout parks at, and the probe finds it. The allocation
    control asserts the Agent is never handed storage.
    """
    base = git_env / f"candidate-own-dep-{skipped.strip('.')}"
    storage = base / "storage-only"
    checkout = _separate_git_dir_checkout(
        base, storage / skipped / "pkg" / "checkout", storage
    )
    linked = base / "linked"
    _git("worktree", "add", "-q", "-b", "linked", str(linked), cwd=checkout)

    find = _import()
    for workspace in (linked, checkout):
        assert find(workspace) == checkout.resolve(), f"{workspace}: storage was crowned"
    assert find(linked) != storage.resolve()

    created = []
    seen = _stub_agent_setup(monkeypatch, created)
    from api.worktrees import create_worktree_for_workspace

    create_worktree_for_workspace(linked)
    assert Path(seen["repo_root"]).resolve() == checkout.resolve()


def test_plain_primary_resolves_beside_an_unrelated_sibling_dependency_dir(
    git_env, monkeypatch
):
    """Re-gate case 2: an ordinary primary is not made unknown by a neighbour.

    ``<base>/node_modules`` sits beside ``<base>/primary``. The skip is probed
    and holds no claim, so the search is complete and the ordinary primary keeps
    resolving -- the behaviour this PR exists to restore. The allocation lands
    under the primary, not under the caller's own checkout.
    """
    base = git_env / "sibling-dep"
    primary = _init_repo(base / "primary")
    (base / "node_modules" / "dep" / "lib").mkdir(parents=True)
    linked = base / "linked"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=primary)

    find = _import()
    assert find(primary) == primary.resolve()
    assert find(linked) == primary.resolve()

    created = []
    seen = _stub_agent_setup(monkeypatch, created)
    from api.worktrees import create_worktree_for_workspace

    info = create_worktree_for_workspace(linked)
    assert Path(seen["repo_root"]).resolve() == primary.resolve()
    assert Path(info["path"]).resolve().is_relative_to(primary.resolve())


@pytest.mark.parametrize("entry", ["linked", "primary", "primary/new/deep"])
def test_stale_gitfile_alias_inside_the_primary_is_not_the_checkout(git_env, entry):
    """Re-gate case 3: a stale alias must not outrank the checkout holding the files.

    ``primary/new/deep/.git`` is a leftover gitfile naming ``primary/.git``. Both
    directories claim the same metadata, so the layout is ambiguous by
    construction: git itself answers which of them carries the committed tree,
    and that is the primary. Asked from the linked worktree, from the primary and
    from the alias directory itself, the answer stays the primary.
    """
    base = git_env / "stale-alias"
    primary = _init_repo(base / "primary")
    stale = primary / "new" / "deep"
    stale.mkdir(parents=True)
    (stale / ".git").write_text(
        f"gitdir: {primary / '.git'}\n", encoding="utf-8"
    )
    linked = base / "linked"
    _git("worktree", "add", "-q", "-b", "side", str(linked), cwd=primary)

    find = _import()
    workspace = {"linked": linked, "primary": primary, "primary/new/deep": stale}[entry]
    assert find(workspace) == primary.resolve()

