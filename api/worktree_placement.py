"""Ask git where a worktree belongs — one rule, no path classification.

The placement invariant
-----------------------

**Git's own records decide where a session's worktree goes.** A directory is a
worktree root only if ``git rev-parse --show-toplevel`` answers it *and* the
first ``git worktree list --porcelain`` record corroborates it. Nothing else is
consulted: not a ``.git`` directory, not a ``.git`` file, not a filename, not a
depth bound, not an index, not the working tree's cleanliness, and never a
filesystem walk over user paths.

Everything here is a question to git about a directory the caller already has:

1. ``git -C <workspace> rev-parse --path-format=absolute --show-toplevel``
   answers *which checkout* the workspace is in (call it ``T``). It is never the
   metadata directory: ``--show-toplevel`` resolves ``core.worktree`` when it is
   set, so the bare/``modules``/``worktrees`` dirs are excluded by construction.
2. ``--git-dir`` (``G``) and ``--git-common-dir`` (``C``) say whether ``T``
   owns its own admin dir or borrows one.
3. The first ``git worktree list --porcelain`` record is the main worktree —
   the only directory git ever considers the repository's root — plus its
   ``bare``/``locked`` flags.

Then:

* ``G == C`` — the workspace's admin dir *is* the common dir, so git treats
  this directory as the main worktree: the root is ``T`` itself.
* ``G != C`` and the record is not bare — the root is the recorded main
  worktree, but only after git corroborates it from inside that directory
  (``--show-toplevel`` == candidate and ``--git-common-dir`` == ``C``). Git is
  asked twice so that a stale, pruned or metadata-path record cannot pass for a
  checkout.
* Anything unreadable, bare, or uncorroborated — including the documented
  ``git init --separate-git-dir=<d>/.git <checkout>`` layout, where git records
  ``<d>`` and ``<d>`` answers exactly like a plain repository — yields **no
  claim**. The caller records that as ``placement: 'selected-unrecorded'`` and
  keeps the tree where git created it. A missing claim is a first-class
  outcome, never a guess.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from api.subprocess_utils import windows_hide_flags

logger = logging.getLogger(__name__)

#: Placed under the repository's main root (``<root>/.worktrees/<name>``).
PLACEMENT_MAIN = "main"
#: Git records no main worktree for this directory; the tree stays where the
#: Agent created it.  A first-class outcome, not a failure.
PLACEMENT_UNRECORDED = "selected-unrecorded"
#: A relocation was attempted and refused (destination exists, locked tree, …).
PLACEMENT_MOVE_FAILED = "move-failed"
PLACEMENTS = (PLACEMENT_MAIN, PLACEMENT_UNRECORDED, PLACEMENT_MOVE_FAILED)

#: Position of ``.worktrees`` inside a repository root, as the Agent creates it.
WORKTREES_DIRNAME = ".worktrees"


def _run_git(
    args: list[str],
    cwd: str | Path,
    timeout: float = 5,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
        creationflags=windows_hide_flags(),
    )


def _rev_parse(cwd: str | Path, *args: str) -> Path | None:
    """Return one ``rev-parse`` answer as an absolute path, or ``None``.

    ``--path-format=absolute`` is git >= 2.31; on older builds the query fails
    and we fall back to the plain form resolved against *cwd*.  A failure of
    both is ``None`` — an unanswerable question is never a claim.
    """
    try:
        result = _run_git(["rev-parse", "--path-format=absolute", *args], cwd)
        if result.returncode != 0:
            result = _run_git(["rev-parse", *args], cwd)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    raw = (result.stdout or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(cwd).expanduser() / path
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path


@dataclass(frozen=True)
class Placement:
    """Where a session started in *workspace* should get its worktree.

    ``workspace`` is git's answer for the directory handed in (the checkout it
    is in). ``primary`` is the main worktree git records for that repository,
    or ``None`` when git does not corroborate one — the caller must then record
    the placement as unrecorded instead of inferring a root.
    """

    workspace: Path
    primary: Path | None
    reason: str

    @property
    def claimed(self) -> bool:
        return self.primary is not None


def _first_record(cwd: str | Path) -> tuple[Path | None, frozenset[str]]:
    """Return the first ``worktree list --porcelain`` record and its flags."""
    try:
        result = _run_git(["worktree", "list", "--porcelain"], cwd)
    except (OSError, subprocess.TimeoutExpired):
        return None, frozenset()
    if result.returncode != 0:
        return None, frozenset()
    block = (result.stdout or "").split("\n\n", 1)[0]
    path: Path | None = None
    flags: set[str] = set()
    for line in block.splitlines():
        if line.startswith("worktree "):
            raw = line[len("worktree "):].strip()
            path = _resolve(raw, cwd) if raw else None
        elif line.startswith("locked"):
            flags.add("locked")
        elif line.strip() in {"bare", "detached", "prunable"}:
            flags.add(line.strip())
    return path, frozenset(flags)


def _resolve(raw: str, cwd: str | Path) -> Path | None:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(cwd).expanduser() / path
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path


def _inside(path: Path, ancestor: Path) -> bool:
    return path == ancestor or ancestor in path.parents


def corroborate(candidate: Path | None, common_dir: Path | None) -> bool:
    """True only when git, asked *inside* the candidate, names it the root.

    This is the whole "is this a real checkout" test: the directory must not be
    (or live under) the metadata dir, it must report itself as its own
    toplevel, and it must belong to the same repository.
    """
    if candidate is None or common_dir is None:
        return False
    if _inside(candidate, common_dir):
        return False
    toplevel = _rev_parse(candidate, "--show-toplevel")
    if toplevel != candidate:
        return False
    return _rev_parse(candidate, "--git-common-dir") == common_dir


def primary_worktree(workspace: str | Path) -> Placement:
    """Classify nothing: ask git who owns this workspace's worktrees.

    Returns a :class:`Placement` whose ``primary`` is either git's recorded
    main worktree or ``None``.  Never raises for a directory git cannot answer
    for: the caller validates existence and the repository separately.
    """
    ws = Path(workspace).expanduser()
    try:
        ws = ws.resolve()
    except (OSError, RuntimeError):
        pass

    toplevel = _rev_parse(ws, "--show-toplevel")
    if toplevel is None:
        return Placement(ws, None, "not-a-repository")

    git_dir = _rev_parse(ws, "--git-dir")
    common_dir = _rev_parse(ws, "--git-common-dir")
    record, flags = _first_record(ws)
    if git_dir is None or common_dir is None or record is None:
        return Placement(toplevel, None, "query-failed")
    if "bare" in flags:
        return Placement(toplevel, None, "bare-repository")

    if git_dir == common_dir:
        # Git keeps this directory's admin dir at its own root, so git itself
        # treats it as the main worktree.  True for a plain repo, a
        # --separate-git-dir checkout and a submodule's own checkout alike.
        return Placement(toplevel, toplevel, "main-worktree")

    if record == common_dir:
        # A bare/--separate-git-dir repository points ``core.worktree`` at the
        # real checkout from inside the metadata dir: ask git for it there.
        candidate = _rev_parse(common_dir, "--show-toplevel")
        if corroborate(candidate, common_dir):
            return Placement(toplevel, candidate, "metadata-core-worktree")
        return Placement(toplevel, None, "metadata-unrecorded")

    if corroborate(record, common_dir):
        return Placement(toplevel, record, "registry")
    return Placement(toplevel, None, "registry-uncorroborated")


def _git_error(result: subprocess.CompletedProcess) -> str:
    stderr = (result.stderr or "").strip()
    stdout = (result.stdout or "").strip()
    for stream in (stderr, stdout):
        lines = [line for line in stream.splitlines() if line.strip()]
        if lines:
            return lines[-1].strip()
    return f"git exited {result.returncode}"


#: A tree's lock as read before the move. ``locked`` means a lock file exists
#: (its reason may be empty), ``unlocked`` that the admin dir was read and
#: carries no lock file, ``unknown`` that neither fact could be established.
LOCK_LOCKED = "locked"
LOCK_UNLOCKED = "unlocked"
LOCK_UNKNOWN = "unknown"


@dataclass(frozen=True)
class LockState:
    """What the tree's lock looked like *before* the move.

    ``unknown`` is a third outcome on purpose: "there is no lock" and "I could
    not tell whether there is a lock" are different facts, and only the first
    one authorises ``git worktree unlock``. Unlocking a tree whose native
    ``hermes pid=<pid>`` lock merely could not be read would silently drop the
    owner's protection for a later maintenance sweep.
    """

    state: str
    reason: str | None = None


def _lock_state(path: Path, cwd: Path) -> LockState:
    """The tree's own lock, verbatim; ``unknown`` when it cannot be inspected.

    The Agent locks every tree it creates with ``hermes pid=<pid>`` (or
    ``hermes session=<id> db=<state.db>``), and only that shape is recognised by
    its stale-owner classifier — any other text under a ``hermes`` prefix is
    kept as live forever. So the reason is read *before* the move and re-applied
    byte for byte afterwards: a relocated tree must stay exactly as ownable as
    the tree the Agent produced. Read from the admin dir git reports inside the
    tree, so a bare lock is re-applied as a bare lock.

    A failed admin-dir query, a missing admin dir, a timeout or an unreadable
    lock file is ``unknown`` — never ``unlocked``: read permission is not
    permission to unlink, and the caller refuses to unlock on ``unknown``.
    """
    try:
        result = _run_git(["rev-parse", "--path-format=absolute", "--git-dir"], path)
        if result.returncode != 0:
            result = _run_git(["rev-parse", "--git-dir"], path)
            raw = (result.stdout or "").strip() if result.returncode == 0 else ""
        else:
            raw = (result.stdout or "").strip()
        if not raw:
            return LockState(LOCK_UNKNOWN)
        admin = Path(raw)
        if not admin.is_absolute():
            admin = path / admin
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return LockState(LOCK_UNKNOWN)
    if not admin.is_dir():
        return LockState(LOCK_UNKNOWN)
    try:
        content = (admin / "locked").read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return LockState(LOCK_UNLOCKED)
    except (OSError, ValueError):
        return LockState(LOCK_UNKNOWN)
    return LockState(LOCK_LOCKED, content.strip(" \t\n\r\v\f"))


def _relock(path: Path, cwd: Path, reason: str | None) -> None:
    """Re-apply *reason*, the lock the tree had before the move (fail-soft).

    ``None`` means the tree was not locked, so nothing is re-applied — creating a
    lock the Agent never set would make this module the owner of a tree it only
    relocated.  A bare lock (``""``) is re-applied as a bare lock.
    """
    if reason is None:
        return
    args = ["worktree", "lock"]
    if reason:
        args += ["--reason", reason]
    args.append(str(path))
    try:
        _run_git(args, cwd, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def relocate_worktree(created: Path, primary: Path) -> tuple[Path | None, str | None]:
    """Move a freshly created worktree to ``<primary>/.worktrees/<name>``.

    Returns ``(destination, None)`` on success and ``(None, reason)`` when the
    move is refused — an error the caller records, never an exception and never
    a nested tree:

    * ``git worktree move`` onto an existing directory *nests* silently, so the
      destination is checked for existence first;
    * it also refuses a locked worktree (the Agent locks every tree it creates),
      so the tree is unlocked before the move and locked again after it.
    """
    destination = primary / WORKTREES_DIRNAME / created.name
    if destination.exists():
        return None, f"destination exists: {destination}"
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, f"could not create {destination.parent}: {exc}"

    # The ownership reason travels with the tree: the Agent's classifier only
    # accepts ``hermes pid=<pid>`` / ``hermes session=<id>``, so anything this
    # module writes instead would make the tree permanently un-reapable.
    lock = _lock_state(created, primary)
    if lock.state == LOCK_UNKNOWN:
        # Neither "locked" nor "unlocked" was established. Unlocking could drop
        # a live owner's lock, so the tree is left exactly where the Agent
        # created it, lock intact, and the refusal is reported as a move error.
        return None, f"lock state could not be inspected for {created}"
    reason = lock.reason if lock.state == LOCK_LOCKED else None
    try:
        _run_git(["worktree", "unlock", str(created)], primary)
        result = _run_git(
            ["worktree", "move", str(created), str(destination)],
            primary,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _relock(created, primary, reason)
        return None, f"git worktree move failed: {exc}"
    if result.returncode != 0:
        _relock(created, primary, reason)
        return None, f"git worktree move failed: {_git_error(result)}"
    _relock(destination, primary, reason)
    return destination, None
