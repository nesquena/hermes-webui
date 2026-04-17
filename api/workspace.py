"""
Hermes Web UI -- Workspace and file system helpers.

Workspace lists and last-used workspace are stored per-profile so each
profile has its own workspace configuration.  State files live at
``{profile_home}/webui_state/workspaces.json`` and
``{profile_home}/webui_state/last_workspace.txt``.  The global STATE_DIR
paths are used as fallback when no profile module is available.
"""
import json
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

from api.config import (
    WORKSPACES_FILE as _GLOBAL_WS_FILE,
    LAST_WORKSPACE_FILE as _GLOBAL_LW_FILE,
    DEFAULT_WORKSPACE as _BOOT_DEFAULT_WORKSPACE,
    MAX_FILE_BYTES, IMAGE_EXTS, MD_EXTS
)


# ── Profile-aware path resolution ───────────────────────────────────────────

def _profile_state_dir() -> Path:
    """Return the webui_state directory for the active profile.

    For the default profile, returns the global STATE_DIR (respects
    HERMES_WEBUI_STATE_DIR env var for test isolation).
    For named profiles, returns {profile_home}/webui_state/.
    """
    try:
        from api.profiles import get_active_profile_name, get_active_hermes_home
        name = get_active_profile_name()
        if name and name != 'default':
            d = get_active_hermes_home() / 'webui_state'
            d.mkdir(parents=True, exist_ok=True)
            return d
    except ImportError:
        logger.debug("Failed to import profiles module, using global state dir")
    return _GLOBAL_WS_FILE.parent


def _workspaces_file() -> Path:
    """Return the workspaces.json path for the active profile."""
    return _profile_state_dir() / 'workspaces.json'


def _last_workspace_file() -> Path:
    """Return the last_workspace.txt path for the active profile."""
    return _profile_state_dir() / 'last_workspace.txt'


def _profile_default_workspace() -> str:
    """Read the profile's default workspace from its config.yaml.

    Checks keys in priority order:
      1. 'workspace'         — explicit webui workspace key
      2. 'default_workspace' — alternate explicit key
      3. 'terminal.cwd'      — hermes-agent terminal working dir (most common)

    Falls back to the boot-time DEFAULT_WORKSPACE constant.
    """
    try:
        from api.config import get_config
        cfg = get_config()
        # Explicit webui workspace keys first
        for key in ('workspace', 'default_workspace'):
            ws = cfg.get(key)
            if ws:
                p = Path(str(ws)).expanduser().resolve()
                if p.is_dir():
                    return str(p)
        # Fall through to terminal.cwd — the agent's configured working directory
        terminal_cfg = cfg.get('terminal', {})
        if isinstance(terminal_cfg, dict):
            cwd = terminal_cfg.get('cwd', '')
            if cwd and str(cwd) not in ('.', ''):
                p = Path(str(cwd)).expanduser().resolve()
                if p.is_dir():
                    return str(p)
    except (ImportError, Exception):
        logger.debug("Failed to load profile default workspace config")
    return str(_BOOT_DEFAULT_WORKSPACE)


# ── Public API ──────────────────────────────────────────────────────────────

def _clean_workspace_list(workspaces: list) -> list:
    """Sanitize a workspace list:
    - Remove entries whose paths no longer exist on disk.
    - Remove entries whose paths live inside another profile's directory
      (e.g. ~/.hermes/profiles/X/... should not appear on a different profile).
    - Rename any entry whose name is literally 'default' to 'Home' (avoids
      confusion with the 'default' profile name).
    Returns the cleaned list (may be empty).
    """
    hermes_profiles = (Path.home() / '.hermes' / 'profiles').resolve()
    result = []
    for w in workspaces:
        path = w.get('path', '')
        name = w.get('name', '')
        p = Path(path).resolve() if path else Path('/')
        # Skip paths that no longer exist
        if not p.is_dir():
            continue
        # Skip paths inside a DIFFERENT profile's directory (cross-profile leak).
        # Allow paths inside the CURRENT profile's own directory (e.g. test workspaces
        # created under ~/.hermes/profiles/webui/webui-mvp-test/).
        try:
            p.relative_to(hermes_profiles)
            # p is under ~/.hermes/profiles/ — only skip if it's under a DIFFERENT profile
            try:
                from api.profiles import get_active_hermes_home
                own_profile_dir = get_active_hermes_home().resolve()
                p.relative_to(own_profile_dir)
                # p is under our own profile dir — keep it
            except (ValueError, Exception):
                continue  # under profiles/ but not our own — cross-profile leak, skip
        except ValueError:
            pass  # not under profiles/ at all — keep it
        # Rename confusing 'default' label to 'Home'
        if name.lower() == 'default':
            name = 'Home'
        result.append({'path': str(p), 'name': name})
    return result


