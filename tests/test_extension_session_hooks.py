"""Functional tests for extension session-open hook + transcript renderer (PR #5508).

Existing static-shape tests verify _openSidebarSession exists but never
exercise the hook registration, preload-veto, transcript rendering, or
_preloadNotified bridge. This module uses Node.js to run the new functions
extracted from boot.js source.
"""

import json
import re
import subprocess
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
COMMANDS_JS = (REPO / "static" / "commands.js").read_text(encoding="utf-8")


def _extract_block(src, signature):
    start = src.find(signature)
    assert start >= 0, f"missing: {signature!r}"
    paren_close = src.index(")", start)
    brace = src.index("{", paren_close)
    depth = 0
    for i, ch in enumerate(src[brace:], brace):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(f"unterminated: {signature!r}")


DOM_SHIM = r"""
if (typeof global.window === 'undefined') global.window = {};
class El {
  constructor(n){this.children=[];this.attrs={};this.dataset={};
    this._html='';this._text='';this.tagName=n;}
  set className(v){this._cls=v;} get className(){return this._cls;}
  set innerHTML(v){this._html=v;} get innerHTML(){return this._html;}
  set textContent(v){this._text=v;} get textContent(){return this._text;}
  setAttribute(k,v){this.attrs[k]=v;
    if(k==='data-role')this.dataset.role=v;}
  appendChild(c){this.children.push(c);
    this._html = this.children.map(child => {
      let a = '';
      if (child._cls) a += ` class="${child._cls}"`;
      if (child.attrs['data-role']) a += ` data-role="${child.attrs['data-role']}"`;
      return `<${child.tagName}${a}>${child._html}</${child.tagName}>`;
    }).join('');
    return c;}
}
global.document={createElement:(n)=>new El(n)};
"""


def _run_in_tmp(tmp, body):
    f = tmp / "run.js"
    f.write_text(body)
    proc = subprocess.run(["node", str(f)], capture_output=True, text=True,
                          timeout=10, cwd=str(tmp))
    assert proc.returncode == 0, "node stderr: " + proc.stderr
    return proc.stdout


def _module_level_vars(src, names):
    """Pull top-level `var X = ...;` lines for each name in `names`."""
    out = []
    for name in names:
        m = re.search(rf"^\s*var\s+{re.escape(name)}\s*=\s*[^;]+;", src, re.M)
        if m:
            out.append(m.group(0).strip() + "\n")
        else:
            raise ValueError(f"Module-level var {name!r} not found")
    return "".join(out)


def _fn_body(src, fn_name, alias, dep_vars=None):
    """Extract a function and assign it to `var <alias>`, with any module-level
    vars it depends on."""
    result = ""
    if dep_vars:
        result += _module_level_vars(src, dep_vars)
    for sig in (f"window.{fn_name}=function", f"function {fn_name}("):
        idx = src.find(sig)
        if idx >= 0:
            eq = src.index("=", idx)
            if src[idx:].startswith("window."):
                start = src.index("function", eq)
            else:
                start = idx
            brace = src.index("{", start)
            depth = 0
            for i, ch in enumerate(src[brace:], brace):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        body = src[start:i + 1]
                        result += f"var {alias} = {body};\n"
                        return result
    raise ValueError(f"Couldn't find {fn_name}")


# ── boot.js: renderTranscript() ──────────────────────────────────────────────


