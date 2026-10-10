"""Settled reply must stay visible in Transparent Stream (#7676).

The live-turn preserve path (`renderMessages`, static/ui.js) and the STREAM_DONE
settle path (static/messages.js) both touch the reply at the exact moment it goes
from "streaming" to "settled". In Transparent Stream the transferable live node
carries the final answer HIDDEN (`assistant-segment-worklog-source` + aria-hidden
+ hidden — the rows rendered the activity instead), so any path that (a) re-pins
that node over the settled projection, (b) leaves the invariant sweep running
before the swap instead of after it, or (c) rebuilds the whole transcript a
second time to reach the final state, can paint the settled reply blank.

Contract encoded here (one per maintainer recommendation on #7676):

1. TERMINAL SETTLEMENT OWNS THE TURN — the preserve guard additionally requires
   an unsettled pane (`S.busy===false && !S.activeStreamId` ⇒ ineligible); a
   reconnecting pane (busy, no active stream) still preserves (#3877).
2. NORMALIZE THE TRANSFERRED PROSE — `_normalizeTransferredLiveProse` un-hides
   the retained final prose unless another visible scene row owns it.
3. INVARIANT AFTER THE SWAP — the zero-visible-content sweep runs after every
   preservation/swap decision and covers a SETTLED live turn (an active stream
   still owns its node).
4. TRANSPARENT SETTLE FINALIZES IN PLACE — `_finalizeJustSettledTransparentScene`
   re-renders only the settled turn instead of the second full innerHTML rebuild.

Every test extracts the REAL source; nothing here mirrors the implementation.
"""
import pathlib
import re
import shutil
import subprocess
import sys
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from js_source_extract import extract_function  # noqa: E402

REPO = pathlib.Path(__file__).parent.parent


def read(rel):
    return (REPO / rel).read_text(encoding="utf-8")


def _preserve_guard_src():
    """The real `let _preservedLiveTurn=null;` … decision block from ui.js."""
    src = read("static/ui.js")
    i = src.find("let _preservedLiveTurn=null;")
    assert i >= 0, "_preservedLiveTurn guard not found"
    j = src.find("const compressionState", i)
    assert j > i, "guard block end not found"
    return src[i:j]


def _run_node(script):
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not available")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, f"node harness failed: {out.stderr}\n{out.stdout}"
    return out.stdout


class TestTerminalSettlementOwnership:
    """Recommendation 1: a settled pane never re-pins the parser-owned node."""

    def test_guard_gates_terminal_settlement(self):
        guard = _preserve_guard_src()
        assert "_paneSettled" in guard, (
            "the preserve guard must recognise a terminally settled pane (#7676)"
        )
        assert re.search(
            r"_paneSettled\s*=\s*S\.busy===false\s*&&\s*!S\.activeStreamId", guard
        ), "settled == idle pane (S.busy false) with no live stream ownership"
        assert re.search(
            r"if\(S\.activeStreamId\s*\|\|\s*\(_hasLiveAssistantProjection\s*&&"
            r"\s*!_paneSettled\)\)\{\s*_preservedLiveTurn=_lt;",
            guard,
        ), (
            "preserve assignment must be gated by (activeStreamId || "
            "(live projection && !settled)) — terminal settlement owns the turn"
        )

    def test_guard_runtime_settled_vs_reconnect(self):
        """Execute the REAL extracted guard, not a mirror of it."""
        guard = _preserve_guard_src()
        script = textwrap.dedent(
            f"""
            const assert=require('assert');
            function decide({{busy, activeStreamId, messages, liveNode}}){{
              const sid='s1';
              const S={{session:{{session_id:sid}}, messages, activeStreamId, busy}};
              const INFLIGHT={{s1:{{streamId:'st1'}}}};
              const document={{getElementById:()=>liveNode}};
              {guard}
              return !!_preservedLiveTurn;
            }}
            const node={{dataset:{{sessionId:'s1'}}, querySelector:()=>({{}})}};
            const liveProjection=[{{role:'assistant',content:'final',_live:true}}];
            const settledProjection=[{{role:'assistant',content:'final'}}];
            // SETTLED pane (idle, no live stream) + live markers still on the
            // projection: the node must NOT be re-pinned (#7676). Before the fix
            // this preserved the pre-settle node over the settled reply.
            assert.strictEqual(
              decide({{busy:false, activeStreamId:null, messages:liveProjection, liveNode:node}}),
              false, 'a settled pane must never preserve the parser-owned live node (#7676)'
            );
            // Same settled pane, no live markers at all (#6948 duplicate guard).
            assert.strictEqual(
              decide({{busy:false, activeStreamId:null, messages:settledProjection, liveNode:node}}),
              false, 'settled + no live projection must not preserve'
            );
            // RECONNECT: the pane is still working (S.busy) but the transport
            // dropped (no active stream) — preservation must still happen (#3877).
            assert.strictEqual(
              decide({{busy:true, activeStreamId:null, messages:liveProjection, liveNode:node}}),
              true, 'a reconnecting pane must still preserve its live node (#3877)'
            );
            // Active stream: always preserved.
            assert.strictEqual(
              decide({{busy:true, activeStreamId:'st1', messages:settledProjection, liveNode:node}}),
              true, 'an active stream always preserves its own node'
            );
            // No live node → nothing to preserve.
            assert.strictEqual(
              decide({{busy:true, activeStreamId:'st1', messages:liveProjection, liveNode:null}}),
              false, 'no live DOM node means nothing to preserve'
            );
            console.log('OK');
            """
        )
        assert "OK" in _run_node(script)


