"""
Hermes Web UI -- Profile state management.
Wraps hermes_cli.profiles to provide profile switching for the web UI.

The web UI maintains a process-level "active profile" that determines which
HERMES_HOME directory is used for config, skills, memory, cron, and API keys.
Profile switches update os.environ['HERMES_HOME'] and monkey-patch module-level
cached paths in hermes-agent modules (skills_tool, skill_manager_tool,
cron/jobs) that snapshot HERMES_HOME at import time.
"""
import json
import logging
import os
import re
import shutil
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

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
        with cron_profile_context_for_home(_home_for_scheduled_cron_job(job)):
            return original(job, *args, **kwargs)

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
        # Lazy import avoids a module-load cycle: streaming imports this helper.
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

    env_keys = set(runtime_env.keys()) | {"HERMES_HOME"}
    # Stage-360 maintainer fix: narrow the _ENV_LOCK critical section to just
    # the env mutation (and the env restoration). Pre-fix, this held _ENV_LOCK
    # for the entire `yield` duration — i.e. the whole background worker's
    # runtime (title generation, compression, update summary). That caused
    # _ENV_LOCK to be held for many seconds, blocking ALL other sessions and
    # surfacing as the QA `test_third_message_completes` timeout. The fix
    # mirrors the narrow-lock pattern in _run_agent_streaming: acquire briefly
    # to set env, run worker without holding the lock, reacquire to restore.
    # See also QA `test_finally_restores_env_with_lock`.
    skill_home_snapshot = None
    old_env = {}
    try:
        with _ENV_LOCK:
            old_env = {key: os.environ.get(key) for key in env_keys}
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
        with _ENV_LOCK:
            for key, old_value in old_env.items():
                if old_value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old_value
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
    default_model = None
    if isinstance(model_cfg, str):
        default_model = model_cfg
    elif isinstance(model_cfg, dict):
        default_model = model_cfg.get('default')

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
        'default_workspace': default_workspace,
    }


def list_profiles_api() -> list:
    """List all profiles with metadata, serialized for JSON response."""
    try:
        from hermes_cli.profiles import list_profiles
        infos = list_profiles()
    except ImportError:
        # hermes_cli not available -- return just the default
        return [_default_profile_dict()]

    active = get_active_profile_name()
    result = []
    for p in infos:
        result.append({
            'name': p.name,
            'path': str(p.path),
            'is_default': p.is_default,
            'is_active': p.name == active,
            'gateway_running': p.gateway_running,
            'model': p.model,
            'provider': p.provider,
            'has_env': p.has_env,
            'skill_count': p.skill_count,
        })
    return result


def _default_profile_dict() -> dict:
    """Fallback profile dict when hermes_cli is not importable."""
    return {
        'name': 'default',
        'path': str(_DEFAULT_HERMES_HOME),
        'is_default': True,
        'is_active': True,
        'gateway_running': False,
        'model': None,
        'provider': None,
        'has_env': (_DEFAULT_HERMES_HOME / '.env').exists(),
        'skill_count': 0,
    }


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
