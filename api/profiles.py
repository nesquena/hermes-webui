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

import yaml

from api.paths import _atomic_write_text
from api.session_events import publish_session_list_changed

logger = logging.getLogger(__name__)

# ── Constants (match hermes_cli.profiles upstream) ─────────────────────────
_PROFILE_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
_PROFILE_DIRS = [
    'memories', 'sessions', 'skills', 'skins',
    'logs', 'plans', 'workspace', 'cron',
]
_CLONE_CONFIG_FILES = ['config.yaml', '.env', 'SOUL.md']

# ── Snapshot startup env before profile init / dotenv reload mutates it ───────
# _is_isolated_profile_mode() needs startup HERMES_HOME, not the value after
# init_profile_state() rewrites it. The opt-in flag is also an operator-level
# startup control: a pinned profile's .env may be loaded into live os.environ
# later, but must not be able to change whether the process is isolated.
_INITIAL_HERMES_HOME = os.getenv('HERMES_HOME', '').strip()
_INITIAL_ISOLATED_PROFILE_OPT_IN = os.getenv('HERMES_WEBUI_ISOLATED_PROFILE', '').strip().lower()
_ISOLATED_SYMLINK_WARNING_EMITTED = False
_ISOLATED_PROFILE_SHAPE_WITHOUT_OPT_IN_WARNING_EMITTED = False
_ISOLATED_PROFILE_TRUTHY_VALUES = frozenset({'1', 'true', 'yes', 'on'})

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
_SKILL_HOME_MODULE_PATCH_LOCK = threading.RLock()


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


def _skill_modules_support_profile_home(profile_home: Path) -> bool:
    """Return ``True`` when both skill modules resolve to this profile home."""
    expected_dir = (Path(profile_home) / 'skills').expanduser()

    for module_name in _SKILL_HOME_MODULES:
        module = sys.modules.get(module_name)
        if module is None:
            logger.debug("Skill capability check: %s not pre-imported in sys.modules", module_name)
            return False

        if not hasattr(module, 'SKILLS_DIR'):
            logger.debug("Skill capability check: %s missing SKILLS_DIR", module_name)
            return False

        if not hasattr(module, '_SKILLS_DIR_AT_IMPORT'):
            logger.debug("Skill capability check: %s missing _SKILLS_DIR_AT_IMPORT", module_name)
            return False

        try:
            current_skills_dir = Path(module.SKILLS_DIR).expanduser()
            import_skills_dir = Path(module._SKILLS_DIR_AT_IMPORT).expanduser()
        except Exception:
            logger.debug(
                "Skill capability check: %s has invalid SKILLS_DIR or _SKILLS_DIR_AT_IMPORT",
                module_name,
                exc_info=True,
            )
            return False

        if current_skills_dir != import_skills_dir:
            logger.debug(
                "Skill capability check: %s.SKILLS_DIR %r does not match imported baseline %r",
                module_name,
                current_skills_dir,
                import_skills_dir,
            )
            return False

        skills_dir = getattr(module, '_skills_dir', None)
        if not callable(skills_dir):
            logger.debug("Skill capability check: %s._skills_dir is not callable", module_name)
            return False

        try:
            resolved = Path(skills_dir()).expanduser()
        except Exception:
            logger.debug(
                "Skill capability check: %s._skills_dir() failed",
                module_name,
                exc_info=True,
            )
            return False

        if resolved != expected_dir:
            logger.debug(
                "Skill capability check: %s resolves %r instead of %r",
                module_name,
                str(resolved),
                str(expected_dir),
            )
            return False

    return True


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


# Env keys a pinned profile's .env may NOT override via _reload_dotenv() — these
# are operator/deployment-level postures, not per-profile toggles. Letting a
# profile .env set HERMES_WEBUI_ISOLATED_PROFILE=0 would let a contained user
# escape isolation (#4589).
_PROTECTED_ENV_KEYS = frozenset({'HERMES_WEBUI_ISOLATED_PROFILE'})


def _isolated_profile_opt_in() -> bool:
    """Return True only when isolated single-profile mode is EXPLICITLY enabled.
    Isolated mode is an intentional multi-user deployment posture (each user is
    pinned to one profile and cross-profile operations are rejected). It must be
    opted into with ``HERMES_WEBUI_ISOLATED_PROFILE`` — it is NEVER inferred from
    the ``HERMES_HOME`` shape alone, because a normal single-user who runs under a
    named profile produces the byte-identical ``*/profiles/<name>`` shape (the
    Hermes Agent launcher exports ``HERMES_HOME=~/.hermes/profiles/<name>`` for any
    active named profile). Keying isolation off the shape alone therefore breaks
    profile switching for ordinary single-user deployments (#4586).

    Accepts the usual truthy values; default (unset/empty/falsey) is OFF.

    Security: this reads the startup snapshot, not live ``os.environ``. A pinned
    profile's ``.env`` is loaded after import, so live env can be profile-owned;
    the opt-in must remain the operator/launcher posture captured at process
    start (#4590). ``_reload_dotenv()`` and the runtime env paths still filter the
    key as defense-in-depth, but detection does not depend on that filtering.
    """
    return _INITIAL_ISOLATED_PROFILE_OPT_IN in _ISOLATED_PROFILE_TRUTHY_VALUES


def _warn_if_profile_shape_without_isolated_opt_in() -> None:
    """Log once when HERMES_HOME looks pinned but startup opt-in is absent."""
    global _ISOLATED_PROFILE_SHAPE_WITHOUT_OPT_IN_WARNING_EMITTED
    if _ISOLATED_PROFILE_SHAPE_WITHOUT_OPT_IN_WARNING_EMITTED:
        return
    hermes_home = _INITIAL_HERMES_HOME
    if not hermes_home:
        return
    p = Path(hermes_home).expanduser()
    if p.parent.name != 'profiles' or not p.name:
        return
    logger.warning(
        "HERMES_HOME points at a profile directory (%s), but "
        "HERMES_WEBUI_ISOLATED_PROFILE was not enabled at startup; isolated "
        "profile mode stays off and normal multi-profile switching remains enabled.",
        p,
    )
    _ISOLATED_PROFILE_SHAPE_WITHOUT_OPT_IN_WARNING_EMITTED = True


