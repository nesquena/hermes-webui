"""Behavioral regression coverage for authoritative wakeup-visibility hydration."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _function_source(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.find(marker)
    assert start != -1, f"{name}() not found"
    params = src.find("(", start)
    assert params != -1
    depth = 0
    close = -1
    for idx in range(params, len(src)):
        if src[idx] == "(":
            depth += 1
        elif src[idx] == ")":
            depth -= 1
            if depth == 0:
                close = idx
                break
    assert close != -1
    brace = src.find("{", close)
    assert brace != -1
    depth = 0
    for idx in range(brace, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"{name}() body did not close")


def _run_node(script: str) -> dict:
    assert NODE is not None
    proc = subprocess.run(
        [NODE, "-e", script],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_authoritative_wakeup_hydration_invalidates_all_transcript_caches():
    apply_source = _function_source(PANELS_JS, "_applyShowBackgroundWakeupsSetting")
    clear_visible_source = _function_source(UI_JS, "clearVisibleMessageRowCache")
    clear_virtual_source = _function_source(UI_JS, "_clearMessageVirtualHeightCache")
    clear_render_source = _function_source(UI_JS, "clearMessageRenderCache")

    script = f"""
    global.window = {{ _showBackgroundWakeups: true }};
    const checkbox = {{ checked: true }};
    function $(id) {{ return id === 'settingsShowBackgroundWakeups' ? checkbox : null; }}
    function _messageVirtualDefaultHeightForRole() {{ return 72; }}
    function _clearUserRowIntrinsicHeightCache() {{}}

    const _renderCache = new Map([['stale-markdown', '<p>stale</p>']]);
    function _clearRenderCache() {{ _renderCache.clear(); }}
    const _sessionHtmlCache = new Map([
      ['current', {{ html: 'assistant process_wakeup', visibleSources: ['', 'process_wakeup'] }}],
      ['other', {{ html: 'other process_wakeup', visibleSources: ['', 'process_wakeup'] }}],
    ]);
    let _sessionHtmlCacheSid = 'current';
    let _visWithIdxCache = [{{ rawIdx: 0 }}, {{ rawIdx: 1 }}];
    let _visWithIdxCacheLen = 2;
    let _visWithIdxCacheSrc = [{{}}, {{}}];
    let _messageVirtualHeightCache = [90, 30];
    let _messageVirtualHeightCacheEntries = [{{}}, {{}}];
    let _messageVirtualHeightCacheLen = 2;
    let _messageVirtualHeightCacheSrc = [{{}}, {{}}];
    let _messageVirtualEstimatedRowHeight = 90;
    let _messageVirtualWindowKey = 'stale-window';
    let _messageVirtualMeasurementCycleKey = 'stale-cycle';
    let _messageVirtualMeasurementRetryCount = 2;
    let _messageVirtualScrollActive = true;
    let _messageVirtualScrollSettleTimer = 0;
    let _messageVirtualDeferredMeasurement = {{ stale: true }};

    let S = {{
      session: {{ session_id: 'current' }},
      messages: [
        {{ role: 'user', content: 'question' }},
        {{ role: 'assistant', content: 'answer' }},
        {{ role: 'user', content: 'completed', _source: 'process_wakeup' }},
      ],
    }};
    const renderCalls = [];
    let currentVisibleSources = [];
    function renderMessages(options) {{
      renderCalls.push(options || null);
      currentVisibleSources = S.messages
        .filter(message => !(message._source === 'process_wakeup' && window._showBackgroundWakeups === false))
        .map(message => message._source || '');
      _sessionHtmlCache.set(S.session.session_id, {{
        html: currentVisibleSources.join('|'),
        visibleSources: currentVisibleSources.slice(),
      }});
      _sessionHtmlCacheSid = S.session.session_id;
    }}
    function switchTo(sessionId, messages) {{
      S = {{ session: {{ session_id: sessionId }}, messages }};
      const cached = _sessionHtmlCache.get(sessionId);
      if (cached) currentVisibleSources = cached.visibleSources.slice();
      else renderMessages({{ preserveScroll: true }});
      return currentVisibleSources.slice();
    }}

    eval({json.dumps(clear_visible_source)});
    eval({json.dumps(clear_virtual_source)});
    eval({json.dumps(clear_render_source)});
    eval({json.dumps(apply_source)});

    const changed = _applyShowBackgroundWakeupsSetting(false);
    const cacheStateAfterHydration = {{
      renderCacheSize: _renderCache.size,
      sessionCacheKeys: Array.from(_sessionHtmlCache.keys()),
      sessionCacheContainsWakeup: Array.from(_sessionHtmlCache.values()).some(entry => entry.html.includes('process_wakeup')),
      visibleCache: _visWithIdxCache,
      virtualHeights: _messageVirtualHeightCache.slice(),
      virtualEntries: _messageVirtualHeightCacheEntries.slice(),
      virtualWindowKey: _messageVirtualWindowKey,
      checkboxChecked: checkbox.checked,
    }};
    const currentAfterHydration = currentVisibleSources.slice();
    const otherMessages = [
      {{ role: 'user', content: 'other question' }},
      {{ role: 'assistant', content: 'other answer' }},
      {{ role: 'user', content: 'other completed', _source: 'process_wakeup' }},
    ];
    const otherAfterSwitch = switchTo('other', otherMessages);
    const currentAfterSwitchBack = switchTo('current', [
      {{ role: 'user', content: 'question' }},
      {{ role: 'assistant', content: 'answer' }},
      {{ role: 'user', content: 'completed', _source: 'process_wakeup' }},
    ]);
    const renderCountBeforeIdempotentApply = renderCalls.length;
    const changedAgain = _applyShowBackgroundWakeupsSetting(false);

    process.stdout.write(JSON.stringify({{
      changed,
      changedAgain,
      renderCountBeforeIdempotentApply,
      renderCountAfterIdempotentApply: renderCalls.length,
      firstRenderOptions: renderCalls[0],
      currentAfterHydration,
      otherAfterSwitch,
      currentAfterSwitchBack,
      cacheStateAfterHydration,
    }}));
    """

    result = _run_node(script)

    assert result["changed"] is True
    assert result["changedAgain"] is False
    assert result["renderCountAfterIdempotentApply"] == result["renderCountBeforeIdempotentApply"]
    assert result["firstRenderOptions"] == {"preserveScroll": True}
    assert "process_wakeup" not in result["currentAfterHydration"]
    assert "process_wakeup" not in result["otherAfterSwitch"]
    assert "process_wakeup" not in result["currentAfterSwitchBack"]
    assert result["cacheStateAfterHydration"] == {
        "renderCacheSize": 0,
        "sessionCacheKeys": ["current"],
        "sessionCacheContainsWakeup": False,
        "visibleCache": None,
        "virtualHeights": [],
        "virtualEntries": [],
        "virtualWindowKey": "",
        "checkboxChecked": False,
    }


def test_wakeup_visibility_writers_use_the_central_application_helper():
    helper = _function_source(PANELS_JS, "_applyShowBackgroundWakeupsSetting")
    load_panel = _function_source(PANELS_JS, "loadSettingsPanel")
    autosave = _function_source(PANELS_JS, "_autosaveAppearanceSettings")
    apply_saved = _function_source(PANELS_JS, "_applySavedSettingsUi")

    assert "window._showBackgroundWakeups=" in helper
    assert "clearMessageRenderCache()" in helper
    assert "renderMessages({preserveScroll:true})" in helper
    assert "_applyShowBackgroundWakeupsSetting(settings.show_background_wakeups)" in load_panel
    assert "_applyShowBackgroundWakeupsSetting(this.checked)" in load_panel
    assert "_applyShowBackgroundWakeupsSetting(saved.show_background_wakeups)" in autosave
    assert "_applyShowBackgroundWakeupsSetting(body.show_background_wakeups,{rerender:false})" in apply_saved
    assert "_applyShowBackgroundWakeupsSetting(s.show_background_wakeups,{rerender:false})" in BOOT_JS

    assert "window._showBackgroundWakeups=" not in load_panel
    assert "window._showBackgroundWakeups=" not in autosave
    assert "window._showBackgroundWakeups=" not in apply_saved
    assert "window._showBackgroundWakeups=" not in BOOT_JS