class TestRenderTranscript:

    def test_tool_messages_skipped(self, tmp_path):
        body = (
            DOM_SHIM
            + _fn_body(BOOT_JS, "renderTranscript", "renderTranscript")
            + "global.window = {renderMd: (s)=>'<p>'+s+'</p>'};\n"
            + textwrap.dedent("""
                var c = document.createElement('div');
                renderTranscript(c, [
                  {"role":"user","content":"hi"},
                  {"role":"tool","content":"invisible"},
                  {"role":"assistant","content":"bye"}
                ], {});
                process.stdout.write(c.innerHTML);
            """)
        )
        out = _run_in_tmp(tmp_path, body)
        assert "hi" in out
        assert "bye" in out
        assert "invisible" not in out

    def test_array_content_concatenated(self, tmp_path):
        body = (
            DOM_SHIM
            + _fn_body(BOOT_JS, "renderTranscript", "renderTranscript")
            + "global.window = {renderMd: (s)=>'<p>'+s+'</p>'};\n"
            + textwrap.dedent("""
                var c = document.createElement('div');
                renderTranscript(c, [{"role":"user","content":[
                  {"type":"text","text":"Hello "},
                  {"type":"text","text":"world!"}
                ]}], {});
                process.stdout.write(c.innerHTML);
            """)
        )
        out = _run_in_tmp(tmp_path, body)
        assert "Hello" in out and "world!" in out
        assert "[object Object]" not in out

    def test_skip_empty(self, tmp_path):
        body = (
            DOM_SHIM
            + _fn_body(BOOT_JS, "renderTranscript", "renderTranscript")
            + "global.window = {renderMd: (s)=>'<p>'+s+'</p>'};\n"
            + textwrap.dedent("""
                var c = document.createElement('div');
                renderTranscript(c, [
                  {"role":"user","content":"x"},
                  {"role":"user","content":""}
                ], {skipEmpty:true});
                process.stdout.write(c.innerHTML);
            """)
        )
        out = _run_in_tmp(tmp_path, body)
        assert out.count("msg-row") == 1

    def test_dom_no_extra_inner_wrapper(self, tmp_path):
        body = (
            DOM_SHIM
            + _fn_body(BOOT_JS, "renderTranscript", "renderTranscript")
            + "global.window = {renderMd: (s)=>'<p>'+s+'</p>'};\n"
            + textwrap.dedent("""
                var c = document.createElement('div');
                renderTranscript(c, [{"role":"user","content":"x"}], {});
                process.stdout.write(c.innerHTML);
            """)
        )
        out = _run_in_tmp(tmp_path, body)
        assert 'class="msg-body"' in out
        assert "msg-body-inner" not in out

    def test_fallback_textcontent_when_renderMd_missing(self, tmp_path):
        body = (
            DOM_SHIM
            + _fn_body(BOOT_JS, "renderTranscript", "renderTranscript")
            + textwrap.dedent("""
                var c = document.createElement('div');
                delete window.renderMd;
                renderTranscript(c, [{
                  "role":"user",
                  "content":"<script>x</script>"
                }], {});
                process.stdout.write(c.innerHTML || c.textContent);
            """)
        )
        out = _run_in_tmp(tmp_path, body)
        assert "<script>x</script>" not in out


# ── boot.js: hook registration ───────────────────────────────────────────────


class TestHookRegistration:

    def test_register_dedupe_and_type_guard(self, tmp_path):
        body = (
            _fn_body(BOOT_JS, "registerHermesSessionOpenHandler",
                     "registerHermesSessionOpenHandler",
                     dep_vars=["_HERMES_SESSION_OPEN_HANDLERS"])
            + _fn_body(BOOT_JS, "_hermesNotifySessionOpen",
                       "_hermesNotifySessionOpen")
            + textwrap.dedent("""
                var calls = 0;
                function handler(sid, data, opts) { calls++; return null; }
                var r1 = registerHermesSessionOpenHandler(handler);
                var r2 = registerHermesSessionOpenHandler("nope");
                var r3 = registerHermesSessionOpenHandler(handler);
                _hermesNotifySessionOpen("s1", null, {preload:true});
                process.stdout.write(JSON.stringify({
                    r1:r1,r2:r2,r3:r3,calls:calls
                }));
            """)
        )
        out = _run_in_tmp(tmp_path, body)
        data = json.loads(out)
        assert data["r1"] is True
        assert data["r2"] is False
        assert data["r3"] is False
        assert data["calls"] == 1

    def test_preload_cancel_honored(self, tmp_path):
        body = (
            _fn_body(BOOT_JS, "registerHermesSessionOpenHandler",
                     "registerHermesSessionOpenHandler",
                     dep_vars=["_HERMES_SESSION_OPEN_HANDLERS"])
            + _fn_body(BOOT_JS, "_hermesNotifySessionOpen",
                       "_hermesNotifySessionOpen")
            + textwrap.dedent("""
                function blocker(){ return {cancel:true}; }
                registerHermesSessionOpenHandler(blocker);
                var r = _hermesNotifySessionOpen("s1", null, {preload:true});
                process.stdout.write(JSON.stringify(r));
            """)
        )
        out = _run_in_tmp(tmp_path, body)
        data = json.loads(out)
        assert data.get("cancel") is True

    def test_loaded_remains_non_cancellable(self, tmp_path):
        body = (
            _fn_body(BOOT_JS, "registerHermesSessionOpenHandler",
                     "registerHermesSessionOpenHandler",
                     dep_vars=["_HERMES_SESSION_OPEN_HANDLERS"])
            + _fn_body(BOOT_JS, "_hermesNotifySessionOpen",
                       "_hermesNotifySessionOpen")
            + textwrap.dedent("""
                var called = false;
                function h(){ called = true; return {cancel:true}; }
                registerHermesSessionOpenHandler(h);
                _hermesNotifySessionOpen("s2", {session_id:"s2"}, {loaded:true});
                process.stdout.write(JSON.stringify({called:called}));
            """)
        )
        out = _run_in_tmp(tmp_path, body)
        data = json.loads(out)
        assert data["called"] is True

    def test_each_handler_sees_one_event(self, tmp_path):
        body = (
            _fn_body(BOOT_JS, "registerHermesSessionOpenHandler",
                     "registerHermesSessionOpenHandler",
                     dep_vars=["_HERMES_SESSION_OPEN_HANDLERS"])
            + _fn_body(BOOT_JS, "_hermesNotifySessionOpen",
                       "_hermesNotifySessionOpen")
            + textwrap.dedent("""
                var events = [];
                function h1(sid,d,o){events.push({h:1,sid:sid,preload:!!o.preload});}
                function h2(sid,d,o){events.push({h:2,sid:sid,preload:!!o.preload});}
                registerHermesSessionOpenHandler(h1);
                registerHermesSessionOpenHandler(h2);
                _hermesNotifySessionOpen("target", null, {preload:true});
                process.stdout.write(JSON.stringify(events));
            """)
        )
        out = _run_in_tmp(tmp_path, body)
        data = json.loads(out)
        assert len(data) == 2
        assert all(ev["preload"] for ev in data)
        assert data[0]["h"] == 1 and data[1]["h"] == 2


