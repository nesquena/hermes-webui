"""Helpers for WebUI-managed Hermes Agent git worktrees."""

from __future__ import annotations

import subprocess
import time
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import logging
import os

from api.subprocess_utils import windows_hide_flags

logger = logging.getLogger(__name__)

# Upper bound on ancestor steps taken while looking for the gitfile that owns a
# common git dir. A real checkout sits a handful of levels below the metadata
# parent; the bound only stops a pathological path, it is not a search radius.
# Running out of steps before the filesystem root is reached leaves the search
# incomplete (unknown ownership), never "no claim".
_CHECKOUT_WALK_MAX_DEPTH = 256

# How many ancestors above the metadata's grandparent are probed. Each is probed
# at its *immediate children* only (one listing per level), which is what keeps a
# probe of a large shared level such as ``$HOME``, ``/tmp`` or ``/`` affordable.
# Reaching the filesystem root ends the probe cleanly; stopping at this bound
# first makes the claim search incomplete.
#
# The probe deliberately does not descend below those children. Descending one
# level more would reach a checkout one directory below a *sibling* of the
# metadata's grandparent (``<base>/y/primary`` beside ``<base>/x/storage/.git``),
# but it also turns every ``$HOME/.venv``, ``~/node_modules`` or ``~/.ruff_cache``
# into a skip *outside* the candidate — which makes every project under that
# level unknown, i.e. it re-breaks the nesting this function exists to prevent.
# One level is the widest region that keeps ordinary repositories resolving.
_CHECKOUT_OWNING_GITDIR_MAX_UP = 64

# How many directory levels below each root the gitfile claim search looks.
# Every level is listed once and never walked twice; descent is decided before a
# directory is entered, so a large sibling subtree is never walked to completion
# (the ``rglob`` failure mode). Depth is not what grants ownership: when the
# search is *truncated* — by this bound or by the listing budget below — the
# ownership is unknown and the caller keeps its own checkout. Truncation is
# therefore strictly conservative, and raising the bound only makes the answer
# better (the real checkout instead of ``None``); it can never crown a
# ``--separate-git-dir`` storage dir.
_CHECKOUT_LISTING_DEPTH = 16

# Hard cap on directories descended into by one claim search. A plain repository
# has no claim to find, so the search runs to completion over its own tree and
# the probed outer levels; the budget keeps that finite for operator layouts with
# very many directories. Exhausting the budget is *truncation*, not "no claim":
# it must never be read as ownership.
_CHECKOUT_SEARCH_MAX_DIRS = 8192

# Directory names the claim search does not walk to completion. They hold
# dependency or tooling trees, so walking them costs far more than it can be
# worth on an ordinary working tree.
#
# A skip is a cost decision and never evidence either way: ``_skip_claim_probe``
# looks inside the tree for a claim on this metadata before the search moves on.
# A ``--separate-git-dir`` checkout parks two levels inside one of these
# (``node_modules/pkg/checkout``, ``.venv/src/checkout``), both beside the
# storage dir and inside the plain candidate, so "we chose not to walk it" must
# never become "there is nothing in it". See ``_claiming_checkout``.
_CHECKOUT_SKIP_DESCENT = frozenset(
    {
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "node_modules",
        "site-packages",
        "venv",
    }
)

# How deep the claim probe looks inside one skipped dependency directory. A
# ``--separate-git-dir`` checkout parked in a dependency tree sits two levels
# below it (``node_modules/pkg/checkout``), and a real dependency tree is far
# too large to walk, so the probe is bounded and *truncation* is reported back
# as such by ``_skip_claim_probe``. Neither inside nor outside the plain
# candidate is a skip taken on faith: skipping is a cost decision, and a
# decision to stop looking is not evidence that nothing is there.
_CHECKOUT_SKIP_PROBE_DEPTH = 4


def _run_git(args: list[str], cwd: str | Path, timeout: float = 2) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
        creationflags=windows_hide_flags(),
    )


def _resolve_path(path: str | Path | None) -> Path | None:
    if not path:
        return None
    try:
        return Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return Path(path).expanduser()


def _worktree_list_cwd(worktree_path: Path, repo_root: str | Path | None) -> Path | None:
    repo = _resolve_path(repo_root)
    if repo and repo.is_dir():
        return repo
    if worktree_path.is_dir():
        return worktree_path
    return None


def _parse_worktree_list_porcelain(output: str) -> set[str]:
    paths: set[str] = set()
    for line in str(output or "").splitlines():
        if not line.startswith("worktree "):
            continue
        path = line[len("worktree "):].strip()
        if not path:
            continue
        resolved = _resolve_path(path)
        paths.add(str(resolved or Path(path).expanduser()))
    return paths


