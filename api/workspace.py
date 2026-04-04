"""
Hermes Web UI -- Workspace and file system helpers.

Workspace lists and last-used workspace are stored per-profile so each
profile has its own workspace configuration.  State files live at
``{profile_home}/webui_state/workspaces.json`` and
``{profile_home}/webui_state/last_workspace.txt``.  The global STATE_DIR
paths are used as fallback when no profile module is available.
"""
import json
import os
import subprocess
from pathlib import Path

from api.config import (
    WORKSPACES_FILE as _GLOBAL_WS_FILE,
    LAST_WORKSPACE_FILE as _GLOBAL_LW_FILE,
    DEFAULT_WORKSPACE as _BOOT_DEFAULT_WORKSPACE,
    WORKSPACE_LABELS_FILE,
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
        pass
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
        pass
    return str(_BOOT_DEFAULT_WORKSPACE)


# ── Public API ──────────────────────────────────────────────────────────────

def _clean_workspace_list(workspaces: list) -> list:
    """Sanitize a workspace list:
    - Remove entries whose paths no longer exist on disk.
    - Remove entries that look like test artifacts (webui-mvp-test, test-workspace).
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
        # Skip test artifacts
        if 'test-workspace' in path or 'webui-mvp-test' in path:
            continue
        # Skip paths that no longer exist
        if not p.is_dir():
            continue
        # Skip paths inside a named profile's directory (cross-profile leak)
        try:
            p.relative_to(hermes_profiles)
            continue  # it IS under profiles/ — remove it
        except ValueError:
            pass
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
                    pass
            return cleaned or [{'path': _profile_default_workspace(), 'name': 'Home'}]
        except Exception:
            pass
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


def save_workspaces(workspaces: list):
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
            pass
    # Fallback: try global file
    if _GLOBAL_LW_FILE.exists():
        try:
            p = _GLOBAL_LW_FILE.read_text(encoding='utf-8').strip()
            if p and Path(p).is_dir():
                return p
        except Exception:
            pass
    return _profile_default_workspace()


def set_last_workspace(path: str):
    try:
        lw_file = _last_workspace_file()
        lw_file.parent.mkdir(parents=True, exist_ok=True)
        lw_file.write_text(str(path), encoding='utf-8')
    except Exception:
        pass


def safe_resolve_ws(root: Path, requested: str) -> Path:
    """Resolve a relative path inside a workspace root, raising ValueError on traversal."""
    resolved = (root / requested).resolve()
    resolved.relative_to(root.resolve())
    return resolved


def _is_git_repo(path: Path) -> bool:
    """Return True if path is the root of a git repository."""
    return (path / '.git').exists()


def git_info(repo_path: Path) -> dict:
    """Return git status, branch, log and remote info for a repo directory."""
    def run(*args, cwd=None):
        try:
            r = subprocess.run(
                args, cwd=str(cwd or repo_path),
                capture_output=True, text=True, timeout=5
            )
            return r.stdout.strip() if r.returncode == 0 else None
        except Exception:
            return None

    branch      = run('git', 'rev-parse', '--abbrev-ref', 'HEAD')
    status_raw  = run('git', 'status', '--short')
    ahead_behind= run('git', 'rev-list', '--left-right', '--count', '@{u}...HEAD')
    # Structured log: hash|subject|author|relative-date|iso-date
    log_structured = run('git', 'log', '--format=%h\x1f%s\x1f%an\x1f%ar\x1f%aI', '-10')
    log_raw     = run('git', 'log', '--oneline', '--decorate', '-10')
    remotes_raw = run('git', 'remote', '-v')
    stash_raw   = run('git', 'stash', 'list')
    last_commit = run('git', 'log', '-1', '--format=%h %s (%ar)')
    last_commit_epoch = run('git', 'log', '-1', '--format=%at')
    # Remote origin URL for linking
    remote_url  = run('git', 'remote', 'get-url', 'origin')

    # Parse ahead/behind
    ahead = behind = 0
    if ahead_behind:
        parts = ahead_behind.split()
        if len(parts) == 2:
            try:
                behind, ahead = int(parts[0]), int(parts[1])
            except ValueError:
                pass

    # Summarise status counts
    status_lines = [l for l in (status_raw or '').splitlines() if l.strip()]
    modified  = sum(1 for l in status_lines if l[1] == 'M' or l[0] == 'M')
    added     = sum(1 for l in status_lines if l[0] == 'A')
    untracked = sum(1 for l in status_lines if l[:2] == '??')
    deleted   = sum(1 for l in status_lines if l[0] == 'D' or l[1] == 'D')

    # Parse structured commits into dicts
    commits = []
    for line in (log_structured or '').splitlines():
        parts = line.split('\x1f')
        if len(parts) == 5:
            commits.append({
                'hash':    parts[0],
                'subject': parts[1],
                'author':  parts[2],
                'rel':     parts[3],
                'iso':     parts[4],
            })

    # Normalise remote URL to a browseable HTTPS URL
    def _normalise_remote(url):
        if not url:
            return None
        url = url.strip()
        # git@github.com:owner/repo.git -> https://github.com/owner/repo
        if url.startswith('git@'):
            url = url.replace(':', '/', 1).replace('git@', 'https://', 1)
        if url.endswith('.git'):
            url = url[:-4]
        return url

    remote_browse = _normalise_remote(remote_url)

    return {
        'branch':       branch,
        'last_commit':  last_commit,
        'ahead':        ahead,
        'behind':       behind,
        'modified':     modified,
        'added':        added,
        'untracked':    untracked,
        'deleted':      deleted,
        'status_lines': status_lines[:30],
        'log':          [l for l in (log_raw or '').splitlines() if l][:10],
        'commits':      commits,
        'remotes':      [l for l in (remotes_raw or '').splitlines() if l],
        'stashes':      [l for l in (stash_raw or '').splitlines() if l],
        'remote_url':      remote_browse,
        'last_commit_ts':  int(last_commit_epoch) if last_commit_epoch and last_commit_epoch.strip().isdigit() else None,
    }


def list_dir(workspace: Path, rel='.'):
    target = safe_resolve_ws(workspace, rel)
    if not target.is_dir():
        raise FileNotFoundError(f"Not a directory: {rel}")
    entries = []
    for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        # Skip symlinks and hidden git internals
        if item.is_symlink():
            continue
        if item.name == '.git':
            continue
        is_git = item.is_dir() and _is_git_repo(item)
        last_commit_ts = None
        git_behind = 0
        if is_git:
            try:
                r = subprocess.run(
                    ['git', 'log', '-1', '--format=%at'],
                    cwd=str(item), capture_output=True, text=True, timeout=3
                )
                ts = r.stdout.strip()
                if ts.isdigit():
                    last_commit_ts = int(ts)
            except Exception:
                pass
            try:
                # Count commits in remote but not local (incoming changes)
                rb = subprocess.run(
                    ['git', 'rev-list', '--count', 'HEAD..@{u}'],
                    cwd=str(item), capture_output=True, text=True, timeout=3
                )
                if rb.returncode == 0 and rb.stdout.strip().isdigit():
                    git_behind = int(rb.stdout.strip())
            except Exception:
                pass
        entries.append({
            'name':   item.name,
            'path':   str(item.relative_to(workspace)),
            'type':   'dir' if item.is_dir() else 'file',
            'size':   item.stat().st_size if item.is_file() else None,
            'is_git': is_git,
            'last_commit_ts': last_commit_ts,
            'git_behind': git_behind,
        })
        if len(entries) >= 200:
            break
    return entries


def read_file_content(workspace: Path, rel: str):
    target = safe_resolve_ws(workspace, rel)
    if not target.is_file():
        raise FileNotFoundError(f"Not a file: {rel}")
    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError(f"File too large ({size} bytes, max {MAX_FILE_BYTES})")
    content = target.read_text(encoding='utf-8', errors='replace')
    return {'path': rel, 'content': content, 'size': size, 'lines': content.count('\n') + 1}


# ── Workspace labels / tags persistence ──────────────────────────────────────

def _load_all_labels() -> dict:
    """Load the full workspace-labels store."""
    if WORKSPACE_LABELS_FILE.exists():
        try:
            return json.loads(WORKSPACE_LABELS_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {}


def _save_all_labels(data: dict):
    WORKSPACE_LABELS_FILE.parent.mkdir(parents=True, exist_ok=True)
    WORKSPACE_LABELS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def load_workspace_labels(workspace: str) -> dict:
    """Return {path: label} dict for the given workspace."""
    return _load_all_labels().get(workspace, {})


def save_workspace_label(workspace: str, path: str, label: str):
    """Set or clear a label for a path within a workspace."""
    data = _load_all_labels()
    ws_labels = data.setdefault(workspace, {})
    if label:
        ws_labels[path] = label
    else:
        ws_labels.pop(path, None)
    _save_all_labels(data)
