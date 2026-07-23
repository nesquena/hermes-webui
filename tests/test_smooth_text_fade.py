import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONFIG_PY = (REPO / "api" / "config.py").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")

FADE_SETTING = "fade_text_effect"
FADE_CHECKBOX_ID = "settingsFadeTextEffect"
FADE_RUNTIME_FLAG = "window._fadeTextEffect"
FADE_LABEL_KEY = "settings_label_fade_text_effect"
FADE_DESC_KEY = "settings_desc_fade_text_effect"


def function_block(src: str, name: str) -> str:
    marker = re.search(rf"(^|\n)\s*(?:async\s+)?function\s+{re.escape(name)}\(", src)
    assert marker is not None, f"{name}() not found"
    start = marker.start()
    brace = src.find("{", marker.end())
    assert brace != -1, f"{name}() opening brace not found"

    depth = 0
    in_string = None
    escape = False
    for i in range(brace, len(src)):
        ch = src[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == in_string:
                in_string = None
            continue
        if ch in "'`\"":
            in_string = ch
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"{name}() closing brace not found")


def assert_contains_all(src: str, snippets: list[str]) -> None:
    for snippet in snippets:
        assert snippet in src


def slice_between(src: str, start_anchor: str, end_anchor: str) -> str:
    start = src.find(start_anchor)
    assert start != -1, f"start anchor not found: {start_anchor!r}"
    end = src.find(end_anchor, start + len(start_anchor))
    assert end != -1, f"end anchor not found after {start_anchor!r}: {end_anchor!r}"
    return src[start:end]


def fade_helper_script(performance_stub: str = "{_t:0,now(){return this._t;}}") -> str:
    helpers = "\n".join(
        function_block(MESSAGES_JS, name)
        for name in [
            "_streamFadeWordCountOf",
            "_streamFadePauseAfter",
            "_resetStreamFadeState",
            "_streamFadeNextText",
        ]
    )
    return f"""
let _streamFadeVisibleText='';
let _streamFadeLastTickMs=0;
let _streamFadeWordCarry=0;
let _streamFadeStartedAt=0;
let _streamFadeLastTargetWords=0;
let _streamFadeLastArrivalMs=0;
let _streamFadeArrivalWps=0;
let _streamFadeLatestAnimationEndAt=0;
let _streamFadeVisibleWords=0;
let _streamFadeHoldUntilMs=0;
let _streamFadeCurrentMs=620;
let _streamFadeDomText='';
const _STREAM_FADE_MS=620;
const _STREAM_FADE_MAX_MS=900;
const _STREAM_FADE_DONE_MAX_MS=1000;
const _STREAM_FADE_DONE_DRAIN_MAX_MS=1400;
const performance={performance_stub};
{helpers}
"""


