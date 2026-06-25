"""
Hermes Web UI -- Workspace and file system helpers.

Workspace lists and last-used workspace are stored per-profile so each
profile has its own workspace configuration.  State files live at
``{profile_home}/webui_state/workspaces.json`` and
``{profile_home}/webui_state/last_workspace.txt``.  The global STATE_DIR
paths are used as fallback when no profile module is available.
"""
import hashlib
import json
import logging
import os
import posixpath
import secrets
import shutil
import stat
import subprocess
import concurrent.futures
import threading
import time
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

_ESCAPE_AUTH_TTL_SECONDS = 300
_ESCAPE_AUTH_LOCK = threading.Lock()
_ESCAPE_AUTH_TOKENS: dict[str, dict[str, str | int | float]] = {}

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


def _expanduser_path(path: str | Path) -> Path:
    """Return *path* after shell-style home expansion.

    ``Path.expanduser()`` on Windows does not consistently honor a monkeypatched
    ``HOME`` in tests, which makes host-native replay diverge from the repo's
    portability expectations. Use ``os.path.expanduser`` as the expansion source,
    then wrap it back into ``Path``.
    """
    raw = str(path)
    if raw.startswith('~'):
        home = (
            os.environ.get('HOME')
            or os.environ.get('USERPROFILE')
            or (
                (os.environ.get('HOMEDRIVE') or '') + (os.environ.get('HOMEPATH') or '')
            )
            or str(Path.home())
        )
        if raw in ('~', '~/', '~\\'):
            return Path(home)
        if raw.startswith('~/') or raw.startswith('~\\'):
            return Path(home) / raw[2:]
        # NOTE: ``~user`` / ``~root`` forms are intentionally NOT expanded here.
        # Master deliberately does not block ``/root`` (#510/#521 — Hermes commonly
        # runs as root, where ``/root`` is the legitimate home and is allowed via
        # the home carve-out). Expanding ``~root`` -> ``/root`` for a NON-root
        # deployment would let it register root's home; leaving the literal form
        # (resolved relative to cwd, then rejected if it escapes) is the safer
        # behavior and matches the current security model.
    return Path(raw)


def _resolve_path(path: str | Path) -> Path:
    """Resolve *path* after env-aware home expansion, without raising."""
    return _safe_resolve(_expanduser_path(path))


def _home_path() -> Path:
    """Return the current effective home directory with env-aware expansion."""
    return _resolve_path("~")


def _as_posix_path(path: str | Path | None) -> PurePosixPath | None:
    if path in (None, ""):
        return None
    raw = _strip_surrounding_quotes(str(path)).strip().replace('\\', '/')
    # Reject embedded null bytes here rather than letting them survive normpath
    # and crash later at .resolve() with an uncaught ValueError (surfaces as a
    # 500). Fail-closed: treat as an invalid path.
    if '\x00' in raw:
        return None
    if not raw.startswith('/'):
        return None
    return PurePosixPath(posixpath.normpath(raw))


def _posix_is_within(path: PurePosixPath, root: PurePosixPath) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _normalize_posix_path(path: str | Path | None) -> str | None:
    candidate = _as_posix_path(path)
    if candidate is None:
        return None
    return candidate.as_posix()


def _is_remote_terminal_backend(terminal_cfg: dict | None) -> bool:
    """Return True when the active terminal backend runs outside this WebUI host."""
    if not isinstance(terminal_cfg, dict):
        return False
    backend = str(terminal_cfg.get('backend') or '').strip().lower()
    return backend not in ('', 'local')


def _remote_terminal_cwd() -> str | None:
    """Return target-side terminal cwd for remote profiles, without local stat()."""
    try:
        from api.config import get_config

        terminal_cfg = get_config().get('terminal', {})
        if not _is_remote_terminal_backend(terminal_cfg):
            return None
        cwd = str(terminal_cfg.get('cwd') or '').strip()
        if not cwd or cwd == '.':
            return None
        return cwd
    except Exception:
        logger.debug("Failed to read remote terminal cwd", exc_info=True)
        return None


def _remote_terminal_workspace_candidate(path: str | Path) -> Path | None:
    """Return a non-stat'ed target-side Path when it is under terminal.cwd."""
    cwd = _remote_terminal_cwd()
    if not cwd:
        return None
    raw = _strip_surrounding_quotes(str(path)).strip()
    if not raw:
        return None
    if '\x00' in raw or '\x00' in cwd:
        return None
    normalized_raw = _normalize_posix_path(raw)
    normalized_cwd = _normalize_posix_path(cwd)
    if normalized_raw is not None and normalized_cwd is not None:
        posix_candidate = PurePosixPath(normalized_raw)
        posix_base = PurePosixPath(normalized_cwd)
        if _is_blocked_workspace_path(Path(normalized_raw), normalized_raw) or _is_blocked_workspace_path(Path(normalized_cwd), normalized_cwd):
            return None
        if posix_candidate == posix_base or _posix_is_within(posix_candidate, posix_base):
            return _resolve_path(normalized_raw)
        return None
    candidate = _resolve_path(raw)
    base = _resolve_path(cwd)
    if _is_blocked_workspace_path(candidate, raw) or _is_blocked_workspace_path(base, cwd):
        return None
    if candidate == base or _is_within(candidate, base):
        return candidate
    return None