class TestTransferredLiveProseNormalization:
    """Recommendation 2: the retained final prose must not stay hidden."""

    def _helper_src(self):
        return extract_function(read("static/ui.js"), "_normalizeTransferredLiveProse")

    @staticmethod
    def _harness(cases_json):
        helper = extract_function(read(str(REPO / "static/ui.js")), "_normalizeTransferredLiveProse")
        return textwrap.dedent(
            f"""
            const assert=require('assert');
            {helper}
            const cases={cases_json};
            let S=null;
            function mkNode(spec){{
              const classes=new Set(spec.classes||[]);
              const attrs=Object.assign({{}}, spec.attrs||{{}});
              return {{
                _text: spec.text||'',
                hidden: !!spec.hidden,
                style:{{display: spec.display||''}},
                get textContent(){{ return this._text; }},
                classList:{{
                  add:(c)=>classes.add(c),
                  remove:(c)=>classes.delete(c),
                  contains:(c)=>classes.has(c),
                }},
                getAttribute:(n)=>(n in attrs ? attrs[n] : null),
                setAttribute:(n,v)=>{{ attrs[n]=String(v); }},
                removeAttribute:(n)=>{{ delete attrs[n]; }},
              }};
            }}
            for(const c of cases){{
              const segs=(c.segs||[]).map(mkNode);
              const rows=(c.rows||[]).map(mkNode);
              const turn={{
                querySelectorAll(sel){{
                  if(sel.indexOf('assistant-segment-worklog-source')!==-1) return segs;
                  if(sel.indexOf('.assistant-segment')!==-1) return segs;
                  return rows;
                }},
              }};
              S=c.streaming ? {{activeStreamId:'st-1'}} : {{activeStreamId:null}};
              _normalizeTransferredLiveProse(turn);
              segs.forEach((seg,i)=>{{
                const expect=c.expect[i];
                const hidden=seg.hidden
                  || seg.getAttribute('aria-hidden')==='true'
                  || seg.classList.contains('assistant-segment-worklog-source');
                assert.strictEqual(hidden, expect.hidden,
                  c.name+': segment '+i+' hidden='+hidden+' expected '+expect.hidden);
              }});
            }}
            console.log('OK');
            """
        )

    def test_normalizes_unless_scene_row_owns_the_prose(self):
        answer = "The final answer is 42, verified against the fixture."
        cases = [
            {
                # Blank-settled transferred node: no scene rows left at all →
                # the retained prose must be revealed (#7676 reported symptom).
                "name": "no scene rows left",
                "segs": [
                    {
                        "text": answer,
                        "classes": [
                            "assistant-segment",
                            "assistant-segment-worklog-source",
                        ],
                        "attrs": {"aria-hidden": "true", "data-live-assistant": "1"},
                        "hidden": True,
                    }
                ],
                "rows": [],
                "expect": [{"hidden": False}],
            },
            {
                # Another visible scene row genuinely shows this prose → keep it
                # hidden (no duplicate answer).
                "name": "visible scene row owns the prose",
                "segs": [
                    {
                        "text": answer,
                        "classes": [
                            "assistant-segment",
                            "assistant-segment-worklog-source",
                        ],
                        "attrs": {"aria-hidden": "true", "data-live-assistant": "1"},
                        "hidden": True,
                    }
                ],
                "rows": [
                    {
                        "text": "Working… " + answer,
                        "classes": ["transparent-event-row"],
                        "attrs": {"data-anchor-scene-row": "1"},
                        "hidden": False,
                    }
                ],
                "expect": [{"hidden": True}],
            },
            {
                # Visible activity rows that do NOT carry the answer (they show
                # tool steps) → the prose is not owned, reveal it.
                "name": "visible rows do not own the prose",
                "segs": [
                    {
                        "text": answer,
                        "classes": [
                            "assistant-segment",
                            "assistant-segment-worklog-source",
                        ],
                        "attrs": {"aria-hidden": "true", "data-live-assistant": "1"},
                        "hidden": True,
                    }
                ],
                "rows": [
                    {
                        "text": "Read file package.json (12ms)",
                        "classes": ["transparent-event-row"],
                        "attrs": {"data-anchor-scene-row": "1"},
                        "hidden": False,
                    }
                ],
                "expect": [{"hidden": False}],
            },
            {
                # Same frame, but a stream is STILL ATTACHED (mid-stream render):
                # the transparent live scene keeps the streaming prose hidden on
                # purpose — do not start showing it early.
                "name": "mid-stream live scene still owns the frame",
                "streaming": True,
                "segs": [
                    {
                        "text": answer,
                        "classes": [
                            "assistant-segment",
                            "assistant-segment-worklog-source",
                        ],
                        "attrs": {"aria-hidden": "true", "data-live-assistant": "1"},
                        "hidden": True,
                    }
                ],
                "rows": [
                    {
                        "text": "Read file package.json (12ms)",
                        "classes": ["transparent-event-row"],
                        "attrs": {"data-anchor-scene-row": "1"},
                        "hidden": False,
                    }
                ],
                "expect": [{"hidden": True}],
            },
            {
                # A HIDDEN row does not own the prose either.
                "name": "hidden scene row does not own the prose",
                "segs": [
                    {
                        "text": answer,
                        "classes": [
                            "assistant-segment",
                            "assistant-segment-worklog-source",
                        ],
                        "attrs": {"aria-hidden": "true", "data-live-assistant": "1"},
                        "hidden": True,
                    }
                ],
                "rows": [
                    {
                        "text": answer,
                        "classes": ["transparent-event-row"],
                        "attrs": {"data-anchor-scene-row": "1", "aria-hidden": "true"},
                        "hidden": True,
                    }
                ],
                "expect": [{"hidden": False}],
            },
        ]
        import json

        assert "OK" in self._harness(json.dumps(cases))

    def test_helper_is_wired_into_the_preserve_swap(self):
        ui = read("static/ui.js")
        assert "function _normalizeTransferredLiveProse(" in ui, (
            "_normalizeTransferredLiveProse must exist (#7676)"
        )
        m = re.search(r"if\(_preservedLiveTurn\)\{(.{0,600}?)const _rebuilt=", ui, re.S)
        assert m, "preserve-swap block not found"
        assert "_normalizeTransferredLiveProse(_preservedLiveTurn)" in m.group(1), (
            "the transferred node must be normalized before any swap branch runs"
        )