def _worktree_listed(worktree_path: Path, repo_root: str | Path | None) -> bool:
    """Return whether git currently lists the worktree.

    False is a safe fallback for probe failures, not definitive orphan proof.
    Future cleanup UI must combine this with the rest of the status payload.
    """
    cwd = _worktree_list_cwd(worktree_path, repo_root)
    if cwd is None:
        return False
    try:
        result = _run_git(["worktree", "list", "--porcelain"], cwd)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    return str(worktree_path) in _parse_worktree_list_porcelain(result.stdout)


def _status_porcelain(worktree_path: Path) -> tuple[bool, int]:
    try:
        result = _run_git(
            ["status", "--porcelain", "--untracked-files=normal"],
            worktree_path,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, 0
    if result.returncode != 0:
        return False, 0
    lines = [line for line in result.stdout.splitlines() if line]
    return bool(lines), sum(1 for line in lines if line.startswith("??"))


def _ahead_behind(worktree_path: Path) -> dict:
    payload = {
        "ahead": 0,
        "behind": 0,
        "available": False,
        "upstream": None,
    }
    try:
        upstream = _run_git(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            worktree_path,
        )
    except (OSError, subprocess.TimeoutExpired):
        return payload
    if upstream.returncode != 0:
        return payload
    upstream_ref = upstream.stdout.strip()
    if not upstream_ref:
        return payload
    payload["upstream"] = upstream_ref
    try:
        counts = _run_git(
            ["rev-list", "--left-right", "--count", "HEAD...@{u}"],
            worktree_path,
        )
    except (OSError, subprocess.TimeoutExpired):
        return payload
    if counts.returncode != 0:
        return payload
    parts = counts.stdout.strip().split()
    if len(parts) != 2:
        return payload
    try:
        payload["ahead"] = max(0, int(parts[0]))
        payload["behind"] = max(0, int(parts[1]))
        payload["available"] = True
    except ValueError:
        pass
    return payload


def _locked_by_stream(session) -> bool:
    stream_id = getattr(session, "active_stream_id", None)
    if not stream_id:
        return False
    try:
        from api.config import STREAMS, STREAMS_LOCK

        with STREAMS_LOCK:
            return stream_id in STREAMS
    except Exception:
        return False


def _locked_by_terminal(session_id: str, worktree_path: Path) -> bool:
    try:
        from api.terminal import get_terminal

        term = get_terminal(session_id)
    except Exception:
        return False
    if not term:
        return False
    try:
        if not term.is_alive():
            return False
        terminal_workspace = _resolve_path(getattr(term, "workspace", None))
        return terminal_workspace == worktree_path
    except Exception:
        return False


def worktree_status_for_session(session) -> dict:
    """Return a read-only worktree status snapshot for a WebUI session."""
    raw_path = getattr(session, "worktree_path", None)
    if not raw_path:
        raise ValueError("Session is not worktree-backed")

    worktree_path = _resolve_path(raw_path)
    if worktree_path is None:
        raise ValueError("Session is not worktree-backed")

    exists = worktree_path.is_dir()
    status = {
        "path": str(worktree_path),
        "exists": bool(exists),
        "dirty": False,
        "untracked_count": 0,
        "ahead_behind": {
            "ahead": 0,
            "behind": 0,
            "available": False,
            "upstream": None,
        },
        "locked_by_stream": _locked_by_stream(session),
        "locked_by_terminal": _locked_by_terminal(
            getattr(session, "session_id", ""),
            worktree_path,
        ),
        "listed": _worktree_listed(
            worktree_path,
            getattr(session, "worktree_repo_root", None),
        ),
    }
    if not exists:
        return status

    dirty, untracked_count = _status_porcelain(worktree_path)
    status["dirty"] = dirty
    status["untracked_count"] = untracked_count
    status["ahead_behind"] = _ahead_behind(worktree_path)
    return status


def remove_worktree_for_session(session, *, force: bool = False) -> dict:
    """Remove a session's git worktree from disk.

    Returns status dict with keys: ok, removed_path, warnings.
    Raises ValueError for terminal blockers (locked by stream/terminal,
    dirty with force=False).
    """
    raw_path = getattr(session, "worktree_path", None)
    if not raw_path:
        raise ValueError("Session is not worktree-backed")

    worktree_path = _resolve_path(raw_path)
    if worktree_path is None:
        raise ValueError("Session is not worktree-backed")

    # Read current status before removal
    status = worktree_status_for_session(session)

    if not status["exists"]:
        return {
            "ok": True,
            "removed_path": str(worktree_path),
            "warnings": ["Worktree directory no longer exists on disk."],
        }

    warnings = []

    # Guard: locked by stream
    if status["locked_by_stream"]:
        raise ValueError("Worktree is locked by an active streaming session")

    # Guard: locked by terminal
    if status["locked_by_terminal"]:
        raise ValueError("Worktree is locked by an active terminal session")

    # Guard: local changes and unpushed commits without explicit force.
    if status["dirty"] and not force:
        raise ValueError(
            "Worktree has uncommitted changes. Use force=true to override."
        )
    if status["untracked_count"] > 0:
        if force:
            warnings.append(
                f"{status['untracked_count']} untracked file(s) will be removed."
            )
        else:
            raise ValueError(
                f"Worktree has {status['untracked_count']} untracked file(s). "
                "Use force=true to override."
            )
    ahead = int((status.get("ahead_behind") or {}).get("ahead") or 0)
    if ahead > 0:
        if force:
            warnings.append(f"{ahead} unpushed commit(s) will be removed.")
        else:
            raise ValueError(
                f"Worktree has {ahead} unpushed commit(s). "
                "Use force=true to override."
            )

    # Remove the worktree — must run from the repo root, not the worktree dir
    repo_root = getattr(session, "worktree_repo_root", None)
    if not repo_root:
        raise ValueError("Session missing worktree_repo_root")

    # Unlock the creation-time bookkeeping lock before removing (fail-soft).
    # The agent locks every worktree it creates (cli._setup_worktree), and git
    # refuses to remove a locked worktree with a single --force, so without
    # this every WebUI-created worktree is unremovable.  The dirty/untracked/
    # unpushed/stream/terminal guards above are the real safety layer, not
    # git's lock — mirrors the agent's own _cleanup_worktree ordering.
    try:
        _run_git(["worktree", "unlock", str(worktree_path)], str(repo_root), timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass  # already unlocked / never locked — non-fatal

    try:
        remove_args = ["worktree", "remove"]
        if force:
            remove_args.append("--force")
        remove_args.append(str(worktree_path))
        result = _run_git(remove_args, str(repo_root), timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"Failed to remove worktree: {exc}") from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").strip().split("\n")[-1]
        raise ValueError(
            f"git worktree remove failed: {stderr or result.stdout.strip()}"
        )

    # Prune in case the worktree dir was already gone
    try:
        _run_git(
            ["worktree", "prune"],
            str(repo_root),
            timeout=5,
        )
    except Exception:
        pass

    return {
        "ok": True,
        "removed_path": str(worktree_path),
        "warnings": warnings or None,
    }


def find_git_repo_root(workspace: str | Path) -> Path:
    """Return the enclosing git repo root for *workspace*.

    Use git itself instead of checking ``workspace/.git`` so nested workspaces
    and linked git worktrees are both handled correctly.
    """
    ws = Path(workspace).expanduser().resolve()
    if not ws.is_dir():
        raise ValueError("Workspace path does not exist or is not a directory")
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=ws,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
            creationflags=windows_hide_flags(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Workspace is not inside a git repository") from exc
    if result.returncode != 0:
        raise ValueError("Workspace is not inside a git repository")
    root = result.stdout.strip()
    if not root:
        raise ValueError("Workspace is not inside a git repository")
    return Path(root).expanduser().resolve()


def _git_toplevel(cwd: str | Path) -> Path | None:
    """``git rev-parse --show-toplevel`` for *cwd*, or None when it is not a checkout."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--show-toplevel"],
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
            creationflags=windows_hide_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    if not out:
        return None
    path = Path(out).expanduser()
    return path.resolve() if path.is_dir() else None


def _common_git_dir(cwd: str | Path) -> Path | None:
    """``git rev-parse --git-common-dir`` as an absolute path, or None."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
            creationflags=windows_hide_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return Path(out).expanduser() if out else None


def find_main_worktree_root(workspace: str | Path) -> Path | None:
    """Return the *main* working-tree root that owns the workspace's repo.

    ``find_git_repo_root`` returns ``git rev-parse --show-toplevel``, which for
    a session started inside a linked worktree (e.g. ``<repo>/.worktrees/x``) is
    that worktree itself. Passing it to the agent's ``_setup_worktree`` nests
    the new tree *inside* the current one, and no pruner ever reclaims a nest.

    The answer is derived from the **common git dir**, which is not always
    ``<main>/.git``: ``--separate-git-dir=X`` and submodules put the metadata
    elsewhere. One rule decides, and it never looks at a directory's name, at
    stray files, at the index or at committed content — all of those describe a
    ``--separate-git-dir`` *storage* dir exactly as well as a real checkout,
    since git even reports storage as a working tree (its basename is ``.git``):

        the working tree is the directory whose ``.git`` **gitfile** claims
        this metadata — the workspace's own ancestry first, then the metadata's
        parent, its parent, and the immediate children of each ancestor above
        that (a plain repository's parent dir holds the metadata itself as
        ``.git``).

    A gitfile naming a per-worktree admin dir (``<common>/worktrees/<name>``)
    is **not** a claim: it belongs to a linked worktree, and its own working
    tree is never the primary this function is asked for. ``core.worktree`` is
    a separate recorded fact and is accepted when git writes it.

    When no claim exists the layout is ambiguous — a ``--separate-git-dir``
    storage dir is shaped exactly like a plain checkout — and the plain layout
    is accepted only when the claim search *finished*: no unreadable level, no
    listing budget exhausted and no depth cut below an ancestor level. A skipped
    dependency directory is not a gap on faith: the search probes inside it for a
    claim (see ``_skip_claim_probe``) before deciding, so a checkout parked at
    ``node_modules/pkg/checkout`` is found instead of being read as "nothing
    there". Any remaining gap leaves ownership **unknown** and the answer
    ``None``, so the caller keeps the workspace's own toplevel — which nests at
    worst, but never starts a session inside git's bookkeeping. The one
    documented limit of the search region is that a claim nested deeper than one
    level below an ancestor above the metadata's grandparent is not looked for;
    see ``_claiming_checkout``. Because that boundary is a region limit and not a
    gap, a plain layout is still accepted there: when the workspace is *not*
    inside the claiming checkout (a session opened on the storage dir itself),
    the metadata dir may be promoted as the repo root.

    A claim found below the plain candidate is layout-ambiguous on its own — the
    real checkout of separate storage, or a stale alias left inside the real
    checkout — so ``_prefer_checkout_over_plain`` asks git which of the two
    directories still carries the committed tree before answering.
    """
    ws = Path(workspace).expanduser().resolve()
    if not ws.is_dir():
        return None

    common_path = _common_git_dir(ws)
    if common_path is None:
        return None
    try:
        common_resolved = common_path.resolve()
    except OSError:
        return None

    # A linked worktree is never the main tree: unnest to whatever owns the
    # metadata, never to the workspace's own toplevel.
    if not _is_linked_worktree(ws, common_path):
        owner = _checkout_from_ancestry(ws, common_resolved)
        if owner is not None:
            # Same adjudication as below: the workspace's own ancestors can
            # hold either the real checkout or a stale alias of the plain one.
            return _prefer_checkout_over_plain(owner, common_resolved)

    # The plain layout would crown the metadata's parent; that directory is the
    # scope every stop is judged against.
    scope = common_resolved.parent if common_resolved.name == ".git" else None
    owner, search_complete = _claiming_checkout(common_resolved, scope=scope)
    if owner is not None:
        # A claim below the plain candidate is layout-ambiguous: real checkout
        # of separate storage, or a stale alias inside the real checkout. Git's
        # own view of which directory holds the committed tree decides.
        return _prefer_checkout_over_plain(owner, common_resolved)

    # No gitfile claimed the metadata. Decide from recorded topology only, and
    # only when the claim search really finished: a truncated search — or one
    # that had to skip directories outside the candidate — says nothing about
    # who owns the metadata, so "found nothing" stays unknown.
    candidate = _topology_candidate(
        common_path, common_resolved, claim_search_complete=search_complete
    )
    if candidate is not None:
        return candidate

    recorded = _core_worktree(common_path)
    if recorded is not None:
        try:
            recorded_resolved = recorded.resolve()
        except OSError:
            recorded_resolved = None
        if (
            recorded_resolved is not None
            and recorded.is_dir()
            and _git_toplevel(recorded) == recorded_resolved
        ):
            return recorded_resolved

    return None


def _core_worktree(common_path: Path) -> Path | None:
    """``core.worktree`` for the repository at *common_path*, as an absolute path."""
    try:
        result = subprocess.run(
            ["git", "--git-dir", str(common_path), "config", "--get", "core.worktree"],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
            creationflags=windows_hide_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    raw = result.stdout.strip() if result.returncode == 0 else ""
    if not raw:
        return None
    wt = Path(raw).expanduser()
    if not wt.is_absolute():
        wt = common_path / wt
    return wt


def _checkout_from_ancestry(start: Path, common_resolved: Path) -> Path | None:
    """The nearest ancestor-or-self of *start* whose gitfile claims the metadata.

    Only a gitfile pointing **straight** at the common dir counts: a linked
    worktree's gitfile names ``<common>/worktrees/<name>`` and belongs to a
    different working tree. Depth is bounded only against a symlink loop.
    """
    try:
        current = start.resolve()
    except OSError:
        return None
    for _ in range(_CHECKOUT_WALK_MAX_DEPTH):
        owner = _gitfile_owner(current, common_resolved)
        if owner is not None:
            return owner
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return None


def _gitfile_owner(directory: Path, common_resolved: Path) -> Path | None:
    """*directory*, when its ``.git`` gitfile points straight at the metadata."""
    gitfile = directory / ".git"
    try:
        if not gitfile.is_file():
            return None
        text = gitfile.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    pointed = Path(text.split(":", 1)[1].strip())
    if not pointed.is_absolute():
        pointed = directory / pointed
    try:
        if pointed.resolve() != common_resolved:
            return None
    except OSError:
        return None
    toplevel = _git_toplevel(directory)
    if toplevel is None:
        return None
    try:
        return toplevel if toplevel == directory.resolve() else None
    except OSError:
        return None


def _claiming_checkout(
    common_resolved: Path, *, scope: Path | None
) -> tuple[Path | None, bool]:
    """A checkout whose gitfile claims the metadata, and whether we searched all.

    Returns ``(owner, complete)``. ``owner`` is a working tree whose ``.git``
    gitfile points straight at this metadata; that claim is the only
    authoritative evidence of who owns separate metadata, and the first one
    found wins.

    ``complete`` says whether the search covered its whole region **and** every
    stop inside that region was sound. It is ``False`` — unknown ownership, not
    "no claim" — when any of these happened:

    * a level could not be listed, or the listing budget ran out: we could not
      look;
    * a level was cut by :data:`_CHECKOUT_LISTING_DEPTH`: a claim may sit below;
    * the ancestor probe ended before the filesystem root;
    * a skipped dependency directory could not be probed completely — the shared
      listing budget ran out inside it.

    Skipping a dependency directory is a cost decision, so it is never read as
    evidence in either direction: :func:`_skip_claim_probe` looks inside it for a
    claim and reports whether it covered the region it was given. That is what
    keeps an ordinary repository resolving beside an unrelated ``node_modules``
    while a checkout parked in one is still found.

    The region is fixed: the metadata's parent and its parent, each listed with
    bounded descent, then the **immediate children** of every ancestor up to the
    filesystem root. A checkout that claims this metadata is the metadata's
    sibling (its parent's subtree) or an immediate child of one of those
    ancestors. Two limits follow from that and are deliberate: a claim nested
    below a *child* of an ancestor above the metadata's grandparent
    (``<base>/y/primary`` beside ``<base>/x/storage/.git``), and a claim nested
    deeper than one level inside another repository's tree, are not looked for.
    Widening either would turn the user's own ``~/.venv``, ``~/node_modules`` or
    ``~/.ruff_cache`` into skips outside the candidate and leave every project
    under that level unknown.

    A directory that is itself a working tree (it holds a ``.git`` entry) is
    never descended into — it is a checkout, not a container of one — but when
    it lies outside *scope* its immediate children are checked first, because a
    checkout nested inside another repository's working tree still claims this
    metadata and is the real working tree.

    The metadata's parent is tried first — for a plain repository the parent
    *is* the working tree, but its ``.git`` is a directory rather than a
    gitfile, so nothing is claimed here — then the ancestors, so a checkout
    sitting beside the metadata (``<base>/storage/.git`` with the checkout in
    ``<base>/elsewhere/…``, or above ``<base>`` entirely) is found.
    """
    parent = common_resolved.parent
    outer = parent.parent
    seen: set[str] = set()
    incomplete = False

    # Bounded descent for the two levels a claim actually sits at; above that,
    # one level per ancestor (see the region description above).
    roots: list[tuple[Path, int]] = [
        (parent, _CHECKOUT_LISTING_DEPTH),
        (outer, _CHECKOUT_LISTING_DEPTH),
    ]
    ancestor = outer.parent
    reached_root = False
    for _ in range(_CHECKOUT_OWNING_GITDIR_MAX_UP):
        if not ancestor.is_dir():
            break
        if ancestor == ancestor.parent or ancestor == Path("/"):
            reached_root = True
            break
        roots.append((ancestor, 0))
        ancestor = ancestor.parent
    if not reached_root:
        # Ancestors we never probed: a claim may live there, so this search
        # cannot testify that the metadata has no claiming checkout.
        incomplete = True
    if parent.name == "modules" and parent.parent.name == ".git":
        # ``<super>/.git/modules/<name>``: the submodule lives in ``<super>/<name>``.
        roots.append((parent.parent.parent, _CHECKOUT_LISTING_DEPTH))

    budget = [_CHECKOUT_SEARCH_MAX_DIRS]
    for root, limit in roots:
        # A git metadata directory is never a container of checkouts (the
        # submodule or storage checkout lives beside it, not inside it), so
        # walking it would only spend the budget on ``objects/``.
        if root.name == ".git":
            continue
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack:
            current, depth = stack.pop()
            try:
                entries = sorted(os.scandir(current), key=lambda entry: entry.name)
            except OSError:
                # Unreadable level: we could not look, so the answer is unknown.
                return None, False
            children: list[tuple[Path, int]] = []
            for entry in entries:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if entry.name == ".git":
                    continue
                child_str = entry.path
                if child_str in seen:
                    continue
                seen.add(child_str)
                # One lstat answers both questions about this directory: does a
                # ``.git`` entry exist (a checkout, or a claim), and is it a
                # gitfile (a claim on our metadata)?
                git_entry = os.path.join(child_str, ".git")
                has_git = os.path.lexists(git_entry)
                if has_git and os.path.isfile(git_entry):
                    owner = _gitfile_owner(Path(child_str), common_resolved)
                    if owner is not None:
                        return owner, True
                # Would this directory have been entered at all? A level that is
                # probed at its immediate children only (an ancestor above the
                # metadata's grandparent) is a region boundary: nothing below it
                # is in the region, so a stop there hides nothing.
                descendable = (
                    depth + 1 < _CHECKOUT_LISTING_DEPTH and depth + 1 <= limit
                )
                if descendable:
                    if budget[0] <= 0:
                        return None, False
                    budget[0] -= 1
                child = Path(child_str)
                # Dependency and tooling trees are not walked to completion, but
                # a skip is a cost decision, never evidence: a
                # ``--separate-git-dir`` checkout parks two levels inside one of
                # them (``node_modules/pkg/checkout``), inside the plain
                # candidate as well as beside it. Probe the tree instead of
                # guessing, and let the probe say whether stopping left a gap.
                if entry.name in _CHECKOUT_SKIP_DESCENT:
                    if descendable:
                        skip_claim, exhausted = _skip_claim_probe(
                            child, common_resolved, budget
                        )
                        if skip_claim is not None:
                            return skip_claim, True
                        if exhausted:
                            incomplete = True
                    continue
                if has_git:
                    # A checkout of some repository: not a container of one.
                    # A checkout nested immediately inside it still claims this
                    # metadata, so look one level down before deciding.
                    if descendable and not _within_scope(child, scope):
                        nested = _nested_claim(child, common_resolved)
                        if nested is not None:
                            return nested, True
                    continue
                if depth + 1 >= _CHECKOUT_LISTING_DEPTH:
                    # A claim may sit below the cap, so this is truncation.
                    incomplete = True
                    continue
                if not descendable:
                    # Region boundary of an ancestor probe (one level only).
                    continue
                children.append((child, depth + 1))
            stack.extend(reversed(children))

    return None, not incomplete


def _skip_claim_probe(
    root: Path, common_resolved: Path, budget: list[int]
) -> tuple[Path | None, bool]:
    """Look for a gitfile claim inside a skipped dependency directory.

    Returns ``(owner, exhausted)``. A dependency tree is never walked to
    completion, so the probe stops at :data:`_CHECKOUT_SKIP_PROBE_DEPTH`; that
    depth is a documented region limit rather than a gap, because a checkout
    parked in a dependency tree sits two levels below it and every real
    ``node_modules`` nests far deeper than any checkout does. Running out of the
    shared listing *budget* is different: it means the probe stopped before the
    region it was given was covered, and it answers ``exhausted=True`` so the
    caller reports unknown ownership instead of "no claim".
    """
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError:
            return None, True
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if entry.name == ".git":
                continue
            child = Path(entry.path)
            if os.path.lexists(os.path.join(entry.path, ".git")):
                # A checkout of some repository: the claim, or not ours.
                owner = _gitfile_owner(child, common_resolved)
                if owner is not None:
                    return owner, False
                continue
            if depth + 1 >= _CHECKOUT_SKIP_PROBE_DEPTH:
                continue
            if budget[0] <= 0:
                return None, True
            budget[0] -= 1
            stack.append((child, depth + 1))
    return None, False


def _holds_its_committed_tree(checkout: Path) -> bool | None:
    """True when *checkout*'s working tree still carries its committed paths.

    Decided by git, not by this module: ``status`` with untracked files ignored
    reports a committed path that is missing or modified on disk. A
    ``--separate-git-dir`` storage dir, a stale gitfile alias and an operator's
    note dump all report deleted committed paths, while the directory that
    really holds the files does not. Untracked content is ignored on purpose —
    every working tree carries build output and dependency trees — and ``None``
    means git could not answer, which is not evidence either way.
    """
    try:
        result = _run_git(
            ["status", "--porcelain", "--untracked-files=no"], checkout, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return not result.stdout.strip()


def _prefer_checkout_over_plain(claimant: Path, common_resolved: Path) -> Path | None:
    """Pick between a gitfile claimant and a plain layout that owns the metadata.

    When the plain candidate's own ``.git`` *is* this metadata and a gitfile
    below it also names this metadata, the two layouts are identical on disk
    and mean opposite things:

    * the claimant is the real checkout of a ``--separate-git-dir`` repository
      whose storage dir sits above it — ``storage-only/node_modules/pkg/checkout``
      (the plain candidate is then just the storage dir);
    * the claimant is a stale alias left inside the real checkout —
      ``primary/new/deep/.git`` pointing at ``primary/.git`` (the plain
      candidate is then the real checkout).

    Layout cannot tell them apart, so the question asked is the one git can
    answer: which directory still holds the committed tree. The other is
    bookkeeping. When neither or both answer -- or when the probe itself fails
    -- ownership stays unknown and the caller keeps its own checkout, never a
    session inside git's bookkeeping.

    Only that ambiguous pair is adjudicated. A claimant that is not below the
    plain candidate is an ordinary external separate checkout: it is not
    competing with the storage dir, so its claim stands whether or not the
    primary happens to be dirty. Unknown probe results are likewise not an
    affirmative ownership answer — treating them as one reauthorized a stale
    alias the moment `git status` failed.
    """
    parent = _as_dir(common_resolved.parent)
    if (
        parent is None
        or common_resolved.name != ".git"
        or not _plain_git_dir_owner(parent, common_resolved)
    ):
        # No plain layout to weigh against: the claim is the only evidence.
        return claimant
    if parent == claimant:
        return claimant
    # Only the AMBIGUOUS pair is adjudicated: the shapes this function exists
    # for both have the claimant BELOW the plain candidate -- a separate
    # checkout nested in its storage dir, or a stale alias left inside the
    # real checkout. An external separate checkout that sits elsewhere on
    # disk is not ambiguous at all, and consulting cleanliness for it revoked
    # a perfectly good checkout whenever the primary had any ordinary
    # modified/staged/deleted state (both sides answer "does not hold", so
    # resolution went unknown and creation nested under the linked checkout
    # instead of the real one). Ordinariness is not evidence of ownership.
    try:
        claimant_resolved = claimant.resolve()
    except OSError:
        # Cannot prove the pair is ambiguous, so do not adjudicate it.
        return claimant
    if parent not in claimant_resolved.parents:
        return claimant
    claim_holds = _holds_its_committed_tree(claimant)
    plain_holds = _holds_its_committed_tree(parent)
    if claim_holds is None or plain_holds is None:
        # Unknown is NOT an affirmative answer. Accepting the claim here
        # reauthorized a stale alias whenever the status probe failed
        # (OSError, timeout, or a nonzero exit): allocation then nested under
        # `primary/new/deep`, the alias, instead of the real checkout. Keep
        # ownership unknown so the caller keeps its own checkout, exactly as
        # for the both/neither case below.
        return None
    if claim_holds == plain_holds:
        # Both hold it (a duplicate export) or neither does (an empty
        # repository): nothing separates them, so do not guess.
        return None
    return claimant if claim_holds else parent


def _nested_claim(checkout: Path, common_resolved: Path) -> Path | None:
    """A checkout one level inside *checkout* whose gitfile claims the metadata.

    ``git init --separate-git-dir=<storage>/.git <otherrepo>/checkout`` puts a
    real working tree of this repository inside another repository's tree. The
    search never descends into the outer checkout, so this is the one level it
    looks at before declaring the stop sound.
    """
    try:
        entries = sorted(os.scandir(checkout), key=lambda entry: entry.name)
    except OSError:
        return None
    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        if entry.name == ".git":
            continue
        owner = _gitfile_owner(Path(entry.path), common_resolved)
        if owner is not None:
            return owner
    return None


def _within_scope(path: Path, scope: Path | None) -> bool:
    """True when *path* is *scope* or below it — inside the plain candidate."""
    if scope is None:
        return False
    try:
        return path == scope or path.is_relative_to(scope)
    except (OSError, ValueError):
        return False


def _topology_candidate(
    common_path: Path, common_resolved: Path, *, claim_search_complete: bool
) -> Path | None:
    """The working tree git's own records tie to this metadata, or ``None``.

    Reached only after no gitfile claimed the metadata. Two recorded facts can
    still name a working tree, in this order:

    * ``core.worktree``, which names it outright (a ``--separate-git-dir``
      checkout may carry it);
    * ``<tree>/.git`` *is* this metadata (a real directory, not a gitfile) — the
      plain layout, where the parent is the working tree.

    Both are *layout* facts, never content or presence facts. A working tree owns
    its repository whether its files are clean, modified, staged, deleted or
    entirely rewritten, so no index query, ``git status`` or per-path existence
    test takes part in this decision: an ordinary primary that deleted a tracked
    leaf still owns its repository. An empty index is not a special case either —
    a fresh ``init`` tracks nothing and still owns its parent.

    A linked admin dir (``commondir``) is never a primary. The plain layout is
    only trusted when the gitfile claim search actually finished
    (``claim_search_complete``): a ``--separate-git-dir`` storage dir named
    ``.git`` has exactly the same shape as a plain checkout, so its ownership is
    decided by the claim its real checkout makes elsewhere — and when that claim
    was not found because the search was truncated (budget exhausted, a level
    unreadable, an ancestor unprobed), the answer is ``None``. Unknown ownership
    stays unknown, and the caller keeps the checkout it has: nesting at worst,
    never a session inside git's bookkeeping. The exception is the documented
    search-region boundary (see ``find_main_worktree_root``): a claim that lives
    beyond the region is not a gap, so the plain layout is accepted there while
    the workspace is not inside the claiming checkout. When a claim *is* found
    and the plain layout also owns the metadata, this function has already been
    preceded by ``_prefer_checkout_over_plain``, so the ambiguous pair never
    reaches here unresolved.
    """
    recorded = _core_worktree(common_path)
    if recorded is not None:
        resolved = _as_dir(recorded)
        if resolved is not None:
            return resolved

    try:
        if (common_resolved / "commondir").is_file():
            # A per-worktree admin dir: never a primary checkout.
            return None
    except OSError:
        return None

    parent = _as_dir(common_resolved.parent)
    if parent is None:
        return None
    # The plain layout: the parent's own ``.git`` *is* this metadata directory.
    # Shape alone cannot tell the parent of a ``--separate-git-dir`` storage dir
    # apart from a plain checkout — both hold a ``.git`` directory, both are
    # reported as the toplevel by git, and ``git worktree list`` names both as the
    # primary worktree — so the claim search above is what refuses storage. Here
    # we only accept the layout once that search provably found no claim.
    if (
        claim_search_complete
        and common_resolved.name == ".git"
        and _plain_git_dir_owner(parent, common_resolved)
    ):
        return parent
    return None


def _plain_git_dir_owner(parent: Path, common_resolved: Path) -> bool:
    """True when *parent*'s own ``.git`` is this metadata directory.

    The plain layout, decided without asking git to interpret a gitfile: the
    parent holds a ``.git`` *directory* that is the metadata itself. This is a
    layout fact only — a storage dir's parent holds the very same ``.git``
    directory (git's bookkeeping lives there too), which is why it is not
    ownership on its own.
    """
    try:
        parent_git = parent / ".git"
        if not parent_git.is_dir():
            return False
        return parent_git.samefile(common_resolved)
    except OSError:
        return False


def _as_dir(raw: Path) -> Path | None:
    """*raw* resolved, when it is a directory."""
    try:
        resolved = raw.resolve()
    except OSError:
        return None
    try:
        return resolved if resolved.is_dir() else None
    except OSError:
        return None


def _is_linked_worktree(workspace: Path, common_path: Path) -> bool:
    """True when *workspace* is a linked worktree of the repo at *common_path*.

    Decided by git itself: the per-worktree admin dir of a linked worktree sits
    under ``<common>/worktrees/<name>`` and ``--git-dir`` reports that path,
    while a primary checkout reports the common dir (or ``<checkout>/.git``,
    which resolves to it). Comparing the two paths avoids guessing from the
    ``.git`` shape — the whole point of the #8162 fix.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-dir"],
            cwd=str(workspace),
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
            creationflags=windows_hide_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    out = result.stdout.strip()
    if not out:
        return False
    try:
        git_dir = Path(out).expanduser().resolve()
        return git_dir != common_path.resolve()
    except OSError:
        return False


def _setup_agent_worktree(repo_root: str) -> dict:
    try:
        import importlib.util
        from pathlib import Path

        from api.config import _AGENT_DIR  # Hermes Agent source root

        # Use importlib to load cli.py from its absolute path instead of a bare
        # ``from cli import _setup_worktree``.  The bare import is vulnerable to
        # namespace-package shadowing — any third-party package (e.g. the ``cli``
        # namespace shipped by stringzilla) that creates a cli/ directory in
        # site-packages will preempt the real hermes-agent/cli.py module.
        cli_path = str(Path(_AGENT_DIR) / "cli.py")
        spec = importlib.util.spec_from_file_location(
            "hermes_cli_worktree", cli_path,
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(
                f"Could not locate hermes-agent worktree helper at {cli_path}"
            )
        cli_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli_mod)
        _setup_worktree = cli_mod._setup_worktree
    except Exception as exc:
        raise RuntimeError("Hermes Agent worktree helper is unavailable") from exc
    output = StringIO()
    with redirect_stdout(output), redirect_stderr(output):
        info = _setup_worktree(repo_root)
    emitted = output.getvalue().strip()
    if emitted:
        logger.debug("Hermes Agent worktree helper output: %s", emitted)
    if not info:
        raise RuntimeError("Hermes Agent failed to create a git worktree")
    return info


def create_worktree_for_workspace(workspace: str | Path) -> dict:
    # Place the tree under the *main* working tree, never under the current
    # linked worktree: a workspace that is itself a worktree (the common case
    # once webui_state/last_workspace.txt carries a session's tree) would
    # otherwise nest the new tree inside it, out of reach of every pruner.
    repo_root = find_main_worktree_root(workspace) or find_git_repo_root(workspace)
    info = _setup_agent_worktree(str(repo_root))
    path = info.get("path")
    branch = info.get("branch")
    if not path or not branch:
        raise RuntimeError("Hermes Agent returned incomplete worktree metadata")
    return {
        "path": str(Path(path).expanduser().resolve()),
        "branch": str(branch),
        "repo_root": str(Path(info.get("repo_root") or repo_root).expanduser().resolve()),
        "created_at": time.time(),
    }