def _migrate_global_workspaces() -> list:
    """Read the legacy global workspaces.json, clean it, and return the result.

    This is the migration path for users upgrading from a pre-profile version:
    their global file may contain cross-profile entries, test artifacts, and
    stale paths accumulated over time.  We clean it in-place and rewrite it.
    """
    if not _GLOBAL_WS_FILE.exists():
        return []
    try:
        raw = json.loads(_GLOBAL_WS_FILE.read_text(encoding='utf-8'))
        cleaned = _clean_workspace_list(raw)
        if len(cleaned) != len(raw):
            # Rewrite the cleaned version so future reads are already clean
            _GLOBAL_WS_FILE.write_text(
                json.dumps(cleaned, ensure_ascii=False, indent=2), encoding='utf-8'
            )
        return cleaned
    except Exception:
        return []


def load_workspaces() -> list:
    ws_file = _workspaces_file()
    if ws_file.exists():
        try:
            raw = json.loads(ws_file.read_text(encoding='utf-8'))
            cleaned = _clean_workspace_list(raw)
            if len(cleaned) != len(raw):
                # Persist the cleaned version so stale entries don't keep reappearing
                try:
                    ws_file.write_text(
                        json.dumps(cleaned, ensure_ascii=False, indent=2), encoding='utf-8'
                    )
                except Exception:
                    logger.debug("Failed to persist cleaned workspace list")
            return cleaned or [{'path': _profile_default_workspace(), 'name': 'Home'}]
        except Exception:
            logger.debug("Failed to load workspaces from %s", ws_file)
    # No profile-local file yet.
    # For the DEFAULT profile: migrate from the legacy global file (one-time cleanup).
    # For NAMED profiles: always start clean with just their own workspace.
    try:
        from api.profiles import get_active_profile_name
        is_default = get_active_profile_name() in ('default', None)
    except ImportError:
        is_default = True
    if is_default:
        migrated = _migrate_global_workspaces()
        if migrated:
            return migrated
    # Fresh start: single entry from the profile's configured workspace, labeled "Home"
    return [{'path': _profile_default_workspace(), 'name': 'Home'}]


def save_workspaces(workspaces: list) -> None:
    ws_file = _workspaces_file()
    ws_file.parent.mkdir(parents=True, exist_ok=True)
    ws_file.write_text(json.dumps(workspaces, ensure_ascii=False, indent=2), encoding='utf-8')


def get_last_workspace() -> str:
    lw_file = _last_workspace_file()
    if lw_file.exists():
        try:
            p = lw_file.read_text(encoding='utf-8').strip()
            if p and Path(p).is_dir():
                return p
        except Exception:
            logger.debug("Failed to read last workspace from %s", lw_file)
    # Fallback: try global file
    if _GLOBAL_LW_FILE.exists():
        try:
            p = _GLOBAL_LW_FILE.read_text(encoding='utf-8').strip()
            if p and Path(p).is_dir():
                return p
        except Exception:
            logger.debug("Failed to read global last workspace")
    return _profile_default_workspace()


def set_last_workspace(path: str) -> None:
    try:
        lw_file = _last_workspace_file()
        lw_file.parent.mkdir(parents=True, exist_ok=True)
        lw_file.write_text(str(path), encoding='utf-8')
    except Exception:
        logger.debug("Failed to set last workspace")


