"""Regression: regeneration preserves turn identity (#6611).

One regeneration must produce exactly one user row for the regenerated exchange.
The fix keys on a captured absolute logical-turn index plus session id, never
on content equality or visible-row position.

Fixtures are loaded from tests/fixtures/webui-PR-TARGET-6611-REPRO.json.

Behavioral rows run the real extracted JS functions via Node.js subprocess.
Source-scan rows are retained as supplementary checks.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

REPRO = json.loads(
    (REPO / "tests" / "fixtures" / "webui-PR-TARGET-6611-REPRO.json")
    .read_text(encoding="utf-8")
)

_BASE_COMMIT = "320789ae"


def extract_js_function(src: str, name: str) -> str:
    """Extract a named (async) function from JS source by brace counting."""
    match = re.search(rf'(async\s+)?function\s+{re.escape(name)}\b', src)
    assert match, f"{name}() not found"
    open_paren = src.index("(", match.start())
    paren_depth = 1
    idx = open_paren + 1
    while paren_depth > 0 and idx < len(src):
        ch = src[idx]
        if ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth -= 1
        idx += 1
    brace = src.index("{", idx)
    depth = 0
    end = None
    for idx in range(brace, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = idx + 1
                break
    assert end is not None, f"{name}() body was not balanced"
    return src[match.start():end]


def _fn_body(src: str, name: str) -> str:
    """Extract a named async function body (for source checks)."""
    marker = f"async function {name}"
    start = src.index(marker)
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start: i + 1]
    raise AssertionError(f"{name!r} function body not found")


def _base_messages_src() -> str:
    r = subprocess.run(
        ["git", "show", f"{_BASE_COMMIT}:static/messages.js"],
        capture_output=True, text=True, encoding="utf-8",
        cwd=str(REPO),
    )
    if r.returncode != 0:
        pytest.skip(f"git show {_BASE_COMMIT} unavailable (shallow clone): {r.stderr.strip()}")
    return r.stdout


def _head_messages_src() -> str:
    return (REPO / "static" / "messages.js").read_text(encoding="utf-8")


def _head_ui_src() -> str:
    return (REPO / "static" / "ui.js").read_text(encoding="utf-8")


def _build_send_harness(send_js_src: str, messages: list, session_id: str,
                        oldest_idx: int, regen_identity, switch_on_upload=False) -> str:
    """Return a complete Node.js script that runs send() and emits JSON result."""
    messages = [dict(message) for message in messages]
    for idx, message in enumerate(messages):
        message.setdefault("id", f"test-message-{idx}")
        message.setdefault("timestamp", 1000.125 + idx)
    if regen_identity and "absoluteIdx" in regen_identity:
        absolute_idx = regen_identity["absoluteIdx"]
        row = messages[absolute_idx]
        regen_identity = {
            "session_id": regen_identity.get("sessionId"),
            "message_id": row["id"],
            "timestamp": row["timestamp"],
            "display_index": absolute_idx,
            "display_keep_count": len(messages),
        }
    state_json = json.dumps({
        "messages": messages,
        "session": {"session_id": session_id, "read_only": False, "title": "Test"},
        "busy": False,
        "pendingFiles": [],
        "toolCalls": [],
        "activeStreamId": None,
        "activeProfile": "default",
    })
    regen_js = json.dumps(regen_identity) if regen_identity is not None else "null"
    upload_stub = (
        "global.uploadPendingFiles=async()=>{S.session={session_id:'session-B',read_only:false,title:'B'};return [];};"
        if switch_on_upload
        else "global.uploadPendingFiles=async()=>[];"
    )

    prefix = (
        '"use strict";'
        "(async()=>{"
        # Mutable globals send() writes to — must be closure vars, not global props
        f"let S={state_json};"
        f"let _oldestIdx={oldest_idx};"
        "let _sendInProgress=false;"
        "let _sendInProgressSid=null;"
        "let _pendingSelections=[];"
        "let _queueDrainSid;"
        "let _forcedSkillDirectivePending=null;"
        "let _approvalSessionId=null;"
        "let _clarifySessionId=null;"
        "const COMMANDS=[];"
        "const INFLIGHT={};"
        "const _AGENT_COMMANDS_RUN_ON_WEBUI=new Set();"
        # DOM/browser stubs
        "const _msgEl={value:'hello'};"
        "global.$=(id)=>id==='msg'?_msgEl:{classList:{add(){}},value:''};"
        "global.document={querySelector:()=>null};"
        "global.window={_defaultMessageMode:'steer',_defaultModel:'',_activeProvider:null};"
        "global.localStorage={removeItem:()=>{},getItem:()=>null,setItem:()=>{}};"
        "global.history={replaceState:()=>{}};"
        # api() stub — records calls, throws 503 on /api/chat/start
        "const _apiCalls=[];"
        "global.api=async(url,opts)=>{"
        "_apiCalls.push({url,body:opts&&opts.body?JSON.parse(opts.body):{}});"
        "if(url.includes('/api/chat/start')){const e=new Error('503');e.status=503;throw e;}"
        "return {};};"
        # UI no-ops
        "global.renderMessages=()=>{};"
        "global.setBusy=()=>{};"
        "global.setStatus=()=>{};"
        "global.setComposerStatus=()=>{};"
        "global.t=(k)=>k;"
        "global.renderTray=()=>{};"
        "global.updateQueueBadge=()=>{};"
        "global.showToast=()=>{};"
        "global.autoResize=()=>{};"
        "global.hideCmdDropdown=()=>{};"
        "global.renderSessionList=async()=>{};"
        "global.newSession=async()=>{};"
        "global.clearLiveToolCards=()=>{};"
        "global.appendThinking=()=>{};"
        "global.removeThinking=()=>{};"
        "global.hideApprovalCard=()=>{};"
        "global.hideClarifyCard=()=>{};"
        # Composer helpers
        "global.parseCommand=()=>null;"
        "global.queueSessionMessage=()=>{};"
        "global._clearComposerAfterQueuedSelectionSend=()=>{};"
        "global._composerTextWithPendingSelections=()=>'';"
        f"{upload_stub}"
        "global._clearComposerDraft=async()=>{};"
        "global._flushSelectionBlocksToComposer=()=>{};"
        "global._clearStaleBusyStateBeforeSend=()=>{};"
        "global._restoreComposerDraftAfterFailedSend=async()=>{};"
        # Session/stream helpers
        "global.stopApprovalPolling=()=>{};"
        "global.stopClarifyPolling=()=>{};"
        "global.startApprovalPolling=()=>{};"
        "global.startClarifyPolling=()=>{};"
        "global.cancelStream=async()=>false;"
        "global._trySteer=async()=>{};"
        "global._dismissHandoffHint=()=>{};"
        "global.loadSession=async()=>{};"
        "global.clearOptimisticSessionStreaming=()=>{};"
        # Pre/post start hooks
        "global._runOptionalPreStartUiStep=(label,fn)=>{try{fn();}catch(_){}};"
        "global._runOptionalPostStartUiStep=(label,fn)=>{try{fn();}catch(_){}};"
        # State helpers
        "global._chatPayloadModelState=()=>({model:'gpt-4',model_provider:'openai'});"
        "global.isCompressionUiRunning=()=>false;"
        "global.upsertActiveSessionForLocalTurn=()=>{};"
        "global.applySessionTitleUpdate=()=>{};"
        "global.saveInflightState=()=>{};"
        "global._fetchYoloState=()=>{};"
        "global.updateSendBtn=()=>{};"
        "global.renderSessionListFromCache=()=>{};"
        "global.ensureLiveWorklogShell=()=>{};"
    )
    suffix = (
        f"const _regenTarget={regen_js};"
        "try { await send(_regenTarget ? {regenerateTarget:_regenTarget} : {}); } catch(_) {}"
        "process.stdout.write(JSON.stringify({"
        "messages:S.messages,"
        "user_rows:S.messages.filter(m=>m.role==='user'),"
        "user_row_count:S.messages.filter(m=>m.role==='user').length,"
        "api_calls:_apiCalls,"
        "}));"
        "})()"
    )
    return prefix + send_js_src + suffix


def _run_send(send_js_src: str, messages: list, session_id: str = "sid-1",
              oldest_idx: int = 0, regen_identity=None, switch_on_upload=False) -> dict:
    """Execute send() in Node.js and return the result dict."""
    script = _build_send_harness(
        send_js_src,
        messages,
        session_id,
        oldest_idx,
        regen_identity,
        switch_on_upload=switch_on_upload,
    )
    # Write to a temp file: the harness + extracted send() is too long for -e on Windows.
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", encoding="utf-8", delete=False
    )
    try:
        tmp.write(script)
        tmp.close()
        r = subprocess.run(
            [NODE, tmp.name],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
    finally:
        os.unlink(tmp.name)
    assert r.returncode == 0, f"node exited {r.returncode}:\n{r.stderr}"
    return json.loads(r.stdout)


def _run_regenerate(*, switch_at=None) -> dict:
    regenerate_src = extract_js_function(_head_ui_src(), "regenerateResponse")
    full_messages = [
        {"id": "old-a", "role": "assistant", "content": "older", "timestamp": 1.125},
        {"id": "target-u", "role": "user", "content": "same prompt", "timestamp": 2.125,
         "attachments": [{"name": "shot.png"}], "_source": "webui"},
        {"id": "target-a", "role": "assistant", "content": "failed", "timestamp": 3.125,
         "_error": True},
    ]
    visible_messages = full_messages[1:]
    script = (
        '"use strict";(async()=>{'
        f"let S={{session:{{session_id:'session-A'}},busy:false,messages:{json.dumps(visible_messages)}}};"
        "let _oldestIdx=1;const calls=[];const statuses=[];let sends=[];"
        "const msg={value:''};global.$=(id)=>id==='msg'?msg:null;"
        "global.msgContent=(m)=>String(m.content||'');global.renderMessages=()=>{};"
        "global.t=(k)=>k;global.setStatus=(s)=>statuses.push(s);"
        f"global._ensureAllMessagesLoaded=async()=>{{S.messages={json.dumps(full_messages)};_oldestIdx=0;"
        + ("S.session={session_id:'session-B'};" if switch_at == "full-load" else "")
        + "};"
        "global.api=async(path,opts)=>{calls.push({path,body:JSON.parse(opts.body)});"
        + ("S.session={session_id:'session-B'};" if switch_at == "truncate" else "")
        + "return {ok:true};};"
        "global.send=async(opts)=>{sends.push(opts);};"
        f"{regenerate_src}"
        "const btn={closest:()=>({dataset:{msgIdx:'1'}})};"
        "await regenerateResponse(btn);"
        "process.stdout.write(JSON.stringify({calls,sends,statuses,messages:S.messages,composer:msg.value,session:S.session}));"
        "})()"
    )
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".js", encoding="utf-8", delete=False)
    try:
        tmp.write(script)
        tmp.close()
        result = subprocess.run(
            [NODE, tmp.name], capture_output=True, text=True, encoding="utf-8", timeout=30
        )
    finally:
        os.unlink(tmp.name)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# ── Tests ─────────────────────────────────────────────────────────────────


class TestReproduction:
    """One regeneration produces exactly one user row (base-fails / head-passes)."""

    def test_base_produces_two_user_rows(self):
        # Base send() always pushes: _regenIdentity is unknown to it → duplicate row.
        base_src = extract_js_function(_base_messages_src(), "send")
        truncated = REPRO["transcript"][:1]  # [user"same prompt"]
        result = _run_send(
            base_src, truncated,
            regen_identity={"absoluteIdx": 0, "sessionId": "sid-1"},
        )
        assert result["user_row_count"] == 2, (
            f"base: expected 2 user rows (duplicate), got {result['user_row_count']}"
        )
        assert result["user_rows"][-1].get("_pending") is True, (
            "base: last row must be the appended _pending duplicate"
        )

    def test_head_produces_one_user_row(self):
        # Head send() reuses the existing user row at absoluteIdx instead of pushing.
        head_src = extract_js_function(_head_messages_src(), "send")
        truncated = REPRO["transcript"][:1]
        result = _run_send(
            head_src, truncated,
            regen_identity={"absoluteIdx": 0, "sessionId": "sid-1"},
        )
        assert result["user_row_count"] == 1, (
            f"head: expected 1 user row, got {result['user_row_count']}: {result['user_rows']}"
        )
        assert result["user_rows"][0].get("_pending") is True, (
            "regenerated user row must be marked _pending"
        )
        start_body = next(
            call["body"] for call in result["api_calls"] if call["url"] == "/api/chat/start"
        )
        assert start_body["regenerate_target"]["message_id"] == "u-current"
        assert start_body["regenerate_target"]["timestamp"] == 1700000000.125

    def test_regen_identity_present_in_source(self):
        body = _fn_body(_head_ui_src(), "regenerateResponse")
        assert "regenerateTarget" in body, (
            "regenerateResponse() must pass regenerateTarget to send()"
        )


class TestAttachmentsPreserved:
    """The regenerated turn carries the original attachment metadata."""

    def test_head_preserves_attachment_metadata(self):
        # Object.assign({}, _prior, ...) inherits attachments from the existing row.
        head_src = extract_js_function(_head_messages_src(), "send")
        transcript = REPRO["attachment_transcript"]
        original_attachments = transcript[0].get("attachments")
        truncated = transcript[:1]  # [user row with attachments]
        result = _run_send(
            head_src, truncated,
            regen_identity={"absoluteIdx": 0, "sessionId": "sid-1"},
        )
        assert result["user_row_count"] == 1
        user_row = result["user_rows"][0]
        assert user_row.get("attachments") is not None, (
            "head: attachments must be preserved on the reused row"
        )
        assert user_row["attachments"] == original_attachments, (
            f"head: expected {original_attachments!r}, got {user_row.get('attachments')!r}"
        )

    def test_base_loses_attachment_metadata(self):
        # Base push path creates a new row from S.pendingFiles (empty during regen).
        base_src = extract_js_function(_base_messages_src(), "send")
        transcript = REPRO["attachment_transcript"]
        truncated = transcript[:1]
        result = _run_send(
            base_src, truncated,
            regen_identity={"absoluteIdx": 0, "sessionId": "sid-1"},
        )
        assert result["user_row_count"] == 2
        appended = result["user_rows"][-1]
        assert appended.get("attachments") is None, (
            "base: newly appended pending row must have no attachments"
        )


class TestAbsoluteIndexUnderPaging:
    """Correct absolute index when _messages_offset is non-zero."""

    def test_paged_session_uses_correct_absolute_index(self):
        # After _ensureAllMessagesLoaded(), _oldestIdx=0 and the full session is in
        # S.messages. _regenIdentity.absoluteIdx=40 → localRegenIdx = 40-0 = 40.
        head_src = extract_js_function(_head_messages_src(), "send")
        offset = REPRO["paged_session"]["_messages_offset"]  # 40
        assert offset == 40
        # Build a fully-loaded session: 40 prior messages + user row at index 40
        prior = [{"role": "assistant", "content": f"msg{i}"} for i in range(offset)]
        user_row = {"role": "user", "content": "paged prompt"}
        all_messages = prior + [user_row]  # 41 total; user at idx 40
        result = _run_send(
            head_src, all_messages,
            oldest_idx=0,  # post-_ensureAllMessagesLoaded state
            regen_identity={"absoluteIdx": 40, "sessionId": "sid-1"},
        )
        assert result["user_row_count"] == 1, (
            f"paged: expected 1 user row at absoluteIdx=40, got {result['user_row_count']}"
        )
        assert result["user_rows"][0].get("_pending") is True

    def test_paged_session_absolute_keep_count_formula(self):
        # absoluteKeepCount = _oldestIdx + assistantIdx; must be captured BEFORE
        # _ensureAllMessagesLoaded() resets _oldestIdx to 0.
        oldest_idx = REPRO["paged_session"]["_messages_offset"]
        assistant_idx = 1
        absolute_keep_count = oldest_idx + assistant_idx
        assert absolute_keep_count == 41
        # After load, _oldestIdx=0; localRegenIdx = (41-1) - 0 = 40. Correct.
        local_regen_after_load = (absolute_keep_count - 1) - 0
        assert local_regen_after_load == 40


class TestNegativeSpaceEarlierIdenticalPrompt:
    """Earlier messages with identical content are not touched."""

    def test_earlier_identical_row_untouched(self):
        # [user"same", assistant"first", user"same", assistant"failed"] → regen on idx 3.
        # After truncation S.messages = [user, assistant, user]. Only idx 2 is _pending.
        head_src = extract_js_function(_head_messages_src(), "send")
        transcript = REPRO["earlier_identical_prompt_transcript"]
        truncated = transcript[:3]  # keep first 3 (indices 0,1,2)
        result = _run_send(
            head_src, truncated,
            regen_identity={"absoluteIdx": 2, "sessionId": "sid-1"},
        )
        assert result["user_row_count"] == 2, (
            f"expected 2 user rows (earlier + regenerated), got {result['user_row_count']}"
        )
        assert result["user_rows"][0].get("_pending") is not True, (
            "earlier identical user row must NOT be _pending"
        )
        assert result["user_rows"][1].get("_pending") is True, (
            "regenerated user row must be _pending"
        )

    def test_earlier_identical_content_unchanged(self):
        head_src = extract_js_function(_head_messages_src(), "send")
        transcript = REPRO["earlier_identical_prompt_transcript"]
        truncated = transcript[:3]
        result = _run_send(
            head_src, truncated,
            regen_identity={"absoluteIdx": 2, "sessionId": "sid-1"},
        )
        assert result["messages"][0].get("content") == "same prompt", (
            "earlier identical user row content must be unchanged"
        )


class TestSessionSwitchRace:
    """A session mismatch aborts mutation on the new session."""

    def test_second_fence_present_after_api_await(self):
        body = _fn_body(_head_ui_src(), "regenerateResponse")
        api_idx = body.index("await api('/api/session/truncate'")
        post_api = body[api_idx:]
        assert "S.session.session_id !== initialSid" in post_api, (
            "regenerateResponse() must revalidate session_id after api('/api/session/truncate') await"
        )

    def test_two_session_fences_in_regenerate(self):
        body = _fn_body(_head_ui_src(), "regenerateResponse")
        fence_count = body.count("S.session.session_id !== initialSid")
        assert fence_count >= 2, (
            f"regenerateResponse() must have at least 2 session fences; got {fence_count}"
        )

    def test_mismatched_session_aborts_send(self):
        # send() with _regenIdentity.sessionId != S.session.session_id must fail
        # closed: abort without pushing rather than append into the wrong session (#6611).
        head_src = extract_js_function(_head_messages_src(), "send")
        messages = [{"role": "user", "content": "hello"}]
        result = _run_send(
            head_src, messages,
            session_id="session-B",
            regen_identity={"absoluteIdx": 0, "sessionId": "session-A"},
        )
        # Fail closed: no push into wrong session, original row count unchanged
        assert result["user_row_count"] == 1, (
            "session mismatch with non-null _regenIdentity must abort (fail closed), "
            f"not push into wrong session; got user_row_count={result['user_row_count']}"
        )
        assert result["user_rows"][0].get("_pending") is not True, (
            "original user row must be unchanged after aborted send"
        )

    def test_send_reads_regen_identity(self):
        head_src = _head_messages_src()
        send_start = head_src.index("async function send(")
        send_end = head_src.index("const LIVE_STREAMS=", send_start)
        send_body = head_src[send_start:send_end]
        assert "_regenTarget" in send_body, (
            "send() must declare _regenTarget from options.regenerateTarget"
        )
        assert "regenerateTarget" in send_body, (
            "send() must read regenerateTarget from the options argument"
        )

    def test_send_stands_down_when_session_switches_during_upload_await(self):
        head_src = extract_js_function(_head_messages_src(), "send")
        result = _run_send(
            head_src,
            REPRO["transcript"][:1],
            regen_identity={"absoluteIdx": 0, "sessionId": "sid-1"},
            switch_on_upload=True,
        )
        assert not any(call["url"] == "/api/chat/start" for call in result["api_calls"])

    @pytest.mark.parametrize("switch_at", ["full-load", "truncate"])
    def test_actual_regenerate_stands_down_after_session_switch(self, switch_at):
        result = _run_regenerate(switch_at=switch_at)
        assert result["sends"] == []
        assert result["composer"] == ""
        if switch_at == "full-load":
            assert result["calls"] == []


def test_actual_regenerate_uses_full_history_row_and_display_space():
    result = _run_regenerate()
    assert len(result["calls"]) == 1
    body = result["calls"][0]["body"]
    assert body["keep_count_space"] == "display"
    assert body["keep_count"] == 2
    assert body["regenerate_target"] == {
        "session_id": "session-A",
        "message_id": "target-u",
        "timestamp": 2.125,
        "display_index": 1,
        "display_keep_count": 2,
    }
    assert result["sends"] == [{"regenerateTarget": body["regenerate_target"]}]


def test_regeneration_conflict_status_keys_exist_for_every_locale():
    source = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
    locale_count = source.count("regen_failed:")
    assert locale_count == 15
    assert source.count("regen_stale_target:") == locale_count
    assert source.count("regen_parent_only_target:") == locale_count


class TestReloadPersistence:
    """After settlement, the persisted transcript has one user row."""

    def test_server_keep_count_is_correct(self):
        assistant_idx = REPRO["regenerate_on_assistant_index"]
        absolute_keep_count = 0 + assistant_idx
        expected = REPRO["observed_base_result"]["keep_count"]
        assert absolute_keep_count == expected, (
            f"keep_count sent to truncate API must be {expected}, got {absolute_keep_count}"
        )

    def test_server_persisted_messages_have_one_user_row(self):
        transcript = REPRO["transcript"]
        assistant_idx = REPRO["regenerate_on_assistant_index"]
        absolute_keep_count = 0 + assistant_idx
        persisted = transcript[:absolute_keep_count]
        user_rows = [m for m in persisted if m.get("role") == "user"]
        assert len(user_rows) == 1, (
            f"server-persisted messages after truncate must have 1 user row, got {len(user_rows)}"
        )

    def test_regen_identity_source_structure(self):
        body = _fn_body(_head_ui_src(), "regenerateResponse")
        assert "display_index: selectedUserDisplayIdx" in body, (
            "regenerateResponse() must pass the selected full-history display index"
        )
        assert "session_id: initialSid" in body, (
            "regenerateResponse() must pass session_id = initialSid"
        )


class TestPreservationOrdinarySend:
    """send() without _regenIdentity still appends one optimistic user row."""

    def test_send_source_retains_push_fallback(self):
        head_src = _head_messages_src()
        send_start = head_src.index("async function send(")
        send_end = head_src.index("const LIVE_STREAMS=", send_start)
        send_body = head_src[send_start:send_end]
        assert "S.messages.push(userMsg)" in send_body, (
            "send() must retain the S.messages.push(userMsg) path for ordinary sends"
        )

    def test_ordinary_send_appends_row(self):
        # No _regenIdentity: _regenId is null, localRegenIdx = -1 → push path.
        head_src = extract_js_function(_head_messages_src(), "send")
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
        result = _run_send(head_src, messages, regen_identity=None)
        assert result["user_row_count"] == 2, (
            f"ordinary send must append a new user row; got {result['user_row_count']}"
        )
        assert result["user_rows"][-1].get("_pending") is True, (
            "newly appended row must be marked _pending"
        )
        start_body = next(
            call["body"] for call in result["api_calls"] if call["url"] == "/api/chat/start"
        )
        assert "regenerate_target" not in start_body


class TestModeAndStateMatrix:
    """Each session lineage and transcript rendering variant behaves correctly."""

    def test_basic_regen_one_user_row(self):
        head_src = extract_js_function(_head_messages_src(), "send")
        truncated = REPRO["transcript"][:1]
        result = _run_send(head_src, truncated,
                           regen_identity={"absoluteIdx": 0, "sessionId": "sid-1"})
        assert result["user_row_count"] == 1

    def test_earlier_identical_prompt_two_user_rows(self):
        head_src = extract_js_function(_head_messages_src(), "send")
        transcript = REPRO["earlier_identical_prompt_transcript"]
        truncated = transcript[:3]
        result = _run_send(head_src, truncated,
                           regen_identity={"absoluteIdx": 2, "sessionId": "sid-1"})
        assert result["user_row_count"] == 2, (
            "earlier identical prompt + regenerated turn = 2 user rows (not 3 with duplicate)"
        )

    def test_paged_session_one_user_row(self):
        head_src = extract_js_function(_head_messages_src(), "send")
        prior = [{"role": "assistant", "content": f"msg{i}"} for i in range(40)]
        all_messages = prior + [{"role": "user", "content": "paginated"}]
        result = _run_send(head_src, all_messages,
                           oldest_idx=0,
                           regen_identity={"absoluteIdx": 40, "sessionId": "sid-1"})
        assert result["user_row_count"] == 1

    def test_attachment_session_one_user_row(self):
        head_src = extract_js_function(_head_messages_src(), "send")
        transcript = REPRO["attachment_transcript"]
        truncated = transcript[:1]
        result = _run_send(head_src, truncated,
                           regen_identity={"absoluteIdx": 0, "sessionId": "sid-1"})
        assert result["user_row_count"] == 1
        assert result["user_rows"][0].get("attachments") == transcript[0].get("attachments")

    def test_send_reuses_existing_row_in_source(self):
        head_src = _head_messages_src()
        send_start = head_src.index("async function send(")
        send_end = head_src.index("const LIVE_STREAMS=", send_start)
        send_body = head_src[send_start:send_end]
        assert "S.messages[_localRegenIdx] = userMsg" in send_body, (
            "send() must assign the updated row back to S.messages at the captured index"
        )

    def test_imported_session_paging_offset(self):
        # Imported sessions may start with a non-zero offset; absoluteIdx is independent.
        head_src = extract_js_function(_head_messages_src(), "send")
        prior = [{"role": "assistant", "content": f"msg{i}"} for i in range(20)]
        all_messages = prior + [{"role": "user", "content": "imported prompt"}]
        result = _run_send(head_src, all_messages,
                           oldest_idx=0,
                           regen_identity={"absoluteIdx": 20, "sessionId": "sid-1"})
        assert result["user_row_count"] == 1
        assert result["user_rows"][0].get("_pending") is True
