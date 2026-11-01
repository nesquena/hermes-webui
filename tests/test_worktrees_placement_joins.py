"""The joins the placement decision has to survive: lock, registry, session.

A placement that only lives in a returned helper dict is not a placement. These
tests follow the decisions the reviewer traced on PR #8272 back into the state
that outlives the HTTP response:

* the Agent's ownership lock must still be recognised after the tree moves, so
  the stale-owner classifier can still reap it;
* the *landing* root must be registered where the maintenance sweep looks;
* the outcome — placement and the reason that produced it — must reach the
  session and its persisted payload, and a refused move must report the move's
  own error instead of the classifier's token.

They are green on their own: the git facts are asserted from git, and the
registry assertions use the installed Agent's own registry module (skipped when
no Agent is installed).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from api.worktree_placement import (
    PLACEMENT_MAIN,
    PLACEMENT_MOVE_FAILED,
    primary_worktree,
    relocate_worktree,
)
from api.worktrees import create_worktree_for_workspace
from tests import _worktree_git as wt


def _lock_reason(primary: Path, tree: Path) -> str | None:
    """The ``locked`` reason git reports for *tree*, or ``None`` if unlocked."""
    current: Path | None = None
    for line in wt.registry(primary).splitlines():
        if line.startswith("worktree "):
            current = Path(line[len("worktree "):].strip()).resolve()
        elif line == "locked" or line.startswith("locked "):
            if current == Path(tree).resolve():
                return line[len("locked"):].strip()
    return None


def _native_lock_re():
    """The installed Agent's ownership regex, or ``None`` without an Agent."""
    try:
        from hermes_cli.worktree_ops import _HERMES_LOCK_RE
    except Exception:  # noqa: BLE001 - no Agent checkout in this environment
        return None
    return _HERMES_LOCK_RE


# ── the ownership lock survives the move ────────────────────────────────────


def test_relocation_keeps_the_agents_ownership_lock(tmp_path):
    """The Agent locks every tree with ``hermes pid=<pid>``, and only that shape
    is classified as ownable. Writing a WebUI-only reason made the moved tree a
    permanent "unknown live" lock: kept by every sweep, even after the creator
    exited. The reason must travel with the tree verbatim.
    """
    primary = wt.init_repo(tmp_path / "primary")
    session = wt.linked_worktree(primary, tmp_path / "session")

    info = create_worktree_for_workspace(
        session, setup=wt.agent_producer(name="hermes-lock")
    )

    landing = Path(info["path"])
    assert landing.parent == primary.resolve() / ".worktrees"
    assert info["placement"] == PLACEMENT_MAIN
    assert _lock_reason(primary, landing) == wt.LOCK_REASON

    native = _native_lock_re()
    if native is not None:
        # The relocated tree is still the kind of lock the Agent can classify.
        assert native.fullmatch(_lock_reason(primary, landing) or "")


def test_failed_move_keeps_the_agents_ownership_lock(tmp_path):
    """A refused move leaves the tree where the Agent put it — so it must still
    carry the Agent's own lock reason, not a WebUI literal and not no lock.
    """
    primary = wt.init_repo(tmp_path / "primary")
    session = wt.linked_worktree(primary, tmp_path / "session")
    squatter = primary / ".worktrees" / "hermes-taken"
    squatter.mkdir(parents=True)

    info = create_worktree_for_workspace(
        session, setup=wt.agent_producer(name="hermes-taken")
    )

    created = Path(info["path"])
    assert info["placement"] == PLACEMENT_MOVE_FAILED
    assert _lock_reason(session, created) == wt.LOCK_REASON
    native = _native_lock_re()
    if native is not None:
        assert native.fullmatch(_lock_reason(session, created) or "")


# ── the landing root is registered where the sweep looks ────────────────────


