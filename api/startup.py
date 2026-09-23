"""Hermes Web UI -- startup helpers."""
from __future__ import annotations
import os, stat, subprocess, sys, threading, time
from pathlib import Path

# Credential files that should never be world-readable
_SENSITIVE_FILES = (
    '.env',
    'google_token.json',
    'google_client_secret.json',
    '.signing_key',
    'auth.json',
)

# Cold-start warm-up (post-bind, best-effort). Disable with
# HERMES_WEBUI_NO_WARMUP=1 (e.g. profiling, debugging a cold-path bug).
_WARMUP_DISABLE_ENV = 'HERMES_WEBUI_NO_WARMUP'
# Upper bound the warm thread waits for the session-list rebuild before it logs
# and gives up. It never blocks readiness (daemon thread) and the claim stays
# with the background rebuild either way, so waiters are still released. The
# routes-side warm-up splits this budget into per-profile slices and warms one
# profile at a time, so a single slow profile cannot consume the whole window.
_WARMUP_SESSION_WAIT_SECONDS = 30.0
_warmup_lock = threading.Lock()
_warmup_started = False


def fix_credential_permissions() -> None:
    """Ensure sensitive files in HERMES_HOME have safe permissions.

    Respects:
      - HERMES_SKIP_CHMOD=1  → bypass entirely
      - HERMES_HOME_MODE     → group bits are allowed if set by the operator,
                               only world-readable/world-writable files are fixed
    """
    if os.environ.get('HERMES_SKIP_CHMOD', '').strip() in ('1', 'true'):
        return

    # Parse operator-declared mode to know if group bits are intentional
    declared_mode = None
    raw_mode = os.environ.get('HERMES_HOME_MODE', '').strip()
    if raw_mode:
        try:
            declared_mode = int(raw_mode, 8)
        except ValueError:
            pass

    hermes_home = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes')))
    if not hermes_home.is_dir():
        return
    for name in _SENSITIVE_FILES:
        fpath = hermes_home / name
        if not fpath.exists():
            continue
        try:
            current = stat.S_IMODE(fpath.stat().st_mode)
            # If operator declared a mode, allow group bits but still fix world bits
            if declared_mode is not None:
                if current & 0o007:  # other bits set (world-readable/writable)
                    fpath.chmod(current & ~0o007)
                    print(f'  [security] removed world bits on {fpath.name} ({oct(current)} -> {oct(current & ~0o007)})', flush=True)
            else:
                if current & 0o077:  # group or other bits set
                    fpath.chmod(0o600)
                    print(f'  [security] fixed permissions on {fpath.name} ({oct(current)} -> 0600)', flush=True)
        except OSError:
            pass  # best-effort; don't abort startup


def _agent_dir() -> Path | None:
    hermes_home = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes')))
    for raw in [os.environ.get('HERMES_WEBUI_AGENT_DIR', '').strip(), str(hermes_home / 'hermes-agent')]:
        if not raw:
            continue
        p = Path(raw).expanduser()
        if p.is_dir():
            return p.resolve()
    return None

def _trusted_agent_dir(agent_dir: Path) -> bool:
    """Return True if agent_dir passes ownership and permission checks.

    Validates that the directory is not world- or group-writable and,
    on POSIX systems, is owned by the current process user.

    Intentionally does NOT enforce a canonical path (i.e. does not require
    the dir to be ~/.hermes/hermes-agent), so custom HERMES_WEBUI_AGENT_DIR
    paths work correctly when HERMES_WEBUI_AUTO_INSTALL=1 is set.
    """
    try:
        st = agent_dir.stat()
        if stat.S_IMODE(st.st_mode) & 0o022:
            # World- or group-writable — untrusted
            return False
        if hasattr(os, 'getuid') and st.st_uid != os.getuid():
            # Not owned by current user (POSIX only; Windows fallback skips)
            return False
        return True
    except OSError:
        return False


