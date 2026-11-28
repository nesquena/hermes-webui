"""Placement tests for worktree sessions, driven by real git.

Every test here names the review finding it covers on PR #8162 (fourteen
``CHANGES_REQUESTED`` rounds over seventeen days). Each fixture is built by the
real git binary — ``git init``, ``git worktree add``, real commits,
``--separate-git-dir``, ``git submodule add`` — and every assertion is about
either git's own state afterwards or the exact directory the WebUI handed the
Agent's worktree helper. Nothing hand-builds a ``git worktree list`` answer.
"""

from __future__ import annotations

import os
from pathlib import Path


from api.worktree_placement import (
    PLACEMENT_MAIN,
    PLACEMENT_UNRECORDED,
    primary_worktree,
)
from api.worktrees import create_worktree_for_workspace
from tests import _worktree_git as wt


def place(workspace: Path, **producer_kwargs):
    """Create a session worktree for *workspace*; return ``(info, producer)``."""
    producer = wt.agent_producer(**producer_kwargs)
    info = create_worktree_for_workspace(workspace, setup=producer)
    return info, producer


# ── row 1: the metadata dir is never a checkout ──────────────────────────────


def test_sep_dir_direct(tmp_path):
    """Finding 1 (rounds 10-12, ``d9d17902``): a ``--separate-git-dir`` session
    must be placed under its own checkout, never under ``<metadata>/.worktrees``,
    and must never return the metadata path as ``repo_root``.

    The reviewer's warning was "do not blindly switch to the first ``worktree
    list`` entry — it also reports the metadata path". Here git answers ``G ==
    C`` for the checkout itself, so the registry is never consulted.
    """
    checkout, metadata = wt.separate_git_dir(tmp_path, dot_git=False)

    info, producer = place(checkout)

    assert producer.calls == [str(checkout.resolve())]
    assert Path(info["path"]).parent == checkout.resolve() / ".worktrees"
    assert info["placement"] == PLACEMENT_MAIN
    assert Path(info["repo_root"]) == checkout.resolve()
    assert metadata.resolve() not in Path(info["path"]).parents
    assert not (metadata.parent / ".worktrees").exists()


def test_submodule_direct(tmp_path):
    """Finding 1 (rounds 10-12): a session inside a submodule checkout belongs to
    *that* submodule — not to the superproject and not to
    ``super/.git/modules/child``.
    """
    superproject, child = wt.submodule_repo(tmp_path)

    placement = primary_worktree(child)
    info, producer = place(child)

    assert placement.primary == child.resolve()
    assert placement.reason == "main-worktree"
    assert producer.calls == [str(child.resolve())]
    assert Path(info["path"]).parent == child.resolve() / ".worktrees"
    assert not (superproject / ".worktrees").exists()
    assert (child / ".git").is_file()  # a submodule checkout, not a metadata dir


def test_sep_dir_linked_no_claim(tmp_path):
    """Finding 1/2 (rounds 10-18): from a linked worktree of a
    ``--separate-git-dir`` repository, git records only the metadata dir — which
    is *not* a working tree. The answer must be "no claim", recorded as
    ``selected-unrecorded``, with nothing written under the metadata.
    """
    checkout, metadata = wt.separate_git_dir(tmp_path, dot_git=False)
    session = wt.linked_worktree(checkout, tmp_path / "session")

    placement = primary_worktree(session)
    info, _producer = place(session)

    assert placement.primary is None
    assert placement.reason == "metadata-unrecorded"
    assert info["placement"] == PLACEMENT_UNRECORDED
    assert Path(info["path"]).parent == session.resolve() / ".worktrees"
    assert not (metadata.parent / ".worktrees").exists()
    assert not (metadata / ".worktrees").exists()


