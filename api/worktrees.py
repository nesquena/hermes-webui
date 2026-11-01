"""Helpers for WebUI-managed Hermes Agent git worktrees."""

from __future__ import annotations

import subprocess
import time
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Callable

import logging

from api.subprocess_utils import windows_hide_flags
from api.worktree_placement import (
    PLACEMENT_MAIN,
    PLACEMENT_MOVE_FAILED,
    PLACEMENT_UNRECORDED,
    WORKTREES_DIRNAME,
    primary_worktree,
    relocate_worktree,
)

logger = logging.getLogger(__name__)


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
    """Return the checkout *workspace* is in, according to git.

    This answers "which checkout", not "which repository": inside a linked
    worktree git correctly names the worktree itself. The repository's *main*
    root — the only directory whose ``.worktrees/`` a new session may use, and
    the one git records first in ``git worktree list`` — is
    :func:`api.worktree_placement.primary_worktree`'s job.
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


def _gitignore_bytes(root: Path) -> bytes | None:
    """Snapshot ``<root>/.gitignore`` exactly (``None`` when it does not exist)."""
    try:
        return (root / ".gitignore").read_bytes()
    except OSError:
        return None


def _restore_gitignore(root: Path, before: bytes | None) -> None:
    """Undo the Agent's own ``.worktrees/`` edit where the helper ran.

    The Agent appends the entry to the root it was given. When the WebUI hands
    it a session's checkout instead of the repository root, that edit would land
    in another session's tree, so the exact prior bytes are put back — and only
    when the call actually changed them.
    """
    path = root / ".gitignore"
    try:
        after = path.read_bytes() if path.exists() else None
    except OSError:
        return
    if after == before:
        return
    try:
        if before is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(before)
    except OSError:
        logger.debug("could not restore %s", path, exc_info=True)


def _ensure_worktrees_ignored(root: Path) -> None:
    """Make sure ``.worktrees/`` is ignored at *root* (idempotent, fail-soft)."""
    entry = f"{WORKTREES_DIRNAME}/"
    path = root / ".gitignore"
    try:
        # utf-8-sig: a BOM or a cp1251/GBK default would hide the entry from the
        # membership check and append a duplicate.
        existing = (
            path.read_text(encoding="utf-8-sig", errors="replace")
            if path.exists()
            else ""
        )
        if entry in existing.splitlines():
            return
        with open(path, "a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write(f"{entry}\n")
    except OSError:
        logger.debug("could not update %s", path, exc_info=True)


def _record_landing_root(root: Path) -> None:
    """Record *root* as a repo with Hermes-made worktrees (native registry, fail-soft).

    The Agent registers the checkout it ran in; a tree moved under the primary is
    only discoverable by the maintenance sweep if the *landing* root is registered
    too — the named sweep enumerates ``<registered root>/.worktrees/``. Uses the
    installed Agent's own registry contract so one profile's sweep sees the tree.
    """
    try:
        from hermes_cli.worktree_registry import record_worktree_repo
    except Exception:
        logger.debug("worktree registry unavailable; landing root not recorded", exc_info=True)
        return
    try:
        record_worktree_repo(str(root))
    except Exception:
        logger.debug("could not record landing root %s", root, exc_info=True)


def create_worktree_for_workspace(
    workspace: str | Path,
    *,
    setup: Callable[[str], dict] | None = None,
) -> dict:
    """Create a new session worktree for *workspace*, at the repository's main root.

    Placement is a git question, not a filesystem guess:

    * **which checkout** the workspace is in — ``find_git_repo_root`` asks git;
    * **which root owns the repository's worktrees** — ``primary_worktree``
      asks git for the recorded main worktree and accepts it only when git
      corroborates it. No claim means the placement is recorded as
      ``selected-unrecorded`` and the tree stays where the Agent created it.

    The Agent's own ``_setup_worktree`` runs against the **selected** checkout,
    so its branch/base policy, ``.worktreeinclude`` handling and lock are
    exactly what an installed Agent would do for a session started there. The
    tree it creates is then *moved* to ``<primary>/.worktrees/<name>``, which
    is the only step the WebUI adds — it never assembles, copies, restores or
    re-derives the tree's content.

    ``setup`` exists for tests to observe which directory the Agent helper is
    given; production always uses the installed Agent's helper.
    """
    selected = find_git_repo_root(workspace)
    placement = primary_worktree(selected)
    producer = setup if setup is not None else _setup_agent_worktree

    # The Agent appends ``.worktrees/`` to the root it runs against. When that
    # root is a session's checkout, the edit is undone below so a session never
    # dirties another session's tree.
    gitignore_before = _gitignore_bytes(selected)

    info = producer(str(selected))
    path = info.get("path")
    branch = info.get("branch")
    if not path or not branch:
        raise RuntimeError("Hermes Agent returned incomplete worktree metadata")

    created = Path(path).expanduser()
    if not created.is_absolute():
        created = selected / created
    landing = created
    primary = placement.primary
    placement_reason = placement.reason
    if primary is None:
        note = PLACEMENT_UNRECORDED
    elif primary == selected:
        note = PLACEMENT_MAIN
    else:
        _restore_gitignore(selected, gitignore_before)
        moved, error = relocate_worktree(created, primary)
        if moved is None:
            logger.warning(
                "worktree created at %s was not moved under %s: %s",
                created,
                primary,
                error,
            )
            note = PLACEMENT_MOVE_FAILED
            # The move error is what actually decided the placement; the
            # classifier's token would blame the wrong step.
            placement_reason = error or PLACEMENT_MOVE_FAILED
        else:
            landing = moved
            note = PLACEMENT_MAIN

    root = (
        landing.parent.parent
        if landing.parent.name == WORKTREES_DIRNAME
        else selected
    )
    _ensure_worktrees_ignored(root)
    _record_landing_root(root)
    return {
        "path": str(_resolve_path(landing) or landing),
        "branch": str(branch),
        "repo_root": str(_resolve_path(root) or root),
        "placement": note,
        "placement_reason": placement_reason,
        "created_at": time.time(),
    }

