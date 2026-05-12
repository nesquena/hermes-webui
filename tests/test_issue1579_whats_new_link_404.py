"""Tests for issue #1579: What's new link can open a 404 GitHub compare page.

Bug shape:
  api/updates.py shipped current_sha=local-HEAD-short. When the local HEAD
  is not present upstream (unpushed work, dirty stage, fork, in-flight
  rebase, release-time merge commit), the resulting compare URL
  https://github.com/<repo>/compare/<localHEAD>...<upstream> returns
  GitHub's 404 page because <localHEAD> is not a public commit.

Fix:
  Use `git merge-base HEAD <compare_ref>` instead of `git rev-parse HEAD`.
  merge-base is the most recent commit both local and upstream agree on,
  and (since `git fetch` succeeded just before) it is guaranteed to exist
  in the upstream GitHub repo. If merge-base fails (shallow clone with
  divergent histories), fall back to current_sha=None — the JS link guard
  suppresses the link rather than emitting a known-broken URL.
"""

import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ── 1. Server-side: api.updates._check_repo uses merge-base, not HEAD ──

def _make_throwaway_repo(tmp_path, *, local_only_commits=0, upstream_advanced=0):
    """Create a tiny git repo with a fake 'origin' remote.

    Returns the local clone path. Set local_only_commits>0 to put commits
    on local HEAD that don't exist on origin (the #1579 trigger). Set
    upstream_advanced>0 to make the remote ahead.
    """
    upstream = tmp_path / 'upstream.git'
    subprocess.run(['git', 'init', '--quiet', '--bare', '--initial-branch=master', str(upstream)], check=True)

    seed = tmp_path / 'seed'
    subprocess.run(['git', 'init', '--quiet', '--initial-branch=master', str(seed)], check=True)
    for cmd in [
        ['git', '-C', str(seed), 'config', 'user.email', 'test@test.test'],
        ['git', '-C', str(seed), 'config', 'user.name', 'test'],
        ['git', '-C', str(seed), 'commit', '--allow-empty', '-m', 'initial', '--quiet'],
        ['git', '-C', str(seed), 'remote', 'add', 'origin', str(upstream)],
        ['git', '-C', str(seed), 'push', '--quiet', '-u', 'origin', 'master'],
    ]:
        subprocess.run(cmd, check=True)

    # Clone FIRST — local and upstream share the initial commit only.
    local = tmp_path / 'local'
    subprocess.run(['git', 'clone', '--quiet', str(upstream), str(local)], check=True)
    subprocess.run(['git', '-C', str(local), 'config', 'user.email', 'test@test.test'], check=True)
    subprocess.run(['git', '-C', str(local), 'config', 'user.name', 'test'], check=True)

    # Add local-only commits to the local clone (the #1579 trigger). These never
    # get pushed — they exist only on the local clone's master branch.
    for i in range(local_only_commits):
        subprocess.run(['git', '-C', str(local), 'commit', '--allow-empty',
                        '-m', f'local-only commit {i}', '--quiet'], check=True)

    # Advance upstream by committing on the seed and pushing — so local clone
    # is now `upstream_advanced` commits behind on the remote-tracking branch.
    for i in range(upstream_advanced):
        subprocess.run(['git', '-C', str(seed), 'commit', '--allow-empty',
                        '-m', f'upstream commit {i}', '--quiet'], check=True)
    if upstream_advanced:
        subprocess.run(['git', '-C', str(seed), 'push', '--quiet'], check=True)

    return local