def _is_isolated_profile_mode() -> bool:
    """Detect isolated single-profile mode.

    Returns True only when BOTH conditions hold:
      1. ``HERMES_WEBUI_ISOLATED_PROFILE`` is explicitly enabled (the PRIMARY
         gate — see _isolated_profile_opt_in), AND
      2. HERMES_HOME at startup points at a concrete profile subdirectory
         (e.g., ~/.hermes/profiles/user1) rather than the base home.

    Why the explicit flag is required (#4586 regression fix): the
    ``*/profiles/<name>`` shape alone CANNOT distinguish an intentional
    multi-user isolation deployment from an ordinary single-user running under a
    named profile — the Hermes Agent launcher sets
    ``HERMES_HOME=~/.hermes/profiles/<name>`` for any active named profile, so the
    two cases are byte-identical at the env-var level. Inferring isolation from
    the shape alone (the v0.51.528 behaviour from #2698) wrongly pinned ordinary
    single-user deployments to one profile and disabled profile switching. The
    multi-user wrapper that genuinely wants isolation now sets the explicit flag;
    everyone else is never caught. The shape stays as a secondary requirement so
    a stray flag without a profile-shaped HERMES_HOME does not engage isolation.

    Uses _INITIAL_HERMES_HOME (snapshotted at import time) to detect the shape,
    not the current os.environ value. init_profile_state() overwrites HERMES_HOME
    at startup, which would disable detection if we read it here.
    """
    # PRIMARY gate: explicit startup opt-in. Default OFF → a normal named-profile
    # launch is never treated as isolated, so profile switching keeps working
    # (#4586). Read the snapshot, not live os.environ, so profile .env reloads
    # cannot silently flip the deployment posture (#4590).
    if not _isolated_profile_opt_in():
        _warn_if_profile_shape_without_isolated_opt_in()
        return False

    hermes_home = _INITIAL_HERMES_HOME
    if not hermes_home:
        return False

    p = Path(hermes_home).expanduser()
    # SECONDARY requirement: HERMES_HOME must look like ~/.hermes/profiles/<name>
    # i.e., parent dir is named 'profiles' and grandparent exists.
    if p.parent.name == 'profiles' and p.parent.parent.exists():
        return True
    if p.is_symlink():
        global _ISOLATED_SYMLINK_WARNING_EMITTED
        if not _ISOLATED_SYMLINK_WARNING_EMITTED:
            logger.warning(
                "HERMES_WEBUI_ISOLATED_PROFILE is set but HERMES_HOME %s does not "
                "literally match */profiles/<name>; isolated profile mode stays off "
                "unless the literal profile path is used.",
                p,
            )
            _ISOLATED_SYMLINK_WARNING_EMITTED = True
    return False


