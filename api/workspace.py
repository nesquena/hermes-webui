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
import stat
import subprocess
import concurrent.futures
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
    - Preserve saved paths even when they are currently missing or inaccessible;
      picker state must not be destroyed by a transient stat/permission failure.
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
        if not path:
            continue
        p = _safe_resolve(Path(path).expanduser())
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


def _workspace_access_error(candidate: Path, *, missing_label: str = "Path does not exist") -> str | None:
    """Return a user-facing validation error for an unusable workspace path.

    ``Path.exists()`` can collapse permission/stat failures into a generic falsey
    result on some Python/OS combinations, which produced misleading "does not
    exist" messages for macOS/TCC-denied directories.  Probe with ``stat()`` so
    missing paths, non-directories, and permission-denied paths can be reported
    separately.
    """
    try:
        st = candidate.stat()
    except FileNotFoundError:
        return f"{missing_label}: {candidate}"
    except PermissionError as exc:
        return (
            f"Cannot access path: {candidate}. The server process could not inspect "
            f"this directory ({exc}). On macOS, grant Full Disk Access or Files and "
            f"Folders permission to the Hermes/WebUI app or server process, then try again."
        )
    except OSError as exc:
        return f"Cannot access path: {candidate}. The server process could not inspect this path ({exc})."
    if not stat.S_ISDIR(st.st_mode):
        return f"Path is not a directory: {candidate}"
    return None


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


def _safe_resolve(p: Path) -> Path:
    """Path.resolve() that never raises — falls back to the input path on error."""
    try:
        return p.resolve()
    except (OSError, RuntimeError):
        return p


# Per-user temp directories that sit nominally under a "system" prefix but are
# actually user-writable scratch space.  Workspaces registered here (e.g. by
# pytest's ``tmp_path_factory`` on macOS, which uses ``/var/folders/<hash>/T/``)
# must remain accepted even though their parent (``/var``) is blocked.  These
# carve-outs apply to BOTH workspace registration and runtime file ops so a
# symlink target inside the carve-out is also reachable.
_USER_TMP_PREFIXES: tuple[Path, ...] = (
    Path('/var/folders'),         # macOS per-user tmp (literal form)
    Path('/private/var/folders'),  # macOS per-user tmp (resolved form)
    Path('/var/tmp'),               # Linux/macOS system-wide tmp (user-writable)
    Path('/private/var/tmp'),       # macOS resolved form
)


def _workspace_blocked_roots() -> tuple[Path, ...]:
    """System roots that must never be accepted as workspace candidates.

    Returns both the literal path and its symlink-resolved canonical form,
    deduped.  This matters on macOS where ``/etc``, ``/var``, and ``/tmp``
    are symlinks to ``/private/etc`` etc.  Without the resolved forms,
    callers that pass a ``.resolve()``-d candidate (every caller does)
    would compare ``/private/etc`` against literal ``Path('/etc')`` and the
    ``relative_to`` check would miss — letting ``/etc`` through as a
    registered workspace on macOS.

    Carve-outs for legitimate user-tmp paths nominally under these roots
    (e.g. ``/var/folders/.../T/`` on macOS) are handled by
    :func:`_is_blocked_system_path`, not by exclusion from this list.
    """
    _raw = (
        # Linux / macOS
        '/etc',
        '/usr',
        '/var',
        '/bin',
        '/sbin',
        '/boot',
        '/proc',
        '/sys',
        '/dev',
        '/lib',
        '/lib64',
        '/opt/homebrew',
        '/System',
        '/Library',
    )
    _seen: set[Path] = set()
    _out: list[Path] = []
    for _p in _raw:
        for _form in (Path(_p), _safe_resolve(Path(_p))):
            if _form not in _seen:
                _seen.add(_form)
                _out.append(_form)
    return tuple(_out)


def _is_blocked_system_path(candidate: Path) -> bool:
    """Return True if *candidate* falls under a blocked system root.

    Honours :data:`_USER_TMP_PREFIXES` carve-outs so per-user tmp directories
    nominally under ``/var`` (``/var/folders`` on macOS, ``/var/tmp`` on
    Linux/macOS) remain valid workspace candidates and reachable file targets.
    """
    for tmp in _USER_TMP_PREFIXES:
        if _is_within(candidate, tmp):
            return False
    for blocked in _workspace_blocked_roots():
        if _is_within(candidate, blocked):
            return True
    return False


