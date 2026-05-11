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


def event_listener_block(src: str, event_name: str) -> str:
    start = src.index(f"source.addEventListener('{event_name}'")
    end = src.index("source.addEventListener(", start + 1)
    return src[start:end]


def compact(src: str) -> str:
    return re.sub(r"\s+", "", src)


def assert_contains_all(src: str, snippets: list[str]) -> None:
    for snippet in snippets:
        assert snippet in src


def fade_helper_script(performance_stub: str = "{_t:0,now(){return this._t;}}") -> str:
    helpers = "\n".join(
        function_block(MESSAGES_JS, name)
        for name in [
            "_streamFadeWordCountOf",
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
let _streamFadeLastRevealCount=0;
const _STREAM_FADE_MS=140;
const _STREAM_FADE_WAVE_MS=320;
const _STREAM_FADE_MAX_STAGGER_MS=520;
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


def test_fade_helpers_and_constants_exist():
    for name in [
        "_resetStreamFadeState",
        "_shouldUseStreamFade",
        "_streamFadeNextText",
        "_streamFadeWordCountOf",
        "_renderStreamingFadeMarkdown",
        "_wrapStreamingFadeWords",
        "_streamFadeSkipNode",
        "_drainStreamFadeBeforeDone",
    ]:
        assert f"function {name}" in MESSAGES_JS

    assert_contains_all(
        MESSAGES_JS,
        [
            "const _STREAM_FADE_MS=140",
            "const _STREAM_FADE_WAVE_MS=320",
            "const _STREAM_FADE_MAX_STAGGER_MS=520",
            "_streamFadeVisibleText",
            "_streamFadeWordBornAt",
            "_streamFadeArrivalWps",
        ],
    )


def test_schedule_render_keeps_default_smd_path_when_fade_is_off():
    block = function_block(MESSAGES_JS, "_scheduleRender")
    assert "_shouldUseStreamFade()" in block
    assert "_renderStreamingFadeMarkdown(displayText)" in block
    assert "_smdWrite(displayText)" in block
    assert "_smdNewParser(assistantBody)" in block
    assert "?16:66" in compact(block)


def test_fade_renderer_uses_playout_buffer_and_markdown_rerender():
    next_block = function_block(MESSAGES_JS, "_streamFadeNextText")
    render_block = function_block(MESSAGES_JS, "_renderStreamingFadeMarkdown")

    assert_contains_all(
        next_block,
        [
            "targetText.startsWith(_streamFadeVisibleText)",
            "wordsPerSecond",
            "instantArrivalWps",
            "backlogWords",
            "streamAgeSeconds",
            "caughtUp",
        ],
    )
    assert_contains_all(
        render_block,
        [
            "_streamFadeNextText(displayText)",
            "renderMd ? renderMd(next.text||'')",
            "stream-fade-active",
            "_wrapStreamingFadeWords(assistantBody)",
            "_sanitizeSmdLinks(assistantBody)",
        ],
    )


def test_fade_animation_state_survives_markdown_rerenders():
    block = function_block(MESSAGES_JS, "_wrapStreamingFadeWords")
    assert_contains_all(
        block,
        [
            "_streamFadeWordBornAt[wordIndex]",
            "ageMs",
            "animationDelay",
            "--stream-fade-ms",
            "_streamFadeLatestAnimationEndAt",
            "_streamFadeWordBornAt.length=wordIndex+1",
        ],
    )
    assert "filter:" not in STYLE_CSS[STYLE_CSS.index("OpenWebUI-style streaming word fade") :].split(
        "[data-live-assistant", 1
    )[0]
    assert "translateY" not in STYLE_CSS[STYLE_CSS.index("OpenWebUI-style streaming word fade") :].split(
        "[data-live-assistant", 1
    )[0]


def test_done_drain_finishes_fade_before_final_dom_replacement_and_blocks_late_mutations():
    done_block = event_listener_block(MESSAGES_JS, "done")
    drain_block = function_block(MESSAGES_JS, "_drainStreamFadeBeforeDone")

    assert_contains_all(done_block, ["_terminalStateReached=true", "_drainStreamFadeBeforeDone(_finishDone)"])
    assert_contains_all(drain_block, ["remainingAnimationMs", "_STREAM_FADE_MAX_STAGGER_MS", "requestAnimationFrame(step)"])

    for event_name in ["token", "interim_assistant", "reasoning"]:
        assert "if(_terminalStateReached||_streamFinalized) return;" in event_listener_block(MESSAGES_JS, event_name)


def test_new_segments_reset_fade_state():
    assert "_resetStreamFadeState()" in function_block(MESSAGES_JS, "_resetAssistantSegment")


def test_fade_css_animates_words_and_hides_live_cursor():
    fade_css = STYLE_CSS[STYLE_CSS.index("OpenWebUI-style streaming word fade") :]
    assert_contains_all(
        fade_css,
        [
            "@keyframes stream-fade-word-in",
            ".stream-fade-word.is-new",
            "var(--stream-fade-ms,140ms) ease-out",
            ".msg-body.stream-fade-active > :last-child::after",
            "display:none",
            "content:none",
        ],
    )
    assert "prefers-reduced-motion: reduce" not in fade_css


def test_stream_fade_next_text_executes_and_advances_playout():
    script = (
        fade_helper_script("{_t:0,now(){this._t+=33;return this._t;}}")
        + r"""
const target='one two three four five six seven eight nine ten eleven twelve';
const first=_streamFadeNextText(target);
const second=_streamFadeNextText(target);
if (!first.text || !second.text) throw new Error('no text revealed');
if (second.text.length < first.text.length) throw new Error('playout regressed');
"""
    )
    result = run_node(script)
    assert "ReferenceError" not in result.stderr


def test_stream_fade_ramps_above_steady_arrival_rate():
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
if(shown < 150) throw new Error(`too slow: shown=${shown} target=${targetCount} backlog=${backlog} arrivalWps=${_streamFadeArrivalWps}`);
if(backlog > 10) throw new Error(`did not catch up: shown=${shown} target=${targetCount} backlog=${backlog} arrivalWps=${_streamFadeArrivalWps}`);
"""
    )
    run_node(script)
