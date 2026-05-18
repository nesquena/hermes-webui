"""
Hermes Web UI -- Profile state management.
Wraps hermes_cli.profiles to provide profile switching for the web UI.

The web UI maintains a process-level "active profile" that determines which
HERMES_HOME directory is used for config, skills, memory, cron, and API keys.
Profile switches update os.environ['HERMES_HOME'] and monkey-patch module-level
cached paths in hermes-agent modules (skills_tool, skill_manager_tool,
cron/jobs) that snapshot HERMES_HOME at import time.
"""
import base64
import copy
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from api.session_events import publish_session_list_changed

logger = logging.getLogger(__name__)

# ── Constants (match hermes_cli.profiles upstream) ─────────────────────────
_PROFILE_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
_PROFILE_DIRS = [
    'memories', 'sessions', 'skills', 'skins',
    'logs', 'plans', 'workspace', 'cron',
]
_CLONE_CONFIG_FILES = ['config.yaml', '.env', 'SOUL.md']

# ── Module state ────────────────────────────────────────────────────────────
_active_profile = 'default'
_profile_lock = threading.Lock()
_loaded_profile_env_keys: set[str] = set()

# Thread-local profile context: set per-request by server.py, cleared after.
# Enables per-client profile isolation (issue #798) — each HTTP request thread
# reads its own profile from the hermes_profile cookie instead of the
# process-global _active_profile.
_tls = threading.local()

_SKILL_HOME_MODULES = ("tools.skills_tool", "tools.skill_manager_tool")


def snapshot_skill_home_modules() -> dict[str, dict[str, object]]:
    """Snapshot imported skill-module path globals before a temporary patch."""
    snapshot: dict[str, dict[str, object]] = {}
    for module_name in _SKILL_HOME_MODULES:
        module = sys.modules.get(module_name)
        if module is None:
            snapshot[module_name] = {"module_present": False}
            continue
        snapshot[module_name] = {
            "module_present": True,
            "has_HERMES_HOME": hasattr(module, "HERMES_HOME"),
            "HERMES_HOME": getattr(module, "HERMES_HOME", None),
            "has_SKILLS_DIR": hasattr(module, "SKILLS_DIR"),
            "SKILLS_DIR": getattr(module, "SKILLS_DIR", None),
        }
    return snapshot


def patch_skill_home_modules(home: Path) -> None:
    """Patch imported skill modules that cache HERMES_HOME at import time."""
    for module_name in _SKILL_HOME_MODULES:
        module = sys.modules.get(module_name)
        if module is None:
            continue
        try:
            module.HERMES_HOME = home
            module.SKILLS_DIR = home / "skills"
        except AttributeError:
            logger.debug("Failed to patch %s module", module_name)


def restore_skill_home_modules(snapshot: dict[str, dict[str, object]]) -> None:
    """Restore skill-module globals captured by snapshot_skill_home_modules()."""
    for module_name, values in snapshot.items():
        module = sys.modules.get(module_name)
        if not values.get("module_present"):
            if module is not None:
                sys.modules.pop(module_name, None)
                parent_name, _, child_name = module_name.rpartition(".")
                parent = sys.modules.get(parent_name)
                if parent is not None:
                    try:
                        delattr(parent, child_name)
                    except AttributeError:
                        pass
            continue
        if module is None:
            continue
        for attr in ("HERMES_HOME", "SKILLS_DIR"):
            has_attr = bool(values.get(f"has_{attr}"))
            try:
                if has_attr:
                    setattr(module, attr, values.get(attr))
                else:
                    try:
                        delattr(module, attr)
                    except AttributeError:
                        pass
            except AttributeError:
                logger.debug("Failed to restore %s.%s", module_name, attr)


def _unwrap_profile_home_to_base(home: Path) -> Path:
    """Return the base Hermes home when *home* is already a named profile dir."""
    if home.parent.name == 'profiles':
        return home.parent.parent
    return home


def _resolve_base_hermes_home() -> Path:
    """Return the BASE ~/.hermes directory — the root that contains profiles/.

    This is intentionally distinct from HERMES_HOME, which tracks the *active
    profile's* home and changes on every profile switch.  The base dir must
    always point to the top-level .hermes regardless of which profile is active.

    Resolution order:
      1. HERMES_BASE_HOME env var (set explicitly, highest priority)
      2. HERMES_HOME env var — but only if it does NOT look like a profile subdir
         (i.e. its parent is not named 'profiles').  This handles test isolation
         where HERMES_HOME is set to an isolated test state dir.
      3. ~/.hermes (always-correct default)

    The bug this prevents: if HERMES_HOME has already been mutated to
    /home/user/.hermes/profiles/webui (by init_profile_state at startup),
    reading it here would make _DEFAULT_HERMES_HOME point to that subdir,
    causing switch_profile('webui') to look for
    /home/user/.hermes/profiles/webui/profiles/webui — which doesn't exist.

    HERMES_BASE_HOME normally points at the base home already, but isolated
    single-profile WebUI deployments can provide /base/profiles/<name> there as
    well.  Normalize both env vars through the same helper so active-profile
    and per-request resolution share one base-root contract (#749).
    """
    # Explicit override for tests or unusual setups
    base_override = os.getenv('HERMES_BASE_HOME', '').strip()
    if base_override:
        return _unwrap_profile_home_to_base(Path(base_override).expanduser())

    hermes_home = os.getenv('HERMES_HOME', '').strip()
    if hermes_home:
        p = Path(hermes_home).expanduser()
        # If HERMES_HOME points to a profiles/ subdir, walk up two levels to the base
        return _unwrap_profile_home_to_base(p)

    return Path.home() / '.hermes'

_DEFAULT_HERMES_HOME = _resolve_base_hermes_home()


def _read_active_profile_file() -> str:
    """Read the sticky active profile from ~/.hermes/active_profile."""
    ap_file = _DEFAULT_HERMES_HOME / 'active_profile'
    if ap_file.exists():
        try:
            name = ap_file.read_text(encoding="utf-8").strip()
            if name:
                return name
        except Exception:
            logger.debug("Failed to read active profile file")
    return 'default'


# ── Public API ──────────────────────────────────────────────────────────────

# ── Root-profile resolution (#1612) ────────────────────────────────────────
#
# Hermes Agent allows the root/default profile (~/.hermes itself) to have a
# display name other than the legacy literal 'default'.  When that happens,
# WebUI must NOT resolve the display name as ~/.hermes/profiles/<name> — that
# directory doesn't exist, and every site that does `if name == 'default':`
# will fall through to the wrong filesystem path.
#
# `_is_root_profile(name)` answers "does this name resolve to ~/.hermes?" and
# is the canonical replacement for scattered `if name == 'default':` checks
# in switch_profile, get_active_hermes_home, _validate_profile_name, etc.
#
# Cost note: list_profiles_api() shells out via hermes_cli (non-trivial), so
# we memoize the lookup. The cache is invalidated whenever profiles are
# created, deleted, renamed, or cloned — i.e. on every mutation site we
# control.
_root_profile_name_cache: set[str] = {'default'}
_root_profile_name_cache_lock = threading.Lock()
_root_profile_name_cache_loaded = False


def _invalidate_root_profile_cache() -> None:
    """Drop the memoized root-profile-name set.

    Called whenever profile metadata might have changed: create, clone,
    delete, rename. The next _is_root_profile() call repopulates from
    list_profiles_api().
    """
    global _root_profile_name_cache_loaded
    with _root_profile_name_cache_lock:
        _root_profile_name_cache.clear()
        _root_profile_name_cache.add('default')
        _root_profile_name_cache_loaded = False


def _is_root_profile(name: str) -> bool:
    """True if *name* resolves to the Hermes Agent root profile (~/.hermes).

    Matches the legacy 'default' alias plus any name where list_profiles_api()
    reports is_default=True. Memoized; call _invalidate_root_profile_cache()
    after mutating profile metadata.
    """
    global _root_profile_name_cache_loaded
    if not name:
        return False
    if name == 'default':
        return True
    with _root_profile_name_cache_lock:
        if _root_profile_name_cache_loaded:
            return name in _root_profile_name_cache
    # Cache miss — populate from list_profiles_api(). Done outside the lock to
    # avoid holding it across a hermes_cli subprocess call.
    try:
        infos = list_profiles_api()
    except Exception:
        logger.debug("Failed to list profiles for root-profile lookup", exc_info=True)
        return False
    with _root_profile_name_cache_lock:
        _root_profile_name_cache.clear()
        _root_profile_name_cache.add('default')
        for p in infos:
            try:
                if p.get('is_default') and p.get('name'):
                    _root_profile_name_cache.add(p['name'])
            except (AttributeError, TypeError):
                continue
        _root_profile_name_cache_loaded = True
        return name in _root_profile_name_cache


def _profiles_match(row_profile, active_profile) -> bool:
    """Return True if a session/project row's profile matches the active profile.

    Treats both the literal alias 'default' and any renamed-root display name
    (per _is_root_profile) as equivalent, so legacy rows tagged 'default'
    still surface when the user has renamed the root profile to e.g. 'kinni',
    and vice versa.

    A row with no profile (`None` or empty string) is treated as belonging to
    the root profile — that's the convention used by the legacy backfill at
    api/models.py::all_sessions, and matches the default seen in
    `static/sessions.js` (`S.activeProfile||'default'`).

    Originally lived in api/routes.py; relocated here so both routes.py and
    out-of-process consumers (mcp_server.py) can import the canonical helper
    instead of duplicating the body. See #1614 for the visibility model.
    """
    row = row_profile or 'default'
    active = active_profile or 'default'
    if row == active:
        return True
    # Cross-alias the renamed root.
    if _is_root_profile(row) and _is_root_profile(active):
        return True
    return False


def get_active_profile_name() -> str:
    """Return the currently active profile name.

    Priority:
      1. Thread-local (set per-request from hermes_profile cookie) — issue #798
      2. Process-level default (_active_profile)
    """
    tls_name = getattr(_tls, 'profile', None)
    if tls_name is not None:
        return tls_name
    return _active_profile


def set_request_profile(name: str) -> None:
    """Set the per-request profile context for this thread.

    Called by server.py at the start of each request when a hermes_profile
    cookie is present.  Always paired with clear_request_profile() in a
    finally block so the thread-local is released after the request.
    """
    _tls.profile = name


def clear_request_profile() -> None:
    """Clear the per-request profile context for this thread.

    Called by server.py in the finally block of do_GET / do_POST.
    Safe to call even if set_request_profile() was never called.
    """
    _tls.profile = None


def _resolve_profile_home_for_name(name: str) -> Path:
    """Resolve a logical profile name to its Hermes home path.

    Root/default aliases resolve to _DEFAULT_HERMES_HOME.  Valid named profiles
    resolve to _DEFAULT_HERMES_HOME/profiles/<name> even when the directory has
    not been created yet; the agent layer may create it on first use.  Invalid
    names fall back to the base home so traversal-shaped cookie values cannot
    influence filesystem paths.
    """
    if not name or _is_root_profile(name):
        return _DEFAULT_HERMES_HOME
    if not _PROFILE_ID_RE.fullmatch(name):
        return _DEFAULT_HERMES_HOME
    return _resolve_named_profile_home(name)


def get_active_hermes_home() -> Path:
    """Return the HERMES_HOME path for the currently active profile.

    Uses get_active_profile_name() so per-request TLS context (issue #798)
    is respected, not just the process-level global.
    """
    return _resolve_profile_home_for_name(get_active_profile_name())



# ── Cron-call profile isolation (issue: Scheduled jobs ignored active profile) ─
# `cron.jobs` reads HERMES_HOME from os.environ (process-global) at function-
# call time. That bypasses our per-request thread-local profile, so the
# `/api/crons*` endpoints always returned the process-default profile's jobs.
# This context manager swaps HERMES_HOME (and the cached module-level constants
# in cron.jobs) for the duration of a cron call, serialized by a lock so
# concurrent requests from different profiles don't race on the global env var.
#
# Thread-safety note on os.environ mutation:
# CPython's os.environ assignment is GIL-protected at the bytecode level, but
# multi-step read-modify-write sequences (snapshot prev → assign new → restore
# on exit) are NOT atomic without explicit serialization. The _cron_env_lock
# below makes the entire context-manager body run-to-completion serially, so
# all webui access to HERMES_HOME goes through one thread at a time. Any
# subprocess.Popen() call inside `run_job` inherits the env at fork time,
# which is also under the lock — so child processes always see a consistent
# (own-profile) HERMES_HOME, never a half-swapped state.
_cron_env_lock = threading.Lock()


def _cron_profile_context_depth() -> int:
    return int(getattr(_tls, 'cron_profile_depth', 0) or 0)


def _push_cron_profile_context_depth() -> None:
    _tls.cron_profile_depth = _cron_profile_context_depth() + 1


def _pop_cron_profile_context_depth() -> None:
    depth = _cron_profile_context_depth()
    _tls.cron_profile_depth = max(0, depth - 1)


def _home_for_scheduled_cron_job(job: dict) -> Path:
    """Resolve the profile home an auto-fired scheduler job should execute in.

    Legacy jobs with no profile keep the scheduler's server-default profile.
    Jobs pinned to a named profile execute under that profile's HERMES_HOME, so
    an in-process WebUI scheduler thread does not leak process-global config or
    .env into the agent run. If a profile was deleted after the job was saved,
    fall back to the server default rather than crashing every scheduler tick.
    """
    raw = str((job or {}).get('profile') or '').strip()
    if not raw:
        return get_active_hermes_home()
    if _is_root_profile(raw):
        return _DEFAULT_HERMES_HOME
    if not _PROFILE_ID_RE.fullmatch(raw):
        logger.warning(
            "Cron job %s has invalid profile %r; falling back to server default",
            (job or {}).get('id', '?'), raw,
        )
        return get_active_hermes_home()
    home = _resolve_named_profile_home(raw)
    if not home.is_dir():
        logger.warning(
            "Cron job %s references missing profile %r; falling back to server default",
            (job or {}).get('id', '?'), raw,
        )
        return get_active_hermes_home()
    return home


def install_cron_scheduler_profile_isolation() -> None:
    """Patch cron.scheduler.run_job for WebUI in-process scheduler safety.

    Standard WebUI deployments do not start the scheduler thread in-process, but
    if a future/single-process deployment calls cron.scheduler.tick() from the
    WebUI worker, tick's background job path has no request TLS context. Wrap
    run_job so each auto-fired job's persisted ``profile`` field gets the same
    HERMES_HOME isolation as the manual /api/crons/run path.
    """
    try:
        import cron.scheduler as _cs
    except ImportError:
        logger.debug("install_cron_scheduler_profile_isolation: cron.scheduler unavailable")
        return

    original = getattr(_cs, 'run_job', None)
    if original is None or getattr(original, '_webui_profile_isolated', False):
        return

    def _webui_profile_isolated_run_job(job, *args, **kwargs):
        # Manual WebUI runs already enter cron_profile_context_for_home before
        # calling run_job. Avoid nesting the non-reentrant env lock or changing
        # the explicitly selected manual execution profile.
        if _cron_profile_context_depth() > 0:
            return original(job, *args, **kwargs)
        try:
            with cron_profile_context_for_home(_home_for_scheduled_cron_job(job)):
                return original(job, *args, **kwargs)
        finally:
            publish_session_list_changed("cron_complete")

    _webui_profile_isolated_run_job._webui_profile_isolated = True
    _webui_profile_isolated_run_job._webui_original_run_job = original
    _cs.run_job = _webui_profile_isolated_run_job


class cron_profile_context_for_home:
    """Context manager that pins HERMES_HOME to an explicit profile home path.

    Use this variant from worker threads that don't have TLS context (e.g. the
    background thread started by /api/crons/run). The HTTP-side variant below
    resolves the home via TLS.
    """

    def __init__(self, home: Path):
        self._home = Path(home)

    def __enter__(self):
        _cron_env_lock.acquire()
        _push_cron_profile_context_depth()
        try:
            self._prev_env = os.environ.get('HERMES_HOME')
            os.environ['HERMES_HOME'] = str(self._home)

            # Re-patch cron.jobs module-level constants (see main context manager
            # below for the rationale).
            self._prev_cj = None
            try:
                import cron.jobs as _cj
                self._prev_cj = (_cj.HERMES_DIR, _cj.CRON_DIR, _cj.JOBS_FILE, _cj.OUTPUT_DIR)
                _cj.HERMES_DIR = self._home
                _cj.CRON_DIR = self._home / 'cron'
                _cj.JOBS_FILE = _cj.CRON_DIR / 'jobs.json'
                _cj.OUTPUT_DIR = _cj.CRON_DIR / 'output'
            except (ImportError, AttributeError):
                logger.debug("cron_profile_context_for_home: cron.jobs unavailable")

            # cron.scheduler snapshots _hermes_home at import time and run_job()
            # reads config/.env from that module global. Patch it alongside
            # cron.jobs so manual WebUI runs actually execute under the selected
            # profile, not merely write output metadata there (#617).
            self._prev_cs = None
            try:
                import cron.scheduler as _cs
                self._prev_cs = (
                    getattr(_cs, '_hermes_home', None),
                    getattr(_cs, '_LOCK_DIR', None),
                    getattr(_cs, '_LOCK_FILE', None),
                )
                _cs._hermes_home = self._home
                _cs._LOCK_DIR = self._home / 'cron'
                _cs._LOCK_FILE = _cs._LOCK_DIR / '.tick.lock'
            except (ImportError, AttributeError):
                logger.debug("cron_profile_context_for_home: cron.scheduler unavailable")
        except Exception:
            _pop_cron_profile_context_depth()
            _cron_env_lock.release()
            raise
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if self._prev_env is None:
                os.environ.pop('HERMES_HOME', None)
            else:
                os.environ['HERMES_HOME'] = self._prev_env
            if self._prev_cj is not None:
                try:
                    import cron.jobs as _cj
                    _cj.HERMES_DIR, _cj.CRON_DIR, _cj.JOBS_FILE, _cj.OUTPUT_DIR = self._prev_cj
                except (ImportError, AttributeError):
                    pass
            if getattr(self, '_prev_cs', None) is not None:
                try:
                    import cron.scheduler as _cs
                    _cs._hermes_home, _cs._LOCK_DIR, _cs._LOCK_FILE = self._prev_cs
                except (ImportError, AttributeError):
                    pass
        finally:
            _pop_cron_profile_context_depth()
            _cron_env_lock.release()
        return False


class cron_profile_context:
    """Context manager that pins HERMES_HOME to the TLS-active profile.

    Usage:
        with cron_profile_context():
            from cron.jobs import list_jobs
            jobs = list_jobs(include_disabled=True)

    Serializes cron API calls across profiles (cron API is low-frequency;
    serialization cost is negligible compared to correctness).
    """

    def __enter__(self):
        _cron_env_lock.acquire()
        _push_cron_profile_context_depth()
        try:
            self._prev_env = os.environ.get('HERMES_HOME')
            home = get_active_hermes_home()
            os.environ['HERMES_HOME'] = str(home)

            # Re-patch cron.jobs module-level constants. They are snapshot at
            # import time (line 68-71 of cron/jobs.py) and don't participate in
            # the module's __getattr__ lazy path, so env-var alone is not enough
            # for callers that reference the module constants directly.
            self._prev_cj = None
            try:
                import cron.jobs as _cj
                self._prev_cj = (_cj.HERMES_DIR, _cj.CRON_DIR, _cj.JOBS_FILE, _cj.OUTPUT_DIR)
                _cj.HERMES_DIR = home
                _cj.CRON_DIR = home / 'cron'
                _cj.JOBS_FILE = _cj.CRON_DIR / 'jobs.json'
                _cj.OUTPUT_DIR = _cj.CRON_DIR / 'output'
            except (ImportError, AttributeError):
                logger.debug("cron_profile_context: cron.jobs unavailable; env-var only")

            self._prev_cs = None
            try:
                import cron.scheduler as _cs
                self._prev_cs = (
                    getattr(_cs, '_hermes_home', None),
                    getattr(_cs, '_LOCK_DIR', None),
                    getattr(_cs, '_LOCK_FILE', None),
                )
                _cs._hermes_home = home
                _cs._LOCK_DIR = home / 'cron'
                _cs._LOCK_FILE = _cs._LOCK_DIR / '.tick.lock'
            except (ImportError, AttributeError):
                logger.debug("cron_profile_context: cron.scheduler unavailable; env-var only")
        except Exception:
            _pop_cron_profile_context_depth()
            _cron_env_lock.release()
            raise
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            # Restore env var
            if self._prev_env is None:
                os.environ.pop('HERMES_HOME', None)
            else:
                os.environ['HERMES_HOME'] = self._prev_env

            # Restore cron.jobs module constants
            if self._prev_cj is not None:
                try:
                    import cron.jobs as _cj
                    _cj.HERMES_DIR, _cj.CRON_DIR, _cj.JOBS_FILE, _cj.OUTPUT_DIR = self._prev_cj
                except (ImportError, AttributeError):
                    pass
            if getattr(self, '_prev_cs', None) is not None:
                try:
                    import cron.scheduler as _cs
                    _cs._hermes_home, _cs._LOCK_DIR, _cs._LOCK_FILE = self._prev_cs
                except (ImportError, AttributeError):
                    pass
        finally:
            _pop_cron_profile_context_depth()
            _cron_env_lock.release()
        return False


def get_hermes_home_for_profile(name: str) -> Path:
    """Return the HERMES_HOME Path for *name* without mutating any process state.

    Safe to call from per-request context (streaming, session creation) because
    it reads only the filesystem — it never touches os.environ, module-level
    cached paths, or the process-level _active_profile global.

    Falls back to _DEFAULT_HERMES_HOME (same as 'default') when *name* is None,
    empty, 'default', or does not match the profile-name format (rejects path
    traversal such as '../../etc').
    """
    return _resolve_profile_home_for_name(name)


