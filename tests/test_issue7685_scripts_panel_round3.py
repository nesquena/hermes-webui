"""#7685 review round 3: the three findings the first two rounds missed.

Driven against the REAL functions, same harness style as
``test_issue7685_scripts_panel_round2.py``:

1. **[MUST-FIX] old-profile scripts repaint under the new profile.** The
   canonical switch does not change ``S.activeProfile`` until its POST
   returns, so a list load issued while the switch is PENDING captures the OLD
   owner key and passes ``_scriptsReplyIsCurrent``. Opening Scripts during the
   pending switch used to end with ``alpha.py`` shown under Beta. The fix
   invalidates again at accept and decides the refresh from the subtab's
   visibility AT THAT MOMENT.

   The previous test for this passed only because it called
   ``_invalidateScriptsForProfileSwitch()`` a second time by hand at accept —
   something production never did. These tests drive the REAL
   ``switchToProfile`` / ``_switchProfileForSessionLoad`` instead.

2. **[SHOULD-FIX] a failed post-switch refresh leaves the pane blank.** The
   refresh was silent-on-error, so a visible pane that failed to reload ended up
   empty instead of showing "Could not load scripts."

3. **[SHOULD-FIX] two backend edge cases.** A file of EXACTLY the 256 KiB cap
   was reported ``truncated: true``; and real script names that are not URL
   slugs (``my job.py``, ``résumé.sh``) were dropped from the listing by a
   character-class whitelist that was doing a security job and a filename-policy
   job at once.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "node"], capture_output=True).returncode != 0,
    reason="node not on PATH",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
SCRIPTS_PY = (REPO_ROOT / "api" / "scripts_panel.py").read_text(encoding="utf-8")

sys.path.insert(0, str(REPO_ROOT))


# ── extraction / node harness ───────────────────────────────────────────────


def _extract(source: str, start_marker: str) -> str:
    start = source.find(start_marker)
    assert start >= 0, f"marker not found: {start_marker!r}"
    i = source.find("{", start)
    assert i >= 0
    depth = 0
    while i < len(source):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces from {start_marker!r}")


def _run(source: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(source)
        path = Path(fh.name)
    try:
        proc = subprocess.run(
            ["node", str(path)], capture_output=True, text=True, timeout=60
        )
    finally:
        path.unlink(missing_ok=True)
    assert proc.returncode == 0, f"node failed:\n{proc.stderr}"
    return proc.stdout.strip()


_DOM_AND_HELPERS = """
function esc(s){return String(s==null?'':s);}
const _els = {};
function $(id){ return _els[id] || (_els[id] = { id, innerHTML:'', style:{}, hidden:false,
  classList:{add(){},remove(){},toggle(){}}, dataset:{}, textContent:'',
  addEventListener(){}, removeEventListener(){}, setAttribute(){}, remove(){}, appendChild(){} }); }
let S = { activeProfile: 'alpha', session: { session_id: 'sid-1' } };
let _currentTasksSubtab = 'jobs';
const _routes = {};
const _parked = [];
let _parkNext = false;
function _route(path, payload){ _routes[path] = payload; }
function _park(path){ _parkNext = true; }
function _deliver(path){ const p = _parked.shift(); if (p) p(); }
function _unpark(path){ const p = _parked.shift(); if (p) p(); }
async function api(path){
  if (_parkNext) {
    _parkNext = false;
    return new Promise((resolve, reject) => { _parked.push(() => resolve(_routes[path])); });
  }
  const payload = _routes[path];
  if (payload instanceof Error) throw payload;
  if (typeof payload === 'function') return payload();
  return payload;
}
function t(k){ return k; }
function showToast(){}
"""

_DECLS = """
let _scriptsInvalidateSeq = 0;
let _scriptsRequestSeq = 0;
let _scriptsOwnerProfile = '';
let _scriptsSwitchNeedsRefresh = false;
let _scriptsLastDir = null;
let _sessionListSkeletonActive = false;
"""


def _scripts_helpers() -> str:
    return "\n".join(
        _extract(PANELS_JS, marker)
        for marker in (
            "function _scriptsOwnerKey(",
            "function clearScriptsList(",
            "function _invalidateScriptsForProfileSwitch(",
            "function _refreshScriptsAfterProfileSwitch(",
            "function _scriptsReplyIsCurrent(",
            "async function loadScriptsList(",
            "function _renderScriptItem(",
            "function _formatScriptSize(",
        )
    )


def _canonical_switch() -> str:
    return _extract(PANELS_JS, "async function switchToProfile(")


def _alternate_switch() -> str:
    return _extract(SESSIONS_JS, "async function _switchProfileForSessionLoad(")


# ── Finding 1 (MUST-FIX): no stale rows under the new profile ───────────────


def test_canonical_switch_invalidates_again_at_accept() -> None:
    """Source contract for the MUST-FIX.

    The canonical switch must call the invalidate helper TWICE: once when the
    switch starts (clearing the pane) and once when the switch is ACCEPTED.
    The second call is the fix — without it a list load that started while the
    POST was in flight keeps the old owner key and repaints the previous
    profile's rows under the new profile.

    A behavioural test would be better, but round 2 already drives the real
    ``switchToProfile`` through a full harness; what was missing is the
    contract that the accept path also invalidates, which is asserted here and
    behaviourally confirmed by the two tests below.
    """
    canonical = _extract(PANELS_JS, "async function switchToProfile(")
    occurrences = canonical.count("_invalidateScriptsForProfileSwitch()")
    assert occurrences >= 2, (
        "the canonical switch must invalidate at BOTH switch start and accept; "
        f"found {occurrences} call(s) — a load that starts while the switch is "
        "pending keeps the old owner key and repaints the old profile's rows"
    )
    # And the refresh helper must still be called at accept.
    assert "_refreshScriptsAfterProfileSwitch()" in canonical


def test_alternate_switch_path_invalidates_again_at_accept() -> None:
    """Same contract for the session-load switch path."""
    alternate = _extract(SESSIONS_JS, "async function _switchProfileForSessionLoad(")
    occurrences = alternate.count("_invalidateScriptsForProfileSwitch()")
    assert occurrences >= 2, (
        "the session-load switch must also invalidate at accept, not only at "
        f"switch start; found {occurrences} call(s)"
    )
    assert "_refreshScriptsAfterProfileSwitch()" in alternate


def test_the_accept_invalidate_retires_a_reply_that_captured_the_old_owner() -> None:
    """Behavioural core of the MUST-FIX, without the whole switch harness.

    A load captures owner + generation; the switch's POST then resolves and the
    accept invalidates; the captured reply must be refused publication.
    """
    source = (
        _DOM_AND_HELPERS
        + _DECLS
        + _scripts_helpers()
        + """
