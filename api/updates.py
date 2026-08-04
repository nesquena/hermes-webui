"""
Hermes Web UI -- Self-update checker.

Checks if the webui and hermes-agent git repos are behind their latest
release tags. Results are cached server-side (30-min TTL) so git fetch runs
at most twice per hour regardless of client count.

Skips repos that are not git checkouts (e.g. Docker baked images where
.git does not exist).
"""
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlparse

from api.agent_health import get_active_profile_gateway_running_pid
from api.gateway_restart import restart_active_profile_gateway
from api.profiles import get_active_profile_name
from api.config import REPO_ROOT, STREAMS, STREAMS_LOCK

logger = logging.getLogger(__name__)

# Lazy -- may be None if agent not found
try:
    from api.config import _AGENT_DIR
except ImportError:
    _AGENT_DIR = None

_update_cache = {'webui': None, 'agent': None, 'checked_at': 0, 'include_agent': True, 'channel': 'stable'}
_SUMMARY_CACHE_MAX = 16
_summary_cache: OrderedDict = OrderedDict()
_cache_lock = threading.Lock()
_check_in_progress = False
_apply_lock = threading.Lock()   # prevents concurrent stash/pull/pop on same repo
CACHE_TTL = 1800  # 30 minutes
_AGENT_GATEWAY_RESTART_RETRY_DELAY_S = 1.0
_GIT_DIAGNOSTIC_MAX_CHARS = 300
_CREDENTIAL_IN_URL_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^/@\s'\"]+)@")
_GITHUB_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_QUERY_SECRET_RE = re.compile(r"([?&](?:access_token|oauth_token|private_token|client_secret|app_secret|api[_-]?key|token|password|secret|auth|key)=)[^&\s'\"]+", re.IGNORECASE)
_FETCH_NETWORK_FAILURE_SIGNATURES = (
    'could not resolve host',
    'failed to connect',
    'network is unreachable',
    'no route to host',
    'connection timed out',
    'timed out after',
    'connection reset by peer',
    'remote end hung up unexpectedly',
    'tls connection was non-properly terminated',
    'ssl certificate problem',
)
_RELEASE_TAG_RE = re.compile(r'^v[0-9][0-9A-Za-z.+-]*$')
# Phrases git emits when its own short-lived index/refs lock files block a
# subsequent operation. Tuned to match only the true "lock file already exists"
# semantics that warrant a lock-conflict response -- v2 deliberately drops the
# broad "lock file" substring from the prior version to avoid false positives
# on unrelated errors like "lock file lost during ref transaction".
# Matched case-insensitively in _is_git_lock_error().
_GIT_LOCK_SIGNATURES = (
    "index.lock': file exists",
    ".lock': file exists",
    'another git process seems to be running',
    'unable to create .git/index.lock',
)
# Lock files we previously enumerated for auto-removal in v2. v2.2 no longer
# removes anything on the server, so the enumerable list is no longer needed;
# ``_inventory_locks`` reports whatever ``.git/**/*.lock`` files currently exist
# via plain ``rglob``.



def _sanitize_git_diagnostic(output: str, *, limit: int = _GIT_DIAGNOSTIC_MAX_CHARS) -> str:
    """Return a user-facing git diagnostic with credentials removed.

    Git can echo remote URLs in failure output.  Keep the actionable error text,
    but strip URL userinfo, common GitHub token shapes, and secret-looking query
    parameter values before any message reaches the update-check API/UI.
    """
    if not output:
        return ""
    sanitized = _CREDENTIAL_IN_URL_RE.sub(r"\1<redacted>@", str(output))
    sanitized = _GITHUB_TOKEN_RE.sub("<redacted>", sanitized)
    sanitized = _QUERY_SECRET_RE.sub(r"\1<redacted>", sanitized)
    sanitized = sanitized.strip()
    if len(sanitized) > limit:
        sanitized = sanitized[:limit].rstrip() + "…"
    return sanitized


def _apply_fetch_failure_message(fetch_out: str, network_message: str) -> str:
    """Return the apply-path fetch failure message for the given stderr."""
    detail = _sanitize_git_diagnostic(fetch_out)
    if not detail:
        return network_message
    detail_lower = detail.lower()
    if any(signature in detail_lower for signature in _FETCH_NETWORK_FAILURE_SIGNATURES):
        return network_message
    return f'fetch failed: {detail}'


def _restart_blocker_snapshot() -> dict:
    """Return active chat work that should block a self-restart."""
    with STREAMS_LOCK:
        stream_ids = [str(k) for k in STREAMS.keys()]
    run_ids: list[str] = []
    try:
        from api import config as _config
        active_runs = getattr(_config, 'ACTIVE_RUNS', {})
        active_runs_lock = getattr(_config, 'ACTIVE_RUNS_LOCK', None)
        if active_runs_lock is not None:
            with active_runs_lock:
                run_ids = [str(k) for k in active_runs.keys()]
        else:
            run_ids = [str(k) for k in active_runs.keys()]
    except Exception:
        run_ids = []
    return {
        'active_streams': len(stream_ids),
        'active_runs': len(run_ids),
        'blocking_stream_ids': stream_ids[:10],
        'blocking_run_ids': run_ids[:10],
        'restart_blocked': bool(stream_ids or run_ids),
    }


def _active_stream_count() -> int:
    """Return the current in-memory chat stream count.

    Kept for compatibility with older tests/helpers; restart safety should use
    ``_restart_blocker_snapshot()`` so detached worker runs also block updates.
    """
    return int(_restart_blocker_snapshot().get('active_streams') or 0)


def _restart_blocked_response(target: str, blocker_snapshot: dict | int) -> dict:
    if isinstance(blocker_snapshot, int):
        blocker_snapshot = {
            'active_streams': blocker_snapshot,
            'active_runs': 0,
            'blocking_stream_ids': [],
            'blocking_run_ids': [],
            'restart_blocked': bool(blocker_snapshot),
        }
    active_streams = int(blocker_snapshot.get('active_streams') or 0)
    active_runs = int(blocker_snapshot.get('active_runs') or 0)
    parts = []
    if active_streams:
        parts.append(f"{active_streams} active chat stream{'s' if active_streams != 1 else ''}")
    if active_runs:
        parts.append(f"{active_runs} active agent run{'s' if active_runs != 1 else ''}")
    detail = ' and '.join(parts) or 'active chat work'
    return {
        'ok': False,
        'message': (
            f'Cannot update {target} while {detail} is running. '
            'Wait for the response to finish, then retry the update.'
        ),
        'target': target,
        'restart_blocked': True,
        'active_streams': active_streams,
        'active_runs': active_runs,
        'blocking_stream_ids': blocker_snapshot.get('blocking_stream_ids') or [],
        'blocking_run_ids': blocker_snapshot.get('blocking_run_ids') or [],
    }


def _wait_until_restart_safe(poll_seconds: float = 2.0, max_wait_seconds: float = 300.0) -> dict:
    """Wait for active work to finish before self-reexec.

    Bounded by ``max_wait_seconds`` so a long-running (or stuck/orphaned) agent
    run can't soft-jam the self-update indefinitely. If the deadline is reached
    while work is still in flight, the snapshot is returned with
    ``wait_timed_out=True`` so the caller can proceed with the re-exec anyway
    (preserving the pre-#3105 "execv preempts in-flight work" fallback) rather
    than holding ``_apply_lock`` for the run's full lifetime.
    """
    snapshot = _restart_blocker_snapshot()
    deadline = time.monotonic() + max(0.0, max_wait_seconds)
    while snapshot.get('restart_blocked'):
        if time.monotonic() >= deadline:
            logger.warning(
                "restart-safety wait exceeded %.0fs with work still in flight (%s); "
                "proceeding with re-exec anyway",
                max_wait_seconds, snapshot,
            )
            snapshot = dict(snapshot)
            snapshot['wait_timed_out'] = True
            return snapshot
        time.sleep(max(0.1, poll_seconds))
        snapshot = _restart_blocker_snapshot()
    return snapshot


def _run_git(args, cwd, timeout=10):
    """Run a git command and return (useful output, ok).

    On failure, returns stderr (or stdout as fallback) so callers can
    surface actionable git error messages instead of empty strings.
    """
    git_executable = _resolve_git_executable()
    if not git_executable:
        return 'git executable not found', False
    try:
        r = subprocess.run(
            [git_executable] + args, cwd=str(cwd), capture_output=True,
            text=True, timeout=timeout,
            encoding='utf-8', errors='replace',
        )
        # On non-UTF-8 locales (e.g. Chinese Windows GBK), a binary git
        # output that fails to decode used to leave r.stdout = None and crash
        # the whole import with AttributeError. Guard against None defensively.
        stdout = (r.stdout or '').strip()
        stderr = (r.stderr or '').strip()
        if r.returncode == 0:
            return stdout, True
        return stderr or stdout or f"git exited with status {r.returncode}", False
    except subprocess.TimeoutExpired as exc:
        detail = (getattr(exc, 'stderr', None) or getattr(exc, 'stdout', None) or '').strip()
        return detail or f"git {' '.join(args)} timed out after {timeout}s", False
    except FileNotFoundError:
        return 'git executable not found', False
    except OSError as exc:
        return f'git failed to start: {exc}', False


def _is_git_lock_error(output: str) -> bool:
    if not output:
        return False
    lower_out = output.lower()
    return any(sig in lower_out for sig in _GIT_LOCK_SIGNATURES)


def _inventory_locks(path: Path) -> dict:
    """Return a snapshot of lock files currently present under ``path/.git``.

    v2.2: replaced v2's `_is_lock_held` + `_try_remove_lock` machinery with
    pure inventory. Round-2 cert (gate-fail) proved that `fcntl.flock`
    cannot detect a live git lock, because git uses `O_CREAT|O_EXCL` and
    `rename(2)`, NOT advisory locking. Any auto-delete path can therefore
    race against a running `git add` and corrupt the index. v2.2 stops
    deleting locks from the server entirely: the only thing that removes
    a lock is the user, on the host, via the manual command surfaced in
    the response. Once the lock is gone, the user re-clicks Update Now
    and the normal non-destructive apply path runs.
    """
    git_dir = path / '.git'
    out = {
        'well_known_lock_present': False,  # ``.git/index.lock`` exists?
        'well_known_lock_path': None,      # absolute path of ``.git/index.lock``
        'other_locks': [],                  # any other lock files, by relative path
    }
    if not git_dir.exists():
        return out
    well_known = git_dir / 'index.lock'
    try:
        out['well_known_lock_present'] = well_known.exists()
    except OSError:
        # Permission problem reading the directory -- treat conservatively.
        out['well_known_lock_present'] = True
    out['well_known_lock_path'] = str(well_known)

    # Enumerate every other lock file under .git/ for diagnostic reporting.
    # We never touch them; this is purely an inventory.
    try:
        for entry in sorted(git_dir.rglob('*.lock')):
            try:
                rel = entry.relative_to(git_dir).as_posix()
            except ValueError:
                continue
            if rel == 'index.lock':
                continue
            out['other_locks'].append(rel)
    except OSError:
        # rglob can fail on unreadable subtrees; skip quietly.
        pass
    return out