def _short_sha(repo, ref):
    out = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--short', ref],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def test_current_sha_is_merge_base_not_local_HEAD(tmp_path, monkeypatch):
    """Reporter's exact scenario: local has unpushed commits, upstream advanced.

    Before #1579 fix: current_sha = local HEAD = unpublished SHA → URL 404s.
    After fix: current_sha = merge-base = the public ancestor commit → URL resolves.
    """
    # Clear cached config (api.updates may import HERMES_HOME at import time)
    repo = _make_throwaway_repo(
        tmp_path, local_only_commits=2, upstream_advanced=3,
    )

    head_sha = _short_sha(repo, 'HEAD')
    expected_base = _short_sha(repo, 'HEAD~2')  # merge-base in this scenario

    # Import updates with a stable CWD
    if 'api.updates' in sys.modules:
        del sys.modules['api.updates']
    from api import updates as upd

    result = upd._check_repo(repo, 'webui')

    assert result is not None, "non-bare repo with origin should return a result"
    assert result['behind'] == 3, f"expected 3 commits behind, got {result['behind']}"

    # The core fix: current_sha must be the merge-base, not local HEAD.
    # merge-base = HEAD~2 in this scenario (local has 2 unpushed commits,
    # so the most recent shared point with upstream is 2 commits before HEAD).
    assert result['current_sha'] == expected_base, (
        f"current_sha should be merge-base ({expected_base}), got {result['current_sha']} "
        f"(local HEAD is {head_sha}). Old #1579 bug regressed."
    )
    assert result['current_sha'] != head_sha, (
        f"current_sha must NOT be local HEAD ({head_sha}) — that's the #1579 bug."
    )
    # latest_sha is what _check_repo's own fetch+rev-parse returns
    assert result['latest_sha'], "latest_sha must be populated"
    # Critical compare-URL property: current_sha and latest_sha both correspond
    # to commits the upstream knows about (one by being upstream tip, the other
    # by being a shared ancestor). The merge-base is verifiable via the local
    # clone's remote-tracking branch:
    upstream_history = subprocess.run(
        ['git', '-C', str(repo), 'log', '--format=%h', 'origin/master'],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert result['current_sha'] in upstream_history or any(
        h.startswith(result['current_sha']) for h in upstream_history
    ), (
        f"current_sha ({result['current_sha']}) must be present in upstream history "
        f"— that's what guarantees the GitHub /compare/ URL won't 404."
    )


def test_current_sha_equals_HEAD_when_no_local_commits(tmp_path):
    """Backward-compat: pure-behind clone (no local-only commits) is unchanged.

    merge-base equals HEAD in this case — so the URL is identical to what
    we shipped before #1579.
    """
    repo = _make_throwaway_repo(tmp_path, local_only_commits=0, upstream_advanced=4)
    if 'api.updates' in sys.modules:
        del sys.modules['api.updates']
    from api import updates as upd
    result = upd._check_repo(repo, 'webui')

    head_sha = _short_sha(repo, 'HEAD')
    assert result['current_sha'] == head_sha, (
        "Pure-behind clone: merge-base equals HEAD; URL should be unchanged "
        "from pre-#1579 behavior."
    )
    assert result['behind'] == 4


def test_current_sha_falls_back_to_None_when_merge_base_fails(tmp_path):
    """Defensive: if merge-base errors (shallow clone, no shared history),
    return current_sha=None so the JS link guard suppresses the bad link
    rather than emitting one that 404s.
    """
    repo = _make_throwaway_repo(tmp_path, local_only_commits=0, upstream_advanced=1)
    if 'api.updates' in sys.modules:
        del sys.modules['api.updates']
    from api import updates as upd

    # Patch _run_git so any 'merge-base' call returns failure
    real_run = upd._run_git

    def fake_run(args, *a, **kw):
        if args and args[0] == 'merge-base':
            return ('', False)
        return real_run(args, *a, **kw)

    with patch.object(upd, '_run_git', side_effect=fake_run):
        result = upd._check_repo(repo, 'webui')

    assert result is not None
    assert result['current_sha'] is None, (
        "merge-base failure must fall back to None so JS suppresses the link "
        "(emitting a known-broken URL is worse than no link)."
    )
    # latest_sha should still be populated — that path doesn't depend on merge-base
    assert result['latest_sha']


# ── 2. Client-side: ui.js link guard suppresses URL on null current_sha ──

def _read_ui_js():
    return (REPO_ROOT / 'static' / 'ui.js').read_text(encoding='utf-8')


def test_whats_new_link_resets_display_and_href_on_every_render():
    """Without reset, a stale link from a prior banner can stay visible after
    a re-render where the new payload has current_sha=None.
    """
    src = _read_ui_js()
    # Find the "What's new" wiring block (~50-line window)
    idx = src.find("Wire up \"What's new?\" link")
    assert idx != -1, "What's-new link wiring block not found"
    block = src[idx:idx + 800]

    # Reset must happen BEFORE the conditional href set
    reset_idx = block.find("style.display='none'")
    set_idx = block.find("style.display='inline'")
    href_clear_idx = block.find("removeAttribute('href')")
    href_set_idx = block.find("link.href=repoUrl")

    assert reset_idx != -1, "Missing display='none' reset on every render"
    assert href_clear_idx != -1, "Missing removeAttribute('href') reset on every render"
    assert reset_idx < set_idx, "display reset must precede inline set"
    assert href_clear_idx < href_set_idx, "href clear must precede href assignment"


def test_whats_new_link_suppressed_when_curSha_falsy():
    """The conditional must guard on all three of repoUrl/curSha/newSha."""
    src = _read_ui_js()
    idx = src.find("Wire up \"What's new?\" link")
    block = src[idx:idx + 800]
    # Match "if(repoUrl && curSha && newSha)" with arbitrary whitespace
    pattern = re.compile(r'if\s*\(\s*repoUrl\s*&&\s*curSha\s*&&\s*newSha\s*\)')
    assert pattern.search(block), (
        "Link must require all three of repoUrl, curSha, newSha to be truthy. "
        "If any is null/empty, link stays display:none."
    )


# ── 3. End-to-end: simulate the exact reporter URL shape ──

def test_reporter_url_shape_no_longer_produces_invalid_compare_url(tmp_path):
    """Reporter saw https://github.com/.../compare/c660c7f...86cb22e where
    c660c7f was an unpublished local SHA. After fix, the URL should use
    a SHA that exists upstream.
    """
    repo = _make_throwaway_repo(tmp_path, local_only_commits=2, upstream_advanced=5)
    if 'api.updates' in sys.modules:
        del sys.modules['api.updates']
    from api import updates as upd
    result = upd._check_repo(repo, 'webui')

    head_sha = _short_sha(repo, 'HEAD')
    base_sha = _short_sha(repo, 'HEAD~2')  # the merge-base

    # The compare URL the JS would build
    cur, latest = result['current_sha'], result['latest_sha']
    # In a real run repo_url is converted from origin's URL; in this test the
    # value will be a file:// path, but that's fine — what we care about is
    # the cur and latest shas.
    assert cur == base_sha
    assert cur != head_sha, "Must not use local HEAD (the #1579 reporter URL bug)"

    # The "merge-base...upstream-tip" URL is by construction valid because
    # both endpoints exist on the upstream (one by being the upstream tip,
    # the other by being a shared ancestor of upstream and local).