def _profile_default_workspace() -> str:
    """Read the profile's default workspace from its config.yaml.

    Checks keys in priority order:
      1. 'workspace'         — explicit webui workspace key
      2. 'default_workspace' — alternate explicit key
      3. 'terminal.cwd'      — hermes-agent terminal working dir (most common)

    For remote/SSH terminal profiles, ``terminal.cwd`` lives on the target
    machine, not on the WebUI server. In that case return it without a
    server-local existence check so WebUI can send the correct workspace hint
    to the agent/tool backend.

    Falls back to the live DEFAULT_WORKSPACE from api.config.
    """
    try:
        from api.config import get_config
        cfg = get_config()
        terminal_cfg = cfg.get('terminal', {})
        remote_terminal = _is_remote_terminal_backend(terminal_cfg)
        # Explicit webui workspace keys first
        for key in ('workspace', 'default_workspace'):
            ws = cfg.get(key)
            if ws:
                if remote_terminal:
                    return str(ws).strip()
                p = _resolve_path(str(ws))
                if remote_terminal or p.is_dir():
                    return str(p)
        # Fall through to terminal.cwd — the agent's configured working directory
        if isinstance(terminal_cfg, dict):
            cwd = terminal_cfg.get('cwd', '')
            if cwd and str(cwd) not in ('.', ''):
                if remote_terminal:
                    return str(cwd).strip()
                p = _resolve_path(str(cwd))
                if remote_terminal or p.is_dir():
                    return str(p)
    except (ImportError, Exception):
        logger.debug("Failed to load profile default workspace config")
    try:
        from api.config import DEFAULT_WORKSPACE as _LIVE_DEFAULT_WORKSPACE

        return str(_resolve_path(_LIVE_DEFAULT_WORKSPACE))
    except Exception:
        return str(_resolve_path(_BOOT_DEFAULT_WORKSPACE))


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
    hermes_profiles = (_home_path() / '.hermes' / 'profiles').resolve()
    result = []
    for w in workspaces:
        path = w.get('path', '')
        name = w.get('name', '')
        if not path:
            continue
        p = _safe_resolve(_expanduser_path(path))
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
    except ValueError as exc:
        # Embedded null byte (or similar invalid path) — .stat() raises ValueError,
        # not OSError. Report as an access error rather than letting it surface as
        # an uncaught 500.
        return f"Cannot access path: {candidate!r}. Invalid path ({exc})."
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
    remote_cwd = _remote_terminal_cwd()

    def valid_last_workspace(raw: str) -> str | None:
        if not raw:
            return None
        if remote_cwd:
            # For remote/SSH profiles, last_workspace is target-side state. Do
            # not accept stale server-local paths merely because they exist on
            # the WebUI host; require the value to stay under terminal.cwd.
            if _remote_terminal_workspace_candidate(raw) is not None:
                return raw
            return None
        if Path(raw).is_dir():
            return raw
        return None

    lw_file = _last_workspace_file()
    if lw_file.exists():
        try:
            p = valid_last_workspace(lw_file.read_text(encoding='utf-8').strip())
            if p:
                return p
        except Exception:
            logger.debug("Failed to read last workspace from %s", lw_file)
    # Fallback: try global file
    if _GLOBAL_LW_FILE.exists():
        try:
            p = valid_last_workspace(_GLOBAL_LW_FILE.read_text(encoding='utf-8').strip())
            if p:
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
    except (OSError, RuntimeError, ValueError):
        # ValueError covers embedded-null-byte paths, which .resolve() raises on
        # — fail-closed to the raw path so the downstream block-list gate rejects
        # it cleanly instead of surfacing a 500.
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