def apply_clear_lock(target: str) -> dict:
    """Manual-instruction lock recovery for ``target``.

    v2.2: NEVER removes a lock file. Strategy:

      - If ``.git/index.lock`` is absent: re-run the normal non-destructive
        apply path so the user lands on the latest version without ever
        touching destructive git operations.
      - If ``.git/index.lock`` is present: do NOT touch it -- the server
        has no reliable proof that no live git process is still using
        it (round-2 cert showed `fcntl.flock` does not detect git's
        actual ``O_CREAT|O_EXCL`` locking). Return a response with the
        exact manual command the operator can run, plus the inventory of
        any other lock files so they can investigate. The frontend then
        surfaces a copyable ``rm`` line and a "I've removed the lock --
        try update again" button that re-invokes this endpoint, which
        (now that the lock is gone) will take the success branch and
        re-run the normal apply.
    """
    blocker_snapshot = _restart_blocker_snapshot()
    if blocker_snapshot.get('restart_blocked'):
        return _restart_blocked_response(target, blocker_snapshot)

    if not _apply_lock.acquire(blocking=False):
        return {'ok': False, 'message': 'Update already in progress'}

    try:
        if target == 'webui':
            path = REPO_ROOT
        elif target == 'agent':
            path = _AGENT_DIR
        else:
            return {'ok': False, 'message': f'Unknown target: {target}'}

        if path is None or not (path / '.git').exists():
            return {'ok': False, 'message': 'Not a git repository'}

        inv = _inventory_locks(path)
        manual_command = f"rm -f {inv['well_known_lock_path']}"

        if not inv['well_known_lock_present']:
            # Lock is gone. Run the normal non-destructive update flow and
            # annotate the response with what we found for the user's
            # records. Pass the configured channel through — otherwise an
            # experimental-channel WebUI lock-recovery retry silently falls back
            # to stable (Codex gate: _apply_update_inner defaults to stable).
            with _cache_lock:
                _update_cache['checked_at'] = 0
            retry_result = _apply_update_inner(target, _read_update_channel())
            retry_result = dict(retry_result)
            retry_result['lock_recovery'] = {
                'action': 'no-lock-found',
                'manual_command': manual_command,
                'other_locks': inv['other_locks'],
            }
            return retry_result

        # Lock is present. The server cannot prove it's safe to delete;
        # the only safe path is to ask the operator.
        message = (
            'A git lock file (.git/index.lock) is present. The server does '
            'not delete locks automatically -- git uses O_CREAT|O_EXCL '
            'locking, which cannot be detected with advisory probes. To '
            'recover: confirm no other git process is running against '
            f'this checkout, then run: {manual_command}  '
            'Click "Retry update" once you have removed it.'
        )
        return {
            'ok': False,
            'message': message,
            'lock_held': True,
            'target': target,
            'manual_command': manual_command,
            'well_known_lock_path': inv['well_known_lock_path'],
            'other_locks': inv['other_locks'],
        }
    finally:
        _apply_lock.release()


def _windows_git_from_registry():
    """Best-effort resolve git.exe from the Git-for-Windows registry key.

    Git for Windows records its install root at
    ``HKLM\\SOFTWARE\\GitForWindows\\InstallPath`` (and the WOW6432Node mirror
    for a 32-bit install on 64-bit Windows). ``git.exe`` lives under
    ``<InstallPath>\\cmd\\git.exe``. This is the reliable way to find git when
    it is installed but NOT on the launching process's PATH — e.g. the WebUI
    server started from a venv python whose environment does not inherit the
    interactive shell PATH, which otherwise degrades WEBUI_VERSION to
    ``'unknown'`` and freezes the ``?v=`` static-asset cache-busting stamp.
    """
    try:
        import winreg
    except ImportError:
        return None
    for hive, flag in (
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_CURRENT_USER, 0),
    ):
        try:
            with winreg.OpenKey(
                hive, r'SOFTWARE\GitForWindows', 0,
                winreg.KEY_READ | flag,
            ) as key:
                install_path, _ = winreg.QueryValueEx(key, 'InstallPath')
        except OSError:
            continue
        if not install_path:
            continue
        candidate = os.path.join(install_path, 'cmd', 'git.exe')
        if os.path.exists(candidate):
            return candidate
    return None


def _resolve_git_executable():
    git_executable = shutil.which('git')
    if git_executable:
        return git_executable
    if sys.platform == 'darwin' and os.path.exists('/usr/bin/git'):
        return '/usr/bin/git'
    if sys.platform == 'win32':
        from_registry = _windows_git_from_registry()
        if from_registry:
            return from_registry
        for candidate in (
            os.path.expandvars(r'%ProgramFiles%\Git\cmd\git.exe'),
            os.path.expandvars(r'%ProgramFiles(x86)%\Git\cmd\git.exe'),
            os.path.expandvars(r'%LocalAppData%\Programs\Git\cmd\git.exe'),
        ):
            if candidate and os.path.exists(candidate):
                return candidate
    return None


def _dirty_suffix(path: Path, timeout=1) -> str:
    """Return a best-effort ``-dirty`` suffix without blocking version display."""
    out, ok = _run_git(['diff-index', '--quiet', 'HEAD', '--'], path, timeout=timeout)
    if ok:
        return ""
    # diff-index --quiet exits 1 with no stdout/stderr to *signal* a dirty tree
    # (not an error). _run_git() substitutes a synthetic "git exited with
    # status N" diagnostic when both streams are empty, which makes the naive
    # `if not out` guard always false on dirty trees — silently dropping the
    # suffix and defeating dev-build cache busting (static/foo.js?v=… stays
    # identical to the last-committed version). Treat the synthetic shape as
    # the dirty signal; real errors (timeouts, missing git) carry a different
    # diagnostic and correctly suppress the suffix.
    if not out or out.startswith('git exited with status '):
        diff, diff_ok = _run_git(['diff', '--binary', 'HEAD', '--'], path, timeout=timeout)
        if diff_ok and diff:
            digest = hashlib.sha1(diff.encode('utf-8', errors='replace')).hexdigest()[:8]
            return f"-dirty-{digest}"
        return "-dirty"
    return ""


def _describe_git_version(path: Path, *, timeout=5, dirty_timeout=1) -> str | None:
    """Return a fast git version string for a checkout, if available."""
    out, ok = _run_git(['describe', '--tags', '--always'], path, timeout=timeout)
    if not (ok and out):
        return None
    return out + _dirty_suffix(path, timeout=dirty_timeout)


def _detect_webui_version() -> str:
    """Detect the running WebUI version from git or installed fallback files.

    Resolution order:
      1. ``git describe --tags --always --dirty`` — works in any git checkout.
         Returns the exact tag on tagged commits (e.g. ``v0.50.124``), a
         post-tag descriptor between releases (e.g. ``v0.50.124-1-ge91325d``),
         or a bare SHA when no tags exist (shallow clones, fresh forks).
      2. ``api/_version.py`` — a fallback written by the Docker / CI release
         workflow when ``.git`` is not present in the image.  Expected to define
         ``__version__ = 'vX.Y.Z'``.
      3. ``api/_scm_version.py`` — setuptools-scm output in an installed wheel.
         Its PEP 440 value is normalized to the channel-neutral ``v...`` form.
      4. ``'unknown'`` — last resort; displayed as-is in the settings badge.
    """
    # Timeout capped at 3s: git describe on a healthy local repo is <50ms;
    # a 10s stall on import (NFS-mounted .git, broken git binary) is unacceptable.
    out = _describe_git_version(REPO_ROOT)
    if out:
        return out

    # Docker / baked-image fallback: api/_version.py written by CI at build time.
    # Parse with regex rather than exec() — the file holds exactly one assignment
    # and regex is sufficient; exec() on a build artifact is an unnecessary surface.
    version_file = REPO_ROOT / 'api' / '_version.py'
    if version_file.exists():
        try:
            import re as _re
            m = _re.search(
                r"""__version__\s*=\s*['"]([^'"]+)['"]""",
                version_file.read_text(encoding='utf-8'),
            )
            if m:
                return m.group(1)
        except Exception:
            pass

    # Installed-wheel fallback: setuptools-scm writes a generated module that
    # is separate from the Docker/Nix-owned _version.py contract above.
    try:
        from api._scm_version import __version__ as scm_version
        scm_version = str(scm_version).strip()
        if scm_version:
            return scm_version if scm_version.startswith(('v', 'exp-v')) else f'v{scm_version}'
    except Exception:
        pass

    return 'unknown'


def _read_agent_source_version(agent_dir: Path) -> str | None:
    """Read Hermes Agent's package version from a copied source tree."""
    init_file = agent_dir / 'hermes_cli' / '__init__.py'
    try:
        text = init_file.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return None
    m = re.search(r"""__version__\s*=\s*['"]([^'"]+)['"]""", text)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return None


def _gateway_health_base_url() -> str:
    """Return the configured/default Hermes Agent gateway base URL."""
    raw = (
        os.environ.get('GATEWAY_HEALTH_URL')
        or os.environ.get('HERMES_GATEWAY_HEALTH_URL')
        or 'http://hermes-agent:8642'
    ).strip()
    if raw.endswith('/health/detailed'):
        raw = raw[: -len('/health/detailed')]
    elif raw.endswith('/health'):
        raw = raw[: -len('/health')]
    return raw.rstrip('/')


def _version_from_gateway_health_payload(payload: object) -> str | None:
    """Extract a version string from a Hermes Agent gateway health payload."""
    if not isinstance(payload, dict):
        return None
    for key in ('version', 'agent_version', 'hermes_version'):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = payload.get('agent')
    if isinstance(nested, dict):
        value = nested.get('version')
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _detect_agent_version_from_gateway_health(timeout: float = 0.75) -> str | None:
    """Best-effort cross-container gateway API fallback for Agent version."""
    base = _gateway_health_base_url()
    if not base:
        return None
    parsed = urlparse(base)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return None
    for path in ('/health', '/health/detailed'):
        try:
            with urllib.request.urlopen(f'{base}{path}', timeout=timeout) as resp:
                payload = json.loads(resp.read().decode('utf-8'))
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        version = _version_from_gateway_health_payload(payload)
        if version:
            return version
    return None


def _detect_agent_version() -> str:
    """Detect the running Hermes Agent version for UI display."""
    agent_dir = Path(_AGENT_DIR) if _AGENT_DIR is not None else None

    if agent_dir is not None:
        version_file = agent_dir / "VERSION"
        try:
            if version_file.exists():
                text = version_file.read_text(encoding='utf-8').strip()
                if text:
                    return text
        except Exception:
            pass

        # Fallback: infer from git describe when the checkout exists but no VERSION
        # file is available (common in source checkouts and developer environments).
        if agent_dir.exists():
            # Symmetric with _detect_webui_version() above — `--dirty` flags a
            # locally-modified checkout so operators can see when their agent has
            # uncommitted changes vs a clean tag. Per Opus advisor on stage-293.
            out = _describe_git_version(agent_dir)
            if out:
                return out

            # Docker two-container deployments often mount a copied agent source
            # tree without .git metadata or a VERSION file.  The package version
            # still lives in hermes_cli/__init__.py, so prefer that before giving
            # up or relying on a live gateway probe.
            source_version = _read_agent_source_version(agent_dir)
            if source_version:
                return source_version

    gateway_version = _detect_agent_version_from_gateway_health()
    if gateway_version:
        return gateway_version

    return 'not detected'


# Resolved once at import time — tags cannot change without a process restart.
WEBUI_VERSION: str = _detect_webui_version()
AGENT_VERSION: str = _detect_agent_version()


