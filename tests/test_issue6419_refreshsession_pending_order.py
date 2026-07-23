"""Issue #6419: mid-stream reconnect must not render the just-sent user message
below the live streaming response.

Symptom (iOS webview/PWA): when the chat SSE drops mid-turn (backgrounding /
screen lock suspends the webview) and the client goes through its soft-recovery
path, the transcript can re-render with the initiating user bubble ORDERED
BELOW the live assistant worklog it started. It corrects itself once the turn
settles (the persisted session JSON is correct), so the flip is display-only
during the active turn.

Root cause: the server keeps the current turn's prompt in
``session.pending_user_message`` and omits both it and the live assistant from
``session.messages`` until the turn settles (deferred merge). ``loadSession``
handles this by rebuilding the live tail from INFLIGHT (injecting a
``{role:'assistant', _live:true}`` row) and then splicing the pending user row
BEFORE it via ``_mergePendingSessionMessage``. ``refreshSession`` in ui.js --
the re-render path used by ``_recoverFromOfflineSoftly`` on iOS background /
resume -- did neither: it reset ``S.messages`` to the settled server turns and
``push()``-ed the pending user row at the END, with no ``_live`` row to anchor
against. So the pending user ended up after the reconstructed live worklog.

Fix: route ``refreshSession`` through the same live-tail reconstruction +
``_mergePendingSessionMessage`` placement that ``loadSession`` uses, and promote
``_mergePendingSessionMessage`` to module scope so both recovery entrypoints
share the one placement chokepoint.

State layer: this changes the *client* Visible-transcript / Pending-turn
projection during recovery re-render only (run-state-consistency invariants 2
"active turn UI keeps its owner" and 3 "reattach preserves order"). It does not
touch server stream buffering, session persistence, or the settle/merge path --
those were already verified correct for the same turn in the report.
"""
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _fn_body(src: str, marker: str) -> str:
    """Return the balanced-brace body of the function opened at ``marker``."""
    idx = src.find(marker)
    assert idx != -1, f"{marker!r} not found"
    brace = src.find("{", idx)
    depth = 1
    i = brace + 1
    while i < len(src) and depth:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{marker!r} body did not close"
    return src[brace : i]


def _extract_fn(src: str, marker: str) -> str:
    """Return the full ``function name(...) { ... }`` declaration text."""
    idx = src.find(marker)
    assert idx != -1, f"{marker!r} not found"
    return marker + _fn_body(src, marker)


def _find_node():
    import shutil
    for cand in ("node", "node.exe"):
        path = shutil.which(cand)
        if path:
            try:
                r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
                if r.returncode == 0 and r.stdout.strip().startswith("v"):
                    return path
            except Exception:
                continue
    pytest.skip("node.js not found -- skipping node-executed behavioral test")