def auto_install_agent_deps() -> bool:
    enabled = os.environ.get('HERMES_WEBUI_AUTO_INSTALL', '').strip().lower() in ('1', 'true', 'yes')
    if not enabled:
        print('[!!] Auto-install disabled. Set HERMES_WEBUI_AUTO_INSTALL=1 to enable.', flush=True)
        return False
    agent_dir = _agent_dir()
    if agent_dir is None:
        print('[!!] Auto-install skipped: agent directory not found.', flush=True)
        return False
    if not _trusted_agent_dir(agent_dir):
        print('[!!] Auto-install skipped: agent directory failed trust check (check ownership/permissions).', flush=True)
        return False
    req_file = agent_dir / 'requirements.txt'
    pyproject = agent_dir / 'pyproject.toml'
    if req_file.exists():
        install_args = [sys.executable, '-m', 'pip', 'install', '--quiet', '-r', str(req_file)]
        print(f'     Installing from {req_file} ...', flush=True)
    elif pyproject.exists():
        install_args = [sys.executable, '-m', 'pip', 'install', '--quiet', str(agent_dir)]
        print(f'     Installing from {agent_dir} (pyproject.toml) ...', flush=True)
    else:
        print('[!!] Auto-install skipped: no requirements.txt or pyproject.toml in agent dir.', flush=True)
        return False
    try:
        result = subprocess.run(install_args, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            print(f'[!!] pip install failed (exit {result.returncode}):', flush=True)
            for line in (result.stderr or '').splitlines()[-10:]:
                print(f'     {line}', flush=True)
            return False
        print('[ok] pip install completed.', flush=True)
        return True
    except subprocess.TimeoutExpired:
        print('[!!] Auto-install timed out after 120s.', flush=True)
        return False
    except Exception as e:
        print(f'[!!] Auto-install error: {e}', flush=True)
        return False


def _warmup_disabled() -> bool:
    return os.environ.get(_WARMUP_DISABLE_ENV, '').strip().lower() in ('1', 'true', 'yes')


def start_cold_start_warmup():
    """Start the bounded post-bind cold-start warm-up thread (best-effort).

    Called once right after the HTTP server has bound, so it can never delay
    readiness. Returns the started thread, or None when disabled
    (``HERMES_WEBUI_NO_WARMUP=1``) or already started this process. The thread is
    a daemon and every failure inside it is logged, never raised.
    """
    global _warmup_started
    if _warmup_disabled():
        return None
    with _warmup_lock:
        if _warmup_started:
            return None
        _warmup_started = True
    thread = threading.Thread(
        target=_run_cold_start_warmup,
        name='webui-cold-start-warmup',
        daemon=True,
    )
    thread.start()
    return thread


def start_cold_start_warmup_after_bind():
    """Post-bind entry point for server.py: start the warm-up, never raise.

    ``server.py`` calls this in ONE line after the socket has bound and before
    ``serve_forever()`` (the wiring lives here so the thin routing shell stays
    thin). Returns the warm-up thread, or None when the kill switch
    (``HERMES_WEBUI_NO_WARMUP=1``) is set or an attempt already ran this process;
    a failure to START the thread is logged, never raised, so startup always
    proceeds to serve. The warm-up's own lifecycle is documented in
    ARCHITECTURE.md ("Cold-start warm-up").
    """
    try:
        return start_cold_start_warmup()
    except Exception as e:
        print(f'[!!] WARNING: cold-start warm-up failed to start: {e}', flush=True)
        return None


def _run_cold_start_warmup() -> dict:
    """Warm the first-paint caches once; returns per-component stats.

    Never raises.

    Models: ``warm_models_catalog_provenance_if_cold()`` only — the disk-cache
    provenance publish. Deliberately NOT ``get_available_models()``: that can
    hold ``_available_models_cache_lock`` + ``_cache_build_in_progress`` for up
    to 60 s (waiting on an in-flight live probe), which would make the first
    user ``/api/models`` slower, not faster. The helper takes the lock
    non-blocking and reads disk only, so this step is bounded.

    Session list: claims the real default-shape cache key and drives the same
    builder the route uses, as a background rebuild — the builder writes what
    the request path already writes (the session index / sidecars), not state.db.
    """
    started = time.monotonic()
    stats: dict = {}

    t0 = time.monotonic()
    try:
        from api.config import warm_models_catalog_provenance_if_cold

        warm_models_catalog_provenance_if_cold()
        status = 'ok'
    except Exception as exc:
        status = f'failed ({type(exc).__name__}: {exc})'
    stats['models_provenance'] = {
        'status': status,
        'elapsed_ms': int((time.monotonic() - t0) * 1000),
    }

    t0 = time.monotonic()
    result: dict = {}
    try:
        from api.routes import warm_default_session_list_cache

        result = warm_default_session_list_cache(
            wait_timeout=_WARMUP_SESSION_WAIT_SECONDS
        )
        error = result.get('error')
        if error:
            status = f'failed ({error})'
        else:
            # The routes-side warm-up derives this from the cache itself: a
            # signaled claim event alone (builder exception, exhausted
            # invalidation retries, worker-start failure) is NOT success, so
            # 'ok' here means the slot really holds a fresh cache entry.
            status = str(result.get('status') or 'unknown')
    except Exception as exc:
        status = f'failed ({type(exc).__name__}: {exc})'
    stats['session_list'] = {
        'status': status,
        'elapsed_ms': int((time.monotonic() - t0) * 1000),
        'profiles': result.get('profiles') or [],
        'profiles_warmed': result.get('profiles_warmed') or 0,
        'profiles_considered': result.get('profiles_considered') or 0,
        'profiles_known': result.get('profiles_known') or 0,
        'capped': bool(result.get('capped')),
    }

    stats['total_ms'] = int((time.monotonic() - started) * 1000)
    session_list = stats['session_list']
    session_detail = f"{session_list['status']} {session_list['elapsed_ms']} ms"
    if session_list['profiles_considered']:
        session_detail += (
            f" profiles={session_list['profiles_warmed']}"
            f"/{session_list['profiles_considered']}"
        )
        if session_list['capped']:
            session_detail += f" (capped of {session_list['profiles_known']} known)"
        # The warm order (sticky/active first, then the process default, then
        # most-recently-used) — the per-profile outcome is the profiles= ratio
        # above, so an operator can tell which profile was warmed first.
        order = ",".join(
            str(entry.get("profile"))
            for entry in session_list['profiles']
            if isinstance(entry, dict) and entry.get("profile")
        )
        if order:
            session_detail += f" order={order}"
    print(
        f"[warmup] cold-start warm-up finished in {stats['total_ms']} ms "
        f"(models_provenance={stats['models_provenance']['status']} "
        f"{stats['models_provenance']['elapsed_ms']} ms; "
        f"session_list={session_detail})",
        flush=True,
    )
    return stats
