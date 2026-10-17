"""Helpers for WebUI-managed Hermes Agent git worktrees."""

from __future__ import annotations

import subprocess
import time
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import logging

from api.subprocess_utils import windows_hide_flags

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
    """Return the *main* working-tree root, resolving linked worktrees.

    ``find_git_repo_root`` returns ``git rev-parse --show-toplevel``, which for
    a session started inside a linked worktree (e.g. ``<repo>/.worktrees/x``)
    is that worktree itself.  Passing it to the agent's ``_setup_worktree``
    nests the new tree *inside* the current one, and no pruner ever reclaims a
    nest.

    The main tree is resolved from the **common git dir**, but that dir is not
    always ``<main>/.git``: ``git init --separate-git-dir=X`` puts the metadata
    in ``X``, and a submodule's common dir is
    ``super/.git/modules/<name>``.  Treating either as a checkout created the
    session trees *inside the metadata* and reported that path as ``repo_root``
    (the reviewer's P2).  So: take the common dir, ask git for the working tree
    that owns it, and only fall back to the parent-of-``.git`` shape when the
    checkout cannot be recovered.  Returns ``None`` when the workspace is not in
    a git repo, letting the caller use ``find_git_repo_root``.
    """
    ws = Path(workspace).expanduser().resolve()
    if not ws.is_dir():
        return None

    common_path = _common_git_dir(ws)
    if common_path is None:
        return None

    # 1. A common dir shaped ``<main>/.git`` names its checkout directly.
    if common_path.name == ".git":
        root = common_path.parent
        if root.is_dir():
            return _git_toplevel(root) or root.resolve()

    # 2. separate-git-dir / submodule: the metadata dir is NOT a checkout, and
    #    ``--show-toplevel`` run *in* the metadata answers with the cwd's repo
    #    (a separate-git-dir checkout sets no core.worktree), so it cannot be
    #    trusted. The reliable source is the gitfile that points at this
    #    metadata: ``<checkout>/.git`` containing ``gitdir: <common dir>``.
    found = _checkout_owning_gitdir(common_path)
    if found is not None:
        return found

    # 3. Resolve a separate-git-dir checkout through ``core.worktree`` when set
    #    (git uses it for the same purpose in other layouts).
    try:
        result = subprocess.run(
            ["git", "--git-dir", str(common_path), "config", "--get", "core.worktree"],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
            creationflags=windows_hide_flags(),
        )
        raw = result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        raw = ""
    if raw:
        wt = Path(raw).expanduser()
        if not wt.is_absolute():
            wt = common_path / wt
        wt = wt.resolve()
        if wt.is_dir() and _git_toplevel(wt) is not None:
            return wt

    # 4. Conservative fallback: unnest to the parent of the metadata dir only
    #    when that parent is itself a real checkout *that owns this common dir*.
    #    Being a checkout is not enough: a repository can keep its separate Git
    #    directory inside an unrelated repo's tree while the checkout lives
    #    elsewhere, and returning that parent would start the session in the
    #    wrong project. Otherwise None -> the caller keeps the workspace's own
    #    toplevel (no worse than today).
    parent = common_path.parent
    if parent.is_dir():
        toplevel = _git_toplevel(parent)
        if toplevel is not None and toplevel == parent.resolve():
            owned = _common_git_dir(parent)
            if owned is not None:
                try:
                    if owned.resolve() == common_path.resolve():
                        return toplevel
                except OSError:
                    pass
    return None


def _checkout_owning_gitdir(common_path: Path) -> Path | None:
    """Find a checkout whose ``.git`` gitfile points at *common_path*.

    ``git init --separate-git-dir=X`` leaves ``<checkout>/.git`` holding a single
    line ``gitdir: X``. Searching the metadata dir's siblings (the usual layout:
    ``<parent>/checkout`` + ``<parent>/metadata``) finds the real working tree
    without guessing, and the resolved path is validated as a checkout. Submodule
    metadata (``<super>/.git/modules/<name>``) is matched the same way when the
    submodule is checked out under ``<super>/<name>``.
    """
    target = common_path.resolve()
    parent = common_path.parent
    candidates = []

    # Siblings of the metadata dir (separate-git-dir), then the superproject's
    # own working tree (submodule: <super>/.git/modules/<name> -> <super>/<name>).
    if parent.is_dir():
        try:
            candidates.extend(sorted(p for p in parent.iterdir() if p.is_dir()))
        except OSError:
            pass
    if parent.name == "modules":
        super_git = parent.parent
        if super_git.name == ".git" and super_git.parent.is_dir():
            candidates.extend(sorted(p for p in super_git.parent.iterdir() if p.is_dir()))

    for candidate in candidates:
        gitfile = candidate / ".git"
        if not gitfile.is_file():
            continue
        try:
            text = gitfile.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not text.startswith("gitdir:"):
            continue
        raw = text.split(":", 1)[1].strip()
        pointed = Path(raw)
        if not pointed.is_absolute():
            pointed = (candidate / pointed)
        try:
            if pointed.resolve() != target:
                continue
        except OSError:
            continue
        toplevel = _git_toplevel(candidate)
        if toplevel is not None:
            return toplevel
        return candidate.resolve()
    return None



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
