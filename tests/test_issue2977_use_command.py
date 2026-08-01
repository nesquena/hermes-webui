from pathlib import Path
import json
import shutil
import subprocess
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_use_entry_in_commands_array():
    src = read("static/commands.js")
    assert "{name:'use'," in src, "COMMANDS must contain a {name:'use', ...} entry"


def test_use_entry_precedes_stop_entry():
    src = read("static/commands.js")
    use_pos = src.index("{name:'use',")
    stop_pos = src.index("{name:'stop',")
    assert use_pos < stop_pos, "/use must be registered before /stop in COMMANDS"


def test_cmdUse_function_defined():
    src = read("static/commands.js")
    assert "async function cmdUse(args)" in src, "cmdUse function must be defined"


def test_forced_skill_directive_declared():
    src = read("static/commands.js")
    assert "let _forcedSkillDirectivePending=null;" in src, "_forcedSkillDirectivePending must be declared at module scope"


def test_forced_skill_directive_set_in_cmdUse():
    src = read("static/commands.js")
    assert "pending.promise = new Promise" in src, "cmdUse must create a pending Promise"
    assert "_forcedSkillDirectivePending = pending;" in src, "cmdUse must publish the pending directive before awaiting"
    assert "resolve({name:match.name,directive,content:skillContent});" in src, \
        "cmdUse must resolve the pending payload with skill name, directive, and fetched content"


def test_use_entry_has_noEcho():
    src = read("static/commands.js")
    # Extract the /use entry line and check noEcho:true is present
    idx = src.index("{name:'use',")
    line_end = src.index("}", idx)
    entry = src[idx:line_end + 1]
    assert "noEcho:true" in entry, "/use entry must have noEcho:true"


def test_use_entry_has_subArgs_skills():
    src = read("static/commands.js")
    idx = src.index("{name:'use',")
    line_end = src.index("}", idx)
    entry = src[idx:line_end + 1]
    assert "subArgs:'skills'" in entry, "/use entry must have subArgs:'skills' for autocomplete"


def test_directive_consumed_at_injection_site():
    """_forcedSkillDirectivePending is cleared at the consume site, not in finally."""
    src = read("static/messages.js")
    finally_part = src.split("finally")[1] if "finally" in src else ""
    assert "_forcedSkillDirectivePending = null;" not in finally_part, \
        "_forcedSkillDirectivePending must NOT be cleared in the finally block"
    assert "const _directivePayload = await _pending.promise;" in src, \
        "consume site must await the pending promise"
    assert "_forcedSkillDirectivePending = null;" in src, \
        "_forcedSkillDirectivePending must be cleared somewhere in messages.js"


def test_directive_injection_before_empty_guard():
    src = read("static/messages.js")
    inject_pos = src.index("_forcedSkillDirectivePending")
    guard_pos = src.index("if(!msgText){setComposerStatus('Nothing to send');return;}")
    assert inject_pos < guard_pos, "directive injection must appear before the if(!msgText) guard"


def test_directive_text_uses_match_name():
    src = read("static/commands.js")
    assert "match.name" in src, "directive must use match.name (canonical casing), not raw user input"
    assert "[USER OVERRIDE] You MUST follow the skill '" in src, "directive text must match the specified format"
    assert "content provided below" in src, "directive must reference the injected skill content"


def test_use_fetches_canonical_skill_content():
    src = read("static/commands.js")
    assert "const detailUrl = `/api/skills/content?name=${encodeURIComponent(match.name)}${sessionId?`&session_id=${encodeURIComponent(sessionId)}`:''}`;" in src, \
        "cmdUse must fetch the canonical skill content after resolving the canonical skill name"
    assert "typeof detail.content==='string' ? detail.content.trim() : ''" in src, \
        "cmdUse must reject missing or non-string skill content"
    assert "const sessionId = String(pending.sessionId||'').trim();" in src
    assert "const cancelPending = () =>" in src


def test_pending_promise_set_synchronously():
    """_forcedSkillDirectivePending must be set before the first await in cmdUse."""
    src = read("static/commands.js")
    fn_start = src.index("async function cmdUse(args)")
    fn_body = src[fn_start:]
    pending_pos = fn_body.index("_forcedSkillDirectivePending = pending;")
    first_await = fn_body.index("await ")
    assert pending_pos < first_await, \
        "_forcedSkillDirectivePending must be set before the first await to close the race window"


