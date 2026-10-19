"""Phase 1 chat transcript and message action accessibility regression tests."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _function_body(source: str, name: str) -> str:
    start = source.find(f"function {name}(")
    assert start != -1, f"{name}() not found"
    depth = 0
    in_str = None
    escaped = False
    i = source.find("{", start)
    assert i != -1
    for idx in range(i, len(source)):
        ch = source[idx]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_str:
                in_str = None
            continue
        if ch in ("'", '"', "`"):
            in_str = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    raise AssertionError(f"Unclosed function {name}")


def test_assistant_role_header_exposes_level3_heading_and_hides_monogram():
    fn = _function_body(UI_JS, "_assistantRoleHtml")
    script = f"""
    function assistantDisplayName() {{ return 'Hermes'; }}
    function isTpsDisplayEnabled() {{ return true; }}
    function esc(s) {{ return String(s || ''); }}
    {fn}
    console.log(JSON.stringify({{ html: _assistantRoleHtml('2026-10-18 17:00', '42.0 t/s') }}));
    """
    out = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    html = out["html"]
    assert '<div class="role-icon assistant" aria-hidden="true">H</div>' in html
    assert '<span class="msg-role-name" role="heading" aria-level="3">Hermes</span>' in html


def test_user_turn_renders_screen_reader_heading_in_render_messages():
    render_messages = _function_body(UI_JS, "renderMessages")
    assert (
        'const userRoleHtml=`<div class="sr-only msg-role-user" role="heading" aria-level="3">'
        '${esc((typeof t===\'function\'&&t(\'you\'))||\'You\')}</div>`;'
    ) in render_messages
    assert 'const nextRowHtml=`${userRoleHtml}${filesHtml}<div class="msg-body">${bodyHtml}</div>${footHtml}`;' in render_messages


def test_message_action_buttons_carry_explicit_type_and_aria_labels():
    render_messages = _function_body(UI_JS, "renderMessages")
    assert 'type="button" class="msg-action-btn" title="${t(\'edit_message\')}" aria-label="${t(\'edit_message\')}"' in render_messages
    assert 'type="button" class="msg-action-btn" title="${t(\'undo_exchange\')}" aria-label="${t(\'undo_exchange\')}"' in render_messages
    assert 'type="button" class="msg-action-btn" title="${t(\'regenerate\')}" aria-label="${t(\'regenerate\')}"' in render_messages
    assert 'type="button" class="msg-copy-btn msg-action-btn" title="${t(\'copy\')}" aria-label="${t(\'copy\')}"' in render_messages
    assert 'type="button" class="msg-action-btn" title="${t(\'fork_from_here\')}" aria-label="${t(\'fork_from_here\')}"' in render_messages
    assert (
        'type="button" class="msg-action-btn msg-tts-btn" '
        'title="${t(\'tts_listen\')||\'Listen\'}" '
        'aria-label="${t(\'tts_listen\')||\'Listen\'}" '
        'aria-pressed="false"'
    ) in render_messages


def test_tts_playback_keeps_aria_pressed_synchronized():
    funcs = "\n".join(
        _function_body(UI_JS, name)
        for name in (
            "_stripForTTS",
            "_splitForTTS",
            "_beginTtsPlayback",
            "_ownsTtsPlayback",
            "_stopActivePlaybackAudio",
            "_buildBrowserUtterance",
            "speakMessage",
            "stopTTS",
        )
    )
    script = f"""
    let _ttsSpeaking = false;
    let _ttsCurrentUtterance = null;
    let _ttsChunkQueue = [];
    let _ttsChunkIndex = 0;
    let _ttsActiveBtn = null;
    let _ttsGeneration = 0;
    let _playingEdgeAudio = null;
    const localStorage = {{ getItem() {{ return 'browser'; }} }};
    const window = {{
      speechSynthesis: {{ speak() {{}}, cancel() {{}}, getVoices() {{ return []; }} }},
    }};
    const speechSynthesis = window.speechSynthesis;
    function SpeechSynthesisUtterance(text) {{ this.text = text; }}
    function t(k) {{ return k; }}
    function showToast() {{}}
    const attrs = {{ 'aria-pressed': 'false' }};
    const row = {{ dataset: {{ rawText: 'Hello from Hermes' }} }};
    const btn = {{
      dataset: {{ speaking: '0' }},
      closest() {{ return row; }},
      setAttribute(k, v) {{ attrs[k] = String(v); }},
      getAttribute(k) {{ return attrs[k]; }},
    }};
    const document = {{
      querySelectorAll() {{ return btn.dataset.speaking === '1' ? [btn] : []; }},
    }};
    {funcs}
    speakMessage(btn);
    const activePressed = btn.getAttribute('aria-pressed');
    const activeSpeaking = btn.dataset.speaking;
    stopTTS();
    const stoppedPressed = btn.getAttribute('aria-pressed');
    const stoppedSpeaking = btn.dataset.speaking;
    console.log(JSON.stringify({{ activePressed, activeSpeaking, stoppedPressed, stoppedSpeaking }}));
    """
    out = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert out == {
        "activePressed": "true",
        "activeSpeaking": "1",
        "stoppedPressed": "false",
        "stoppedSpeaking": "0",
    }


def test_inline_message_editor_and_code_copy_buttons_are_accessible():
    edit_fn = _function_body(UI_JS, "editMessage")
    assert "ta.setAttribute('aria-label', (typeof t==='function'&&t('edit_message'))||'Edit message');" in edit_fn
    assert '<button type="button" class="msg-edit-send">Send edit</button><button type="button" class="msg-edit-cancel">Cancel</button>' in edit_fn

    copy_fn = _function_body(UI_JS, "addCopyButtons")
    assert "btn.type='button';" in copy_fn
    assert "btn.setAttribute('aria-label',t('copy'));" in copy_fn
    assert "btn.setAttribute('aria-label',t('copied'));" in copy_fn


def test_message_actions_and_footers_reveal_on_focus_within():
    assert ".msg-row:hover .msg-actions,.msg-row:focus-within .msg-actions{opacity:1;}" in STYLE_CSS
    assert ".msg-action-btn:focus-visible{" in STYLE_CSS
    assert ".msg-row[data-role=\"assistant\"]:focus-within .msg-foot," in STYLE_CSS
    assert ".msg-row[data-role=\"assistant\"]:focus-within .msg-foot-with-usage .msg-actions" in STYLE_CSS


def test_copy_msg_announces_copied_and_updates_aria_label():
    funcs = "\n".join(
        _function_body(UI_JS, name)
        for name in ("_announceA11yPolite", "copyMsg")
    )
    script = f"""
    let _a11yPoliteClearTimer = null;
    const announcer = {{ textContent: '' }};
    function $(id) {{ return id === 'a11yAnnouncer' ? announcer : null; }}
    function _copyText() {{ return Promise.resolve(); }}
    function li(name) {{ return `<svg data-icon="${{name}}"></svg>`; }}
    function t(k) {{ return k === 'copied' ? 'Copied!' : k === 'copy' ? 'Copy' : k; }}
    function showToast() {{}}
    function requestAnimationFrame(fn) {{ fn(); }}
    let resetFn = null;
    function setTimeout(fn, ms) {{
      if (ms === 1500) resetFn = fn;
      return 1;
    }}
    function clearTimeout() {{}}
    const attrs = {{ 'aria-label': 'Copy' }};
    const row = {{ dataset: {{ rawText: 'Message text' }} }};
    const btn = {{
      innerHTML: '<svg data-icon="copy"></svg>',
      style: {{}},
      closest() {{ return row; }},
      getAttribute(k) {{ return attrs[k]; }},
      setAttribute(k, v) {{ attrs[k] = String(v); }},
    }};
    {funcs}
    copyMsg(btn);
    setImmediate(() => {{
      const copiedLabel = btn.getAttribute('aria-label');
      const announced = announcer.textContent;
      if (resetFn) resetFn();
      const restoredLabel = btn.getAttribute('aria-label');
      console.log(JSON.stringify({{ copiedLabel, announced, restoredLabel }}));
    }});
    """
    out = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert out == {
        "copiedLabel": "Copied!",
        "announced": "Copied!",
        "restoredLabel": "Copy",
    }


def test_a11y_module_registered_in_index_html_and_sw():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    sw = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
    a11y_pos = html.index('static/a11y.js?v=__WEBUI_VERSION__')
    ui_pos = html.index('static/ui.js?v=__WEBUI_VERSION__')
    assert a11y_pos < ui_pos, "static/a11y.js must load before static/ui.js"
    assert "'./static/a11y.js' + VQ" in sw
    assert 'id="a11yAnnouncer" class="sr-only" role="status" aria-live="polite" aria-atomic="true"' in html
    assert 'id="a11yAlertAnnouncer" class="sr-only" role="alert" aria-live="assertive" aria-atomic="true"' in html
    assert 'id="composerStatus" role="status" aria-live="polite" aria-atomic="true"' in html
    assert 'id="toast" role="status" aria-live="polite" aria-atomic="true"' in html
    assert 'id="reconnectBanner" role="status" aria-live="polite"' in html


def test_a11y_stream_lifecycle_and_tool_throttling():
    a11y_js = (ROOT / "static" / "a11y.js").read_text(encoding="utf-8")
    script = f"""
    const elements = {{
      a11yAnnouncer: {{ textContent: '', setAttribute() {{}} }},
      a11yAlertAnnouncer: {{ textContent: '', setAttribute() {{}} }},
      messages: {{
        attrs: {{}},
        setAttribute(k, v) {{ this.attrs[k] = String(v); }},
      }},
      liveAssistantTurn: {{
        attrs: {{}},
        setAttribute(k, v) {{ this.attrs[k] = String(v); }},
      }},
    }};
    globalThis.window = globalThis;
    globalThis.document = {{
      getElementById(id) {{ return elements[id] || null; }},
      activeElement: null,
      body: {{}},
    }};
    globalThis.S = {{ session: {{ session_id: 's1' }} }};
    globalThis.assistantDisplayName = () => 'Hermes';
    globalThis.t = (k) => ({{
      thinking: 'Thinking',
      a11y_running_tool: 'Running tool',
      a11y_tool_failed: 'Tool failed',
      a11y_response_complete: 'Response complete',
      a11y_response_cancelled: 'Response cancelled',
      a11y_response_error: 'Response interrupted by an error',
    }})[k] || k;
    let fakeNow = 10000;
    Date.now = () => fakeNow;
    globalThis.requestAnimationFrame = (fn) => fn();
    globalThis.setTimeout = () => 1;
    globalThis.clearTimeout = () => {{}};

    {a11y_js}

    const log = [];
    a11yOnStreamStart('s1');
    log.push({{
      phase: 'start',
      polite: elements.a11yAnnouncer.textContent,
      busy: elements.messages.attrs['aria-busy'],
    }});

    // Reconnect on same active session should not re-announce thinking
    elements.a11yAnnouncer.textContent = '';
    a11yOnStreamStart('s1', {{ reconnecting: true }});
    log.push({{
      phase: 'reconnect',
      polite: elements.a11yAnnouncer.textContent,
    }});

    // First tool call announces immediately
    a11yOnToolStart({{ name: 'terminal_exec', preview: 'pytest' }}, 's1');
    log.push({{
      phase: 'tool1',
      polite: elements.a11yAnnouncer.textContent,
    }});

    // Second tool call 500ms later is throttled
    elements.a11yAnnouncer.textContent = '';
    fakeNow += 500;
    a11yOnToolStart({{ name: 'read_file', preview: 'README.md' }}, 's1');
    log.push({{
      phase: 'tool2_throttled',
      polite: elements.a11yAnnouncer.textContent,
    }});

    // Third tool call after throttle window announces
    fakeNow += 3000;
    a11yOnToolStart({{ name: 'search_files' }}, 's1');
    log.push({{
      phase: 'tool3_after_window',
      polite: elements.a11yAnnouncer.textContent,
    }});

    // Failed tool completion announces error
    a11yOnToolComplete({{ name: 'terminal_exec', is_error: true }}, 's1');
    log.push({{
      phase: 'tool_error',
      polite: elements.a11yAnnouncer.textContent,
    }});

    // Stream done clears aria-busy and announces completion
    a11yOnStreamDone('s1');
    log.push({{
      phase: 'done',
      polite: elements.a11yAnnouncer.textContent,
      busy: elements.messages.attrs['aria-busy'],
    }});

    // Stream error announces assertively
    a11yOnStreamStart('s1');
    a11yOnStreamError('s1', 'Connection lost');
    log.push({{
      phase: 'error',
      assertive: elements.a11yAlertAnnouncer.textContent,
      busy: elements.messages.attrs['aria-busy'],
    }});

    console.log(JSON.stringify(log));
    """
    out = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert out == [
        {"phase": "start", "polite": "Hermes: Thinking…", "busy": "true"},
        {"phase": "reconnect", "polite": ""},
        {"phase": "tool1", "polite": "Running tool: terminal exec (pytest)"},
        {"phase": "tool2_throttled", "polite": ""},
        {"phase": "tool3_after_window", "polite": "Running tool: search files"},
        {"phase": "tool_error", "polite": "Tool failed: terminal exec"},
        {"phase": "done", "polite": "Hermes: Response complete", "busy": "false"},
        {
            "phase": "error",
            "assertive": "Response interrupted by an error: Connection lost",
            "busy": "false",
        },
    ]


def test_live_run_status_hides_ticking_timer_from_screen_readers():
    fn_src = _function_body(UI_JS, "_renderLiveRunStatusContent")
    assert 'class="live-run-status-text lf-time" aria-hidden="true"' in fn_src
    assert 'class="live-run-status-dot tool-card-running-dot" aria-hidden="true"' in fn_src


def test_a11y_spoken_response_on_done_short_code_and_long():
    a11y_js = (ROOT / "static" / "a11y.js").read_text(encoding="utf-8")
    script = f"""
    const elements = {{
      a11yAnnouncer: {{ textContent: '', setAttribute() {{}} }},
      messages: {{ setAttribute() {{}} }},
    }};
    globalThis.window = globalThis;
    globalThis.document = {{
      getElementById(id) {{ return elements[id] || null; }},
      body: {{}},
    }};
    globalThis.S = {{ session: {{ session_id: 's1' }} }};
    globalThis.assistantDisplayName = () => 'Hermes';
    globalThis.t = (k) => ({{
      a11y_response_complete: 'Response complete',
      a11y_code_block_omitted: 'Code block omitted.',
      a11y_full_response_in_transcript: 'Full response ready in transcript.',
    }})[k] || k;
    globalThis.requestAnimationFrame = (fn) => fn();
    let scheduledClearTimers = 0;
    globalThis.setTimeout = () => {{ scheduledClearTimers++; return 1; }};
    globalThis.clearTimeout = () => {{}};

    {a11y_js}

    // 1. Short/medium reply with inline code is spoken in full without auto-clear timer
    a11yOnStreamDone('s1', {{
      message: {{ role: 'assistant', content: 'Run `pytest` to verify **all** checks pass.' }},
    }});
    const shortReply = elements.a11yAnnouncer.textContent;
    const timersAfterShort = scheduledClearTimers;

    // 2. Reply containing a fenced code block replaces code block and adds transcript notice
    a11yOnStreamDone('s1', {{
      message: {{ role: 'assistant', content: 'Here is the fix:\\n```python\\nprint(1)\\n```\\nTry it now.' }},
    }});
    const codeReply = elements.a11yAnnouncer.textContent;

    // 3. Very long reply is truncated at sentence boundary with transcript notice
    const longParagraph = 'First sentence is clear and concise. ' + ('This is additional detail that makes the response exceed the threshold. ').repeat(12);
    a11yOnStreamDone('s1', {{
      message: {{ role: 'assistant', content: longParagraph }},
      maxChars: 120,
    }});
    const longReply = elements.a11yAnnouncer.textContent;

    console.log(JSON.stringify({{ shortReply, timersAfterShort, codeReply, longReply }}));
    """
    out = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert out["shortReply"] == "Hermes: Run pytest to verify all checks pass."
    assert out["timersAfterShort"] == 0, "spoken response must not schedule a short auto-clear timer"
    assert out["codeReply"] == (
        "Hermes: Here is the fix: Code block omitted. Try it now. "
        "Full response ready in transcript."
    )
    assert out["longReply"].startswith("Hermes: First sentence is clear and concise.")
    assert out["longReply"].endswith("Full response ready in transcript.")
    assert len(out["longReply"]) < 200



