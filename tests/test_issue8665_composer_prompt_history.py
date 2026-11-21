"""#8665: ArrowUp/ArrowDown in the composer recall prompts sent in this chat.

Drives the real helpers from static/boot.js in Node with a fake textarea.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def _helpers_src() -> str:
    start = BOOT_JS.index("let _promptHistory=null;")
    end = BOOT_JS.index("// Detect phone software keyboards", start)
    return BOOT_JS[start:end]


SCENARIO = r"""
let _imeComposing=false;
const S={session:{session_id:'s1'}, messages:[
  {role:'user', content:'first prompt'},
  {role:'assistant', content:'ok'},
  {role:'user', content:[{type:'text', text:'second\nprompt'}]},
  {role:'user', content:'second\nprompt'},
  {role:'user', content:'wake', _source:'delegation_wakeup'},
  {role:'user', content:'third'},
]};
function msgContent(m){let c=m.content||'';if(Array.isArray(c))c=c.filter(p=>p&&p.type==='text').map(p=>p.text||'').join('').trim();return String(c).trim();}
const el={value:'', selectionStart:0, selectionEnd:0,
  setSelectionRange(a,b){this.selectionStart=a;this.selectionEnd=b;}};
function key(k, extra={}){
  let prevented=false;
  const e={key:k, shiftKey:false, altKey:false, ctrlKey:false, metaKey:false, isComposing:false,
    preventDefault(){prevented=true;}, ...extra};
  const handled=_handlePromptHistoryKey(e, el);
  return {handled, prevented, value:el.value};
}
function caret(n){el.selectionStart=el.selectionEnd=n;}
const out={};
// Empty composer: Up walks back through sent prompts, skipping repeats and wake-ups.
out.up1=key('ArrowUp');
out.up2=key('ArrowUp');
// Up leaves the caret at the start of the recalled prompt.
out.caretAfterUp=el.selectionStart;
// Anywhere past the start (here the end of the last line) Up moves the caret natively.
caret(el.value.length);
out.upMultilineNotFirstLine=key('ArrowUp');
caret(0);
out.up3=key('ArrowUp');
out.upAtOldest=key('ArrowUp');
// Down only takes over at the very end; from the start it moves the caret natively.
out.downAtStart=key('ArrowDown');
caret(el.value.length);
// Down goes newer, and past the newest restores the draft ('').
out.down1=key('ArrowDown');
out.caretAfterDown=el.selectionStart;
caret(0);
out.downMidMultiline=key('ArrowDown');
caret(el.value.length);
out.down2=key('ArrowDown');
out.down3=key('ArrowDown');
out.downNotBrowsing=key('ArrowDown');
// A typed draft with the caret in the middle keeps normal arrows.
el.value='my draft'; caret(3);
out.upMidText=key('ArrowUp');
// Caret at the start of a draft starts browsing and Down brings the draft back.
caret(0);
out.upFromDraft=key('ArrowUp');
caret(el.value.length);
out.downBackToDraft=key('ArrowDown');
// Typing after recalling ends browsing: Down does nothing.
caret(0); key('ArrowUp'); el.value=el.value+' edited'; caret(el.value.length);
out.downAfterEdit=key('ArrowDown');
// Modifiers and IME composition are left alone.
el.value=''; caret(0);
out.shiftUp=key('ArrowUp', {shiftKey:true});
out.imeUp=key('ArrowUp', {isComposing:true});
// No sent prompts: nothing to recall.
S.messages=[]; _promptHistory=null;
out.upEmptyChat=key('ArrowUp');
console.log(JSON.stringify(out));
"""


def _run():
    src = _helpers_src() + "\n" + SCENARIO
    res = subprocess.run([NODE, "-e", src], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_up_and_down_walk_sent_prompts_and_restore_the_draft():
    out = _run()
    assert out["up1"] == {"handled": True, "prevented": True, "value": "third"}
    assert out["up2"]["value"] == "second\nprompt"
    assert out["caretAfterUp"] == 0
    assert out["upMultilineNotFirstLine"]["handled"] is False
    assert out["up3"]["value"] == "first prompt"
    assert out["upAtOldest"] == {"handled": True, "prevented": True, "value": "first prompt"}
    assert out["downAtStart"]["handled"] is False
    assert out["down1"]["value"] == "second\nprompt"
    assert out["caretAfterDown"] == len("second\nprompt")
    assert out["downMidMultiline"]["handled"] is False
    assert out["down2"]["value"] == "third"
    assert out["down3"] == {"handled": True, "prevented": True, "value": ""}
    assert out["downNotBrowsing"]["handled"] is False


def test_normal_editing_keeps_its_arrow_keys():
    out = _run()
    assert out["upMidText"]["handled"] is False
    assert out["upFromDraft"]["value"] == "third"
    assert out["downBackToDraft"]["value"] == "my draft"
    assert out["downAfterEdit"]["handled"] is False
    assert out["shiftUp"]["handled"] is False
    assert out["imeUp"]["handled"] is False
    assert out["upEmptyChat"]["handled"] is False


def test_history_runs_only_when_the_slash_dropdown_is_closed():
    start = BOOT_JS.index("$('msg').addEventListener('keydown',e=>{")
    block = BOOT_JS[start:start + 2500]
    dropdown = block.index("if(dropdownOpen){")
    history = block.index("_handlePromptHistoryKey(e, $('msg'))) return;")
    send = block.index("if(e.key==='Enter'){")
    assert dropdown < history < send


SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
COMMANDS_JS = (REPO_ROOT / "static" / "commands.js").read_text(encoding="utf-8")
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _pre_switch_draft_src() -> str:
    """The exact draft-text lines loadSession() runs before leaving a chat."""
    start = SESSIONS_JS.index("    const draftText = typeof _composerDraftValue")
    end = SESSIONS_JS.index("    const draftFiles = S.pendingFiles", start)
    return SESSIONS_JS[start:end]


def _cmd_undo_src() -> str:
    start = COMMANDS_JS.index("async function cmdUndo(){")
    end = COMMANDS_JS.index("async function undoLastExchange()", start)
    return COMMANDS_JS[start:end]


LIFECYCLE = r"""
let _imeComposing=false;
const pairs=n=>{const m=[];for(let i=1;i<=n;i++){m.push({role:'user',content:'p'+i});m.push({role:'assistant',content:'a'+i});}return m;};
const S={session:{session_id:'s1'}, messages:pairs(4)};
function msgContent(m){return String(m.content||'').trim();}
const el={value:'', selectionStart:0, selectionEnd:0,
  setSelectionRange(a,b){this.selectionStart=a;this.selectionEnd=b;}};