def test_directive_survives_local_slash_commands():
    """The consume block must appear after the slash-command early-return, not before."""
    src = read("static/messages.js")
    early_return = src.index("autoResize();hideCmdDropdown();return;")
    consume = src.index("_forcedSkillDirectivePending")
    assert early_return < consume, \
        "slash-command early-return must precede the directive consume block"


def test_directive_pending_captures_session_id():
    src = read("static/commands.js")
    assert "const pending = {sessionId:S.session&&S.session.session_id||null,promise:null};" in src, \
        "cmdUse must capture the session where /use was issued"
    assert "const isCurrentSession = () => !pending.sessionId || (S.session&&S.session.session_id)===pending.sessionId;" in src, \
        "async /use completion must avoid writing status messages into a different session"


def test_directive_only_consumed_by_matching_session():
    src = read("static/messages.js")
    assert "const _pending=_forcedSkillDirectivePending;" in src, \
        "send() must snapshot the pending directive before awaiting it"
    assert "if(_pending.sessionId && _pending.sessionId!==activeSid){" in src, \
        "send() must clear /use directives issued for a different session"
    assert "if(_forcedSkillDirectivePending===_pending)_forcedSkillDirectivePending = null;" in src, \
        "send() must not clear a newer pending directive created while awaiting"
    assert "[FORCED SKILL CONTEXT: ${_forcedSkillName}]" in src, \
        "send() must prepend deterministic forced-skill content before the user message"


def test_send_payload_keeps_session_context_atomic_across_awaits():
    src = read("static/messages.js")
    assert "function _sendSessionSnapshot(sid)" in src
    assert "let _sendInProgressContext = null;" in src
    assert "uploaded=await uploadPendingFiles({files:_submittedFiles, sessionId:activeSid" in src
    assert src.count("if(!_sendSessionSnapshot(activeSid)){_clearStaleSend(activeSid);return;}") >= 2
    assert "session_id:_chatSession.session_id" in src
    assert "const _modelState=_chatPayloadModelState();" in src
    assert "model:_modelState.model" in src
    assert "model_provider:_modelState.model_provider" in src
    assert "workspace:_chatSession.workspace" in src
    assert "profile:_chatSession.profile" in src
    assert "..._chatPayloadModelState()," in src
    assert "postStartData = startData;" in src
    assert "if(!_ownerIsCurrent) return;" in src


def test_accepted_chat_start_keeps_owner_until_stream_attach():
    src = read("static/messages.js")
    start = src.index("const startData=await api('/api/chat/start'")
    accepted = src.index("postStartData = startData;", start)
    attach = src.index("attachLiveStream(activeSid, streamId, uploadedNames);", accepted)
    block = src[accepted:attach]
    assert "staleError.code='SESSION_CHANGED'" not in block
    assert "if(!INFLIGHT[activeSid])" in block
    assert "markInflight(activeSid, streamId);" in block
    assert "_ownerIsCurrent" in block


def test_queued_drain_recomputes_model_provider_with_shared_authority():
    src = read("static/ui.js")
    assert "function _applyQueuedSessionModelState(next)" in src
    drain_start = src.index("const _queuedModelState=_applyQueuedSessionModelState(next);")
    drain = src[drain_start:src.index("autoResize();", drain_start)]
    assert "_applyQueuedSessionModelState(next)" in drain
    assert "S.session.model_provider=_queuedModelState.model_provider||null" in drain
    assert "S.session.model_provider=next.model_provider" not in drain


def test_queued_drain_preserves_model_provider_pair_behaviorally():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    src = read("static/ui.js")
    start = src.index("function _applyQueuedSessionModelState(next)")
    end = src.index("// ── Queue chip display", start)
    helper = src[start:end]
    harness = textwrap.dedent(
        """
        const S = {session: {model: 'old-model', model_provider: 'old-provider'}};
        function _chatPayloadModelState() {
          return {model: S.session.model, model_provider: S.session.model_provider};
        }
        %(helper)s
        const state = _applyQueuedSessionModelState({
          model: 'queued-model', model_provider: 'queued-provider'
        });
        console.log(JSON.stringify({state, session: S.session}));
        """
    ) % {"helper": helper}
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["state"] == {"model": "queued-model", "model_provider": "queued-provider"}
    assert result["session"] == {"model": "queued-model", "model_provider": "queued-provider"}


def test_upload_stops_owner_work_after_session_switch():
    src = read("static/ui.js")
    assert "if(!_uploadPendingFilesCurrentSession(sessionId))break;" in src
