"""Regression tests for #7899 / #7925 — the bounded same-session reload.

#7899: with a large session (multi-MB transcript), every focus/SSE
reconciliation on a >500-row session used to fall back to a bare
full-transcript GET (no msg_limit), forcing the backend to re-run the full
merge on the whole transcript each time.

#7925 re-gate (blocking, scope): making the fallback explicit (`msg_limit=all`)
did not fix #7899 — server-side, ``all`` and master's bare GET are the same
request, so a refresh of a session the user has scrolled deep into still
re-downloaded the entire transcript. The fix this file drives is the
append-only fast path: the client asks for only the rows strictly after its
last rendered index (``msg_after`` + ``anchor_fp`` + ``transcript_generation``)
and appends them to what is rendered. The server refuses the fast path (and
returns the authoritative full document) when the anchor fingerprint or the
transcript generation changed or the count shrank — so compaction, undo and
edits stay exactly as safe as master's full reload.

These tests drive the REAL ``_ensureMessagesLoaded`` / ``_messageReloadLimitForSession``
from static/sessions.js in a Node harness against a fake server that applies
the same 500-row ``msg_limit`` ceiling as ``api/routes.py``, and assert on the
number of ROWS FETCHED — a query-string pin could not detect a full download
(the old head sent ``msg_limit=all`` and the mutant ``msg_limit=999999`` past
the ceiling, both of which still fetched the whole transcript).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

#: The reload-path helpers extracted from sessions.js and fed to the harness.
_HARNESS_FUNCTIONS = (
    "_ensureMessagesLoaded",
    "_messageReloadLimitForSession",
    "_appendAnchorRowFingerprint",
    "_syncToolCallsForLoadedMessages",
    "_mergeSessionToolCards",
)


def _extract_function(source: str, name: str) -> str:
    """Return the full function source for ``name`` from static/sessions.js.

    Brace-depth tracking handles nested blocks; the signature is located with
    paren-depth tracking so a default parameter containing a string with a
    brace cannot mislead the search.
    """
    marker = f"async function {name}("
    start = source.find(marker)
    if start < 0:
        marker = f"function {name}("
        start = source.find(marker)
    assert start >= 0, f"{name} not found in sessions.js"
    signature_start = source.find("(", start)
    assert signature_start >= 0, f"function {name} is missing its parameter list"
    paren_depth = 0
    in_string = None
    escaped = False
    in_line_comment = False
    in_block_comment = False
    signature_end = None
    for index in range(signature_start, len(source)):
        ch = source[index]
        nxt = source[index + 1] if index + 1 < len(source) else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
            continue
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
            continue
        if ch == "/" and nxt == "/":
            in_line_comment = True
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            continue
        if ch in ('\'', '"', "`"):
            in_string = ch
            continue
        if ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth -= 1
            if paren_depth == 0:
                signature_end = index + 1
                break
    assert signature_end is not None, f"function {name} has an unterminated parameter list"
    brace_start = source.find("{", signature_end)
    assert brace_start >= 0, f"function {name} is missing '{{'"
    depth = 0
    end = brace_start
    for i in range(brace_start, len(source)):
        c = source[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    return source[start:end]


_HARNESS_SRC = "\n".join(_extract_function(SESSIONS_JS, name) for name in _HARNESS_FUNCTIONS)

#: Stub preamble: every free variable the extracted helpers touch is defined
#: here so the harness can drive the REAL functions without a DOM.
_HARNESS_PREAMBLE = r"""
'use strict';
const fs = require('fs');
const _INITIAL_MSG_LIMIT = 30;
const _MSG_LIMIT_MAX = 500;
let S = null;
let _oldestIdx = 0;
let _messagesTruncated = false;
let _msgLimitMax = 500;
let _loadingSessionId = null;
let _loadSessionGeneration = null;
let _sameSessionForceReloadHint = null;
let _pendingCarryForwardSnapshot = null;
let _messageRenderWindowSize = 200;
// #7925 re-gate (2026-10-27, finding 4): the prefix digest the client holds
// for its view, and the absolute held-end it covers. Seeded by runScenario
// exactly like the load that established the view (the server mints the
// digest over rows [0, coverage end) and the client stores it).
let _sessionPrefixFp = '';
let _sessionPrefixFpEnd = -1;
// The prefix digest is server-private (the client only stores and echoes
// it), so any deterministic function over the render-relevant fields of
// rows [0, K) serves the harness — the coverage length rides along so two
// different ranges can never collide.
function _prefixFpFor(rows){
  let h=0x811c9dc5>>>0;
  const s=JSON.stringify(rows.map(r=>({role:r.role,content:r.content,reasoning:r.reasoning,attachments:r.attachments,tool_calls:r.tool_calls})));
  for(let i=0;i<s.length;i++){ h=(h^s.charCodeAt(i))>>>0; h=(Math.imul(h,0x01000193))>>>0; }
  return (h>>>0).toString(36).padStart(8,'0')+':'+rows.length;
}
const INFLIGHT = {};
const window = globalThis;
globalThis.window = window;