_TERMINAL_ENV_MAPPINGS = {
    'backend': 'TERMINAL_ENV',
    'env_type': 'TERMINAL_ENV',
    'cwd': 'TERMINAL_CWD',
    'timeout': 'TERMINAL_TIMEOUT',
    'lifetime_seconds': 'TERMINAL_LIFETIME_SECONDS',
    'modal_mode': 'TERMINAL_MODAL_MODE',
    'docker_image': 'TERMINAL_DOCKER_IMAGE',
    'docker_forward_env': 'TERMINAL_DOCKER_FORWARD_ENV',
    'docker_env': 'TERMINAL_DOCKER_ENV',
    'docker_mount_cwd_to_workspace': 'TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE',
    'singularity_image': 'TERMINAL_SINGULARITY_IMAGE',
    'modal_image': 'TERMINAL_MODAL_IMAGE',
    'daytona_image': 'TERMINAL_DAYTONA_IMAGE',
    'container_cpu': 'TERMINAL_CONTAINER_CPU',
    'container_memory': 'TERMINAL_CONTAINER_MEMORY',
    'container_disk': 'TERMINAL_CONTAINER_DISK',
    'container_persistent': 'TERMINAL_CONTAINER_PERSISTENT',
    'docker_volumes': 'TERMINAL_DOCKER_VOLUMES',
    'persistent_shell': 'TERMINAL_PERSISTENT_SHELL',
    'ssh_host': 'TERMINAL_SSH_HOST',
    'ssh_user': 'TERMINAL_SSH_USER',
    'ssh_port': 'TERMINAL_SSH_PORT',
    'ssh_key': 'TERMINAL_SSH_KEY',
    'ssh_persistent': 'TERMINAL_SSH_PERSISTENT',
    'local_persistent': 'TERMINAL_LOCAL_PERSISTENT',
}


def _stringify_env_value(value) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return str(value)


def get_profile_runtime_env(home: Path) -> dict[str, str]:
    """Return env vars needed to run an agent turn for a profile home.

    WebUI profile switching is per-client/cookie scoped, so it intentionally
    does not call ``switch_profile(..., process_wide=True)`` for every browser.
    Agent/tool code still consumes terminal backend settings through
    environment variables (matching ``hermes -p <profile>``), so streaming must
    apply the selected profile's terminal config and ``.env`` for the duration
    of that run.
    """
    home = Path(home).expanduser()
    env: dict[str, str] = {}

    try:
        import yaml as _yaml

        cfg_path = home / 'config.yaml'
        cfg = _yaml.safe_load(cfg_path.read_text(encoding='utf-8')) if cfg_path.exists() else {}
        if not isinstance(cfg, dict):
            cfg = {}
    except Exception:
        cfg = {}

    terminal_cfg = cfg.get('terminal', {}) if isinstance(cfg, dict) else {}
    if isinstance(terminal_cfg, dict):
        for key, env_key in _TERMINAL_ENV_MAPPINGS.items():
            if key in terminal_cfg and terminal_cfg[key] is not None:
                env[env_key] = _stringify_env_value(terminal_cfg[key])

    env_path = home / '.env'
    if env_path.exists():
        try:
            for line in env_path.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k and v:
                        env[k] = v
        except Exception:
            logger.debug("Failed to read runtime env from %s", env_path)

    return env


@contextmanager
def profile_env_for_background_worker(
    session,
    purpose: str = "background worker",
    logger_override: Optional[logging.Logger] = None,
):
    """Temporarily route detached worker config reads through a profile.

    Background WebUI workers run outside the request/streaming thread that
    established the profile-scoped environment.  Workers that read agent config,
    runtime provider settings, or skill paths must temporarily apply the
    session/request profile env or they can fall back to the server-default
    profile. Pass either a session-like object with `.profile` or a profile name.
    """
    log = logger_override or logger
    raw_profile = session if isinstance(session, str) else getattr(session, "profile", "")
    profile = str(raw_profile or "").strip()
    if not profile or profile == "default":
        yield
        return

    try:
        # Lazy imports avoid a module-load cycle: streaming imports this helper.
        from api.config import _clear_thread_env, _set_thread_env, _thread_ctx
        from api.streaming import _ENV_LOCK

        profile_home_path = Path(get_hermes_home_for_profile(profile))
        runtime_env = get_profile_runtime_env(profile_home_path)
    except Exception:
        log.debug(
            "Failed to resolve profile env for %s profile %s; falling back to current env",
            purpose,
            profile,
            exc_info=True,
        )
        yield
        return

    thread_env = dict(runtime_env)
    thread_env["HERMES_HOME"] = str(profile_home_path)
    # Hybrid profile routing: keep the broad runtime env in WebUI's thread-local
    # channel for WebUI helpers, and also mirror it into process env for the
    # worker body because several production Hermes readers still call
    # os.getenv() directly for provider credentials.  Keep the _ENV_LOCK scope
    # narrow: serialize only setup/restore, not the whole worker body.
    skill_home_snapshot = None
    old_runtime_env: dict[str, Optional[str]] = {}
    old_hermes_home = None
    had_hermes_home = False
    previous_thread_env = getattr(_thread_ctx, "env", {}).copy()
    try:
        _set_thread_env(**thread_env)
        with _ENV_LOCK:
            old_runtime_env = {key: os.environ.get(key) for key in runtime_env}
            had_hermes_home = "HERMES_HOME" in os.environ
            old_hermes_home = os.environ.get("HERMES_HOME")
            skill_home_snapshot = snapshot_skill_home_modules()
            os.environ.update(runtime_env)
            os.environ["HERMES_HOME"] = str(profile_home_path)
            try:
                patch_skill_home_modules(profile_home_path)
            except Exception:
                log.debug(
                    "Failed to patch skill modules for %s profile %s",
                    purpose,
                    profile,
                    exc_info=True,
                )
        yield
    finally:
        if previous_thread_env:
            _set_thread_env(**previous_thread_env)
        else:
            _clear_thread_env()
        with _ENV_LOCK:
            for key, old_value in old_runtime_env.items():
                if old_value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old_value
            if had_hermes_home:
                os.environ["HERMES_HOME"] = old_hermes_home or ""
            else:
                os.environ.pop("HERMES_HOME", None)
            if skill_home_snapshot is not None:
                restore_skill_home_modules(skill_home_snapshot)


def _set_hermes_home(home: Path):
    """Set HERMES_HOME env var and monkey-patch cached module-level paths."""
    os.environ['HERMES_HOME'] = str(home)

    patch_skill_home_modules(home)

    # Patch cron/jobs module-level cache
    try:
        import cron.jobs as _cj
        _cj.HERMES_DIR = home
        _cj.CRON_DIR = home / 'cron'
        _cj.JOBS_FILE = _cj.CRON_DIR / 'jobs.json'
        _cj.OUTPUT_DIR = _cj.CRON_DIR / 'output'
    except (ImportError, AttributeError):
        logger.debug("Failed to patch cron.jobs module")

    try:
        import cron.scheduler as _cs
        _cs._hermes_home = home
        _cs._LOCK_DIR = home / 'cron'
        _cs._LOCK_FILE = _cs._LOCK_DIR / '.tick.lock'
    except (ImportError, AttributeError):
        logger.debug("Failed to patch cron.scheduler module")


def _reload_dotenv(home: Path):
    """Load .env from the profile dir into os.environ with profile isolation.

    Clears env vars that were loaded from the previously active profile before
    applying the current profile's .env. This prevents API keys and other
    profile-scoped secrets from leaking across profile switches.
    """
    global _loaded_profile_env_keys

    # Remove keys loaded from the previous profile first.
    for key in list(_loaded_profile_env_keys):
        os.environ.pop(key, None)
    _loaded_profile_env_keys = set()

    env_path = home / '.env'
    if not env_path.exists():
        return
    try:
        loaded_keys: set[str] = set()
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and v:
                    os.environ[k] = v
                    loaded_keys.add(k)
        _loaded_profile_env_keys = loaded_keys
    except Exception:
        _loaded_profile_env_keys = set()
        logger.debug("Failed to reload dotenv from %s", env_path)


def init_profile_state() -> None:
    """Initialize profile state at server startup.

    Reads ~/.hermes/active_profile, sets HERMES_HOME env var, patches
    module-level cached paths.  Called once from config.py after imports.
    """
    global _active_profile
    _active_profile = _read_active_profile_file()
    home = get_active_hermes_home()
    _set_hermes_home(home)
    install_cron_scheduler_profile_isolation()
    _reload_dotenv(home)


def switch_profile(name: str, *, process_wide: bool = True) -> dict:
    """Switch the active profile.

    Validates the profile exists, updates process state, patches module caches,
    reloads .env, and reloads config.yaml.

    Args:
        name: Profile name to switch to.
        process_wide: If True (default), updates the process-global
            _active_profile.  Set to False for per-client switches from the
            WebUI where the profile is managed via cookie + thread-local (#798).

    Returns: {'profiles': [...], 'active': name}
    Raises ValueError if profile doesn't exist or agent is busy.
    """
    global _active_profile

    # Import here to avoid circular import at module load
    from api.config import STREAMS, STREAMS_LOCK, reload_config

    # Process-wide profile switches mutate HERMES_HOME, module-level path caches,
    # os.environ-backed .env keys, and the global config cache. Keep those blocked
    # while any agent stream is active. Per-client WebUI switches are cookie/TLS
    # scoped (process_wide=False) and do not mutate those globals, so users can
    # leave a running session in one profile and start work in another (#1700).
    if process_wide:
        with STREAMS_LOCK:
            if len(STREAMS) > 0:
                raise RuntimeError(
                    'Cannot switch profiles while an agent is running. '
                    'Cancel or wait for it to finish.'
                )

    # Resolve profile directory
    if _is_root_profile(name):
        home = _DEFAULT_HERMES_HOME
    else:
        home = _resolve_named_profile_home(name)
        if not home.is_dir():
            raise ValueError(f"Profile '{name}' does not exist.")

    with _profile_lock:
        if process_wide:
            global _active_profile
            _active_profile = name
            _set_hermes_home(home)
            _reload_dotenv(home)

    if process_wide:
        # Write sticky default for CLI consistency
        try:
            ap_file = _DEFAULT_HERMES_HOME / 'active_profile'
            ap_file.write_text('' if _is_root_profile(name) else name, encoding='utf-8')
        except Exception:
            logger.debug("Failed to write active profile file")

        # Reload config.yaml from the new profile
        reload_config()

    # Return profile-specific defaults so frontend can apply them.
    # For process_wide=False (per-client switch), read the target profile's
    # config.yaml directly from disk rather than from _cfg_cache (process-global),
    # since reload_config() was intentionally skipped.
    if process_wide:
        from api.config import get_config
        cfg = get_config()
    else:
        # Direct disk read — does not touch _cfg_cache
        try:
            import yaml as _yaml
            cfg_path = home / 'config.yaml'
            cfg = _yaml.safe_load(cfg_path.read_text(encoding='utf-8')) if cfg_path.exists() else {}
            if not isinstance(cfg, dict):
                cfg = {}
        except Exception:
            cfg = {}
    model_cfg = cfg.get('model', {})
    try:
        from api.config import get_effective_default_model
        default_model = get_effective_default_model(cfg)
    except Exception:
        default_model = None
    default_model_provider = None
    if isinstance(model_cfg, str):
        default_model = model_cfg.strip() or default_model
    elif isinstance(model_cfg, dict):
        default_model = str(model_cfg.get('default') or '').strip() or default_model
        default_model_provider = model_cfg.get('provider')

    # Read the target profile's workspace directly from *home* rather than via
    # get_last_workspace() which routes through the thread-local/process-global active
    # profile — both of which still point to the OLD profile during process_wide=False
    # switches (the Set-Cookie has been sent but hasn't been processed by a new request
    # yet).  We derive workspace in priority order:
    #   1. {home}/webui_state/last_workspace.txt  (previously chosen workspace for this profile)
    #   2. cfg terminal.cwd / workspace / default_workspace keys
    #   3. Boot-time DEFAULT_WORKSPACE constant
    # Use the module-level ``Path`` (imported at line 17) rather than re-importing
    # it locally — keeps the exception fallback simple and avoids a latent NameError
    # if a future refactor moves the inner imports.
    default_workspace = None
    try:
        from api.config import DEFAULT_WORKSPACE as _DW
        lw_file = home / 'webui_state' / 'last_workspace.txt'
        if lw_file.exists():
            _p = lw_file.read_text(encoding='utf-8').strip()
            if _p:
                _pp = Path(_p).expanduser()
                if _pp.is_dir():
                    default_workspace = str(_pp.resolve())
        if default_workspace is None:
            for _key in ('workspace', 'default_workspace'):
                _v = cfg.get(_key)
                if _v:
                    _pp = Path(str(_v)).expanduser().resolve()
                    if _pp.is_dir():
                        default_workspace = str(_pp)
                        break
        if default_workspace is None:
            _tc = cfg.get('terminal', {})
            if isinstance(_tc, dict):
                _cwd = _tc.get('cwd', '')
                if _cwd and str(_cwd) not in ('.', ''):
                    _pp = Path(str(_cwd)).expanduser().resolve()
                    if _pp.is_dir():
                        default_workspace = str(_pp)
        if default_workspace is None:
            default_workspace = str(_DW)
    except Exception:
        try:
            from api.config import DEFAULT_WORKSPACE as _DW2
            default_workspace = str(_DW2)
        except Exception:
            default_workspace = str(Path.home())

    return {
        'profiles': list_profiles_api(),
        'active': name,
        'default_model': default_model,
        'default_model_provider': str(default_model_provider).strip() if default_model_provider else None,
        'default_workspace': default_workspace,
    }


_MISSING = object()
_PROFILE_SETTINGS_FILE = 'profile_settings.json'
_AVATAR_TYPES = {'emoji', 'url', 'asset', 'image'}
_AVATAR_SHAPES = {'square', 'circle'}
_AVATAR_MODES = {'static', 'reactive'}
_DEFAULT_AVATAR_SHAPE = 'circle'
_DEFAULT_AVATAR_MODE = 'static'
_MAX_AVATAR_VALUE_LEN = 4 * 1024 * 1024
_MAX_EMOJI_AVATAR_LEN = 64
_MAX_REACTIVE_AVATAR_SLOT_BYTES = 5 * 1024 * 1024
_MAX_REACTIVE_AVATAR_PACK_BYTES = 20 * 1024 * 1024
REACTIVE_AVATAR_MULTIPART_MAX_BYTES = _MAX_REACTIVE_AVATAR_PACK_BYTES + 1024 * 1024
_REACTIVE_AVATAR_ASSET_DIR = 'avatar_assets'
_REACTIVE_AVATAR_STATES = ('idle', 'thinking', 'talking', 'working', 'error')
_REACTIVE_AVATAR_FILE_FIELDS = {
    f'slot_{state}': state for state in _REACTIVE_AVATAR_STATES
}
_REACTIVE_AVATAR_FALLBACKS = {
    'idle': ('idle',),
    'thinking': ('thinking', 'idle'),
    'talking': ('talking', 'thinking', 'idle'),
    'working': ('working', 'thinking', 'idle'),
    'error': ('error', 'idle'),
}
_REACTIVE_AVATAR_ASSET_ID_RE = re.compile(r'^[a-z]+-[a-f0-9]{16}$')
_IMAGE_AVATAR_RE = re.compile(r'^data:image/(png|jpeg|jpg|gif|webp);base64,[A-Za-z0-9+/=\s]+$')
_IMAGE_AVATAR_CAPTURE_RE = re.compile(r'^data:(image/(?:png|jpeg|jpg|gif|webp));base64,([A-Za-z0-9+/=\s]+)$')


def _validate_profile_settings_name(name: str) -> str:
    """Validate a profile name for settings reads/writes."""
    if not isinstance(name, str):
        name = str(name or '')
    name = name.strip()
    if not name:
        raise ValueError('name is required')
    if name == 'default':
        return name
    if not _PROFILE_ID_RE.fullmatch(name):
        raise ValueError(
            f"Invalid profile name {name!r}. "
            "Must match [a-z0-9][a-z0-9_-]{0,63}"
        )
    return name


def _require_profile_home_for_settings(name: str) -> tuple[str, Path]:
    """Return a validated profile name and existing profile home path."""
    name = _validate_profile_settings_name(name)
    if _is_root_profile(name):
        home = _DEFAULT_HERMES_HOME
    else:
        home = _resolve_named_profile_home(name)
    if not home.is_dir():
        raise FileNotFoundError(f"Profile '{name}' not found.")
    return name, home


def _load_profile_config_for_settings(profile_home: Path) -> dict:
    config_path = profile_home / 'config.yaml'
    if not config_path.exists():
        return {}
    try:
        import yaml as _yaml
        loaded = _yaml.safe_load(config_path.read_text(encoding='utf-8'))
    except Exception:
        logger.debug("Failed to load profile settings config from %s", config_path, exc_info=True)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _save_profile_config_for_settings(profile_home: Path, config_data: dict) -> None:
    try:
        import yaml as _yaml
    except ImportError as exc:
        raise RuntimeError('PyYAML is required to update profile settings') from exc
    config_path = profile_home / 'config.yaml'
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        _yaml.safe_dump(config_data, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding='utf-8',
    )


# Mirrors api.config.VALID_REASONING_EFFORTS without importing — keeps profile
# settings independent of the active-profile reasoning helpers in api.config.
_VALID_REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
_PROFILE_RESPONSE_MODES = ("", "concise", "technical", "teacher", "kawaii", "hype")
_PROFILE_COMPRESSION_DEFAULTS = {
    'enabled': True,
    'threshold': 0.5,
    'target_ratio': 0.5,
    'protect_last_n': 20,
    'protect_first_n': 0,
}
_PROFILE_AUXILIARY_TASKS = (
    {'task': 'vision', 'label': 'Vision', 'description': 'Image and multimodal interpretation.'},
    {'task': 'web_extract', 'label': 'Web extraction', 'description': 'Extract and summarize web page content.'},
    {'task': 'compression', 'label': 'Compression', 'description': 'Summarize long session context.'},
    {'task': 'session_search', 'label': 'Session search', 'description': 'Search and synthesize prior sessions.'},
    {'task': 'skills_hub', 'label': 'Skills hub', 'description': 'Skill discovery and routing support.'},
    {'task': 'approval', 'label': 'Approval', 'description': 'Policy and approval helper calls.'},
    {'task': 'mcp', 'label': 'MCP', 'description': 'MCP tool routing helper calls.'},
    {'task': 'title_generation', 'label': 'Title generation', 'description': 'Short chat and session titles.'},
    {'task': 'triage_specifier', 'label': 'Triage specifier', 'description': 'Clarify routing and task specification.'},
    {'task': 'curator', 'label': 'Curator', 'description': 'Curated summaries and organization.'},
)
_PROFILE_AUXILIARY_TASK_META = {
    item['task']: item for item in _PROFILE_AUXILIARY_TASKS
}


def _extract_profile_reasoning_effort(config_data: dict) -> str:
    agent_cfg = config_data.get('agent') if isinstance(config_data, dict) else None
    if not isinstance(agent_cfg, dict):
        return ''
    raw = agent_cfg.get('reasoning_effort')
    if raw is None:
        return ''
    return str(raw).strip().lower()


def _merge_profile_reasoning_effort(config_data: dict, effort) -> bool:
    """Apply *effort* into ``agent.reasoning_effort`` on *config_data*.

    Returns True when the config changed. Accepts ``''`` (unset),
    ``'none'`` (explicitly disabled), or any value in
    ``_VALID_REASONING_EFFORTS``. Raises ValueError on unknown values.
    """
    if not isinstance(effort, str):
        raise ValueError('reasoning_effort must be a string')
    raw = effort.strip().lower()
    if raw and raw != 'none' and raw not in _VALID_REASONING_EFFORTS:
        raise ValueError(
            f"Unknown reasoning effort '{effort}'. "
            f"Valid: none, {', '.join(_VALID_REASONING_EFFORTS)}."
        )
    agent_cfg = config_data.get('agent')
    if not isinstance(agent_cfg, dict):
        agent_cfg = {}
        had_agent = False
    else:
        agent_cfg = dict(agent_cfg)
        had_agent = True
    before_effort = agent_cfg.get('reasoning_effort')
    if not raw:
        # Empty string means "remove the override" — use profile default.
        if 'reasoning_effort' in agent_cfg:
            agent_cfg.pop('reasoning_effort', None)
            changed = True
        else:
            changed = False
    else:
        agent_cfg['reasoning_effort'] = raw
        changed = before_effort != raw
    if not changed and had_agent:
        return False
    if agent_cfg:
        config_data['agent'] = agent_cfg
    elif had_agent:
        # Removed the last key from agent; collapse the empty section.
        config_data.pop('agent', None)
    return changed


def _extract_profile_fallback_model(config_data: dict) -> dict:
    fallback_providers = (
        config_data.get('fallback_providers') if isinstance(config_data, dict) else None
    )
    if not isinstance(fallback_providers, list) or not fallback_providers:
        return {}
    first = fallback_providers[0]
    if not isinstance(first, dict):
        return {}
    provider = first.get('provider')
    model = first.get('model')
    provider_s = str(provider).strip() if provider is not None else ''
    model_s = str(model).strip() if model is not None else ''
    if not provider_s or not model_s:
        return {}
    return {'provider': provider_s, 'model': model_s}


def _merge_profile_fallback_model(config_data: dict, value) -> bool:
    before = config_data.get('fallback_providers')
    if value is None or value == {} or value == '':
        if 'fallback_providers' in config_data:
            config_data.pop('fallback_providers', None)
            return True
        return False
    if not isinstance(value, dict):
        raise ValueError('fallback_model must be an object')
    provider = value.get('provider')
    model = value.get('model')
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError('fallback_model.provider is required')
    if not isinstance(model, str) or not model.strip():
        raise ValueError('fallback_model.model is required')
    fallback_providers = [{
        'provider': provider.strip(),
        'model': model.strip(),
    }]
    config_data['fallback_providers'] = fallback_providers
    return before != fallback_providers


def _extract_profile_response_mode(config_data: dict) -> str:
    agent_cfg = config_data.get('agent') if isinstance(config_data, dict) else None
    if not isinstance(agent_cfg, dict):
        return ''
    raw = agent_cfg.get('personality')
    return str(raw).strip().lower() if isinstance(raw, str) else ''