def test_sep_dir_dotgit_linked_declares_limitation(tmp_path):
    """Finding 2 (rounds 10-18, ``66c70f5c``), DECLARED LIMITATION.

    ``git init --separate-git-dir=<d>/.git <checkout>`` records ``<d>`` as the
    main worktree, with ``core.worktree`` unset — so ``rev-parse`` on ``<d>``
    answers exactly what a plain repository answers for its own root. Plain and
    this layout are indistinguishable by git query, and the only thing that
    tells them apart is a filesystem scan for a ``.git`` gitfile naming the
    metadata dir — precisely the classification that produced rows 2-11.

    So the rewrite pins the *declared* outcome instead of pretending to a claim:
    the tree lands under ``<d>/.worktrees`` because git says ``<d>`` owns it,
    and the payload says ``placement_reason="registry"``.
    """
    checkout, metadata = wt.separate_git_dir(tmp_path, dot_git=True)
    storage = metadata.parent  # <d>: the metadata's parent, named ".git" inside
    session = wt.linked_worktree(checkout, tmp_path / "session")

    placement = primary_worktree(session)
    info, producer = place(session)

    assert placement.primary == storage.resolve()
    assert placement.reason == "registry"
    assert producer.calls == [str(session.resolve())]
    assert Path(info["path"]).parent == storage.resolve() / ".worktrees"
    assert not (checkout / ".worktrees").exists()


# ── rows 3 and 4: distance, emptiness, content beside the metadata ───────────


def test_distant_primary_linked(tmp_path):
    """Finding 3 (rounds 10-18, ``9a869493``): the main root is found at any
    distance. The old search had a depth bound and a listing budget; here
    ``a/deep/primary`` and ``z/elsewhere/linked`` are four levels apart and no
    traversal happens at all.
    """
    primary = wt.init_repo(tmp_path / "a" / "deep" / "primary")
    linked = wt.linked_worktree(primary, tmp_path / "z" / "elsewhere" / "linked")

    info, producer = place(linked)

    assert producer.calls == [str(linked.resolve())]
    assert Path(info["path"]).parent == primary.resolve() / ".worktrees"
    assert info["placement"] == PLACEMENT_MAIN
    assert Path(info["repo_root"]) == primary.resolve()


def test_empty_primary_linked(tmp_path):
    """Finding 4 (rounds 10-18, ``e46e22e2``): an empty primary is still the
    primary. Nothing about the working tree's content may decide placement, so
    deleting every tracked file (and committing that) changes nothing.
    """
    primary, _origin = wt.clone_with_origin(tmp_path)
    linked = wt.linked_worktree(primary, tmp_path / "session")
    for path in primary.iterdir():
        if path.name not in {".git", ".worktrees"}:
            if path.is_dir():
                for child in sorted(path.rglob("*"), reverse=True):
                    child.unlink() if child.is_file() else child.rmdir()
                path.rmdir()
            else:
                path.unlink()
    wt.git(primary, "add", "-A")
    wt.git(primary, "commit", "-qm", "empty the working tree")
    assert [p.name for p in primary.iterdir()] == [".git"]

    info, _producer = place(linked)

    assert Path(info["path"]).parent == primary.resolve() / ".worktrees"
    assert info["placement"] == PLACEMENT_MAIN


def test_storage_sibling_with_matching_readme_ignored(tmp_path):
    """Finding 4 (rounds 10-18): content beside the metadata is not ownership. A
    sibling directory holding a byte-identical README must not be mistaken for
    the checkout, and the no-claim outcome must not wander into it.
    """
    checkout, metadata = wt.separate_git_dir(tmp_path, dot_git=False)
    sibling = metadata.parent / "sibling"
    sibling.mkdir()
    (sibling / "README.md").write_bytes((checkout / "README.md").read_bytes())
    session = wt.linked_worktree(checkout, tmp_path / "session")

    info, _producer = place(session)

    assert info["placement"] == PLACEMENT_UNRECORDED
    assert not (sibling / ".worktrees").exists()
    assert Path(info["path"]).parent == session.resolve() / ".worktrees"


# ── rows 7, 8, 9: dirtiness, sameness and deleted leaves decide nothing ──────