def _is_blocked_posix_workspace_path(raw_path: str | Path | None) -> bool:
    """Detect blocked POSIX-style system roots even on non-POSIX hosts."""
    candidate = _as_posix_path(raw_path)
    if candidate is None:
        return False
    if candidate == PurePosixPath('/'):
        return True
    carveouts = (
        PurePosixPath('/var/folders'),
        PurePosixPath('/private/var/folders'),
        PurePosixPath('/var/tmp'),
        PurePosixPath('/private/var/tmp'),
    )
    for tmp in carveouts:
        if _posix_is_within(candidate, tmp):
            return False
    blocked_roots = (
        PurePosixPath('/etc'),
        PurePosixPath('/usr'),
        PurePosixPath('/var'),
        PurePosixPath('/bin'),
        PurePosixPath('/sbin'),
        PurePosixPath('/boot'),
        PurePosixPath('/proc'),
        PurePosixPath('/sys'),
        PurePosixPath('/dev'),
        PurePosixPath('/lib'),
        PurePosixPath('/lib64'),
        PurePosixPath('/opt/homebrew'),
        PurePosixPath('/System'),
        PurePosixPath('/Library'),
        PurePosixPath('/private/etc'),
        PurePosixPath('/private/var'),
    )
    for blocked in blocked_roots:
        if _posix_is_within(candidate, blocked):
            return True
    return False


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
            normalized_posix = _normalize_posix_path(raw_path)
            raw = Path(normalized_posix) if normalized_posix is not None else _expanduser_path(raw_path)
        except Exception:
            raw = None

    posix_probe = raw_path if raw_path not in (None, "") else candidate.as_posix()
    if _is_blocked_posix_workspace_path(posix_probe):
        return True

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
            p = _resolve_path(candidate)
        except Exception:
            return
        if not p.exists() or not p.is_dir():
            return
        if _is_blocked_workspace_path(p, candidate):
            return
        if p not in roots:
            roots.append(p)

    add(_home_path())
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
        target = _expanduser_path(raw)
    elif Path(raw).is_absolute():
        target = Path(raw)
    else:
        target = _home_path() / raw

    try:
        match_target = _resolve_path(target)
    except Exception:
        match_target = target

    normalized = str(match_target)
    normalized_lower = normalized.lower()
    preserve_tilde = raw.startswith("~")
    home_root: Path | None = None
    if preserve_tilde:
        try:
            home_root = _home_path()
        except Exception:
            home_root = None
    suggestions: list[str] = []

    def format_suggestion(path: Path) -> str:
        if preserve_tilde and home_root is not None:
            try:
                rel = path.resolve().relative_to(home_root)
                if str(rel) == ".":
                    return "~"
                return "~/" + rel.as_posix()
            except (OSError, ValueError):
                pass
        return str(path)

    def add(path: Path) -> None:
        value = format_suggestion(path)
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
        parent_resolved = _resolve_path(parent)
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
        return _resolve_path(_BOOT_DEFAULT_WORKSPACE)

    candidate = _resolve_path(path)

    access_error = _workspace_access_error(candidate)
    remote_candidate = _remote_terminal_workspace_candidate(path)
    if access_error:
        # For remote terminal profiles, workspace paths belong to the target
        # machine. Allow paths under terminal.cwd so session switching can
        # update the workspace hint even though this WebUI host cannot stat
        # the target-side path.
        if remote_candidate is None:
            raise ValueError(access_error)

    if remote_candidate is not None:
        return remote_candidate

    # (A) Trusted if under the user's home directory — cross-platform via Path.home()
    # Must be checked before system roots to allow symlinks like /var/home.
    _home = _home_path()
    if _home != Path("/"):
        try:
            candidate.relative_to(_home)
            return candidate
        except ValueError:
            pass

    if _is_blocked_workspace_path(candidate, path):
        raise ValueError(f"Path points to a system directory: {candidate}")

    # (B) Trusted if already in the saved workspace list — covers non-home installs
    try:
        saved = load_workspaces()
        saved_paths = {_resolve_path(w["path"]) for w in saved if w.get("path")}
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
        boot_default = _resolve_path(_BOOT_DEFAULT_WORKSPACE)
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
    candidate = _resolve_path(path)

    access_error = _workspace_access_error(candidate)
    remote_candidate = _remote_terminal_workspace_candidate(path)
    if access_error:
        # Remote terminal profiles validate workspace existence on the target
        # machine, not on the WebUI server. Permit target-side paths under
        # terminal.cwd.
        if remote_candidate is None:
            raise ValueError(access_error)

    if remote_candidate is not None:
        return remote_candidate

    # Home directory is always trusted regardless of where it lives on disk
    # (e.g. /var/home/... on systemd-homed Fedora/RHEL).
    _home = _home_path()
    if _home != Path("/") and _is_within(candidate, _home):
        return candidate

    if _is_blocked_workspace_path(candidate, path):
        raise ValueError(f"Path points to a system directory: {candidate}")

    return candidate

def safe_resolve_ws(root: Path, requested: str) -> Path:
    """Resolve a relative path inside a workspace root, raising ValueError on traversal.

    Both raw ``..`` traversal and symlink escapes are blocked.  Workspace file
    APIs can be reached by browser UI actions and agent/tool calls, so a symlink
    inside the workspace must not expand the trusted workspace boundary to an
    arbitrary host path.
    """
    root_resolved = root.resolve()
    resolved = (root / requested).resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        raise ValueError(f"Path traversal blocked: {requested}")
    return resolved


# ── Race-safe (TOCTOU) anchored open ─────────────────────────────────────────
# safe_resolve_ws() validates a path, but if callers then re-open by pathname a
# symlink swapped in AFTER the check could still escape the workspace. To close
# that window we open the (already symlink-resolved) target component-by-component
# from the workspace root using openat (dir_fd) + O_NOFOLLOW: every component must
# be a real, non-symlink entry, so a component swapped to a symlink mid-flight is
# refused. Legit in-workspace symlinks still work because safe_resolve_ws() has
# already collapsed them to their real in-workspace target, and we walk that real
# (symlink-free) path. Portable: uses os.supports_dir_fd where available (Linux,
# macOS); on platforms without dir_fd support (Windows — where creating symlinks
# also requires admin) we fall back to a plain pathname open, matching the prior
# behaviour with no regression.