function _clearSameSessionForceReloadHint(sid){ if(!_sameSessionForceReloadHint) return; if(!sid || _sameSessionForceReloadHint.session_id===sid) _sameSessionForceReloadHint=null; }
// #7925 re-gate (2026-10-27, silent finding): the loaded card list kept
// across loadSession's metadata replacement (the real _syncToolCallsFor-
// LoadMessages is extracted, so the full merge→sync chain runs here).
let _pendingSessionToolCalls = null;
function clearLiveToolCards() {}
function clearVisibleMessageRowCache() {}
function _messageRenderableMessageCount(){ return S && Array.isArray(S.messages) ? S.messages.length : 0; }
function _currentMessageRenderWindowSize(){ return _messageRenderWindowSize; }
const MESSAGE_RENDER_WINDOW_DEFAULT = 50;
function _adoptRegenerationRevision() {}
function _hydrateTodosFromSession() {}
function scheduleTodosRefresh() {}
function _isSessionActivelyViewedForList(){ return true; }
function _setSessionViewedCount() {}
function syncTopbar() {}

// ── Fake server: mirrors api/routes.py's window contract ─────────────────────
//  - msg_after=N + anchor_fp + transcript_generation: the append-only fast
//    path. Valid (generation matches, anchor row exists and its fingerprint
//    matches) → only the rows strictly after N (capped like any bounded
//    window); any doubt → the authoritative FULL transcript (no _append_only),
//    exactly what master's bare GET returns.
//  - msg_limit=all / no msg_limit: the full transcript.
//  - numeric msg_limit ≤ ceiling: the tail window of that many renderable
//    rows, offset = its absolute start (the 500-row ceiling contract).
function makeRows(n){
  const rows=[];
  for(let i=0;i<n;i++) rows.push({role:(i%2===0)?'user':'assistant', content:'m'+i, timestamp:1700000000+i});
  return rows;
}
function isRenderable(r){ return !!r && (r.role==='user'||r.role==='assistant'); }
function appendWindow(after, cap){
  // Mirror api/routes.py:_message_window_for_display(msg_after=...): the
  // window is the CONTIGUOUS suffix starting at the anchor's NEXT row;
  // renderable rows decide the budget, never where the window begins. The
  // old shape scanned forward for the first renderable row and started
  // there, dropping any tool-result row between the anchor and it — a tab
  // loading mid-turn then never received that output (2026-10-27
  // should-fix).
  let last=-1, count=0;
  for(let i=0;i<after.length;i++){
    if(isRenderable(after[i])){
      count++;
      last=i;  // the latest renderable row bounds the window even under the cap
      if(count>=cap){ break; }
    }
  }
  if(last===-1) return {window:[], offset:after.length};
  return {window:after.slice(0,last+1), offset:0};
}
function tailWindow(rows, limit){
  let start=0, count=0;
  for(let i=rows.length-1;i>=0;i--){ if(isRenderable(rows[i])){ count++; if(count>=limit){ start=i; break; } } }
  return {window:rows.slice(start), offset:start};
}
function makeFakeServer(state){
  return function api(url){
    state.requests.push(url);
    const q=new URLSearchParams(url.split('?')[1]);
    const msgAfter=q.get('msg_after');
    const msgLimitRaw=q.get('msg_limit');
    const limit=(msgLimitRaw==='all'||msgLimitRaw===null)?null:parseInt(msgLimitRaw,10);
    const base={session_id:'s1', transcript_generation:state.generation, message_count:state.rows.length,
                visible_message_count:state.rows.length, _msg_limit_max:500};
    if(msgAfter!==null){
      const afterIdx=parseInt(msgAfter,10);
      const anchor=state.rows[afterIdx];
      const genRaw=q.get('transcript_generation');
      const gen=(genRaw===null||genRaw==='')?-1:parseInt(genRaw,10);
      // The real server fingerprints the row's PUBLIC projection; these test
      // rows have no internal fields and nothing to redact, so the projection
      // is identity and the client fingerprint compares equal.
      // #7925 re-gate (2026-10-27, finding 4): the append must also carry the
      // prefix digest over [0, afterIdx+1) — a missing or stale digest is
      // refused exactly like a generation or anchor mismatch.
      const pfRaw=q.get('prefix_fp');
      const ok=(gen===state.generation && anchor && typeof anchor==='object'
                && q.get('anchor_fp')===_appendAnchorRowFingerprint(anchor)
                && pfRaw!==null && pfRaw===_prefixFpFor(state.rows.slice(0, afterIdx+1)));
      if(ok){
        const aw=appendWindow(state.rows.slice(afterIdx+1), limit===null?500:limit);
        const offset=afterIdx+1+aw.offset;
        state.rowsReturned+=aw.window.length;
        return {session:{...base, messages:aw.window, _messages_offset:offset,
                         _messages_truncated:(offset+aw.window.length)<state.rows.length, _append_only:true,
                         tool_calls:state.appendToolCalls.slice(),
                         prefix_fp:_prefixFpFor(state.rows.slice(0, offset+aw.window.length))}};
      }
      state.rowsReturned+=state.rows.length;
      return {session:{...base, messages:state.rows.slice(), _messages_offset:0, _messages_truncated:false,
                       prefix_fp:_prefixFpFor(state.rows)}};
    }
    if(limit===null){
      state.rowsReturned+=state.rows.length;
      return {session:{...base, messages:state.rows.slice(), _messages_offset:0, _messages_truncated:false,
                       prefix_fp:_prefixFpFor(state.rows)}};
    }
    const tw=tailWindow(state.rows, limit);
    state.rowsReturned+=tw.window.length;
    return {session:{...base, messages:tw.window, _messages_offset:tw.offset, _messages_truncated:tw.offset>0,
                     prefix_fp:_prefixFpFor(state.rows.slice(0, tw.offset+tw.window.length))}};
  };
}

