from pathlib import Path
import json
import re
import shutil
import subprocess

import pytest


REPO = Path(__file__).parent.parent
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
TERMINAL_JS = (REPO / "static" / "terminal.js").read_text(encoding="utf-8")
PANEL_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _iter_root_skin_blocks(css):
    selector_re = re.compile(r'(:root(?:\.dark)?\[data-skin="[^"]+"\][^{]*?)\{')
    for selector_match in selector_re.finditer(css):
        selector = selector_match.group(1).strip()
        idx = selector_match.end()
        depth = 1
        end = idx
        while end < len(css) and depth:
            ch = css[end]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            end += 1
        if depth:
            continue
        yield selector, css[idx : end - 1]


def _get_root_skin_block(css, skin):
    target = f':root[data-skin="{skin}"]'
    for selector, block in _iter_root_skin_blocks(css):
        if selector == target:
            return block
    return ""


def _font_stack_offenders(css):
    cleaned = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    pattern = re.compile(r"(?i)\b(font-family|font)\s*:\s*([^;]+);")
    offenders = []
    for _, declaration in pattern.findall(cleaned):
        value = declaration.strip().lower()
        if ("monospace" not in value and "ui-monospace" not in value):
            continue
        if value == "inherit":
            continue
        offenders.append(declaration)
    return offenders


def test_typography_root_tokens_are_defined():
    assert '--font-ui:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,system-ui,sans-serif;' in CSS
    assert '--font-conversation:var(--font-ui);' in CSS
    assert '--font-mono:ui-monospace,"SFMono-Regular","SF Mono",Menlo,Consolas,"Liberation Mono",monospace;' in CSS


def test_msg_body_uses_conversation_font():
    assert '.msg-body{font-family:var(--font-conversation);font-size:var(--message-body-font-size);line-height:var(--message-body-line-height);' in CSS


def test_builtin_skin_msg_body_rules_use_conversation_font():
    for skin in ("graphite", "codex", "terracotta", "github"):
        selector = (
            f':root[data-skin="{skin}"] .msg-body'
            '{font-family:var(--font-conversation);font-size:13px;font-weight:430;letter-spacing:0;line-height:1.6;}'
        )
        assert selector in CSS


def test_no_skin_msg_body_rules_use_root_font_ui_token():
    assert not re.search(
        r':root(?:\.dark)?\[data-skin="[^"]+"\]\s*\.msg-body\s*\{[^{}]*\bfont-family\s*:\s*var\(--font-ui\)\s*;[^{}]*\}',
        CSS,
        re.S,
    )


def test_no_skin_redefines_font_conversation():
    for selector, block in _iter_root_skin_blocks(CSS):
        if '--font-conversation:' in block:
            raise AssertionError(f'Unexpected skin-level --font-conversation: {selector}')


def test_nous_skin_keeps_monospace_ui_default():
    assert _get_root_skin_block(CSS, "nous")
    assert re.search(
        r'--font-ui:"SF Mono","Roboto Mono","Courier New",monospace;',
        _get_root_skin_block(CSS, "nous"),
        re.S,
    )


def test_geist_and_neon_skins_set_expected_font_ui_tokens():
    geist_block = _get_root_skin_block(CSS, "geist-contrast")
    assert geist_block
    assert "--font-ui:\"Geist\",\"Geist Sans\",-apple-system,BlinkMacSystemFont,\"Segoe UI\",Helvetica,Arial,sans-serif;" in geist_block
    for skin in ("neon", "neon-soft", "neon-paint"):
        block = _get_root_skin_block(CSS, skin)
        assert block, f"Missing {skin} root skin block"
        assert "--font-ui:system-ui,-apple-system,sans-serif;" in block


def test_no_stale_var_mono_usage_or_literal_monospace_font_family_stacks():
    assert 'var(--mono' not in CSS
    offending_lines = _font_stack_offenders(CSS)
    assert not offending_lines, f"Unexpected literal monospace font stacks: {offending_lines[:4]}"