_DIR_FD_OK = os.open in getattr(os, "supports_dir_fd", set())
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


def open_anchored_fd(workspace: Path, target: Path, *, want_dir: bool) -> int:
    """Open ``target`` race-safely and return an owned file descriptor.

    ``target`` must be the symlink-resolved path returned by safe_resolve_ws()
    (i.e. already verified to live under the workspace). Raises FileNotFoundError
    if a component is missing / wrong-type, or ValueError if a component was
    swapped to a symlink (escape attempt). Caller owns and must close the fd.
    """
    root_resolved = workspace.resolve()
    # Relative, symlink-free component list (resolve() already collapsed any links).
    try:
        rel_parts = target.relative_to(root_resolved).parts
    except ValueError:
        raise ValueError(f"Path traversal blocked: {target}") from None

    if not _DIR_FD_OK:
        # Windows / no openat: fall back to a plain pathname open. No new race
        # protection, but no regression vs the prior path-based behaviour, and
        # symlink creation needs admin on Windows anyway.
        flags = os.O_RDONLY | (_O_DIRECTORY if want_dir else 0) | _O_NOFOLLOW
        try:
            return os.open(str(target), flags)
        except OSError:
            raise FileNotFoundError(f"Not found: {target}") from None

    # Open the (trusted) workspace root. root_resolved is canonical (resolve()
    # collapsed any symlinks to REACH it, e.g. macOS /tmp -> /private/tmp), so its
    # final component is legitimately a real directory — O_NOFOLLOW here only fires
    # if the root itself was raced into a symlink after resolve() (escape attempt).
    fd = os.open(str(root_resolved), os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
    try:
        for i, part in enumerate(rel_parts):
            is_last = i == len(rel_parts) - 1
            want_directory = (not is_last) or want_dir
            flags = os.O_RDONLY | _O_NOFOLLOW | (_O_DIRECTORY if want_directory else 0)
            try:
                nfd = os.open(part, flags, dir_fd=fd)
            except OSError:
                # ELOOP (component is a symlink — swapped in) or missing/wrong type.
                raise FileNotFoundError(f"Not found: {target}") from None
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def open_anchored_create_fd(root: Path, dest: Path) -> int:
    """Create ``dest`` for exclusive writing race-safely, anchored under ``root``.

    Walks from ``root`` via openat + O_NOFOLLOW (creating missing intermediate
    directories with mkdir(dir_fd=...)), then creates the leaf with
    O_CREAT|O_EXCL|O_NOFOLLOW so a symlink raced into any component cannot
    redirect the write outside ``root``. ``dest`` must be the resolved path and
    must not already exist (callers dedup first). Raises ValueError if ``dest``
    is not under ``root``, FileExistsError if it exists, FileNotFoundError if a
    component was swapped to a symlink. Caller owns and must close the returned
    write fd. On platforms without dir_fd support (Windows) falls back to a plain
    exclusive create — no new race protection but no regression.
    """
    root_resolved = root.resolve()
    try:
        rel_parts = dest.relative_to(root_resolved).parts
    except ValueError:
        raise ValueError(f"Path traversal blocked: {dest}") from None
    if not rel_parts:
        raise ValueError(f"Invalid destination: {dest}")

    if not _DIR_FD_OK:
        # Windows / no openat: create parent dirs then exclusively create the leaf.
        dest.parent.mkdir(parents=True, exist_ok=True)
        return os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW, 0o644)

    fd = os.open(str(root_resolved), os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
    try:
        for part in rel_parts[:-1]:
            try:
                nfd = os.open(part, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(part, 0o755, dir_fd=fd)
                nfd = os.open(part, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=fd)
            except OSError:
                # ELOOP — component swapped to a symlink (escape attempt).
                raise FileNotFoundError(f"Not found: {dest}") from None
            os.close(fd)
            fd = nfd
        return os.open(
            rel_parts[-1],
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
            0o644,
            dir_fd=fd,
        )
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def make_anchored_dir(root: Path, dest: Path) -> None:
    """Create directory ``dest`` (and any missing parents) race-safely under ``root``.

    Walks from ``root`` via openat + O_NOFOLLOW, creating each missing component
    with mkdir(dir_fd=...), so a symlink raced into any component cannot make the
    server create directories outside ``root``. Idempotent (existing dirs are
    fine). Raises ValueError if ``dest`` is not under ``root``, FileNotFoundError
    if a component was swapped to a symlink. On platforms without dir_fd support
    (Windows) falls back to a plain Path.mkdir — no regression.
    """
    root_resolved = root.resolve()
    dest_resolved = dest.resolve()
    if dest_resolved == root_resolved:
        return
    try:
        rel_parts = dest_resolved.relative_to(root_resolved).parts
    except ValueError:
        raise ValueError(f"Path traversal blocked: {dest}") from None

    if not _DIR_FD_OK:
        dest.mkdir(parents=True, exist_ok=True)
        return

    fd = os.open(str(root_resolved), os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
    try:
        for part in rel_parts:
            try:
                nfd = os.open(part, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(part, 0o755, dir_fd=fd)
                nfd = os.open(part, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=fd)
            except OSError:
                # ELOOP — component swapped to a symlink (escape attempt).
                raise FileNotFoundError(f"Not found: {dest}") from None
            os.close(fd)
            fd = nfd
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def open_anchored_write_fd(root: Path, target: Path) -> int:
    """Open existing ``target`` for truncating writes anchored under ``root``."""
    root_resolved = root.resolve()
    target_resolved = target.resolve()
    try:
        rel_parts = target_resolved.relative_to(root_resolved).parts
    except ValueError:
        raise ValueError(f"Path traversal blocked: {target}") from None
    if not rel_parts:
        raise ValueError(f"Invalid target: {target}")

    flags = os.O_WRONLY | os.O_TRUNC | _O_NOFOLLOW
    if not _DIR_FD_OK:
        return os.open(str(target_resolved), flags)

    parent_fd = open_anchored_fd(root_resolved, target_resolved.parent, want_dir=True)
    try:
        return os.open(rel_parts[-1], flags, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def unlink_anchored(root: Path, target: Path) -> None:
    """Unlink an existing file anchored under ``root``."""
    root_resolved = root.resolve()
    target_resolved = target.resolve()
    try:
        rel_parts = target_resolved.relative_to(root_resolved).parts
    except ValueError:
        raise ValueError(f"Path traversal blocked: {target}") from None
    if not rel_parts:
        raise ValueError(f"Invalid target: {target}")

    if not _DIR_FD_OK:
        target_resolved.unlink()
        return

    parent_fd = open_anchored_fd(root_resolved, target_resolved.parent, want_dir=True)
    try:
        os.unlink(rel_parts[-1], dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def rmtree_anchored(root: Path, target: Path) -> None:
    """Remove a directory tree anchored under ``root`` without following symlink swaps."""
    root_resolved = root.resolve()
    target_resolved = target.resolve()
    try:
        rel_parts = target_resolved.relative_to(root_resolved).parts
    except ValueError:
        raise ValueError(f"Path traversal blocked: {target}") from None
    if not rel_parts:
        raise ValueError(f"Invalid target: {target}")

    if not _DIR_FD_OK:
        shutil.rmtree(target_resolved)
        return

    parent_fd = open_anchored_fd(root_resolved, target_resolved.parent, want_dir=True)
    try:
        shutil.rmtree(rel_parts[-1], dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def rename_anchored(root: Path, source: Path, dest: Path) -> None:
    """Rename ``source`` to ``dest`` using anchored parent directory fds."""
    root_resolved = root.resolve()
    source_resolved = source.resolve()
    dest_parent_resolved = dest.parent.resolve()
    try:
        source_parts = source_resolved.relative_to(root_resolved).parts
    except ValueError:
        raise ValueError(f"Path traversal blocked: {source}") from None
    try:
        dest_parent_resolved.relative_to(root_resolved)
    except ValueError:
        raise ValueError(f"Path traversal blocked: {dest}") from None
    if not source_parts:
        raise ValueError(f"Invalid source: {source}")
    dest_leaf = dest.name
    if not dest_leaf:
        raise ValueError(f"Invalid destination: {dest}")

    if not _DIR_FD_OK:
        source_resolved.rename(dest)
        return

    src_parent_fd = open_anchored_fd(root_resolved, source_resolved.parent, want_dir=True)
    try:
        dst_parent_fd = open_anchored_fd(root_resolved, dest_parent_resolved, want_dir=True)
        try:
            try:
                os.stat(dest_leaf, dir_fd=dst_parent_fd, follow_symlinks=False)
                raise FileExistsError(dest_leaf)
            except FileNotFoundError:
                pass
            os.rename(
                source_parts[-1],
                dest_leaf,
                src_dir_fd=src_parent_fd,
                dst_dir_fd=dst_parent_fd,
            )
        finally:
            os.close(dst_parent_fd)
    finally:
        os.close(src_parent_fd)


def list_dir(workspace: Path, rel: str='.'):
    target = safe_resolve_ws(workspace, rel)
    if not target.is_dir():
        raise FileNotFoundError(f"Not a directory: {rel}")
    ws_resolved = workspace.resolve()
    target_resolved = target.resolve()
    entries = []

    def _process(name, is_symlink, raw_link, lstat_result, reachable):
        """Append one directory entry. ``raw_link`` is the os.readlink() result
        for symlinks (else None); ``lstat_result`` is an os.stat_result obtained
        with follow_symlinks=False (else None); ``reachable`` is False when a
        follow_symlinks=True stat raised (broken target or symlink loop)."""
        if is_symlink:
            if raw_link is None:
                return
            # A symlink whose follow-stat raised (ELOOP / broken target) can never
            # be opened — filter it. This catches mutual/self loops portably across
            # Python versions where Path.resolve() loop handling differs (3.11
            # raises RuntimeError, 3.13 can return a path), so do not rely on
            # resolve() raising for cycle detection.
            if not reachable:
                return
            try:
                link_target = (target_resolved / raw_link).resolve()
            except (OSError, RuntimeError):
                return
            # Cycle detection: skip if symlink points back to current dir or root.
            if link_target == target_resolved or link_target == ws_resolved:
                return
            try:
                target_resolved.relative_to(link_target)
                return  # target is under link_target — ancestor → cycle
            except ValueError:
                pass
            # Tag symlinks whose resolved target escapes the workspace root.
            # Previously silently dropped; now emitted with target_outside_workspace=True
            # so the workspace tree can show the link exists (display-only — the
            # read/list gate in safe_resolve_ws / open_anchored_fd still blocks
            # navigation through it).
            target_outside_workspace = False
            try:
                link_target.relative_to(ws_resolved)
            except ValueError:
                target_outside_workspace = True
            if _is_blocked_system_path(link_target):
                return
            display_path = name
            if rel and rel != '.':
                display_path = rel + '/' + display_path
            mtime_ns = lstat_result.st_mtime_ns if lstat_result is not None else None
            if target_outside_workspace:
                # #4581 hardening: a display-only escape-target symlink must NOT
                # disclose where it points. Emit ONLY display-safe fields — never
                # the resolved outside path, target-derived is_dir, or target size
                # (the row exists to show the link is present; navigation/read
                # through it stays blocked by safe_resolve_ws/open_anchored_fd).
                entry = {
                    'name': name,
                    'path': display_path,
                    'type': 'symlink',
                    'is_dir': False,
                    'target_outside_workspace': True,
                    'mtime_ns': mtime_ns,
                }
                entries.append(entry)
            else:
                is_dir = link_target.is_dir()
                entry = {
                    'name': name,
                    'path': display_path,
                    'type': 'symlink',
                    'target': str(link_target),
                    'is_dir': is_dir,
                    'target_outside_workspace': False,
                    'mtime_ns': mtime_ns,
                }
                if not is_dir:
                    try:
                        entry['size'] = link_target.stat().st_size
                    except OSError:
                        entry['size'] = None
                entries.append(entry)
        else:
            entry_path = name
            if rel and rel != '.':
                entry_path = rel + '/' + name
            if lstat_result is not None:
                is_file = stat.S_ISREG(lstat_result.st_mode)
                size = lstat_result.st_size if is_file else None
                mtime_ns = lstat_result.st_mtime_ns
                is_dir_entry = stat.S_ISDIR(lstat_result.st_mode)
            else:
                size = None
                mtime_ns = None
                is_dir_entry = False
            entries.append({
                'name': name,
                'path': entry_path,
                'type': 'dir' if is_dir_entry else 'file',
                'size': size,
                'mtime_ns': mtime_ns,
            })

    if _DIR_FD_OK:
        # #3398 TOCTOU hardening (Linux/macOS): open the directory via an anchored
        # openat-walk (O_NOFOLLOW on every component) and enumerate via the verified
        # fd (os.scandir(fd) + fd-relative fstatat/readlinkat), so a path component
        # swapped to an escaping symlink after safe_resolve_ws() cannot redirect the
        # listing.
        def _sort_key_de(de):
            try:
                is_link = de.is_symlink()
            except OSError:
                is_link = False
            is_file = False
            if not is_link:
                try:
                    is_file = de.is_file()
                except OSError:
                    pass
            return (not is_link, is_file, de.name.lower())

        dir_fd = open_anchored_fd(workspace, target, want_dir=True)
        try:
            st = os.fstat(dir_fd)
            if not stat.S_ISDIR(st.st_mode):
                raise FileNotFoundError(f"Not a directory: {rel}")
            with os.scandir(dir_fd) as scan:
                scandir_entries = sorted(scan, key=_sort_key_de)
            for de in scandir_entries:
                name = de.name
                is_symlink = de.is_symlink()
                raw_link = None
                if is_symlink:
                    try:
                        raw_link = os.readlink(name, dir_fd=dir_fd)
                    except OSError:
                        raw_link = None
                try:
                    lst = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                except OSError:
                    lst = None
                # reachable: follow-stat succeeds (filters ELOOP/broken symlinks).
                reachable = True
                if is_symlink:
                    try:
                        os.stat(name, dir_fd=dir_fd, follow_symlinks=True)
                    except OSError:
                        reachable = False
                _process(name, is_symlink, raw_link, lst, reachable)
                if len(entries) >= 200:
                    break
        finally:
            try:
                os.close(dir_fd)
            except OSError:
                pass
    else:
        # Portability fallback (Windows / no dir_fd): path-based enumeration after
        # safe_resolve_ws(). No anchored-fd race protection on these platforms, but
        # no regression vs the prior behaviour (creating symlinks on Windows needs
        # admin anyway), and safe_resolve_ws() still blocks the static escape.
        def _sort_key_p(p: Path):
            is_link = p.is_symlink()
            is_file = False
            if not is_link:
                try:
                    is_file = p.is_file()
                except OSError:
                    pass
            return (not is_link, is_file, p.name.lower())

        for item in sorted(target.iterdir(), key=_sort_key_p):
            name = item.name
            is_symlink = item.is_symlink()
            raw_link = None
            if is_symlink:
                try:
                    raw_link = os.readlink(str(item))
                except OSError:
                    raw_link = None
            try:
                lst = item.lstat()
            except OSError:
                lst = None
            # reachable: follow-stat succeeds (filters ELOOP/broken symlinks).
            reachable = True
            if is_symlink:
                try:
                    os.stat(str(item), follow_symlinks=True)
                except OSError:
                    reachable = False
            _process(name, is_symlink, raw_link, lst, reachable)
            if len(entries) >= 200:
                break
    return entries


def dir_signature(workspace: Path, rel: str = '.', entries: list[dict] | None = None) -> str:
    """Return a cheap, stable signature for a listed workspace directory.

    The signature is based only on bounded directory-entry metadata already used
    by the workspace tree: names, displayed paths, entry type, file sizes,
    mtimes, and symlink targets. It intentionally does not read file contents.
    """
    if entries is None:
        entries = list_dir(workspace, rel)
    payload = []
    for entry in entries:
        payload.append({
            'name': entry.get('name'),
            'path': entry.get('path'),
            'type': entry.get('type'),
            'is_dir': entry.get('is_dir'),
            'size': entry.get('size'),
            'mtime_ns': entry.get('mtime_ns'),
            'target': entry.get('target'),
            'target_outside_workspace': entry.get('target_outside_workspace'),
        })
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def read_file_content(workspace: Path, rel: str) -> dict:
    target = safe_resolve_ws(workspace, rel)
    if not target.is_file():
        raise FileNotFoundError(f"Not a file: {rel}")
    # #3398 TOCTOU hardening: open the resolved file via an anchored openat-walk
    # (O_NOFOLLOW on every component) so a path swapped to an escaping symlink
    # after safe_resolve_ws() cannot be followed, then read from the fd (not the
    # pathname) so the bytes returned are guaranteed to be the verified file.
    fd = open_anchored_fd(workspace, target, want_dir=False)
    with os.fdopen(fd, 'rb', closefd=True) as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise FileNotFoundError(f"Not a file: {rel}")
        if st.st_size > MAX_FILE_BYTES:
            raise ValueError(f"File too large ({st.st_size} bytes, max {MAX_FILE_BYTES})")
        raw = fh.read(MAX_FILE_BYTES + 1)
    content = raw.decode('utf-8', errors='replace')
    return {'path': rel, 'content': content, 'size': len(raw), 'lines': content.count('\n') + 1}


def _normalize_workspace_rel_path(rel: str | Path) -> str:
    raw = _strip_surrounding_quotes(str(rel or "")).strip().replace("\\", "/")
    if not raw or raw == ".":
        return "."
    norm = posixpath.normpath(raw)
    if not norm or norm == ".":
        return "."
    if norm == ".." or norm.startswith("../") or norm.startswith("/"):
        raise ValueError(f"Path traversal blocked: {rel}")
    return norm


def _escape_virtual_path(root: str, rel: str) -> str:
    root_norm = _normalize_workspace_rel_path(root)
    rel_norm = _normalize_workspace_rel_path(rel)
    if root_norm == ".":
        return rel_norm
    if rel_norm == ".":
        return root_norm
    return f"{root_norm}/{rel_norm}"


def _escape_surface_target(workspace: Path, rel: str) -> tuple[Path, Path]:
    workspace_root = workspace.resolve()
    surface_rel = _normalize_workspace_rel_path(rel)
    surface_posix = PurePosixPath(surface_rel)
    parent_rel = str(surface_posix.parent)
    if parent_rel in ("", "."):
        parent_path = workspace_root
    else:
        parent_path = safe_resolve_ws(workspace_root, parent_rel)
    surface_path = parent_path / surface_posix.name
    if not surface_path.is_symlink():
        raise ValueError(f"Path is not an escape-target symlink: {rel}")
    target = surface_path.resolve()
    if not target.exists():
        raise ValueError(f"Path is no longer reachable: {rel}")
    try:
        target.relative_to(workspace_root)
    except ValueError:
        pass
    else:
        raise ValueError(f"Path does not escape workspace: {rel}")
    if _is_blocked_system_path(target):
        raise ValueError(f"Path points to a system directory: {target}")
    return surface_path, target


def _escape_authorized_root(target: Path) -> tuple[Path, str]:
    resolved_target = target.resolve()
    if resolved_target.is_dir():
        return resolved_target, "."
    return resolved_target.parent, resolved_target.name


class EscapeAuthorizationExpiredError(ValueError):
    pass


def _escape_prune_tokens(now: float | None = None) -> None:
    cutoff = time.time() if now is None else now
    expired = [token for token, record in _ESCAPE_AUTH_TOKENS.items() if float(record.get("expires_at") or 0.0) <= cutoff]
    for token in expired:
        _ESCAPE_AUTH_TOKENS.pop(token, None)


def authorize_escape_target(workspace: Path, session_id: str, rel: str) -> dict:
    """Mint a short-lived browser-only grant for one surfaced escape-target symlink."""
    workspace_root = workspace.resolve()
    _surface_path, target = _escape_surface_target(workspace_root, rel)
    external_root, external_entry_rel = _escape_authorized_root(target)
    token = secrets.token_urlsafe(24)
    expires_at = time.time() + _ESCAPE_AUTH_TTL_SECONDS
    record = {
        "session_id": str(session_id or ""),
        "workspace_root": str(workspace_root),
        "surface_path": _normalize_workspace_rel_path(rel),
        "external_root": str(external_root),
        "external_entry_rel": external_entry_rel,
        "surface_target": str(target),
        "expires_at": expires_at,
    }
    with _ESCAPE_AUTH_LOCK:
        _escape_prune_tokens()
        _ESCAPE_AUTH_TOKENS[token] = record
    return {
        "token": token,
        "path": record["surface_path"],
        "is_dir": target.is_dir(),
        "expires_at": expires_at,
        "expires_in": _ESCAPE_AUTH_TTL_SECONDS,
        "read_only": True,
    }


def _escape_authorization_record(workspace: Path, session_id: str, token: str) -> dict:
    workspace_root = str(workspace.resolve())
    token = str(token or "").strip()
    if not token:
        raise ValueError("Escape authorization token is required")
    now = time.time()
    with _ESCAPE_AUTH_LOCK:
        _escape_prune_tokens(now)
        record = dict(_ESCAPE_AUTH_TOKENS.get(token) or {})
    if not record:
        raise EscapeAuthorizationExpiredError("Escape authorization expired")
    if str(record.get("session_id") or "") != str(session_id or ""):
        raise EscapeAuthorizationExpiredError("Escape authorization expired")
    if str(record.get("workspace_root") or "") != workspace_root:
        raise EscapeAuthorizationExpiredError("Escape authorization expired")
    surface_path = _normalize_workspace_rel_path(record.get("surface_path") or ".")
    surface_target = str(record.get("surface_target") or "")
    try:
        _surface, current_target = _escape_surface_target(Path(workspace_root), surface_path)
    except ValueError:
        raise EscapeAuthorizationExpiredError("Escape authorization expired") from None
    if str(current_target.resolve()) != surface_target:
        raise EscapeAuthorizationExpiredError("Escape authorization expired")
    if not current_target.exists() or _is_blocked_system_path(current_target):
        raise EscapeAuthorizationExpiredError("Escape authorization expired")
    return record


def resolve_authorized_escape_request(workspace: Path, session_id: str, token: str, rel: str) -> dict:
    record = _escape_authorization_record(workspace, session_id, token)
    surface_path = _normalize_workspace_rel_path(record["surface_path"])
    request_path = _normalize_workspace_rel_path(rel)
    try:
        requested_rel = str(PurePosixPath(request_path).relative_to(PurePosixPath(surface_path)))
    except ValueError:
        raise ValueError(f"Path traversal blocked: {rel}") from None
    if not requested_rel:
        requested_rel = "."
    external_root = Path(str(record["external_root"]))
    external_entry_rel = _normalize_workspace_rel_path(record.get("external_entry_rel") or ".")
    if external_entry_rel == ".":
        external_rel = requested_rel
    elif requested_rel == ".":
        external_rel = external_entry_rel
    else:
        external_rel = str(PurePosixPath(external_entry_rel) / PurePosixPath(requested_rel))
    target = external_root / external_rel
    return {
        "record": record,
        "workspace_root": Path(str(record["workspace_root"])),
        "surface_path": surface_path,
        "request_path": request_path,
        "external_rel": external_rel,
        "external_root": external_root,
        "target": target,
    }


def list_authorized_escape_dir(workspace: Path, session_id: str, token: str, rel: str) -> dict:
    resolved = resolve_authorized_escape_request(workspace, session_id, token, rel)
    external_root = resolved["external_root"]
    external_rel = resolved["external_rel"]
    entries = list_dir(external_root, external_rel)
    surface_path = resolved["surface_path"]
    external_root_resolved = external_root.resolve()
    for entry in entries:
        entry["path"] = _escape_virtual_path(surface_path, entry.get("path") or ".")
        entry["escape_read_only"] = True
        target = entry.get("target")
        if not target:
            continue
        try:
            target_path = Path(str(target)).resolve()
            target_rel = target_path.relative_to(external_root_resolved).as_posix()
        except Exception:
            entry.pop("target", None)
            continue
        entry["target"] = _escape_virtual_path(surface_path, target_rel)
    return {
        "path": resolved["request_path"],
        "entries": entries,
        "signature": dir_signature(external_root, external_rel, entries),
        "virtual_root": surface_path,
        "read_only": True,
    }


def read_authorized_escape_file_content(workspace: Path, session_id: str, token: str, rel: str) -> dict:
    resolved = resolve_authorized_escape_request(workspace, session_id, token, rel)
    payload = read_file_content(resolved["external_root"], resolved["external_rel"])
    payload["path"] = resolved["request_path"]
    payload["escape_read_only"] = True
    return payload


def raw_authorized_escape_target(workspace: Path, session_id: str, token: str, rel: str) -> tuple[Path, Path]:
    resolved = resolve_authorized_escape_request(workspace, session_id, token, rel)
    target = safe_resolve_ws(resolved["external_root"], resolved["external_rel"])
    return resolved["external_root"], target


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