def run_node(script: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["node", "-e", script],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result


def test_fade_text_effect_setting_is_wired_through_backend_and_startup():
    bool_keys = CONFIG_PY[CONFIG_PY.index("_SETTINGS_BOOL_KEYS") : CONFIG_PY.index("# Language codes")]
    assert f'"{FADE_SETTING}": False' in CONFIG_PY
    assert f'"{FADE_SETTING}"' in bool_keys
    assert f"{FADE_RUNTIME_FLAG}=!!s.{FADE_SETTING}" in BOOT_JS
    assert f"{FADE_RUNTIME_FLAG}=false" in BOOT_JS


def test_preferences_ui_exposes_and_saves_fade_text_effect():
    assert f'id="{FADE_CHECKBOX_ID}"' in INDEX_HTML
    assert f'data-i18n="{FADE_LABEL_KEY}"' in INDEX_HTML
    assert f'data-i18n="{FADE_DESC_KEY}"' in INDEX_HTML
    assert FADE_LABEL_KEY in I18N_JS
    assert FADE_DESC_KEY in I18N_JS

    payload_block = function_block(PANELS_JS, "_preferencesPayloadFromUi")
    assert_contains_all(payload_block, [f"$('{FADE_CHECKBOX_ID}')", f"payload.{FADE_SETTING}="])

    load_block = function_block(PANELS_JS, "loadSettingsPanel")
    fade_load = load_block[load_block.index(f"$('{FADE_CHECKBOX_ID}')") :]
    assert_contains_all(
        fade_load[:700],
        [f"settings.{FADE_SETTING}", FADE_RUNTIME_FLAG, "addEventListener('change',_schedulePreferencesAutosave"],
    )

    autosave_block = function_block(PANELS_JS, "_autosavePreferencesSettings")
    assert_contains_all(autosave_block, [FADE_SETTING, f"{FADE_RUNTIME_FLAG}=!!payload.{FADE_SETTING}"])

    save_block = function_block(PANELS_JS, "saveSettings")
    assert_contains_all(save_block, [FADE_CHECKBOX_ID, f"body.{FADE_SETTING}", "fadeTextEffect"])

    apply_block = function_block(PANELS_JS, "_applySavedSettingsUi")
    assert_contains_all(apply_block, ["fadeTextEffect", f"{FADE_RUNTIME_FLAG}=!!fadeTextEffect"])


def test_stream_fade_uses_incremental_renderer_without_changing_default_path():
    # _scheduleRender is deeply nested inside attachLiveStream; the simple
    # brace-counting function_block parser can't handle template literals
    # with ${...} that contain braces.  Use the full file for assertions
    # instead — the checked strings are unique enough.
    assert re.search(r"function\s+_scheduleRender\(", MESSAGES_JS)
    render_block = function_block(MESSAGES_JS, "_renderStreamingFadeMarkdown")
    renderer_block = function_block(MESSAGES_JS, "_streamFadeRenderer")
    cleanup_block = function_block(MESSAGES_JS, "_streamFadeBindCleanup")

    assert_contains_all(
        MESSAGES_JS,
        [
            "_renderStreamingFadeMarkdown(displayText)",
            "_smdWrite(displayText)",
            "?33:66",
        ],
    )
    assert_contains_all(
        render_block,
        [
            "_streamFadeNextText(displayText)",
            "if(!next.changed) return next.caughtUp",
            "if(!_shouldUseTransparentStreamFade())",
            "_smdNewParser(assistantBody,true)",
            "_smdWrite(next.text,true)",
            "_sanitizeSmdLinks(assistantBody)",
            "assistantBody.appendChild(document.createTextNode(delta))",
            "_streamFadeDomText=String(next.text||'')",
            "stream-fade-active",
        ],
    )
    assert render_block.index("_smdWrite(next.text,true)") < render_block.index(
        "assistantBody.appendChild(document.createTextNode(delta))"
    )
    assert "_streamFadeAppendText(assistantBody,delta)" not in render_block
    assert "_streamFadeBindCleanup(assistantBody)" not in render_block
    append_block = function_block(MESSAGES_JS, "_streamFadeAppendText")
    assert_contains_all(
        append_block,
        [
            "document.createDocumentFragment()",
            "span.className='stream-fade-word is-new'",
            "el.appendChild(frag)",
            "_streamFadeLatestAnimationEndAt",
        ],
    )
    assert_contains_all(
        renderer_block,
        [
            "span.className='stream-fade-word is-new'",
            "_streamFadeReduceMotionEnabled()",
            "const appendStartedAt=performance.now()",
            "--stream-fade-ms",
            "renderer.set_attr",
            "data-blocked-scheme",
            "_streamFadeLatestAnimationEndAt",
        ],
    )
    assert_contains_all(
        cleanup_block,
        ["animationend", "span.replaceWith(document.createTextNode"],
    )
    assert "_wrapStreamingFadeWords" not in MESSAGES_JS
    assert "animationDelay" not in renderer_block
    assert "_STREAM_FADE_STAGGER_MS" not in MESSAGES_JS
    assert "_streamFadeAppendOffset" not in MESSAGES_JS


def test_stream_fade_appends_new_spans_without_replacing_existing_nodes():
    script = (
        function_block(MESSAGES_JS, "_streamFadeAppendText")
        + r"""
const _STREAM_FADE_MS=620;
let _streamFadeLatestAnimationEndAt=0;
let _streamFadeCurrentMs=620;
const performance={_t:0,now(){return this._t;}};
function _streamFadeReduceMotionEnabled(){ return false; }
class FakeNode{
  constructor(type,text=''){
    this.type=type;
    this.children=[];
    this.className='';
    this.textContent=text;
    this.style={values:{},setProperty:(name,value)=>{this.style.values[name]=value;}};
  }
  appendChild(child){
    if(child&&child.type==='fragment'){
      child.children.forEach(n=>this.children.push(n));
    }else{
      this.children.push(child);
    }
    return child;
  }
}
global.document={
  createDocumentFragment(){ return new FakeNode('fragment'); },
  createTextNode(text){ return new FakeNode('text',String(text)); },
  createElement(tag){ const node=new FakeNode(tag); node.tagName=String(tag).toUpperCase(); return node; },
};
const body=new FakeNode('div');
_streamFadeAppendText(body,'alpha beta ');
const firstSpan=body.children.find(node=>node.className==='stream-fade-word is-new');
if(!firstSpan) throw new Error('missing first fade span');
_streamFadeAppendText(body,'gamma');
if(body.children.find(node=>node.className==='stream-fade-word is-new')!==firstSpan){
  throw new Error('first span was replaced');
}
const spans=body.children.filter(node=>node.className==='stream-fade-word is-new');
if(spans.length!==3) throw new Error(`expected three animated spans, got ${spans.length}`);
if(spans.map(node=>node.textContent).join('|')!=='alpha|beta|gamma'){
  throw new Error(`wrong span text: ${spans.map(node=>node.textContent).join('|')}`);
}
"""
    )
    run_node(script)


def test_transparent_anchor_prose_uses_fade_renderer_when_enabled():
    anchor_block = function_block(MESSAGES_JS, "_anchorProseIncrementalNode")
    predicate_block = function_block(MESSAGES_JS, "_shouldUseLiveProseFade")
    assert_contains_all(
        anchor_block,
        [
            "const fade=typeof _shouldUseLiveProseFade==='function'&&_shouldUseLiveProseFade()",
            "if(st && st.fade!==fade) st=null",
            "if(body.classList) body.classList.toggle('stream-fade-active',fade)",
            "const baseRenderer=fade?_streamFadeRenderer(body):_safeSmdRenderer(body)",
            "st={node,parser:window.smd.parser(renderer),writtenText:'',fade}",
            "const body=st.node&&st.node.querySelector&&st.node.querySelector('.msg-body')",
        ],
    )
    assert_contains_all(
        predicate_block,
        [
            "!_streamFadeReduceMotionEnabled()",
            "_shouldUseStreamFade()",
            "_shouldUseTransparentStreamFade()",
        ],
    )
    assert "function _shouldUseTransparentStreamFade()" in MESSAGES_JS
    assert "typeof isTransparentStream==='function'&&isTransparentStream()" in MESSAGES_JS


def test_reduced_motion_disables_live_prose_fade_predicate():
    script = (
        "\n".join(
            function_block(MESSAGES_JS, name)
            for name in [
                "_shouldUseStreamFade",
                "_shouldUseTransparentStreamFade",
                "_streamFadeReduceMotionEnabled",
                "_shouldUseLiveProseFade",
            ]
        )
        + r"""
let _streamFadeReduceMotionMql=null;
let _streamFadeReduceMotion=false;
let _streamFadeReduceMotionOnChange=null;
let transparent=true;
let reduceMotion=true;
global.window={
  _fadeTextEffect:true,
  matchMedia(){
    return {
      get matches(){ return reduceMotion; },
      addEventListener(){},
      removeEventListener(){},
    };
  },
};
function isTransparentStream(){ return transparent; }
if(_shouldUseLiveProseFade()) throw new Error('reduced motion allowed live prose fade');
_streamFadeReduceMotionMql=null;
reduceMotion=false;
window._fadeTextEffect=false;
if(!_shouldUseLiveProseFade()) throw new Error('transparent stream fade should work when motion is allowed');
_streamFadeReduceMotionMql=null;
transparent=false;
window._fadeTextEffect=true;
if(!_shouldUseLiveProseFade()) throw new Error('regular fade preference should work when motion is allowed');
"""
    )
    run_node(script)


def test_transparent_stream_hidden_body_appends_plain_text_only():
    script = (
        function_block(MESSAGES_JS, "_renderStreamingFadeMarkdown")
        + r"""
let _streamFadeDomText='';
let _smdParser=null;
let _smdReconnect=false;
let parserEnded=false;
function _streamFadeNextText(){ return {changed:true,caughtUp:false,text:'alpha beta'}; }
function _shouldUseTransparentStreamFade(){ return true; }
function _smdEndParser(){ parserEnded=true; }
const assistantBody={
  textContent:'',
  innerHTML:'',
  children:[],
  classList:{added:[],add(name){ this.added.push(name); }},
  appendChild(node){
    this.children.push(node);
    this.textContent += String(node.textContent || '');
    return node;
  },
};
global.document={
  createTextNode(text){ return {type:'text',textContent:String(text)}; },
};
const caughtUp=_renderStreamingFadeMarkdown('alpha beta');
if(caughtUp) throw new Error('expected fade playout to remain catching up');
if(assistantBody.textContent!=='alpha beta') throw new Error(`wrong hidden text: ${assistantBody.textContent}`);
if(_streamFadeDomText!=='alpha beta') throw new Error(`wrong dom text: ${_streamFadeDomText}`);
if(assistantBody.children.some(node=>node.className==='stream-fade-word is-new')){
  throw new Error('hidden body received fade span');
}
if(!assistantBody.classList.added.includes('stream-fade-active')) throw new Error('missing stream fade active marker');
"""
    )
    run_node(script)


def test_transparent_anchor_prose_receives_revealed_fade_text():
    render_section = slice_between(
        MESSAGES_JS,
        "const displayText = segmentStart===0",
        "scrollIfPinned();",
    )
    assert_contains_all(
        render_section,
        [
            "let anchorProcessText=displayText",
            "if(assistantBody){",
            "const caughtUp=_renderStreamingFadeMarkdown(displayText)",
            "if(_shouldUseLiveProseFade())",
            "anchorProcessText=_streamFadeDomText||''",
            "if(anchorProcessText) _upsertAnchorProcessProse(anchorProcessText)",
        ],
    )
    assert render_section.index("let anchorProcessText=displayText") < render_section.index("if(assistantBody){")
    assert render_section.index("anchorProcessText=_streamFadeDomText||''") < render_section.index(
        "_upsertAnchorProcessProse(anchorProcessText)"
    )
    assert render_section.index("if(assistantBody){") < render_section.rindex(
        "if(anchorProcessText) _upsertAnchorProcessProse(anchorProcessText)"
    )


def test_stream_fade_done_drain_has_hard_cap_for_large_buffered_responses():
    drain_block = function_block(MESSAGES_JS, "_drainStreamFadeBeforeDone")
    assert "const _STREAM_FADE_DONE_DRAIN_MAX_MS=1400" in MESSAGES_JS
    assert_contains_all(
        drain_block,
        [
            "const drainStartedAt=performance.now();",
            "const target=_streamFadeCurrentDisplayText();",
            "const caughtUp=_renderStreamingFadeMarkdown(target);",
            "const anchorProcessText=_streamFadeDomText||target;",
            "if(anchorProcessText) _upsertAnchorProcessProse(anchorProcessText);",
            "performance.now()-drainStartedAt>=_STREAM_FADE_DONE_DRAIN_MAX_MS",
            "if(_smdParser) _smdEndParser();",
            "onDone();",
        ],
    )
    assert drain_block.index("_renderStreamingFadeMarkdown(target)") < drain_block.index(
        "_upsertAnchorProcessProse(anchorProcessText)"
    )


def test_live_streaming_assistant_content_opts_out_of_global_theme_transitions():
    """Per-token markdown rewrites must not inherit global div color/background fades.

    The global theme transition is useful for dark/light switches, but live
    assistant DOM updates happen for every streamed token. If those live nodes
    inherit color/background transitions, light themes visibly flash/fade on
    each word.
    """
    live_transition_guard = slice_between(
        STYLE_CSS,
        "Live assistant content is updated token-by-token",
        ":root{--app-titlebar-safe-top",
    )
    assert_contains_all(
        live_transition_guard,
        [
            "#liveAssistantTurn *",
            "#thinkingRow *",
            '.assistant-segment[data-live-assistant="1"] *',
            '.agent-activity-thinking[data-thinking-active="1"] *',
            '.agent-activity-thinking[data-live-thinking="1"] *',
            '.live-worklog[data-live-worklog-shell="1"] *',
            "transition-property:none!important",
            "transition-duration:0s!important",
            "transition-delay:0s!important",
        ],
    )


def test_stream_fade_css_is_opacity_only_and_hides_live_cursor():
    fade_css = STYLE_CSS[STYLE_CSS.index("OpenWebUI-style streaming word fade") :]
    assert "filter:" not in STYLE_CSS[STYLE_CSS.index("OpenWebUI-style streaming word fade") :].split(
        "[data-live-assistant", 1
    )[0]
    assert "translateY" not in STYLE_CSS[STYLE_CSS.index("OpenWebUI-style streaming word fade") :].split(
        "[data-live-assistant", 1
    )[0]
    assert_contains_all(
        fade_css,
        [
            "@keyframes stream-fade-word-in",
            ".stream-fade-word.is-new",
            "var(--stream-fade-ms,620ms) cubic-bezier(.16,.84,.32,1)",
            "35%{opacity:.18;}",
            "70%{opacity:.72;}",
            "prefers-reduced-motion: reduce",
            ".msg-body.stream-fade-active > :last-child::after",
            "display:none",
            "content:none",
        ],
    )
    assert ".stream-fade-active .stream-fade-word{display:inline;}" in fade_css


def test_stream_fade_reduced_motion_listener_is_cleaned_up_on_terminal_paths():
    assert "_streamFadeReduceMotionOnChange" in MESSAGES_JS
    assert "function _streamFadeCleanupReduceMotionListener()" in MESSAGES_JS
    assert "removeEventListener('change',_streamFadeReduceMotionOnChange)" in MESSAGES_JS
    assert "removeListener(_streamFadeReduceMotionOnChange)" in MESSAGES_JS
    assert MESSAGES_JS.count("_streamFadeCleanupReduceMotionListener();") >= 4


def test_stream_fade_duration_scales_up_with_playback_speed():
    script = (
        fade_helper_script()
        + r"""
const words=Array.from({length:260},(_,i)=>'w'+i).join(' ');
performance._t += 33;
let out=_streamFadeNextText('slow start');
if(!out.changed) throw new Error('expected initial reveal');
if(_streamFadeCurrentMs !== 620) throw new Error(`expected base fade 620ms, got ${_streamFadeCurrentMs}`);
for(let frame=0;frame<20&&_streamFadeCurrentMs<900;frame++){
  performance._t += 120;
  out=_streamFadeNextText(words);
}
if(_streamFadeCurrentMs !== 900) throw new Error(`expected max fade 900ms, got ${_streamFadeCurrentMs}`);
"""
    )
    run_node(script)


def test_stream_fade_playout_handles_fast_models_without_paragraph_pops():
    script = (
        fade_helper_script()
        + r"""
const words=Array.from({length:240},(_,i)=>'w'+i);
let shown=0;
let targetCount=0;
for(let frame=0;frame<240;frame++){
  performance._t += 16;
  // Simulate sustained fast generation: ~40 words/sec arriving.
  targetCount = Math.min(words.length, Math.floor(performance._t/1000*40));
  const out=_streamFadeNextText(words.slice(0,targetCount).join(' '));
  shown=(out.text.match(/\S+/g)||[]).length;
}
const backlog=targetCount-shown;
if(shown < 145) throw new Error(`too slow: shown=${shown} target=${targetCount} backlog=${backlog} arrivalWps=${_streamFadeArrivalWps}`);
if(backlog > 15) throw new Error(`did not catch up: shown=${shown} target=${targetCount} backlog=${backlog} arrivalWps=${_streamFadeArrivalWps}`);
const huge=Array.from({length:500},(_,i)=>'b'+i).join(' ');
let previous=0;
for(let frame=0;frame<40;frame++){
  performance._t += 16;
  const out=_streamFadeNextText(huge);
  const shown=(out.text.match(/\S+/g)||[]).length;
  const revealed=shown-previous;
  previous=shown;
  if(revealed>3) throw new Error(`revealed too much in one frame: ${revealed}`);
}
if(previous<50) throw new Error(`too slow under large backlog: ${previous}`);
"""
    )
    run_node(script)


def test_stream_fade_respects_sentence_and_paragraph_boundaries():
    script = (
        fade_helper_script()
        + r"""
const target='alpha beta gamma\n\nsecond paragraph starts here\n\nthird paragraph starts here';
performance._t += 200;
let out=_streamFadeNextText(target);
const breaks=(out.text.match(/\n\s*\n/g)||[]).length;
if(breaks>1) throw new Error(`revealed multiple paragraph breaks: ${JSON.stringify(out.text)}`);
_resetStreamFadeState();
const pausedTarget='alpha beta.\n\nsecond paragraph starts here';
out={text:''};
for(let frame=0;frame<8&&!out.text.includes('.');frame++){
  performance._t += 33;
  out=_streamFadeNextText(pausedTarget);
}
if(!out.text.includes('.')) throw new Error(`expected first sentence: ${JSON.stringify(out.text)}`);
const held=_streamFadeNextText(pausedTarget);
if(held.changed) throw new Error('expected sentence pause to hold next reveal');
performance._t += 50;
for(let frame=0;frame<8&&!out.text.includes('\n\n');frame++){
  performance._t += 33;
  out=_streamFadeNextText(pausedTarget);
}
if(!out.text.includes('\n\n')) throw new Error(`expected paragraph break: ${JSON.stringify(out.text)}`);
const afterBreak=_streamFadeNextText(pausedTarget);
if(afterBreak.changed) throw new Error('expected paragraph pause to hold next reveal');
"""
    )
    run_node(script)