class TestInvariantAfterSwap:
    """Recommendation 3: the zero-visible-content sweep runs after the swap."""

    def test_sweep_runs_after_preservation_swap(self):
        ui = read("static/ui.js")
        sweep = ui.find("  // Fail-safe invariant (#3875):")
        swap = ui.find("  // Re-attach the preserved live turn (#3877):".replace(":", "."))
        if swap < 0:
            swap = ui.find("  // Re-attach the preserved live turn (#3877).")
        scroll = ui.find("  // Only force-scroll when not actively streaming")
        assert sweep > 0 and swap > 0 and scroll > 0, "anchors not found in ui.js"
        assert swap < sweep < scroll, (
            "the #3875 invariant sweep must run AFTER the preserve/swap block and "
            "before the post-render scroll pass (#7676: the swap could otherwise "
            "re-pin a blank node with the invariant already satisfied upstream)"
        )

    def test_sweep_covers_a_settled_live_turn(self):
        ui = read("static/ui.js")
        i = ui.find("  // Fail-safe invariant (#3875):")
        j = ui.find("  // Only force-scroll when not actively streaming", i)
        assert 0 < i < j, "sweep block not found"
        block = ui[i:j]
        assert "_liveTurnSettled" in block, (
            "the sweep must decide whether the live turn is still stream-owned (#7676)"
        )
        assert "if(turn.id==='liveAssistantTurn'&&!_liveTurnSettled) continue;" in block, (
            "the live turn may only be skipped while a stream still owns it"
        )


class TestTransparentSettleInPlace:
    """Recommendation 4: transparent settlement finalizes the turn in place."""

    def test_finalizer_exists_and_is_used_at_settle(self):
        ui = read("static/ui.js")
        assert "function _finalizeJustSettledTransparentScene(" in ui, (
            "_finalizeJustSettledTransparentScene must exist (#7676)"
        )
        msg = read("static/messages.js")
        i = msg.find("_armKeepSettledWorklogOpen(_settledStreamId)")
        assert i > 0, "settle render sequence not found in messages.js"
        window = msg[i : i + 1600]
        assert "_finalizeJustSettledTransparentScene(_settledStreamId)" in window, (
            "STREAM_DONE must try the in-place transparent finalize before "
            "falling back to the second full render (#7676)"
        )
        # The fallback full rebuild must remain for everything that does not
        # finalize in place (compact worklog, no scene, blank result).
        assert "_renderMessagesWithScrollSnapshot({_prescrollSnapshot:_doneLiveScrollSnapshot})" in window
        assert "!_settledInPlace&&typeof _renderMessagesWithScrollSnapshot" in window

    def test_finalizer_source_only_returns_true_when_visible(self):
        src = extract_function(read("static/ui.js"), "_finalizeJustSettledTransparentScene")
        assert "isTransparentStream" in src, "scoped to Transparent Stream"
        assert "_renderSettledAnchorSceneTransparentForMessage" in src, (
            "the in-place finalize must re-render the settled transparent scene"
        )
        assert "_assistantTurnHasVisibleRenderedSegment" in src, (
            "must refuse to claim an in-place settle that left the turn blank"
        )
        assert "_sessionHtmlCache.delete" in src, (
            "the skipped full render also rewrote the session HTML cache; drop "
            "the stale entry so a later switch rebuilds from S.messages"
        )