def _isolated_profile_name() -> str:
    """Return the profile directory name from _INITIAL_HERMES_HOME."""
    return Path(_INITIAL_HERMES_HOME).expanduser().name


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

    # Platform default. On Windows this includes the #2905 migration-safety
    # fallback (prefer the populated legacy %USERPROFILE%\.hermes over an
    # empty %LOCALAPPDATA%\hermes). Import the shared path helper directly
    # instead of importing api.config here; api.config imports profiles during
    # startup, so going through config creates a partial-module circular import
    # when api.profiles is imported first.
    from api.paths import _platform_default_hermes_home

    return _platform_default_hermes_home()

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
      1. Isolated-profile deployment name from the configured HERMES_HOME path
      2. Thread-local (set per-request from hermes_profile cookie) — issue #798
      3. Process-level default (_active_profile)
    """
    if _is_isolated_profile_mode():
        return _isolated_profile_name()
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
    # In isolated mode, every logical profile lookup clamps to the configured
    # startup HERMES_HOME so callers cannot resolve a foreign profile path.
    if _is_isolated_profile_mode():
        isolated_name = _isolated_profile_name()
        isolated_home = Path(_INITIAL_HERMES_HOME).expanduser()
        if name and not _profiles_match(name, isolated_name):
            logger.warning(
                "Ignoring profile lookup %r in isolated profile mode; using pinned profile %r",
                name, isolated_name,
            )
        return isolated_home
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
    if _is_isolated_profile_mode():
        return Path(_INITIAL_HERMES_HOME).expanduser()
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
    if _is_isolated_profile_mode():
        active = _isolated_profile_name()
        if raw and not _profiles_match(raw, active):
            logger.warning(
                "Cron job %s references profile %r outside isolated profile %r; falling back to isolated home",
                (job or {}).get('id', '?'), raw, active,
            )
        return get_active_hermes_home()
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
            event_profile = str((job or {}).get("profile") or "").strip() or None
            if _is_isolated_profile_mode():
                event_profile = _isolated_profile_name()
            try:
                publish_session_list_changed("cron_complete", profile=event_profile)
            except TypeError:
                # Focused tests and older integrations may patch the publisher
                # with the historical one-argument shape.
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
                        # #4589: never let a profile's own .env override an
                        # operator/deployment posture (e.g. disable isolation via
                        # HERMES_WEBUI_ISOLATED_PROFILE=0) on the runtime-env path
                        # the same way _reload_dotenv() protects the live env.
                        if k in _PROTECTED_ENV_KEYS:
                            continue
                        env[k] = v
        except Exception:
            logger.debug("Failed to read runtime env from %s", env_path)

    return env


# Match Hermes Agent gateway behavior: profile-scoped WebUI runs should
# project intended runtime vars (credentials, HERMES_HOME, TERMINAL_*)
# without allowing profile env to override core shell identity variables
# like HOME or PATH.
_BLOCKED_RUNTIME_ENV_KEYS = {
    'HOME',
    'PATH',
    'PWD',
    'SHELL',
    'USER',
    'LOGNAME',
    'SHLVL',
    'OLDPWD',
    'PYTHONPATH',
    'VIRTUAL_ENV',
    'LD_LIBRARY_PATH',
    # #4589: operator/deployment isolation posture — never overridable by a
    # profile's own env on any runtime/gateway-parity path.
    'HERMES_WEBUI_ISOLATED_PROFILE',
}


def filter_runtime_env_for_gateway_parity(env: dict[str, str]) -> dict[str, str]:
    """Return a profile runtime env filtered to mimic Hermes gateway semantics."""
    filtered: dict[str, str] = {}
    for key, value in (env or {}).items():
        k = str(key).strip()
        if not k:
            continue
        if k in _BLOCKED_RUNTIME_ENV_KEYS:
            continue
        if k.startswith('XDG_'):
            continue
        filtered[k] = value
    return filtered


# Credential env vars the agent runtime resolves via raw os.getenv() that are
# NOT in hermes_cli.auth.PROVIDER_REGISTRY (so the registry-derived scrub set
# would miss them). Fail-closed list — verified against the installed agent:
#   CUSTOM_API_KEY            hermes_cli/models.py (generic custom provider key)
#   AZURE_ANTHROPIC_KEY       hermes_cli/runtime_provider.py (Azure-hosted Anthropic)
#   AZURE_FOUNDRY_API_KEY     hermes_cli/runtime_provider.py (Azure Foundry key)
#   AZURE_* identity family   agent/azure_identity_adapter.py (service-principal /
#                             workload-identity model auth)
#   AWS_BEARER_TOKEN_BEDROCK  hermes_cli/model_switch.py (Bedrock bearer token)
#   AWS_* credential chain    agent/bedrock_adapter.py + model_switch._has_aws_creds
#                             (boto3 access keys, session token, profile,
#                              container/web-identity credential providers)
# NOTE: region/base-url config vars (AWS_REGION, AWS_DEFAULT_REGION,
# AZURE_FOUNDRY_BASE_URL) are deliberately NOT included — they're configuration,
# not credentials, and the child probe may legitimately need them.
# Stripping these in a profile-scoped read prevents an empty named profile from
# inheriting the server-process credential (#3961 residual cross-profile leak).
_NON_REGISTRY_AGENT_CREDENTIAL_ENV_NAMES: tuple[str, ...] = (
    "CUSTOM_API_KEY",
    # Anthropic OAuth/token aliases. These ARE in the agent auth registry, but
    # are duplicated here as a fail-closed floor so the scrub still covers them
    # when the agent package can't be imported (e.g. a WebUI-only CI/test env
    # where hermes_cli.auth is absent) — the registry union is best-effort.
    "ANTHROPIC_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "AZURE_ANTHROPIC_KEY",
    "AZURE_FOUNDRY_API_KEY",
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
    "AZURE_TENANT_ID",
    "AZURE_FEDERATED_TOKEN_FILE",
    # Azure managed-identity (App Service MSI / IMDS) credential-source vars —
    # agent/azure_identity_adapter.py treats these as ManagedIdentityCredential
    # sources, so an empty named profile must not inherit the host's managed
    # identity. (NOT AZURE_FOUNDRY_BASE_URL — that's config, kept.)
    "IDENTITY_ENDPOINT",
    "IDENTITY_HEADER",
    "MSI_ENDPOINT",
    "MSI_SECRET",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
)


def _agent_registry_credential_env_names() -> set[str]:
    """Credential env-var names the *agent* runtime reads, beyond the WebUI's own
    settable-key map. Two sources:

    1. ``hermes_cli.auth.PROVIDER_REGISTRY[*].api_key_env_vars`` — every provider
       the agent CLI knows, incl. OAuth/token-flow providers like Anthropic's
       ``ANTHROPIC_TOKEN`` / ``CLAUDE_CODE_OAUTH_TOKEN`` that the WebUI's own
       ``_PROVIDER_ENV_VAR`` map omits (they aren't WebUI-settable API keys).
    2. ``_NON_REGISTRY_AGENT_CREDENTIAL_ENV_NAMES`` — a fail-closed fallback for
       credential env vars the agent resolves via raw ``os.getenv()`` that are NOT
       in the auth registry (the generic ``CUSTOM_API_KEY`` and the AWS/Bedrock
       credential family the bedrock adapter relies on).

    A profile scrub built only from the WebUI map would leave all of these in
    ``os.environ`` — letting an empty named profile inherit the server-process
    credential on the quota subprocess and detached-worker model-rebuild paths
    (#3961 residual cross-profile leak)."""
    names: set[str] = set(_NON_REGISTRY_AGENT_CREDENTIAL_ENV_NAMES)
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY

        registry = PROVIDER_REGISTRY
        items = registry.items() if hasattr(registry, "items") else enumerate(registry)
        for _key, entry in items:
            env_vars = getattr(entry, "api_key_env_vars", None)
            for env_var in env_vars or ():
                if env_var:
                    names.add(str(env_var))
    except Exception:
        logger.debug(
            "Failed to load agent registry credential env names for profile scope",
            exc_info=True,
        )
    return names


def _profile_secret_env_names(profile_home_path: Path) -> set[str]:
    names: set[str] = set()
    try:
        from api.providers import _provider_credential_env_vars

        names.update(_provider_credential_env_vars())
    except Exception:
        logger.debug(
            "Failed to load provider credential env names for profile scope",
            exc_info=True,
        )

    # Also scrub credential env vars the agent runtime resolves directly
    # (OAuth/token-flow providers absent from the WebUI's settable-key map) so a
    # profile-scoped read can't inherit the server process's ANTHROPIC_TOKEN /
    # CLAUDE_CODE_OAUTH_TOKEN etc. (#3961 cross-profile residual leak).
    names.update(_agent_registry_credential_env_names())

    config_path = Path(profile_home_path) / "config.yaml"
    if not config_path.exists():
        return names
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.debug(
            "Failed to inspect custom-provider credential env names from %s",
            config_path,
            exc_info=True,
        )
        return names

    custom_providers = payload.get("custom_providers") if isinstance(payload, dict) else None
    if not isinstance(custom_providers, list):
        return names
    for custom_provider in custom_providers:
        if not isinstance(custom_provider, dict):
            continue
        key_env = str(custom_provider.get("key_env") or "").strip()
        if key_env:
            names.add(key_env)
        api_key = str(custom_provider.get("api_key") or "").strip()
        match = re.fullmatch(r"\$\{([^}]+)\}", api_key)
        if match:
            env_name = str(match.group(1) or "").strip()
            if env_name:
                names.add(env_name)
    return names


def _apply_profile_env_to_process(
    process_env,
    safe_runtime_env: dict[str, str],
    *,
    secret_env_names: set[str],
) -> dict[str, Optional[str]]:
    scoped_keys = set(safe_runtime_env) | set(secret_env_names)
    previous_env = {key: process_env.get(key) for key in scoped_keys}
    for key in secret_env_names:
        if key not in safe_runtime_env:
            process_env.pop(key, None)
    return previous_env


_secret_scope_available = None


def _resolve_secret_scope_module():
    global _secret_scope_available
    import sys as _sys
    mod = _sys.modules.get('agent.secret_scope')
    if mod is not None:
        return mod
    if _secret_scope_available is False:
        return None
    if _secret_scope_available is None:
        try:
            import importlib.util
            _secret_scope_available = importlib.util.find_spec('agent') is not None
        except Exception:
            _secret_scope_available = False
    if _secret_scope_available:
        try:
            from agent.secret_scope import set_secret_scope, reset_secret_scope  # noqa: F401
            return _sys.modules.get('agent.secret_scope')
        except ImportError:
            _secret_scope_available = False
    return None


# #5567: hermes-agent v0.18.0+ exposes a CONTEXT-LOCAL Hermes-home override
# (`hermes_constants.set_hermes_home_override`) that `get_hermes_home()` — and
# therefore `hermes_cli.config.get_config_path()` / `load_config()` — consults
# BEFORE the process-global `os.environ["HERMES_HOME"]`. Installing it inside the
# profile worker scope eliminates the cross-profile HERMES_HOME race at the
# reader (a config read resolves the task-local profile home even if another
# thread clobbers os.environ mid-body) WITHOUT serializing workers or mutating
# shared state. Resolved lazily + optionally so OLDER agents (no override symbol)
# degrade gracefully to the pre-existing os.environ-mirror behavior — unchanged.
_hermes_home_override_available = None


def _resolve_hermes_home_override():
    """Return the hermes_constants module iff it exposes the v0.18.0+ context-local
    home override (set/reset), else None. Cached; import-safe on older agents."""
    global _hermes_home_override_available
    import sys as _sys
    if _hermes_home_override_available is False:
        return None
    mod = _sys.modules.get('hermes_constants')
    if mod is None and _hermes_home_override_available is None:
        try:
            import hermes_constants as mod  # noqa: F811
        except Exception:
            _hermes_home_override_available = False
            return None
    if mod is not None and hasattr(mod, 'set_hermes_home_override') and hasattr(
        mod, 'reset_hermes_home_override'
    ):
        _hermes_home_override_available = True
        return mod
    _hermes_home_override_available = False
    return None


@contextmanager
def profile_env_for_background_worker(
    session,
    purpose: str = "background worker",
    logger_override: Optional[logging.Logger] = None,
    *,
    scope_skill_modules: bool = True,
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
        safe_runtime_env = filter_runtime_env_for_gateway_parity(runtime_env)
        secret_env_names = _profile_secret_env_names(profile_home_path)
    except Exception:
        log.debug(
            "Failed to resolve profile env for %s profile %s; falling back to current env",
            purpose,
            profile,
            exc_info=True,
        )
        yield
        return

    thread_env = dict(safe_runtime_env)
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
    previous_block_process_env = bool(
        getattr(_thread_ctx, "block_process_env_fallback", False)
    )
    _scope_token = None
    _has_scope = False
    _secret_scope_mod = None
    # #5567: context-local Hermes-home override (hermes-agent v0.18.0+). None on
    # older agents → graceful no-op (falls back to the os.environ mirror below).
    _home_override_mod = None
    _home_override_token = None
    _home_override_installed = False
    has_profile_skill_home = False
    should_restore_skill_modules = False
    _acquired_skill_home_patch_lock = False
    try:
        _set_thread_env(**thread_env)
        _thread_ctx.block_process_env_fallback = True
        _secret_scope_mod = _resolve_secret_scope_module()
        _scope_token = None
        _has_scope = False
        if _secret_scope_mod is not None:
            try:
                _scope_token = _secret_scope_mod.set_secret_scope(thread_env)
                _has_scope = True
            except Exception:
                pass
        # #5567: install the context-local Hermes-home override so the agent
        # config reader (get_hermes_home -> get_config_path/load_config) resolves
        # THIS profile's home from task-local state, immune to a concurrent
        # cross-profile os.environ["HERMES_HOME"] clobber during the worker body.
        # No-op on agents < v0.18.0 (resolver returns None) → os.environ mirror
        # below remains the behavior, exactly as today.
        _home_override_mod = _resolve_hermes_home_override()
        if _home_override_mod is not None:
            try:
                _home_override_token = _home_override_mod.set_hermes_home_override(
                    str(profile_home_path)
                )
                _home_override_installed = True
            except Exception:
                _home_override_token = None
                _home_override_installed = False

        if scope_skill_modules:
            if _home_override_mod is not None and _home_override_installed:
                try:
                    has_profile_skill_home = _skill_modules_support_profile_home(
                        profile_home_path
                    )
                except Exception:
                    logger.debug(
                        "Failed to evaluate profile-home skill module capability for %s in %s",
                        profile,
                        purpose,
                        exc_info=True,
                    )
                    has_profile_skill_home = False

            # #5567-fallback: if override is unavailable, or module-side
            # profile resolution is missing/failed, serialize the full worker
            # lifespan under the shared legacy patch lock.
            should_restore_skill_modules = not (
                _home_override_installed and has_profile_skill_home
            )
            if should_restore_skill_modules:
                _SKILL_HOME_MODULE_PATCH_LOCK.acquire()
                _acquired_skill_home_patch_lock = True

        with _ENV_LOCK:
            if scope_skill_modules and should_restore_skill_modules:
                # Snapshot and patch before mutating process env so setup
                # failures can unwind without leaking either state.
                skill_home_snapshot = snapshot_skill_home_modules()
                patch_skill_home_modules(profile_home_path)

            old_runtime_env = _apply_profile_env_to_process(
                os.environ,
                safe_runtime_env,
                secret_env_names=secret_env_names,
            )
            had_hermes_home = "HERMES_HOME" in os.environ
            old_hermes_home = os.environ.get("HERMES_HOME")
            os.environ.update(safe_runtime_env)
            os.environ["HERMES_HOME"] = str(profile_home_path)
        yield
    finally:
        try:
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
                if should_restore_skill_modules and skill_home_snapshot is not None:
                    restore_skill_home_modules(skill_home_snapshot)
        finally:
            if _acquired_skill_home_patch_lock:
                _SKILL_HOME_MODULE_PATCH_LOCK.release()
                _acquired_skill_home_patch_lock = False
            # Reset context-local state after the fallback globals are restored.
            if _home_override_mod is not None and _home_override_installed:
                try:
                    _home_override_mod.reset_hermes_home_override(_home_override_token)
                except Exception:
                    pass
            if _has_scope and _secret_scope_mod is not None:
                try:
                    _secret_scope_mod.reset_secret_scope(_scope_token)
                except Exception:
                    pass
            _thread_ctx.block_process_env_fallback = previous_block_process_env
            if previous_thread_env:
                _set_thread_env(**previous_thread_env)
            else:
                _clear_thread_env()


@contextmanager
def profile_env_for_active_request_readonly(
    purpose: str = "provider/model read",
    logger_override: Optional[logging.Logger] = None,
):
    """Apply the active per-request profile's env to thread-local state only (#3957).

    WebUI profile switching is per-client/cookie scoped (issue #798): a browser
    on a named profile sets a ``hermes_profile`` cookie, which ``server.py``
    turns into a thread-local via ``set_request_profile()``.  This wrapper keeps
    provider-credential reads isolated to the request profile and does not touch
    process-wide environment for read-only endpoints.

    A thread-local read-only scope is used for ``/api/providers`` and
    ``/api/models`` flows that now resolve credentials through thread-local
    environment first. It also sets a context-local Hermes-home override so
    agent-side auth-store reads stay on the active profile without mutating
    process-global ``os.environ``.

    No-ops for the default/root profile, which is the common single-profile
    deployment case.
    """
    profile = (get_active_profile_name() or "").strip()
    if not profile or _is_root_profile(profile):
        yield
        return
    try:
        from api.config import _clear_thread_env, _set_thread_env, _thread_ctx
        profile_home_path = Path(get_hermes_home_for_profile(profile))
        runtime_env = get_profile_runtime_env(profile_home_path)
        safe_runtime_env = filter_runtime_env_for_gateway_parity(runtime_env)
    except Exception:
        log = logger_override or logger
        log.debug(
            "Failed to resolve profile env for active request profile %s in %s; "
            "falling back to current env",
            profile,
            purpose,
            exc_info=True,
        )
        yield
        return
    try:
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )
    except Exception:
        reset_hermes_home_override = None
        set_hermes_home_override = None

    thread_env = dict(safe_runtime_env)
    thread_env["HERMES_HOME"] = str(profile_home_path)
    previous_thread_env = getattr(_thread_ctx, "env", {}).copy()
    previous_block_process_env = bool(
        getattr(_thread_ctx, "block_process_env_fallback", False)
    )
    home_override_token = None
    home_override_installed = False
    _scope_token = None
    _has_scope = False
    try:
        _set_thread_env(**thread_env)
        _thread_ctx.block_process_env_fallback = True
        _secret_scope_mod = _resolve_secret_scope_module()
        _scope_token = None
        _has_scope = False
        if _secret_scope_mod is not None:
            try:
                _scope_token = _secret_scope_mod.set_secret_scope(thread_env)
                _has_scope = True
            except Exception:
                pass
        if set_hermes_home_override is not None:
            try:
                home_override_token = set_hermes_home_override(profile_home_path)
                home_override_installed = True
            except Exception:
                logger.debug(
                    "Failed to install Hermes-home override for active request profile %s in %s",
                    profile,
                    purpose,
                    exc_info=True,
                )
        yield
    finally:
        if _has_scope and _secret_scope_mod is not None:
            try:
                _secret_scope_mod.reset_secret_scope(_scope_token)
            except Exception:
                pass
        if reset_hermes_home_override is not None and home_override_installed:
            try:
                reset_hermes_home_override(home_override_token)
            except Exception:
                (logger_override or logger).debug(
                    "Failed to reset Hermes-home override for active request profile %s in %s",
                    profile,
                    purpose,
                    exc_info=True,
                )
        _thread_ctx.block_process_env_fallback = previous_block_process_env
        if previous_thread_env:
            _set_thread_env(**previous_thread_env)
        else:
            _clear_thread_env()


@contextmanager
def profile_env_for_active_request(
    purpose: str = "active request",
    logger_override: Optional[logging.Logger] = None,
):
    """Apply the active per-request profile through the legacy mirrored path.

    Some request-scoped readers still delegate into Hermes helpers that resolve
    credentials directly from process env or ``get_hermes_home()``. Those paths
    stay on the mirrored scope until they are fully audited.
    """
    profile = (get_active_profile_name() or "").strip()
    if not profile or _is_root_profile(profile):
        yield
        return
    with profile_env_for_background_worker(
        profile, purpose, logger_override=logger_override
    ):
        yield


@contextmanager
def profile_scope_for_detached_worker(
    profile_name,
    purpose: str = "detached worker",
    logger_override: Optional[logging.Logger] = None,
):
    """Bind BOTH the per-request profile TLS and the profile env on a NEW thread (#3957).

    A detached worker thread (e.g. the ``models-catalog-rebuild`` daemon that
    ``get_available_models`` spawns for a bounded rebuild) inherits neither the
    spawning request's profile thread-local (issue #798) nor its ``os.environ``.
    Without re-establishing both, the worker resolves the *default* profile:
      - profile-keyed paths (``_get_models_cache_path`` / ``_get_config_path`` /
        ``_get_auth_store_path`` / ``_models_cache_source_fingerprint``) read the
        per-request profile via ``get_active_profile_name()`` — needs the TLS;
      - credential lookups (``provider_model_ids`` / ``_lookup_custom_api_key_env``)
        read ``os.environ`` — needs the profile ``.env`` applied.

    Pass the profile name CAPTURED on the spawning thread (where the TLS is
    valid) into the worker, then enter this scope at the top of the worker body.
    It sets the request-profile TLS for this (worker) thread and applies the
    profile env via ``profile_env_for_background_worker``, restoring both on exit.
    No-op for the default/root profile.

    Unlike ``profile_env_for_active_request`` (which reads the *current* thread's
    TLS and must NOT clear it — the request thread keeps using it after the call),
    this sets and then CLEARS the TLS, which is correct for a dedicated worker
    thread that has no other use for it.
    """
    name = (profile_name or "").strip()
    if not name or _is_root_profile(name):
        yield
        return
    set_request_profile(name)
    try:
        with profile_env_for_background_worker(
            name, purpose, logger_override=logger_override
        ):
            yield
    finally:
        clear_request_profile()


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
                    # Operator/deployment-level keys are never overridable by a
                    # profile's own .env (#4589 — prevents a contained user from
                    # disabling their isolation via HERMES_WEBUI_ISOLATED_PROFILE=0).
                    if k in _PROTECTED_ENV_KEYS:
                        logger.warning(
                            "Ignoring protected key %s in profile .env %s; "
                            "operator/deployment env takes precedence",
                            k, env_path,
                        )
                        continue
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
    if _is_isolated_profile_mode():
        _active_profile = _isolated_profile_name()
        home = Path(_INITIAL_HERMES_HOME).expanduser()
    else:
        _active_profile = _read_active_profile_file()
        home = get_active_hermes_home()
    _set_hermes_home(home)
    install_cron_scheduler_profile_isolation()
    _reload_dotenv(home)


def switch_profile(name: str, *, process_wide: bool = True) -> dict:
    """Switch the active profile.

    Validates the profile exists, updates process state, patches module caches,
    reloads .env, and reloads config.yaml.

    In isolated profile mode, switching to a different profile is rejected (403).
    Switching to the isolated profile itself is allowed (idempotent).

    Args:
        name: Profile name to switch to.
        process_wide: If True (default), updates the process-global
            _active_profile.  Set to False for per-client switches from the
            WebUI where the profile is managed via cookie + thread-local (#798).

    Returns: {'profiles': [...], 'active': name}
    Raises ValueError when profile doesn't exist, RuntimeError when agent is running,
    PermissionError in isolated mode for cross-profile switches.
    """
    global _active_profile

    # In isolated profile mode, reject switching to other profiles
    if _is_isolated_profile_mode():
        active = _isolated_profile_name()
        if name != active:
            raise PermissionError(
                f"Profile switching is not allowed in isolated profile mode. "
                f"Currently pinned to profile '{active}'."
            )

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
    if _is_isolated_profile_mode():
        home = Path(_INITIAL_HERMES_HOME).expanduser()
    elif _is_root_profile(name):
        home = _DEFAULT_HERMES_HOME
    else:
        home = _resolve_named_profile_home(name)
        if not home.is_dir():
            raise ValueError(f"Profile '{name}' does not exist.")

    with _profile_lock:
        _SKILLS_STATS_CACHE.clear()
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
    default_model_provider = None
    if isinstance(model_cfg, str):
        default_model = model_cfg
    elif isinstance(model_cfg, dict):
        default_model = model_cfg.get('default')
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
        'is_default': _is_root_profile(name),
        'default_model': default_model,
        'default_model_provider': default_model_provider,
        'default_workspace': default_workspace,
    }


_SKILLS_STATS_CACHE: dict[Path, tuple[int, int, int, float]] = {}
_SKILLS_STATS_CACHE_TTL = 300.0  # seconds — long because .clear() handles programmatic changes

# Per-profile compute locks (#5364). Without these, concurrent cold-startup
# requests (ThreadingHTTPServer runs one OS thread per request) all miss the
# unlocked _SKILLS_STATS_CACHE at once and each walks + parses the whole skill
# tree simultaneously — a thundering herd that stalled workers 57–70s under
# Docker overlay2. A per-profile lock lets independent profiles compute in
# parallel while collapsing concurrent misses on the SAME profile to a single
# shared compute (double-checked locking below). The lock registry is guarded by
# its own meta-lock and is bounded by the (small) number of profiles.
_SKILLS_STATS_LOCKS: dict[Path, threading.Lock] = {}
_SKILLS_STATS_LOCKS_GUARD = threading.Lock()


def _skills_stats_lock_for(profile_dir: Path) -> threading.Lock:
    """Return (creating if needed) the per-profile compute lock for profile_dir.

    profile_dir must already be resolved so distinct spellings of the same
    directory share one lock.
    """
    with _SKILLS_STATS_LOCKS_GUARD:
        lock = _SKILLS_STATS_LOCKS.get(profile_dir)
        if lock is None:
            lock = threading.Lock()
            _SKILLS_STATS_LOCKS[profile_dir] = lock
        return lock


def _skill_tree_max_mtime_ns(skills_dir: Path, config_path: Path) -> int:
    """Return the max st_mtime_ns across config.yaml, skill dirs, and SKILL.md files."""
    max_ns = 0
    try:
        if config_path.exists():
            max_ns = max(max_ns, config_path.stat().st_mtime_ns)
    except OSError:
        pass
    if not skills_dir.is_dir():
        return max_ns
    try:
        from agent.skill_utils import EXCLUDED_SKILL_DIRS, SKILL_SUPPORT_DIRS
    except Exception:
        EXCLUDED_SKILL_DIRS = frozenset()
        SKILL_SUPPORT_DIRS = frozenset()
    try:
        # Directory mtimes catch nested out-of-band deletes that leave file mtimes unchanged.
        # followlinks=True mirrors agent.skill_utils.iter_skill_index_files (the compute
        # path), so a symlinked skill directory is descended into and edits to its target
        # SKILL.md change the probe value — otherwise such edits would stay stale up to the TTL.
        for root, dirnames, filenames in os.walk(skills_dir, followlinks=True):
            root_path = Path(root)
            # Prune the SAME trees iter_skill_index_files prunes (.git/.venv/
            # node_modules/site-packages + skill support dirs), so a skill that
            # vendors a dependency tree doesn't make this every-call probe walk
            # thousands of irrelevant files and defeat the cache's perf goal.
            has_skill_md = "SKILL.md" in filenames
            dirnames[:] = [
                d for d in dirnames
                if d not in EXCLUDED_SKILL_DIRS
                and not (has_skill_md and d in SKILL_SUPPORT_DIRS)
            ]
            try:
                max_ns = max(max_ns, root_path.stat().st_mtime_ns)
            except OSError:
                pass
            for dirname in dirnames:
                try:
                    max_ns = max(max_ns, (root_path / dirname).stat().st_mtime_ns)
                except OSError:
                    pass
            if "SKILL.md" in filenames:
                try:
                    max_ns = max(max_ns, (root_path / "SKILL.md").stat().st_mtime_ns)
                except OSError:
                    pass
    except Exception:
        pass
    return max_ns


def _compute_profile_skills_stats(profile_dir: Path) -> tuple[int, int]:
    """Compute (enabled_count, compatible_count) by reading and parsing all SKILL.md files."""
    skills_dir = profile_dir / "skills"
    if not skills_dir.is_dir():
        return (0, 0)

    disabled = set()
    config_path = profile_dir / "config.yaml"
    if config_path.exists():
        try:
            import yaml as _yaml
            cfg = _yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if isinstance(cfg, dict):
                skills_cfg = cfg.get("skills")
                if isinstance(skills_cfg, dict):
                    # Align with get_disabled_skill_names(platform="webui") behavior:
                    platform_disabled = (skills_cfg.get("platform_disabled") or {}).get("webui")
                    if platform_disabled is not None:
                        disabled_val = platform_disabled
                    else:
                        disabled_val = skills_cfg.get("disabled")

                    if disabled_val is not None:
                        if isinstance(disabled_val, str):
                            disabled_val = [disabled_val]
                        disabled = {str(v).strip() for v in disabled_val if str(v).strip()}
        except Exception:
            pass

    from agent.skill_utils import iter_skill_index_files, parse_frontmatter, skill_matches_platform

    seen_names = set()
    enabled_count = 0
    compatible_count = 0

    for skill_md in iter_skill_index_files(skills_dir, "SKILL.md"):
        try:
            content = skill_md.read_text(encoding="utf-8")[:4000]
            frontmatter, _ = parse_frontmatter(content)
            if not skill_matches_platform(frontmatter):
                continue
            name = frontmatter.get("name", skill_md.parent.name)[:64]
            if name in seen_names:
                continue
            seen_names.add(name)

            compatible_count += 1
            if name not in disabled:
                enabled_count += 1
        except Exception:
            pass

    return (enabled_count, compatible_count)


def _get_profile_skills_stats(profile_dir: Path) -> tuple[int, int]:
    """Calculate (enabled_count, compatible_count) with two-tier mtime cache.

    A cheap stat-only mtime probe runs on EVERY call so out-of-band (CLI/git)
    skill changes are reflected promptly — the expensive part (reading + parsing
    every SKILL.md) is what the cache avoids, not the change detection. The TTL
    is only a safety-net upper bound that forces an occasional full recompute
    even when the mtime probe sees no change.
    """
    import time
    profile_dir = Path(profile_dir).resolve()
    now = time.time()
    skills_dir = profile_dir / "skills"
    config_path = profile_dir / "config.yaml"

    # Always run the cheap stat-only probe first — this is what catches an
    # out-of-band create/edit/delete within the same request (not after the TTL).
    current_mtime_ns = _skill_tree_max_mtime_ns(skills_dir, config_path)

    # Read via .get() (not membership-check + index) so a concurrent
    # _SKILLS_STATS_CACHE.clear() on another thread can't raise KeyError
    # between the `in` test and the lookup.
    cached = _SKILLS_STATS_CACHE.get(profile_dir)
    if cached is not None:
        enabled, compat, cached_mtime_ns, expiry = cached
        # Fast path: files unchanged (by the cheap probe above) AND still within
        # the TTL → serve cached without re-reading any SKILL.md. The mtime probe
        # already ran, so an out-of-band change is caught immediately regardless
        # of the TTL. On TTL expiry we deliberately fall through to a full
        # recompute (the TTL is a safety net for mtime-preserving changes that
        # the probe can't see — e.g. a git checkout that restores the old mtime).
        if current_mtime_ns == cached_mtime_ns and now < expiry:
            return enabled, compat

    # Cache miss, mtime changed, or TTL expired — serialize per-profile so a
    # burst of concurrent misses (cold startup) collapses to ONE compute instead
    # of a thundering herd of simultaneous os.walk + SKILL.md parses (#5364).
    lock = _skills_stats_lock_for(profile_dir)
    with lock:
        # Double-checked locking: another thread may have populated a fresh entry
        # while we waited for the lock. Reuse it when the mtime we already probed
        # still matches and the entry is within its TTL — no second compute.
        cached = _SKILLS_STATS_CACHE.get(profile_dir)
        if cached is not None:
            enabled, compat, cached_mtime_ns, expiry = cached
            if current_mtime_ns == cached_mtime_ns and time.time() < expiry:
                return enabled, compat

        # Snapshot mtime BEFORE compute so any concurrent SKILL.md write during
        # the compute window causes a mismatch on the next probe instead of
        # silently serving stale data (TOCTOU).
        new_mtime_ns = _skill_tree_max_mtime_ns(skills_dir, config_path)
        res = _compute_profile_skills_stats(profile_dir)
        _SKILLS_STATS_CACHE[profile_dir] = (
            res[0], res[1], new_mtime_ns, time.time() + _SKILLS_STATS_CACHE_TTL
        )
        return res


_LIST_PROFILES_CACHE: tuple[list, float] | None = None
_LIST_PROFILES_CACHE_TTL = 4.0  # seconds. The perf(session-load-latency) pass bumped this to 60s, but that was reverted: profile-row mutations (defaults / providers / skills / gateway config) do NOT invalidate this cache, so a 60s TTL served stale profile rows for too long after such a change. 4s keeps the os.walk frequent enough that mutation→poll staleness is negligible while still making rapid dropdown re-opens free. The create/delete invalidation hooks below clear the cache immediately on those specific mutations.
_LIST_PROFILES_CACHE_LOCK = threading.Lock()


def _invalidate_list_profiles_cache() -> None:
    """Drop the cached profile list (call after create/delete/switch)."""
    global _LIST_PROFILES_CACHE
    with _LIST_PROFILES_CACHE_LOCK:
        _LIST_PROFILES_CACHE = None


def _build_profile_rows_fast() -> list | None:
    """Build the profile list WITHOUT the upstream alias scan.

    ``hermes_cli.profiles.list_profiles()`` calls ``find_alias_for_profile()``
    once per profile, which iterates every file in the wrapper dir
    (``~/.local/bin``) and ``read_text()``s each one — including large binaries
    (claude, node, uv, …). On a machine with big binaries on PATH that is
    hundreds of MB of reads PER PROFILE, which makes the compose-footer profile
    dropdown hang for many seconds.

    The WebUI never uses the alias data (``list_profiles_api`` does not return
    ``alias_name``/``alias_path``), so we replicate the cheap part of upstream's
    ``list_profiles()`` — the same per-profile metadata, the same hardcoded
    ``"default"`` name for the base home — and simply skip the alias scan.

    Returns ``None`` if the upstream cheap helpers can't be imported, so the
    caller can fall back to the original (slow but correct) path. Forward-
    compatible: if upstream fixes ``find_alias_for_profile`` this stays fast and
    correct with nothing to revert.
    """
    try:
        from hermes_cli.profiles import (
            _get_default_hermes_home,
            _get_profiles_root,
            _read_config_model,
            _check_gateway_running,
            _PROFILE_ID_RE as _UPSTREAM_PROFILE_ID_RE,
        )
    except Exception:
        return None

    def _row(home: Path, name: str, is_default: bool) -> dict:
        try:
            model, provider = _read_config_model(home)
        except Exception:
            model, provider = None, None
        try:
            gateway_running = _check_gateway_running(home)
        except Exception:
            gateway_running = False
        enabled_count, total_count = _get_profile_skills_stats(home)
        return {
            'name': name,
            'path': str(home),
            'is_default': is_default,
            'is_active': False,  # filled in by caller (cheap, varies per request)
            'gateway_running': gateway_running,
            'model': model,
            'provider': provider,
            'has_env': (home / '.env').exists(),
            'visible': _profile_visible_from_meta(home),
            'skill_count': enabled_count,
            'enabled_skills': enabled_count,
            'total_skills': total_count,
        }

    rows: list = []
    default_home = _get_default_hermes_home()
    if default_home.is_dir():
        # Upstream hardcodes the base home's display name to "default" even when
        # the directory is literally ".hermes" — match that exactly.
        rows.append(_row(default_home, 'default', True))

    profiles_root = _get_profiles_root()
    if profiles_root.is_dir():
        for entry in sorted(profiles_root.iterdir()):
            if not entry.is_dir():
                continue
            if not _UPSTREAM_PROFILE_ID_RE.match(entry.name):
                continue
            rows.append(_row(entry, entry.name, False))

    return rows


def list_profiles_api() -> list:
    """List all profiles with metadata, serialized for JSON response.

    In isolated profile mode (HERMES_HOME points to ~/.hermes/profiles/<name>),
    returns only that single profile and skips other profiles entirely.

    Fast path: build the rows from upstream's cheap per-profile helpers and skip
    ``find_alias_for_profile`` (whose result the WebUI discards) — see
    ``_build_profile_rows_fast``. Results are cached for a short TTL so rapid
    re-opens of the compose-footer dropdown are free; the cache is busted on
    profile create/delete. Falls back to upstream ``list_profiles()`` if the
    cheap helpers are unavailable.
    """
    import time
    global _LIST_PROFILES_CACHE
    now = time.time()

    # In isolated profile mode, return only the active (isolated) profile
    if _is_isolated_profile_mode():
        active = _isolated_profile_name()
        hermes_home = Path(_INITIAL_HERMES_HOME).expanduser()
        try:
            from hermes_cli.profiles import list_profiles
            infos = list_profiles()
            # When the isolated profile is literally named "default", upstream
            # can surface the base-home row first. Only trust a row whose path
            # resolves to the same directory as the isolated startup home.
            for p in infos:
                try:
                    same_home = Path(p.path).expanduser().resolve() == hermes_home.resolve()
                except OSError:
                    same_home = False
                if p.name == active and same_home:
                    enabled_count, total_count = _get_profile_skills_stats(p.path)
                    return [{
                        'name': p.name,
                        'path': str(p.path),
                        'is_default': p.is_default,
                        'is_active': True,  # Always true in isolated mode
                        'gateway_running': p.gateway_running,
                        'model': p.model,
                        'provider': p.provider,
                        'has_env': p.has_env,
                        'visible': _profile_visible_from_meta(p.path),
                        'skill_count': enabled_count,
                        'enabled_skills': enabled_count,
                        'total_skills': total_count,
                    }]
        except (ImportError, OSError, PermissionError):
            pass
        # Fallback: construct profile dict with actual active name and hermes_home path
        enabled_count, total_count = _get_profile_skills_stats(hermes_home)
        return [{
            'name': active,
            'path': str(hermes_home),
            'is_default': active == 'default',
            'is_active': True,
            'gateway_running': False,
            'model': None,
            'provider': None,
            'has_env': (hermes_home / '.env').exists(),
            'visible': _profile_visible_from_meta(hermes_home),
            'skill_count': enabled_count,
            'enabled_skills': enabled_count,
            'total_skills': total_count,
        }]

    # Single-flight the build (#5364): hold the cache lock across the row build
    # so a cold-startup burst of concurrent requests collapses to ONE build while
    # the others wait and then serve the freshly-cached rows — instead of every
    # thread rebuilding (each walking all profiles' skill trees) at once. The
    # per-profile skills locks taken inside _build_profile_rows_fast are always
    # acquired AFTER this lock (never the reverse), so there is no deadlock.
    with _LIST_PROFILES_CACHE_LOCK:
        cached = _LIST_PROFILES_CACHE
        if cached is not None and now - cached[1] < _LIST_PROFILES_CACHE_TTL:
            rows = cached[0]
        else:
            rows = _build_profile_rows_fast()
            if rows is not None:
                _LIST_PROFILES_CACHE = (rows, now)

    if rows is None:
        # Fallback: cheap helpers unavailable — use the original (slow) path,
        # or the default-only dict if hermes_cli isn't importable at all.
        logger.debug(
            "list_profiles_api: fast path unavailable, falling back to "
            "upstream list_profiles() (slower)"
        )
        try:
            from hermes_cli.profiles import list_profiles
            infos = list_profiles()
        except ImportError:
            return [_default_profile_dict()]

        active = get_active_profile_name()
        result = []
        for p in infos:
            enabled_count, total_count = _get_profile_skills_stats(p.path)
            result.append({
                'name': p.name,
                'path': str(p.path),
                'is_default': p.is_default,
                'is_active': p.name == active,
                'gateway_running': p.gateway_running,
                'model': p.model,
                'provider': p.provider,
                'has_env': p.has_env,
                'visible': _profile_visible_from_meta(p.path),
                'skill_count': enabled_count,
                'enabled_skills': enabled_count,
                'total_skills': total_count,
            })
        return result

    active = get_active_profile_name()
    return [{**p, 'is_active': p['name'] == active} for p in rows]


def _profile_visible_from_meta(profile_path: Path) -> bool:
    """Return False only for an explicit boolean ``visible: false`` in profile.yaml."""
    try:
        meta_path = Path(profile_path) / 'profile.yaml'
        if not meta_path.exists():
            return True
        data = yaml.safe_load(meta_path.read_text(encoding='utf-8'))
    except Exception:
        return True
    if not isinstance(data, dict):
        return True
    visible = data.get('visible')
    return visible is not False


def _default_profile_dict() -> dict:
    """Fallback profile dict when hermes_cli is not importable."""
    enabled_count, compatible_count = _get_profile_skills_stats(_DEFAULT_HERMES_HOME)
    return {
        'name': 'default',
        'path': str(_DEFAULT_HERMES_HOME),
        'is_default': True,
        'is_active': True,
        'gateway_running': False,
        'model': None,
        'provider': None,
        'has_env': (_DEFAULT_HERMES_HOME / '.env').exists(),
        'visible': True,
        'skill_count': enabled_count,
        'enabled_skills': enabled_count,
        'total_skills': compatible_count,
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


# Provider → .env variable name mapping.
# When a user supplies an API key during profile creation in the WebUI,
# the key must be written to the profile's .env file so that Hermes Agent's
# provider layer can read it — config.yaml model.api_key is not consumed.
_PROVIDER_ENV_MAP: dict[str, str] = {
    "kimi-coding": "KIMI_API_KEY",
    "kimi-coding-cn": "KIMI_CN_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "google": "GEMINI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "xai": "XAI_API_KEY",
    "groq": "GROQ_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "minimax-cn": "MINIMAX_CN_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "zai": "ZAI_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
    "kilocode": "KILOCODE_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "github-copilot": "COPILOT_GITHUB_TOKEN",
    "nous": "NOUS_API_KEY",
}


def _resolve_env_var_for_provider(provider: Optional[str]) -> Optional[str]:
    """Return the .env variable name for *provider*, or the generic fallback."""
    if not provider:
        return None
    return _PROVIDER_ENV_MAP.get(str(provider).strip().lower())


def _upsert_dotenv_line(env_path: Path, key: str, value: str) -> None:
    """Write or replace a KEY=value line in a dotenv file.

    Reads existing lines; if *key* already exists its value is replaced.
    Otherwise a new line is appended.  The file (and parent dirs) are created
    when they do not exist yet.
    """
    env_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    except Exception:
        lines = []

    new_line = f"{key}={value}"
    found = False
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _ = stripped.split("=", 1)
            if k.strip() == key:
                new_lines.append(new_line)
                found = True
                continue
        new_lines.append(line)

    if not found:
        new_lines.append(new_line)

    try:
        env_path.write_text("\n".join(new_lines).rstrip("\n") + "\n", encoding="utf-8")
    except Exception as exc:
        logger.error("Failed to write %s to %s: %s", key, env_path, exc)
        raise


def _write_api_key_to_dotenv(
    profile_dir: Path,
    api_key: str,
    model_provider: Optional[str] = None,
) -> None:
    """Write *api_key* to the profile's .env under the correct variable name.

    If *model_provider* is known, the key is stored under the provider-specific
    env var (e.g. ``KIMI_API_KEY``); otherwise it falls back to a generic
    ``HERMES_API_KEY`` that the user can rename later.
    """
    env_var = _resolve_env_var_for_provider(model_provider)
    if not env_var:
        env_var = "HERMES_API_KEY"
        logger.info(
            "No provider→env mapping for %r; writing API key as %s",
            model_provider,
            env_var,
        )

    env_path = profile_dir / ".env"
    _upsert_dotenv_line(env_path, env_var, api_key)

    # Tighten permissions so the key isn't world-readable.
    try:
        env_path.chmod(0o600)
    except Exception:
        logger.debug("Failed to chmod 0o600 on %s", env_path)


def _write_endpoint_to_config(profile_dir: Path, base_url: str = None, api_key: str = None) -> None:
    """Write base_url into config.yaml for a profile.

    API keys are intentionally NOT written to config.yaml — they belong in
    the profile's .env file instead (see ``_write_api_key_to_dotenv``).
    The *api_key* parameter is accepted for backward compatibility with
    callers that still pass it; it is silently dropped here (the caller
    should have already called ``_write_api_key_to_dotenv``).
    """
    if not base_url:
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
    cfg['model'] = model_section
    _atomic_write_text(config_path, _yaml.dump(cfg, default_flow_style=False, allow_unicode=True), encoding='utf-8')


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
        all_group_models = (group.get("models") or []) + (group.get("extra_models") or [])
        for model in all_group_models:
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
    _atomic_write_text(config_path, _yaml.dump(cfg, default_flow_style=False, allow_unicode=True), encoding='utf-8')


def create_profile_api(name: str, clone_from: str = None,
                       clone_config: bool = False,
                       base_url: str = None,
                       api_key: str = None,
                       default_model: str = None,
                       model_provider: str = None) -> dict:
    """Create a new profile. Returns the new profile info dict.

    In isolated profile mode, profile creation is rejected (403).
    """
    if _is_isolated_profile_mode():
        raise PermissionError("Profile creation is not allowed in isolated profile mode.")
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

    _write_endpoint_to_config(profile_path, base_url=base_url)
    if api_key:
        _write_api_key_to_dotenv(
            profile_path,
            api_key=api_key,
            model_provider=model_provider,
        )
    _write_model_defaults_to_config(
        profile_path,
        default_model=default_model,
        model_provider=model_provider,
    )

    # Invalidate cached root-profile-name lookup; create_profile may have added
    # a new profile that flips is_default semantics on the agent side (#1612).
    _SKILLS_STATS_CACHE.clear()
    _invalidate_list_profiles_cache()
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
        'enabled_skills': 0,
        'total_skills': 0,
    }


def delete_profile_api(name: str) -> dict:
    """Delete a profile. Switches to default first if it's the active one.

    In isolated profile mode, profile deletion is rejected (403).
    """
    if _is_isolated_profile_mode():
        raise PermissionError("Profile deletion is not allowed in isolated profile mode.")
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
    _SKILLS_STATS_CACHE.clear()
    _invalidate_list_profiles_cache()
    _invalidate_root_profile_cache()
    return {'ok': True, 'name': name}
