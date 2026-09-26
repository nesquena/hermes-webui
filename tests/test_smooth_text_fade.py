import re
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONFIG_PY = (REPO / "api" / "config.py").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
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
let _streamFadeSilentPrefixChars=0;
const _STREAM_FADE_MS=620;
const _STREAM_FADE_MAX_MS=900;
const _STREAM_FADE_DONE_MAX_MS=1000;
const _STREAM_FADE_DONE_DRAIN_MAX_MS=1400;
const performance={performance_stub};
{helpers}
"""


def run_node(script: str) -> subprocess.CompletedProcess[str]:
    # Windows cmdline length limit (~32K): long extracted function blocks
    # (e.g. _smdWrite with its comment block) overflow `node -e`. Write long
    # scripts to a temp file and run `node <file>` instead.
    if len(script) > 24000:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(script)
            tmp_path = f.name
        try:
            result = subprocess.run(
                ["node", tmp_path],
                cwd=REPO,
                text=True,
                capture_output=True,
                check=False,
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        assert result.returncode == 0, result.stderr
        return result
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
        ["animationend", "span.classList.remove('is-new')"],
    )
    assert "span.replaceWith" not in cleanup_block
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
let _streamFadeSilentPrefixChars=0;
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


def test_fade_cleanup_preserves_the_animated_node_as_the_scroll_anchor():
    """Animation cleanup must not replace inline nodes during a live stream.

    Replacing every word node on animationend makes the browser choose a new
    native scroll anchor while the transcript is still growing.  The opacity
    animation can finish by removing its transient class; final settlement will
    rebuild the persisted answer as plain text.
    """
    cleanup = function_block(MESSAGES_JS, "_streamFadeBindCleanup")
    script = r"""
class FakeClassList {
  constructor(names){ this.names=new Set(names); }
  contains(name){ return this.names.has(name); }
  remove(name){ this.names.delete(name); }
}
const listeners={};
const document={createTextNode(text){ return {textContent:String(text)}; }};
const body={
  _streamFadeCleanupBound:false,
  addEventListener(name,fn){ listeners[name]=fn; },
};
""" + cleanup + r"""
_streamFadeBindCleanup(body);
const span={
  textContent:'stable anchor',
  classList:new FakeClassList(['stream-fade-word','is-new']),
  style:{
    removed:[],
    removeProperty(name){ this.removed.push(name); },
  },
  replacements:0,
  replaceWith(){ this.replacements += 1; },
};
listeners.animationend({target:span});
if(span.replacements!==0) throw new Error('fade cleanup replaced the active scroll-anchor node');
if(span.classList.contains('is-new')) throw new Error('fade animation class was not cleared');
if(!span.classList.contains('stream-fade-word')) throw new Error('stable fade node lost its identity');
if(span.style.removed.join('|')!=='--stream-fade-ms') throw new Error('fade timing override was not cleared');
"""
    run_node(script)


def test_transparent_fade_cleanup_preserves_the_animated_node_as_the_scroll_anchor():
    """The transparent-activity fade path must share the messages.js cleanup.

    `_bindTransparentFadeCleanup()` in ui.js is the visible transparent_stream
    sibling of `_streamFadeBindCleanup()` in messages.js.  It used to replace
    every animated word span with a fresh text node on animationend, which made
    native scroll anchoring pick a new anchor while the transparent prose row
    was still growing.  Both live prose paths must keep the node stable: only
    the transient `is-new` class and the `--stream-fade-ms` timing override are
    removed; final settlement rebuilds the persisted DOM as plain text.
    """
    cleanup = function_block(UI_JS, "_bindTransparentFadeCleanup")
    assert_contains_all(
        cleanup,
        [
            "animationend",
            "span.classList.remove('is-new')",
            "span.style.removeProperty('--stream-fade-ms')",
        ],
    )
    assert "span.replaceWith" not in cleanup
    script = r"""