def test_landing_root_is_registered_for_maintenance(tmp_path, monkeypatch):
    """The Agent registers the checkout it ran in; a tree moved under the main
    root is only swept if that *main* root is registered too — the sweep
    enumerates ``<registered root>/.worktrees/``. Fresh registry, one move.
    """
    registry = pytest.importorskip("hermes_cli.worktree_registry")
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert registry.registry_path() == home / "worktree_repos.json"
    assert registry.registered_repos() == []

    primary = wt.init_repo(tmp_path / "primary")
    session = wt.linked_worktree(primary, tmp_path / "session")

    info = create_worktree_for_workspace(session, setup=wt.agent_producer())

    landing = Path(info["path"])
    assert landing.parent.parent == primary.resolve()
    assert primary.resolve().as_posix() in registry.registered_repos()
    # And that is exactly what makes the moved tree discoverable: the sweep
    # enumerates the registered root's own .worktrees directory.
    sweep = sorted(p for p in (primary.resolve() / ".worktrees").iterdir() if p.is_dir())
    assert landing.resolve() in [p.resolve() for p in sweep]


def test_unrecorded_placement_registers_only_the_selected_checkout(tmp_path, monkeypatch):
    """No claim means no move: the tree stays in the selected checkout, which is
    the root the Agent already registered. Nothing else is claimed.
    """
    registry = pytest.importorskip("hermes_cli.worktree_registry")
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    _bare, tree = wt.bare_with_worktree(tmp_path)
    placement = primary_worktree(tree)
    info = create_worktree_for_workspace(tree, setup=wt.agent_producer())

    assert placement.primary is None
    assert Path(info["path"]).parent == tree.resolve() / ".worktrees"
    assert registry.registered_repos() == [tree.resolve().as_posix()]


# ── the outcome reaches the session and its persisted payload ───────────────


def test_placement_reaches_the_session_payload(tmp_path):
    """The helper's outcome must survive into the session the response is built
    from: ``Session`` fields, the persisted payload, and the reloaded session.
    A helper-return assertion alone would not cover this join.
    """
    from api.models import Session, new_session

    info = {
        "path": str(tmp_path / "tree"),
        "branch": "hermes/placed",
        "repo_root": str(tmp_path / "root"),
        "placement": PLACEMENT_MOVE_FAILED,
        "placement_reason": "destination exists: /nowhere",
        "created_at": 1.0,
    }

    session = new_session(workspace=str(tmp_path), worktree_info=info)
    assert session.worktree_placement == PLACEMENT_MOVE_FAILED
    assert session.worktree_placement_reason == "destination exists: /nowhere"
    assert session.compact()["worktree_placement"] == PLACEMENT_MOVE_FAILED

    session.save()
    reloaded = Session.load(session.session_id)
    assert reloaded is not None
    assert reloaded.worktree_placement == PLACEMENT_MOVE_FAILED
    assert reloaded.worktree_placement_reason == "destination exists: /nowhere"


def test_refused_move_reports_the_move_error_not_the_classifier_token(tmp_path):
    """The reason must name the step that actually decided the placement: a
    refused move reports its own error, not ``registry`` (the classification
    that had succeeded just before it).
    """
    primary = wt.init_repo(tmp_path / "primary")
    session = wt.linked_worktree(primary, tmp_path / "session")
    squatter = primary / ".worktrees" / "hermes-taken"
    squatter.mkdir(parents=True)

    info = create_worktree_for_workspace(
        session, setup=wt.agent_producer(name="hermes-taken")
    )

    assert info["placement"] == PLACEMENT_MOVE_FAILED
    assert re.search(r"destination exists", info["placement_reason"])
    assert info["placement_reason"] != "registry"


# ── unknown lock state never authorises an unlock ───────────────────────────


def _unlock_or_move_attempts(log: Path) -> list[str]:
    """The git invocations that could have moved or unlocked the tree.

    Empty is the point: on an uninspectable lock the module refuses *before*
    touching the tree, so the lock it could not read is never at risk.
    """
    return [
        line
        for line in wt.git_argv_since(log, 0)
        if line.startswith("worktree unlock ") or line.startswith("worktree move ")
    ]