const $=()=>el;
function key(k){
  const e={key:k, shiftKey:false, altKey:false, ctrlKey:false, metaKey:false, isComposing:false, preventDefault(){}};
  return {handled:_handlePromptHistoryKey(e, el), value:el.value};
}
function caret(n){el.selectionStart=el.selectionEnd=n;}
const out={};
(async()=>{
  // 1. Switch chats while a recalled prompt is showing: the draft saved is the unsent text.
  el.value='unsent original draft'; caret(0);
  key('ArrowUp');
  out.shownBeforeSwitch=el.value;
  out.savedOnSwitch=(()=>{ PRE_SWITCH; return draftText; })();
  // Not browsing: the live text is saved as before.
  _promptHistory=null; el.value='plain draft';
  out.savedWhenNotBrowsing=(()=>{ PRE_SWITCH; return draftText; })();

  // 2. Undo shortens the transcript while browsing: no stale index, never 'undefined'.
  let len=4;
  const api=async(url)=>{ if(url.includes('/undo')){len-=1;return {removed_count:2};}
    return {session:{messages:pairs(len)}}; };
  const showToast=()=>{}, t=x=>x, renderMessages=()=>{};
  CMD_UNDO
  S.messages=pairs(4); _promptHistory=null; el.value='draft2'; caret(0);
  key('ArrowUp'); key('ArrowUp'); key('ArrowUp'); key('ArrowUp');
  out.atOldest=el.value;
  await cmdUndo(); await cmdUndo();
  caret(0);
  out.upAfterUndo=key('ArrowUp');
  caret(el.value.length);
  out.downAfterUndo=key('ArrowDown');
  out.downToDraft=key('ArrowDown');

  // 3. A real edit ends browsing, even if it ends on the same text.
  S.messages=pairs(2); _promptHistory=null; el.value='draft3'; caret(0);
  key('ArrowUp');
  const recalled=el.value;
  el.value=recalled+'x'; _endPromptHistory();
  el.value=recalled; _endPromptHistory();
  caret(el.value.length);
  out.downAfterEditBack=key('ArrowDown');
  console.log(JSON.stringify(out));
})();
"""


def _run_lifecycle():
    src = (_helpers_src() + "\n" + LIFECYCLE
           .replace("PRE_SWITCH", _pre_switch_draft_src())
           .replace("CMD_UNDO", _cmd_undo_src()))
    res = subprocess.run([NODE, "-e", src], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_switching_chats_while_recalling_saves_the_unsent_draft():
    out = _run_lifecycle()
    assert out["shownBeforeSwitch"] == "p4"
    assert out["savedOnSwitch"] == "unsent original draft"
    assert out["savedWhenNotBrowsing"] == "plain draft"


def test_clarify_lock_saves_the_unsent_draft_too():
    start = UI_JS.index("function lockComposerForClarify(")
    block = UI_JS[start:start + 1500]
    assert "_composerDraftValue(input)" in block


def test_undo_shortening_history_rebinds_instead_of_inserting_undefined():
    out = _run_lifecycle()
    assert out["atOldest"] == "p1"
    # p1 is still in the shorter list, so browsing restarts from the newest.
    assert out["upAfterUndo"] == {"handled": True, "value": "p2"}
    # Down past the newest still brings back the draft typed before browsing.
    assert out["downAfterUndo"] == {"handled": True, "value": "draft2"}
    assert out["downToDraft"]["handled"] is False


def test_a_real_edit_ends_browsing_even_back_to_the_same_text():
    out = _run_lifecycle()
    assert out["downAfterEditBack"]["handled"] is False
    start = BOOT_JS.index("$('msg').addEventListener('input',()=>{")
    assert "_endPromptHistory()" in BOOT_JS[start:start + 200]


MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
RESTORE_LINE = "if(currentSid&&currentSid!==sid&&typeof _restorePromptHistoryDraft==='function') _restorePromptHistoryDraft($('msg'));"


def _draft_persistence_src() -> str:
    """The real draft save + serializer from sessions.js."""
    start = SESSIONS_JS.index("// ── Composer draft persistence")
    end = SESSIONS_JS.index("// Restore composer draft from server onto #msg textarea.", start)
    return SESSIONS_JS[start:end]


def _binding_src() -> str:
    """The real goal-continuation binding writer/peek from messages.js."""
    start = MESSAGES_JS.index("function _normalizeGoalContinuationId(id)")
    end = MESSAGES_JS.index("function _bgTaskCompleteRingBufferAdd(", start)
    return MESSAGES_JS[start:end]


def _switch_save_src() -> str:
    """loadSession()'s real pre-switch lines, as one unedited slice: from the
    #8665 draft restore through the binding check and the awaited draft save."""
    load = SESSIONS_JS.index("async function loadSession(sid){")
    anchor = SESSIONS_JS.index("  // #8665: leaving a chat while a sent prompt is recalled", load)
    end = SESSIONS_JS.index(
        "      _handleComposerDraftPostFailure(error, currentSid, draftText, draftFiles);", anchor)
    return SESSIONS_JS[anchor:end] + "    }\n  }\n"


