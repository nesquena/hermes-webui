"""Regression: blank assistant turn (对话消失) — dead empty live-turn shell
survives a session-updated swap re-render and hides the settled answer.

Root cause (reproduced + fixed on an isolated debug instance, 2026-07-01)
------------------------------------------------------------------------
`renderMessages()` (static/ui.js) preserves the `#liveAssistantTurn` DOM node
across the `inner.innerHTML=''` wipe so the smd parser's live reference is not
detached mid-stream (#3877 flicker fix). The preserve guard originally fired
whenever `INFLIGHT[sid]` existed:

    let _preservedLiveTurn=null;
    if(sid&&INFLIGHT[sid]){
      const _lt=document.getElementById('liveAssistantTurn');
      if(_lt&&(...sessionId matches...)){ _preservedLiveTurn=_lt; }
    }

When a turn's SSE dropped (S.activeStreamId cleared to null) but its
`INFLIGHT[sid]` entry was NOT cleaned, the live turn was a DEAD empty shell —
avatar + an empty worklog group ("Processed Ns", no body/tool rows). On the
next `session-updated` self-heal swap (loadSession force + keepStaleUntilLoaded,
common under repeated self-wake restarts), the guard re-attached that empty
shell OVER the freshly-wiped transcript, pinning an avatar-only blank turn on
top of the already-persisted answer. That is the reported "对话消失".

Fix
---
Preserve the live turn ONLY when it is genuinely live: an active stream is
still running (`S.activeStreamId`) — the #3877 case — OR it already holds real
rendered content (`.msg-body`, `.tool-card-row`, or `.wl-reason`). A dead empty
shell (no content, no active stream) is no longer preserved, so the swap wipe
drops it and the settled transcript renders normally.
"""
import pathlib
import re
import shutil
import subprocess
import textwrap

REPO = pathlib.Path(__file__).parent.parent


def read(rel):
    return (REPO / rel).read_text(encoding="utf-8")


def _preserve_guard_src():
    src = read("static/ui.js")
    i = src.find("let _preservedLiveTurn=null;")
    assert i >= 0, "_preservedLiveTurn guard not found"
    # capture through the closing of the if-block (next 'const compressionState')
    j = src.find("const compressionState", i)
    assert j > i, "guard block end not found"
    return src[i:j]


class TestBlankLiveTurnPreserveGuard:
    def test_guard_requires_real_content_or_active_stream(self):
        guard = _preserve_guard_src()
        # Must gate the preserve on real content OR an active stream — not merely
        # on INFLIGHT existence.
        assert "_hasRealLiveContent" in guard, (
            "preserve guard must compute whether the live turn has real content"
        )
        assert ".msg-body" in guard and ".tool-card-row" in guard and ".wl-reason" in guard, (
            "real-content check must look for a visible body / tool card / reason row"
        )
        assert "S.activeStreamId" in guard, (
            "preserve guard must still preserve a genuinely-streaming turn (#3877)"
        )
        # The assignment must be inside the new conditional.
        assert re.search(
            r"if\(_hasRealLiveContent\s*\|\|\s*S\.activeStreamId\)\{\s*_preservedLiveTurn=_lt;",
            guard,
        ), "preserve assignment must be gated by (hasRealContent || activeStreamId)"

    def test_runtime_rejects_dead_shell_preserves_live(self):
        node = shutil.which("node")
        if not node:
            import pytest
            pytest.skip("node not available")
        script = textwrap.dedent(
            """
            const assert=require('assert');
            // Mirror the guard's decision predicate exactly.
            function guardWouldPreserve(lt, activeStreamId){
              if(!lt) return false;
              const hasReal=!!lt.querySelector('.msg-body, .tool-card-row, .wl-reason');
              return hasReal || !!activeStreamId;
            }
            // Minimal DOM element stub with querySelector over a class set.
            function el(classes){
              const set=new Set(classes||[]);
              return { querySelector(sel){
                // sel is a comma list of .class tokens
                return sel.split(',').map(s=>s.trim().replace(/^\\./,''))
                  .some(c=>set.has(c)) ? {} : null;
              }};
            }
            const deadShell = el([]);                 // empty worklog shell, no content
            const withBody  = el(['msg-body']);
            const withTool  = el(['tool-card-row']);
            const withReason= el(['wl-reason']);
            assert.strictEqual(guardWouldPreserve(deadShell, null), false, 'dead shell must NOT be preserved');
            assert.strictEqual(guardWouldPreserve(deadShell, 'sid'), true, 'streaming empty shell preserved (#3877)');
            assert.strictEqual(guardWouldPreserve(withBody, null), true, 'body content preserved');
            assert.strictEqual(guardWouldPreserve(withTool, null), true, 'tool card preserved');
            assert.strictEqual(guardWouldPreserve(withReason, null), true, 'reason row preserved');
            console.log('OK');
            """
        )
        out = subprocess.run([node, "-e", script], capture_output=True, text=True)
        assert out.returncode == 0, f"node harness failed: {out.stderr}\n{out.stdout}"
        assert "OK" in out.stdout