def test_dirty_primary_linked_places_at_primary(tmp_path):
    """Finding 7 (rounds 10-18, ``f03becc4``): a modified primary used to make
    linked sessions nest ("the primary does not hold its committed tree, so it
    is not the owner"). Placement never looks at the working tree, so a dirty
    primary is placed under exactly like a clean one.
    """
    primary, _origin = wt.clone_with_origin(tmp_path)
    linked = wt.linked_worktree(primary, tmp_path / "session")
    (primary / "README.md").write_text("locally edited\n", encoding="utf-8")
    (primary / "untracked.txt").write_text("scratch\n", encoding="utf-8")
    assert wt.git(primary, "status", "--porcelain").stdout.strip()

    info, producer = place(linked)

    assert producer.calls == [str(linked.resolve())]
    assert Path(info["path"]).parent == primary.resolve() / ".worktrees"
    assert (primary / "README.md").read_text(encoding="utf-8") == "locally edited\n"


def test_same_content_linked_places_at_primary(tmp_path):
    """Finding 8 (rounds 10-18, ``7fe4bedd``): identical content in both trees
    used to make the linked tree win ("no divergence, so prefer the nearer
    one"). Content is never compared: the recorded main worktree wins.
    """
    primary, _origin = wt.clone_with_origin(tmp_path)
    linked = wt.linked_worktree(primary, tmp_path / "session")
    assert wt.head(primary) == wt.head(linked)
    assert (primary / "README.md").read_bytes() == (linked / "README.md").read_bytes()

    info, _producer = place(linked)

    assert Path(info["path"]).parent == primary.resolve() / ".worktrees"


def test_deleted_tracked_leaf_does_not_change_placement(tmp_path):
    """Finding 9 (rounds 10-19, ``49327239``): deleting a tracked leaf used to
    revoke ownership ("the tracked names no longer match"). No name membership
    is ever tested, so removing a tracked file anywhere changes nothing.
    """
    primary, _origin = wt.clone_with_origin(tmp_path)
    (primary / "leaf").mkdir()
    wt.commit(primary, "add leaf", {"leaf/file.txt": "leaf\n"})
    linked = wt.linked_worktree(primary, tmp_path / "session")
    (linked / "leaf" / "file.txt").unlink()
    assert "leaf/file.txt" in wt.git(linked, "status", "--porcelain").stdout

    info, _producer = place(linked)

    assert Path(info["path"]).parent == primary.resolve() / ".worktrees"


# ── row 10: aliases and sibling repositories are never candidates ────────────


def test_stale_alias_ignored(tmp_path):
    """Finding 10 (rounds 10-19, ``351a3be8``): a stale gitfile alias — a
    directory whose ``.git`` *file* points at the real git dir, with a copy of
    the tree — used to win as "the checkout". It is not in git's records, so it
    is never a candidate; the recorded main worktree wins.
    """
    primary = wt.init_repo(tmp_path / "primary")
    session = wt.linked_worktree(primary, tmp_path / "session")
    alias = tmp_path / "alias"
    alias.mkdir()
    (alias / ".git").write_text(f"gitdir: {(primary / '.git').as_posix()}\n", encoding="utf-8")
    (alias / "README.md").write_text("alias copy\n", encoding="utf-8")

    info, _producer = place(session)

    assert Path(info["path"]).parent == primary.resolve() / ".worktrees"
    assert not (alias / ".worktrees").exists()
    # The alias itself is an unregistered second checkout of the same repository
    # (git answers ``G == C`` there, exactly like ``--separate-git-dir``): a
    # session opened *in* it is placed under it, by the same rule. What must
    # never happen — and used to — is the alias displacing the real checkout for
    # a session started elsewhere.
    assert primary_worktree(primary).primary == primary.resolve()


