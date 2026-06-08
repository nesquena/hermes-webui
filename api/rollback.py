"""
Hermes Web UI -- Filesystem checkpoint (rollback) API.

Provides endpoints to list, diff, and restore filesystem checkpoints
created by the Hermes agent's CheckpointManager.  Checkpoints live at
``{hermes_home}/checkpoints/<hash>/`` as shadow git repositories.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Checkpoint identifiers are SHA-style hex hashes from the agent's
# CheckpointManager. We only allow [A-Za-z0-9_.-]{1,64} (no '/' so the
# value cannot be a path separator, no leading '.' so it cannot escape
# upward via '..'/'.'). This is defense-in-depth: the workspace arg is
# already allowlisted, but ``Path() / "../escape"`` does not normalize,
# so without this guard a `checkpoint` value of `../<other-ws-hash>/<sha>`
# would let any authenticated caller diff or restore from another
# allowlisted workspace's checkpoint store. (Opus pre-release advisor.)
_CHECKPOINT_ID_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}$")


def _validate_checkpoint_id(checkpoint: str) -> str:
    cid = str(checkpoint or "").strip()
    if not cid or cid in (".", "..") or not _CHECKPOINT_ID_RE.fullmatch(cid):
        raise ValueError(
            "checkpoint id must match [A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}"
        )
    return cid


def _hermes_home() -> Path:
    """Return the active Hermes home directory."""
    try:
        from api.profiles import get_active_hermes_home
        return Path(get_active_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def _workspace_hash(workspace: str) -> str:
    """Derive the checkpoint directory name from a workspace path.

    Matches the agent's CheckpointManager._get_checkpoint_dir logic:
    SHA-256 of the canonical workspace path.
    """
    try:
        canonical = os.path.realpath(workspace)
    except (OSError, ValueError):
        canonical = workspace
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def _checkpoint_root() -> Path:
    return _hermes_home() / "checkpoints"


def _resolve_workspace(workspace: str) -> str:
    """Validate and return the canonical workspace path.

    Security: workspace must match a known configured workspace
    (from workspaces.json or session-attached workspaces).
    """
    if not workspace or not isinstance(workspace, str):
        raise ValueError("workspace is required")
    # Basic path validation
    resolved = os.path.realpath(workspace)
    if not os.path.isdir(resolved):
        raise ValueError(f"Workspace does not exist: {workspace}")
    # Security: confirm workspace is in the known list
    try:
        from api.workspace import load_workspaces
        known_paths = set()
        for ws in load_workspaces():
            p = ws.get("path", "")
            if p:
                known_paths.add(os.path.realpath(p))
        if resolved not in known_paths:
            raise ValueError(f"Workspace not in configured list: {workspace}")
    except ImportError:
        logger.warning("Could not load workspace list for rollback validation")
    return resolved


def _find_git() -> str:
    """Return the path to the git binary."""
    return shutil.which("git") or "git"


# ── Public API functions (called from routes.py) ────────────────────────────


def list_checkpoints(workspace: str) -> dict[str, Any]:
    """List all checkpoints for a workspace.

    Returns a dict with:
        checkpoints: list of checkpoint objects
        workspace: resolved workspace path
        checkpoint_dir: the checkpoint directory path
    """
    resolved = _resolve_workspace(workspace)
    ws_hash = _workspace_hash(resolved)
    ckpt_dir = _checkpoint_root() / ws_hash

    checkpoints = []
    if not ckpt_dir.is_dir():
        return {"checkpoints": [], "workspace": resolved, "checkpoint_dir": str(ckpt_dir)}

    # Each checkpoint is a git repo in <ckpt_dir>/<commit_hash>/
    git = _find_git()
    for entry in sorted(ckpt_dir.iterdir(), key=lambda p: p.stat().st_mtime if p.is_dir() else 0, reverse=True):
        if not entry.is_dir():
            continue
        ckpt_info = _inspect_checkpoint(entry, git)
        if ckpt_info:
            checkpoints.append(ckpt_info)

    return {
        "checkpoints": checkpoints,
        "workspace": resolved,
        "checkpoint_dir": str(ckpt_dir),
    }


def _inspect_checkpoint(ckpt_path: Path, git: str) -> dict[str, Any] | None:
    """Extract metadata from a single checkpoint directory."""
    git_dir = ckpt_path / ".git"
    if not git_dir.is_dir():
        return None

    name = ckpt_path.name
    try:
        result = subprocess.run(
            [git, "-C", str(ckpt_path), "log", "--format=%H%n%s%n%aI", "-1"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None

        lines = result.stdout.strip().split("\n")
        commit_hash = lines[0] if len(lines) > 0 else name
        message = lines[1] if len(lines) > 1 else "checkpoint"
        date_str = lines[2] if len(lines) > 2 else ""

        # Parse date for display
        date_display = ""
        if date_str:
            try:
                dt = datetime.fromisoformat(date_str)
                date_display = dt.strftime("%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                date_display = date_str

        # Count files
        files_result = subprocess.run(
            [git, "-C", str(ckpt_path), "ls-files"],
            capture_output=True, text=True, timeout=5,
        )
        file_count = len(files_result.stdout.strip().split("\n")) if files_result.stdout.strip() else 0

        return {
            "id": name,
            "commit": commit_hash[:12],
            "message": message,
            "date": date_str,
            "date_display": date_display,
            "files": file_count,
            "path": str(ckpt_path),
        }
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.debug("Failed to inspect checkpoint %s: %s", ckpt_path, e)
        return None


def get_checkpoint_diff(workspace: str, checkpoint: str) -> dict[str, Any]:
    """Show the diff between a checkpoint and the current workspace state.

    Returns a dict with:
        diff: unified diff text
        files_changed: list of changed file paths
    """
    resolved = _resolve_workspace(workspace)
    checkpoint = _validate_checkpoint_id(checkpoint)
    ws_hash = _workspace_hash(resolved)
    ckpt_dir = _checkpoint_root() / ws_hash / checkpoint

    if not ckpt_dir.is_dir():
        raise ValueError(f"Checkpoint not found: {checkpoint}")

    git = _find_git()

    # Get list of files in the checkpoint
    ls_result = subprocess.run(
        [git, "-C", str(ckpt_dir), "ls-files"],
        capture_output=True, text=True, timeout=10,
    )
    if ls_result.returncode != 0:
        raise ValueError("Failed to list checkpoint files")

    ckpt_files = [f for f in ls_result.stdout.strip().split("\n") if f]
    files_changed = []
    diff_lines = []

    for rel_path in ckpt_files:
        ckpt_file = ckpt_dir / rel_path
        ws_file = Path(resolved) / rel_path

        if not ckpt_file.is_file():
            continue

        # Read checkpoint version
        try:
            ckpt_content = ckpt_file.read_text(errors="replace")
        except OSError:
            continue

        # Read workspace version (if exists)
        if ws_file.is_file():
            try:
                ws_content = ws_file.read_text(errors="replace")
            except OSError:
                ws_content = ""
        else:
            ws_content = None  # File was deleted in workspace

        if ws_content is None:
            # File exists in checkpoint but not in workspace (deleted)
            files_changed.append({"file": rel_path, "status": "deleted"})
            diff_lines.append(f"--- a/{rel_path}")
            diff_lines.append(f"+++ /dev/null")
            diff_lines.append("@@ -1,{lines} +0,0 @@".format(lines=len(ckpt_content.splitlines())))
            for line in ckpt_content.splitlines():
                diff_lines.append(f"-{line}")
        elif ckpt_content != ws_content:
            # File changed
            import difflib
            ckpt_lines = ckpt_content.splitlines(keepends=True)
            ws_lines = ws_content.splitlines(keepends=True)
            diff = list(difflib.unified_diff(ckpt_lines, ws_lines, fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}", lineterm=""))
            if diff:
                files_changed.append({"file": rel_path, "status": "modified"})
                diff_lines.extend(diff)

    # Check for new files in workspace that aren't in checkpoint
    # (skip for performance — diff is primarily for seeing what the checkpoint captures)

    return {
        "checkpoint": checkpoint,
        "workspace": resolved,
        "diff": "\n".join(diff_lines) if diff_lines else "",
        "files_changed": files_changed,
        "total_changes": len(files_changed),
    }


def restore_checkpoint(workspace: str, checkpoint: str) -> dict[str, Any]:
    """Restore a checkpoint by copying files back to the workspace.

    Only restores files that exist in the checkpoint.  Does NOT delete
    files that were added after the checkpoint was created.

    Returns a dict with:
        ok: True
        files_restored: list of restored file paths
    """
    resolved = _resolve_workspace(workspace)
    checkpoint = _validate_checkpoint_id(checkpoint)
    ws_hash = _workspace_hash(resolved)
    ckpt_dir = _checkpoint_root() / ws_hash / checkpoint

    if not ckpt_dir.is_dir():
        raise ValueError(f"Checkpoint not found: {checkpoint}")

    git = _find_git()

    # Get list of files in the checkpoint
    ls_result = subprocess.run(
        [git, "-C", str(ckpt_dir), "ls-files"],
        capture_output=True, text=True, timeout=10,
    )
    if ls_result.returncode != 0:
        raise ValueError("Failed to list checkpoint files")

    ckpt_files = [f for f in ls_result.stdout.strip().split("\n") if f]
    restored = []
    errors = []

    for rel_path in ckpt_files:
        ckpt_file = ckpt_dir / rel_path
        ws_file = Path(resolved) / rel_path

        if not ckpt_file.is_file():
            continue

        try:
            ws_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(ckpt_file), str(ws_file))
            restored.append(rel_path)
        except OSError as e:
            errors.append({"file": rel_path, "error": str(e)})
            logger.warning("Failed to restore %s: %s", rel_path, e)

    return {
        "ok": True,
        "checkpoint": checkpoint,
        "workspace": resolved,
        "files_restored": restored,
        "files_restored_count": len(restored),
        "errors": errors,
    }