# ── sessions.js: static shape (controls + integration points) ────────────────


def test_canonical_sid_resolved_before_preload_notification():
    canonical = _extract_block(SESSIONS_JS, "function _canonicalSessionLoadId(sid, opts)")
    body = _extract_block(SESSIONS_JS, "function loadSession(sid)")
    assert "_resolveSessionIdFromSidebarLineage" in canonical
    idx_resolve = body.index("_canonicalSessionLoadId")
    idx_preload = body.index("_hermesNotifySessionOpen")
    assert idx_resolve < idx_preload


def test_load_session_coordinator_honors_preload_veto_and_bridge_flag(tmp_path):
    body = (
        _extract_block(SESSIONS_JS, "function _canonicalSessionLoadId(sid, opts)")
        + "\n"
        + _extract_block(SESSIONS_JS, "function loadSession(sid)")
        + textwrap.dedent("""
            var S={session:null};
            var _activeSessionLoad=null;
            var _sessionLoadIntentGeneration=0;
            var starts=[];
            var notifications=[];
            function _resolveSessionIdFromSidebarLineage(sid){return sid==='alias'?'canonical':sid;}
            function _hermesNotifySessionOpen(sid,data,opts){
              notifications.push({sid:sid,preload:!!opts.preload});
              return {cancel:true};
            }
            function _startSessionLoad(sid,opts){starts.push({sid:sid,opts:opts});return Promise.resolve(sid);}
            function _sessionLoadNeedsFollowUp(){return false;}
            function _queueSessionLoadAfterActive(){throw new Error('unexpected queue');}

            loadSession('alias',{});
            loadSession('alias',{_preloadNotified:true});
            process.stdout.write(JSON.stringify({notifications:notifications,starts:starts}));
        """)
    )
    data = json.loads(_run_in_tmp(tmp_path, body))
    assert data["notifications"] == [{"sid": "canonical", "preload": True}]
    assert len(data["starts"]) == 1
    assert data["starts"][0]["sid"] == "canonical"


def test_retry_and_undo_internal_reload_bypasses_preload_veto(tmp_path):
    body = (
        _extract_block(SESSIONS_JS, "function _canonicalSessionLoadId(sid, opts)")
        + "\n"
        + _extract_block(SESSIONS_JS, "function loadSession(sid)")
        + "\n"
        + _extract_block(COMMANDS_JS, "async function cmdRetry()")
        + "\n"
        + _extract_block(COMMANDS_JS, "async function cmdUndo()")
        + textwrap.dedent("""
            var S={session:{session_id:'active',is_cli_session:false}};
            var _activeSessionLoad=null;
            var _sessionLoadIntentGeneration=0;
            var starts=[];
            var notifications=[];
            var mutations=[];
            var sends=0;
            var input={value:''};
            function _resolveSessionIdFromSidebarLineage(sid){return sid;}
            function _hermesNotifySessionOpen(sid,data,opts){
              notifications.push({sid:sid,preload:!!opts.preload});
              return {cancel:true};
            }
            function _startSessionLoad(sid,opts,intentGeneration){
              starts.push({sid:sid,opts:opts,intentGeneration:intentGeneration});
              return Promise.resolve(sid);
            }
            function _sessionLoadNeedsFollowUp(){return false;}
            function _queueSessionLoadAfterActive(){throw new Error('unexpected queue');}
            function _deliberateSessionModelPick(){return null;}
            function _reArmRecoveryPick(){}
            function autoResize(){}
            function send(){sends+=1;return Promise.resolve();}
            function showToast(){}
            function t(value){return value;}
            function $(id){return id==='msg'?input:null;}
            async function api(url){
              mutations.push(url);
              if(url==='/api/session/retry') return {last_user_text:'retry-user'};
              if(url==='/api/session/undo') return {removed_count:2};
              throw new Error('unexpected api '+url);
            }
            (async()=>{
              await cmdRetry();
              await cmdUndo();
              process.stdout.write(JSON.stringify({
                starts:starts,notifications:notifications,mutations:mutations,
                sends:sends,input:input.value
              }));
            })().catch(error=>{console.error(error.stack||error);process.exit(1);});
        """)
    )
    data = json.loads(_run_in_tmp(tmp_path, body))
    assert data["mutations"] == ["/api/session/retry", "/api/session/undo"]
    assert data["notifications"] == []
    assert len(data["starts"]) == 2
    assert [entry["opts"]["externalRefreshReason"] for entry in data["starts"]] == [
        "retry",
        "undo",
    ]
    assert all(entry["opts"]["skipExtHooks"] is True for entry in data["starts"])
    assert data["sends"] == 1