def _merge_profile_response_mode(config_data: dict, value) -> bool:
    if value is None:
        value = ''
    if not isinstance(value, str):
        raise ValueError('response_mode must be a string')
    mode = value.strip().lower()
    if mode not in _PROFILE_RESPONSE_MODES:
        raise ValueError(
            f"Unknown response mode '{value}'. "
            f"Valid: {', '.join(m or 'default' for m in _PROFILE_RESPONSE_MODES)}."
        )
    agent_cfg = config_data.get('agent')
    if not isinstance(agent_cfg, dict):
        agent_cfg = {}
        had_agent = False
    else:
        agent_cfg = dict(agent_cfg)
        had_agent = True
    before = agent_cfg.get('personality')
    if not mode:
        if 'personality' in agent_cfg:
            agent_cfg.pop('personality', None)
            changed = True
        else:
            changed = False
    else:
        agent_cfg['personality'] = mode
        changed = before != mode
    if not changed and had_agent:
        return False
    if agent_cfg:
        config_data['agent'] = agent_cfg
    elif had_agent:
        config_data.pop('agent', None)
    return changed


def _coerce_profile_unit_interval(value, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f'compression.{field} must be a number')
    return max(0.0, min(1.0, float(value)))


def _coerce_profile_nonnegative_int(value, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f'compression.{field} must be an integer')
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'compression.{field} must be an integer') from exc
    if result < 0:
        raise ValueError(f'compression.{field} must be >= 0')
    return result


def _extract_profile_compression(config_data: dict) -> dict:
    compression_cfg = config_data.get('compression') if isinstance(config_data, dict) else None
    if not isinstance(compression_cfg, dict):
        return dict(_PROFILE_COMPRESSION_DEFAULTS)
    result = dict(_PROFILE_COMPRESSION_DEFAULTS)
    for field in ('threshold', 'target_ratio'):
        raw = compression_cfg.get(field)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            result[field] = max(0.0, min(1.0, float(raw)))
    for field in ('protect_last_n', 'protect_first_n'):
        raw = compression_cfg.get(field)
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
            result[field] = raw
    return result


def _merge_profile_compression(config_data: dict, value) -> bool:
    if value is None:
        if 'compression' in config_data:
            config_data.pop('compression', None)
            return True
        return False
    if not isinstance(value, dict):
        raise ValueError('compression must be an object')
    current = config_data.get('compression')
    compression_cfg = dict(current) if isinstance(current, dict) else {}
    before = dict(compression_cfg)
    if 'enabled' in value:
        if not isinstance(value['enabled'], bool):
            raise ValueError('compression.enabled must be a boolean')
    for field in ('threshold', 'target_ratio'):
        if field in value:
            compression_cfg[field] = _coerce_profile_unit_interval(value[field], field)
    for field in ('protect_last_n', 'protect_first_n'):
        if field in value:
            compression_cfg[field] = _coerce_profile_nonnegative_int(value[field], field)
    compression_cfg['enabled'] = True
    if compression_cfg:
        config_data['compression'] = compression_cfg
    elif isinstance(current, dict):
        config_data.pop('compression', None)
    return before != compression_cfg


def _extract_profile_max_turns(config_data: dict) -> int | None:
    agent_cfg = config_data.get('agent') if isinstance(config_data, dict) else None
    if not isinstance(agent_cfg, dict):
        return None
    raw = agent_cfg.get('max_turns')
    if isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if 1 <= value <= 1000 else None


def _merge_profile_max_turns(config_data: dict, value) -> bool:
    agent_cfg = config_data.get('agent')
    if not isinstance(agent_cfg, dict):
        agent_cfg = {}
        had_agent = False
    else:
        agent_cfg = dict(agent_cfg)
        had_agent = True
    before = agent_cfg.get('max_turns')
    if value is None or value == '':
        if 'max_turns' in agent_cfg:
            agent_cfg.pop('max_turns', None)
            changed = True
        else:
            changed = False
    else:
        if isinstance(value, bool):
            raise ValueError('max_turns must be an integer')
        try:
            turns = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError('max_turns must be an integer') from exc
        if turns < 1 or turns > 1000:
            raise ValueError('max_turns must be between 1 and 1000')
        agent_cfg['max_turns'] = turns
        changed = before != turns
    if not changed and had_agent:
        return False
    if agent_cfg:
        config_data['agent'] = agent_cfg
    elif had_agent:
        config_data.pop('agent', None)
    return changed


def _clean_profile_optional_string(value, field: str) -> str:
    if value is None:
        return ''
    if not isinstance(value, str):
        raise ValueError(f'{field} must be a string')
    return value.strip()


def _extract_profile_auxiliary_models(config_data: dict) -> list[dict]:
    auxiliary_cfg = config_data.get('auxiliary') if isinstance(config_data, dict) else None
    if not isinstance(auxiliary_cfg, dict):
        auxiliary_cfg = {}
    result = []
    for meta in _PROFILE_AUXILIARY_TASKS:
        task = meta['task']
        task_cfg = auxiliary_cfg.get(task)
        if not isinstance(task_cfg, dict):
            task_cfg = {}
        provider = task_cfg.get('provider')
        model = task_cfg.get('model')
        result.append({
            'task': task,
            'label': meta['label'],
            'description': meta['description'],
            'provider': provider.strip() if isinstance(provider, str) else '',
            'model': model.strip() if isinstance(model, str) else '',
        })
    return result


def _normalize_profile_auxiliary_model_updates(value) -> list[dict]:
    if isinstance(value, dict):
        raw_items = []
        for task, task_value in value.items():
            if task_value is None or task_value == '':
                raw_items.append({'task': task, 'provider': '', 'model': ''})
            elif isinstance(task_value, dict):
                item = dict(task_value)
                item['task'] = task
                raw_items.append(item)
            else:
                raise ValueError('auxiliary_models entries must be objects')
    elif isinstance(value, list):
        raw_items = value
    else:
        raise ValueError('auxiliary_models must be a list or object')

    normalized = []
    seen = set()
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise ValueError('auxiliary_models entries must be objects')
        task = _clean_profile_optional_string(raw_item.get('task'), 'auxiliary_models.task')
        if task not in _PROFILE_AUXILIARY_TASK_META:
            raise ValueError(f"Unknown auxiliary model task '{task}'")
        provider = raw_item.get('provider', _MISSING)
        model = raw_item.get('model', _MISSING)
        if provider is not _MISSING:
            provider = _clean_profile_optional_string(provider, f'auxiliary.{task}.provider')
        if model is not _MISSING:
            model = _clean_profile_optional_string(model, f'auxiliary.{task}.model')
        normalized_item = {'task': task}
        if provider is not _MISSING:
            normalized_item['provider'] = provider
        if model is not _MISSING:
            normalized_item['model'] = model
        if 'provider' not in normalized_item and 'model' not in normalized_item:
            continue
        if task in seen:
            normalized = [item for item in normalized if item['task'] != task]
        seen.add(task)
        normalized.append(normalized_item)
    return normalized


def _merge_profile_auxiliary_models(config_data: dict, value) -> bool:
    updates = _normalize_profile_auxiliary_model_updates(value)
    current = config_data.get('auxiliary')
    auxiliary_cfg = copy.deepcopy(current) if isinstance(current, dict) else {}
    before = copy.deepcopy(auxiliary_cfg)
    for update in updates:
        task = update['task']
        task_cfg = auxiliary_cfg.get(task)
        task_cfg = dict(task_cfg) if isinstance(task_cfg, dict) else {}
        provider = update.get('provider', _MISSING)
        model = update.get('model', _MISSING)
        should_clear = (
            (provider is not _MISSING and provider == '')
            or (model is not _MISSING and model == '')
        )
        if should_clear:
            task_cfg.pop('provider', None)
            task_cfg.pop('model', None)
        else:
            if provider is not _MISSING:
                task_cfg['provider'] = provider
            if model is not _MISSING:
                task_cfg['model'] = model
        if task_cfg:
            auxiliary_cfg[task] = task_cfg
        else:
            auxiliary_cfg.pop(task, None)

    if auxiliary_cfg:
        config_data['auxiliary'] = auxiliary_cfg
    elif isinstance(current, dict):
        config_data.pop('auxiliary', None)
    return before != auxiliary_cfg


def _extract_profile_toolsets(config_data: dict) -> list[str]:
    platform_cfg = config_data.get('platform_toolsets') if isinstance(config_data, dict) else None
    cli_toolsets = platform_cfg.get('cli') if isinstance(platform_cfg, dict) else None
    if not isinstance(cli_toolsets, list):
        return []
    result = []
    seen = set()
    for item in cli_toolsets:
        if not isinstance(item, str):
            continue
        toolset = item.strip()
        if toolset and toolset not in seen:
            seen.add(toolset)
            result.append(toolset)
    return result


def _profile_toolsets_configured(config_data: dict) -> bool:
    platform_cfg = config_data.get('platform_toolsets') if isinstance(config_data, dict) else None
    return isinstance(platform_cfg, dict) and isinstance(platform_cfg.get('cli'), list)


def _normalize_profile_toolsets(value) -> list[str]:
    if not isinstance(value, list):
        raise ValueError('toolsets must be a list')
    result = []
    seen = set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError('toolsets entries must be strings')
        toolset = item.strip()
        if toolset and toolset not in seen:
            seen.add(toolset)
            result.append(toolset)
    return result


def _merge_profile_toolsets(config_data: dict, value) -> bool:
    toolsets = _normalize_profile_toolsets(value)
    current = config_data.get('platform_toolsets')
    platform_cfg = dict(current) if isinstance(current, dict) else {}
    before = list(platform_cfg.get('cli')) if isinstance(platform_cfg.get('cli'), list) else None
    platform_cfg['cli'] = toolsets
    config_data['platform_toolsets'] = platform_cfg
    return before != toolsets


def _extract_profile_default_workspace(config_data: dict) -> str:
    if not isinstance(config_data, dict):
        return ''
    workspace = config_data.get('workspace')
    if isinstance(workspace, str) and workspace.strip():
        return workspace.strip()
    default_workspace = config_data.get('default_workspace')
    if isinstance(default_workspace, str) and default_workspace.strip():
        return default_workspace.strip()
    return ''


def _merge_profile_default_workspace(config_data: dict, value) -> bool:
    if value is None:
        workspace = ''
    elif isinstance(value, str):
        workspace = value.strip()
    else:
        raise ValueError('default_workspace must be a string')

    if not workspace:
        changed = 'workspace' in config_data or 'default_workspace' in config_data
        config_data.pop('workspace', None)
        config_data.pop('default_workspace', None)
        return changed

    key = 'workspace'
    if 'workspace' not in config_data and 'default_workspace' in config_data:
        key = 'default_workspace'
    before = config_data.get(key)
    config_data[key] = workspace
    return before != workspace


def _extract_profile_model_settings(config_data: dict) -> tuple[str | None, str | None]:
    model_cfg = config_data.get('model') if isinstance(config_data, dict) else None
    if isinstance(model_cfg, dict):
        model = model_cfg.get('default') or model_cfg.get('model') or model_cfg.get('name')
        provider = model_cfg.get('provider')
    elif isinstance(model_cfg, str):
        model = model_cfg
        provider = None
    else:
        model = None
        provider = None
    model_s = str(model).strip() if model is not None else None
    provider_s = str(provider).strip() if provider is not None else None
    return (model_s or None, provider_s or None)


def _normalize_model_provider_inputs(provider, model) -> tuple[str | object, str | object]:
    normalized_provider = provider
    normalized_model = model
    if model is not _MISSING:
        if not isinstance(model, str):
            raise ValueError('model must be a string')
        selected = model.strip()
        if not selected:
            raise ValueError('model is required')
        if selected.startswith('@') and ':' in selected:
            provider_hint, bare_model = selected[1:].split(':', 1)
            provider_hint = provider_hint.strip()
            bare_model = bare_model.strip()
            if not bare_model:
                raise ValueError('model is required')
            selected = bare_model
            if provider is _MISSING and provider_hint:
                normalized_provider = provider_hint
        normalized_model = selected
    if normalized_provider is not _MISSING:
        if normalized_provider is None:
            pass
        elif not isinstance(normalized_provider, str):
            raise ValueError('provider must be a string')
        else:
            normalized_provider = normalized_provider.strip()
            if not normalized_provider:
                raise ValueError('provider is required')
    return normalized_provider, normalized_model


def _merge_profile_model_settings(config_data: dict, provider, model) -> bool:
    provider, model = _normalize_model_provider_inputs(provider, model)
    current = config_data.get('model')
    if isinstance(current, dict):
        model_cfg = dict(current)
    elif isinstance(current, str) and current.strip():
        model_cfg = {'default': current.strip()}
    else:
        model_cfg = {}

    before = dict(model_cfg)
    if model is not _MISSING:
        model_cfg['default'] = model
    if provider is not _MISSING:
        if provider is None:
            model_cfg.pop('provider', None)
        else:
            model_cfg['provider'] = provider
    config_data['model'] = model_cfg
    return before != model_cfg


def _profile_settings_state_path(profile_home: Path) -> Path:
    return profile_home / 'webui_state' / _PROFILE_SETTINGS_FILE


def _read_profile_settings_state(profile_home: Path) -> dict:
    path = _profile_settings_state_path(profile_home)
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        logger.debug("Failed to load WebUI profile settings from %s", path, exc_info=True)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_profile_settings_state(profile_home: Path, state: dict) -> None:
    path = _profile_settings_state_path(profile_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding='utf-8',
    )
    tmp.replace(path)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _read_profile_avatar_for_home(profile_home: Path):
    state = _read_profile_settings_state(profile_home)
    avatar = state.get('avatar')
    return avatar if isinstance(avatar, dict) else None


def _normalize_avatar_shape_payload(shape):
    if shape is None:
        return _DEFAULT_AVATAR_SHAPE
    if not isinstance(shape, str):
        raise ValueError('avatar_shape must be a string')
    normalized = shape.strip().lower()
    if not normalized:
        return _DEFAULT_AVATAR_SHAPE
    if normalized not in _AVATAR_SHAPES:
        raise ValueError('avatar_shape must be square or circle')
    return normalized


def _read_profile_avatar_shape_for_home(profile_home: Path):
    state = _read_profile_settings_state(profile_home)
    try:
        return _normalize_avatar_shape_payload(state.get('avatar_shape'))
    except ValueError:
        return _DEFAULT_AVATAR_SHAPE


def _normalize_avatar_mode_payload(mode):
    if mode is None:
        return _DEFAULT_AVATAR_MODE
    if not isinstance(mode, str):
        raise ValueError('avatar_mode must be a string')
    normalized = mode.strip().lower()
    if not normalized:
        return _DEFAULT_AVATAR_MODE
    if normalized not in _AVATAR_MODES:
        raise ValueError('avatar_mode must be static or reactive')
    return normalized


def _read_profile_avatar_mode_for_home(profile_home: Path) -> str:
    state = _read_profile_settings_state(profile_home)
    try:
        return _normalize_avatar_mode_payload(state.get('avatar_mode'))
    except ValueError:
        return _DEFAULT_AVATAR_MODE


def _reactive_avatar_asset_root(profile_home: Path) -> Path:
    return profile_home / 'webui_state' / _REACTIVE_AVATAR_ASSET_DIR


def _avatar_content_ext(content_type: str) -> str:
    if content_type == 'image/jpeg':
        return 'jpg'
    if content_type == 'image/png':
        return 'png'
    if content_type == 'image/gif':
        return 'gif'
    if content_type == 'image/webp':
        return 'webp'
    raise ValueError('unsupported avatar image')


def _detect_reactive_avatar_upload(filename: str, payload: bytes) -> dict:
    if not isinstance(payload, (bytes, bytearray)):
        raise ValueError('avatar upload must be bytes')
    payload = bytes(payload)
    if not payload:
        raise ValueError('avatar upload is empty')
    if len(payload) > _MAX_REACTIVE_AVATAR_SLOT_BYTES:
        raise ValueError('avatar upload is too large')

    content_type = None
    animated = False
    if payload.startswith(b'GIF87a') or payload.startswith(b'GIF89a'):
        content_type = 'image/gif'
        animated = payload.count(b'\x2c') > 1
    elif payload.startswith(b'\x89PNG\r\n\x1a\n'):
        content_type = 'image/png'
    elif payload.startswith(b'\xff\xd8'):
        content_type = 'image/jpeg'
    elif len(payload) >= 12 and payload[:4] == b'RIFF' and payload[8:12] == b'WEBP':
        content_type = 'image/webp'
        animated = b'ANIM' in payload[12:] or b'ANMF' in payload[12:]

    if not content_type:
        raise ValueError('unsupported avatar image')

    digest = hashlib.sha256(payload).hexdigest()
    raw_name = Path(str(filename or '')).name[:200]
    return {
        'filename': raw_name or f'avatar.{_avatar_content_ext(content_type)}',
        'content_type': content_type,
        'ext': _avatar_content_ext(content_type),
        'size': len(payload),
        'sha256': digest,
        'animated': bool(animated),
    }


def _normalize_reactive_avatar_pack(raw) -> dict:
    slots = {}
    updated_at = None
    if isinstance(raw, dict):
        updated_at = raw.get('updated_at') if isinstance(raw.get('updated_at'), str) else None
        raw_slots = raw.get('slots')
        if isinstance(raw_slots, dict):
            for state in _REACTIVE_AVATAR_STATES:
                meta = raw_slots.get(state)
                if not isinstance(meta, dict):
                    continue
                asset_id = str(meta.get('asset_id') or '').strip()
                content_type = str(meta.get('content_type') or '').strip().lower()
                if not _REACTIVE_AVATAR_ASSET_ID_RE.fullmatch(asset_id):
                    continue
                try:
                    ext = str(meta.get('ext') or _avatar_content_ext(content_type)).strip().lower()
                    if ext != _avatar_content_ext(content_type):
                        continue
                except ValueError:
                    continue
                sha256 = str(meta.get('sha256') or '').strip().lower()
                if not re.fullmatch(r'[a-f0-9]{64}', sha256):
                    continue
                try:
                    size = int(meta.get('size') or 0)
                except (TypeError, ValueError):
                    size = 0
                if size <= 0:
                    continue
                filename = Path(str(meta.get('filename') or f'{state}.{ext}')).name[:200]
                slots[state] = {
                    'state': state,
                    'asset_id': asset_id,
                    'filename': filename or f'{state}.{ext}',
                    'content_type': content_type,
                    'ext': ext,
                    'size': size,
                    'sha256': sha256,
                    'animated': bool(meta.get('animated')),
                }
    return {
        'version': 1,
        'updated_at': updated_at,
        'slots': slots,
    }


def _reactive_avatar_slot_url(profile_name: str, meta: dict) -> str:
    digest = str(meta.get('sha256') or '')[:16]
    asset_id = str(meta.get('asset_id') or '')
    return (
        f'api/profile/avatar-asset?name={quote(profile_name, safe="")}'
        f'&asset={quote(asset_id, safe="")}&v={quote(digest, safe="")}'
    )


def _reactive_avatar_pack_for_response(profile_name: str, raw) -> dict:
    pack = _normalize_reactive_avatar_pack(raw)
    slots = {}
    for state, meta in pack['slots'].items():
        response_meta = dict(meta)
        response_meta['url'] = _reactive_avatar_slot_url(profile_name, meta)
        slots[state] = response_meta
    return {
        'version': pack['version'],
        'updated_at': pack['updated_at'],
        'slots': slots,
    }


def _effective_reactive_avatar(profile_name: str, avatar, raw_pack) -> dict:
    pack = _normalize_reactive_avatar_pack(raw_pack)
    effective = {}
    for state in _REACTIVE_AVATAR_STATES:
        chosen_state = None
        chosen_meta = None
        for candidate in _REACTIVE_AVATAR_FALLBACKS[state]:
            meta = pack['slots'].get(candidate)
            if meta:
                chosen_state = candidate
                chosen_meta = meta
                break
        if chosen_meta is not None:
            effective[state] = {
                'type': 'reactive',
                'state': chosen_state,
                'avatar': {
                    'type': 'asset',
                    'value': _reactive_avatar_slot_url(profile_name, chosen_meta),
                    'content_type': chosen_meta.get('content_type'),
                    'animated': bool(chosen_meta.get('animated')),
                },
            }
        else:
            effective[state] = {
                'type': 'static',
                'state': 'static',
                'avatar': avatar if isinstance(avatar, dict) else None,
            }
    return effective


def _reactive_avatar_asset_path(profile_home: Path, meta: dict) -> Path:
    asset_id = str(meta.get('asset_id') or '')
    if not _REACTIVE_AVATAR_ASSET_ID_RE.fullmatch(asset_id):
        raise FileNotFoundError('Avatar asset not found.')
    return _reactive_avatar_asset_root(profile_home) / f'{asset_id}.{meta.get("ext")}'


def _truthy_avatar_field(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def _avatar_payload_from_field(value):
    if value is _MISSING:
        return _MISSING
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or stripped.lower() == 'null':
            return None
        try:
            loaded = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError('avatar must be a JSON object') from exc
        if loaded is None or isinstance(loaded, dict):
            return loaded
    raise ValueError('avatar must be an object')


def _clear_reactive_slot_names(payload: dict) -> set[str]:
    states = set()
    raw = payload.get('clear_slots') if isinstance(payload, dict) else None
    if isinstance(raw, str):
        candidates = re.split(r'[\s,]+', raw)
    elif isinstance(raw, (list, tuple, set)):
        candidates = raw
    else:
        candidates = ()
    for candidate in candidates:
        state = str(candidate or '').strip().lower()
        if state:
            if state not in _REACTIVE_AVATAR_STATES:
                raise ValueError('clear_slots contains an unknown avatar state')
            states.add(state)
    for state in _REACTIVE_AVATAR_STATES:
        if _truthy_avatar_field(payload.get(f'clear_slot_{state}')):
            states.add(state)
    return states


def _profile_avatar_for_summary(name: str, profile_home: Path):
    """Return avatar metadata suitable for profile list/dropdown payloads.

    Uploaded avatars are persisted as data URLs and can be several MB. Summary
    responses only need a renderable reference, so expose those images through
    an authenticated lazy image route instead of embedding the full data URL in
    every `/api/profiles` response.
    """
    avatar = _read_profile_avatar_for_home(profile_home)
    if not isinstance(avatar, dict):
        return None
    avatar_type = str(avatar.get('type') or '').strip().lower()
    value = avatar.get('value')
    if avatar_type != 'image' or not isinstance(value, str) or not value:
        return avatar
    digest = hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]
    return {
        'type': 'asset',
        'value': f'api/profile/avatar-image?name={quote(name, safe="")}&v={digest}',
    }