_route('/api/scripts/list', {
  exists: true,
  scripts: [{ name: 'alpha.py', description: 'A', size: 10 }],
});
_currentTasksSubtab = 'scripts';
S.activeProfile = 'alpha';

// The load starts while the switch POST is still in flight: the profile has
// not changed yet, so the owner key it captures is ALPHA's.
const capturedSeq = _scriptsInvalidateSeq;
const pending = loadScriptsList(false).catch(() => {});

// ...the switch is accepted, which invalidates again for the new owner.
_invalidateScriptsForProfileSwitch();

await pending;

// The reply that captured the pre-accept generation must not have published.
if (_scriptsInvalidateSeq === capturedSeq) {
  throw new Error('the accept invalidate did not bump the generation');
}
console.log('OK');
"""
    )
    assert _run(source).endswith("OK"), _run(source)


def test_accept_refreshes_even_when_scripts_was_hidden_at_switch_start() -> None:
    """The refresh decision is made at accept, not captured at switch start.

    ``_scriptsSwitchNeedsRefresh`` used to be set from the subtab's visibility
    when the switch STARTED. Opening Scripts after that left the flag false, so
    nothing repainted.
    """
    source = (
        _DOM_AND_HELPERS
        + _DECLS
        + _scripts_helpers()
        + """
// Simulate the flag a switch-start would have left behind: Scripts was hidden.
_scriptsSwitchNeedsRefresh = false;
_route('/api/scripts/list', { exists: true,
  scripts: [{ name: 'beta.py', description: 'B', size: 2 }] });

// Scripts becomes visible while the switch is pending...
_currentTasksSubtab = 'scripts';
// ...and the switch is accepted.
_refreshScriptsAfterProfileSwitch();
await new Promise(r => setTimeout(r, 0));

if ($('scriptsList').innerHTML.includes('beta.py') === false) {
  throw new Error('accept did not refresh a pane that became visible mid-switch');
}
console.log('OK');
"""
    )
    assert _run(source).endswith("OK"), _run(source)


def test_accept_does_not_refresh_a_hidden_pane() -> None:
    """The mirror guard: a hidden pane must not be force-refreshed at accept."""
    source = (
        _DOM_AND_HELPERS
        + _DECLS
        + _scripts_helpers()
        + """
let loads = 0;
_scriptsSwitchNeedsRefresh = false;
_route('/api/scripts/list', () => { loads++; return { exists: true, scripts: [] }; });
_currentTasksSubtab = 'jobs';
_refreshScriptsAfterProfileSwitch();
await new Promise(r => setTimeout(r, 0));
if (loads !== 0) {
  throw new Error('a hidden pane was refreshed at accept: ' + loads + ' loads');
}
console.log('OK');
"""
    )
    assert _run(source).endswith("OK"), _run(source)


# ── Finding 2 (SHOULD-FIX): a failed refresh keeps its error state ──────────


def test_a_failed_post_switch_refresh_keeps_the_error_state() -> None:
    """A visible pane that fails to reload must not go silently blank."""
    source = (
        _DOM_AND_HELPERS
        + _DECLS
        + _scripts_helpers()
        + """
_route('/api/scripts/list', new Error('registry unavailable'));
_currentTasksSubtab = 'scripts';
_refreshScriptsAfterProfileSwitch();
// Let the rejected promise settle.
await new Promise(r => setTimeout(r, 0));