# ---------------------------------------------------------------------------
# Behavioral test: execute the REAL placement/merge functions from sessions.js
# through the exact sequence refreshSession now runs, and assert the resulting
# array orders the pending user row immediately BEFORE the live assistant turn.
# ---------------------------------------------------------------------------
def test_refresh_recovery_sequence_orders_user_before_live_assistant():
    node = _find_node()

    ensure_live = _extract_fn(SESSIONS_JS, "function _ensureInflightLiveAssistantMessage(inflight)")
    merge_tail = _extract_fn(SESSIONS_JS, "function _mergeInflightTailMessages(baseMessages, inflightMessages)")
    merge_pending = _extract_fn(SESSIONS_JS, "function _mergePendingSessionMessage(session,messages)")

    script = textwrap.dedent(
        """
        // --- leaf-helper stubs (peripheral to the ordering logic under test) ---
        function _messageComparableText(m){ return (m && typeof m.content === 'string') ? m.content.trim() : ''; }
        function _sameTranscriptMessage(a, b){
            return !!(a && b && a.role === b.role && String(a.content||'') === String(b.content||''));
        }
        // No duplicates in this fixture; keep the guard honest so a real dup
        // (same current-turn user text) would still be suppressed.
        function _hasCurrentTailUserDuplicate(messages, candidate){
            if(!candidate || String(candidate.role||'') !== 'user') return false;
            for(let i=messages.length-1;i>=0;i--){
                const m=messages[i];
                if(m && m.role==='user') return _sameTranscriptMessage(m, candidate);
                if(m && m.role==='assistant' && m._live) return false;
            }
            return false;
        }
        function getPendingSessionMessage(session, messages){
            const text = String(session && session.pending_user_message || '').trim();
            if(!text) return null;
            return { role:'user', content:text, _pending:true };
        }
        const Date = { now: () => 1000 };  // _ensureInflightLiveAssistantMessage stamps _ts

        __REAL_FUNCS__

        // --- fixture mirrors the reported turn shape --------------------------
        // A settled prior turn, an active stream whose live assistant text lives
        // ONLY in INFLIGHT, and the initiating prompt held as pending_user_message.
        const base = [
            { role:'user', content:'previous question' },
            { role:'assistant', content:'previous settled answer' },
        ];
        const inflight = { lastAssistantText:'streaming answer in progress', lastReasoningText:'', messages: [] };
        const session = { pending_user_message:'NEW PROMPT that started this turn' };

        // --- exact refreshSession reconstruction sequence ---------------------
        _ensureInflightLiveAssistantMessage(inflight);
        let S_messages = base.slice();
        S_messages = _mergeInflightTailMessages(S_messages, inflight.messages);
        _mergePendingSessionMessage(session, S_messages);

        const liveIdx = S_messages.findIndex(m => m && m.role==='assistant' && m._live);
        const userIdx = S_messages.findIndex(m => m && m._pending);

        let ok = true;
        function check(cond, msg){ if(!cond){ console.error('FAIL: '+msg); ok=false; } }
        check(liveIdx !== -1, 'live assistant row was not reconstructed into S.messages');
        check(userIdx !== -1, 'pending user row was not inserted');
        check(userIdx < liveIdx, 'pending user row must be ABOVE the live assistant (userIdx='+userIdx+', liveIdx='+liveIdx+')');
        check(userIdx === liveIdx - 1, 'pending user row must be immediately before the live assistant');
        // The prior settled turn stays first; the new turn is appended after it.
        check(S_messages[0] && S_messages[0].content === 'previous question', 'settled history was disturbed');
        // No duplicate user rows introduced.
        check(S_messages.filter(m => m && m.role==='user' && m.content==='NEW PROMPT that started this turn').length === 1,
              'pending user row was duplicated');

        console.log('order: ' + S_messages.map(m => m.role + (m._live?'*':'') + (m._pending?'^':'')).join(','));
        process.exit(ok ? 0 : 1);
        """
    ).replace("__REAL_FUNCS__", "\n".join([ensure_live, merge_tail, merge_pending]))

    result = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, (
        "refreshSession recovery ordering is wrong:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # Sanity: the reconstructed order is settled-user, settled-assistant,
    # pending-user(^), live-assistant(*).
    assert "user,assistant,user^,assistant*" in result.stdout, result.stdout


# ---------------------------------------------------------------------------
# Source-structure tests: pin the refreshSession wiring (the actual change).
# These fail against the pre-fix bare-push refreshSession and pass after.
# ---------------------------------------------------------------------------
def test_refresh_session_rebuilds_live_tail_and_splices_pending_before_render():
    body = _fn_body(UI_JS, "async function refreshSession()")

    # The old path had none of these; it reset S.messages and push()-ed pending.
    merge_tail_pos = body.find("_mergeInflightTailMessages(S.messages")
    merge_pending_pos = body.find("_mergePendingSessionMessage(S.session")
    ensure_live_pos = body.find("_ensureInflightLiveAssistantMessage(INFLIGHT[")
    render_pos = body.find("_renderMessagesWithScrollSnapshot(")

    assert ensure_live_pos != -1, (
        "refreshSession must rebuild the live assistant row from INFLIGHT so the "
        "pending user row has a _live anchor to splice before (#6419)"
    )
    assert merge_tail_pos != -1, (
        "refreshSession must merge the INFLIGHT live tail into S.messages, "
        "mirroring loadSession's reattach"
    )
    assert merge_pending_pos != -1, (
        "refreshSession must route the pending user row through "
        "_mergePendingSessionMessage (splice-before-live), not push()-at-end"
    )
    assert render_pos != -1, "refreshSession render call not found"
    assert ensure_live_pos < merge_tail_pos < merge_pending_pos < render_pos, (
        "Order must be: ensure live row -> merge live tail -> splice pending -> "
        "render, so the reconstructed transcript is correct before painting"
    )
    # The gate must be conditioned on an active stream + an INFLIGHT recovery
    # cache; an idle refresh degrades to the plain placement helper.
    assert "S.activeStreamId&&_sid&&INFLIGHT[_sid]" in body.replace(" ", "").replace("\n", "")


def test_refresh_session_no_unconditional_push_at_end():
    body = _fn_body(UI_JS, "async function refreshSession()")
    # The buggy line was: `if(pendingMsg) S.messages.push(pendingMsg);` used
    # unconditionally. It may only survive as the defensive else-fallback for
    # when _mergePendingSessionMessage is somehow unavailable.
    if "S.messages.push(pendingMsg)" in body:
        assert "else{" in body.replace(" ", "").replace("\n", ""), (
            "A bare push(pendingMsg) may only remain inside the else fallback"
        )
        # It must not be the primary path: the merge helper is reached first.
        assert body.find("_mergePendingSessionMessage") < body.find("S.messages.push(pendingMsg)")


def test_merge_pending_helper_is_module_scoped_and_shared():
    # Promoted out of loadSession so refreshSession (ui.js) can call it too.
    assert "\nfunction _mergePendingSessionMessage(session,messages){" in SESSIONS_JS, (
        "_mergePendingSessionMessage must be a module-scope function so both "
        "loadSession and refreshSession share the one placement chokepoint"
    )
    # The splice-before-live primitive is intact.
    assert "messages.findIndex(m=>m&&m.role==='assistant'&&m._live)" in SESSIONS_JS
    assert "messages.splice(liveAssistantIdx,0,pendingMsg)" in SESSIONS_JS
    # loadSession still calls it inside its reattach path (no regression to #2341).
    assert SESSIONS_JS.count("_mergePendingSessionMessage(S.session,S.messages)") >= 2