def _workspace_blocked_resolved_subtrees() -> tuple[Path, ...]:
    roots = list(_workspace_blocked_roots()) + [Path('/private/etc')]
    resolved: list[Path] = []
    for root in roots:
        try:
            p = root.expanduser().resolve()
        except Exception:
            p = root
        if p not in resolved:
            resolved.append(p)
    return tuple(resolved)


def _workspace_blocked_exact_roots() -> tuple[Path, ...]:
    roots = [Path('/'), Path('/private/var')]
    for root in _workspace_blocked_roots():
        try:
            roots.append(root.expanduser().resolve())
        except Exception:
            roots.append(root)
    unique: list[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return tuple(unique)


def _is_blocked_workspace_path(candidate: Path, raw_path: str | Path | None = None) -> bool:
    """Return True when candidate points at a known OS/system directory.

    Compare both the original spelling and the resolved path.  This closes the
    macOS /etc -> /private/etc bypass without globally banning temporary pytest
    paths under /private/var/folders.
    """
    raw = None
    if raw_path not in (None, ""):
        try:
            raw = Path(raw_path).expanduser()
        except Exception:
            raw = None

    exact = _workspace_blocked_exact_roots()
    if candidate in exact or (raw is not None and raw in _workspace_blocked_roots()):
        return True

    for tmp in _USER_TMP_PREFIXES:
        if _is_within(candidate, tmp) or (raw is not None and _is_within(raw, tmp)):
            return False

    # Raw paths under literal roots (e.g. /etc/ssh, /var/db) are always blocked.
    if raw is not None:
        for blocked in _workspace_blocked_roots():
            if _is_within(raw, blocked):
                return True

    # Resolved subtree checks catch symlink aliases such as /private/etc.  The
    # macOS temp root /private/var/folders is intentionally allowed for pytest
    # and per-user temporary workspaces; other direct /private/var system data
    # such as /private/var/db and /private/var/log remains blocked.
    allowed_private_var = (Path('/private/var/folders'), Path('/private/var/tmp'))
    for blocked in _workspace_blocked_resolved_subtrees():
        if blocked == Path('/private/var'):
            if candidate == blocked:
                return True
            if any(_is_within(candidate, allowed) for allowed in allowed_private_var):
                continue
            if _is_within(candidate, blocked):
                return True
            continue
        if _is_within(candidate, blocked):
            return True
    return False


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _trusted_workspace_roots() -> list[Path]:
    roots: list[Path] = []

    def add(candidate: str | Path | None) -> None:
        if candidate in (None, ""):
            return
        try:
            p = Path(candidate).expanduser().resolve()
        except Exception:
            return
        if not p.exists() or not p.is_dir():
            return
        if _is_blocked_workspace_path(p, candidate):
            return
        if p not in roots:
            roots.append(p)

    add(Path.home())
    add(_BOOT_DEFAULT_WORKSPACE)
    for w in load_workspaces():
        add(w.get("path"))
    roots.sort(key=lambda p: len(str(p)))
    return roots


def list_workspace_suggestions(prefix: str = "", limit: int = 12) -> list[str]:
    """Return workspace path suggestions under trusted roots only.

    Suggestions are limited to directories under one of:
      - Path.home()
      - the boot default workspace
      - already-saved workspace roots

    Arbitrary system prefixes return an empty list rather than an error so the
    UI can safely autocomplete while the user types.
    """
    roots = _trusted_workspace_roots()
    if not roots:
        return []

    raw = (prefix or "").strip()
    if not raw:
        return [str(p) for p in roots[:limit]]

    if raw.startswith("~"):
        target = Path(raw).expanduser()
    elif Path(raw).is_absolute():
        target = Path(raw)
    else:
        target = Path.home() / raw

    normalized = str(target)
    normalized_lower = normalized.lower()
    suggestions: list[str] = []

    def add(path: Path) -> None:
        value = str(path)
        if value not in suggestions:
            suggestions.append(value)

    # If the user is typing a partial trusted root like /Users/xuef..., suggest
    # the matching trusted roots without scanning arbitrary system parents.
    for root in roots:
        if str(root).lower().startswith(normalized_lower):
            add(root)

    in_root = [
        root
        for root in roots
        if normalized == str(root) or normalized.startswith(str(root) + os.sep)
    ]
    if not in_root:
        return suggestions[:limit]

    anchor_root = max(in_root, key=lambda p: len(str(p)))
    ends_with_sep = raw.endswith(os.sep) or raw.endswith('/')
    parent = target if ends_with_sep else target.parent
    leaf = '' if ends_with_sep else target.name
    show_hidden = leaf.startswith('.')

    try:
        parent_resolved = parent.expanduser().resolve()
    except Exception:
        return suggestions[:limit]

    if not parent_resolved.exists() or not parent_resolved.is_dir():
        return suggestions[:limit]
    if not _is_within(parent_resolved, anchor_root):
        return suggestions[:limit]

    leaf_lower = leaf.lower()
    try:
        children = sorted(parent_resolved.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return suggestions[:limit]

    for child in children:
        if not child.is_dir():
            continue
        if child.name.startswith('.') and not show_hidden:
            continue
        if leaf_lower and not child.name.lower().startswith(leaf_lower):
            continue
        add(child.resolve())
        if len(suggestions) >= limit:
            break
    return suggestions[:limit]


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
    if path in (None, ""):
        return Path(_BOOT_DEFAULT_WORKSPACE).expanduser().resolve()

    candidate = Path(path).expanduser().resolve()

    access_error = _workspace_access_error(candidate)
    if access_error:
        raise ValueError(access_error)

    # (A) Trusted if under the user's home directory — cross-platform via Path.home()
    # Must be checked before system roots to allow symlinks like /var/home.
    _home = Path.home().resolve()
    if _home != Path("/"):
        try:
            candidate.relative_to(_home)
            return candidate
        except ValueError:
            pass

    # Block known system roots and their children.
    if _is_blocked_workspace_path(candidate, path):
        raise ValueError(f"Path points to a system directory: {candidate}")

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




def _strip_surrounding_quotes(path: str) -> str:
    """Strip a single pair of surrounding single or double quotes from a path string.

    macOS Finder's "Copy as Pathname" (Cmd+Option+C) returns paths wrapped in
    single quotes, e.g. ``'/Users/x/Documents/foo'``. Other shells and OS file
    managers do similar things with double quotes. Users routinely paste these
    quoted strings into the Add Space input expecting them to "just work" —
    the only reason they didn't was a missing strip.

    Only paired quotes are stripped (matching opener and closer). One-sided quotes
    are preserved on the slim chance a path legitimately contains a literal quote
    character.
    """
    s = path.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def validate_workspace_to_add(path: str) -> Path:
    """Validate a path for *adding* to the workspace list (less restrictive than resolve_trusted_workspace).

    When a user explicitly adds a new workspace path, we trust their intent — they
    have console or filesystem access to that path and are consciously registering it.
    We only block: non-existent paths, non-directories, and known system roots.

    The stricter ``resolve_trusted_workspace`` is used when *using* an existing workspace
    (file reads/writes) to prevent path traversal after the list is built.

    Surrounding quotes (single or double) are stripped before validation —
    macOS Finder's "Copy as Pathname" wraps paths in single quotes by default,
    and users routinely paste those into the Add Space input.
    """
    path = _strip_surrounding_quotes(path)
    candidate = Path(path).expanduser().resolve()

    access_error = _workspace_access_error(candidate)
    if access_error:
        raise ValueError(access_error)

    # Home directory is always trusted regardless of where it lives on disk
    # (e.g. /var/home/... on systemd-homed Fedora/RHEL).
    _home = Path.home().resolve()
    if _home != Path("/") and _is_within(candidate, _home):
        return candidate

    # Block known system roots and their immediate children.
    if _is_blocked_workspace_path(candidate, path):
        raise ValueError(f"Path points to a system directory: {candidate}")

    return candidate

def safe_resolve_ws(root: Path, requested: str) -> Path:
    """Resolve a relative path inside a workspace root, raising ValueError on traversal.

    Symlinks whose *unresolved* path is within the workspace root are allowed —
    the user placed them there intentionally.  Only raw ``..`` traversal outside
    the root is blocked.
    """
    import os
    unresolved = root / requested
    resolved = unresolved.resolve()
    # Fast path: resolved path is inside root (covers most cases)
    try:
        resolved.relative_to(root.resolve())
        return resolved
    except ValueError:
        pass
    # Symlink path: normalize '..' (without following symlinks) and check
    # os.path.normpath collapses '..' but does NOT follow symlinks.
    norm = Path(os.path.normpath(str(unresolved)))
    try:
        norm.relative_to(root)
    except ValueError:
        raise ValueError(f"Path traversal blocked: {requested}")
    # Symlink points outside workspace root — additionally block system directories.
    # Even if the user placed the symlink intentionally, prevent reads from
    # /etc, /proc, /sys, /dev and other blocked roots (LLM agents can call
    # read_file_content via tool calls, not just human users).
    if _is_blocked_system_path(resolved):
        raise ValueError(f"Path traversal blocked (system dir): {requested}")
    return resolved


def list_dir(workspace: Path, rel: str='.'):
    target = safe_resolve_ws(workspace, rel)
    if not target.is_dir():
        raise FileNotFoundError(f"Not a directory: {rel}")
    ws_resolved = workspace.resolve()
    entries = []
    for item in sorted(target.iterdir(), key=lambda p: (not p.is_symlink(), p.is_file(), p.name.lower())):
        if item.is_symlink():
            # Resolve the symlink target and check if it stays within workspace
            try:
                link_target = item.resolve()
            except OSError:
                continue
            # Cycle detection: skip if symlink points back to current dir,
            # workspace root, or any ancestor of current dir.
            # This must run REGARDLESS of whether target is inside workspace.
            if (link_target == target.resolve() or link_target == target
                    or link_target == ws_resolved):
                continue
            try:
                target.resolve().relative_to(link_target)
                # target is under link_target — link_target is an ancestor → cycle
                continue
            except ValueError:
                pass
            # Block symlinks that resolve to system directories.
            if _is_blocked_system_path(link_target):
                continue
            is_dir = link_target.is_dir()
            # Keep the display path relative to workspace (don't follow the link)
            display_path = str(Path(item.name))
            if rel and rel != '.':
                display_path = rel + '/' + display_path
            entry = {
                'name': item.name,
                'path': display_path,
                'type': 'symlink',
                'target': str(link_target),
                'is_dir': is_dir,
            }
            if not is_dir:
                try:
                    entry['size'] = link_target.stat().st_size
                except OSError:
                    entry['size'] = None
            entries.append(entry)
        else:
            # Use rel-based path so entries under symlink targets (outside
            # the workspace root) still get a valid workspace-relative path.
            entry_path = item.name
            if rel and rel != '.':
                entry_path = rel + '/' + item.name
            entries.append({
                'name': item.name,
                'path': entry_path,
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
    # Run the remaining git commands in parallel via threads — they are
    # independent subprocess calls and together can take 50-200ms when run
    # serially.  Threading is safe here because each call blocks only on the
    # subprocess pipe, not on the GIL.
    def _ahead():
        r = _run_git(['rev-list', '--count', '@{u}..HEAD'], workspace)
        return int(r) if r and r.isdigit() else 0
    def _behind():
        r = _run_git(['rev-list', '--count', 'HEAD..@{u}'], workspace)
        return int(r) if r and r.isdigit() else 0
    def _status():
        out = _run_git(['status', '--porcelain'], workspace) or ''
        lines = [l for l in out.splitlines() if l]
        modified = sum(1 for l in lines if len(l) >= 2 and (l[0] in 'MAR' or l[1] in 'MAR'))
        untracked = sum(1 for l in lines if l.startswith('??'))
        return len(lines), modified, untracked
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        f_status = pool.submit(_status)
        f_ahead  = pool.submit(_ahead)
        f_behind = pool.submit(_behind)
        dirty, modified, untracked = f_status.result()
        ahead  = f_ahead.result()
        behind = f_behind.result()
    return {
        'branch': branch,
        'dirty': dirty,
        'modified': modified,
        'untracked': untracked,
        'ahead': ahead,
        'behind': behind,
        'is_git': True,
    }