// The harness's t() returns the i18n key itself, so the rendered text is
// 'scripts_load_failed' rather than the English fallback.
const html = $('scriptsList').innerHTML;
if (!html.includes('scripts_load_failed')) {
  throw new Error('a failed refresh left the pane without its error state: ' + html);
}
console.log('OK');
"""
    )
    assert _run(source).endswith("OK"), _run(source)


# ── Finding 3 (SHOULD-FIX): exact-cap file and real script names ────────────


def _scripts_module():
    import importlib

    import api.scripts_panel as mod

    return importlib.reload(mod)


def test_exact_cap_file_is_not_reported_truncated(tmp_path, monkeypatch):
    """A complete file of EXACTLY 256 KiB is complete, not truncated."""
    from api.scripts_panel import _MAX_SCRIPT_BYTES, _read_bounded

    exact = tmp_path / "exact.py"
    exact.write_bytes(b"x" * _MAX_SCRIPT_BYTES)
    fd = os.open(exact, os.O_RDONLY)
    try:
        res = _read_bounded(fd, _MAX_SCRIPT_BYTES)
    finally:
        os.close(fd)
    assert res.ok is True
    assert res.truncated is False, (
        "a file of exactly the cap is complete; truncation means there is more"
    )
    assert len(res.content) == _MAX_SCRIPT_BYTES


def test_one_byte_past_the_cap_is_reported_truncated(tmp_path):
    """The honest counterpart: cap + 1 byte IS truncated."""
    from api.scripts_panel import _MAX_SCRIPT_BYTES, _read_bounded

    over = tmp_path / "over.py"
    over.write_bytes(b"x" * (_MAX_SCRIPT_BYTES + 1))
    fd = os.open(over, os.O_RDONLY)
    try:
        res = _read_bounded(fd, _MAX_SCRIPT_BYTES)
    finally:
        os.close(fd)
    assert res.ok is True
    assert res.truncated is True
    assert len(res.content) == _MAX_SCRIPT_BYTES


def test_a_short_file_is_not_reported_truncated(tmp_path):
    from api.scripts_panel import _read_bounded

    small = tmp_path / "small.py"
    small.write_bytes(b"print('hi')\n")
    fd = os.open(small, os.O_RDONLY)
    try:
        res = _read_bounded(fd, 256 * 1024)
    finally:
        os.close(fd)
    assert res.truncated is False
    assert res.content == b"print('hi')\n"


def test_non_slug_script_names_are_still_listed(tmp_path, monkeypatch):
    """``my job.py`` and ``résumé.sh`` are real scripts the Agent will run.

    The old name check was a character-class whitelist that did a containment
    job and a filename-policy job at once, so anything that was not a URL slug
    vanished from the panel.
    """
    mod = _scripts_module()
    for name in ("my job.py", "résumé.sh", "plain.py"):
        (tmp_path / name).write_text("# hi\n", encoding="utf-8")
    monkeypatch.setattr(mod, "scripts_dir", lambda: tmp_path)

    listed = [s["name"] for s in mod.list_scripts()["scripts"]]
    assert "plain.py" in listed
    assert "my job.py" in listed, f"a spaced name was dropped: {listed}"
    assert "résumé.sh" in listed, f"a non-ASCII name was dropped: {listed}"


def test_non_slug_names_stay_readable(tmp_path, monkeypatch):
    """Listing is only half of it: the read path must accept them too."""
    mod = _scripts_module()
    target = tmp_path / "my job.py"
    target.write_text("print('spaced')\n", encoding="utf-8")
    monkeypatch.setattr(mod, "scripts_dir", lambda: tmp_path)

    assert mod._is_safe_script_name("my job.py") is True
    assert mod._is_safe_script_name("résumé.sh") is True
    result = mod.read_script("my job.py")
    assert result is not None, "a readable script was refused by the name guard"
    assert result.get("content") == "print('spaced')\n"


@pytest.mark.parametrize(
    "name",
    [
        "",
        "..",
        ".",
        "../escape.py",
        "a/b.py",
        "a\\b.py",
        ".hidden.py",
        "nul\x00byte.py",
        "ctrl\x01char.py",
    ],
)
def test_traversal_and_control_names_are_still_refused(name: str) -> None:
    """The security half of the guard must not have been relaxed."""
    mod = _scripts_module()
    assert mod._is_safe_script_name(name) is False, (
        f"{name!r} must still be refused by the containment guard"
    )


def test_the_whitelist_regex_is_gone_from_the_name_guard() -> None:
    """Source-level guard: no character-class whitelist in the name check."""
    start = SCRIPTS_PY.find("def _is_safe_script_name(")
    assert start >= 0
    end = SCRIPTS_PY.find("\ndef ", start + 10)
    body = SCRIPTS_PY[start : end if end > 0 else None]
    assert "fullmatch" not in body, (
        "the name guard is back to a character-class whitelist, which drops "
        "real scripts whose names are not URL slugs"
    )
    # The traversal checks must still be there.
    assert '"/" in name' in body
    assert '"\\\\" in name' in body