async function runScenario(sc){
  const state={rows:sc.serverRows, generation:sc.generation, requests:[], rowsReturned:0, appendToolCalls:sc.appendToolCalls||[]};
  globalThis.api=makeFakeServer(state);
  S={messages:(sc.clientRows||[]).slice(), session:{session_id:'s1',
      transcript_generation:sc.clientGeneration!==undefined?sc.clientGeneration:sc.generation,
      message_count:sc.sessionMessageCount!==undefined?sc.sessionMessageCount:sc.serverRows.length},
      busy:false, lastUsage:{}};
  _loadingSessionId='s1';
  _oldestIdx=sc.oldestIdx!==undefined?sc.oldestIdx:0;
  _messagesTruncated=sc.truncated!==undefined?sc.truncated:false;
  _msgLimitMax=500;
  _sameSessionForceReloadHint=sc.hint||null;
  _messageRenderWindowSize=sc.renderWindowSize!==undefined?sc.renderWindowSize:200;
  // #7925 re-gate (2026-10-27, finding 4): seed the client's held prefix
  // digest exactly like the load that established its view did — minted by
  // the server over rows [0, held end). A paged view's digest still covers
  // [0, held end): the append vouches for the whole prefix, not just the
  // rows on screen. Bare identifiers: the harness shares one script scope
  // with the extracted helpers (globalThis.* would not reach the `let`).
  // `noPrefixFp` models a client that holds the rows but cannot PROVE them
  // (no digest from the load that established its view) — the append fast
  // path is then skipped and the reload takes the authoritative full fetch.
  const _heldEnd=_oldestIdx+(sc.clientRows||[]).length;
  _sessionPrefixFp=sc.noPrefixFp ? '' : _prefixFpFor(sc.serverRows.slice(0,_heldEnd));
  _sessionPrefixFpEnd=sc.noPrefixFp ? -1 : _heldEnd;
  // A non-destructive server-side edit that lands AFTER the client's view
  // was established (settled-reasoning backfill, saved attachments): same
  // generation, anchor row untouched — only the prefix digest catches it.
  if(sc.editRow && typeof sc.editRow.index==='number' && state.rows[sc.editRow.index]){
    Object.assign(state.rows[sc.editRow.index], sc.editRow.fields||{});
  }
  // #7925 re-gate (2026-10-27, silent finding): what loadSession's metadata
  // adoption leaves on S.session (the messages=0 response's empty list) and
  // the loaded card list kept across that replacement.
  if(S.session){ S.session.tool_calls = sc.sessionToolCalls || []; }
  _pendingSessionToolCalls = (sc.heldToolCalls||[]).map(tc=>({...tc}));
  await _ensureMessagesLoaded('s1', {force:true});
  const last=(Array.isArray(S.messages)&&S.messages.length)?S.messages[S.messages.length-1]:null;
  return {requests:state.requests, rowsReturned:state.rowsReturned,
          messagesLen:Array.isArray(S.messages)?S.messages.length:0,
          messages:Array.isArray(S.messages)?S.messages:[],
          toolCalls:(S.session&&Array.isArray(S.session.tool_calls))?S.session.tool_calls:[],
          firstContent:(Array.isArray(S.messages)&&S.messages.length)?S.messages[0].content:null,
          lastContent:last?last.content:null,
          oldestIdx:_oldestIdx, truncated:_messagesTruncated,
          renderWindowSize:_messageRenderWindowSize};
}
"""


def _run_harness(scenario_json: str, tmp_path: Path) -> dict:
    assert NODE is not None, "node is required"
    script = (
        _HARNESS_PREAMBLE
        + "\n"
        + _HARNESS_SRC
        + "\n"
        + "runScenario(JSON.parse(fs.readFileSync(process.argv[2], 'utf8'))).then(r => { console.log('RESULT:' + JSON.stringify(r)); })\n"
    )
    script_path = tmp_path / "harness.js"
    scenario_path = tmp_path / "scenario.json"
    script_path.write_text(script, encoding="utf-8")
    scenario_path.write_text(scenario_json, encoding="utf-8")
    completed = subprocess.run(
        [NODE, str(script_path), str(scenario_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    output_lines = [
        line for line in completed.stdout.splitlines()
        if line.startswith("RESULT:")
    ]
    assert output_lines, (
        f"node produced no parseable output\nstdout={completed.stdout}\nstderr={completed.stderr}"
    )
    return json.loads(output_lines[-1][len("RESULT:"):])


def _hint(loaded, message_count, truncated):
    return {
        "session_id": "s1",
        "loaded_renderable_count": loaded,
        "loaded_message_count": loaded,
        "message_count": message_count,
        "truncated": truncated,
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
class TestAppendOnlyFastPath:
    """The real reload function, driven against the 500-row-window fake server.

    Each scenario asserts on ROWS FETCHED — a request-shape pin cannot detect
    a full re-download (the pre-fix head sent ``msg_limit=all`` over the
    ceiling and still fetched every row).
    """

    def test_over_ceiling_paged_refresh_fetches_only_new_rows(self, tmp_path):
        # The #7899 shape (maintainer table: "500 rows paged, 1 appended"): the
        # client paged until it held 500 rows ending at the newest row
        # [1500..2000) of a 2001-row transcript — a truncated window (older
        # rows unloaded), so the width hint (501) exceeds the 500-row ceiling.
        # Old head: msg_limit=all → 2001 rows downloaded. Fast path:
        # msg_after=1999 → exactly the 1 new row.
        rows = make_rows_json(2001)
        scenario = {
            "serverRows": rows,
            "clientRows": rows[1500:2000],
            "oldestIdx": 1500,
            "truncated": True,
            "generation": 0,
            "hint": _hint(500, 2000, True),
            "sessionMessageCount": 2001,
        }
        result = _run_harness(json.dumps(scenario), tmp_path)
        assert "msg_after=1999" in result["requests"][0], result["requests"]
        assert "msg_limit=all" not in result["requests"][0], result["requests"]
        assert result["rowsReturned"] == 1, result
        assert result["messagesLen"] == 501, result
        assert result["lastContent"] == "m2000", result
        assert result["oldestIdx"] == 1500, result
        assert result["truncated"] is True, result

    def test_paged_631_rows_three_appended_fetches_only_new_rows(self, tmp_path):
        # Maintainer table: "631 rows paged, 3 appended" — the client holds
        # the newest 631 rows [1369..2000) of a 2003-row transcript. Old head:
        # 2003 rows. Fast path: msg_after=1999 → the 3 new rows.
        rows = make_rows_json(2003)
        scenario = {
            "serverRows": rows,
            "clientRows": rows[1369:2000],
            "oldestIdx": 1369,
            "truncated": True,
            "generation": 0,
            "hint": _hint(631, 2000, True),
            "sessionMessageCount": 2003,
        }
        result = _run_harness(json.dumps(scenario), tmp_path)
        assert "msg_after=1999" in result["requests"][0], result["requests"]
        assert result["rowsReturned"] == 3, result
        assert result["messagesLen"] == 634, result
        assert result["lastContent"] == "m2002", result
        assert result["oldestIdx"] == 1369, result
        assert result["truncated"] is True, result

    def test_fully_loaded_idle_poll_fetches_nothing(self, tmp_path):
        # 601 rows fully loaded, idle poll, nothing appended (maintainer
        # table: "601 rows fully loaded, idle poll"). Old head: 601 rows
        # re-downloaded. Fast path: msg_after=600 → empty append, 0 rows.
        rows = make_rows_json(601)
        scenario = {
            "serverRows": rows,
            "clientRows": rows[:601],
            "generation": 0,
            "hint": _hint(601, 601, False),
            "sessionMessageCount": 601,
        }
        result = _run_harness(json.dumps(scenario), tmp_path)
        assert "msg_after=600" in result["requests"][0], result["requests"]
        assert result["rowsReturned"] == 0, result
        assert result["messagesLen"] == 601, result
        assert result["lastContent"] == "m600", result

    def test_bounded_tail_refresh_keeps_bounded_replace_shape(self, tmp_path):
        # 30-row tail window, 3 appended: the hint is truncated, so the width
        # (33) fits under the ceiling and the reload stays the bounded replace
        # master uses — no fast path, no full download (maintainer table row 1).
        rows = make_rows_json(2001)
        scenario = {
            "serverRows": rows,
            "clientRows": rows[1971:2001],
            "generation": 0,
            "hint": _hint(30, 30, True),
            "sessionMessageCount": 33,
        }
        result = _run_harness(json.dumps(scenario), tmp_path)
        assert "msg_after=" not in result["requests"][0], result["requests"]
        assert "msg_limit=33" in result["requests"][0], result["requests"]
        assert "msg_limit=all" not in result["requests"][0], result["requests"]
        assert result["rowsReturned"] == 33, result
        assert result["messagesLen"] == 33, result

    def test_generation_changed_falls_back_to_full_fetch(self, tmp_path):
        # A destructive rewrite (truncate/retry/undo) bumped the generation:
        # the retained prefix can no longer be trusted, so the fast path is
        # refused and the authoritative full transcript replaces the view.
        rows = make_rows_json(2001)
        scenario = {
            "serverRows": rows,
            "clientRows": rows[:500],
            "oldestIdx": 0,
            "generation": 1,  # server moved on
            "clientGeneration": 0,  # client still sends the generation it saw
            "hint": _hint(500, 2000, False),
            "sessionMessageCount": 2001,
        }
        result = _run_harness(json.dumps(scenario), tmp_path)
        assert "msg_after=499" in result["requests"][0], result["requests"]
        assert result["rowsReturned"] == 2001, result
        assert result["messagesLen"] == 2001, result
        assert result["oldestIdx"] == 0, result

    def test_anchor_mismatch_falls_back_to_full_fetch(self, tmp_path):
        # The seam row changed without a generation bump (e.g. a middle edit
        # that touched the last rendered row): the anchor fingerprint no
        # longer matches, so the full document is served — never stale rows.
        server_rows = make_rows_json(2001)
        client_rows = [dict(r) for r in server_rows[:500]]
        client_rows[499]["content"] = "m499-edited"
        scenario = {
            "serverRows": server_rows,
            "clientRows": client_rows,
            "generation": 0,
            "hint": _hint(500, 2000, False),
            "sessionMessageCount": 2001,
        }
        result = _run_harness(json.dumps(scenario), tmp_path)
        assert "msg_after=499" in result["requests"][0], result["requests"]
        assert result["rowsReturned"] == 2001, result
        assert result["messagesLen"] == 2001, result
        assert result["firstContent"] == "m0", result

    def test_count_shrank_falls_back_to_full_fetch(self, tmp_path):
        # The transcript shrank below the client's anchor: the anchor row no
        # longer exists, so the fast path is refused and the current (smaller)
        # authoritative document replaces the view.
        rows = make_rows_json(500)
        scenario = {
            "serverRows": rows,
            "clientRows": make_rows_json(600)[:600],  # client saw 600 rows
            "generation": 0,
            "hint": _hint(600, 600, False),
            "sessionMessageCount": 500,
        }
        result = _run_harness(json.dumps(scenario), tmp_path)
        assert "msg_after=599" in result["requests"][0], result["requests"]
        assert result["rowsReturned"] == 500, result
        assert result["messagesLen"] == 500, result
        assert result["lastContent"] == "m499", result


    def test_paged_append_carries_the_prefix_digest(self, tmp_path):
        # #7925 re-gate (2026-10-27, finding 4): a paged view's digest still
        # covers [0, held end) — the append vouches for the whole prefix, not
        # just the rows on screen. The request must carry it and the append
        # must still fetch only the new row.
        rows = make_rows_json(2001)
        scenario = {
            "serverRows": rows,
            "clientRows": rows[1500:2000],
            "oldestIdx": 1500,
            "truncated": True,
            "generation": 0,
            "hint": _hint(500, 2000, True),
            "sessionMessageCount": 2001,
        }
        result = _run_harness(json.dumps(scenario), tmp_path)
        assert "prefix_fp=" in result["requests"][0], result["requests"]
        assert result["rowsReturned"] == 1, result
        assert result["messagesLen"] == 501, result
        assert result["lastContent"] == "m2000", result

    def test_nondestructive_earlier_row_edit_forces_the_full_fetch(self, tmp_path):
        # #7925 re-gate (2026-10-27, finding 4): a settled-reasoning backfill
        # lands on row 100 — a row this paged client does NOT even hold, but
        # inside the [0, 2000) prefix its digest vouches for. No generation
        # bump, the anchor row untouched: only the prefix digest catches it,
        # and the client must take the authoritative full fetch (which also
        # delivers the backfilled reasoning).
        rows = make_rows_json(2001)
        scenario = {
            "serverRows": rows,
            "clientRows": rows[1500:2000],
            "oldestIdx": 1500,
            "truncated": True,
            "generation": 0,
            "hint": _hint(500, 2000, True),
            "sessionMessageCount": 2001,
            "editRow": {"index": 100, "fields": {"reasoning": "backfilled"}},
        }
        result = _run_harness(json.dumps(scenario), tmp_path)
        assert "prefix_fp=" in result["requests"][0], result["requests"]
        assert result["rowsReturned"] == 2001, result
        assert result["messagesLen"] == 2001, result
        assert any(m.get("reasoning") == "backfilled" for m in result["messages"]), (
            "the full fetch must deliver the edited row"
        )

    def test_append_reexpands_the_render_window(self, tmp_path):
        # 2026-10-27 should-fix: the append path returned without re-expanding
        # the render window (only the replace path did), so after an append
        # the hidden-rows affordance still described the pre-append window —
        # it read 551 where master's replace reads 401. The append now runs
        # the same bounded expansion as the replace path.
        rows = make_rows_json(2001)
        scenario = {
            "serverRows": rows,
            "clientRows": rows[1500:2000],
            "oldestIdx": 1500,
            "truncated": True,
            "generation": 0,
            "hint": _hint(500, 2000, True),
            "sessionMessageCount": 2001,
            "renderWindowSize": 50,
        }
        result = _run_harness(json.dumps(scenario), tmp_path)
        assert result["rowsReturned"] == 1, result
        # min(501 loaded rows, 4 × the 50-row default) = 200 — the replace
        # path's #6999-capped expansion, now applied on the append path too.
        assert result["renderWindowSize"] == 200, result

    def test_append_keeps_older_session_tool_cards(self, tmp_path):
        # 2026-10-27 silent finding: loadSession's metadata adoption empties
        # S.session.tool_calls (the messages=0 response carries []), and the
        # append response then carries ONLY the new owners — merging into the
        # emptied list dropped every older card, so after appending a new
        # reply `older-card` was gone (maintainer's Chromium repro through
        # the real loadSession). The loaded list is now kept across the
        # metadata replacement and the new owner merges into it, rebased
        # into the combined transcript's coordinate space.
        rows = make_rows_json(2001)
        scenario = {
            "serverRows": rows,
            "clientRows": rows[1500:2000],
            "oldestIdx": 1500,
            "truncated": True,
            "generation": 0,
            "hint": _hint(500, 2000, True),
            "sessionMessageCount": 2001,
            # What the metadata adoption leaves on S.session …
            "sessionToolCalls": [],
            # … and the loaded list kept across that replacement.
            "heldToolCalls": [{"tid": "tc-old", "assistant_msg_idx": 100, "name": "old"}],
            # The append window's own card, window-relative.
            "appendToolCalls": [{"tid": "tc-new", "assistant_msg_idx": 0, "name": "new"}],
        }
        result = _run_harness(json.dumps(scenario), tmp_path)
        tids = [tc.get("tid") for tc in result["toolCalls"]]
        assert "tc-old" in tids, result["toolCalls"]
        assert "tc-new" in tids, result["toolCalls"]
        new_card = next(tc for tc in result["toolCalls"] if tc.get("tid") == "tc-new")
        # _messages_offset 2000 - _oldestIdx 1500 = 500 shift onto the
        # combined transcript's coordinate space.
        assert new_card.get("assistant_msg_idx") == 500, new_card
        old_card = next(tc for tc in result["toolCalls"] if tc.get("tid") == "tc-old")
        assert old_card.get("assistant_msg_idx") == 100, old_card

    def test_append_window_keeps_tool_rows_after_the_anchor(self, tmp_path):
        # 2026-10-27 should-fix: the fake server used to skip to the first
        # renderable row, dropping any tool-result row between the anchor and
        # it. The real server keeps the CONTIGUOUS suffix after the anchor —
        # a tab that captured its anchor before the tool result was saved
        # must still receive that output.
        rows = make_rows_json(501)
        rows[500] = {
            "role": "assistant",
            "content": "calling",
            "timestamp": 1700000500,
            "tool_calls": [{"id": "t1"}],
        }
        rows.append(
            {"role": "tool", "tool_call_id": "t1", "content": "the tool output",
             "timestamp": 1700000501}
        )
        rows.append(
            {"role": "assistant", "content": "after the tool", "timestamp": 1700000502}
        )
        scenario = {
            "serverRows": rows,
            "clientRows": rows[:501],
            "oldestIdx": 0,
            "generation": 0,
            "hint": _hint(501, 503, False),
            "sessionMessageCount": 503,
        }
        result = _run_harness(json.dumps(scenario), tmp_path)
        assert "prefix_fp=" in result["requests"][0], result["requests"]
        # The tool row travels with the window: 2 new rows, not 1.
        assert result["rowsReturned"] == 2, result
        assert result["messagesLen"] == 503, result
        assert any(m.get("content") == "the tool output" for m in result["messages"]), (
            "the tool-result row after the anchor was dropped"
        )
        assert result["lastContent"] == "after the tool", result

    def test_over_ceiling_without_a_provable_prefix_uses_the_bare_full_fetch(self, tmp_path):
        # #7925 re-gate (2026-10-30, CI): the authoritative full-transcript
        # reload must keep master's BARE-GET shape — no `msg_limit`, no
        # `expand_renderable`. `msg_limit=all` is the SAME request server-side
        # (`_parse_msg_limit('all')` returns None; `explicit_all` is never
        # read), but api/routes.py reserves `all` for the frontend paths that
        # address rows by ABSOLUTE transcript index, and upstream's own
        # contract test (tests/test_inflight_tail_loadsession_canonical_indices.py)
        # pins the reload's full fetch to the no-msg_limit shape — the shape
        # this PR had broken, turning every pytest shard red.
        #
        # The client holds a >ceiling transcript but CANNOT prove its prefix
        # (no digest), so the append fast path is skipped and the reload
        # takes the authoritative full fetch.
        rows = make_rows_json(2001)
        scenario = {
            "serverRows": rows,
            "clientRows": rows[:1500],
            "oldestIdx": 0,
            "generation": 0,
            # A width hint that exceeds the 500-row ceiling …
            "hint": _hint(1500, 2000, True),
            "sessionMessageCount": 2001,
            # … and no provable prefix digest, so the fast path cannot run.
            "noPrefixFp": True,
        }
        result = _run_harness(json.dumps(scenario), tmp_path)
        request = result["requests"][0]
        assert "msg_after=" not in request, result["requests"]
        assert "msg_limit=" not in request, (
            "the over-ceiling full fetch must keep master's bare-GET shape "
            f"(no msg_limit), got {request}"
        )
        assert "expand_renderable" not in request, (
            "the over-ceiling full fetch must keep master's bare-GET shape "
            f"(no expand_renderable), got {request}"
        )
        # … and it still delivers the authoritative document.
        assert result["rowsReturned"] == 2001, result
        assert result["messagesLen"] == 2001, result
        assert result["oldestIdx"] == 0, result

    def test_append_refusal_fallback_uses_the_bare_full_fetch(self, tmp_path):
        # The append was refused (the transcript was rewritten), so the client
        # re-fetches the authoritative full transcript — again in master's
        # bare-GET shape, not `msg_limit=all`.
        rows = make_rows_json(2001)
        scenario = {
            "serverRows": rows,
            "clientRows": rows[:500],
            "oldestIdx": 0,
            "generation": 1,  # server moved on …
            "clientGeneration": 0,  # … so the client's anchor is refused
            "hint": _hint(500, 2000, False),
            "sessionMessageCount": 2001,
        }
        result = _run_harness(json.dumps(scenario), tmp_path)
        # The refused append falls through to the full fetch, which must not
        # carry msg_limit either.
        assert result["rowsReturned"] == 2001, result
        assert result["messagesLen"] == 2001, result
        assert all("msg_limit=all" not in req for req in result["requests"]), (
            f"the full-fetch fallback must not use msg_limit=all: {result['requests']}"
        )


def make_rows_json(n: int) -> list:
    """Rows in the same shape the Node harness builds (mirror of makeRows)."""
    return [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"m{i}",
            "timestamp": 1700000000 + i,
        }
        for i in range(n)
    ]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_anchor_fingerprint_matches_server_side_python(tmp_path):
    """The JS and Python anchor digests agree on the shapes the client renders.

    The server hashes the row's PUBLIC projection (internal fields stripped,
    content redacted) and the client hashes the row it rendered — for a row
    with internal fields or redactable content both sides must still produce
    the same digest, otherwise every refresh would fail closed to a full
    fetch. Any shape the two runtimes disagree on (exotic floats, a BOM,
    integers above 2^53) is EXPECTED to fail closed — the fast path is
    refused and master's full reload runs, never a stale row.
    """
    from api.helpers import _public_message_projection
    from api.routes import _append_anchor_fingerprint

    rows = [
        {"role": "assistant", "content": [{"type": "text", "text": "plain"}], "timestamp": 1700000000.0},
        {"role": "assistant", "content": [{"type": "text", "text": "plain"}], "timestamp": 1700000000.5},
        {"role": "user", "content": "emoji \U0001f600 ok", "timestamp": 1700000001},
        {"role": "assistant", "content": [{"type": "text", "text": "tool call"}],
         "timestamp": 1700000002, "tool_calls": [{"id": "call_abc"}, {"tool_call_id": "call_xyz"}]},
        {"role": "tool", "tool_call_id": "call_abc", "content": "result", "timestamp": 1700000002.25},
    ]
    for row in rows:
        projected = _public_message_projection(row, _enabled=True)
        py_fp = _append_anchor_fingerprint(projected)
        assert py_fp, f"server fingerprint empty for {row!r}"
        script = (
            _HARNESS_PREAMBLE
            + "\n"
            + _extract_function(SESSIONS_JS, "_appendAnchorRowFingerprint")
            + "\n"
            + "console.log('RESULT:' + _appendAnchorRowFingerprint(JSON.parse(fs.readFileSync(process.argv[2], 'utf8'))));\n"
        )
        script_path = tmp_path / "fp.js"
        row_path = tmp_path / "row.json"
        script_path.write_text(script, encoding="utf-8")
        row_path.write_text(json.dumps(projected), encoding="utf-8")
        completed = subprocess.run(
            [NODE, str(script_path), str(row_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        js_fp = next(
            (line[len("RESULT:"):] for line in completed.stdout.splitlines() if line.startswith("RESULT:")),
            None,
        )
        assert js_fp, f"node produced no fingerprint\nstdout={completed.stdout}\nstderr={completed.stderr}"
        assert js_fp == py_fp, f"digests diverged for {row!r}: js={js_fp} py={py_fp}"