def read_profile_avatar_image_api(name: str) -> tuple[bytes, str, str]:
    """Return decoded uploaded avatar image bytes for *name*.

    Returns ``(payload, content_type, etag)``. Only uploaded data-image avatars
    are served here; emoji, URL, and asset avatars remain inline metadata.
    """
    name, profile_home = _require_profile_home_for_settings(name)
    avatar = _read_profile_avatar_for_home(profile_home)
    if not isinstance(avatar, dict) or str(avatar.get('type') or '').strip().lower() != 'image':
        raise FileNotFoundError(f"Profile '{name}' has no uploaded avatar image.")
    value = avatar.get('value')
    if not isinstance(value, str):
        raise FileNotFoundError(f"Profile '{name}' has no uploaded avatar image.")
    match = _IMAGE_AVATAR_CAPTURE_RE.fullmatch(value)
    if not match:
        raise ValueError('Stored profile avatar image is invalid.')
    content_type = match.group(1)
    raw_b64 = ''.join(match.group(2).split())
    payload = base64.b64decode(raw_b64, validate=True)
    etag = hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]
    return payload, content_type, etag


def get_profile_avatar_summary_api(name: str):
    """Return lightweight avatar metadata for topbar/list style surfaces."""
    name, profile_home = _require_profile_home_for_settings(name)
    return _profile_avatar_for_summary(name, profile_home)


def get_profile_avatar_settings_api(name: str) -> dict:
    """Return static and reactive avatar settings for one profile."""
    name, profile_home = _require_profile_home_for_settings(name)
    state = _read_profile_settings_state(profile_home)
    avatar = state.get('avatar') if isinstance(state.get('avatar'), dict) else None
    pack = state.get('reactive_avatar')
    return {
        'name': name,
        'avatar': avatar,
        'avatar_shape': _read_profile_avatar_shape_for_home(profile_home),
        'avatar_mode': _read_profile_avatar_mode_for_home(profile_home),
        'reactive_avatar': _reactive_avatar_pack_for_response(name, pack),
        'effective_reactive_avatar': _effective_reactive_avatar(name, avatar, pack),
    }


def read_profile_avatar_asset_api(name: str, asset_id: str) -> tuple[bytes, str, str]:
    """Return a file-backed reactive avatar asset as ``(bytes, content_type, etag)``."""
    name, profile_home = _require_profile_home_for_settings(name)
    asset_id = str(asset_id or '').strip()
    if not _REACTIVE_AVATAR_ASSET_ID_RE.fullmatch(asset_id):
        raise FileNotFoundError('Avatar asset not found.')
    state = _read_profile_settings_state(profile_home)
    pack = _normalize_reactive_avatar_pack(state.get('reactive_avatar'))
    for meta in pack['slots'].values():
        if meta.get('asset_id') != asset_id:
            continue
        path = _reactive_avatar_asset_path(profile_home, meta)
        if not path.is_file():
            raise FileNotFoundError('Avatar asset not found.')
        return path.read_bytes(), meta['content_type'], str(meta['sha256'])[:16]
    raise FileNotFoundError('Avatar asset not found.')


def _normalize_avatar_payload(avatar):
    if avatar is None:
        return None
    if not isinstance(avatar, dict):
        raise ValueError('avatar must be an object')
    avatar_type = str(avatar.get('type') or '').strip().lower()
    value = avatar.get('value')
    if avatar_type not in _AVATAR_TYPES:
        raise ValueError('avatar type must be emoji, url, asset, or image')
    if not isinstance(value, str):
        raise ValueError('avatar value must be a string')
    value = value.strip()
    if not value:
        raise ValueError('avatar value is required')
    if len(value) > _MAX_AVATAR_VALUE_LEN:
        raise ValueError('avatar value is too large')
    if avatar_type == 'emoji' and len(value) > _MAX_EMOJI_AVATAR_LEN:
        raise ValueError('emoji avatar value is too large')
    if avatar_type == 'url':
        from urllib.parse import urlparse
        parsed = urlparse(value)
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
            raise ValueError('avatar URL must start with http:// or https://')
    if avatar_type == 'image':
        compact_value = ''.join(value.split())
        if not _IMAGE_AVATAR_RE.fullmatch(compact_value):
            raise ValueError('uploaded avatar must be a PNG, JPEG, GIF, or WebP data image')
        value = compact_value
    if avatar_type == 'asset':
        if value.startswith(('/', '\\')) or '..' in value.split('/') or '\\' in value or ':' in value:
            raise ValueError('avatar asset must be a safe relative asset path')
    return {'type': avatar_type, 'value': value}


def _remove_reactive_avatar_asset(profile_home: Path, meta: dict) -> None:
    try:
        path = _reactive_avatar_asset_path(profile_home, meta)
        path.unlink(missing_ok=True)
    except Exception:
        logger.debug("Failed to remove reactive avatar asset", exc_info=True)


def update_profile_avatar_settings_api(payload: dict, files: dict | None = None) -> dict:
    """Update static/reactive avatar mode settings and uploaded reactive slots."""
    if not isinstance(payload, dict):
        raise ValueError('payload must be an object')
    files = files or {}
    name = str(payload.get('name') or '').strip()
    if not name:
        raise ValueError('name is required')
    name, profile_home = _require_profile_home_for_settings(name)

    state = _read_profile_settings_state(profile_home)
    current_pack = _normalize_reactive_avatar_pack(state.get('reactive_avatar'))
    current_slots = dict(current_pack['slots'])
    current_asset_ids = {
        str(meta.get('asset_id') or '')
        for meta in current_slots.values()
        if isinstance(meta, dict)
    }

    avatar_value = _avatar_payload_from_field(payload.get('avatar', _MISSING))
    if avatar_value is not _MISSING:
        normalized_avatar = _normalize_avatar_payload(avatar_value)
        if normalized_avatar is None:
            state.pop('avatar', None)
        else:
            state['avatar'] = normalized_avatar

    if 'avatar_shape' in payload:
        state['avatar_shape'] = _normalize_avatar_shape_payload(payload.get('avatar_shape'))
    if 'avatar_mode' in payload:
        state['avatar_mode'] = _normalize_avatar_mode_payload(payload.get('avatar_mode'))

    clear_pack = _truthy_avatar_field(payload.get('clear_reactive_avatar'))
    clear_slots = set(_REACTIVE_AVATAR_STATES) if clear_pack else _clear_reactive_slot_names(payload)

    incoming = {}
    for field, upload in files.items():
        slot = _REACTIVE_AVATAR_FILE_FIELDS.get(str(field))
        if not slot:
            continue
        if not isinstance(upload, (list, tuple)) or len(upload) < 2:
            raise ValueError('avatar upload is invalid')
        filename, body = upload[0], upload[1]
        meta = _detect_reactive_avatar_upload(filename, body)
        meta['state'] = slot
        meta['asset_id'] = f'{slot}-{meta["sha256"][:16]}'
        incoming[slot] = (meta, bytes(body))
        clear_slots.discard(slot)

    next_slots = {
        slot: meta for slot, meta in current_slots.items()
        if slot not in clear_slots and slot not in incoming
    }
    total_size = sum(int(meta.get('size') or 0) for meta in next_slots.values())
    total_size += sum(meta['size'] for meta, _body in incoming.values())
    if total_size > _MAX_REACTIVE_AVATAR_PACK_BYTES:
        raise ValueError('reactive avatar uploads are too large')

    asset_root = _reactive_avatar_asset_root(profile_home)
    written = []
    try:
        for slot, (meta, body) in incoming.items():
            path = asset_root / f'{meta["asset_id"]}.{meta["ext"]}'
            asset_root.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + '.tmp')
            tmp.write_bytes(body)
            tmp.replace(path)
            written.append(meta)
            next_slots[slot] = meta

        if clear_pack or clear_slots or incoming or 'reactive_avatar' in state:
            state['reactive_avatar'] = {
                'version': 1,
                'updated_at': _utc_now_iso(),
                'slots': next_slots,
            }
        _write_profile_settings_state(profile_home, state)
    except Exception:
        for meta in written:
            if str(meta.get('asset_id') or '') not in current_asset_ids:
                _remove_reactive_avatar_asset(profile_home, meta)
        raise

    referenced = {
        str(meta.get('asset_id') or '')
        for meta in next_slots.values()
        if isinstance(meta, dict)
    }
    for slot, meta in current_slots.items():
        if slot in clear_slots or slot in incoming:
            if str(meta.get('asset_id') or '') not in referenced:
                _remove_reactive_avatar_asset(profile_home, meta)

    return get_profile_avatar_settings_api(name)


def get_profile_settings_api(name: str, *, include_avatar: bool = True) -> dict:
    """Return structured WebUI settings for a profile."""
    name, profile_home = _require_profile_home_for_settings(name)
    config_data = _load_profile_config_for_settings(profile_home)
    model, provider = _extract_profile_model_settings(config_data)
    state = _read_profile_settings_state(profile_home)
    avatar = _read_profile_avatar_for_home(profile_home) if include_avatar else None
    effective_avatar = avatar if include_avatar else _profile_avatar_for_summary(name, profile_home)
    return {
        'name': name,
        'provider': provider,
        'model': model,
        'avatar': avatar,
        'avatar_shape': _read_profile_avatar_shape_for_home(profile_home),
        'avatar_mode': _read_profile_avatar_mode_for_home(profile_home),
        'reactive_avatar': _reactive_avatar_pack_for_response(name, state.get('reactive_avatar')),
        'effective_reactive_avatar': _effective_reactive_avatar(name, effective_avatar, state.get('reactive_avatar')),
        'reasoning_effort': _extract_profile_reasoning_effort(config_data),
        'fallback_model': _extract_profile_fallback_model(config_data),
        'response_mode': _extract_profile_response_mode(config_data),
        'compression': _extract_profile_compression(config_data),
        'max_turns': _extract_profile_max_turns(config_data),
        'auxiliary_models': _extract_profile_auxiliary_models(config_data),
        'toolsets': _extract_profile_toolsets(config_data),
        'toolsets_configured': _profile_toolsets_configured(config_data),
        'default_workspace': _extract_profile_default_workspace(config_data),
        'description': _extract_profile_description(config_data),
    }


# ── Profile description (profile screen rework v3.1 — 2026-05-15) ─────────
#
# The hero dossier on the profile detail screen shows a short user-authored
# description distinct from SOUL.md (which carries the agent's persona /
# voice for the model). Persisted at `webui.description` inside the profile's
# config.yaml, capped at _PROFILE_DESCRIPTION_MAX chars to keep the dossier
# from turning into an essay. The persona endpoint returns it; the existing
# /api/profile/settings POST writes it.

_PROFILE_DESCRIPTION_MAX = 280


def _extract_profile_description(config_data: dict) -> str:
    """Return the user-set description string from a loaded config blob.

    Reads ``webui.description``. Missing/non-string values become ``''``.
    """
    if not isinstance(config_data, dict):
        return ''
    webui_cfg = config_data.get('webui')
    if not isinstance(webui_cfg, dict):
        return ''
    raw = webui_cfg.get('description')
    return str(raw).strip() if isinstance(raw, str) else ''


def _merge_profile_description(config_data: dict, description) -> bool:
    """Apply *description* into ``webui.description`` on *config_data*.

    Empty / None removes the override and collapses the ``webui`` section if
    it becomes empty. Returns True when the config changed. Raises
    ValueError on non-string inputs or strings longer than the hard cap.
    """
    if description is None:
        description = ''
    if not isinstance(description, str):
        raise ValueError('description must be a string')
    new_value = description.strip()
    if len(new_value) > _PROFILE_DESCRIPTION_MAX:
        raise ValueError(
            f"description must be <= {_PROFILE_DESCRIPTION_MAX} characters"
        )
    webui_cfg = config_data.get('webui')
    if not isinstance(webui_cfg, dict):
        webui_cfg = {}
        had_webui = False
    else:
        webui_cfg = dict(webui_cfg)
        had_webui = True
    before = webui_cfg.get('description')
    if not new_value:
        if 'description' in webui_cfg:
            webui_cfg.pop('description', None)
            changed = True
        else:
            changed = False
    else:
        webui_cfg['description'] = new_value
        changed = before != new_value
    if not changed and had_webui:
        return False
    if webui_cfg:
        config_data['webui'] = webui_cfg
    elif had_webui:
        config_data.pop('webui', None)
    return changed


# Legacy helper retained for the file-read flow / tests that exercise SOUL
# parsing; the persona endpoint no longer routes through it.
def _first_non_blank_paragraph(text: str) -> str:
    """Return the first non-blank, non-heading-only paragraph from a markdown blob.

    Heading marks ('#') are stripped, but a paragraph that contains *only*
    headings is skipped in favour of a paragraph that has at least one body
    line. Joined lines are space-separated so a wrapped voice quote survives.
    """
    for raw in text.split('\n\n'):
        para = raw.strip()
        if not para:
            continue
        kept = []
        for line in para.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            is_heading = stripped.startswith('#')
            text_line = stripped.lstrip('#').strip() if is_heading else stripped
            kept.append((text_line, is_heading))
        if not kept:
            continue
        if all(h for _, h in kept):
            # Whole paragraph is headings — look at the next paragraph instead.
            continue
        body = ' '.join(t for t, h in kept if t and not h) or ' '.join(t for t, _ in kept if t)
        body = body.strip()
        if body:
            return body
    return ''


# ── Activity aggregator (profile screen rework 2026-05-14) ─────────────────
#
# The activity line on the reworked profile screen reports last-used,
# sessions-this-week, optional spend, and the gateway's last-run timestamp.
# Sessions live in a single global WebUI index keyed by profile name, so the
# aggregation is a pure data filter; the gateway timestamp lives inside the
# profile's HOME at .gateway-state.json, written by the gateway control path
# on successful start (Task 6 below).

_ACTIVITY_WINDOW_DAYS = 7


def _compute_profile_activity(rows, name: str, *, now: float) -> dict:
    """Aggregate session-index *rows* for profile *name*.

    ``sessions_week`` is the count of profile-tagged sessions within the
    last 7 days. ``last_used_at`` is the most-recent ``updated_at`` for
    this profile across ALL time — a profile last touched 30 days ago
    should still report a non-null last-used (just outside the weekly
    window). Validator F#15 caught the prior version scoping both signals
    to the same cutoff.

    A row whose ``profile`` field is missing is treated as belonging to
    the default profile (matches the index's pre-multi-profile shape).
    Spend is intentionally ``None`` in v1 — the UI hides the segment
    until cost tracking lands.
    """
    import datetime as _dt

    cutoff = now - _ACTIVITY_WINDOW_DAYS * 86400.0
    all_timestamps = []     # unbounded — for last_used_at
    window_timestamps = []  # within cutoff — for sessions_week
    for r in rows or ():
        if not isinstance(r, dict):
            continue
        row_profile = r.get('profile') or 'default'
        if row_profile != name:
            continue
        ts = r.get('updated_at')
        if not isinstance(ts, (int, float)):
            continue
        all_timestamps.append(ts)
        if ts >= cutoff:
            window_timestamps.append(ts)

    last_used_at = None
    if all_timestamps:
        most_recent = max(all_timestamps)
        last_used_at = _dt.datetime.fromtimestamp(
            most_recent, tz=_dt.timezone.utc
        ).isoformat().replace('+00:00', 'Z')

    return {
        'sessions_week': len(window_timestamps),
        'last_used_at': last_used_at,
        'spend_week_usd': None,
    }


def _read_gateway_state(profile_home: Path) -> dict:
    """Read .gateway-state.json — return {} on missing or malformed file."""
    state_path = profile_home / '.gateway-state.json'
    if not state_path.exists():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


# Grace window for gateway start before a missing/dead PID is considered
# a failure. Tuned for Telegram + Slack adapter cold-start latency.
GATEWAY_START_GRACE_SECONDS = 8

# Grace window for gateway stop before an alive signal is treated as the
# truthful post-stop state again. This prevents a stale "stopping" stamp from
# disabling the Profile Gateway toggle indefinitely when the gateway remains
# running or is auto-restarted by its supervisor.
GATEWAY_STOP_GRACE_SECONDS = 12


def _write_gateway_phase(
    profile_home: Path,
    phase: str,
    *,
    last_error: str | None = None,
    started_at: str | None = None,
) -> None:
    """Stamp the gateway phase in .gateway-state.json without clobbering
    sibling fields (e.g. last_run_at).

    phase values:
      'starting'  — set phase + phase_started_at + clear last_error
      'stopping'  — set phase + phase_started_at + clear last_error
      'running'   — set phase + phase_started_at + clear last_error
      'failed'    — set phase + phase_started_at + record last_error
      'stopped'   — clear phase, phase_started_at, last_error

    If ``started_at`` is supplied, it is used verbatim for
    ``phase_started_at`` on the non-stopped phases (preserves the original
    transition timestamp during promotion). Otherwise a fresh "now" is
    stamped. The 'stopped' phase always clears ``phase_started_at``
    regardless of ``started_at``.
    """
    import datetime as _dt
    state_path = profile_home / '.gateway-state.json'
    payload: dict = {}
    if state_path.exists():
        try:
            existing = json.loads(state_path.read_text(encoding='utf-8'))
            if isinstance(existing, dict):
                payload = existing
        except (ValueError, OSError):
            payload = {}

    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat().replace('+00:00', 'Z')
    timestamp = started_at if started_at else now_iso

    if phase == 'stopped':
        payload['desired_enabled'] = False
        payload['phase'] = None
        payload['phase_started_at'] = None
        payload['last_error'] = None
    elif phase == 'failed':
        payload['desired_enabled'] = False
        payload['phase'] = 'failed'
        payload['phase_started_at'] = timestamp
        payload['last_error'] = last_error
    elif phase in ('starting', 'running'):
        payload['desired_enabled'] = True
        payload['phase'] = phase
        payload['phase_started_at'] = timestamp
        payload['last_error'] = None
    elif phase == 'stopping':
        payload['desired_enabled'] = False
        payload['phase'] = phase
        payload['phase_started_at'] = timestamp
        payload['last_error'] = None
    else:
        raise ValueError(f"unknown gateway phase: {phase!r}")

    try:
        state_path.write_text(json.dumps(payload), encoding='utf-8')
    except OSError:
        logger.debug("Failed to write gateway phase state", exc_info=True)