def test_preload_veto_only_on_preload_phase():
    body = _extract_block(BOOT_JS, "window._hermesNotifySessionOpen=function")
    assert "opts.preload" in body


def test_continuation_retry_stays_inside_execution_core():
    body = _extract_block(SESSIONS_JS, "async function _loadSessionOnce(sid)")
    idx_cont = body.index("continuationSid=")
    cont_branch = body[idx_cont:idx_cont + 400]
    assert "return _loadSessionOnce(continuationSid" in cont_branch
    assert "return loadSession(continuationSid" not in cont_branch


def test_bridge_flag_passed_by_sidebar_open():
    body = _extract_block(SESSIONS_JS, "async function _openSidebarSession(")
    assert "_preloadNotified:true" in body


# ── Regression: {cancel:true} must leave NO side-effect ─────────────────────


def test_cancel_does_not_close_mobile_sidebar():
    """A {cancel:true} handler must not close the sidebar.

    Regression: closeMobileSidebar() was called synchronously BEFORE
    _openSidebarSession()'s veto guard, so a cancel still closed it.
    Fix: the three early calls are gone; one now sits AFTER the guard.
    """
    # The only remaining closeMobileSidebar() inside _openSidebarSession
    # must appear AFTER the cancel guard, not before it.
    body = _extract_block(SESSIONS_JS, "async function _openSidebarSession(")
    idx_cancel = body.index("_preResult&&_preResult.cancel===true")
    idx_close = body.index("closeMobileSidebar()")
    assert idx_close > idx_cancel, (
        "closeMobileSidebar() must run AFTER the cancel guard"
    )


def test_no_early_closemobilesidebar_before_sidebar_open():
    """The three premature closeMobileSidebar() calls before _openSidebarSession
    must be removed — a {cancel:true} veto must not close the sidebar."""
    # Tap-to-open handler: the setTimeout callback that calls _openSidebarSession
    # must NOT contain a closeMobileSidebar() before the await.
    tap_block = _extract_block(SESSIONS_JS, "_tapTimer=setTimeout(async()=>{")
    # Find the _openSidebarSession call inside the tap handler.
    idx_open = tap_block.index("await _openSidebarSession(s)")
    before_open = tap_block[:idx_open]
    assert "closeMobileSidebar()" not in before_open, (
        "tap-to-open handler must not closeMobileSidebar() before veto"
    )

    # Child-session open handler must not contain closeMobileSidebar() at all.
    child_block = _extract_block(SESSIONS_JS, "const openChildSession=async(childSession)=>{")
    assert "closeMobileSidebar()" not in child_block, (
        "openChildSession handler must not contain closeMobileSidebar()"
    )

    # Lineage-segment handler must not contain closeMobileSidebar() at all.
    lineage_block = _extract_block(SESSIONS_JS, "row.onclick=async(e)=>{")
    assert "closeMobileSidebar()" not in lineage_block, (
        "lineage-segment handler must not contain closeMobileSidebar()"
    )


def test_cross_profile_retry_stays_inside_execution_core():
    """Cross-profile retry must not re-enter the preload coordinator."""
    body = _extract_block(SESSIONS_JS, "async function _loadSessionOnce(sid)")
    idx_profile = body.index("skipProfileResolve:true")
    profile_branch = body[max(0, idx_profile - 100):idx_profile + 200]
    assert "return _loadSessionOnce(sid" in profile_branch
    assert "return loadSession(sid" not in profile_branch
