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
    """Run a git command and return (stdout, ok)."""
    try:
        r = subprocess.run(
            ['git'] + args, cwd=str(cwd), capture_output=True,
            text=True, timeout=timeout,
        )
        return r.stdout.strip(), r.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return '', False


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

    # Check for dirty working tree
    status_out, _ = _run_git(['status', '--porcelain'], path)
    stashed = False
    if status_out:
        _, ok = _run_git(['stash'], path)
        if not ok:
            return {'ok': False, 'message': 'Failed to stash local changes'}
        stashed = True

    # Pull with ff-only (no merge commits)
    pull_out, pull_ok = _run_git(['pull', '--ff-only', compare_ref], path, timeout=30)
    if not pull_ok:
        if stashed:
            _run_git(['stash', 'pop'], path)
        return {'ok': False, 'message': f'Pull failed: {pull_out[:200]}'}

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