def test_sibling_dependency_dir_ignored(tmp_path):
    """Finding 10 (rounds 10-19): an unrelated sibling repository (a vendored
    dependency) used to change allocation by being enumerated as a candidate.
    Nothing is enumerated, so it cannot.
    """
    primary = wt.init_repo(tmp_path / "primary")
    session = wt.linked_worktree(primary, tmp_path / "session")
    dependency = wt.init_repo(tmp_path / "vendor" / "dependency")
    wt.commit(dependency, "dependency", {"README.md": "dependency\n"})

    info, producer = place(session)

    assert producer.calls == [str(session.resolve())]
    assert Path(info["path"]).parent == primary.resolve() / ".worktrees"
    assert not (dependency / ".worktrees").exists()


# ── row 6 / row 8: the index is never consulted ──────────────────────────────


def test_index_untouched(tmp_path, monkeypatch):
    """Finding 6 (rounds 10-18, ``308e09a6``): an empty index or a matching
    filename is not checkout ownership — the index must never be read. Every git
    argv spawned during placement is logged and checked.
    """
    primary, _origin = wt.clone_with_origin(tmp_path)
    session = wt.linked_worktree(primary, tmp_path / "session")
    log = wt.git_argv_log(tmp_path, monkeypatch)
    start = wt.git_argv_count(log)

    info, _producer = place(session)

    spawned = wt.git_argv_since(log, start)
    assert spawned, "the creation path must still spawn git"
    forbidden = ("ls-files", "diff-files", "cat-file", "ls-tree", "--refresh", "status")
    offenders = [argv for argv in spawned if any(word in argv for word in forbidden)]
    assert offenders == [], f"index/content queries on the creation path: {offenders}"
    assert Path(info["path"]).parent == primary.resolve() / ".worktrees"


def test_index_refresh_never_spawned(tmp_path, monkeypatch):
    """Finding 8 (rounds 10-18, ``7fe4bedd``): a stale stat cache used to be
    "refreshed" (`git update-index --refresh`, `diff-files`) and a partially
    matching storage dir was accepted afterwards. The creation path spawns no
    index refresh at all, so a stale index cannot change placement.
    """
    primary, _origin = wt.clone_with_origin(tmp_path)
    session = wt.linked_worktree(primary, tmp_path / "session")
    readme = primary / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8"), encoding="utf-8")
    os.utime(readme, (1, 1))  # desync the index stat cache
    log = wt.git_argv_log(tmp_path, monkeypatch)
    start = wt.git_argv_count(log)

    info, _producer = place(session)

    spawned = wt.git_argv_since(log, start)
    assert [argv for argv in spawned if "--refresh" in argv or "diff-files" in argv] == []
    assert Path(info["path"]).parent == primary.resolve() / ".worktrees"


# ── row 11: an external checkout survives; a failing query decides nothing ───


def test_external_sep_checkout_kept(tmp_path):
    """Finding 11 (rounds 10-22, ``6d508a94``): a real external
    ``--separate-git-dir`` checkout used to be revoked by a cleanliness
    adjudicator, and "unknown status" re-authorized the stale alias. There is no
    adjudicator: git's own answer for the checkout stands, and the session's
    tree is created under it.
    """
    checkout, metadata = wt.separate_git_dir(tmp_path, dot_git=False)
    (checkout / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    assert wt.git(checkout, "status", "--porcelain").stdout.strip()

    info, producer = place(checkout)

    assert producer.calls == [str(checkout.resolve())]
    assert Path(info["path"]).parent == checkout.resolve() / ".worktrees"
    assert info["placement"] == PLACEMENT_MAIN
    assert not (metadata / ".worktrees").exists()


def test_status_failure_changes_nothing(tmp_path, monkeypatch):
    """Finding 11 (rounds 10-22): with content/index queries removed, a failing
    ``status``/``diff-files``/``ls-files`` cannot change or authorize anything —
    the placement is identical with those commands forced to fail.
    """
    primary, _origin = wt.clone_with_origin(tmp_path)
    session = wt.linked_worktree(primary, tmp_path / "session")
    wt.git_argv_log(tmp_path, monkeypatch, fail_on=("status", "diff-files", "ls-files"))

    placement = primary_worktree(session)
    info, _producer = place(session)

    assert placement.primary == primary.resolve()
    assert Path(info["path"]).parent == primary.resolve() / ".worktrees"