def _normalize_remote_url(remote_url):
    """Return the browser-facing repository URL for update compare links.

    Git remotes may be HTTPS or SSH and may include a literal ``.git`` suffix.
    Strip only that literal suffix — never use ``str.rstrip('.git')`` because it
    treats the argument as a character set and can truncate ``hermes-webui`` to
    ``hermes-webu``.
    """
    if not remote_url:
        return remote_url
    remote_url = remote_url.strip()
    if remote_url.startswith('git@'):
        remote_url = remote_url.replace(':', '/', 1).replace('git@', 'https://', 1)
    remote_url = remote_url.rstrip('/')
    if remote_url.endswith('.git'):
        remote_url = remote_url[:-4]
    return remote_url.rstrip('/')


def _build_compare_url(repo_url, current_sha, latest_sha):
    """Return a safe browser compare URL, or None when any piece is missing."""
    if not (repo_url and current_sha and latest_sha):
        return None
    parsed = urlparse(repo_url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return None
    return f"{repo_url}/compare/{current_sha}...{latest_sha}"


def _split_remote_ref(ref):
    """Split 'origin/branch-name' into ('origin', 'branch-name').

    Returns (None, ref) if ref contains no slash.
    """
    if '/' not in ref:
        return None, ref
    remote, branch = ref.split('/', 1)
    return remote, branch


def _detect_default_branch(path):
    """Detect the remote default branch (master or main)."""
    out, ok = _run_git(['symbolic-ref', 'refs/remotes/origin/HEAD'], path)
    if ok and out:
        # refs/remotes/origin/master -> master
        return out.split('/')[-1]
    # Fallback: try master, then main
    for branch in ('master', 'main'):
        _, ok = _run_git(['rev-parse', '--verify', f'origin/{branch}'], path)
        if ok:
            return branch
    return 'master'


# ── Release channels ─────────────────────────────────────────────────────────
# The self-updater tracks ONE of several release channels, selected in Settings
# (``update_channel``). A channel is nothing more than *which glob of tags the
# updater reads* on the single linear master line — no branches, no divergence,
# so every hard-won ff-only guarantee (#2653/#2846/#3140) is preserved.
#
#   stable       -> 'v*'        promoted, soaked releases (the default). Same glob
#                                the updater has always used — every existing
#                                v0.51.N tag matches, so legacy installs and the
#                                full existing test suite keep working unchanged.
#   experimental -> 'exp-v*'    every release batch, tagged for testers who opt in.
#
# ``exp-v*`` deliberately does NOT match ``v*`` (exp tags start with 'e', not
# 'v'): the two channels never leak into each other's tag list, and a legacy
# install running the historical 'v*' glob never matches an exp tag, so it
# auto-lands on the stable stream with zero action.
DEFAULT_UPDATE_CHANNEL = 'stable'
_CHANNEL_TAG_GLOBS = {
    'stable': 'v*',
    'experimental': 'exp-v*',
}


def _normalize_channel(channel) -> str:
    """Return a known channel name, defaulting to stable for anything unknown."""
    if isinstance(channel, str) and channel in _CHANNEL_TAG_GLOBS:
        return channel
    return DEFAULT_UPDATE_CHANNEL


def _channel_tag_glob(channel) -> str:
    """Return the ``git tag --list`` glob for the given channel."""
    return _CHANNEL_TAG_GLOBS[_normalize_channel(channel)]


def _read_update_channel() -> str:
    """Read the configured update channel from settings (stable fallback).

    Read lazily at request time — never baked at import — so a channel switch in
    Settings takes effect on the next update check without a process restart.
    """
    try:
        from api.config import load_settings
        return _normalize_channel(load_settings().get('update_channel'))
    except Exception:
        return DEFAULT_UPDATE_CHANNEL


def channel_version_badge(channel=None) -> str:
    """Return a channel-scoped version string for the Settings display badge ONLY.

    This is DELIBERATELY separate from ``WEBUI_VERSION``. ``WEBUI_VERSION`` is
    load-bearing in exact-string-equality systems — asset cache-busting URLs, the
    service-worker CACHE_NAME, the models-cache stamp, and the stale-client skew
    banner — so it must stay channel-neutral and stable for the process lifetime.
    Making it channel-dependent would falsely trip "hard refresh" banners and
    spurious cache rebuilds on every channel flip. This helper is read at request
    time purely to render ``WebUI: v0.52.47 · Experimental`` in Settings.

    Returns the channel-matched ``git describe`` (e.g. ``v0.52.47`` on stable,
    ``exp-v0.52.51`` on experimental), or falls back to ``WEBUI_VERSION`` when no
    channel tag is reachable (fresh clone, Docker image without channel tags).
    """
    if channel is None:
        channel = _read_update_channel()
    channel = _normalize_channel(channel)
    # NOTE: no ``--always`` here (deliberately different from _detect_webui_version).
    # The current version is channel-INDEPENDENT — it's just what's installed. The
    # channel only picks which tag family we compare AGAINST for updates. On a
    # stable-tagged install (e.g. HEAD == v0.52.0) that opts into Experimental, no
    # ``exp-v*`` tag is reachable BEHIND HEAD (the exp tags sit ahead on master), so
    # ``--always`` would fall through to a bare SHA and render "WebUI: d4e80b45 ·
    # Experimental" instead of the real installed version. Falling back to the
    # channel-neutral WEBUI_VERSION keeps the badge showing "v0.52.0 · Experimental".
    # (#5862)
    out, ok = _run_git(
        ['describe', '--tags', '--match', _channel_tag_glob(channel)],
        REPO_ROOT,
    )
    if ok and out:
        return out + _dirty_suffix(REPO_ROOT)
    return WEBUI_VERSION


def _release_tags(path, channel=DEFAULT_UPDATE_CHANNEL):
    """Return the channel's release tags newest-first, in version-sort order."""
    glob = _channel_tag_glob(channel)
    out, ok = _run_git(['tag', '--list', glob, '--sort=-v:refname'], path)
    if not (ok and out):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _current_release_tag(path, channel=DEFAULT_UPDATE_CHANNEL):
    """Return the latest channel release tag reachable from HEAD, if one exists.

    MUST filter by the channel glob (``--match``): a commit tagged BOTH
    ``v0.52.0`` and ``exp-v0.52.0`` describes as ``exp-v0.52.0`` (git prefers the
    lexically-later tag), so an unfiltered ``describe`` would make stable-channel
    math resolve to the experimental tag and fall through to the branch firehose.
    """
    out, ok = _run_git(
        ['describe', '--tags', '--abbrev=0', '--match', _channel_tag_glob(channel)],
        path,
    )
    return out if ok and out else None


def _release_gap(tags, current, latest):
    """Count release tags between current and latest in a newest-first list."""
    if not latest or current == latest:
        return 0
    if current in tags:
        return tags.index(current)
    return 1


def _count_channel_tags_ahead(path, channel=DEFAULT_UPDATE_CHANNEL):
    """Count channel release tags strictly ahead of HEAD (fast-forwardable).

    Used only when NO channel tag is reachable behind HEAD — the channel-scoped
    ``describe`` returned None — e.g. a stable ``v0.52.0`` install opting into
    Experimental (all ``exp-v*`` tags sit ahead on master). ``_release_gap`` can't
    position HEAD in the tag list then and returns a bogus 1. ``git tag --contains
    HEAD`` lists tags whose history includes HEAD, i.e. tags that are ahead of (or
    on) HEAD; since HEAD carries no channel tag in this path, that count is exactly
    the number of channel releases the install can fast-forward to. (#5862)
    """
    out, ok = _run_git(
        ['tag', '--list', _channel_tag_glob(channel), '--contains', 'HEAD'],
        path,
    )
    if not (ok and out):
        return 0
    return sum(1 for line in out.splitlines() if line.strip())


def _release_tag_sort_key(tag):
    """Return a version-sort key that keeps release tags newest-first."""
    raw = str(tag or '').strip()
    if raw.startswith('v'):
        raw = raw[1:]
    parts = []
    for chunk in re.split(r'(\d+)', raw):
        if not chunk:
            continue
        parts.append((0, int(chunk)) if chunk.isdigit() else (1, chunk.lower()))
    return tuple(parts)


def _is_stable_release_tag(tag):
    """Return True for stable release tags and False for prerelease tags."""
    raw = str(tag or '').strip()
    return bool(_RELEASE_TAG_RE.fullmatch(raw) and '-' not in raw[1:])


def _github_release_tags(url='https://api.github.com/repos/nesquena/hermes-webui/tags?per_page=100', *, timeout=3.0):
    """Return GitHub release tags newest-first, including commit SHAs when available."""
    request = urllib.request.Request(
        url,
        headers={
            'Accept': 'application/vnd.github+json',
            'User-Agent': 'hermes-webui',
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode('utf-8'))
    if not isinstance(payload, list):
        return []
    tags = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = item.get('name')
        if not isinstance(name, str):
            continue
        name = name.strip()
        if not _is_stable_release_tag(name):
            continue
        commit = item.get('commit')
        sha = None
        if isinstance(commit, dict):
            commit_sha = commit.get('sha')
            if isinstance(commit_sha, str):
                commit_sha = commit_sha.strip()
                if commit_sha:
                    sha = commit_sha
        tags.append({'name': name, 'sha': sha})
    return sorted(tags, key=lambda item: _release_tag_sort_key(item['name']), reverse=True)


def _check_webui_published_release_update():
    """Return a manual-update payload when the baked WebUI version trails GitHub tags."""
    current_version = str(WEBUI_VERSION or '').strip()
    if not _RELEASE_TAG_RE.fullmatch(current_version):
        return None
    try:
        tags = _github_release_tags()
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    if not tags:
        return None

    tag_names = [item['name'] for item in tags]
    if current_version not in tag_names:
        return None

    latest = tags[0]
    latest_version = latest['name']
    behind = _release_gap(tag_names, current_version, latest_version)
    if behind <= 0:
        return None

    current = next((item for item in tags if item['name'] == current_version), None) or {}
    current_ref = current.get('sha') or current_version
    latest_ref = latest.get('sha') or latest_version
    repo_url = 'https://github.com/nesquena/hermes-webui'
    return {
        'name': 'webui',
        'behind': behind,
        'current_sha': current_ref,
        'latest_sha': latest_ref,
        'branch': latest_version,
        'repo_url': repo_url,
        'release_based': True,
        'current_version': current_version,
        'latest_version': latest_version,
        'compare_url': _build_compare_url(repo_url, current_ref, latest_ref),
        'manual_update': True,
    }


def _head_is_past_latest_tag(path, current_tag, channel=DEFAULT_UPDATE_CHANNEL):
    """Return True when HEAD has moved past the latest reachable channel tag.

    `git describe --tags --always --match <glob>` returns the bare tag name
    (e.g. ``v2026.5.16``) when HEAD is exactly on the tag, and a
    ``v2026.5.16-608-g1d22b9c2`` suffix when HEAD has moved 608 commits past it.
    Used by both the update check and the update apply path so they agree on
    which ref to advance to — see #2653 (check side) and #2846 (apply side).

    The ``--match`` filter is mandatory: without it, a HEAD sitting on a commit
    that carries the other channel's tag would describe against that tag and
    give a wrong past/at answer for THIS channel.
    """
    if not current_tag:
        return False
    full_desc, ok = _run_git(
        ['describe', '--tags', '--always', '--match', _channel_tag_glob(channel)],
        path,
    )
    return bool(ok and full_desc and full_desc != current_tag)


def _head_contains_ref(path, ref):
    """Return True when ``ref`` is an ancestor of HEAD.

    Release-channel checks are tag-name based, but users tracking ``main`` can
    be on a commit that already contains the newest published tag. In that case
    a positive tag gap is not an available update; applying the tag would move
    backwards or fail fast-forward. Use the commit graph to detect that state.
    """
    if not ref:
        return False
    _, ok = _run_git(['merge-base', '--is-ancestor', ref, 'HEAD'], path)
    return bool(ok)


def _can_fast_forward_to(path, ref):
    """Return True when ``ref`` is a descendant of HEAD (``git pull --ff-only`` can reach it)."""
    if not ref:
        return False
    _, ok = _run_git(['merge-base', '--is-ancestor', 'HEAD', ref], path)
    return bool(ok)


def _select_apply_compare_ref(path, channel=DEFAULT_UPDATE_CHANNEL, target=None):
    """Return the same remote ref family that the update check reports.

    The update banner prefers published release tags when they exist. Applying
    an update must therefore advance to the latest release tag too; otherwise a
    checkout on a local/fork tracking branch can report release updates, pull a
    different branch that is already current, restart, and still remain behind.

    When HEAD is past the latest tag (the agent repo's day-to-day state between
    tagged releases), the check side falls through to the branch comparison via
    `_check_repo_release` returning None. The apply side must mirror that
    decision — otherwise we run `git pull --ff-only <latest-tag>` against a
    checkout that's already past the tag, no-op, restart, and the banner
    re-appears with the same N commits available. See #2846.

    CHANNEL SEMANTICS (webui only): the stable/experimental channels govern the
    WebUI repo. For ``target == 'webui'`` on the ``stable`` channel, stable tags
    are a *promoted subset* of master, so a stable install whose HEAD already
    contains the latest stable tag but sits behind master's tip must NOT fall
    through to the branch comparison (that would advance it to ``origin/master``
    — the full experimental firehose, defeating the channel). We return ``None``
    so the caller reports "no update". Every other case — the experimental
    channel, and the AGENT repo (which is a separate project that legitimately
    tracks master past its tags) — keeps the historical branch fallthrough
    unchanged. This mirrors ``_check_repo_release``.
    """
    channel = _normalize_channel(channel)
    suppress_stable_fallthrough = (channel == 'stable' and target == 'webui')
    tags = _release_tags(path, channel)
    if tags:
        latest_tag = tags[0]
        current_tag = _current_release_tag(path, channel)
        behind = _release_gap(tags, current_tag, latest_tag)
        # Mirror the check side exactly: fall through to the branch comparison
        # whenever the checkout has already moved past the release tag that the
        # banner would otherwise advertise. The common case is behind == 0 and
        # HEAD is past its nearest tag, but main-tracking checkouts can also
        # have behind > 0 after fetching a newer tag that HEAD already contains
        # (#3140). In both cases applying the tag would no-op, move backwards,
        # or fail fast-forward; branch comparison is the truthful update path.
        # Short-circuit `or` preserves the original minimal git-call pattern.
        if (
            (behind == 0 and _head_is_past_latest_tag(path, current_tag, channel))
            or (behind > 0 and _head_contains_ref(path, latest_tag))
            or (behind > 0 and not _can_fast_forward_to(path, latest_tag))
        ):
            # WebUI stable: "HEAD past/contains the latest stable tag" means
            # up-to-date on the promoted subset — NOT a signal to advance to
            # master. Return None so the caller reports no update.
            if suppress_stable_fallthrough:
                return None
            # Experimental / agent: preserve the historical branch fallthrough.
            pass
        else:
            return latest_tag

    upstream, ok = _run_git(['rev-parse', '--abbrev-ref', '@{upstream}'], path)
    if ok and upstream:
        return upstream

    branch = _detect_default_branch(path)
    return f'origin/{branch}'


def _channel_up_to_date_info(path, name, channel, current_tag):
    """Return an 'up to date' payload for a channel that must NOT branch-compare.

    Used by the stable channel: stable tags are a promoted subset of master, so
    when HEAD already contains the latest stable tag we report up-to-date
    (behind == 0) rather than falling through to the branch comparison, which
    would advance the user onto the experimental firehose.
    """
    remote_url, _ = _run_git(['remote', 'get-url', 'origin'], path)
    remote_url = _normalize_remote_url(remote_url)
    return {
        'name': name,
        'behind': 0,
        'current_sha': current_tag,
        'latest_sha': current_tag,
        'branch': current_tag,
        'repo_url': remote_url,
        'release_based': True,
        'current_version': current_tag,
        'latest_version': current_tag,
        'channel': channel,
    }


def _check_repo_release(path, name, channel=DEFAULT_UPDATE_CHANNEL):
    """Check if a git repo is behind its latest published channel release tag."""
    channel = _normalize_channel(channel)
    tags = _release_tags(path, channel)
    if not tags:
        return None

    latest_tag = tags[0]
    current_tag = _current_release_tag(path, channel)
    behind = _release_gap(tags, current_tag, latest_tag)

    # When NO channel tag is reachable behind HEAD, _current_release_tag returns
    # None (channel-scoped `describe --abbrev=0` fatals with "No tags can describe").
    # This is the normal state of a stable-tagged install (HEAD == v0.52.0) opting
    # into Experimental: every exp-v* tag sits AHEAD on master. _release_gap can't
    # position None in the tag list and returns a bogus 1, and the display fields
    # would carry current_version=None (rendered as "unknown"). Recover the real
    # ahead-count and show the channel-neutral installed version as the current
    # version — the channel only chooses the comparison tag family, not what's
    # installed. (#5862)
    current_version_display = current_tag
    # A git-verified ref for the compare link (defaults to the resolved channel
    # tag; may be refined below in the no-channel-tag-behind-HEAD fallback).
    current_sha_ref = current_tag
    if current_tag is None:
        ahead = _count_channel_tags_ahead(path, channel)
        if ahead > 0:
            behind = ahead
        # Scope the installed-version fallback to the WebUI repo only.
        # _check_repo_release() is shared with the Agent repo, and WEBUI_VERSION
        # (e.g. v0.52.0) is not a valid ref/tag in the Agent repository — injecting
        # it there would display the WebUI version as the Agent's installed version
        # and produce a broken Agent compare link. (#5864)
        if name == "webui":
            current_version_display = WEBUI_VERSION
            # For the compare link, derive a git-VERIFIED installed tag rather than
            # reusing WEBUI_VERSION (which can be `vX.Y.Z-dirty-<hash>`, `-N-g<sha>`,
            # a bare SHA, or `unknown` — none guaranteed refs). Prefer the exact tag
            # on HEAD across ALL release families (channel-neutral), so a stable-
            # pinned Experimental install still gets a resolvable /compare/<tag>...
            # link; fall back to None (no link) when HEAD is not exactly on a tag. (#5864)
            exact_tag, ok = _run_git(
                ['describe', '--tags', '--exact-match', 'HEAD'], path
            )
            exact_tag = (exact_tag or '').strip()
            current_sha_ref = exact_tag if ok and exact_tag else None

    # If behind == 0 but HEAD has moved past the tag (e.g. the agent repo
    # keeps committing to master between tagged releases), the release check
    # would report "Up to date" even though hundreds of commits are missing.
    # Fall through to _check_repo_branch so the real commit count is reported
    # instead. The same predicate is used by _select_apply_compare_ref so the
    # check and apply sides cannot drift again. See #2653 (check), #2846 (apply).
    #
    # CHANNEL (webui only): for the WebUI repo on stable, stable tags are a
    # promoted SUBSET of master, so "HEAD past the latest stable tag" means
    # up-to-date on the promoted subset, NOT a signal to branch-compare against
    # origin/master (the firehose). Report up-to-date. The AGENT repo and the
    # experimental channel keep the historical fall-through.
    suppress_stable_fallthrough = (channel == 'stable' and name == 'webui')
    if behind == 0 and _head_is_past_latest_tag(path, current_tag, channel):
        if suppress_stable_fallthrough:
            return _channel_up_to_date_info(path, name, channel, current_tag)
        return None

    # Users tracking main can already contain the newest fetched release tag
    # while their nearest reachable tag is older. A positive tag gap then means
    # only "there is a newer tag name", not "HEAD is behind that tag" (#3140).
    # Fall through to the branch check so the banner compares against the
    # configured upstream instead of advertising a tag that cannot fast-forward.
    if behind > 0 and _head_contains_ref(path, latest_tag):
        if suppress_stable_fallthrough:
            return _channel_up_to_date_info(path, name, channel, current_tag)
        return None

    # Patch releases can land on a side branch while day-to-day installs track
    # main past an older tag. A positive tag-name gap then advertises an update
    # that `git pull --ff-only <latest-tag>` cannot reach.
    if behind > 0 and not _can_fast_forward_to(path, latest_tag):
        if suppress_stable_fallthrough:
            return _channel_up_to_date_info(path, name, channel, current_tag)
        return None

    remote_url, _ = _run_git(['remote', 'get-url', 'origin'], path)
    remote_url = _normalize_remote_url(remote_url)

    return {
        'name': name,
        'behind': behind,
        # GitHub compare URLs accept tag names, and tag-to-tag links are the
        # clearest "what changed in this release?" view for operators. Use a
        # git-VERIFIED ref for the compare link: the resolved channel tag when
        # one is reachable behind HEAD, else None. WEBUI_VERSION is NOT safe here
        # — it can be `v0.52.0-dirty-<hash>`, `v0.52.0-N-g<sha>`, a bare SHA, or
        # `unknown`, none of which are guaranteed refs, so reusing it would emit
        # a broken /compare link (ui.js) and lose update-summary commit subjects. (#5864)
        'current_sha': current_sha_ref,
        'latest_sha': latest_tag,
        'branch': latest_tag,
        'repo_url': remote_url,
        'release_based': True,
        'current_version': current_version_display,
        'latest_version': latest_tag,
        'channel': channel,
    }


def _check_repo_branch(path, name, *, fetch=True):
    """Fallback: check if a git repo is behind its upstream branch."""

    # Fetch latest from origin (network call, cached by TTL)
    if fetch:
        _, fetch_ok = _run_git(['fetch', 'origin', '--quiet'], path, timeout=15)
        if not fetch_ok:
            return {'name': name, 'behind': 0, 'error': 'fetch failed'}

    # Use the current branch's upstream tracking branch, not the repo default.
    # This avoids false "N updates behind" alerts when the user is on a feature
    # branch and master/main has moved forward with unrelated commits.
    # If no upstream is set (brand-new local branch), fall back to the default branch.
    upstream, ok = _run_git(['rev-parse', '--abbrev-ref', '@{upstream}'], path)
    if ok and upstream:
        # upstream is like "origin/feat/foo" — use it directly in rev-list
        compare_ref = upstream
    else:
        branch = _detect_default_branch(path)
        compare_ref = f'origin/{branch}'

    # Count commits behind
    out, ok = _run_git(['rev-list', '--count', f'HEAD..{compare_ref}'], path)
    behind = int(out) if ok and out.isdigit() else 0

    # Get short SHAs for display.
    #
    # latest_sha = upstream tip (compare_ref). Always exists on github.com
    # because it is literally the commit `git fetch` just pulled.
    #
    # current_sha is trickier. The intuitive choice — local HEAD — breaks
    # the "What's new?" compare URL whenever HEAD is not a public commit:
    # unpushed work, dirty stage branches, forks, in-flight rebases, or
    # release-time merge commits whose SHA only lives in the maintainer's
    # checkout. We saw exactly this in #1579: a banner reporting "17 updates"
    # linked to /compare/<localHEAD>...<upstream> and 404'd because <localHEAD>
    # was never pushed to the canonical repo.
    #
    # The right base is the merge-base between HEAD and the upstream ref —
    # that's the most recent commit both sides agree on, and (because
    # `git fetch` succeeded above) it is guaranteed to be present upstream.
    # If a user is 17 commits behind with no local-only commits, merge-base
    # equals local HEAD and the URL is identical to what we shipped before;
    # if they ARE ahead with local-only commits, the URL still resolves to
    # the public history they share with upstream. If merge-base fails for
    # any reason (e.g. shallow clone where the bases diverge before the
    # cutoff), fall back to None so the JS link guard suppresses the link
    # rather than emitting a known-broken URL.
    mb_full, mb_ok = _run_git(['merge-base', 'HEAD', compare_ref], path)
    if mb_ok and mb_full:
        short, ok = _run_git(['rev-parse', '--short', mb_full], path)
        current = short if (ok and short) else None
    else:
        current = None
    latest, _ = _run_git(['rev-parse', '--short', compare_ref], path)

    # Get repo URL for "What's new?" link
    remote_url, _ = _run_git(['remote', 'get-url', 'origin'], path)
    remote_url = _normalize_remote_url(remote_url)

    return {
        'name': name,
        'behind': behind,
        'current_sha': current,
        'latest_sha': latest,
        'branch': compare_ref,
        'repo_url': remote_url,
        'compare_url': _build_compare_url(remote_url, current, latest),
    }


def _check_repo(path, name, channel=DEFAULT_UPDATE_CHANNEL):
    """Check if a git repo is behind its latest release. Returns dict or None.

    The returned dict (when not None) always carries a ``dirty: bool`` reflecting
    the working-tree state vs HEAD. A dirty install at-or-past the latest release
    tag used to silently report "Up to date" with no remediation affordance, so
    the Settings panel reads this flag to offer ``apply_force_update`` (issue
    #4085).

    When ``.git`` is absent (Docker images, pip installs), returns a minimal dict
    with ``no_git: True`` and ``behind: None`` so the frontend can distinguish
    "can't check" from "up to date" (issue #4356).
    """
    channel = _normalize_channel(channel)
    if path is None or not (path / '.git').exists():
        if name == 'webui':
            release_info = _check_webui_published_release_update()
            if release_info is not None:
                release_info = dict(release_info)
                release_info['no_git'] = True
                return release_info
        return {
            'name': name,
            'behind': None,
            'no_git': True,
        }

    # Fetch tags first so update prompts track published releases, not every
    # development commit that lands on master/main after the latest release.
    #
    # --force is required because the WebUI is a release-tracking consumer:
    # it never pushes tags, so it should always defer to whatever the remote
    # says a release tag points to. Without --force, a remote re-tag (e.g.
    # after a squash-merge that re-points a release tag at a new SHA) jams
    # the update path indefinitely with "would clobber existing tag" errors.
    # See #2756.
    fetch_out, fetch_ok = _run_git(['fetch', 'origin', '--tags', '--force'], path, timeout=15)
    if not fetch_ok:
        release_info = _check_repo_release(path, name, channel)
        message = 'fetch failed'
        if fetch_out:
            message = f'{message}: {_sanitize_git_diagnostic(fetch_out)}'
        if release_info is not None:
            release_info = dict(release_info)
            release_info['error'] = message
            release_info['stale_check'] = True
            release_info['dirty'] = _is_dirty(path)
            return release_info
        return {
            'name': name,
            'behind': None,
            'error': message,
            'stale_check': True,
            'dirty': _is_dirty(path),
        }

    release_info = _check_repo_release(path, name, channel)
    if release_info is not None:
        release_info = dict(release_info)
        release_info['dirty'] = _is_dirty(path)
        return release_info

    branch_info = _check_repo_branch(path, name, fetch=False)
    if branch_info is not None:
        branch_info = dict(branch_info)
        branch_info['dirty'] = _is_dirty(path)
        branch_info['channel'] = channel
        return branch_info
    return None


def _is_dirty(path: Path, timeout: int = 1) -> bool:
    """Return True when the working tree has uncommitted changes vs HEAD.

    Same primitive as ``_dirty_suffix`` (issue #4085): ``git diff-index
    --quiet HEAD --`` exits 0 on a clean tree and 1 on a dirty tree (not an
    error). Real errors (timeout, missing git, fatal) are conservatively
    reported as clean so a transient probe failure never produces a false-
    positive "local changes" alert.
    """
    out, ok = _run_git(['diff-index', '--quiet', 'HEAD', '--'], path, timeout=timeout)
    if ok:
        return False
    return not out or out.startswith('git exited with status ')


def _ignored_agent_update_info() -> dict:
    """Return a stable update-check payload for intentionally ignored Agent updates."""
    return {'name': 'agent', 'behind': 0, 'ignored': True}


def cached_update_status(*, include_agent=True, channel=None):
    """Return cached update status without performing network or git mutations."""
    include_agent = bool(include_agent)
    if channel is None:
        channel = _read_update_channel()
    channel = _normalize_channel(channel)
    with _cache_lock:
        cached = dict(_update_cache)
    # If the cache was populated for a different channel, it is not a valid
    # answer for this channel — signal that so callers don't render stale
    # cross-channel data as authoritative.
    if cached.get('channel') != channel:
        cached['channel'] = channel
        cached['stale_channel'] = True
    if cached.get('include_agent') != include_agent:
        cached['include_agent'] = include_agent
        if not include_agent:
            cached['agent'] = _ignored_agent_update_info()
    cached['cached'] = True
    return cached


def check_for_updates(force=False, *, include_agent=True, channel=None):
    """Return cached update status for webui and agent repos."""
    global _check_in_progress
    include_agent = bool(include_agent)
    if channel is None:
        channel = _read_update_channel()
    channel = _normalize_channel(channel)
    with _cache_lock:
        # Cache is only valid when BOTH the channel AND include_agent match —
        # a channel switch must not serve the previous channel's answer, and an
        # in-progress check for the other channel must not short-circuit this one
        # with a stale cross-channel payload (Codex SILENT #5).
        cache_matches = (
            _update_cache.get('include_agent') == include_agent
            and _update_cache.get('channel') == channel
        )
        if (
            not force
            and cache_matches
            and time.time() - _update_cache['checked_at'] < CACHE_TTL
        ):
            return dict(_update_cache)
        if _check_in_progress and cache_matches:
            return dict(_update_cache)  # another thread is already checking this channel
        _check_in_progress = True

    try:
        # Run checks outside the lock (network I/O)
        webui_info = _check_repo(REPO_ROOT, 'webui', channel)
        # The update channel is a WebUI-only concept. The Agent is a separate
        # project that tags plain v* and legitimately tracks master past its
        # tags; it must ALWAYS use the default channel regardless of the user's
        # WebUI channel selection. (Codex gate: passing 'experimental' here made
        # the Agent ignore its v* tags and fall back to origin/master.)
        agent_info = _check_repo(_AGENT_DIR, 'agent', DEFAULT_UPDATE_CHANNEL) if include_agent else _ignored_agent_update_info()

        with _cache_lock:
            _update_cache['webui'] = webui_info
            _update_cache['agent'] = agent_info
            _update_cache['checked_at'] = time.time()
            _update_cache['include_agent'] = include_agent
            _update_cache['channel'] = channel
            return dict(_update_cache)
    finally:
        _check_in_progress = False


def _repo_path_for_update_target(target: str):
    if target == 'webui':
        return REPO_ROOT
    if target == 'agent':
        return _AGENT_DIR
    return None


def _commit_subjects_for_update(info: dict, *, limit: int = 24) -> list[str]:
    """Return commit subjects for an update range, if the local git refs exist."""
    subjects, _truncated = _commit_subjects_for_update_with_limit(info, limit=limit)
    return subjects


def _commit_subjects_for_update_with_limit(info: dict, *, limit: int = 24) -> tuple[list[str], bool]:
    """Return recent commit subjects plus whether the local list was capped."""
    if not isinstance(info, dict):
        return [], False
    target = info.get('name')
    if target not in ('webui', 'agent'):
        target = 'webui' if info.get('repo_url', '').endswith('hermes-webui') else target
    path = _repo_path_for_update_target(target)
    if path is None or not (Path(path) / '.git').exists():
        return [], False
    current = str(info.get('current_sha') or '').strip()
    latest = str(info.get('latest_sha') or '').strip()
    if not (current and latest):
        return [], False
    probe_limit = max(1, int(limit)) + 1
    out, ok = _run_git(['log', '--format=%s', f'{current}..{latest}', f'-n{probe_limit}'], path, timeout=5)
    if not ok or not out:
        return [], False
    subjects = [line.strip() for line in out.splitlines() if line.strip()]
    truncated = len(subjects) > limit
    return subjects[:limit], truncated


def _summary_cache_key(updates: dict, details: list[dict]) -> str:
    """Stable key for the exact update range being summarized."""
    payload = []
    for item in details:
        payload.append({
            'name': item.get('name'),
            'behind': item.get('behind'),
            'current_sha': item.get('current_sha'),
            'latest_sha': item.get('latest_sha'),
            'compare_url': item.get('compare_url'),
        })
    blob = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


def _clean_summary_bullet(line: str) -> str:
    line = re.sub(r'^\s*(?:[-*•]+|\d+[.)])\s*', '', str(line or '')).strip()
    line = re.sub(r'\s+', ' ', line)
    if not line:
        return ''
    if line[-1] not in '.!?':
        line += '.'
    return line[:240]


def _split_summary_category(line: str) -> tuple[str | None, str]:
    raw = str(line or '').strip()
    match = re.match(r'^\s*(?:[-*•]+|\d+[.)])?\s*(notice|what you(?:ll|\'ll| will) notice|user(?:s)? will notice|worth knowing|worth|note)\s*:\s*(.+)$', raw, re.I)
    if not match:
        return None, raw
    label = match.group(1).lower()
    category = 'worth' if label in {'worth knowing', 'worth', 'note'} else 'notice'
    return category, match.group(2)


def _unique_summary_bullets(items: list[str]) -> list[str]:
    seen = set()
    bullets = []
    for item in items:
        cleaned = _clean_summary_bullet(item)
        key = cleaned.lower()
        if cleaned and key not in seen:
            bullets.append(cleaned)
            seen.add(key)
    return bullets


def _summary_bullets_from_text(text: str, *, fallback_items: list[str]) -> list[str]:
    raw = str(text or '').strip()
    candidates = []
    for line in raw.splitlines():
        _category, body = _split_summary_category(line)
        cleaned = _clean_summary_bullet(body)
        if cleaned:
            candidates.append(cleaned)
    if len(candidates) <= 1 and raw:
        candidates = [_clean_summary_bullet(part) for part in re.split(r'(?<=[.!?])\s+', raw)]
        candidates = [item for item in candidates if item]
    if not candidates:
        candidates = [_clean_summary_bullet(item) for item in fallback_items]
    bullets = _unique_summary_bullets(candidates)
    return bullets or ['Updates are available.']


def _categorized_summary_bullets_from_text(text: str) -> tuple[list[str], list[str]]:
    notice_items: list[str] = []
    worth_items: list[str] = []
    for line in str(text or '').splitlines():
        category, body = _split_summary_category(line)
        if category == 'notice':
            notice_items.append(body)
        elif category == 'worth':
            worth_items.append(body)
        elif re.match(r'^\s*(?:[-*•]+|\d+[.)])?\s*[A-Za-z][A-Za-z ]{1,32}\s*:', str(line or '')):
            notice_items.append(body)
    return _unique_summary_bullets(notice_items), _unique_summary_bullets(worth_items)


def _fallback_update_bullets(details: list[dict]) -> list[str]:
    bullets = []
    for item in details:
        label = item.get('label') or item.get('name') or 'Hermes'
        behind = item.get('behind') or 0
        commits = item.get('commits') or []
        if commits:
            highlights = '; '.join(commits[:3])
            qualifier = 'recent updates' if item.get('commits_truncated') else 'updates'
            bullets.append(f"{label} has {behind} update(s), including {qualifier}: {highlights}.")
        else:
            bullets.append(f"{label} has {behind} update(s) available.")
    return bullets or ['Updates are available.']


def _worth_knowing_bullets(details: list[dict]) -> list[str]:
    items = []
    truncated = [item for item in details if item.get('commits_truncated') and item.get('commits_limit')]
    for item in truncated[:2]:
        label = item.get('label') or item.get('name') or 'Hermes'
        behind = item.get('behind') or 0
        limit = item.get('commits_limit') or len(item.get('commits') or [])
        items.append(
            f"{label} has {behind} updates; this summary uses the latest {limit} commit subjects, with the full comparison still available in the diff link."
        )
    if items:
        return items
    targets = [
        f"{item.get('label') or item.get('name') or 'Hermes'} ({item.get('behind') or 0} update{'s' if (item.get('behind') or 0) != 1 else ''})"
        for item in details
        if item.get('behind')
    ]
    if len(targets) > 1:
        return ['This summary combines updates from ' + ' and '.join(targets) + '.']
    return []


def _format_update_summary_sections(summary_text: str, details: list[dict]) -> tuple[list[dict], str]:
    notice_items, worth_items = _categorized_summary_bullets_from_text(summary_text)
    if not notice_items:
        notice_items = _summary_bullets_from_text(summary_text, fallback_items=_fallback_update_bullets(details))
    notice_keys = {item.lower() for item in notice_items}
    worth_items = [item for item in worth_items if item.lower() not in notice_keys]
    worth_items.extend(
        item for item in _worth_knowing_bullets(details)
        if item.lower() not in notice_keys and item.lower() not in {existing.lower() for existing in worth_items}
    )
    sections = [
        {
            'title': "What you'll notice",
            'items': notice_items,
        },
    ]
    if worth_items:
        sections.append(
            {
                'title': 'Worth knowing',
                'items': worth_items,
            }
        )
    lines = []
    for section in sections:
        lines.append(section['title'])
        lines.extend(f"- {item}" for item in section['items'])
        lines.append('')
    return sections, '\n'.join(lines).strip()


def _fallback_update_summary(updates: dict, details: list[dict]) -> str:
    _sections, summary = _format_update_summary_sections('', details)
    return summary


def _update_summary_prompt(details: list[dict]) -> tuple[str, str]:
    system = (
        "You write human-readable release summaries for Hermes users. "
        "Focus on what the user will notice in the product. Keep it simple, specific, and short. "
        "avoid technical jargon, implementation details, SHA names, branch names, and file paths unless necessary. "
        "Return only bullets. Do not include headings, markdown tables, intro paragraphs, or closing notes."
    )
    user_lines = [
        "Summarize these available updates as concise bullets.",
        "Prefix each bullet with `Notice:` for user-visible behavior changes or `Worth knowing:` for useful context.",
        "Put user-visible Notice bullets first and include every meaningful user-facing change from the available commit subjects.",
        "Use Worth knowing only for helpful context that is not a duplicate of a Notice bullet.",
        "Use everyday language and explain visible behavior changes, not code mechanics.",
        "Return only prefixed bullets; the WebUI will add the fixed section headings separately.",
        "",
    ]
    for item in details:
        user_lines.append(f"{item['label']}: {item['behind']} commit(s) behind")
        commits = item.get('commits') or []
        if commits:
            if item.get('commits_truncated'):
                user_lines.append(
                    f"- Showing latest {len(commits)} of {item['behind']} commit subjects; summarize trends, not every commit."
                )
            user_lines.extend(f"- {subject}" for subject in commits)
        else:
            user_lines.append("- No local commit subjects available; summarize only the update count.")
        user_lines.append("")
    return system, '\n'.join(user_lines)


def summarize_update_payload(updates: dict, llm_callback=None, *, target: str | None = None, use_cache: bool = True) -> dict:
    """Build a human-readable What's New summary and keep regular diff comparison links.

    ``llm_callback`` receives ``(system_prompt, user_prompt)`` and returns text.
    The caller may wire that to AIAgent; this module keeps a deterministic
    fallback so the banner remains useful when no LLM provider is configured.
    Summaries are cached per exact update range so refreshes do not generate
    slightly different wording for the same available updates.
    """
    if not isinstance(updates, dict):
        updates = {}
    requested_target = target if target in ('webui', 'agent') else None
    details = []
    for key, label in (('webui', 'WebUI'), ('agent', 'Agent')):
        if requested_target and key != requested_target:
            continue
        info = updates.get(key)
        if not isinstance(info, dict) or int(info.get('behind') or 0) <= 0:
            continue
        commit_limit = 24
        commits, commits_truncated = _commit_subjects_for_update_with_limit({'name': key, **info}, limit=commit_limit)
        behind = int(info.get('behind') or 0)
        item = {
            'name': key,
            'label': label,
            'behind': behind,
            'current_sha': info.get('current_sha'),
            'latest_sha': info.get('latest_sha'),
            'compare_url': info.get('compare_url'),
            'commits': commits,
            'commits_limit': commit_limit,
            'commits_truncated': bool(commits_truncated or (commits and behind > len(commits))),
        }
        details.append(item)
    cache_key = _summary_cache_key(updates, details)
    if use_cache:
        with _cache_lock:
            cached = _summary_cache.get(cache_key)
            if cached:
                _summary_cache.move_to_end(cache_key)
        if cached:
            result = dict(cached)
            result['cached'] = True
            return result

    generated_by = 'fallback'
    candidate = ''
    if details and callable(llm_callback):
        system, prompt = _update_summary_prompt(details)
        try:
            candidate = (llm_callback(system, prompt) or '').strip()
            if candidate:
                generated_by = 'llm'
        except Exception:
            candidate = ''
    sections, summary = _format_update_summary_sections(candidate, details)
    result = {
        'ok': True,
        'summary': summary,
        'summary_sections': sections,
        'generated_by': generated_by,
        'cached': False,
        'cache_key': cache_key,
        'target': requested_target,
        'targets': details,
    }
    if use_cache:
        with _cache_lock:
            if len(_summary_cache) >= _SUMMARY_CACHE_MAX and cache_key not in _summary_cache:
                _summary_cache.popitem(last=False)
            _summary_cache[cache_key] = dict(result)
    return result


# ── Self-update application ───────────────────────────────────────────────────


def _purge_agent_pycache(repo_dir: Path) -> None:
    """Delete all __pycache__ dirs under *repo_dir* so the next import
    recompiles from source, avoiding stale-bytecode errors after git pull.

    ``os.execv()`` replaces the process image but does not touch the
    on-disk bytecode cache.  When a ``git pull`` writes new ``.py`` files
    whose mtime lands within the same second as the pre-existing ``.pyc``
    files, CPython may trust the stale cache and serve an old class
    definition.  The mismatch between cached class symbols and newly-imported
    supporting modules causes ``AttributeError`` (e.g. a method added in
    the same update is missing from the cached ``AIAgent`` class).

    This is safe to call right before ``os.execv()`` because the current
    process is about to be replaced — losing the bytecode cache is harmless
    and forces a clean recompilation on the next startup.
    """
    if repo_dir is None or not repo_dir.exists():
        return
    try:
        for pycache in repo_dir.rglob("__pycache__"):
            try:
                shutil.rmtree(pycache, ignore_errors=True)
            except OSError:
                pass
    except Exception:
        pass


def _schedule_restart(delay: float = 2.0) -> None:
    """Re-exec this process after *delay* seconds.

    Called after a successful update so that the freshly-pulled code is
    loaded on the next request, rather than running with a mix of old and
    new Python modules in sys.modules.

    os.execv() replaces the current process image with a fresh interpreter
    running the same argv — sessions are preserved on disk, the HTTP port
    is reclaimed within the delay window, and the client's own
    ``setTimeout(() => location.reload(), 2500)`` lands after the restart.

    Coordinates with ``_apply_lock``: when the user updates both webui
    and agent, the client POSTs them sequentially.  Without coordination
    the restart timer scheduled by the first update's success would fire
    while the second update's git-pull is still running, killing it mid-
    stream and leaving the second repo in an unknown partial state.
    Blocking on ``_apply_lock`` before ``os.execv`` means a pending
    second update always completes before the restart happens.
    """
    import os
    import sys

    def _do():
        import time
        time.sleep(delay)
        # Hold _apply_lock through os.execv so no new update can start between
        # the lock-release and the process replacement.  Any in-flight update
        # finishes first (since it holds the lock), and then the process is
        # replaced while still holding the lock — meaning no new update can
        # sneak in during the brief TOCTOU window that existed with the
        # original acquire-release-execv sequence.
        # Threads die when execv replaces the process image, so the lock is
        # released atomically by the kernel.
        with _apply_lock:
            _wait_until_restart_safe()
            # Purge bytecode caches so the new process imports from
            # current source.  Without this, Python may serve stale .pyc
            # files whose mtime matches the just-pulled .py files,
            # causing AttributeError when new methods are missing from
            # cached class definitions.
            if _AGENT_DIR is not None:
                _purge_agent_pycache(Path(_AGENT_DIR))
            _purge_agent_pycache(REPO_ROOT)
            try:
                # Re-exec into the just-pulled image.
                #
                # sys.argv[0]'s meaning depends on how the server was launched:
                #
                #   * Source checkout (`python server.py` via bootstrap.py /
                #     ctl.sh / start.sh): sys.argv[0] is the SCRIPT path
                #     (e.g. "/root/hermes-webui/server.py"), sys.executable is
                #     the interpreter. CPython treats argv[1] as the script to
                #     run, so we must pass [sys.executable] + sys.argv.
                #
                #   * Frozen/packaged build (PyInstaller, embedded zipapp,
                #     etc.): sys.argv[0] == sys.executable == <binary>. Passing
                #     [sys.executable] + sys.argv would re-insert the binary as
                #     argv[1] — the kernel launches it, the interpreter treats
                #     the binary itself as the "script" to run, and execv
                #     effectively becomes a recursive no-op that never reaches
                #     bind(), leaving the WebUI stuck "offline" after every
                #     self-update. Pass argv as-is instead.
                #
                # Distinguish the two cases with sys.frozen (set by
                # PyInstaller / zipapp / similar). For source checkouts the
                # `[sys.executable] + sys.argv` form is the canonical CPython
                # re-exec idiom (same shape Flask/Django reloaders use) and
                # is the correct path.
                #
                # IMPORTANT: On Windows, os.execv() does NOT replace the
                # current process — it spawns a new process while the old
                # one keeps running.  This causes "address already in use"
                # because the old process still holds the port.  On Windows
                # we use subprocess.Popen() + os._exit() instead.
                if sys.platform == 'win32':
                    import subprocess
                    if getattr(sys, "frozen", False):
                        args = sys.argv
                    else:
                        args = [sys.executable] + sys.argv
                    # Prefer pythonw.exe over python.exe so the restarted
                    # server does not create a visible console window.
                    # sys.executable may point at python.exe (console
                    # subsystem); substitute pythonw.exe if it exists
                    # next to python.exe.
                    _exe = sys.executable
                    if _exe.lower().endswith('python.exe'):
                        _w_exe = _exe[:-4] + 'w.exe'  # python.exe -> pythonw.exe
                        if os.path.isfile(_w_exe):
                            if getattr(sys, "frozen", False):
                                args = sys.argv
                            else:
                                args = [_w_exe] + sys.argv
                    # Start new process fully detached with NO console
                    # window.  DETACHED_PROCESS alone is not sufficient
                    # on modern Windows — without CREATE_NO_WINDOW a
                    # python.exe (console-subsystem) child still flashes
                    # an empty terminal window, which the user then
                    # manually kills (taking the WebUI with it).
                    subprocess.Popen(
                        args,
                        cwd=os.getcwd(),
                        creationflags=(
                            subprocess.DETACHED_PROCESS
                            | subprocess.CREATE_NEW_PROCESS_GROUP
                            | subprocess.CREATE_NO_WINDOW
                        ),
                        close_fds=True,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    # Exit immediately — the port is released as soon as
                    # this process dies, allowing the new process to bind.
                    os._exit(0)
                else:
                    if getattr(sys, "frozen", False):
                        os.execv(sys.executable, sys.argv)
                    else:
                        os.execv(sys.executable, [sys.executable] + sys.argv)
            except Exception:
                # Last-resort: if execv fails for any reason, just exit so the
                # process supervisor (start.sh / Docker) restarts us.
                os._exit(0)

    threading.Thread(target=_do, daemon=True).start()


def _ensure_gateway_restart_for_agent_update() -> tuple[bool, dict]:
    """Run the active-profile gateway restart when agent checkout changed.

    Returns:
        (ok, restart_payload) where:
        - ok is False when restart did not complete and callers must abort success.
        - restart_payload contains helper status fields for response shaping.
    """
    target_profile = str(get_active_profile_name() or "default").strip() or "default"
    gateway_pid_before_restart = get_active_profile_gateway_running_pid(profile=target_profile)
    restart_result = restart_active_profile_gateway(profile=target_profile)
    status = str(restart_result.get("status") or "")
    if status in {"completed", "in_progress"}:
        return True, restart_result
    if status != "failed":
        return False, restart_result

    # launchd can briefly fail to spawn the replacement gateway while it is
    # rotating the supervised process (#6045). Retry exactly once after a
    # bounded delay so an already-applied Agent update is not reported as a
    # complete failure because of that transient process handoff.
    time.sleep(_AGENT_GATEWAY_RESTART_RETRY_DELAY_S)
    retry_result = restart_active_profile_gateway(profile=target_profile)
    retry_status = str(retry_result.get("status") or "")
    if retry_status in {"completed", "in_progress"}:
        return True, {
            **retry_result,
            "retry_attempted": True,
            "initial_failure": restart_result.get("message"),
        }
    if retry_status != "failed":
        return False, {
            **retry_result,
            "retry_attempted": True,
            "initial_failure": restart_result.get("message"),
        }

    # A restart command can still exit non-zero after launchd has recovered the
    # service. Only accept that recovery when the confirmed local PID changed;
    # a merely-alive old gateway has not loaded the updated Agent checkout.
    time.sleep(_AGENT_GATEWAY_RESTART_RETRY_DELAY_S)
    gateway_pid_after_retry = get_active_profile_gateway_running_pid(profile=target_profile)
    if (
        gateway_pid_before_restart is not None
        and gateway_pid_after_retry is not None
        and gateway_pid_after_retry != gateway_pid_before_restart
    ):
        return True, {
            "status": "completed",
            "message": "Gateway service recovered after a transient restart failure",
            "retry_attempted": True,
            "process_replaced": True,
            "initial_failure": restart_result.get("message"),
            "retry_failure": retry_result.get("message"),
        }

    initial_message = str(restart_result.get("message") or "Restart failed")
    retry_message = str(retry_result.get("message") or "retry did not complete")
    return False, {
        **retry_result,
        "message": f"{initial_message}; recovery retry did not complete: {retry_message}",
        "retry_attempted": True,
        "initial_failure": restart_result.get("message"),
    }


def _agent_gateway_restart_failure_message(target: str, restart_result: dict) -> str:
    if restart_result.get("message"):
        return (
            f'{target} updated, but gateway restart did not complete: '
            f'{restart_result["message"]}. Run `hermes gateway restart` manually.'
        )
    return (
        f'{target} updated, but gateway restart did not complete. '
        'Run `hermes gateway restart` manually.'
    )


def apply_force_update(target: str, channel=None) -> dict:
    """Force-reset the target repo to the latest remote HEAD.

    Unlike apply_update() which requires a clean working tree and refuses
    merge conflicts, this discards all local modifications (checkout .) and
    resets to origin/<branch> — equivalent to what the diverged/conflict
    error messages ask the user to run manually.

    Should only be called when apply_update() has already returned a
    response with ``conflict: True`` or ``diverged: True`` and the user
    has confirmed they want to discard local changes.

    CHANNEL SAFETY (rewind guard): ``reset --hard`` is destructive. When the
    selected channel resolves to a ref that is an ANCESTOR of HEAD (i.e. the
    checkout is already ahead of the channel — e.g. an ex-experimental install
    switching back to stable), resetting to it would REWIND code and on-disk
    state. We refuse and return a clear message instead of silently downgrading.
    A deliberate rollback would be a separate, explicit feature.
    """
    if channel is None:
        channel = _read_update_channel()
    channel = _normalize_channel(channel)
    blocker_snapshot = _restart_blocker_snapshot()
    if blocker_snapshot.get('restart_blocked'):
        return _restart_blocked_response(target, blocker_snapshot)

    if not _apply_lock.acquire(blocking=False):
        return {'ok': False, 'message': 'Update already in progress'}
    try:
        if target == 'webui':
            path = REPO_ROOT
        elif target == 'agent':
            path = _AGENT_DIR
            # Channel is WebUI-only — the Agent always uses the default channel.
            channel = DEFAULT_UPDATE_CHANNEL
        else:
            return {'ok': False, 'message': f'Unknown target: {target}'}

        if path is None or not (path / '.git').exists():
            return {'ok': False, 'message': 'Not a git repository'}

        # NOTE: v2 of PR #5688 removed the prior stale-lock cleanup loop from
        # this entry point. The mtime-based heuristic was empirically proven
        # unsafe (a live `git add` was shown to hold .git/index.lock past 31 s
        # with unchanged mtime) and unconditional pre-cleanup clobbered locks
        # for force-update retries that had nothing to do with a lock error.
        # Lock cleanup is now ONLY performed by the explicit
        # /api/updates/clear_lock endpoint, where the user has opted in to
        # a non-destructive retry.

        # --force so a remote re-tag (e.g. squash-merge that re-points an
        # existing release tag) doesn't jam the apply path with "would clobber
        # existing tag". See #2756.
        fetch_out, fetch_ok = _run_git(['fetch', 'origin', '--quiet', '--tags', '--force'], path, timeout=15)
        if not fetch_ok:
            return {
                'ok': False,
                'message': _apply_fetch_failure_message(
                    fetch_out,
                    'Could not reach the remote repository. Check your connection.',
                ),
            }

        compare_ref = _select_apply_compare_ref(path, channel, target)
        # Stable channel, already up to date on the promoted subset: nothing to
        # force to. Do NOT fall back to origin/master (firehose). See
        # _select_apply_compare_ref channel semantics.
        if compare_ref is None:
            return {
                'ok': True,
                'message': f'{target} is already up to date on the {channel} channel.',
                'target': target,
                'up_to_date': True,
                'channel': channel,
            }

        # Rewind guard (Codex CORE #3): refuse to reset --hard onto a ref that
        # is an ANCESTOR of HEAD — that would downgrade the checkout. This is the
        # switch-back-to-stable-while-ahead case. A ref that is a descendant of
        # HEAD (normal update / opt-in to experimental) fast-forwards fine and is
        # allowed. Refs on a divergent line (neither ancestor nor descendant) are
        # the legitimate force-update case (conflict/diverged recovery) and are
        # also allowed — the guard fires ONLY on a pure-ancestor rewind.
        if _head_contains_ref(path, compare_ref) and not _can_fast_forward_to(path, compare_ref):
            return {
                'ok': False,
                'message': (
                    f'{target} is already ahead of the {channel} channel '
                    f'({compare_ref}); refusing to rewind the checkout. '
                    'Switching to a slower channel keeps your current version '
                    'until that channel catches up.'
                ),
                'target': target,
                'channel': channel,
                'refused_rewind': True,
            }
        # Discard local modifications and untracked colliders before resetting.
        # Do not use -x: ignored build/cache artifacts should survive force update.
        _run_git(['checkout', '.'], path)
        # Best-effort clean: a `git clean -fd` failure is NOT fatal. The
        # following `reset --hard` overwrites any tracked-file collisions
        # regardless, and residual untracked files that git can't delete are
        # harmless. In particular, on Windows a file named after a reserved
        # device name (nul, con, prn, aux, com1-9, lpt1-9) — which can appear
        # in the working tree when a shell command redirects to `> nul` under
        # Git Bash — cannot be removed via the normal Win32 path that git uses,
        # so `clean` exits non-zero. Aborting the whole force update over that
        # left users stuck (issue #4914). Log the stderr for diagnostics and
        # proceed to the reset, which is what actually applies the update.
        clean_out, clean_ok = _run_git(['clean', '-fd'], path)
        if not clean_ok:
            logger.warning(
                'force_apply_update: `git clean -fd` failed (non-fatal, '
                'continuing to reset --hard): %s',
                clean_out,
            )
        _, ok = _run_git(['reset', '--hard', compare_ref], path)
        if not ok:
            return {'ok': False, 'message': f'Force reset to {compare_ref} failed'}

        with _cache_lock:
            _update_cache['checked_at'] = 0

        if target == 'agent':
            gateway_ok, gateway_result = _ensure_gateway_restart_for_agent_update()
            if not gateway_ok:
                return {
                    'ok': False,
                    'message': _agent_gateway_restart_failure_message(target, gateway_result),
                    'target': target,
                    'gateway_restart': gateway_result.get('status'),
                }

        _schedule_restart()

        response = {
            'ok': True,
            'message': f'{target} force-updated to {compare_ref}',
            'target': target,
            'restart_scheduled': True,
        }
        if target == 'agent':
            response['gateway_restart'] = gateway_result.get('status')
        return response
    finally:
        _apply_lock.release()


def apply_update(target, channel=None):
    """Stash, pull --ff-only, pop for the given target repo."""
    if channel is None:
        channel = _read_update_channel()
    channel = _normalize_channel(channel)
    blocker_snapshot = _restart_blocker_snapshot()
    if blocker_snapshot.get('restart_blocked'):
        return _restart_blocked_response(target, blocker_snapshot)

    if not _apply_lock.acquire(blocking=False):
        return {'ok': False, 'message': 'Update already in progress'}
    try:
        return _apply_update_inner(target, channel)
    finally:
        _apply_lock.release()


def _restore_stash_after_pull_failure(
    target: str,
    path: Path,
    pull_out: str,
) -> str:
    """Best-effort re-apply of a stash pushed earlier in `_apply_update_inner`.

    Called when `git pull` failed with a lock error and we had already pushed
    a stash for the user's local modifications. Without this, the user's
    modifications remain in git stash with the working tree clean -- the
    wrong user experience because the failure was a lock conflict, not a
    stash-apply conflict, and the stash should re-apply cleanly.

    Returns a human-readable note for inclusion in the response message.
    """
    _, pop_ok = _run_git(['stash', 'pop'], path)
    if pop_ok:
        return ('Local modifications were restored from the temporary stash.')

    # `git stash pop` failed -- could be that the working tree changed under
    # us. Try apply + drop to keep the change separation explicit.
    _, apply_ok = _run_git(['stash', 'apply'], path)
    if apply_ok:
        _, _ = _run_git(['stash', 'drop'], path)
        return ('Local modifications were restored from the temporary stash.')

    detail = (pull_out or '').strip()[:200]
    return (
        'Your local modifications could not be restored automatically '
        f'(stash pop failed after pull error: {detail or "no detail"}). '
        'They remain safely in `git stash list`; run `git -C '
        + str(path) + ' stash pop` once the lock is cleared.'
    )


def _apply_update_inner(target, channel=DEFAULT_UPDATE_CHANNEL):
    """Inner implementation of apply_update, called under _apply_lock."""
    channel = _normalize_channel(channel)
    if target == 'webui':
        path = REPO_ROOT
    elif target == 'agent':
        path = _AGENT_DIR
        # Channel is WebUI-only — the Agent always uses the default channel
        # regardless of the user's WebUI selection (see check_for_updates).
        channel = DEFAULT_UPDATE_CHANNEL
    else:
        return {'ok': False, 'message': f'Unknown target: {target}'}

    if path is None or not (path / '.git').exists():
        return {'ok': False, 'message': 'Not a git repository'}

    # Fetch before attempting pull, so the remote ref is current.
    # --force so a remote re-tag doesn't block the update path (see #2756).
    fetch_out, fetch_ok = _run_git(['fetch', 'origin', '--quiet', '--tags', '--force'], path, timeout=15)
    if not fetch_ok:
        if _is_git_lock_error(fetch_out):
            return {
                'ok': False,
                'message': f'Fetch failed due to a repository lock: {fetch_out.strip()}',
                'lock_conflict': True,
            }
        return {
            'ok': False,
            'message': _apply_fetch_failure_message(
                fetch_out,
                'Could not reach the remote repository. Check your internet connection and try again.',
            ),
        }

    compare_ref = _select_apply_compare_ref(path, channel, target)
    # On the stable channel a None ref means HEAD already contains the latest
    # promoted stable tag (up-to-date on the promoted subset). Do NOT fall back
    # to origin/master — that would advance the user onto the experimental
    # firehose. Report success/no-op instead. See _select_apply_compare_ref.
    if compare_ref is None:
        return {
            'ok': True,
            'message': f'{target} is already up to date on the {channel} channel.',
            'target': target,
            'up_to_date': True,
            'channel': channel,
        }

    # Check for dirty working tree (ignore untracked files — git stash
    # doesn't include them, so stashing on '??' alone leaves nothing to pop)
    status_out, status_ok = _run_git(
        ['status', '--porcelain', '--untracked-files=no'], path
    )
    if not status_ok:
        if _is_git_lock_error(status_out):
            return {
                'ok': False,
                'message': f'Failed to inspect repo status due to a repository lock: {status_out.strip()}',
                'lock_conflict': True,
            }
        return {'ok': False, 'message': f'Failed to inspect repo status: {status_out[:200]}'}
    # Fail early on unresolved merge conflicts
    if any(line[:2] in {'DD', 'AU', 'UD', 'UA', 'DU', 'AA', 'UU'}
           for line in status_out.splitlines()):
        return {
            'ok': False,
            'message': (
                f'The local {target} repo has unresolved merge conflicts. '
                'To reset to the latest remote version run: '
                'git -C ' + str(path) + ' checkout . && '
                'git -C ' + str(path) + ' pull --ff-only'
            ),
            'conflict': True,
        }
    stashed = False
    if status_out:
        _, ok = _run_git(['stash', 'push', '-m', 'hermes-update-autostash'], path)
        if not ok:
            return {'ok': False, 'message': 'Failed to stash local changes'}
        stashed = True

    # Pull with ff-only (no merge commits).
    # Split tracking refs like 'origin/main' into separate remote + branch
    # arguments — git treats 'origin/main' as a repository name otherwise.
    remote, branch = _split_remote_ref(compare_ref)
    pull_args = ['pull', '--ff-only']
    if remote:
        pull_args.extend([remote, branch])
    else:
        pull_args.extend(['origin', compare_ref])
    pull_out, pull_ok = _run_git(pull_args, path, timeout=30)
    if not pull_ok:
        if _is_git_lock_error(pull_out):
            # Lock conflict during pull. If a stash was pushed for the local
            # modifications, attempt to restore it before returning so the
            # user's working tree is not silently left empty with changes
            # stranded in the stash (Greptile P1 on PR #5688).
            stash_recovery_note = ''
            if stashed:
                stash_recovery_note = _restore_stash_after_pull_failure(
                    target, path, pull_out
                )
            message = f'Pull failed due to a repository lock: {pull_out.strip()}'
            if stash_recovery_note:
                message = f'{message} {stash_recovery_note}'
            return {
                'ok': False,
                'message': message,
                'lock_conflict': True,
            }
        pull_lower = pull_out.lower()
        detail = pull_out.strip()[:300] if pull_out.strip() else '(no output from git)'
        untracked_collision = (
            'untracked working tree files would be overwritten' in pull_lower
        )
        diverged_failure = (
            'not possible to fast-forward' in pull_lower or 'diverged' in pull_lower
        )
        restored_stash = False
        stash_drop_failed = False
        if stashed:
            _, apply_ok = _run_git(['stash', 'apply'], path)
            if apply_ok:
                _, drop_ok = _run_git(['stash', 'drop'], path)
                restored_stash = True
                stash_drop_failed = not drop_ok
            else:
                _, reset_ok = _run_git(['reset', '--hard', 'HEAD'], path)
                if not reset_ok:
                    response = {
                        'ok': False,
                        'message': (
                            'Pull failed, and failed to clean up a stash-apply '
                            'conflict while restoring local changes. Manual '
                            'intervention needed: run git -C ' + str(path) + ' '
                            'reset --hard HEAD to remove conflict markers. Your '
                            'changes remain in the git stash. Pull error: '
                            + detail
                        ),
                        'stash_conflict': True,
                    }
                    if diverged_failure:
                        response['diverged'] = True
                    return response
                response = {
                    'ok': False,
                    'message': (
                        f'Pull failed, and your local {target} modifications '
                        'conflicted while restoring from stash. The index and '
                        'tracked files were restored to HEAD, and your changes '
                        'remain in the git stash. To inspect: git -C ' + str(path) + ' stash show -p. '
                        'To re-apply: git -C ' + str(path) + ' stash apply, then '
                        'resolve conflicts. Pull error: ' + detail
                    ),
                    'stash_conflict': True,
                }
                if diverged_failure:
                    response['diverged'] = True
                return response

        restored_note_parts = []
        if restored_stash:
            restored_note_parts.append(
                f'Local {target} modifications were restored to the working '
                'tree; save or stash them before running destructive recovery '
                'commands.'
            )
            if stash_drop_failed:
                restored_note_parts.append(
                    'The temporary stash entry may still be present because '
                    'git stash drop failed.'
                )
        restored_note = ' '.join(restored_note_parts)

        # Diagnose the most common failure modes and surface actionable messages.
        if diverged_failure:
            message_parts = [
                f'The local {target} repo has commits that are not on the remote '
                'branch, so a fast-forward update is not possible.'
            ]
            if restored_note:
                message_parts.append(restored_note)
            message_parts.append(
                'Run: git -C ' + str(path) + ' fetch origin && '
                'git -C ' + str(path) + ' reset --hard ' + compare_ref
            )
            return {
                'ok': False,
                'message': ' '.join(message_parts),
                'diverged': True,
            }
        if 'does not track' in pull_lower or 'no tracking information' in pull_lower:
            message_parts = [
                f'The local {target} branch has no upstream tracking branch configured.'
            ]
            if restored_note:
                message_parts.append(restored_note)
            message_parts.append(
                'Run: git -C ' + str(path) + ' branch --set-upstream-to=' + compare_ref
            )
            return {
                'ok': False,
                'message': ' '.join(message_parts),
            }
        # Generic fallback — include the raw git output for debugging.
        message_parts = [f'Pull failed: {detail}']
        if restored_note:
            message_parts.append(restored_note)
        response = {'ok': False, 'message': ' '.join(message_parts)}
        if untracked_collision:
            response['conflict'] = True
        return response

    # Re-apply stash if we stashed.
    stash_drop_failed = False
    if stashed:
        _, apply_ok = _run_git(['stash', 'apply'], path)
        if apply_ok:
            _, drop_ok = _run_git(['stash', 'drop'], path)
            stash_drop_failed = not drop_ok
        else:
            _, reset_ok = _run_git(['reset', '--hard', 'HEAD'], path)
            if not reset_ok:
                return {
                    'ok': False,
                    'message': (
                        'Updated successfully, but failed to clean up a '
                        'stash-apply conflict. Manual intervention needed: '
                        'run git -C ' + str(path) + ' reset --hard HEAD to '
                        'remove conflict markers. Your changes remain in the '
                        'git stash.'
                    ),
                    'stash_conflict': True,
                }
            with _cache_lock:
                _update_cache['checked_at'] = 0

            if target == 'agent':
                gateway_ok, gateway_result = _ensure_gateway_restart_for_agent_update()
                if not gateway_ok:
                    return {
                        'ok': False,
                        'message': _agent_gateway_restart_failure_message(target, gateway_result),
                        'target': target,
                        'gateway_restart': gateway_result.get('status'),
                    }
            _schedule_restart()
            response = {
                'ok': True,
                'message': (
                    f'{target} updated to the latest version. Your local '
                    'modifications conflicted with upstream changes and were '
                    'set aside in a git stash. To inspect: '
                    'git -C ' + str(path) + ' stash show -p. To re-apply: '
                    'git -C ' + str(path) + ' stash apply, then resolve '
                    'conflicts. Drop the stash after you are satisfied.'
                ),
                'target': target,
                'restart_scheduled': True,
                'stash_conflict': True,
            }
            if target == 'agent':
                response['gateway_restart'] = gateway_result.get('status')
            return response

    # Invalidate cache
    with _cache_lock:
        _update_cache['checked_at'] = 0

    if target == 'agent':
        gateway_ok, gateway_result = _ensure_gateway_restart_for_agent_update()
        if not gateway_ok:
            return {
                'ok': False,
                'message': _agent_gateway_restart_failure_message(target, gateway_result),
                'target': target,
                'gateway_restart': gateway_result.get('status'),
            }

    # Schedule a self-restart so the updated code is loaded fresh.  A plain
    # git pull leaves stale Python modules in sys.modules — agent imports that
    # reference new symbols (functions, classes) added in the update will fail
    # on the next request with AttributeError / ImportError.  os.execv() re-
    # execs the same interpreter with the same argv, picking up the new code
    # cleanly without requiring the user to restart manually.
    #
    # The 2 s delay gives the HTTP response time to flush to the client before
    # the process replaces itself.  The client already does
    # setTimeout(() => location.reload(), 1500) on success, so the page reload
    # and the restart land at roughly the same time.
    _schedule_restart()
    message = f'{target} updated successfully'
    if stash_drop_failed:
        message += (
            '. Local modifications were restored, but the temporary stash '
            'entry may still be present because git stash drop failed.'
        )

    response = {
        'ok': True,
        'message': message,
        'target': target,
        'restart_scheduled': True,
    }
    if target == 'agent':
        response['gateway_restart'] = gateway_result.get('status')
    return response