def resolve_trusted_workspace(path: str | Path | None = None) -> Path:
    """Resolve and validate a workspace path.

    A path is trusted if it satisfies at least one of:
      (A) It is under the user's home directory (Path.home()).
          Works cross-platform: ~/... on Linux/macOS, C:\\Users\\... on Windows.
      (B) It is already in the profile's saved workspace list.
          This covers self-hosted deployments where workspaces live outside home
          (e.g. /data/projects, /opt/workspace) — once a workspace is saved by
          an admin, it can be reused without re-validation.

    Additionally enforced regardless of (A)/(B):
      1. The path must exist.
      2. The path must be a directory.
      3. The path must not be a known system root (/etc, /usr, /var, /bin, /sbin,
         /boot, /proc, /sys, /dev, /root on Linux/macOS; Windows system dirs).
         This prevents even admin-saved workspaces from pointing at OS internals.

    None/empty path falls back to the boot-time DEFAULT_WORKSPACE, which is always
    trusted (it was validated at server startup).
    """
    _BLOCKED_SYSTEM_ROOTS = {
        # Linux / macOS
        Path('/etc'), Path('/usr'), Path('/var'), Path('/bin'), Path('/sbin'),
        Path('/boot'), Path('/proc'), Path('/sys'), Path('/dev'),
        Path('/lib'), Path('/lib64'), Path('/opt/homebrew'),
    }

    if path in (None, ""):
        return Path(_BOOT_DEFAULT_WORKSPACE).expanduser().resolve()

    candidate = Path(path).expanduser().resolve()

    if not candidate.exists():
        raise ValueError(f"Path does not exist: {candidate}")
    if not candidate.is_dir():
        raise ValueError(f"Path is not a directory: {candidate}")

    # Block known system roots and their children
    for blocked in _BLOCKED_SYSTEM_ROOTS:
        try:
            candidate.relative_to(blocked)
            raise ValueError(f"Path points to a system directory: {candidate}")
        except ValueError as e:
            if "system directory" in str(e):
                raise
            # relative_to raised ValueError = candidate is NOT under blocked = safe

    # (A) Trusted if under the user's home directory — cross-platform via Path.home()
    try:
        candidate.relative_to(Path.home().resolve())
        return candidate
    except ValueError:
        pass

    # (B) Trusted if already in the saved workspace list — covers non-home installs
    try:
        saved = load_workspaces()
        saved_paths = {Path(w["path"]).resolve() for w in saved if w.get("path")}
        if candidate in saved_paths:
            return candidate
    except Exception:
        pass

    # (C) Trusted if it is equal to or under the boot-time DEFAULT_WORKSPACE.
    #     In Docker deployments HERMES_WEBUI_DEFAULT_WORKSPACE is often set to a
    #     volume mount outside the user's home (e.g. /data/workspace).  That path
    #     was already validated at server startup, so any sub-path of it is safe
    #     without requiring the user to add it to the workspace list manually.
    try:
        boot_default = Path(_BOOT_DEFAULT_WORKSPACE).expanduser().resolve()
        candidate.relative_to(boot_default)
        return candidate
    except ValueError:
        pass

    raise ValueError(
        f"Path is outside the user home directory, not in the saved workspace "
        f"list, and not under the default workspace: {candidate}. "
        f"Add it via Settings → Workspaces first."
    )


def safe_resolve_ws(root: Path, requested: str) -> Path:
    """Resolve a relative path inside a workspace root, raising ValueError on traversal."""
    resolved = (root / requested).resolve()
    resolved.relative_to(root.resolve())
    return resolved


def list_dir(workspace: Path, rel: str='.'):
    target = safe_resolve_ws(workspace, rel)
    if not target.is_dir():
        raise FileNotFoundError(f"Not a directory: {rel}")
    entries = []
    for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        entries.append({
            'name': item.name,
            'path': str(item.relative_to(workspace)),
            'type': 'dir' if item.is_dir() else 'file',
            'size': item.stat().st_size if item.is_file() else None,
        })
        if len(entries) >= 200:
            break
    return entries


def read_file_content(workspace: Path, rel: str) -> dict:
    target = safe_resolve_ws(workspace, rel)
    if not target.is_file():
        raise FileNotFoundError(f"Not a file: {rel}")
    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError(f"File too large ({size} bytes, max {MAX_FILE_BYTES})")
    content = target.read_text(encoding='utf-8', errors='replace')
    return {'path': rel, 'content': content, 'size': size, 'lines': content.count('\n') + 1}


# ── Git detection ──────────────────────────────────────────────────────────

def _run_git(args, cwd, timeout=3):
    """Run a git command and return stdout, or None on failure."""
    try:
        r = subprocess.run(
            ['git'] + args, cwd=str(cwd), capture_output=True,
            text=True, timeout=timeout,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def git_info_for_workspace(workspace: Path) -> dict:
    """Return git info for a workspace directory, or None if not a git repo."""
    if not (workspace / '.git').exists():
        return None
    branch = _run_git(['rev-parse', '--abbrev-ref', 'HEAD'], workspace)
    if branch is None:
        return None
    # Status counts
    status_out = _run_git(['status', '--porcelain'], workspace) or ''
    lines = [l for l in status_out.splitlines() if l]
    # git status --porcelain: XY format where X=index, Y=worktree
    modified = sum(1 for l in lines if len(l) >= 2 and (l[0] in 'MAR' or l[1] in 'MAR'))
    untracked = sum(1 for l in lines if l.startswith('??'))
    dirty = len(lines)
    # Ahead/behind
    ahead = _run_git(['rev-list', '--count', '@{u}..HEAD'], workspace)
    behind = _run_git(['rev-list', '--count', 'HEAD..@{u}'], workspace)
    return {
        'branch': branch,
        'dirty': dirty,
        'modified': modified,
        'untracked': untracked,
        'ahead': int(ahead) if ahead and ahead.isdigit() else 0,
        'behind': int(behind) if behind and behind.isdigit() else 0,
        'is_git': True,
    }