class FakeClassList {
  constructor(names){ this.names=new Set(names); }
  contains(name){ return this.names.has(name); }
  remove(name){ this.names.delete(name); }
}
const listeners={};
const document={createTextNode(text){ return {textContent:String(text)}; }};
const body={
  _transparentFadeCleanupBound:false,
  addEventListener(name,fn){ listeners[name]=fn; },
};
""" + cleanup + r"""
_bindTransparentFadeCleanup(body);
if(body._transparentFadeCleanupBound!==true) throw new Error('transparent fade cleanup was not bound');
const span={
  textContent:'stable anchor',
  classList:new FakeClassList(['stream-fade-word','is-new']),
  style:{
    removed:[],
    removeProperty(name){ this.removed.push(name); },
  },
  replacements:0,
  replaceWith(){ this.replacements += 1; },
};
listeners.animationend({target:span});
if(span.replacements!==0) throw new Error('transparent fade cleanup replaced the active scroll-anchor node');
if(span.classList.contains('is-new')) throw new Error('transparent fade animation class was not cleared');
if(!span.classList.contains('stream-fade-word')) throw new Error('stable transparent fade node lost its identity');
if(span.style.removed.join('|')!=='--stream-fade-ms') throw new Error('transparent fade timing override was not cleared');
"""
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


def test_stream_fade_rewind_keeps_common_prefix_visible():
    """A display-text REWIND (tool-call XML stripped mid-stream) must not reset
    the playout to zero — that would replay the fade on every already-visible
    word and produce a full-message blink per tool call (#fade-flash)."""
    script = (
        fade_helper_script()
        + r"""
// Phase 1: play out a sentence normally.
let out=_streamFadeNextText('alpha beta gamma delta');
for(let frame=0;frame<60&&!out.caughtUp;frame++){
  performance._t += 33;
  out=_streamFadeNextText('alpha beta gamma delta');
}
if(!out.caughtUp) throw new Error(`never caught up: ${JSON.stringify(out.text)}`);
const fullText=out.text;
// Phase 2: the stream text REWINDS (e.g. <function_calls> stripped): the
// target becomes a strict prefix of what was already shown.
performance._t += 33;
out=_streamFadeNextText('alpha beta');
// The playout must stay at the common prefix, NOT reset to ''.
if(!out.text.startsWith('alpha beta')) throw new Error(`rewind dropped prefix: ${JSON.stringify(out.text)}`);
if(out.text.length>fullText.length) throw new Error(`rewind grew text: ${JSON.stringify(out.text)}`);
if(!out.caughtUp) throw new Error(`rewind to prefix should be caught up, got changed=${out.changed}`);
"""
    )
    run_node(script)


def test_stream_fade_rewind_remount_mutes_common_prefix():
    """When the DOM must be rebuilt after a rewind (smd self-heal), words inside
    the common prefix must be written as plain text so their fade animation is
    not replayed; only the post-rewind tail may animate."""
    script = (
        function_block(MESSAGES_JS, "_streamFadeAppendText")
        + r"""
const _STREAM_FADE_MS=620;
let _streamFadeLatestAnimationEndAt=0;
let _streamFadeCurrentMs=620;
let _streamFadeSilentPrefixChars=0;
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
// Simulate a rebuild: the rewind-triggered self-heal mutes the common prefix
// ("alpha beta" = 10 chars) so those words must NOT become animated spans.
_streamFadeSilentPrefixChars=10;
_streamFadeAppendText(body,'alpha beta gamma');
const spans=body.children.filter(node=>node.className==='stream-fade-word is-new');
if(spans.length!==1) throw new Error(`expected exactly 1 animated span, got ${spans.length}`);
if(spans[0].textContent!=='gamma') throw new Error(`wrong animated word: ${spans[0].textContent}`);
const plain=body.children.filter(node=>node.type==='text').map(n=>n.textContent).join('');
if(!plain.includes('alpha beta')) throw new Error(`common prefix not written as plain text: ${plain}`);
if(_streamFadeSilentPrefixChars!==0) throw new Error(`silent prefix not consumed: ${_streamFadeSilentPrefixChars}`);
"""
    )
    run_node(script)


def test_stream_fade_smd_write_self_heal_sets_silent_prefix_on_rewind():
    """_smdWrite's self-heal rebuild (triggered when the display text REWINDS,
    e.g. tool-call XML stripped mid-stream) must mute the rebuild's rendered
    common prefix. Without this the cleared + rebuilt body would replay the
    fade on every already-visible word — the full-message blink. The prefix is
    computed in RENDERED-text space (old node text vs new node text), never in
    source space (#6783 review)."""
    write_block = function_block(MESSAGES_JS, "_smdWrite")
    script = (
        write_block
        + r"""
let _smdParser=null;
let _smdWrittenLen=0;
let _smdWrittenText='';
let _streamFadeSilentPrefixChars=0;
let rebuildCalls=0;
let muteCalls=[];
const writes=[];
global.window={
  smd:{
    parser(renderer){ return { renderer }; },
    parser_write(parser,delta){ writes.push(String(delta)); },
  },
};
function _streamFadeMuteRenderedPrefix(el, prev){ muteCalls.push(prev); }
const assistantBody={ innerHTML:'', textContent:'' };
function _smdNewParser(el, fade){ _smdParser={fresh:fade?true:false}; rebuildCalls+=1; }
function _scheduleStreamingKatex(){}
// Phase 0: parser already attached (as after _smdNewParser on stream start).
_smdParser={};
// Phase 1: normal incremental write of the full text.
_smdWrite('alpha beta gamma', true);
if(_smdWrittenText!=='alpha beta gamma') throw new Error(`phase1 writtenText wrong: ${_smdWrittenText}`);
if(rebuildCalls!==0) throw new Error('phase1 must not rebuild');
// Phase 2: the display text REWINDS to a strict prefix (tool-call XML tail
// stripped). Simulate the rendered body state (what smd already painted)
// before the rebuild; the self-heal must snapshot it and hand it to
// _streamFadeMuteRenderedPrefix (rendered-space mute).
assistantBody.textContent='alpha beta gamma';
_smdWrite('alpha beta', true);
if(rebuildCalls!==1) throw new Error(`expected 1 rebuild, got ${rebuildCalls}`);
if(muteCalls.length!==1) throw new Error(`expected 1 mute call, got ${muteCalls.length}`);
if(muteCalls[0]!=='alpha beta gamma') throw new Error(`mute prev wrong: ${JSON.stringify(muteCalls[0])}`);
if(_smdWrittenText!=='alpha beta') throw new Error(`phase2 writtenText wrong: ${_smdWrittenText}`);
// Phase 3: after the tool call completes, new text continues past the rewind
// point; the written delta must start exactly at the rewind point (no replay
// of the prefix). No rewind now → no mute call.
writes.length=0;
muteCalls.length=0;
_smdWrite('alpha beta new tail', true);
if(writes.length!==1||writes[0]!==' new tail') throw new Error(`delta wrong: ${JSON.stringify(writes)}`);
if(muteCalls.length!==0) throw new Error(`phase3 must not mute, got ${muteCalls.length}`);
"""
    )
    run_node(script)


def _fade_fake_node(tag='div', text=''):
    """Fake DOM node with dynamic textContent (sums descendant text), parentNode
    backlinks and classList — enough for _streamFadeMuteRenderedPrefix."""
    return """
class FakeNode{
  constructor(tag='div',text=''){
    this.tagName=String(tag).toUpperCase();
    this.nodeType=(tag==='#text')?3:1;
    this.type=(tag==='#text')?'text':undefined;
    this.children=[];
    this.parentNode=null;
    this.className='';
    this._text=text;
    if(text!=='' && tag!=='#text'){ this.appendChild(new FakeNode('#text',text)); }
  }
  appendChild(child){ child.parentNode=this; this.children.push(child); return child; }
  get childNodes(){ return this.children; }
  get textContent(){
    if(this.nodeType===3) return this._text;
    let s='';
    for(const c of this.children) s+=c.textContent;
    return s;
  }
  set textContent(v){ this._text=String(v); }
  get classList(){
    const self=this;
    return {
      contains(c){ return (' '+self.className+' ').indexOf(' '+c+' ')>-1; },
      remove(c){ self.className=(' '+self.className+' ').replace(' '+c+' ',' ').trim(); },
    };
  }
}
"""


def test_stream_fade_mute_rendered_prefix_plain_words():
    """Rendered-space mute: only spans fully inside the rendered common prefix
    lose is-new; the first genuinely new word keeps its fade span. Old text
    'alpha beta' vs new 'alpha beta gamma' → alpha+beta plain, gamma animated."""
    script = (
        function_block(MESSAGES_JS, "_streamFadeMuteRenderedPrefix")
        + _fade_fake_node()
        + r"""
const body=new FakeNode('div');
const a=new FakeNode('span','alpha'); a.className='stream-fade-word is-new';
const sp1=new FakeNode('#text',' ');
const b=new FakeNode('span','beta');  b.className='stream-fade-word is-new';
const sp2=new FakeNode('#text',' ');
const g=new FakeNode('span','gamma'); g.className='stream-fade-word is-new';
body.appendChild(a); body.appendChild(sp1); body.appendChild(b);
body.appendChild(sp2); body.appendChild(g);
// Rebuild scenario: old rendered text was 'alpha beta' (rewind point), new
// rendered text is 'alpha beta gamma'. Common prefix = 'alpha beta ' (11).
_streamFadeMuteRenderedPrefix(body,'alpha beta');
if(a.classList.contains('is-new')) throw new Error('alpha must be muted');
if(b.classList.contains('is-new')) throw new Error('beta must be muted');
if(!g.classList.contains('is-new')) throw new Error('gamma must stay animated');
"""
    )
    run_node(script)


def test_stream_fade_mute_rendered_prefix_markdown_and_media_bytes():
    """Regression for the #6783 blocker: source-space budgets over-mute because
    markdown delimiters (`**alpha**`) and MEDIA tokens add bytes that never
    reach the fade add_text hook. The mute must operate on RENDERED text only:
    old rendered 'alpha beta' (from `**alpha** beta`) rewritten to `**alpha**
    gamma` renders 'alpha gamma' → alpha muted, gamma animated."""
    script = (
        function_block(MESSAGES_JS, "_streamFadeMuteRenderedPrefix")
        + _fade_fake_node()
        + r"""
const body=new FakeNode('div');
const a=new FakeNode('span','alpha'); a.className='stream-fade-word is-new';
const sp=new FakeNode('#text',' ');
const g=new FakeNode('span','gamma'); g.className='stream-fade-word is-new';
body.appendChild(a); body.appendChild(sp); body.appendChild(g);
// Rendered common prefix 'alpha ' = 6 chars. A SOURCE-space compare of
// `**alpha** beta` vs `**alpha** gamma` would count 10 (4 delimiters +
// 'alpha' + space) and mute gamma too — exactly the bug this test locks in.
_streamFadeMuteRenderedPrefix(body,'alpha beta');
if(a.classList.contains('is-new')) throw new Error('alpha must be muted');
if(!g.classList.contains('is-new')) throw new Error('gamma must stay animated (source-space budget bug)');
"""
    )
    run_node(script)


def test_stream_fade_mute_rendered_prefix_scoped_per_root():
    """The mute is scoped to the passed root: muting one parser's node must not
    touch another parser's node (main vs anchor prose). Two concurrent roots
    with identical text content stay independent."""
    script = (
        function_block(MESSAGES_JS, "_streamFadeMuteRenderedPrefix")
        + _fade_fake_node()
        + r"""
function buildBody(){
  const body=new FakeNode('div');
  const w1=new FakeNode('span','alpha'); w1.className='stream-fade-word is-new';
  const sp=new FakeNode('#text',' ');
  const w2=new FakeNode('span','beta');  w2.className='stream-fade-word is-new';
  body.appendChild(w1); body.appendChild(sp); body.appendChild(w2);
  return body;
}
const main=buildBody();
const anchor=buildBody();
_streamFadeMuteRenderedPrefix(main,'alpha'); // common prefix 'alpha' (5)
// main: alpha muted, beta animated.
if(main.children[0].classList.contains('is-new')) throw new Error('main alpha must be muted');
if(!main.children[2].classList.contains('is-new')) throw new Error('main beta must stay animated');
// anchor: untouched.
if(!anchor.children[0].classList.contains('is-new')) throw new Error('anchor must be untouched');
if(!anchor.children[2].classList.contains('is-new')) throw new Error('anchor must be untouched');
"""
    )
    run_node(script)