def _load_session_index_rows() -> list:
    """Best-effort read of the WebUI session index. Returns [] on any failure.

    The index is global to the WebUI installation (not per-profile), so we
    can compute activity for any profile from a single read.
    """
    try:
        from api.config import SESSION_INDEX_FILE
    except ImportError:
        return []
    if not SESSION_INDEX_FILE.exists():
        return []
    try:
        data = json.loads(SESSION_INDEX_FILE.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return []
    return data if isinstance(data, list) else []


def read_profile_activity_api(name: str) -> dict:
    """Return aggregated activity signals for the profile detail screen.

    Raises:
        ValueError: invalid profile name.
        FileNotFoundError: profile directory does not exist.
    """
    import time as _time
    name, profile_home = _require_profile_home_for_settings(name)
    rows = _load_session_index_rows()
    agg = _compute_profile_activity(rows, name, now=_time.time())
    state = _read_gateway_state(profile_home)
    gateway_last = state.get('last_run_at') if isinstance(state.get('last_run_at'), str) else None
    return {
        'name': name,
        'sessions_week': agg['sessions_week'],
        'last_used_at': agg['last_used_at'],
        'ever_started_gateway': gateway_last is not None,
        'gateway_last_run_at': gateway_last,
        'spend_week_usd': agg['spend_week_usd'],
    }


def read_profile_persona_api(name: str) -> dict:
    """Return the user-authored description for *name*.

    The hero dossier renders this line. It is stored at
    ``webui.description`` in the profile's config.yaml and is intentionally
    separate from SOUL.md, which carries the agent's persona / voice for
    the model itself. Empty when no description has been set yet — the UI
    surfaces an "Add a description" placeholder in that case.

    Raises:
        ValueError: invalid profile name.
        FileNotFoundError: profile directory does not exist.
    """
    name, profile_home = _require_profile_home_for_settings(name)
    config_data = _load_profile_config_for_settings(profile_home)
    description = _extract_profile_description(config_data)
    return {
        'name': name,
        'description': description,
    }


def update_profile_settings_api(name: str, *, provider=_MISSING, model=_MISSING,
                                avatar=_MISSING, avatar_shape=_MISSING, reasoning_effort=_MISSING,
                                description=_MISSING, fallback_model=_MISSING,
                                response_mode=_MISSING, compression=_MISSING,
                                max_turns=_MISSING, auxiliary_models=_MISSING,
                                toolsets=_MISSING, default_workspace=_MISSING) -> dict:
    """Update profile runtime settings and/or WebUI avatar metadata."""
    if (provider is _MISSING and model is _MISSING
            and avatar is _MISSING and avatar_shape is _MISSING
            and reasoning_effort is _MISSING
            and description is _MISSING and fallback_model is _MISSING
            and response_mode is _MISSING and compression is _MISSING
            and max_turns is _MISSING and auxiliary_models is _MISSING
            and toolsets is _MISSING and default_workspace is _MISSING):
        raise ValueError(
            'At least one profile setting is required'
        )
    name, profile_home = _require_profile_home_for_settings(name)

    needs_yaml_write = (
        provider is not _MISSING or model is not _MISSING
        or reasoning_effort is not _MISSING or description is not _MISSING
        or fallback_model is not _MISSING or response_mode is not _MISSING
        or compression is not _MISSING or max_turns is not _MISSING
        or auxiliary_models is not _MISSING or toolsets is not _MISSING
        or default_workspace is not _MISSING
    )
    invalidate_models = False
    if needs_yaml_write:
        config_data = _load_profile_config_for_settings(profile_home)
        config_changed = False
        if provider is not _MISSING or model is not _MISSING:
            if _merge_profile_model_settings(config_data, provider, model):
                config_changed = True
                invalidate_models = True
        if reasoning_effort is not _MISSING:
            if _merge_profile_reasoning_effort(config_data, reasoning_effort):
                config_changed = True
        if fallback_model is not _MISSING:
            if _merge_profile_fallback_model(config_data, fallback_model):
                config_changed = True
        if response_mode is not _MISSING:
            if _merge_profile_response_mode(config_data, response_mode):
                config_changed = True
        if compression is not _MISSING:
            if _merge_profile_compression(config_data, compression):
                config_changed = True
        if max_turns is not _MISSING:
            if _merge_profile_max_turns(config_data, max_turns):
                config_changed = True
        if auxiliary_models is not _MISSING:
            if _merge_profile_auxiliary_models(config_data, auxiliary_models):
                config_changed = True
        if toolsets is not _MISSING:
            if _merge_profile_toolsets(config_data, toolsets):
                config_changed = True
        if default_workspace is not _MISSING:
            if _merge_profile_default_workspace(config_data, default_workspace):
                config_changed = True
        if description is not _MISSING:
            if _merge_profile_description(config_data, description):
                config_changed = True
        if config_changed:
            _save_profile_config_for_settings(profile_home, config_data)
        if invalidate_models:
            from api.config import invalidate_models_cache
            invalidate_models_cache()

    if avatar is not _MISSING or avatar_shape is not _MISSING:
        state = _read_profile_settings_state(profile_home)
        if avatar is not _MISSING:
            normalized_avatar = _normalize_avatar_payload(avatar)
            if normalized_avatar is None:
                state.pop('avatar', None)
            else:
                state['avatar'] = normalized_avatar
        if avatar_shape is not _MISSING:
            state['avatar_shape'] = _normalize_avatar_shape_payload(avatar_shape)
        _write_profile_settings_state(profile_home, state)

    return get_profile_settings_api(name)


def list_profiles_api(include_skill_counts: bool = False,
                      include_full_avatars: bool = False) -> list:
    """List all profiles with metadata, serialized for JSON response."""
    try:
        from hermes_cli.profiles import list_profiles
        infos = list_profiles()
    except ImportError:
        # hermes_cli not available -- return just the default
        return [_default_profile_dict(
            include_skill_counts=include_skill_counts,
            include_full_avatars=include_full_avatars,
        )]

    active = get_active_profile_name()
    result = []
    for p in infos:
        profile_home = Path(p.path)
        avatar = (
            _read_profile_avatar_for_home(profile_home)
            if include_full_avatars
            else _profile_avatar_for_summary(p.name, profile_home)
        )
        state = _read_profile_settings_state(profile_home)
        reactive_pack = state.get('reactive_avatar')
        row = {
            'name': p.name,
            'path': str(p.path),
            'is_default': p.is_default,
            'is_active': p.name == active,
            'gateway_running': p.gateway_running,
            'model': p.model,
            'provider': p.provider,
            'avatar': avatar,
            'avatar_shape': _read_profile_avatar_shape_for_home(profile_home),
            'avatar_mode': _read_profile_avatar_mode_for_home(profile_home),
            'reactive_avatar': _reactive_avatar_pack_for_response(p.name, reactive_pack),
            'effective_reactive_avatar': _effective_reactive_avatar(p.name, avatar, reactive_pack),
            'has_env': p.has_env,
            'skill_count': p.skill_count,
        }
        if include_skill_counts:
            enabled_count, total_count = _profile_skill_counts_for_summary(
                p.name,
                Path(p.path),
            )
            row['skill_enabled_count'] = enabled_count
            row['skill_total'] = total_count
        result.append(row)
    return result


def _default_profile_dict(include_skill_counts: bool = False,
                          include_full_avatars: bool = False) -> dict:
    """Fallback profile dict when hermes_cli is not importable."""
    avatar = (
        _read_profile_avatar_for_home(_DEFAULT_HERMES_HOME)
        if include_full_avatars
        else _profile_avatar_for_summary('default', _DEFAULT_HERMES_HOME)
    )
    state = _read_profile_settings_state(_DEFAULT_HERMES_HOME)
    reactive_pack = state.get('reactive_avatar')
    row = {
        'name': 'default',
        'path': str(_DEFAULT_HERMES_HOME),
        'is_default': True,
        'is_active': True,
        'gateway_running': False,
        'model': None,
        'provider': None,
        'avatar': avatar,
        'avatar_shape': _read_profile_avatar_shape_for_home(_DEFAULT_HERMES_HOME),
        'avatar_mode': _read_profile_avatar_mode_for_home(_DEFAULT_HERMES_HOME),
        'reactive_avatar': _reactive_avatar_pack_for_response('default', reactive_pack),
        'effective_reactive_avatar': _effective_reactive_avatar('default', avatar, reactive_pack),
        'has_env': (_DEFAULT_HERMES_HOME / '.env').exists(),
        'skill_count': 0,
    }
    if include_skill_counts:
        enabled_count, total_count = _profile_skill_counts_for_summary(
            'default',
            _DEFAULT_HERMES_HOME,
        )
        row['skill_enabled_count'] = enabled_count
        row['skill_total'] = total_count
    return row


def _validate_profile_name(name: str):
    """Validate profile name format (matches hermes_cli.profiles upstream)."""
    if name == 'default':
        raise ValueError("Cannot create a profile named 'default' -- it is the built-in profile.")
    # Use fullmatch (not match) so a trailing newline can't sneak past the $ anchor
    if not _PROFILE_ID_RE.fullmatch(name):
        raise ValueError(
            f"Invalid profile name {name!r}. "
            "Must match [a-z0-9][a-z0-9_-]{0,63}"
        )


# Newly-created or renamed profiles are capped at a tighter limit than the
# regex's 64-char back-compat ceiling — long names overflow the hero card
# layout. Keep the regex permissive on read (so existing 33-64 char profiles
# still load) but reject anything longer on write.
_PROFILE_NAME_NEW_MAX_LEN = 32


def _enforce_new_profile_name_cap(name: str) -> None:
    if len(name) > _PROFILE_NAME_NEW_MAX_LEN:
        raise ValueError(
            f"Profile name must be {_PROFILE_NAME_NEW_MAX_LEN} characters or fewer "
            f"(got {len(name)})."
        )


def _profiles_root() -> Path:
    """Return the canonical root that contains named profiles."""
    return (_DEFAULT_HERMES_HOME / 'profiles').resolve()


def _resolve_named_profile_home(name: str) -> Path:
    """Resolve a named profile to a directory under the profiles root.

    Validates *name* as a logical profile identifier first, then resolves the
    final filesystem path and enforces containment under ~/.hermes/profiles.
    """
    _validate_profile_name(name)
    profiles_root = _profiles_root()
    candidate = (profiles_root / name).resolve()
    candidate.relative_to(profiles_root)
    return candidate


def _create_profile_fallback(name: str, clone_from: str = None,
                              clone_config: bool = False) -> Path:
    """Create a profile directory without hermes_cli (Docker/standalone fallback)."""
    profile_dir = _DEFAULT_HERMES_HOME / 'profiles' / name
    if profile_dir.exists():
        raise FileExistsError(f"Profile '{name}' already exists.")

    # Bootstrap directory structure (exist_ok=False so a concurrent create raises)
    profile_dir.mkdir(parents=True, exist_ok=False)
    for subdir in _PROFILE_DIRS:
        (profile_dir / subdir).mkdir(parents=True, exist_ok=True)

    # Clone config files from source profile if requested
    if clone_config and clone_from:
        if _is_root_profile(clone_from):
            source_dir = _DEFAULT_HERMES_HOME
        else:
            source_dir = _DEFAULT_HERMES_HOME / 'profiles' / clone_from
        if source_dir.is_dir():
            for filename in _CLONE_CONFIG_FILES:
                src = source_dir / filename
                if src.exists():
                    shutil.copy2(src, profile_dir / filename)

    return profile_dir


def _write_endpoint_to_config(profile_dir: Path, base_url: str = None, api_key: str = None) -> None:
    """Write custom endpoint fields into config.yaml for a profile."""
    if not base_url and not api_key:
        return
    config_path = profile_dir / 'config.yaml'
    try:
        import yaml as _yaml
    except ImportError:
        return
    cfg = {}
    if config_path.exists():
        try:
            loaded = _yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg = loaded
        except Exception:
            logger.debug("Failed to load config from %s", config_path)
    model_section = cfg.get('model', {})
    if not isinstance(model_section, dict):
        model_section = {}
    if base_url:
        model_section['base_url'] = base_url
    if api_key:
        model_section['api_key'] = api_key
    cfg['model'] = model_section
    config_path.write_text(_yaml.dump(cfg, default_flow_style=False, allow_unicode=True), encoding='utf-8')


def _clean_profile_config_value(value: Optional[str], field: str) -> Optional[str]:
    """Return a safe single-line config value or raise ValueError."""
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    if any(ch in cleaned for ch in ("\x00", "\r", "\n")):
        raise ValueError(f"{field} must be a single-line value")
    if len(cleaned) > 512:
        raise ValueError(f"{field} is too long")
    return cleaned


def _split_webui_provider_model_value(default_model: Optional[str], model_provider: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Normalize WebUI-internal @provider:model picker values for config.yaml."""
    model = _clean_profile_config_value(default_model, "default_model")
    provider = _clean_profile_config_value(model_provider, "model_provider")
    if model and model.startswith("@") and ":" in model:
        provider_part, model_part = model[1:].rsplit(":", 1)
        provider = provider or _clean_profile_config_value(provider_part, "model_provider")
        model = _clean_profile_config_value(model_part, "default_model")
    return model, provider


def _strip_webui_provider_prefix(model_id: object) -> str:
    value = str(model_id or "").strip()
    if value.startswith("@") and ":" in value:
        return value.rsplit(":", 1)[1]
    return value


def _profile_model_selection_exists(
    available_models: object,
    default_model: Optional[str],
    model_provider: Optional[str],
) -> bool:
    """Return True when a profile default model/provider exists in /api/models."""
    if not default_model and not model_provider:
        return True
    if not isinstance(available_models, dict):
        return False

    provider_seen = False
    model_seen = False
    for group in available_models.get("groups", []) or []:
        if not isinstance(group, dict):
            continue
        provider_id = str(group.get("provider_id") or "").strip()
        if model_provider and provider_id != model_provider:
            continue
        if model_provider and provider_id == model_provider:
            provider_seen = True
        for model in group.get("models", []) or []:
            if not isinstance(model, dict):
                continue
            model_id = str(model.get("id") or "").strip()
            if not model_id:
                continue
            if default_model and (
                model_id == default_model
                or _strip_webui_provider_prefix(model_id) == default_model
            ):
                model_seen = True
                if model_provider:
                    return True
        if not default_model and provider_seen:
            return True

    if model_provider and not provider_seen:
        return False
    return bool(model_seen)


def _get_available_models_for_profile_validation() -> dict:
    from api.config import get_available_models

    return get_available_models()


def _validate_profile_model_selection(
    default_model: Optional[str],
    model_provider: Optional[str],
    available_models: Optional[dict] = None,
) -> None:
    """Reject profile model defaults that do not exist in the server catalog."""
    if not default_model and not model_provider:
        return
    catalog = (
        available_models
        if available_models is not None
        else _get_available_models_for_profile_validation()
    )
    if _profile_model_selection_exists(catalog, default_model, model_provider):
        return
    if default_model and model_provider:
        raise ValueError(
            f"Selected model '{default_model}' is not available for provider '{model_provider}'"
        )
    if default_model:
        raise ValueError(f"Selected model '{default_model}' is not available")
    raise ValueError(f"Selected model provider '{model_provider}' is not available")


def _write_model_defaults_to_config(
    profile_dir: Path,
    *,
    default_model: Optional[str] = None,
    model_provider: Optional[str] = None,
) -> None:
    """Write model default/provider fields into config.yaml for a profile."""
    default_model, model_provider = _split_webui_provider_model_value(default_model, model_provider)
    if not default_model and not model_provider:
        return
    config_path = profile_dir / 'config.yaml'
    try:
        import yaml as _yaml
    except ImportError:
        return
    cfg = {}
    if config_path.exists():
        try:
            loaded = _yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg = loaded
        except Exception:
            logger.debug("Failed to load config from %s", config_path)
    model_section = cfg.get('model', {})
    if not isinstance(model_section, dict):
        model_section = {}
    if default_model:
        model_section['default'] = default_model
    if model_provider:
        model_section['provider'] = model_provider
    cfg['model'] = model_section
    config_path.write_text(_yaml.dump(cfg, default_flow_style=False, allow_unicode=True), encoding='utf-8')


def create_profile_api(name: str, clone_from: str = None,
                       clone_config: bool = False,
                       base_url: str = None,
                       api_key: str = None,
                       default_model: str = None,
                       model_provider: str = None) -> dict:
    """Create a new profile. Returns the new profile info dict."""
    _validate_profile_name(name)
    _enforce_new_profile_name_cap(name)
    # Defense-in-depth: validate clone_from here too, even though routes.py
    # also validates it. Any caller that bypasses the HTTP layer gets protection.
    if clone_from is not None and not _is_root_profile(clone_from):
        _validate_profile_name(clone_from)
    default_model, model_provider = _split_webui_provider_model_value(default_model, model_provider)
    _validate_profile_model_selection(default_model, model_provider)

    try:
        from hermes_cli.profiles import create_profile
        create_profile(
            name,
            clone_from=clone_from,
            clone_config=clone_config,
            clone_all=False,
            no_alias=True,
        )
    except ImportError:
        _create_profile_fallback(name, clone_from, clone_config)

    # Resolve the profile directory from the profile list when possible.
    # hermes_cli and the webui runtime do not always agree on the exact root,
    # so we prefer the path returned by list_profiles_api() and fall back to the
    # standard profile location only if the profile cannot be found there yet.
    profile_path = _DEFAULT_HERMES_HOME / 'profiles' / name
    for p in list_profiles_api():
        if p['name'] == name:
            try:
                profile_path = Path(p.get('path') or profile_path)
            except Exception:
                logger.debug("Failed to parse profile path")
            break

    profile_path.mkdir(parents=True, exist_ok=True)

    # Seed bundled skills for non-cloned profiles (#2305).
    # Cloned profiles should preserve the clone-source behaviour and must not
    # receive a second bundled-skill overlay.
    if clone_from is None:
        try:
            from hermes_cli.profiles import seed_profile_skills
            seed_profile_skills(profile_path, quiet=True)
        except ImportError:
            logger.debug(
                'seed_profile_skills unavailable — bundled skills not seeded '
                'for profile %s (hermes_cli not in path)',
                name,
            )
        except Exception:
            logger.warning(
                'Bundled skills could not be seeded for profile %s; '
                'profile created successfully anyway',
                name,
                exc_info=True,
            )

    _write_endpoint_to_config(profile_path, base_url=base_url, api_key=api_key)
    _write_model_defaults_to_config(
        profile_path,
        default_model=default_model,
        model_provider=model_provider,
    )

    # Invalidate cached root-profile-name lookup; create_profile may have added
    # a new profile that flips is_default semantics on the agent side (#1612).
    _invalidate_root_profile_cache()

    # Find and return the newly created profile info.
    # When hermes_cli is not importable, list_profiles_api() also falls back
    # to the stub default-only list and won't find the new profile by name.
    # In that case, return a complete profile dict directly.
    for p in list_profiles_api():
        if p['name'] == name:
            return p
    return {
        'name': name,
        'path': str(profile_path),
        'is_default': False,
        'is_active': _active_profile == name,
        'gateway_running': False,
        'model': None,
        'provider': None,
        'has_env': (profile_path / '.env').exists(),
        'skill_count': 0,
    }


def rename_profile_api(name: str, new_name: str) -> dict:
    """Rename a profile. Refuses the default profile.

    Falls back to a filesystem rename when ``hermes_cli.profiles.rename_profile``
    is not importable. Returns ``{'ok': True, 'old_name', 'new_name',
    'was_active'}`` so callers can refresh the active-profile cookie.
    """
    if _is_root_profile(name):
        raise ValueError("Cannot rename the default profile.")
    _validate_profile_name(name)
    if not isinstance(new_name, str):
        raise ValueError("new_name is required")
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("new_name is required")
    if new_name == name:
        raise ValueError("new_name must differ from current name.")
    _validate_profile_name(new_name)
    _enforce_new_profile_name_cap(new_name)

    profiles_root = _profiles_root()
    src_dir = _resolve_named_profile_home(name)
    if not src_dir.is_dir():
        raise FileNotFoundError(f"Profile '{name}' does not exist.")
    dst_dir = (profiles_root / new_name).resolve()
    dst_dir.relative_to(profiles_root)
    if dst_dir.exists():
        raise FileExistsError(f"Profile '{new_name}' already exists.")

    global _active_profile
    was_active = _active_profile == name

    try:
        from hermes_cli.profiles import rename_profile as _cli_rename
    except ImportError:
        _cli_rename = None

    if _cli_rename is not None:
        try:
            _cli_rename(name, new_name)
        except TypeError:
            # Older signature variants might require keyword arguments.
            _cli_rename(old_name=name, new_name=new_name)
    else:
        # Filesystem fallback: rename the directory in place.
        src_dir.rename(dst_dir)

    if was_active:
        # Update the process-global active profile so subsequent requests
        # without a cookie still resolve to the renamed directory.
        with _profile_lock:
            _active_profile = new_name

    _invalidate_root_profile_cache()
    return {'ok': True, 'old_name': name, 'new_name': new_name, 'was_active': was_active}


def duplicate_profile_api(name: str, new_name: str, *, clone_all: bool = False) -> dict:
    """Duplicate a profile. Copies config and (when supported) WebUI state.

    By default ``clone_all=False`` clones only config files. Pass
    ``clone_all=True`` to mirror everything supported by the CLI duplicate
    semantics.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required")
    if not isinstance(new_name, str) or not new_name.strip():
        raise ValueError("new_name is required")
    name = name.strip()
    new_name = new_name.strip()
    if name == new_name:
        raise ValueError("new_name must differ from source name.")
    _validate_profile_name(new_name)
    _enforce_new_profile_name_cap(new_name)

    profiles_root = _profiles_root()
    dst_dir = (profiles_root / new_name).resolve()
    dst_dir.relative_to(profiles_root)
    if dst_dir.exists():
        raise FileExistsError(f"Profile '{new_name}' already exists.")

    # Resolve source directory: root profile maps to ~/.hermes
    if _is_root_profile(name):
        src_dir = _DEFAULT_HERMES_HOME
    else:
        _validate_profile_name(name)
        src_dir = _resolve_named_profile_home(name)
    if not src_dir.is_dir():
        raise FileNotFoundError(f"Profile '{name}' does not exist.")

    try:
        from hermes_cli.profiles import create_profile as _cli_create_profile
    except ImportError:
        _cli_create_profile = None

    if _cli_create_profile is not None:
        try:
            _cli_create_profile(
                new_name,
                clone_from=name,
                clone_config=True,
                clone_all=bool(clone_all),
                no_alias=True,
            )
        except TypeError:
            _cli_create_profile(new_name, clone_from=name, clone_config=True)
    else:
        # Filesystem fallback: copy config files (and optionally additional dirs).
        _create_profile_fallback(new_name, clone_from=name, clone_config=True)
        if clone_all:
            for sub in ('memories', 'skills'):
                src_sub = src_dir / sub
                dst_sub = dst_dir / sub
                if src_sub.is_dir():
                    shutil.copytree(src_sub, dst_sub, dirs_exist_ok=True)

    # Copy WebUI state (avatar etc.) when present — not handled by hermes_cli.
    try:
        src_state = _profile_settings_state_path(src_dir)
        if src_state.exists():
            dst_state = _profile_settings_state_path(dst_dir)
            dst_state.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_state, dst_state)
        src_assets = _reactive_avatar_asset_root(src_dir)
        if src_assets.is_dir():
            shutil.copytree(
                src_assets,
                _reactive_avatar_asset_root(dst_dir),
                dirs_exist_ok=True,
            )
    except OSError:
        logger.debug("Failed to copy WebUI state during duplicate", exc_info=True)

    _invalidate_root_profile_cache()
    # Return the freshly-listed profile metadata for the duplicate.
    for p in list_profiles_api():
        if p['name'] == new_name:
            return p
    return {
        'name': new_name,
        'path': str(dst_dir),
        'is_default': False,
        'is_active': False,
        'gateway_running': False,
        'model': None,
        'provider': None,
        'avatar': _read_profile_avatar_for_home(dst_dir),
        'avatar_shape': _read_profile_avatar_shape_for_home(dst_dir),
        'avatar_mode': _read_profile_avatar_mode_for_home(dst_dir),
        'has_env': (dst_dir / '.env').exists(),
        'skill_count': 0,
    }


# ── Per-profile skills list API ────────────────────────────────────────────

# Module-level cache: absolute SKILL.md path string → {name, description, category}
# Does NOT store `enabled` or `path` — those are recomputed per call.
_skill_md_cache: dict[str, dict] = {}


def _invalidate_skill_cache_for_path(path) -> None:
    """Remove the cached metadata entry for *path* (if any).

    Accepts a Path object or a string.  Key normalisation uses str(Path(path))
    with no resolve() call so it stays consistent with how list_profile_skills_api
    stores keys (using the raw absolute path string from rglob).
    """
    key = str(path)
    _skill_md_cache.pop(key, None)


def _parse_skill_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from SKILL.md content.

    Returns (frontmatter_dict, body_str).  If no frontmatter block is present
    the returned dict is empty and body_str is the full content.

    Implemented inline so ``api/profiles`` does not depend on the hermes-agent
    ``tools.skills_tool`` package (which may not be installed in the WebUI venv).
    """
    import yaml

    if not content.startswith("---"):
        return {}, content
    # Find the closing ---
    end = content.find("\n---", 3)
    if end == -1:
        return {}, content
    fm_text = content[3:end].strip()
    body = content[end + 4:].lstrip("\n")
    try:
        fm = yaml.safe_load(fm_text) or {}
        if not isinstance(fm, dict):
            fm = {}
    except Exception:
        fm = {}
    return fm, body


def _get_external_skills_dirs() -> list:
    """Return the list of external skill directories from hermes-agent.

    These are the agent-bundled skill roots (typically
    ``<HERMES_HOME>/hermes-agent/skills``) that are NOT under the per-profile
    ``skills/`` directory.  The global ``/api/skills`` endpoint already walks
    these via ``_active_skill_search_dirs``; this function lets the per-profile
    skills list include them too.

    Best-effort: returns an empty list if hermes-agent is not installed.
    Tests may monkeypatch this function to inject synthetic external dirs.
    """
    try:
        from agent.skill_utils import get_external_skills_dirs as _ext  # type: ignore
        result = _ext()
        return list(result) if result else []
    except (ImportError, Exception):
        return []


def _append_existing_skill_dir(dirs: list[Path], path: Path) -> None:
    """Append *path* once when it is an existing skill directory."""
    if path.is_dir() and path not in dirs:
        dirs.append(path)


def _all_profile_skill_search_dirs(profile_home: Path) -> list[Path]:
    """Return the Hermes-wide skill universe with selected-profile precedence.

    The UI lets every profile enable/disable skills from the same available
    skill set. A profile's local ``skills/`` directory can override metadata
    for duplicate names, but the denominator must come from every skill root
    Hermes can see, not only the selected profile's local folder.
    """
    dirs: list[Path] = []
    _append_existing_skill_dir(dirs, profile_home / "skills")
    _append_existing_skill_dir(dirs, _DEFAULT_HERMES_HOME / "skills")

    profiles_root = _DEFAULT_HERMES_HOME / "profiles"
    if profiles_root.is_dir():
        for child in sorted(profiles_root.iterdir(), key=lambda p: p.name.lower()):
            if child.is_dir():
                _append_existing_skill_dir(dirs, child / "skills")

    for ext in _get_external_skills_dirs():
        try:
            ext_path = Path(ext)
        except Exception:
            continue
        _append_existing_skill_dir(dirs, ext_path)
    return dirs


def _profile_skill_counts_for_summary(name: str, profile_home: Path | None = None) -> tuple[int, int]:
    """Return (enabled_count, total_count) for lightweight profile summaries."""
    try:
        if profile_home is None:
            if _is_root_profile(name):
                profile_home = _DEFAULT_HERMES_HOME
            else:
                profile_home = _resolve_named_profile_home(name)
        data = _list_profile_skills_for_home(name, Path(profile_home))
        return int(data.get("enabled_count") or 0), int(data.get("total_count") or 0)
    except Exception:
        logger.debug("Failed to compute profile skill summary for %s", name, exc_info=True)
        return 0, 0


def list_profile_skills_api(name: str) -> dict:
    """Return the Hermes-wide skills available to *name*.

    The denominator is shared across profiles: it scans every visible Hermes
    skill root (selected profile first for duplicate-name precedence, default
    profile, named profile folders, and external/bundled dirs). The selected
    profile's ``skills.disabled`` config is the only per-profile count input.

    The disabled set is read from ``<profile-home>/config.yaml`` (key
    ``skills.disabled``).  Disabled skills are INCLUDED in the response list
    with ``enabled: False``; enabled skills get ``enabled: True``.

    Results are cached by SKILL.md absolute path.  Call
    ``_invalidate_skill_cache_for_path(path)`` after writing a SKILL.md to
    force a re-parse on the next list call.

    Raises:
        FileNotFoundError: profile home directory does not exist.
        ValueError: *name* fails basic validation.
    """
    _validate_profile_settings_name(name)
    if _is_root_profile(name):
        profile_home = _DEFAULT_HERMES_HOME
    else:
        profile_home = _resolve_named_profile_home(name)

    if not profile_home.is_dir():
        raise FileNotFoundError(f"Profile '{name}' not found.")

    return _list_profile_skills_for_home(name, profile_home)


def _list_profile_skills_for_home(name: str, profile_home: Path) -> dict:
    import yaml as _yaml

    # --- Read disabled set from config.yaml ---
    disabled_set: set[str] = set()
    config_path = profile_home / "config.yaml"
    if config_path.is_file():
        try:
            cfg = _yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            if isinstance(cfg, dict):
                disabled_list = cfg.get("skills", {}).get("disabled", [])
                if isinstance(disabled_list, list):
                    disabled_set = {str(x) for x in disabled_list if isinstance(x, str)}
        except Exception:
            disabled_set = set()

    search_dirs = _all_profile_skill_search_dirs(profile_home)

    if not search_dirs:
        return {
            "ok": True,
            "profile": name,
            "skills": [],
            "total_count": 0,
            "enabled_count": 0,
            "categories": [],
        }

    # --- Iterate SKILL.md files across all search roots ---
    skills: list[dict] = []
    seen_names: set[str] = set()

    for skills_root in search_dirs:
        for skill_md in sorted(skills_root.rglob("SKILL.md")):
            if not skill_md.is_file():
                continue
            try:
                rel = skill_md.relative_to(skills_root)
            except ValueError:
                continue
            parts = rel.parts  # e.g. ("research", "deep-dive", "SKILL.md") or ("name", "SKILL.md")
            if len(parts) < 2:
                # SKILL.md must live inside at least one directory (the skill dir).
                continue
            # Skill dir name is the immediate parent of SKILL.md.
            skill_dir_name = parts[-2]
            # Category is the path segment above the skill dir (if present).
            category: str | None = parts[-3] if len(parts) >= 3 else None

            # Check cache by absolute path string (no resolve — consistent with invalidation)
            path_str = str(skill_md)
            cached = _skill_md_cache.get(path_str)
            if cached is None:
                try:
                    content = skill_md.read_text(encoding="utf-8")[:4000]
                except (OSError, UnicodeDecodeError):
                    continue
                fm, body = _parse_skill_frontmatter(content)
                skill_name = str(fm.get("name", skill_dir_name))[:64]
                description = fm.get("description", "")
                if not description:
                    for line in body.strip().split("\n"):
                        line = line.strip()
                        if line and not line.startswith("#"):
                            description = line
                            break
                cached = {
                    "name": skill_name,
                    "description": description,
                    "category": category,
                }
                _skill_md_cache[path_str] = cached

            # Deduplicate by name: profile-local wins (added first).
            if cached["name"] in seen_names:
                continue
            seen_names.add(cached["name"])

            skills.append({
                "name": cached["name"],
                "category": cached["category"],
                "description": cached["description"],
                "enabled": cached["name"] not in disabled_set,
                "path": path_str,
                "source": str(skills_root),
            })

    # Sort alphabetically by name (case-insensitive)
    skills.sort(key=lambda s: s["name"].lower())

    total_count = len(skills)
    enabled_count = sum(1 for s in skills if s["enabled"])
    categories = sorted({s["category"] for s in skills if s.get("category")})
    return {
        "ok": True,
        "profile": name,
        "skills": skills,
        "total_count": total_count,
        "enabled_count": enabled_count,
        "categories": categories,
    }


# ---------------------------------------------------------------------------
# Skill name validation helper
# ---------------------------------------------------------------------------

def _validate_skill_name(skill: str) -> None:
    """Raise ``ValueError`` for empty or path-traversal skill names."""
    if not isinstance(skill, str) or not skill.strip():
        raise ValueError("skill name must be a non-empty string")
    if ".." in skill or "/" in skill or "\\" in skill:
        raise ValueError(f"skill name contains invalid characters: {skill!r}")


# ---------------------------------------------------------------------------
# toggle_profile_skill_api
# ---------------------------------------------------------------------------

def toggle_profile_skill_api(name: str, skill: str, enabled: bool) -> dict:
    """Enable or disable a single skill for the given profile.

    Reads ``<profile-home>/config.yaml``, updates the ``skills.disabled`` list,
    and writes back only when the state actually changes.  The response mirrors
    ``list_profile_skills_api`` plus a ``changed`` boolean so callers can tell
    whether a write occurred.

    Args:
        name:    Profile name (validated via ``_validate_profile_settings_name``).
        skill:   Skill name to toggle (must be non-empty, no path traversal).
        enabled: ``True`` → remove from disabled list; ``False`` → add to it.

    Returns:
        ``{ok, changed, profile, skills, total_count, enabled_count}``

    Raises:
        FileNotFoundError: profile home directory does not exist.
        ValueError: *name* or *skill* fails validation.
    """
    import yaml as _yaml

    _validate_profile_settings_name(name)
    _validate_skill_name(skill)

    if _is_root_profile(name):
        profile_home = _DEFAULT_HERMES_HOME
    else:
        profile_home = _resolve_named_profile_home(name)

    if not profile_home.is_dir():
        raise FileNotFoundError(f"Profile '{name}' not found.")

    config_path = profile_home / "config.yaml"

    # --- Read existing config ---
    cfg: dict = {}
    if config_path.is_file():
        try:
            loaded = _yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg = loaded
        except Exception:
            cfg = {}

    # --- Derive existing disabled set ---
    raw_disabled = cfg.get("skills", {}).get("disabled", [])
    if not isinstance(raw_disabled, list):
        raw_disabled = []
    old_set: set[str] = {str(x) for x in raw_disabled if isinstance(x, str)}

    # --- Compute new disabled set ---
    new_set: set[str] = set(old_set)
    if enabled:
        new_set.discard(skill)
    else:
        new_set.add(skill)

    changed = new_set != old_set

    if changed:
        if new_set:
            cfg.setdefault("skills", {})["disabled"] = sorted(new_set)
        else:
            # Empty list — remove the key entirely to keep config tidy.
            if "skills" in cfg:
                cfg["skills"].pop("disabled", None)
                if not cfg["skills"]:
                    del cfg["skills"]
        config_path.write_text(
            _yaml.safe_dump(cfg, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    result = list_profile_skills_api(name)
    result["changed"] = changed
    return result


# ---------------------------------------------------------------------------
# set_profile_disabled_skills_api
# ---------------------------------------------------------------------------

def set_profile_disabled_skills_api(name: str, disabled_list: list) -> dict:
    """Replace the full disabled-skills list for the given profile in one write.

    Unlike ``toggle_profile_skill_api`` this overwrites the entire set, which
    is what the bulk "Save" action in the Skills manager modal needs.

    Args:
        name:          Profile name.
        disabled_list: A ``list`` of skill-name strings (may be empty to
                       clear all disabled skills).  Passing a non-list (e.g.
                       a bare string) raises ``ValueError``.

    Returns:
        ``{ok, changed, profile, skills, total_count, enabled_count}``

    Raises:
        FileNotFoundError: profile home directory does not exist.
        ValueError: *name* or any element in *disabled_list* fails validation,
                    or *disabled_list* is not a ``list``.
    """
    import yaml as _yaml

    _validate_profile_settings_name(name)

    if not isinstance(disabled_list, list):
        raise ValueError("disabled_list must be a list of skill-name strings")

    for item in disabled_list:
        _validate_skill_name(item)

    if _is_root_profile(name):
        profile_home = _DEFAULT_HERMES_HOME
    else:
        profile_home = _resolve_named_profile_home(name)

    if not profile_home.is_dir():
        raise FileNotFoundError(f"Profile '{name}' not found.")

    config_path = profile_home / "config.yaml"

    # --- Read existing config ---
    cfg: dict = {}
    if config_path.is_file():
        try:
            loaded = _yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg = loaded
        except Exception:
            cfg = {}

    # --- Derive existing disabled set ---
    raw_disabled = cfg.get("skills", {}).get("disabled", [])
    if not isinstance(raw_disabled, list):
        raw_disabled = []
    old_set: set[str] = {str(x) for x in raw_disabled if isinstance(x, str)}

    new_set: set[str] = set(disabled_list)
    changed = new_set != old_set

    if changed:
        if new_set:
            cfg.setdefault("skills", {})["disabled"] = sorted(new_set)
        else:
            if "skills" in cfg:
                cfg["skills"].pop("disabled", None)
                if not cfg["skills"]:
                    del cfg["skills"]
        config_path.write_text(
            _yaml.safe_dump(cfg, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    result = list_profile_skills_api(name)
    result["changed"] = changed
    return result


# ---------------------------------------------------------------------------
# resolve_profile_skill_file
# ---------------------------------------------------------------------------

def resolve_profile_skill_file(name: str, skill: str):
    """Return the ``Path`` to the SKILL.md for *skill* visible to *name*.

    Searches the same Hermes-wide skill roots that ``list_profile_skills_api``
    uses. A match is found when the containing directory name equals *skill*,
    OR when the frontmatter ``name:`` field equals *skill*.

    Args:
        name:  Profile name.
        skill: Skill name to locate.

    Returns:
        :class:`pathlib.Path` pointing to the matching ``SKILL.md``.

    Raises:
        FileNotFoundError: profile home directory does not exist, or no
                           matching skill was found.
        ValueError: *name* or *skill* fails validation.
    """
    _validate_profile_settings_name(name)
    _validate_skill_name(skill)

    if _is_root_profile(name):
        profile_home = _DEFAULT_HERMES_HOME
    else:
        profile_home = _resolve_named_profile_home(name)

    if not profile_home.is_dir():
        raise FileNotFoundError(f"Profile '{name}' not found.")

    search_dirs = _all_profile_skill_search_dirs(profile_home)

    if not search_dirs:
        raise FileNotFoundError(f"Skill '{skill}' not found in profile '{name}'.")

    for skills_root in search_dirs:
        for skill_md in sorted(skills_root.rglob("SKILL.md")):
            if not skill_md.is_file():
                continue
            # Check directory name first (fast path, no file read needed).
            if skill_md.parent.name == skill:
                return skill_md
            # Fall back to frontmatter name field.
            try:
                content = skill_md.read_text(encoding="utf-8")[:4000]
            except (OSError, UnicodeDecodeError):
                continue
            fm, _ = _parse_skill_frontmatter(content)
            if str(fm.get("name", "")) == skill:
                return skill_md

    raise FileNotFoundError(f"Skill '{skill}' not found in profile '{name}'.")


# Gateway control helper override hook — tests monkeypatch this with a fake
# runner. When set, ``profile_gateway_control_api`` calls it instead of
# importing ``hermes_cli.gateway``. The hook must return a dict shaped like the
# API response (``ok``, ``running`` etc.) or raise to signal failure.
_gateway_control_hook = None


def _set_gateway_control_hook(fn) -> None:
    """Install a test-only gateway control override."""
    global _gateway_control_hook
    _gateway_control_hook = fn


def _resolve_hermes_bin() -> str:
    """Resolve the path to the ``hermes`` CLI entry script.

    The container's PATH does not include the venv's bin dir, so a bare
    ``'hermes'`` argv[0] fails with FileNotFoundError (swallowed by
    stderr=DEVNULL — silent failure). The hermes script is always
    co-located with the running Python interpreter (venvs put entry
    points next to the interpreter), so derive from ``sys.executable``.
    Falls back to ``shutil.which`` for unusual layouts.
    """
    import shutil as _shutil
    import sys as _sys
    from pathlib import Path as _Path

    # 1. Venv-relative lookup — the canonical case inside the container.
    venv_bin = _Path(_sys.executable).parent
    for candidate in (venv_bin / 'hermes', venv_bin / 'hermes.exe'):
        if candidate.is_file():
            return str(candidate)
    # 2. shutil.which on the standard PATH.
    found = _shutil.which('hermes')
    if found:
        return found
    # 3. Last resort — return the bare name and let subprocess raise a
    #    visible FileNotFoundError instead of silently DEVNULLing.
    return 'hermes'


def _local_gateway_control(name: str, action: str) -> dict:
    """In-process local gateway control backend.

    Brackets the HERMES_HOME swap via ``cron_profile_context_for_home`` so
    the child gateway process inherits the right profile via os.environ
    at fork time.

    Start/restart spawn the gateway as a DETACHED background subprocess
    invoking ``hermes gateway run`` (the foreground runner). We do NOT call
    ``gateway_command(Namespace(gateway_command='start'))`` because that
    routes through service-manager logic which sys.exit()s inside Docker
    containers — the production deployment is containerized.

    Stop uses the in-process ``stop_profile_gateway()`` which kills the
    PID recorded by start_gateway.
    """
    import subprocess as _subprocess
    import sys as _sys
    from hermes_cli import gateway as _gw  # raises ImportError if absent

    if _is_root_profile(name):
        profile_home = _DEFAULT_HERMES_HOME
    else:
        profile_home = _resolve_named_profile_home(name)

    def _spawn_gateway() -> None:
        # Detached background process. Resolve the hermes binary to an
        # absolute path so it works regardless of the container's PATH.
        # On POSIX: start_new_session=True severs the controlling terminal.
        # On Windows: DETACHED_PROCESS via creationflags.
        hermes_bin = _resolve_hermes_bin()
        kwargs: dict = {"close_fds": True}
        if _sys.platform == "win32":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        # Surface gateway-spawn errors to a per-profile log instead of
        # DEVNULL — silent failure here was what hid the wrong-PATH bug.
        log_path = profile_home / ".gateway-stderr.log"
        log_fh = None
        try:
            log_fh = open(log_path, "a", encoding="utf-8", buffering=1)  # noqa: WPS515
        except OSError:
            pass  # Best-effort logging only.
        try:
            # `--replace` clears any stale gateway.pid the hermes CLI
            # would otherwise refuse to overwrite ("Gateway already
            # running (PID X)"). Without this, a clean gateway exit
            # (e.g. 'No messaging platforms enabled') leaves a PID file
            # that blocks every subsequent start until the user
            # manually deletes it inside HERMES_HOME.
            _subprocess.Popen(
                [hermes_bin, "gateway", "run", "--replace"],
                stdin=_subprocess.DEVNULL,
                stdout=log_fh if log_fh else _subprocess.DEVNULL,
                stderr=log_fh if log_fh else _subprocess.DEVNULL,
                **kwargs,
            )
        finally:
            # Close the parent's end of the log file handle after Popen
            # inherits (duplicates) it into the child. Keeps the file
            # unlocked in the parent process on Windows.
            if log_fh is not None:
                try:
                    log_fh.close()
                except OSError:
                    pass

    try:
        with cron_profile_context_for_home(profile_home):
            if action == 'stop':
                _gw.stop_profile_gateway()
                return {'ok': True, 'running': False}
            if action == 'start':
                _spawn_gateway()
                return {'ok': True, 'running': True}
            raise ValueError(f"unknown gateway action: {action!r}")
    except SystemExit as exc:
        # Defensive: the underlying CLI helpers may call sys.exit() in some
        # platforms (notably container/wsl/termux). Converting to a normal
        # exception prevents process termination of the WebUI itself.
        raise RuntimeError(f"gateway subsystem aborted: {exc}") from exc


class _LocalGatewayControlAdapter:
    name = 'local'
    control_available = True

    def status(self, profile_home: Path, profile_name: str) -> dict | None:
        return None

    def start(self, profile_home: Path, profile_name: str) -> dict:
        return _local_gateway_control(profile_name, 'start')

    def stop(self, profile_home: Path, profile_name: str) -> dict:
        return _local_gateway_control(profile_name, 'stop')


_CONTAINER_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')


class _DockerGatewayControlAdapter:
    name = 'docker_exec'

    def __init__(self, container: str):
        container = str(container or '').strip()
        if not _CONTAINER_NAME_RE.fullmatch(container):
            raise ValueError('WEBUI_GATEWAY_DOCKER_CONTAINER must be a Docker container/service name')
        self.container = container

    def status(self, profile_home: Path, profile_name: str) -> dict | None:
        import shutil as _shutil
        if _shutil.which('docker'):
            return None
        detail = 'Docker gateway control is configured, but the docker CLI is unavailable to this WebUI process.'
        return {
            'phase': 'unavailable',
            'status_source': 'adapter',
            'health': {'alive': None, 'state': 'unknown', 'reason': 'docker_cli_unavailable'},
            'control_available': False,
            'detail': detail,
        }

    def _run(self, profile_home: Path, action: str) -> dict:
        import subprocess as _subprocess
        if action == 'start':
            gateway_args = ['gateway', 'run', '--replace']
            running = True
        elif action == 'stop':
            gateway_args = ['gateway', 'stop']
            running = False
        else:
            raise ValueError(f"unknown gateway action: {action!r}")
        cmd = [
            'docker', 'exec', '-e', f'HERMES_HOME={Path(profile_home)}', self.container,
            'hermes', *gateway_args,
        ]
        result = _subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
        if result.returncode != 0:
            raise RuntimeError(_sanitize_gateway_message(result.stderr or 'docker gateway command failed'))
        return {'ok': True, 'running': running, 'adapter': self.name}

    def start(self, profile_home: Path, profile_name: str) -> dict:
        return self._run(profile_home, 'start')

    def stop(self, profile_home: Path, profile_name: str) -> dict:
        return self._run(profile_home, 'stop')


def _load_gateway_webui_config(profile_home: Path) -> dict:
    config_path = profile_home / 'config.yaml'
    if not config_path.exists():
        return {}
    try:
        import yaml as _yaml
        loaded = _yaml.safe_load(config_path.read_text(encoding='utf-8'))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    webui_cfg = loaded.get('webui') if isinstance(loaded.get('webui'), dict) else {}
    gateway_cfg = webui_cfg.get('gateway') if isinstance(webui_cfg.get('gateway'), dict) else {}
    return gateway_cfg if isinstance(gateway_cfg, dict) else {}


def _gateway_control_config(profile_home: Path) -> dict:
    cfg = _load_gateway_webui_config(profile_home)
    control_cfg = cfg.get('control') if isinstance(cfg.get('control'), dict) else {}
    mode = os.getenv('WEBUI_GATEWAY_CONTROL_MODE') or control_cfg.get('mode') or 'local'
    container = os.getenv('WEBUI_GATEWAY_DOCKER_CONTAINER') or control_cfg.get('container')
    remote_health_url = os.getenv('WEBUI_GATEWAY_REMOTE_HEALTH_URL') or cfg.get('remote_health_url')
    allow_service = str(os.getenv('WEBUI_GATEWAY_REMOTE_HEALTH_ALLOW_SERVICE', '')).lower() in ('1', 'true', 'yes')
    if cfg.get('remote_health_allow_service') is True:
        allow_service = True
    return {
        'mode': str(mode or 'local').strip().lower(),
        'container': str(container or '').strip(),
        'remote_health_url': str(remote_health_url or '').strip(),
        'remote_health_allow_service': allow_service,
    }


def _select_gateway_control_adapter(name: str, profile_home: Path):
    cfg = _gateway_control_config(profile_home)
    mode = cfg['mode']
    if mode in ('', 'local'):
        return _LocalGatewayControlAdapter()
    if mode == 'docker_exec':
        return _DockerGatewayControlAdapter(cfg['container'])
    if mode in ('remote_health', 'status_only', 'unavailable'):
        return _UnavailableGatewayControlAdapter(f"Gateway lifecycle control mode '{mode}' is status-only in this WebUI.")
    return _UnavailableGatewayControlAdapter(f"Unsupported gateway lifecycle control mode '{mode}'.")


class _UnavailableGatewayControlAdapter:
    name = 'unavailable'

    def __init__(self, detail: str):
        self.detail = _sanitize_gateway_message(detail)

    def status(self, profile_home: Path, profile_name: str) -> dict:
        return {
            'phase': 'unavailable',
            'status_source': 'adapter',
            'health': {'alive': None, 'state': 'unknown', 'reason': 'control_unavailable'},
            'control_available': False,
            'detail': self.detail,
        }

    def start(self, profile_home: Path, profile_name: str) -> dict:
        raise RuntimeError(self.detail)

    def stop(self, profile_home: Path, profile_name: str) -> dict:
        raise RuntimeError(self.detail)


def _default_gateway_control(name: str, action: str) -> dict:
    if _is_root_profile(name):
        profile_home = _DEFAULT_HERMES_HOME
    else:
        profile_home = _resolve_named_profile_home(name)
    adapter = _select_gateway_control_adapter(name, profile_home)
    if action == 'start':
        return adapter.start(profile_home, name)
    if action == 'stop':
        return adapter.stop(profile_home, name)
    raise ValueError(f"unknown gateway action: {action!r}")


def _is_pid_alive(pid: int) -> bool:
    """True if a process with `pid` exists and is signal-able.

    Module-level binding so tests can monkey-patch.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # EPERM means the process exists; we just can't signal it.
        return True
    except OSError:
        return False
    return True


def _gateway_status_module():
    """Load Hermes gateway.status lazily so WebUI-only installs still work."""
    import importlib as _importlib
    return _importlib.import_module('gateway.status')


def _gateway_running_pid_from_helper(pid_path: Path) -> int | None:
    """Return the canonical Hermes running PID when the helper is available."""
    try:
        gateway_status = _gateway_status_module()
        get_running_pid = gateway_status.get_running_pid
    except Exception:
        return None
    try:
        pid = get_running_pid(pid_path, cleanup_stale=False)
    except TypeError:
        try:
            pid = get_running_pid(pid_path=pid_path, cleanup_stale=False)
        except TypeError:
            try:
                pid = get_running_pid(cleanup_stale=False)
            except TypeError:
                pid = get_running_pid()
    except Exception:
        return None
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return None
    return pid_int if pid_int > 0 else None


def _read_gateway_pid_record(profile_home: Path) -> int | None:
    """Return the PID recorded in gateway.pid, supporting JSON and legacy int."""
    pid_path = profile_home / 'gateway.pid'
    if not pid_path.exists():
        return None
    try:
        raw = pid_path.read_text(encoding='utf-8').strip()
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = raw
    if isinstance(payload, dict):
        payload = payload.get('pid')
    if payload is None:
        return None
    try:
        pid = int(payload)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _read_gateway_pid(profile_home: Path) -> int | None:
    """Return an observed-live gateway PID or None if missing/not visible."""
    pid_path = profile_home / 'gateway.pid'
    helper_pid = _gateway_running_pid_from_helper(pid_path)
    if helper_pid is not None:
        return helper_pid
    pid = _read_gateway_pid_record(profile_home)
    if pid and _is_pid_alive(pid):
        return pid
    return None


def _read_gateway_runtime_status(profile_home: Path) -> dict | None:
    """Read Hermes Agent's runtime gateway_state.json for this profile."""
    runtime_path = profile_home / 'gateway_state.json'
    if not runtime_path.exists():
        return None
    try:
        data = json.loads(runtime_path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _gateway_runtime_health(runtime_status: dict | None) -> tuple[str | None, dict | None, str | None, str | None]:
    """Map gateway_state.json to (phase, health, source, updated_at)."""
    if not isinstance(runtime_status, dict):
        return None, None, None, None
    try:
        from api import agent_health as _agent_health
    except Exception:
        _agent_health = None
    updated_at = runtime_status.get('updated_at') if isinstance(runtime_status.get('updated_at'), str) else None
    if _agent_health is not None and _agent_health._runtime_status_is_fresh(runtime_status):
        return (
            'running',
            {'alive': True, 'state': 'alive', 'reason': 'cross_container_freshness'},
            'runtime_file',
            updated_at,
        )
    if _agent_health is not None and _agent_health._runtime_status_is_stale_running(runtime_status):
        return (
            'unknown',
            {'alive': None, 'state': 'unknown', 'reason': 'gateway_stale_running_state'},
            'runtime_file',
            updated_at,
        )
    if runtime_status.get('gateway_state') == 'running':
        return (
            'unknown',
            {'alive': None, 'state': 'unknown', 'reason': 'gateway_stale_running_state'},
            'runtime_file',
            updated_at,
        )
    return (
        'stopped',
        {'alive': False, 'state': 'down', 'reason': 'gateway_not_running'},
        'runtime_file',
        updated_at,
    )


def _remote_health_host_allowed(hostname: str, *, allow_service_hosts: bool = False) -> bool:
    host = (hostname or '').strip().lower().strip('[]')
    if host in ('localhost', '127.0.0.1', '::1'):
        return True
    if allow_service_hosts and re.fullmatch(r'[a-z][a-z0-9-]{0,62}', host):
        return True
    return False


def _probe_gateway_remote_health(base_url: str, *, allow_service_hosts: bool = False, timeout: float = 0.75) -> dict | None:
    """Probe a configured backend-only Gateway health URL with SSRF guardrails."""
    from urllib.parse import urljoin, urlparse
    import urllib.error as _urlerror
    import urllib.request as _urlrequest

    raw_url = str(base_url or '').strip()
    if not raw_url:
        return None
    parsed = urlparse(raw_url)
    if parsed.scheme not in ('http', 'https') or not _remote_health_host_allowed(parsed.hostname or '', allow_service_hosts=allow_service_hosts):
        return {
            'phase': 'unavailable',
            'status_source': 'remote_health',
            'health': {'alive': None, 'state': 'unknown', 'reason': 'unsafe_remote_health_url'},
            'control_available': False,
            'detail': 'Configured gateway remote health URL is not allowed by WebUI safety policy.',
        }

    class _NoRedirectHandler(_urlrequest.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = _urlrequest.build_opener(_NoRedirectHandler)

    for suffix in ('/health/detailed', '/health'):
        try:
            req = _urlrequest.Request(urljoin(raw_url.rstrip('/') + '/', suffix.lstrip('/')), headers={'Accept': 'application/json'})
            with opener.open(req, timeout=min(float(timeout), 1.0)) as resp:
                body = resp.read(65536).decode('utf-8', errors='replace')
            payload = json.loads(body) if body else {}
            if not isinstance(payload, dict):
                payload = {}
            gateway_state = payload.get('gateway_state') or payload.get('state')
            if gateway_state == 'running' or payload.get('ok') is True:
                return {
                    'phase': 'running',
                    'status_source': 'remote_health',
                    'health': {'alive': True, 'state': 'alive', 'reason': 'remote_health'},
                    'control_available': False,
                    'updated_at': payload.get('updated_at') if isinstance(payload.get('updated_at'), str) else None,
                    'detail': 'Gateway is reachable through configured remote health; lifecycle control is status-only.',
                }
        except _urlerror.HTTPError as exc:
            if suffix == '/health/detailed' and exc.code in (404, 405):
                continue
            return {
                'phase': 'unavailable',
                'status_source': 'remote_health',
                'health': {'alive': None, 'state': 'unknown', 'reason': 'remote_health_unavailable'},
                'control_available': False,
                'detail': _sanitize_gateway_message(f'Gateway remote health probe failed: {exc}'),
            }
        except (_urlerror.URLError, TimeoutError, OSError, ValueError) as exc:
            return {
                'phase': 'unavailable',
                'status_source': 'remote_health',
                'health': {'alive': None, 'state': 'unknown', 'reason': 'remote_health_unavailable'},
                'control_available': False,
                'detail': _sanitize_gateway_message(f'Gateway remote health probe failed: {exc}'),
            }
    return {
        'phase': 'stopped',
        'status_source': 'remote_health',
        'health': {'alive': False, 'state': 'down', 'reason': 'remote_health_down'},
        'control_available': False,
        'detail': 'Gateway remote health did not report a running gateway; lifecycle control is status-only.',
    }


def _read_stderr_tail(profile_home: Path, *, max_bytes: int = 5120) -> str:
    """Return the last `max_bytes` of the gateway stderr log, sanitized."""
    log_path = profile_home / '.gateway-stderr.log'
    if not log_path.exists():
        return ''
    try:
        size = log_path.stat().st_size
        with log_path.open('rb') as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            chunk = fh.read()
        text = chunk.decode('utf-8', errors='replace')
    except OSError:
        return ''
    return _sanitize_gateway_message(text)


def _phase_age_seconds(phase_started_at: str | None) -> float:
    """Seconds elapsed since phase_started_at; inf when missing/malformed."""
    if not isinstance(phase_started_at, str):
        return float('inf')
    import datetime as _dt
    try:
        # Accept trailing 'Z' or explicit offsets.
        normalized = phase_started_at.replace('Z', '+00:00')
        started = _dt.datetime.fromisoformat(normalized)
    except ValueError:
        return float('inf')
    if started.tzinfo is None:
        started = started.replace(tzinfo=_dt.timezone.utc)
    now = _dt.datetime.now(_dt.timezone.utc)
    return (now - started).total_seconds()


def profile_gateway_status_api(name: str) -> dict:
    """Return the current gateway phase for `name`, promoting transient
    phases when the world has caught up to them.

    Promotion rules (first match wins):
      * phase 'starting' + pid alive  -> 'running'
      * phase 'starting' + age >= grace + pid dead/missing -> 'failed'
      * phase 'stopping' + pid gone   -> 'stopped'
      * phase 'running'  + pid dead   -> 'stopped'  (post-running crash)
      * phase 'failed' or 'stopped'   -> as-is (sticky)

    Raises:
        ValueError: invalid profile name.
        FileNotFoundError: profile directory missing.
    """
    _validate_profile_settings_name(name)
    if _is_root_profile(name):
        profile_home = _DEFAULT_HERMES_HOME
    else:
        profile_home = _resolve_named_profile_home(name)
    if not profile_home.is_dir():
        raise FileNotFoundError(f"Profile '{name}' not found.")

    state = _read_gateway_state(profile_home)
    phase = state.get('phase')
    phase_started_at = state.get('phase_started_at')
    last_error = _sanitize_gateway_message(state.get('last_error') or '') or None
    desired_enabled = state.get('desired_enabled') if isinstance(state.get('desired_enabled'), bool) else None
    pid = _read_gateway_pid(profile_home)
    pid_alive = pid is not None
    runtime_status = _read_gateway_runtime_status(profile_home)
    runtime_phase, runtime_health, runtime_source, runtime_updated_at = _gateway_runtime_health(runtime_status)
    gateway_cfg = _gateway_control_config(profile_home)
    remote_status = _probe_gateway_remote_health(
        gateway_cfg.get('remote_health_url'),
        allow_service_hosts=bool(gateway_cfg.get('remote_health_allow_service')),
    )
    adapter_status = None
    try:
        adapter_status = _select_gateway_control_adapter(name, profile_home).status(profile_home, name)
    except Exception as exc:
        adapter_status = {
            'phase': 'unavailable',
            'status_source': 'adapter',
            'health': {'alive': None, 'state': 'unknown', 'reason': 'control_unavailable'},
            'control_available': False,
            'detail': _sanitize_gateway_message(str(exc)),
        }
    adapter_control_unavailable = (
        isinstance(adapter_status, dict)
        and adapter_status.get('control_available') is False
    )
    adapter_unavailable_detail = None
    if adapter_control_unavailable and isinstance(adapter_status, dict):
        adapter_unavailable_detail = adapter_status.get('detail')

    pid_health = {'alive': True, 'state': 'alive', 'reason': 'pid_alive'}
    down_health = {'alive': False, 'state': 'down', 'reason': 'gateway_not_running'}
    unknown_health = {'alive': None, 'state': 'unknown', 'reason': 'gateway_not_configured'}

    # Decisive alive signals win except while an explicit stop transition is in flight.
    if phase != 'stopping' and pid_alive:
        if phase in (None, '', 'starting'):
            _write_gateway_phase(profile_home, 'running', started_at=phase_started_at)
        return _status_payload(
            name, 'running', pid, None, phase_started_at,
            status_source='pid', health=pid_health, desired_enabled=desired_enabled,
            control_available=not adapter_control_unavailable,
            detail=adapter_unavailable_detail,
        )

    if phase != 'stopping' and runtime_phase == 'running':
        if phase in (None, '', 'starting'):
            _write_gateway_phase(profile_home, 'running', started_at=phase_started_at)
        return _status_payload(
            name, 'running', pid, None, phase_started_at,
            status_source=runtime_source or 'runtime_file', health=runtime_health,
            desired_enabled=desired_enabled, updated_at=runtime_updated_at,
            control_available=not adapter_control_unavailable,
            detail=adapter_unavailable_detail,
        )

    if phase != 'stopping' and isinstance(remote_status, dict) and remote_status.get('phase') == 'running':
        if phase in (None, '', 'starting'):
            _write_gateway_phase(profile_home, 'running', started_at=phase_started_at)
        return _status_payload(
            name, 'running', pid, None, phase_started_at,
            status_source='remote_health', health=remote_status.get('health'),
            desired_enabled=desired_enabled, updated_at=remote_status.get('updated_at'),
            control_available=False, detail=remote_status.get('detail'),
        )

    if isinstance(adapter_status, dict) and adapter_status.get('phase') == 'unavailable':
        return _status_payload(
            name, 'unavailable', pid, None, phase_started_at,
            status_source=adapter_status.get('status_source') or 'adapter',
            health=adapter_status.get('health'), control_available=False,
            desired_enabled=desired_enabled, detail=adapter_status.get('detail'),
        )

    if isinstance(remote_status, dict) and remote_status.get('phase') == 'unavailable' and runtime_phase is None:
        return _status_payload(
            name, 'unavailable', pid, None, phase_started_at,
            status_source='remote_health', health=remote_status.get('health'),
            control_available=False, desired_enabled=desired_enabled,
            detail=remote_status.get('detail'),
        )

    # No WebUI phase recorded -> infer from runtime evidence or no evidence.
    if not phase:
        if runtime_phase == 'unknown':
            # The gateway last self-reported running, but its heartbeat is
            # stale (>120s old) and we have no live PID. Either the gateway
            # process is gone or its heartbeat is broken; in both cases the
            # actionable UI state is 'stopped', not 'unknown'. Reporting
            # 'unknown' here would re-trap the profile in "Check Status"
            # after the running->stopped reconciliation below clears phase.
            return _status_payload(
                name, 'stopped', None, None, None,
                status_source=runtime_source or 'runtime_file', health=down_health,
                desired_enabled=desired_enabled if desired_enabled is not None else False,
                updated_at=runtime_updated_at,
                detail='Gateway last reported running, but the heartbeat is stale and no live PID is visible.',
            )
        if runtime_phase == 'stopped':
            return _status_payload(
                name, 'stopped', None, None, None,
                status_source=runtime_source or 'runtime_file', health=runtime_health,
                desired_enabled=desired_enabled, updated_at=runtime_updated_at,
            )
        return _status_payload(
            name, 'stopped', None, None, None,
            status_source='none', health=unknown_health, desired_enabled=desired_enabled,
        )

    if phase == 'starting':
        # NOTE: previously this branch promoted 'starting' to 'unknown' as soon
        # as the runtime file was stale, on the theory that we lacked a
        # positive liveness signal. In practice the runtime file is *expected*
        # to be stale immediately after a fresh start — the brand-new gateway
        # hasn't ticked yet and any pre-existing gateway_state.json is from a
        # prior (dead) process. Promoting to 'unknown' surfaces a scary
        # "runtime file is stale" detail to the user during what should be a
        # normal "Starting…" window. Stay at 'starting' through the grace
        # window; the natural failure path below escalates to 'failed' if no
        # live PID materializes in time.
        if _phase_age_seconds(phase_started_at) >= GATEWAY_START_GRACE_SECONDS:
            tail = _read_stderr_tail(profile_home)
            err = tail if tail else 'gateway failed to start within grace window'
            _write_gateway_phase(profile_home, 'failed', last_error=err)
            return _status_payload(
                name, 'failed', pid, err, phase_started_at,
                status_source='state_file', health=down_health, desired_enabled=False,
                detail=err,
            )
        return _status_payload(
            name, 'starting', pid, None, phase_started_at,
            status_source='state_file', health={'alive': None, 'state': 'unknown', 'reason': 'starting'},
            desired_enabled=True,
        )

    if phase == 'stopping':
        if not pid_alive and runtime_phase != 'running':
            _write_gateway_phase(profile_home, 'stopped')
            return _status_payload(
                name, 'stopped', None, None, None,
                status_source=runtime_source or 'state_file', health=runtime_health or down_health,
                desired_enabled=False, updated_at=runtime_updated_at,
            )
        if _phase_age_seconds(phase_started_at) >= GATEWAY_STOP_GRACE_SECONDS:
            detail = 'Gateway is still running after the stop grace window; status refreshed so controls are available.'
            if pid_alive:
                _write_gateway_phase(profile_home, 'running')
                return _status_payload(
                    name, 'running', pid, None, None,
                    status_source='pid', health=pid_health, desired_enabled=True,
                    control_available=not adapter_control_unavailable,
                    detail=adapter_unavailable_detail or detail,
                )
            if runtime_phase == 'running':
                _write_gateway_phase(profile_home, 'running')
                return _status_payload(
                    name, 'running', pid, None, None,
                    status_source=runtime_source or 'runtime_file', health=runtime_health,
                    desired_enabled=True, updated_at=runtime_updated_at,
                    control_available=not adapter_control_unavailable,
                    detail=adapter_unavailable_detail or detail,
                )
        return _status_payload(
            name, 'stopping', pid, None, phase_started_at,
            status_source='state_file', health={'alive': None, 'state': 'unknown', 'reason': 'stopping'},
            desired_enabled=False,
        )

    if phase == 'running':
        # A stale gateway_state.json + state.phase=running with no live PID is
        # most commonly a WebUI restart that took the gateway subprocess with
        # it, or a cross-container gateway whose heartbeat has been silent
        # for >120s. Either way the recorded "running" belief is no longer
        # backed by a positive liveness signal, so reconcile to 'stopped' and
        # persist — leaving the profile pinned in 'unknown' forever (the
        # previous behavior) traps the UI on "Check Status" with no escape.
        _write_gateway_phase(profile_home, 'stopped')
        if runtime_phase == 'unknown':
            return _status_payload(
                name, 'stopped', None, None, None,
                status_source=runtime_source or 'runtime_file',
                health=down_health, desired_enabled=False,
                updated_at=runtime_updated_at,
                detail='Gateway last reported running, but the heartbeat is stale and no live PID is visible; resetting to stopped.',
            )
        # Post-running crash/no evidence — drop to stopped, not failed.
        return _status_payload(
            name, 'stopped', None, None, None,
            status_source=runtime_source or 'state_file', health=runtime_health or down_health,
            desired_enabled=False, updated_at=runtime_updated_at,
        )

    if phase == 'failed':
        return _status_payload(
            name, 'failed', pid, last_error, phase_started_at,
            status_source='state_file', health=down_health,
            desired_enabled=False, detail=last_error,
        )

    # Unknown phase string -> treat as stopped (defensive).
    _write_gateway_phase(profile_home, 'stopped')
    return _status_payload(
        name, 'stopped', None, None, None,
        status_source='state_file', health=down_health, desired_enabled=False,
    )


def _status_payload(
    name: str,
    phase: str,
    pid: int | None,
    last_error: str | None,
    phase_started_at: str | None,
    *,
    status_source: str = 'none',
    health: dict | None = None,
    control_available: bool = True,
    desired_enabled: bool | None = None,
    updated_at: str | None = None,
    detail: str | None = None,
) -> dict:
    safe_last_error = _sanitize_gateway_message(last_error or '') or None
    safe_detail = _sanitize_gateway_message(detail or '') or None
    if desired_enabled is None:
        desired_enabled = phase in ('running', 'starting', 'stopping')
    payload = {
        'ok': True,
        'profile': name,
        'phase': phase,
        'pid': pid,
        'last_error': safe_last_error,
        'phase_started_at': phase_started_at,
        'status_source': status_source,
        'health': health or {'alive': None, 'state': 'unknown', 'reason': 'not_configured'},
        'control_available': control_available,
        'desired_enabled': desired_enabled,
        'updated_at': updated_at,
        'detail': safe_detail,
    }
    return payload


def _gateway_control_failure_message(name: str, action: str, raw_message: str) -> str:
    """Return a sanitized, actionable message for profile gateway failures."""
    conflict = _telegram_token_profile_conflict_detail(name, action, raw_message)
    if conflict:
        return conflict
    return _sanitize_gateway_message(raw_message)


def _telegram_token_profile_conflict_detail(name: str, action: str, raw_message: str) -> str | None:
    """Classify Telegram token-lock failures as profile conflicts.

    The Hermes Gateway uses a lock named ``telegram-bot-token_lock`` to prevent
    duplicate long-polling with the same Telegram bot token. Raw lock errors can
    include token fragments, so replace them with copyable remediation text.
    """
    text = (raw_message or '').lower()
    if 'telegram-bot-token_lock' not in text:
        return None
    return (
        f"Profile '{name}' could not start the Telegram Gateway because its "
        "Telegram bot token is already in use by another Gateway/profile. "
        "Stop the other profile's Gateway, or configure a unique "
        "TELEGRAM_BOT_TOKEN for this profile, then retry."
    )


def profile_gateway_control_api(name: str, action: str) -> dict:
    """Start or stop the gateway for a named profile.

    Writes phase ('starting' or 'stopping') to .gateway-state.json BEFORE
    invoking the runner so a concurrent status poll sees the transient
    phase even if the runner blocks. On failure the phase becomes
    'failed' and the sanitized exception is stored as last_error.

    The 'restart' action is no longer accepted — clients should issue
    stop then start (the toggle UX does this implicitly).
    """
    _validate_profile_settings_name(name)
    action = (action or '').strip().lower()
    if action not in ('start', 'stop'):
        raise ValueError("action must be one of: start, stop")

    if _is_root_profile(name):
        profile_home = _DEFAULT_HERMES_HOME
    else:
        profile_home = _resolve_named_profile_home(name)
    if not profile_home.is_dir():
        raise FileNotFoundError(f"Profile '{name}' not found.")

    # Stamp the transient phase BEFORE invoking the runner so a racing
    # status poll observes the in-flight transition.
    transient_phase = 'starting' if action == 'start' else 'stopping'
    _write_gateway_phase(profile_home, transient_phase)

    def _record_failure(exc: Exception) -> dict:
        message = _gateway_control_failure_message(name, action, str(exc))
        _write_gateway_phase(profile_home, 'failed', last_error=message)
        return {
            'ok': False,
            'profile': name,
            'action': action,
            'phase': 'failed',
            'running': False,
            'configured': False,
            'message': message,
        }

    if _gateway_control_hook is not None:
        try:
            hook_result = _gateway_control_hook(name, action)
        except Exception as exc:  # noqa: BLE001 — surface any test-injected failure
            return _record_failure(exc)
        if not isinstance(hook_result, dict):
            hook_result = {'ok': True, 'running': action != 'stop'}
        hook_result.setdefault('ok', True)
        hook_result.setdefault('profile', name)
        hook_result.setdefault('action', action)
        hook_result.setdefault('configured', True)
        # For a successful stop the handler is synchronous: by the time we
        # return, the PID has been signalled and reaped. Persist phase='stopped'
        # immediately so a racing status poll does not observe a stale
        # 'stopping' stamp and flip the UI back from Off to Stopping.
        if action == 'stop' and hook_result.get('ok'):
            _write_gateway_phase(profile_home, 'stopped')
            hook_result['phase'] = 'stopped'
        else:
            hook_result['phase'] = transient_phase
        if hook_result.get('ok') and action == 'start':
            _write_gateway_last_run(profile_home)
        return hook_result

    try:
        result = _default_gateway_control(name, action)
    except Exception as exc:  # noqa: BLE001 — keep error surface narrow + safe
        return _record_failure(exc)
    if not isinstance(result, dict):
        result = {'ok': True, 'running': action != 'stop'}
    result.setdefault('ok', True)
    result.setdefault('profile', name)
    result.setdefault('action', action)
    result.setdefault('configured', True)
    # See hook branch above: persist 'stopped' eagerly on a successful stop so
    # the response is authoritative and a concurrent poll cannot read back
    # a transient 'stopping' stamp.
    if action == 'stop' and result.get('ok'):
        _write_gateway_phase(profile_home, 'stopped')
        result['phase'] = 'stopped'
    else:
        result['phase'] = transient_phase
    if result.get('ok') and action == 'start':
        _write_gateway_last_run(profile_home)
    return result


def _write_gateway_last_run(profile_home: Path) -> None:
    """Stamp ``.gateway-state.json`` with last_run_at on a successful start.

    Best-effort: never fail the gateway action because the state write
    failed. The activity line reads this back via :func:`_read_gateway_state`.
    """
    import datetime as _dt
    state_path = profile_home / '.gateway-state.json'
    try:
        payload: dict = {}
        if state_path.exists():
            try:
                existing = json.loads(state_path.read_text(encoding='utf-8'))
                if isinstance(existing, dict):
                    payload = existing
            except (ValueError, OSError):
                payload = {}
        payload['last_run_at'] = (
            _dt.datetime.now(_dt.timezone.utc)
            .isoformat()
            .replace('+00:00', 'Z')
        )
        state_path.write_text(json.dumps(payload), encoding='utf-8')
    except OSError:
        logger.debug("Failed to write gateway last_run_at state", exc_info=True)


_SECRET_PATTERN = re.compile(
    r'(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S+'
)
_AUTH_HEADER_PATTERN = re.compile(
    r'(?i)\bauthorization\s*:\s*(?:bearer|token|basic)?\s*[^\s\'\"]+'
)
_CLI_SECRET_PATTERN = re.compile(
    r'(?i)(--(?:api[-_]?key|token|secret|password))(?:\s+|=)(?:"[^"]*"|\'[^\']*\'|\S+)'
)
_TELEGRAM_BOT_TOKEN_VALUE_PATTERN = re.compile(
    r'\b\d{5,}:[A-Za-z0-9_-]{10,}\b'
)


def _sanitize_gateway_message(message: str) -> str:
    """Strip obviously secret-looking substrings from gateway runner output.

    Truncates to the LAST 500 chars — for a stderr tail the final lines
    carry the actual exit reason (e.g. 'No messaging platforms enabled'),
    while the head of the buffer is usually box-drawing noise or stale
    output from prior runs. Redaction runs on the full string first so
    secrets are not preserved by the slice.
    """
    if not message:
        return ''
    text = _AUTH_HEADER_PATTERN.sub('Authorization: [redacted]', message)
    text = _CLI_SECRET_PATTERN.sub(lambda match: f'{match.group(1)} [redacted]', text)
    text = _SECRET_PATTERN.sub(r'\1=[redacted]', text)
    text = _TELEGRAM_BOT_TOKEN_VALUE_PATTERN.sub('[redacted]', text)
    return text[-500:]


def delete_profile_api(name: str) -> dict:
    """Delete a profile. Switches to default first if it's the active one."""
    if _is_root_profile(name):
        raise ValueError("Cannot delete the default profile.")
    _validate_profile_name(name)

    # If deleting the active profile, switch to default first
    if _active_profile == name:
        try:
            switch_profile('default')
        except RuntimeError:
            raise RuntimeError(
                f"Cannot delete active profile '{name}' while an agent is running. "
                "Cancel or wait for it to finish."
            )

    try:
        from hermes_cli.profiles import delete_profile
        delete_profile(name, yes=True)
    except ImportError:
        # Manual fallback: just remove the directory
        import shutil
        profile_dir = _resolve_named_profile_home(name)
        if profile_dir.is_dir():
            shutil.rmtree(str(profile_dir))
        else:
            raise ValueError(f"Profile '{name}' does not exist.")

    # Drop cached root-profile-name lookup — list_profiles_api() shape changed.
    _invalidate_root_profile_cache()
    return {'ok': True, 'name': name}


# ── Per-profile messaging-platform configuration ──────────────────────────
#
# Three helpers backing the WebUI's per-profile platform endpoints:
#
#   _list_platforms_for_profile(profile_name)        -> GET shape
#   _set_platform_for_profile(profile_name, key, values) -> POST shape
#   _clear_platform_for_profile(profile_name, key)   -> DELETE shape
#
# All three brackets HERMES_HOME via cron_profile_context_for_home so
# `_platform_status()` reads the right profile's .env. Writes go through a
# small parse-modify-serialize helper here (not hermes_cli.config.save_env_value)
# so we round-trip user comments and blank lines bit-for-bit, and so we can
# stay within stdlib without forcing an import path through hermes_cli for
# every write.

_ENV_VAR_NAME_RE = re.compile(r'^[A-Z_][A-Z0-9_]*$')

# Map hermes _platform_status() free-text return values to the 3-state
# contract the frontend expects.
_STATUS_TEXT_TO_CONTRACT = {
    'configured': 'configured',
    'configured + paired': 'configured',
    'enabled, not paired': 'partial',
    'partially configured': 'partial',
    'not configured': 'not_configured',
}


def _normalize_platform_status(text: str) -> str:
    """Reduce an `_platform_status()` free-text label to the API contract.

    Handles known suffixes (e.g. ``"configured + E2EE"``) by prefix-match.
    Anything unrecognised maps to ``not_configured`` (the conservative default).
    """
    if not isinstance(text, str):
        return 'not_configured'
    raw = text.strip()
    if not raw:
        return 'not_configured'
    if raw in _STATUS_TEXT_TO_CONTRACT:
        return _STATUS_TEXT_TO_CONTRACT[raw]
    # Suffix-tolerant: "configured + E2EE", "configured + something".
    if raw.startswith('configured'):
        return 'configured'
    if raw.startswith('partially'):
        return 'partial'
    return 'not_configured'


def _get_platforms_module():
    """Return the hermes-agent gateway module providing platforms metadata.

    Isolated into its own indirection so tests can monkey-patch the import
    path without forcing `hermes_cli` onto `sys.path`. Raises ImportError
    when hermes-agent is not installed; callers catch and return
    ``{"ok": False, "message": "hermes-agent not available"}``.
    """
    from hermes_cli import gateway as _gw  # noqa: WPS433
    return _gw


def _resolve_profile_home_strict(name: str) -> Path:
    """Validate name + resolve to profile home; raise FileNotFoundError if missing.

    Mirrors the bracket pattern from `profile_gateway_status_api`.
    """
    _validate_profile_settings_name(name)
    if _is_root_profile(name):
        profile_home = _DEFAULT_HERMES_HOME
    else:
        profile_home = _resolve_named_profile_home(name)
    if not profile_home.is_dir():
        raise FileNotFoundError(f"Profile '{name}' not found.")
    return profile_home


def _read_env_file(env_path: Path) -> 'list[tuple[str, str, str]]':
    """Read .env as a list of (kind, key, raw_line) tuples preserving order.

    kind is one of:
      * 'kv'      — key=value line. key is the env name, raw_line is the full
                    line text including trailing newline (or "" for last-line-no-nl).
      * 'other'   — blank line or comment. key is "", raw_line is the line text.

    Used by the write/clear helpers to round-trip the file: edit in place,
    drop / append keys, then re-serialise without losing user comments or
    blank lines.
    """
    out: list[tuple[str, str, str]] = []
    if not env_path.exists():
        return out
    try:
        text = env_path.read_text(encoding='utf-8-sig', errors='replace')
    except OSError:
        return out
    # splitlines(keepends=True) keeps each line's terminator so re-join is exact.
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped or stripped.startswith('#') or '=' not in stripped:
            out.append(('other', '', line))
            continue
        key, _ = stripped.split('=', 1)
        key = key.strip()
        if not _ENV_VAR_NAME_RE.fullmatch(key):
            out.append(('other', '', line))
            continue
        out.append(('kv', key, line))
    return out


def _env_value_for(records: list, key: str) -> Optional[str]:
    """Return the current value for *key* in a parsed .env record list."""
    for kind, k, line in records:
        if kind == 'kv' and k == key:
            stripped = line.strip()
            _, v = stripped.split('=', 1)
            return v.strip().strip('"').strip("'")
    return None


def _format_env_line(key: str, value: str) -> str:
    """Render a single KEY=VALUE line. Strips embedded newlines for safety."""
    safe = (value or '').replace('\r', '').replace('\n', '')
    return f"{key}={safe}\n"


def _write_env_atomic(env_path: Path, records: 'list[tuple[str, str, str]]') -> None:
    """Serialise records back to *env_path* atomically.

    Preserves original file permissions when present (mirrors hermes_cli
    `save_env_value`).
    """
    import stat
    import tempfile

    env_path.parent.mkdir(parents=True, exist_ok=True)
    payload = ''.join(line for _kind, _k, line in records)
    # Make sure the file ends with a newline.
    if payload and not payload.endswith('\n'):
        payload += '\n'

    original_mode = None
    if env_path.exists():
        try:
            original_mode = stat.S_IMODE(env_path.stat().st_mode)
        except OSError:
            original_mode = None

    fd, tmp_path = tempfile.mkstemp(dir=str(env_path.parent),
                                    suffix='.tmp', prefix='.env_')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(payload)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_path, env_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    if original_mode is not None:
        try:
            os.chmod(env_path, original_mode)
        except OSError:
            pass


def _apply_env_updates(env_path: Path,
                       updates: 'dict[str, Optional[str]]') -> None:
    """Apply a dict of {KEY: value_or_None} to .env, preserving siblings.

    Semantics:
      * ``value is None``  -> remove the key from the file (no-op if absent).
      * ``value == ""``    -> stored as ``KEY=`` (caller decides whether to use).
      * otherwise          -> set / replace the value.

    Order preservation: keys that already exist are updated in place; new
    keys are appended in the order they appear in *updates*.
    """
    records = _read_env_file(env_path)
    seen_keys = {k for kind, k, _ in records if kind == 'kv'}

    # Updates split into (existing-keys to update/remove) and (new keys to append).
    to_append: list[tuple[str, str]] = []
    for key, value in updates.items():
        if not _ENV_VAR_NAME_RE.fullmatch(key):
            raise ValueError(f"Invalid environment variable name: {key!r}")
        if key in seen_keys:
            continue
        if value is not None:
            to_append.append((key, value))

    # Walk records, updating or dropping in place.
    new_records: list[tuple[str, str, str]] = []
    for kind, k, line in records:
        if kind != 'kv' or k not in updates:
            new_records.append((kind, k, line))
            continue
        new_value = updates[k]
        if new_value is None:
            # Drop the line entirely.
            continue
        new_records.append((kind, k, _format_env_line(k, new_value)))

    for key, value in to_append:
        new_records.append(('kv', key, _format_env_line(key, value)))

    _write_env_atomic(env_path, new_records)


def _normalize_allowlist_value(raw: str) -> str:
    """Strip + dedupe a comma-separated allowlist while preserving order.

    Mirrors the spirit of hermes_cli's per-platform setup helpers without
    coupling to their per-platform regexes — those validate format, this
    only canonicalises whitespace + dedupes. Server-side trim so we don't
    trust client-side cleanup.
    """
    if not isinstance(raw, str):
        return ''
    seen: set[str] = set()
    out: list[str] = []
    for tok in raw.split(','):
        tok = tok.strip()
        if not tok or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return ','.join(out)


def _platform_by_key(gw_module, key: str) -> Optional[dict]:
    """Locate a single platform dict by `key` in `_all_platforms()`."""
    try:
        platforms = gw_module._all_platforms()
    except Exception:
        return None
    for p in platforms:
        if p.get('key') == key:
            return p
    return None


def _declared_keys_for_platform(platform: dict) -> list[str]:
    """Return the env-var names the schema declares for *platform*.

    Built-in platforms expose `vars` (list of dicts with `name`); plugin
    platforms expose `_registry_entry.required_env`.
    """
    if platform.get('_registry_entry') is not None:
        entry = platform['_registry_entry']
        req = getattr(entry, 'required_env', None) or []
        return [str(k) for k in req]
    out: list[str] = []
    for var in platform.get('vars') or []:
        name = var.get('name')
        if isinstance(name, str) and name:
            out.append(name)
    return out


def _list_platforms_for_profile(profile_name: str) -> dict:
    """GET /api/profile/gateway/platforms?name=<profile_name>.

    Raises:
        ValueError: invalid profile name.
        FileNotFoundError: profile directory missing.
    """
    profile_home = _resolve_profile_home_strict(profile_name)

    try:
        gw = _get_platforms_module()
    except ImportError:
        return {'ok': False, 'message': 'hermes-agent not available'}

    env_path = profile_home / '.env'
    env_records = _read_env_file(env_path)
    env_values: dict[str, str] = {}
    for kind, k, line in env_records:
        if kind == 'kv':
            _, v = line.strip().split('=', 1)
            env_values[k] = v.strip().strip('"').strip("'")

    with cron_profile_context_for_home(profile_home):
        try:
            raw_platforms = gw._all_platforms()
        except Exception as exc:
            logger.debug("platforms enumeration failed: %s", exc, exc_info=True)
            raw_platforms = []
        status_fn = getattr(gw, '_platform_status', None)

        out_platforms: list[dict] = []
        for p in raw_platforms:
            try:
                raw_status = status_fn(p) if callable(status_fn) else 'not configured'
            except Exception:
                raw_status = 'not configured'
            status = _normalize_platform_status(raw_status)

            entry = p.get('_registry_entry')
            if entry is not None:
                required_env = [str(k) for k in (getattr(entry, 'required_env', None) or [])]
                set_env = [k for k in required_env if env_values.get(k)]
                out_platforms.append({
                    'key': p.get('key'),
                    'label': p.get('label'),
                    'emoji': p.get('emoji'),
                    'status': status,
                    'is_plugin': True,
                    'required_env': required_env,
                    'set_env': set_env,
                })
                continue

            # Built-in platform: build the rich `vars` list.
            vars_out: list[dict] = []
            for var in p.get('vars') or []:
                name = var.get('name')
                if not isinstance(name, str) or not name:
                    continue
                is_password = bool(var.get('password'))
                # `optional` truthy → required is False. Default to required.
                required = not bool(var.get('optional'))
                current = env_values.get(name)
                entry_out: dict = {
                    'name': name,
                    'prompt': var.get('prompt') or name,
                    'password': is_password,
                    'help': var.get('help') or '',
                    'required': required,
                    'is_set': bool(current),
                }
                if var.get('is_allowlist'):
                    entry_out['is_allowlist'] = True
                if not is_password:
                    # Non-password vars round-trip the current value (may be empty string).
                    entry_out['value'] = current if current is not None else ''
                vars_out.append(entry_out)

            out_platforms.append({
                'key': p.get('key'),
                'label': p.get('label'),
                'emoji': p.get('emoji'),
                'status': status,
                'is_plugin': False,
                'vars': vars_out,
            })

    return {
        'ok': True,
        'profile': profile_name,
        'platforms': out_platforms,
    }


def _set_platform_for_profile(profile_name: str,
                              platform_key: str,
                              values: dict) -> dict:
    """POST /api/profile/gateway/platform?name=<profile_name>.

    Raises:
        ValueError: invalid profile name, unknown platform key, or unknown
            value keys (validation messages name the offenders).
        FileNotFoundError: profile directory missing.
    """
    profile_home = _resolve_profile_home_strict(profile_name)

    try:
        gw = _get_platforms_module()
    except ImportError:
        return {'ok': False, 'message': 'hermes-agent not available'}

    if not isinstance(values, dict):
        raise ValueError("values must be a dict of {KEY: string}")

    if not isinstance(platform_key, str) or not platform_key:
        raise ValueError("platform key is required")

    platform = _platform_by_key(gw, platform_key)
    if platform is None:
        raise ValueError(f"Unknown platform key: {platform_key!r}")

    declared = _declared_keys_for_platform(platform)
    declared_set = set(declared)
    submitted_keys = list(values.keys())
    unknown = [k for k in submitted_keys if k not in declared_set]
    if unknown:
        raise ValueError(
            f"Unknown values keys for platform {platform_key!r}: "
            f"{sorted(unknown)!r}"
        )

    # Build the per-field intent: empty-password = no change (omit key),
    # empty-non-password = remove key (None sentinel), otherwise set.
    is_plugin = platform.get('_registry_entry') is not None

    # Index var schemas by name for built-in lookup.
    var_index: dict[str, dict] = {}
    if not is_plugin:
        for var in platform.get('vars') or []:
            name = var.get('name')
            if isinstance(name, str) and name:
                var_index[name] = var

    updates: dict[str, Optional[str]] = {}
    for key in declared:
        if key not in values:
            continue
        raw = values[key]
        if not isinstance(raw, str):
            raw = '' if raw is None else str(raw)

        is_password = False
        is_allowlist = False
        if not is_plugin:
            spec = var_index.get(key, {})
            is_password = bool(spec.get('password'))
            is_allowlist = bool(spec.get('is_allowlist'))

        if raw == '':
            if is_password:
                # No-change: skip the update entirely.
                continue
            # Remove key from .env.
            updates[key] = None
            continue

        if is_allowlist:
            raw = _normalize_allowlist_value(raw)

        updates[key] = raw

    env_path = profile_home / '.env'
    with cron_profile_context_for_home(profile_home):
        if updates:
            _apply_env_updates(env_path, updates)
        # Recompute status (the bracketed context lets _platform_status
        # see the freshly-written values).
        status_fn = getattr(gw, '_platform_status', None)
        try:
            raw_status = status_fn(platform) if callable(status_fn) else 'not configured'
        except Exception:
            raw_status = 'not configured'

    return {
        'ok': True,
        'profile': profile_name,
        'platform': platform_key,
        'status': _normalize_platform_status(raw_status),
    }


def _clear_platform_for_profile(profile_name: str, platform_key: str) -> dict:
    """DELETE /api/profile/gateway/platform?name=<profile_name>&platform=<key>.

    Removes every key declared by the platform schema. Sibling keys
    (other platforms' creds, unrelated env vars) are preserved.

    Raises:
        ValueError: invalid profile name or unknown platform key.
        FileNotFoundError: profile directory missing.
    """
    profile_home = _resolve_profile_home_strict(profile_name)

    try:
        gw = _get_platforms_module()
    except ImportError:
        return {'ok': False, 'message': 'hermes-agent not available'}

    if not isinstance(platform_key, str) or not platform_key:
        raise ValueError("platform key is required")

    platform = _platform_by_key(gw, platform_key)
    if platform is None:
        raise ValueError(f"Unknown platform key: {platform_key!r}")

    declared = _declared_keys_for_platform(platform)
    updates: dict[str, Optional[str]] = {k: None for k in declared}

    env_path = profile_home / '.env'
    with cron_profile_context_for_home(profile_home):
        if updates:
            _apply_env_updates(env_path, updates)

    return {
        'ok': True,
        'profile': profile_name,
        'platform': platform_key,
        'status': 'not_configured',
    }