def test_terminal_sync_tracks_applied_appearance_and_observes_relevant_root_attributes():
    block_match = re.search(
        r"function\s+syncComposerTerminalAppearance\(\)\s*\{.*?\n\}\s*\n\s*function\s+_xtermReady\(\)",
        TERMINAL_JS,
        re.S,
    )
    assert block_match
    sync_block = block_match.group(0)
    assert "function _terminalThemesEqual" in TERMINAL_JS
    assert "lastAppliedTheme" in sync_block
    assert "lastAppliedFontFamily" in sync_block
    assert re.search(
        r"new\s+MutationObserver\(syncComposerTerminalAppearance\)\.observe\(document\.documentElement,\{.*?attributeFilter:\s*\[\s*['\"]class['\"],\s*['\"]data-skin['\"],\s*['\"]style['\"],?\s*\]",
        TERMINAL_JS,
        re.S,
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_terminal_appearance_sync_is_behaviorally_idempotent_and_lifecycle_safe():
    script = f"""
const terminalSource={json.dumps(TERMINAL_JS)};
const styles={{
  '--font-mono':'"Active Mono",monospace',
  '--code-bg':'#111111',
}};
const rootInline={{}};
let dark=false;
let observerCallback=null;
let observedTarget=null;
let observedOptions=null;
let fitCalls=0;
let resizeSchedules=0;
let nextAnimationFrameId=1;
const animationFrames=new Map();
const writes=[];
const createdAppearances=[];
const surface={{textContent:''}};

function pendingAnimationFrames(){{
  return animationFrames.size;
}}

function flushAnimationFrames(){{
  const pending=[...animationFrames.values()];
  animationFrames.clear();
  pending.forEach(callback=>callback());
}}

class FakeMutationObserver {{
  constructor(callback){{ observerCallback=callback; }}
  observe(target,options){{
    observedTarget=target;
    observedOptions=options;
  }}
}}

class FakeTerminal {{
  constructor(options){{
    createdAppearances.push({{
      fontFamily:options.fontFamily,
      theme:{{...options.theme}},
    }});
    const current={{
      fontFamily:options.fontFamily,
      theme:options.theme,
    }};
    this.options=new Proxy(current,{{
      set(target,key,value){{
        writes.push(String(key));
        target[key]=value;
        return true;
      }},
    }});
    this.cols=80;
    this.rows=24;
  }}
  loadAddon(){{}}
  open(){{}}
  onData(){{}}
  dispose(){{}}
}}

class FakeFitAddon {{
  fit(){{ fitCalls+=1; }}
}}

const root={{
  classList:{{contains(name){{ return name==='dark'&&dark; }}}},
  style:{{setProperty(name,value){{ rootInline[name]=value; }}}},
}};
global.window={{
  addEventListener(){{}},
  MutationObserver:FakeMutationObserver,
  Terminal:FakeTerminal,
  FitAddon:{{FitAddon:FakeFitAddon}},
}};
global.MutationObserver=FakeMutationObserver;
global.document={{
  documentElement:root,
  getElementById(){{ return null; }},
}};
global.getComputedStyle=()=>({{
  getPropertyValue(name){{ return styles[name]||rootInline[name]||''; }},
}});
global.$=(id)=>id==='terminalSurface'?surface:null;
global.requestAnimationFrame=(callback)=>{{
  const id=nextAnimationFrameId++;
  animationFrames.set(id,callback);
  return id;
}};
global.cancelAnimationFrame=(id)=>animationFrames.delete(id);
global.setTimeout=(_callback,delay)=>{{
  if(delay===120)resizeSchedules+=1;
  return 1;
}};
global.clearTimeout=()=>{{}};

eval(terminalSource+String.raw`
const first=_ensureXterm();
TERMINAL_UI.open=true;
TERMINAL_UI.sessionId='session-1';
const initialFitCalls=fitCalls;
resizeSchedules=0;

writes.length=0;
const fitsBeforeUnchanged=fitCalls;
syncComposerTerminalAppearance();
const unchangedWrites=[...writes];
const unchangedFitDelta=fitCalls-fitsBeforeUnchanged;
const unchangedPendingFrames=pendingAnimationFrames();

writes.length=0;
const fitsBeforeUnrelated=fitCalls;
document.documentElement.style.setProperty('--unrelated-outline-offset','3px');
observerCallback([{{attributeName:'style'}}]);
const unrelatedStyleWrites=[...writes];
const unrelatedFitDelta=fitCalls-fitsBeforeUnrelated;
const unrelatedPendingFrames=pendingAnimationFrames();

writes.length=0;
const fitsBeforeTheme=fitCalls;
styles['--code-bg']='#222222';
observerCallback([{{attributeName:'style'}}]);
const themeOnlyWrites=[...writes];
const backgroundAfterChange=first.options.theme.background;
const themeOnlyFitDelta=fitCalls-fitsBeforeTheme;
const themeOnlyPendingFrames=pendingAnimationFrames();

writes.length=0;
resizeSchedules=0;
const fitsBeforeFont=fitCalls;
styles['--font-mono']='"Extension Mono",monospace';
observerCallback([{{attributeName:'style'}}]);
observerCallback([{{attributeName:'style'}}]);
const fontOnlyWrites=[...writes];
const fontAfterChange=first.options.fontFamily;
const fontFitDeltaBeforeFrame=fitCalls-fitsBeforeFont;
const fontPendingFramesBeforeFlush=pendingAnimationFrames();
flushAnimationFrames();
const fontFitDeltaAfterFrame=fitCalls-fitsBeforeFont;
const fontResizeSchedules=resizeSchedules;

styles['--font-mono']='"Disposed Mono",monospace';
observerCallback([{{attributeName:'style'}}]);
const pendingFitBeforeDispose=pendingAnimationFrames();
_disposeXterm();
const pendingFitAfterDispose=pendingAnimationFrames();
const cacheReset=TERMINAL_UI.lastAppliedTheme===null&&TERMINAL_UI.lastAppliedFontFamily===null&&TERMINAL_UI.fontFitFrame===null;
styles['--font-mono']='"Recreated Mono",monospace';
styles['--code-bg']='#333333';
const fitsBeforeRecreate=fitCalls;
_ensureXterm();
const recreateFitDelta=fitCalls-fitsBeforeRecreate;
const recreatePendingFrames=pendingAnimationFrames();
flushAnimationFrames();
const recreateFitDeltaAfterFlush=fitCalls-fitsBeforeRecreate;

process.stdout.write(JSON.stringify({{
  initial:createdAppearances[0],
  initialFitCalls,
  unchangedWrites,
  unchangedFitDelta,
  unchangedPendingFrames,
  unrelatedStyleWrites,
  unrelatedFitDelta,
  unrelatedPendingFrames,
  themeOnlyWrites,
  backgroundAfterChange,
  themeOnlyFitDelta,
  themeOnlyPendingFrames,
  fontOnlyWrites,
  fontAfterChange,
  fontFitDeltaBeforeFrame,
  fontPendingFramesBeforeFlush,
  fontFitDeltaAfterFrame,
  fontResizeSchedules,
  observerUsesRoot:observedTarget===document.documentElement,
  observedOptions,
  pendingFitBeforeDispose,
  pendingFitAfterDispose,
  cacheReset,
  recreated:createdAppearances[1],
  recreateFitDelta,
  recreatePendingFrames,
  recreateFitDeltaAfterFlush,
}}));
`);
"""
    result = subprocess.run(
        [NODE, "-e", script],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)

    assert observed["initial"]["fontFamily"] == '"Active Mono",monospace'
    assert observed["initial"]["theme"]["background"] == "#111111"
    assert observed["initialFitCalls"] == 1
    assert observed["unchangedWrites"] == []
    assert observed["unchangedFitDelta"] == 0
    assert observed["unchangedPendingFrames"] == 0
    assert observed["unrelatedStyleWrites"] == []
    assert observed["unrelatedFitDelta"] == 0
    assert observed["unrelatedPendingFrames"] == 0
    assert observed["themeOnlyWrites"] == ["theme"]
    assert observed["backgroundAfterChange"] == "#222222"
    assert observed["themeOnlyFitDelta"] == 0
    assert observed["themeOnlyPendingFrames"] == 0
    assert observed["fontOnlyWrites"] == ["fontFamily"]
    assert observed["fontAfterChange"] == '"Extension Mono",monospace'
    assert observed["fontFitDeltaBeforeFrame"] == 0
    assert observed["fontPendingFramesBeforeFlush"] == 1
    assert observed["fontFitDeltaAfterFrame"] == 1
    assert observed["fontResizeSchedules"] == 1
    assert observed["observerUsesRoot"] is True
    assert observed["observedOptions"] == {
        "attributes": True,
        "attributeFilter": ["class", "data-skin", "style"],
    }
    assert observed["pendingFitBeforeDispose"] == 1
    assert observed["pendingFitAfterDispose"] == 0
    assert observed["cacheReset"] is True
    assert observed["recreated"]["fontFamily"] == '"Recreated Mono",monospace'
    assert observed["recreated"]["theme"]["background"] == "#333333"
    assert observed["recreateFitDelta"] == 1
    assert observed["recreatePendingFrames"] == 0
    assert observed["recreateFitDeltaAfterFlush"] == 1


def test_first_party_technical_js_uses_font_mono_contract():
    assert 'function _terminalMonoFont()' in TERMINAL_JS
    assert "'ui-monospace,\"SFMono-Regular\",\"SF Mono\",Menlo,Consolas,\"Liberation Mono\",monospace'" in TERMINAL_JS
    assert 'font-family:var(--font-mono)' in PANEL_JS
    assert "fontFamily='var(--font-mono)'" in UI_JS
    assert "font-family:var(--font-ui)" in BOOT_JS


def test_terminal_contract_updates_on_root_style_changes():
    assert "attributeFilter:['class','data-skin','style']" in TERMINAL_JS
    assert 'function syncComposerTerminalAppearance' in TERMINAL_JS
    assert 'function _terminalMonoFont' in TERMINAL_JS