CONTINUATION = r"""
let _imeComposing=false;
const posts=[];
async function api(url, opts){ posts.push(JSON.parse(opts.body)); return {}; }
const el={value:'', selectionStart:0, selectionEnd:0, dataset:{},
  setSelectionRange(a,b){this.selectionStart=a;this.selectionEnd=b;}};
const $=()=>el;
var window={};
const S={session:{session_id:'s1'}, messages:[{role:'user',content:'latest'},{role:'assistant',content:'ok'}], pendingFiles:[]};
function msgContent(m){return String(m.content||'').trim();}
function key(k){
  const e={key:k, shiftKey:false, altKey:false, ctrlKey:false, metaKey:false, isComposing:false, preventDefault(){}};
  return _handlePromptHistoryKey(e, el);
}
const attachment={name:'a.txt', size:3, type:'text/plain', path:'/tmp/a.txt'};
async function leave(){
  const currentSid='s1', sid='s2';
  const _clearQueueCardDisplay=()=>{};
  SWITCH_SAVE
}
(async()=>{
  const out={};
  for (const name of ['plain', 'recalled']) {
    posts.length=0; _promptHistory=null; el.dataset={};
    el.value='unsent original draft'; el.selectionStart=el.selectionEnd=0;
    S.pendingFiles=[attachment];
    _setRestoredGoalContinuationDraft('cont-1', 'unsent original draft');
    if (name==='recalled') key('ArrowUp');
    out[name]={shown:el.value};
    await leave();
    out[name].post=posts[posts.length-1]||null;
    out[name].peekAfter=_peekRestoredGoalContinuationDraft();
  }
  // A genuinely edited draft still drops the token.
  posts.length=0; _promptHistory=null; el.dataset={};
  _setRestoredGoalContinuationDraft('cont-1', 'unsent original draft');
  el.value='edited draft'; S.pendingFiles=[];
  await leave();
  out.edited=posts[posts.length-1]||null;
  console.log(JSON.stringify(out));
})().catch(e=>{console.error(e&&e.stack||e); process.exit(1);});
"""


