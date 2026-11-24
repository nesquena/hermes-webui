"""#9338: a focus-triggered reload of the SAME session must not close the preview.

The window `focus` / `visibilitychange` hooks call
``refreshActiveSessionIfExternallyUpdated()``; when the server message count
moved (a Telegram turn, a sibling tab, a background turn) it force-reloads the
active session via ``loadSession(sid, {force:true, ...})``. That reload then
refreshes the workspace tree through ``_deferWorkspaceRefreshForSession(sid)``
with no options, and ``loadDir('.')`` takes the navigation branch: it closes the
open preview (or pops the "Discard edits?" dialog over a dirty editor).

The flag that already implements the right semantics is ``preservePreview``
(used by the stream-done path in messages.js). The defect is the CALLSITE: the
same-session reload must request it, a real session switch must not (#8514).
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
WORKSPACE_JS = (REPO / "static" / "workspace.js").read_text(encoding="utf-8")

NODE_SKIP = pytest.mark.skipif(
    subprocess.run(["sh", "-c", "command -v node"], capture_output=True).returncode != 0,
    reason="node not on PATH",
)


def _function_block(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.find(marker)
    assert start != -1, f"{name}() not found"
    params_end = src.find("){", start)
    assert params_end != -1, f"{name}() body not found"
    brace = params_end + 1
    depth = 0
    for idx in range(brace, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"{name}() body did not close")


def _run_node(script: str):
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def _load_session_block() -> str:
    return _function_block(SESSIONS_JS, "loadSession")


def _workspace_refresh_calls(block: str):
    """Every ``_deferWorkspaceRefreshForSession(...)`` call inside loadSession."""
    return re.findall(r"_deferWorkspaceRefreshForSession\([^;]*?\);", block)


def _helper_block() -> str:
    return _function_block(SESSIONS_JS, "_sessionLoadWorkspaceRefreshOpts")


def test_focus_triggered_refresh_force_reloads_the_active_session():
    """The scenario under test still exists: focus -> force reload of the same sid."""
    assert "addEventListener('focus'" in SESSIONS_JS
    assert re.search(
        r"addEventListener\('focus',\s*\(\)\s*=>\s*\{\s*void refreshActiveSessionIfExternallyUpdated\('focus'\)",
        SESSIONS_JS,
    ), "focus handler must still funnel through refreshActiveSessionIfExternallyUpdated()"
    refresh = _function_block(SESSIONS_JS, "refreshActiveSessionIfExternallyUpdated")
    flat = refresh.replace(" ", "")
    assert "remoteCount!==localCount" in flat
    assert "awaitloadSession(sid,{force:true" in flat, (
        "an external message-count delta must still force-reload the same session"
    )


def test_load_session_workspace_refresh_preserves_preview_for_same_session_reload():
    """Same-session reload: every deferred workspace refresh must ask to keep the preview."""
    block = _load_session_block()
    calls = _workspace_refresh_calls(block)
    assert len(calls) >= 3, f"expected the three loadSession workspace refreshes, got {calls}"
    for call in calls:
        assert "preservePreview" in call or "_workspaceRefreshOpts" in call, (
            "loadSession() must pass the same-session preview decision to every "
            f"_deferWorkspaceRefreshForSession(sid, ...) call; got {call!r}"
        )

    flat = block.replace(" ", "")
    decision = "_workspaceRefreshOpts=_sessionLoadWorkspaceRefreshOpts(forceReload,currentSid,sid);"
    assert decision in flat, (
        "loadSession() must compute the workspace-refresh options ONCE from the "
        "entry-captured currentSid, like sameSessionForceReload does"
    )
    # Captured before the session is swapped in, so a reload of the same sid is
    # not mistaken for a navigation.
    decision_idx = flat.index(decision)
    first_swap = re.search(r"S\.session=", flat)
    assert first_swap is not None, "loadSession() must still install the loaded session"
    assert decision_idx < first_swap.start(), (
        "the same-session decision must be taken before S.session is replaced"
    )
    assert "sameSessionForceReload=forceReload&&currentSid===sid;" in flat, (
        "the preview decision must use the same criterion as sameSessionForceReload"
    )


@NODE_SKIP
def test_workspace_refresh_opts_helper_preserves_only_same_session_reloads():
    """The pure decision: same-session force reload keeps the preview, switches do not."""
    assert "preservePreview:true" in _helper_block().replace(" ", ""), (
        "the same-session decision must request {preservePreview:true}"
    )
    script = _helper_block() + r"""
const cases = {
  same_session:  _sessionLoadWorkspaceRefreshOpts(true, 'sess-a', 'sess-a'),
  switched:      _sessionLoadWorkspaceRefreshOpts(true, 'sess-a', 'sess-b'),
  click_no_force:_sessionLoadWorkspaceRefreshOpts(false, 'sess-a', 'sess-a'),
  no_open:       _sessionLoadWorkspaceRefreshOpts(true, null, 'sess-a'),
  missing_force: _sessionLoadWorkspaceRefreshOpts(undefined, 'sess-a', 'sess-a'),
};
const out = {};
for (const [k, v] of Object.entries(cases)) out[k] = !!(v && v.preservePreview);
console.log(JSON.stringify(out));
"""
    out = _run_node(script)
    assert out["same_session"] is True, (
        "a forced reload of the session already on screen must keep the open preview"
    )
    assert out["switched"] is False, "a real session switch must still clear the preview (#8514)"
    assert out["click_no_force"] is False, "a plain re-select is not a same-session reload"
    assert out["no_open"] is False
    assert out["missing_force"] is False


def test_real_session_switch_still_clears_preview():
    """#8514 protection unchanged: loadDir() without preservePreview clears."""
    block = _function_block(WORKSPACE_JS, "loadDir")
    flat = block.replace(" ", "")
    assert "preservePreview" in block
    assert "if(!preservePreview&&typeofclearPreview==='function')" in flat
    assert "clearPreview({keepPanelOpen:true})" in flat
    assert "awaitrefreshOpenPreviewIfMutated()" in flat


@NODE_SKIP
def test_switch_opts_leave_load_dir_on_its_clearing_branch():
    """End-to-end of the decision + loadDir's guard, with no DOM: switch => clears."""
    script = _helper_block() + r"""
function loadDirGuard(opts){
  const preservePreview = !!(opts && opts.preservePreview);
  // Mirrors static/workspace.js loadDir(): the clearing branch is skipped only
  // when preservePreview is set, otherwise clearPreview() runs (or the
  // "Discard edits?" dialog when the editor is dirty).
  return preservePreview ? 'refresh' : 'clear';
}
const same = loadDirGuard(_sessionLoadWorkspaceRefreshOpts(true, 's1', 's1'));
const other = loadDirGuard(_sessionLoadWorkspaceRefreshOpts(true, 's1', 's2'));
console.log(JSON.stringify({same, other}));
"""
    out = _run_node(script)
    assert out["same"] == "refresh", "focus reload of the same session must refresh, not clear"
    assert out["other"] == "clear", "switching sessions must keep the #8514 clearing behaviour"
