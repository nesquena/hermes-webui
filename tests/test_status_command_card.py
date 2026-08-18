"""Regression tests for issue #463: WebUI /status info card.

/status should be a client-handled slash command that renders a safe,
ephemeral assistant-style card from already-loaded session/profile/model data.
It must not round-trip through the agent or a status endpoint just to draw the
card.
"""
import json
import pathlib
import re
import shutil
import subprocess
import tempfile

import pytest


REPO_ROOT = pathlib.Path(__file__).parent.parent
COMMANDS_JS = (REPO_ROOT / "static" / "commands.js").read_text(encoding="utf-8")
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.index(marker)
    brace = src.index("{", start)
    depth = 0
    for idx in range(brace, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[start:idx + 1]
    raise AssertionError(f"Could not extract {name}()")


def _js_function(src: str, name: str) -> str:
    markers = (f"async function {name}(", f"function {name}(")
    start = next((src.find(marker) for marker in markers if src.find(marker) >= 0), -1)
    if start < 0:
        raise AssertionError(f"Could not extract {name}()")
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
        raise AssertionError(f"Could not extract {name}() body")
    depth = 1
    for idx in range(brace + 1, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"Could not close {name}() body")


def _run_node(source: str) -> str:
    if NODE is None:
        pytest.skip("node not on PATH")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=REPO_ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = pathlib.Path(script.name)
    try:
        result = subprocess.run(
            [NODE, str(script_path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _status_render_probe() -> dict:
    render_body = _function_body(UI_JS, "renderMessages")
    return json.loads(
        _run_node(
            f"""
const renderBody = {render_body!r};

function extractBlock(startMarker, endMarker) {{
  const start = renderBody.indexOf(startMarker);
  if (start === -1) throw new Error(`missing start marker: ${{startMarker}}`);
  const end = renderBody.indexOf(endMarker, start);
  if (end === -1) throw new Error(`missing end marker: ${{endMarker}}`);
  return renderBody.slice(start, end);
}}

function makeSeg() {{
  return {{
    html: '',
    classList: {{
      added: [],
      add(value) {{ this.added.push(value); }},
    }},
    insertAdjacentHTML(_where, html) {{ this.html += String(html || ''); }},
  }};
}}

const ordinaryBlock = extractBlock(
  "const hasVisibleBody=!!(String(content||'').trim()||filesHtml||recoveryHtml);",
  "_assistantTurnBlocks(currentAssistantTurn).appendChild(seg);"
);
const orderedBlock = extractBlock(
  "if(isLastTextPart&&statusHtml){{",
  "blocks.appendChild(orderedSeg);"
);

function runBaseOrdinary(opts) {{
  const seg = makeSeg();
  const content = opts.content || '';
  const filesHtml = opts.filesHtml || '';
  const statusHtml = opts.statusHtml || '';
  const recoveryHtml = opts.recoveryHtml || '';
  const bodyHtml = opts.bodyHtml || '';
  const footHtml = opts.footHtml || '';
  const thinkingText = opts.thinkingText || '';
  const window = {{ _showThinking: opts.showThinking !== false }};
  function isSimplifiedToolCalling() {{ return !!opts.simplified; }}
  const hasVisibleBody = !!(String(content || '').trim() || filesHtml || statusHtml || recoveryHtml);
  if (statusHtml) {{
    seg.insertAdjacentHTML('beforeend', statusHtml);
  }} else if (hasVisibleBody) {{
    seg.insertAdjacentHTML('beforeend', `${{filesHtml}}<div class="msg-body">${{bodyHtml}}</div>${{footHtml}}`);
  }} else if (!(thinkingText && window._showThinking !== false && !isSimplifiedToolCalling())) {{
    seg.classList.add('assistant-segment-anchor');
  }}
  return seg;
}}

function runHeadOrdinary(opts) {{
  const seg = makeSeg();
  const content = opts.content || '';
  const filesHtml = opts.filesHtml || '';
  const statusHtml = opts.statusHtml || '';
  const recoveryHtml = opts.recoveryHtml || '';
  const bodyHtml = opts.bodyHtml || '';
  const footHtml = opts.footHtml || '';
  const thinkingText = opts.thinkingText || '';
  const window = {{ _showThinking: opts.showThinking !== false }};
  function isSimplifiedToolCalling() {{ return !!opts.simplified; }}
  eval(ordinaryBlock);
  return seg;
}}

function runOrderedSpecial(opts) {{
  const orderedSeg = makeSeg();
  const isLastTextPart = opts.isLastTextPart ?? true;
  const statusHtml = opts.statusHtml || '';
  const filesHtml = opts.filesHtml || '';
  const footHtml = opts.footHtml || '';
  const partBodyHtml = opts.partBodyHtml || '';
  eval(orderedBlock);
  return orderedSeg;
}}

const ordinaryInput = {{
  content: 'Final report',
  bodyHtml: '<p>Final report</p>',
  statusHtml: '<status-card>limit</status-card>',
  filesHtml: '',
  footHtml: '<footer>meta</footer>',
  recoveryHtml: '',
}};
const ordinaryBase = runBaseOrdinary(ordinaryInput);
const ordinaryHead = runHeadOrdinary(ordinaryInput);
const statusOnly = runHeadOrdinary({{
  statusHtml: '<status-card>limit</status-card>',
  content: '',
  bodyHtml: '',
  filesHtml: '',
  footHtml: '<footer>meta</footer>',
  recoveryHtml: '',
}});
const ordinaryNoStatus = runHeadOrdinary({{
  statusHtml: '',
  content: 'Final report',
  bodyHtml: '<p>Final report</p>',
  filesHtml: '',
  footHtml: '<footer>meta</footer>',
  recoveryHtml: '',
}});
const filesOnlyWithStatus = runHeadOrdinary({{
  statusHtml: '<status-card>limit</status-card>',
  content: '',
  bodyHtml: '',
  filesHtml: '<div class="msg-files">artifact.txt</div>',
  footHtml: '',
  recoveryHtml: '',
}});
const orderedSpecial = runOrderedSpecial({{
  statusHtml: '<status-card>limit</status-card>',
  filesHtml: '',
  footHtml: '<footer>meta</footer>',
  partBodyHtml: '<p>Final report</p>',
}});

console.log(JSON.stringify({{
  ordinary: {{
    baseHtml: ordinaryBase.html,
    headHtml: ordinaryHead.html,
    headHasBody: ordinaryHead.html.includes('<div class="msg-body"><p>Final report</p></div>'),
    headStatusBeforeBody:
      ordinaryHead.html.indexOf('<status-card>limit</status-card>') <
      ordinaryHead.html.indexOf('<div class="msg-body"><p>Final report</p></div>'),
    baseDropsBody: !ordinaryBase.html.includes('<div class="msg-body"><p>Final report</p></div>'),
  }},
  statusOnly: {{
    html: statusOnly.html,
    isCardOnly: statusOnly.html === '<status-card>limit</status-card>',
  }},
  ordinaryNoStatus: {{
    html: ordinaryNoStatus.html,
    isBodyOnly:
      ordinaryNoStatus.html === '<div class="msg-body"><p>Final report</p></div><footer>meta</footer>',
  }},
  filesOnlyWithStatus: {{
    html: filesOnlyWithStatus.html,
    hasFiles: filesOnlyWithStatus.html.includes('<div class="msg-files">artifact.txt</div>'),
    statusBeforeFiles:
      filesOnlyWithStatus.html.indexOf('<status-card>limit</status-card>') <
      filesOnlyWithStatus.html.indexOf('<div class="msg-files">artifact.txt</div>'),
  }},
  orderedSpecial: {{
    html: orderedSpecial.html,
    statusCount: (orderedSpecial.html.match(/<status-card>/g) || []).length,
    bodyCount: (orderedSpecial.html.match(/<div class="msg-body">/g) || []).length,
  }},
}}));
"""
        )
    )


def _status_send_probe() -> dict:
    """Run the real send/planner path with a harmless status command stub."""
    command_start = COMMANDS_JS.index("const COMMANDS=[")
    command_end = COMMANDS_JS.index("];", command_start) + 2
    command_registry = COMMANDS_JS[command_start:command_end]
    handler_names = sorted(set(re.findall(r"fn:([A-Za-z_$][\w$]*)", command_registry)))
    handlers = []
    for name in handler_names:
        if name == "cmdStatus":
            handlers.append(
                "function cmdStatus(args){handled.push({name:'status',args:String(args||'')});return true;}"
            )
        else:
            handlers.append(f"function {name}(){{return true;}}")
    helpers = "\n".join(
        _js_function(MESSAGES_JS, name)
        for name in (
            "_slashCommandMatch",
            "_echoSlashUserMessage",
            "_finishSlashCommand",
            "_runBuiltinSlashCommand",
            "_prepareSlashTurn",
            "send",
        )
    )
    return json.loads(
        _run_node(
            f"""
const handled=[];
const startCalls=[];
const input={{value:'/status'}};
const S={{
  busy:false,
  pendingFiles:[],
  session:{{session_id:'sid',workspace:'workspace-id',read_only:false}},
  messages:[],
}};
const window={{}};
const document={{querySelector:()=>null}};
const _pendingSelections=[];
let _sendInProgress=false;
let _sendInProgressSid=null;
function $(id){{return id==='msg'?input:null;}}
function t(key){{return key;}}
function parseCommand(text){{
  if(!text.startsWith('/'))return null;
  const parts=text.slice(1).split(/\\s+/);
  return {{name:parts[0].toLowerCase(),args:parts.slice(1).join(' ').trim()}};
}}
function api(url){{startCalls.push(String(url));return Promise.resolve({{}});}}
function renderMessages(){{}}
function autoResize(){{}}
function hideCmdDropdown(){{}}
function _flushSelectionBlocksToComposer(){{}}
function _clearStaleBusyStateBeforeSend(){{}}
function isCompressionUiRunning(){{return false;}}
function _dismissHandoffHint(){{}}
function _chatPayloadModelState(){{return {{model:'model',model_provider:'provider'}};}}
{chr(10).join(handlers)}
{command_registry}
{helpers}
(async()=>{{
  await send();
  console.log(JSON.stringify({{
    handled,
    messages:S.messages,
    input:input.value,
    startCalls,
  }}));
}})().catch(error=>{{console.error(error.stack||error);process.exit(1);}});
"""
        )
    )


def test_status_command_is_registered_with_help_text():
    assert "{name:'status'" in COMMANDS_JS
    assert "desc:t('cmd_status')" in COMMANDS_JS
    assert "fn:cmdStatus" in COMMANDS_JS
    assert "cmd_status:'Show session info'" in I18N_JS


def test_status_command_uses_client_state_not_status_endpoint():
    body = _function_body(COMMANDS_JS, "cmdStatus")
    assert "/api/session/status" not in body
    assert "api(" not in body
    assert "S.session" in body
    assert "S.activeProfile" in COMMANDS_JS
    assert "model_provider" in COMMANDS_JS
    assert "last_usage" in COMMANDS_JS


def test_status_command_pushes_ephemeral_status_card_message():
    body = _function_body(COMMANDS_JS, "cmdStatus")
    assert "_statusCard" in body
    assert "_ephemeral:true" in body
    assert "renderMessages()" in body
    assert "_statusCardFromSession(S.session)" in body
    helper = _function_body(COMMANDS_JS, "_statusCardFromSession")
    assert "session_id" in helper
    assert "updated_at" in helper
    assert "message_count" in helper
    assert "active_stream_id" in helper


def test_status_card_renderer_escapes_all_dynamic_values_and_is_copyable():
    body = _function_body(UI_JS, "_statusCardHtml")
    assert "data-status-card" in body
    assert "data-copy-status-session" in body
    assert "onclick=\"copyStatusSessionId(this);event.stopPropagation()\"" in body
    assert "esc(card.title" in body
    assert "esc(card.subtitle" in body
    assert "esc(row.label" in body
    assert "esc(row.value" in body
    assert "esc(card.sessionId" in body
    assert "renderMd(" not in body, "Status card data should not be interpreted as markdown"


def test_render_messages_treats_status_card_as_visible_assistant_content():
    render_body = _function_body(UI_JS, "renderMessages")
    assert "m._statusCard" in render_body
    assert "_statusCardHtml(m._statusCard)" in render_body
    assert "statusHtml" in render_body


def test_render_messages_keeps_final_report_when_status_card_present():
    probe = _status_render_probe()
    ordinary = probe["ordinary"]
    assert ordinary["baseDropsBody"] is True
    assert ordinary["headHasBody"] is True
    assert ordinary["headStatusBeforeBody"] is True


def test_render_messages_keeps_status_only_card_boundary():
    probe = _status_render_probe()
    status_only = probe["statusOnly"]
    assert status_only["isCardOnly"] is True
    assert status_only["html"] == "<status-card>limit</status-card>"


def test_render_messages_keeps_body_only_boundary_without_status_card():
    probe = _status_render_probe()
    ordinary_no_status = probe["ordinaryNoStatus"]
    assert ordinary_no_status["isBodyOnly"] is True
    assert ordinary_no_status["html"] == "<div class=\"msg-body\"><p>Final report</p></div><footer>meta</footer>"


def test_render_messages_keeps_files_when_status_card_present():
    probe = _status_render_probe()
    files_only = probe["filesOnlyWithStatus"]
    assert files_only["hasFiles"] is True
    assert files_only["statusBeforeFiles"] is True


def test_ordered_transparent_branch_keeps_single_status_card_boundary():
    probe = _status_render_probe()
    ordered = probe["orderedSpecial"]
    assert ordered["statusCount"] == 1
    assert ordered["bodyCount"] == 1
    assert ordered["html"] == "<status-card>limit</status-card><div class=\"msg-body\"><p>Final report</p></div><footer>meta</footer>"


def test_status_card_styles_exist():
    assert ".status-card" in STYLE_CSS
    assert ".status-card-grid" in STYLE_CSS
    assert ".status-card-session-copy" in STYLE_CSS


def test_status_command_never_reaches_agent_send_path():
    result = _status_send_probe()
    assert result["handled"] == [{"name": "status", "args": ""}]
    assert [
        {key: message[key] for key in ("role", "content")}
        for message in result["messages"]
    ] == [{"role": "user", "content": "/status"}]
    assert result["input"] == ""
    assert result["startCalls"] == []
