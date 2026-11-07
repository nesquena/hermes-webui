"""Source-assembly tests: who builds the tree, and out of which checkout.

Rows 12-14 of PR #8162. All three were found by the reviewer only because the
WebUI mirrored the Agent's own policy — base resolution, ``.worktreeinclude``
handling, "restore what the deploy dropped" — and every mirror drifted.

The rule here is the invariant: the WebUI hands the Agent's real
``_setup_worktree`` the **selected** checkout and then only *moves* the tree it
produced. These tests assert that, plus the git state of the result. Where the
installed Agent's helper is importable the tests run against it; the recorded
stand-in (same git operations, plus an argument log) covers the environments
that have no Agent installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from api.worktrees import create_worktree_for_workspace
from tests import _worktree_git as wt

REAL_AGENT = wt.real_agent_setup()
requires_agent = pytest.mark.skipif(
    REAL_AGENT is None,
    reason="the installed Agent's cli.py is not importable in this environment",
)


def _dirtying_producer():
    """The stand-in producer, plus a dirty tree and a snapshot at creation time."""
    base = wt.agent_producer()
    captured: dict[str, object] = {}

    def setup(root: str) -> dict:
        info = base(root)
        tree = Path(info["path"])
        (tree / "README.md").write_text("edited before the move\n", encoding="utf-8")
        (tree / "untracked.txt").write_text("scratch\n", encoding="utf-8")
        captured["before"] = wt.snapshot(tree)
        captured["head"] = wt.head(tree)
        captured["branch"] = wt.git(tree, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        return info

    setup.captured = captured  # type: ignore[attr-defined]
    return setup


# ── row 12: the split that decides which checkout is the source ──────────────


def test_linked_session_keeps_selected_base(tmp_path):
    """Finding 12 (rounds 10-27, ``19d216a0``), CORE: a session started in a
    linked worktree must build its new tree from *that* worktree's branch.

    The old code passed the main root to the Agent, so the selected branch's
    committed work was silently absent from the new tree. Here the tree's HEAD
    is the selected branch's tip and its committed file is present, while the
    tree itself still lands under the main root.
    """
    primary, _origin = wt.clone_with_origin(tmp_path)
    session = wt.linked_worktree(primary, tmp_path / "session")
    tip = wt.commit(session, "work on the side branch", {"side.txt": "side work\n"})
    assert tip != wt.head(primary)

    producer = wt.agent_producer()
    info = create_worktree_for_workspace(session, setup=producer)

    created = Path(info["path"])
    assert producer.calls == [str(session.resolve())]
    assert created.parent == primary.resolve() / ".worktrees"
    assert wt.head(created) == tip
    assert (created / "side.txt").read_text(encoding="utf-8") == "side work\n"
    assert info["branch"].startswith("hermes/")


def test_include_manifest_from_selected(tmp_path):
    """Finding 12 (rounds 10-27), SILENT half: ``.worktreeinclude`` is read from
    the *selected* checkout. Two different manifests must not be resolved by
    which root the WebUI happened to pass.
    """
    primary, _origin = wt.clone_with_origin(tmp_path)
    session = wt.linked_worktree(primary, tmp_path / "session")
    wt.write(session / ".worktreeinclude", "selected.env\n")
    wt.write(session / "selected.env", "FROM_SELECTED=1\n")
    wt.write(primary / ".worktreeinclude", "primary.env\n")
    wt.write(primary / "primary.env", "FROM_PRIMARY=1\n")
    wt.commit(session, "manifest in the session tree", {"marker.txt": "x\n"})

    producer = wt.agent_producer()
    info = create_worktree_for_workspace(session, setup=producer)

    created = Path(info["path"])
    assert producer.calls == [str(session.resolve())]
    assert (created / "selected.env").read_text(encoding="utf-8") == "FROM_SELECTED=1\n"
    assert not (created / "primary.env").exists()


def test_relocate_preserves_dirty_tree(tmp_path):
    """Finding 12 (rounds 10-27): ``git worktree move`` is git's own relocation.
    A tree that is dirty (modified tracked file, untracked file, copied include)
    at creation time is byte-identical after the move, with HEAD and branch
    unchanged — so relocating cannot lose the session's work.
    """
    primary, _origin = wt.clone_with_origin(tmp_path)
    session = wt.linked_worktree(primary, tmp_path / "session")
    producer = _dirtying_producer()

    info = create_worktree_for_workspace(session, setup=producer)

    created = Path(info["path"])
    assert created.parent == primary.resolve() / ".worktrees"
    assert wt.snapshot(created) == producer.captured["before"]
    assert wt.head(created) == producer.captured["head"]
    assert wt.git(created, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == producer.captured["branch"]
    registered = wt.registry(primary)
    assert str(created) in registered
    assert f"worktree {session.resolve()}" in registered  # the source tree is untouched


# ── row 13: nothing is deleted, restored or re-copied ────────────────────────


def test_no_content_is_deleted_anywhere(tmp_path):
    """Finding 13 (rounds 10-27, ``94e98ddf``): the include cleanup could delete
    or replace the selected committed tree. No cleanup exists, so every path in
    the selected and main checkouts is unchanged by a creation — the only write
    is the ``.gitignore`` line the Agent added, which is put back byte-exactly.
    """
    primary, _origin = wt.clone_with_origin(tmp_path)
    session = wt.linked_worktree(primary, tmp_path / "session")
    wt.write(session / ".worktreeinclude", "local.env\n")
    wt.write(session / "local.env", "LOCAL=1\n")
    (session / "README.md").write_text("dirty session tree\n", encoding="utf-8")
    gitignore_before = (session / ".gitignore").read_bytes()
    primary_before = wt.snapshot(primary)
    session_before = wt.snapshot(session)

    create_worktree_for_workspace(session, setup=wt.agent_producer())

    assert wt.snapshot(session) == session_before
    primary_after = wt.snapshot(primary)
    unchanged = {
        name: digest for name, digest in primary_after.items() if name in primary_before
    }
    assert unchanged == primary_before, "no pre-existing path in the main root may change"
    added = set(primary_after) - set(primary_before)
    assert added, "the new tree must actually be there"
    assert all(name.startswith(".worktrees/") for name in added), added
    assert (session / ".gitignore").read_bytes() == gitignore_before


def test_tracked_gitignore_gains_only_the_entry(tmp_path):
    """``_ensure_worktrees_ignored`` appends one line and rewrites nothing.

    A tracked ``.gitignore`` that lacks the entry becomes ``M .gitignore`` in the
    main checkout — the accepted cost of keeping the session trees out of a
    status the user reads. What must never happen is a rewrite of bytes that were
    already there (the previous version of this test pre-seeded the entry, so it
    could not see this path at all). The property pinned here is that the prior
    content is an exact prefix of the new content.
    """
    primary, _origin = wt.clone_with_origin(tmp_path)
    wt.commit(
        primary,
        "track a gitignore without the worktrees entry",
        {".gitignore": "*.log\n"},
    )
    before = (primary / ".gitignore").read_bytes()
    session = wt.linked_worktree(primary, tmp_path / "session")

    create_worktree_for_workspace(session, setup=wt.agent_producer())

    after = (primary / ".gitignore").read_bytes()
    assert after.startswith(before), "a tracked .gitignore must never be rewritten"
    assert after[len(before):] == b".worktrees/\n"
    status = wt.git(primary, "status", "--porcelain").stdout.strip()
    assert status == "M .gitignore", status


def test_ack_include_tracked_file_not_restored(tmp_path):
    """Finding 13 (rounds 10-27): a tracked include entry must not be "restored"
    to its committed bytes. The Agent copies the file as it is in the selected
    checkout, and the WebUI leaves both files alone afterwards.
    """
    primary, _origin = wt.clone_with_origin(tmp_path)
    session = wt.linked_worktree(primary, tmp_path / "session")
    wt.write(session / ".worktreeinclude", "config.txt\n")
    wt.write(session / "config.txt", "committed\n")
    wt.commit(session, "track the include entry", {"config.txt": "committed\n"})
    (session / "config.txt").write_text("live edit\n", encoding="utf-8")

    info = create_worktree_for_workspace(session, setup=wt.agent_producer())

    created = Path(info["path"])
    assert (created / "config.txt").read_text(encoding="utf-8") == "live edit\n"
    assert (session / "config.txt").read_text(encoding="utf-8") == "live edit\n"
    # The include entry is tracked in the new tree's HEAD, so the copy lands as a
    # local modification of a tracked file — not as an untracked one.
    assert wt.git(created, "ls-files", "config.txt").stdout.strip() == "config.txt"
    assert wt.git(created, "status", "--porcelain").stdout == " M config.txt\n"


# ── row 14: base policy belongs to the Agent, untouched ─────────────────────


@requires_agent
def test_missing_cached_origin_head_uses_remote_default(tmp_path):
    """Finding 14 (rounds 10-28, ``0708fd70``): the reviewer's exact repro — no
    cached ``refs/remotes/origin/HEAD`` and two unpublished commits on the
    selected branch. The Agent's ``_resolve_worktree_base`` discovers the remote
    default itself and branches from the fetched tip; the WebUI does not
    re-derive it, so the new tree is at the remote tip, not at the session HEAD.
    """
    primary, _origin = wt.clone_with_origin(tmp_path)
    tip = wt.commit(primary, "publish", {"upstream.txt": "upstream\n"})
    wt.git(primary, "push", "-q", "origin", "main")
    session = wt.linked_worktree(primary, tmp_path / "session")
    wt.commit(session, "unpublished one", {"one.txt": "1\n"})
    wt.commit(session, "unpublished two", {"two.txt": "2\n"})
    wt.git(primary, "update-ref", "-d", "refs/remotes/origin/HEAD")

    info = create_worktree_for_workspace(session, setup=REAL_AGENT)

    created = Path(info["path"])
    assert created.parent == primary.resolve() / ".worktrees"
    assert wt.head(created) == tip
    assert not (created / "two.txt").exists()
    assert wt.head(session) != tip  # the session kept its own work


@requires_agent
def test_same_head_both_manifests_dirty_primary_clean_result(tmp_path):
    """Finding 14 (rounds 10-28): with the same HEAD on both sides and a dirty
    main checkout, the tree must carry the *selected* checkout's bytes — the
    tracked-path guard that retained the main root's dirty bytes is gone, because
    the WebUI never touches content after creation.
    """
    primary, _origin = wt.clone_with_origin(tmp_path)
    wt.write(primary / ".worktreeinclude", "settings.txt\n")
    wt.write(primary / "settings.txt", "committed\n")
    tip = wt.commit(primary, "settings file", {"settings.txt": "committed\n"})
    (primary / "settings.txt").write_text("dirty in the main root\n", encoding="utf-8")
    session = wt.linked_worktree(primary, tmp_path / "session")
    assert wt.head(session) == tip

    info = create_worktree_for_workspace(session, setup=REAL_AGENT, )

    created = Path(info["path"])
    assert created.parent == primary.resolve() / ".worktrees"
    assert (created / "settings.txt").read_text(encoding="utf-8") == "committed\n"
    assert (primary / "settings.txt").read_text(encoding="utf-8") == "dirty in the main root\n"
