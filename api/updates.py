"""
Hermes Web UI -- Self-update checker.

Checks if the webui and hermes-agent git repos are behind their upstream
branches. Results are cached server-side (30-min TTL) so git fetch runs
at most twice per hour regardless of client count.

Skips repos that are not git checkouts (e.g. Docker baked images where
.git does not exist).
"""
import subprocess
import threading
import time
from pathlib import Path

from api.config import REPO_ROOT

# Lazy -- may be None if agent not found
try:
    from api.config import _AGENT_DIR
except ImportError:
    _AGENT_DIR = None

_update_cache = {'webui': None, 'agent': None, 'checked_at': 0}
_cache_lock = threading.Lock()
_check_in_progress = False
_apply_lock = threading.Lock()   # prevents concurrent stash/pull/pop on same repo
CACHE_TTL = 1800  # 30 minutes


def _run_git(args, cwd, timeout=10):
    """Run a git command and return (useful output, ok).

    On failure, returns stderr (or stdout as fallback) so callers can
    surface actionable git error messages instead of empty strings.
    """
    try:
        r = subprocess.run(
            ['git'] + args, cwd=str(cwd), capture_output=True,
            text=True, timeout=timeout,
        )
        stdout = r.stdout.strip()
        stderr = r.stderr.strip()
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


def _detect_webui_version() -> str:
    """Detect the running WebUI version from git or a baked-in fallback file.

    Resolution order:
      1. ``git describe --tags --always --dirty`` — works in any git checkout.
         Returns the exact tag on tagged commits (e.g. ``v0.50.124``), a
         post-tag descriptor between releases (e.g. ``v0.50.124-1-ge91325d``),
         or a bare SHA when no tags exist (shallow clones, fresh forks).
      2. ``api/_version.py`` — a fallback written by the Docker / CI release
         workflow when ``.git`` is not present in the image.  Expected to define
         ``__version__ = 'vX.Y.Z'``.
      3. ``'unknown'`` — last resort; displayed as-is in the settings badge.
    """
    # Timeout capped at 3s: git describe on a healthy local repo is <50ms;
    # a 10s stall on import (NFS-mounted .git, broken git binary) is unacceptable.
    out, ok = _run_git(['describe', '--tags', '--always', '--dirty'], REPO_ROOT, timeout=3)
    if ok and out:
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

    return 'unknown'


# Resolved once at import time — tags cannot change without a process restart.
WEBUI_VERSION: str = _detect_webui_version()


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


def _check_repo(path, name):
    """Check if a git repo is behind its upstream. Returns dict or None."""
    if path is None or not (path / '.git').exists():
        return None

    # Fetch latest from origin (network call, cached by TTL)
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

    # Get short SHAs for display
    current, _ = _run_git(['rev-parse', '--short', 'HEAD'], path)
    latest, _ = _run_git(['rev-parse', '--short', compare_ref], path)

    return {
        'name': name,
        'behind': behind,
        'current_sha': current,
        'latest_sha': latest,
        'branch': compare_ref,
    }


def check_for_updates(force=False):
    """Return cached update status for webui and agent repos."""
    global _check_in_progress
    with _cache_lock:
        if not force and time.time() - _update_cache['checked_at'] < CACHE_TTL:
            return dict(_update_cache)
        if _check_in_progress:
            return dict(_update_cache)  # another thread is already checking
        _check_in_progress = True

    try:
        # Run checks outside the lock (network I/O)
        webui_info = _check_repo(REPO_ROOT, 'webui')
        agent_info = _check_repo(_AGENT_DIR, 'agent')

        with _cache_lock:
            _update_cache['webui'] = webui_info
            _update_cache['agent'] = agent_info
            _update_cache['checked_at'] = time.time()
            return dict(_update_cache)
    finally:
        _check_in_progress = False


def apply_update(target):
    """Stash, pull --ff-only, pop for the given target repo."""
    if not _apply_lock.acquire(blocking=False):
        return {'ok': False, 'message': 'Update already in progress'}
    try:
        return _apply_update_inner(target)
    finally:
        _apply_lock.release()


def _apply_update_inner(target):
    """Inner implementation of apply_update, called under _apply_lock."""
    if target == 'webui':
        path = REPO_ROOT
    elif target == 'agent':
        path = _AGENT_DIR
    else:
        return {'ok': False, 'message': f'Unknown target: {target}'}

    if path is None or not (path / '.git').exists():
        return {'ok': False, 'message': 'Not a git repository'}

    # Use the current branch's upstream for pull, matching the behaviour
    # of _check_repo. Falls back to default branch if no upstream is set.
    upstream, ok = _run_git(['rev-parse', '--abbrev-ref', '@{upstream}'], path)
    if ok and upstream:
        compare_ref = upstream
    else:
        branch = _detect_default_branch(path)
        compare_ref = f'origin/{branch}'

    # Fetch before attempting pull, so the remote ref is current.
    _, fetch_ok = _run_git(['fetch', 'origin', '--quiet'], path, timeout=15)
    if not fetch_ok:
        return {
            'ok': False,
            'message': (
                'Could not reach the remote repository. '
                'Check your internet connection and try again.'
            ),
        }

    # Check for dirty working tree (ignore untracked files — git stash
    # doesn't include them, so stashing on '??' alone leaves nothing to pop)
    status_out, status_ok = _run_git(
        ['status', '--porcelain', '--untracked-files=no'], path
    )
    if not status_ok:
        return {'ok': False, 'message': f'Failed to inspect repo status: {status_out[:200]}'}
    # Fail early on unresolved merge conflicts
    if any(line[:2] in {'DD', 'AU', 'UD', 'UA', 'DU', 'AA', 'UU'}
           for line in status_out.splitlines()):
        return {'ok': False, 'message': 'Repository has unresolved merge conflicts'}
    stashed = False
    if status_out:
        _, ok = _run_git(['stash'], path)
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
        pull_args.append(compare_ref)
    pull_out, pull_ok = _run_git(pull_args, path, timeout=30)
    if not pull_ok:
        if stashed:
            _run_git(['stash', 'pop'], path)

        # Diagnose the most common failure modes and surface actionable messages.
        pull_lower = pull_out.lower()
        if 'not possible to fast-forward' in pull_lower or 'diverged' in pull_lower:
            return {
                'ok': False,
                'message': (
                    f'The local {target} repo has commits that are not on the remote '
                    'branch, so a fast-forward update is not possible. '
                    'Run: git -C ' + str(path) + ' fetch origin && '
                    'git -C ' + str(path) + ' reset --hard ' + compare_ref
                ),
                'diverged': True,
            }
        if 'does not track' in pull_lower or 'no tracking information' in pull_lower:
            return {
                'ok': False,
                'message': (
                    f'The local {target} branch has no upstream tracking branch configured. '
                    'Run: git -C ' + str(path) + ' branch --set-upstream-to=' + compare_ref
                ),
            }
        # Generic fallback — include the raw git output for debugging.
        detail = pull_out.strip()[:300] if pull_out.strip() else '(no output from git)'
        return {'ok': False, 'message': f'Pull failed: {detail}'}

    # Pop stash if we stashed
    if stashed:
        _, pop_ok = _run_git(['stash', 'pop'], path)
        if not pop_ok:
            return {
                'ok': False,
                'message': 'Updated but stash pop failed -- manual merge needed',
                'stash_conflict': True,
            }

    # Invalidate cache
    with _cache_lock:
        _update_cache['checked_at'] = 0

    return {'ok': True, 'message': f'{target} updated successfully', 'target': target}