def test_unknown_admin_dir_query_refuses_the_move_before_unlocking(tmp_path, monkeypatch):
    """An inspection failure is not "no lock": "there is no lock" and "I could
    not read the lock" are different facts, and only the first one authorises
    ``git worktree unlock``. A live ``hermes pid=<pid>`` lock must survive a
    relocation that could not be inspected.

    The fault is injected in the git shim for the admin-dir query only; unlock,
    move and lock stay real, so the refusal is the module's own decision and not
    an artefact of a dead git.
    """
    primary = wt.init_repo(tmp_path / "primary")
    created = wt.linked_worktree(primary, tmp_path / "created", branch="hermes-unknown")
    wt.git(primary, "worktree", "lock", "--reason", wt.LOCK_REASON, str(created))

    log = wt.git_argv_log(tmp_path, monkeypatch, fail_on=("--git-dir",))

    destination, reason = relocate_worktree(created, primary)

    assert destination is None
    assert reason and "lock state could not be inspected" in reason
    # Nothing moved, nothing unlocked: the Agent's ownership lock is intact.
    assert created.is_dir()
    assert not (primary / ".worktrees" / "created").exists()
    assert _lock_reason(primary, created) == wt.LOCK_REASON
    # ...and git was never even asked to unlock or move the tree.
    assert _unlock_or_move_attempts(log) == []


@pytest.mark.skipif(
    os.geteuid() == 0 or os.name == "nt",
    reason="needs a non-root POSIX user: root reads straight through a 000 mode",
)
def test_unreadable_lock_file_refuses_the_move_before_unlocking(tmp_path, monkeypatch):
    """A lock file that exists but cannot be read is ``unknown``, not
    ``unlocked``: read permission is not permission to unlink, so the tree keeps
    its place and its lock instead of being unlocked on a failed read.

    Only the lock-file *read* is faulted (mode 000); git itself stays fully
    functional, so the refusal is the module's own decision — and the argv log
    proves git was never asked to unlock or move the tree.
    """
    primary = wt.init_repo(tmp_path / "primary")
    created = wt.linked_worktree(primary, tmp_path / "created", branch="hermes-unreadable")
    admin = Path(
        wt.git(created, "rev-parse", "--path-format=absolute", "--git-dir").stdout.strip()
    )
    wt.git(primary, "worktree", "lock", "--reason", wt.LOCK_REASON, str(created))
    locked = admin / "locked"
    locked.chmod(0o000)
    log = wt.git_argv_log(tmp_path, monkeypatch)
    try:
        destination, reason = relocate_worktree(created, primary)
    finally:
        locked.chmod(0o644)

    assert destination is None
    assert reason and "lock state could not be inspected" in reason
    assert created.is_dir()
    assert not (primary / ".worktrees" / "created").exists()
    # The admin dir still holds the owner's reason, byte for byte.
    assert locked.read_text(encoding="utf-8").strip() == wt.LOCK_REASON
    assert _unlock_or_move_attempts(log) == []


def test_genuinely_unlocked_tree_moves_and_stays_unlocked(tmp_path):
    """The control: an inspected, genuinely unlocked tree is relocated and no
    lock is invented — this module only relocates, it never becomes an owner."""
    primary = wt.init_repo(tmp_path / "primary")
    created = wt.linked_worktree(primary, tmp_path / "created", branch="hermes-unlocked")

    destination, reason = relocate_worktree(created, primary)

    assert reason is None
    assert destination == primary.resolve() / ".worktrees" / "created"
    assert destination.is_dir()
    assert _lock_reason(primary, destination) is None


@pytest.mark.skipif(
    os.geteuid() == 0 or os.name == "nt",
    reason="needs a non-root POSIX user: root and Windows ignore the destination permissions",
)
def test_failed_move_after_unlock_restores_the_lock(tmp_path):
    """A real failed move *after* the unlock (unwritable landing parent, so git
    itself fails) must re-apply the tree's own lock where it still stands —
    otherwise the refused tree would sit unlocked and reapable."""
    primary = wt.init_repo(tmp_path / "primary")
    created = wt.linked_worktree(primary, tmp_path / "created", branch="hermes-refused")
    wt.git(primary, "worktree", "lock", "--reason", wt.LOCK_REASON, str(created))
    landing = primary / ".worktrees"
    landing.mkdir()
    landing.chmod(0o555)
    try:
        destination, reason = relocate_worktree(created, primary)
    finally:
        landing.chmod(0o755)

    assert destination is None
    assert reason and "git worktree move failed" in reason
    assert created.is_dir()
    assert _lock_reason(primary, created) == wt.LOCK_REASON