def _run_continuation():
    if "function _peekRestoredGoalContinuationDraft(" not in MESSAGES_JS:
        pytest.skip("goal-continuation drafts (#7855) not on this base")
    src = (_binding_src() + "\n" + _draft_persistence_src() + "\n" + _helpers_src() + "\n"
           + CONTINUATION.replace("SWITCH_SAVE", _switch_save_src()))
    res = subprocess.run([NODE, "-e", src], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_switching_while_recalling_keeps_a_restored_continuation_token():
    out = _run_continuation()
    assert out["plain"]["post"]["goal_continuation_id"] == "cont-1"
    assert out["recalled"]["shown"] == "latest"
    post = out["recalled"]["post"]
    assert post["text"] == "unsent original draft"
    assert post["goal_continuation_id"] == "cont-1"
    assert [f["name"] for f in post["files"]] == ["a.txt"]
    # The peek does not consume the binding.
    assert out["recalled"]["peekAfter"]["id"] == "cont-1"


def test_an_edited_draft_still_drops_the_continuation_token():
    out = _run_continuation()
    assert out["edited"]["text"] == "edited draft"
    assert out["edited"]["goal_continuation_id"] == ""


def test_leaving_the_chat_puts_the_unsent_draft_back():
    src = _helpers_src() + r"""
let _imeComposing=false;
const S={session:{session_id:'s1'}, messages:[{role:'user',content:'latest'}]};
function msgContent(m){return String(m.content||'').trim();}
const el={value:'mine', selectionStart:0, selectionEnd:0, setSelectionRange(){}};
const $=()=>el;
_handlePromptHistoryKey({key:'ArrowUp', shiftKey:false, altKey:false, ctrlKey:false, metaKey:false, isComposing:false, preventDefault(){}}, el);
const shown=el.value;
const currentSid='s1', sid='s2';
RESTORE
console.log(JSON.stringify({shown, after:el.value, browsing:_promptHistory!==null}));
""".replace("RESTORE", RESTORE_LINE)
    res = subprocess.run([NODE, "-e", src], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == {"shown": "latest", "after": "mine", "browsing": False}
    assert RESTORE_LINE in SESSIONS_JS


# Gate RED on 73306e7eb: in a real page, a recalled long one-line prompt wraps
# over several rows, and the second ArrowUp replaced it instead of moving the
# caret up a row. This drives the production textarea and keydown listener in
# Chromium: the arrows stay native until the caret is at the very start (Up)
# or very end (Down), with short and hard-newline neighbours on each side.
_BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]
_LONG = " ".join(f"word{i:04d}" for i in range(200))

_WRAP_SETUP = r"""
(long) => {
  S.session = {session_id: 'p8665-wrap'};
  S.messages = [
    {role: 'user', content: 'older short'},
    {role: 'assistant', content: 'ok'},
    {role: 'user', content: 'two\nlines'},
    {role: 'assistant', content: 'ok'},
    {role: 'user', content: long},
  ];
  _promptHistory = null;
  const el = $('msg');
  el.value = '';
  el.focus();
  el.setSelectionRange(0, 0);
}
"""

_STATE = "() => ({value: $('msg').value, caret: $('msg').selectionStart})"


def test_wrapped_recalled_prompt_keeps_native_arrows_in_a_real_textarea():
    pw = pytest.importorskip("playwright.sync_api")
    from tests._pytest_port import BASE

    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=_BROWSER_ARGS)
        context = browser.new_context(viewport={"width": 1400, "height": 900})
        page = context.new_page()
        page.goto(BASE + "/", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => typeof S !== 'undefined' && S._bootReady === true", timeout=15000
        )
        page.wait_for_function(
            "() => typeof _handlePromptHistoryKey === 'function'", timeout=15000
        )

        def press(k):
            page.keyboard.press(k)
            return page.evaluate(_STATE)

        page.evaluate(_WRAP_SETUP, _LONG)
        steps = {"newest": press("ArrowUp")}
        # Caret to the end of the long prompt, like the gate's probe.
        page.evaluate("() => { const el=$('msg'); el.setSelectionRange(el.value.length, el.value.length); }")
        up_rows = []
        for _ in range(60):
            s = press("ArrowUp")
            if s["value"] != _LONG or s["caret"] == 0:
                up_rows.append(s)
                break
            up_rows.append(s)
        steps["older_hard_newline"] = press("ArrowUp")
        steps["older_short"] = press("ArrowUp")
        steps["down_from_start_of_short"] = press("ArrowDown")
        steps["down_to_hard_newline"] = press("ArrowDown")
        steps["down_to_long"] = press("ArrowDown")
        page.evaluate("() => $('msg').setSelectionRange(0, 0)")
        down_rows = []
        for _ in range(60):
            s = press("ArrowDown")
            down_rows.append(s)
            if s["value"] != _LONG or s["caret"] == len(_LONG):
                break
        steps["past_newest"] = press("ArrowDown")
        context.close()
        browser.close()

    assert steps["newest"] == {"value": _LONG, "caret": 0}
    # Several native moves up through the wrapped rows, text untouched.
    assert len(up_rows) >= 3, up_rows
    assert all(s["value"] == _LONG for s in up_rows), up_rows
    assert 0 < up_rows[0]["caret"] < len(_LONG), up_rows
    assert up_rows[-1]["caret"] == 0
    assert steps["older_hard_newline"] == {"value": "two\nlines", "caret": 0}
    assert steps["older_short"] == {"value": "older short", "caret": 0}
    # Down from the start of a one-row prompt just moves the caret to its end.
    assert steps["down_from_start_of_short"]["value"] == "older short"
    assert steps["down_to_hard_newline"] == {"value": "two\nlines", "caret": len("two\nlines")}
    assert steps["down_to_long"] == {"value": _LONG, "caret": len(_LONG)}
    # From the start of the long prompt, Down walks the wrapped rows natively.
    assert len(down_rows) >= 3, down_rows
    assert all(s["value"] == _LONG for s in down_rows), down_rows
    assert 0 < down_rows[0]["caret"] < len(_LONG), down_rows
    assert down_rows[-1]["caret"] == len(_LONG)
    assert steps["past_newest"]["value"] == ""
