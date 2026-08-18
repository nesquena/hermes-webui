import json
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def _js_function(src, name):
    markers = (f"async function {name}(", f"function {name}(")
    start = next((src.find(marker) for marker in markers if src.find(marker) >= 0), -1)
    if start < 0:
        raise AssertionError(f"{name}() not found")
    parens = 0
    brace = -1
    for idx in range(src.index("(", start), len(src)):
        if src[idx] == "(":
            parens += 1
        elif src[idx] == ")":
            parens -= 1
        elif src[idx] == "{" and parens == 0:
            brace = idx
            break
    if brace < 0:
        raise AssertionError(f"{name}() body not found")
    depth = 1
    for idx in range(brace + 1, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"{name}() body is unbalanced")


def _local_slash_send_probe():
    commands = read("static/commands.js")
    messages = read("static/messages.js")
    start = commands.index("const COMMANDS=[")
    end = commands.index("];", start) + 2
    registry = commands[start:end]
    names = sorted(set(re.findall(r"fn:([A-Za-z_$][\w$]*)", registry)))
    handlers = []
    for name in names:
        if name == "cmdClear":
            handlers.append(
                "function cmdClear(){handled.push('clear');return true;}"
            )
        else:
            handlers.append(f"function {name}(){{return true;}}")
    helpers = "\n".join(
        _js_function(messages, name)
        for name in (
            "_slashCommandMatch",
            "_echoSlashUserMessage",
            "_finishSlashCommand",
            "_runBuiltinSlashCommand",
            "_prepareSlashTurn",
            "send",
        )
    )
    script = f"""
const handled=[];
let directiveConsumed=false;
let _forcedSkillDirectivePending={{
  sessionId:'sid',
  promise:{{then(resolve){{directiveConsumed=true;resolve({{directive:'must not run'}});}}}},
}};
const input={{value:'/clear'}};
const S={{busy:false,pendingFiles:[],session:{{session_id:'sid',workspace:'',read_only:false}},messages:[]}};
const _pendingSelections=[];
const window={{}};
const document={{querySelector:()=>null}};
let _sendInProgress=false;
let _sendInProgressSid=null;
function $(id){{return id==='msg'?input:null;}}
function t(key){{return key;}}
function parseCommand(text){{
  if(!text.startsWith('/'))return null;
  const parts=text.slice(1).split(/\\s+/);
  return {{name:parts[0].toLowerCase(),args:parts.slice(1).join(' ').trim()}};
}}
function renderMessages(){{}}
function autoResize(){{}}
function hideCmdDropdown(){{}}
function _flushSelectionBlocksToComposer(){{}}
function _clearStaleBusyStateBeforeSend(){{}}
function isCompressionUiRunning(){{return false;}}
function _dismissHandoffHint(){{}}
function _chatPayloadModelState(){{return {{model:'model',model_provider:'provider'}};}}
{chr(10).join(handlers)}
{registry}
{helpers}
(async()=>{{
  await send();
  console.log(JSON.stringify({{handled,messages:S.messages,input:input.value,directiveConsumed}}));
}})().catch(error=>{{console.error(error.stack||error);process.exit(1);}});
"""
    node = shutil.which("node")
    if node is None:
        raise AssertionError("node is required for the send-path regression")
    result = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


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
    assert "api(`/api/skills/content?name=${encodeURIComponent(match.name)}`)" in src, \
        "cmdUse must fetch the canonical skill content after resolving the canonical skill name"
    assert "typeof detail.content==='string' ? detail.content.trim() : ''" in src, \
        "cmdUse must reject missing or non-string skill content"


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
    """A handled local slash command returns before awaiting a forced directive."""
    result = _local_slash_send_probe()
    assert result["handled"] == ["clear"]
    assert result["messages"] == []
    assert result["input"] == ""
    assert result["directiveConsumed"] is False


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
    assert "if(!_pending.sessionId||_pending.sessionId===activeSid){" in src, \
        "send() must only consume /use directives issued for the active session"
    assert "if(_forcedSkillDirectivePending===_pending)_forcedSkillDirectivePending = null;" in src, \
        "send() must not clear a newer pending directive created while awaiting"
    assert "[FORCED SKILL CONTEXT: ${_forcedSkillName}]" in src, \
        "send() must prepend deterministic forced-skill content before the user message"
