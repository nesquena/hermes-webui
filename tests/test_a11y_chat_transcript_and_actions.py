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


def _run_node(script: str):
    return json.loads(
        subprocess.check_output(["node", "-"], input=script, text=True)
    )


def test_assistant_role_header_exposes_level3_heading_and_hides_monogram():
    fn = _function_body(UI_JS, "_assistantRoleHtml")
    script = f"""
    function assistantDisplayName() {{ return 'Hermes'; }}
    function isTpsDisplayEnabled() {{ return true; }}
    function esc(s) {{ return String(s || ''); }}
    {fn}
    console.log(JSON.stringify({{ html: _assistantRoleHtml('2026-10-18 17:00', '42.0 t/s') }}));
    """
    out = _run_node(script)
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
    out = _run_node(script)
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
        for name in ("_announceA11yPolite", "_applyButtonCopiedFeedback", "copyMsg")
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
    out = _run_node(script)
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
    out = _run_node(script)
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
    out = _run_node(script)
    assert out["shortReply"] == "Hermes: Run pytest to verify all checks pass."
    assert out["timersAfterShort"] == 0, "spoken response must not schedule a short auto-clear timer"
    assert out["codeReply"] == (
        "Hermes: Here is the fix: Code block omitted. Try it now. "
        "Full response ready in transcript."
    )
    assert out["longReply"].startswith("Hermes: First sentence is clear and concise.")
    assert out["longReply"].endswith("Full response ready in transcript.")
    assert len(out["longReply"]) < 200


def test_a11y_navigation_and_recovery_lifecycle_retires_busy_and_start_state():
    a11y_js = (ROOT / "static" / "a11y.js").read_text(encoding="utf-8")
    script = f"""
    const elements = {{
      a11yAnnouncer: {{ textContent: '', setAttribute() {{}} }},
      a11yAlertAnnouncer: {{ textContent: '', setAttribute() {{}} }},
      messages: {{
        attrs: {{}},
        setAttribute(k, v) {{ this.attrs[k] = String(v); }},
        getAttribute(k) {{ return this.attrs[k]; }},
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
    globalThis.S = {{ session: {{ session_id: 'sessionA' }} }};
    globalThis.assistantDisplayName = () => 'Hermes';
    globalThis.t = (k) => ({{
      thinking: 'Thinking',
      a11y_response_complete: 'Response complete',
    }})[k] || k;
    let rAFCallbacks = [];
    globalThis.requestAnimationFrame = (fn) => {{ rAFCallbacks.push(fn); return rAFCallbacks.length; }};
    globalThis.cancelAnimationFrame = (id) => {{ rAFCallbacks[id - 1] = null; }};
    let timerId = 0;
    const timers = new Map();
    globalThis.setTimeout = (fn, ms) => {{
      const id = ++timerId;
      timers.set(id, fn);
      return id;
    }};
    globalThis.clearTimeout = (id) => {{ timers.delete(id); }};

    {a11y_js}

    const log = [];

    // 1. Live stream A starts: sets aria-busy="true" on messages and announces Thinking
    a11yOnStreamStart('sessionA', {{ streamId: 'stream1' }});
    rAFCallbacks.forEach(fn => fn && fn());
    rAFCallbacks = [];
    log.push({{
      step: 'start_A',
      busy: elements.messages.attrs['aria-busy'],
      announcement: elements.a11yAnnouncer.textContent,
    }});

    // 2. Composed navigation: switch from live A to existing idle B
    // S.session becomes B, stream A is closed and resets stream state, and idle B loads
    globalThis.S.session = {{ session_id: 'sessionB' }};
    a11yResetStreamState('sessionA');
    log.push({{
      step: 'switched_to_idle_B',
      busy: elements.messages.attrs['aria-busy'],
    }});

    // 3. Same-turn silent reconnect control:
    // Switch back to session A, reconnecting the SAME stream1 does not re-announce thinking
    globalThis.S.session = {{ session_id: 'sessionA' }};
    elements.a11yAnnouncer.textContent = '';
    a11yOnStreamStart('sessionA', {{ streamId: 'stream1', reconnecting: true }});
    rAFCallbacks.forEach(fn => fn && fn());
    rAFCallbacks = [];
    log.push({{
      step: 'reconnect_same_turn',
      busy: elements.messages.attrs['aria-busy'],
      announcement: elements.a11yAnnouncer.textContent,
    }});

    // 4. stream_end recovery finishes through fallback / restoreSettledSession without done
    // Simulates _setActivePaneIdleIfOwner: resets stream state
    a11yResetStreamState('sessionA', {{ streamId: 'stream1' }});
    log.push({{
      step: 'stream_end_recovery',
      busy: elements.messages.attrs['aria-busy'],
    }});

    // 5. Fresh turn on the SAME session (sessionA) with new streamId2:
    // Must announce Thinking and set aria-busy="true"
    elements.a11yAnnouncer.textContent = '';
    a11yOnStreamStart('sessionA', {{ streamId: 'stream2' }});
    rAFCallbacks.forEach(fn => fn && fn());
    rAFCallbacks = [];
    log.push({{
      step: 'fresh_same_sid_turn',
      busy: elements.messages.attrs['aria-busy'],
      announcement: elements.a11yAnnouncer.textContent,
    }});

    // 6. Retirement cancels/fences pending announcement:
    // Schedule a pending announcement with rAF while sessionA is active,
    // then switch to sessionC before rAF runs.
    elements.a11yAnnouncer.textContent = 'Existing session text';
    announceA11yPolite('Stale thinking for A', 2500, {{ sessionId: 'sessionA' }});
    globalThis.S.session = {{ session_id: 'sessionC' }};
    a11yResetStreamState('sessionA');
    rAFCallbacks.forEach(fn => fn && fn());
    rAFCallbacks = [];
    log.push({{
      step: 'fenced_announcement',
      announcement: elements.a11yAnnouncer.textContent,
    }});

    console.log(JSON.stringify(log));
    """
    out = _run_node(script)
    assert out == [
        {"step": "start_A", "busy": "true", "announcement": "Hermes: Thinking…"},
        {"step": "switched_to_idle_B", "busy": "false"},
        {"step": "reconnect_same_turn", "busy": "true", "announcement": ""},
        {"step": "stream_end_recovery", "busy": "false"},
        {"step": "fresh_same_sid_turn", "busy": "true", "announcement": "Hermes: Thinking…\u00a0"},
        {"step": "fenced_announcement", "announcement": ""},
    ]


def test_identical_consecutive_announcements_alternate_whitespace_for_screen_readers():
    a11y_js = (ROOT / "static" / "a11y.js").read_text(encoding="utf-8")
    script = f"""
    const elements = {{
      a11yAnnouncer: {{ textContent: '', setAttribute() {{}} }},
      a11yAlertAnnouncer: {{ textContent: '', setAttribute() {{}} }},
      messages: {{ attrs: {{}}, setAttribute(k, v) {{ this.attrs[k] = String(v); }} }},
      liveAssistantTurn: {{ attrs: {{}}, setAttribute(k, v) {{ this.attrs[k] = String(v); }} }},
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
      a11y_response_error: 'Response interrupted by an error',
      a11y_response_complete: 'Response complete',
    }})[k] || k;
    let rAFCallbacks = [];
    globalThis.requestAnimationFrame = (fn) => {{ rAFCallbacks.push(fn); return rAFCallbacks.length; }};
    globalThis.cancelAnimationFrame = (id) => {{ rAFCallbacks[id - 1] = null; }};
    globalThis.setTimeout = () => 1;
    globalThis.clearTimeout = () => {{}};

    {a11y_js}

    const log = [];

    // --- Case 1: Identical consecutive errors from Hermes ---
    // Error 1
    a11yOnStreamError('s1', 'Rate limit exceeded');
    rAFCallbacks.forEach(fn => fn && fn());
    rAFCallbacks = [];
    log.push({{
      kind: 'error1',
      text: elements.a11yAlertAnnouncer.textContent,
    }});

    // Error 2 (identical error from Hermes)
    a11yOnStreamError('s1', 'Rate limit exceeded');
    rAFCallbacks.forEach(fn => fn && fn());
    rAFCallbacks = [];
    log.push({{
      kind: 'error2',
      text: elements.a11yAlertAnnouncer.textContent,
    }});

    // Error 3 (identical error from Hermes again)
    a11yOnStreamError('s1', 'Rate limit exceeded');
    rAFCallbacks.forEach(fn => fn && fn());
    rAFCallbacks = [];
    log.push({{
      kind: 'error3',
      text: elements.a11yAlertAnnouncer.textContent,
    }});

    // --- Case 2: Identical consecutive responses from Hermes ---
    // Response 1
    a11yOnStreamDone('s1', {{
      message: {{ role: 'assistant', content: 'Yes, that is correct.' }},
    }});
    rAFCallbacks.forEach(fn => fn && fn());
    rAFCallbacks = [];
    log.push({{
      kind: 'reply1',
      text: elements.a11yAnnouncer.textContent,
    }});

    // Response 2 (identical reply)
    a11yOnStreamDone('s1', {{
      message: {{ role: 'assistant', content: 'Yes, that is correct.' }},
    }});
    rAFCallbacks.forEach(fn => fn && fn());
    rAFCallbacks = [];
    log.push({{
      kind: 'reply2',
      text: elements.a11yAnnouncer.textContent,
    }});

    console.log(JSON.stringify(log));
    """
    out = _run_node(script)
    base_err = "Response interrupted by an error: Rate limit exceeded"
    base_reply = "Hermes: Yes, that is correct."
    assert out == [
        {"kind": "error1", "text": base_err},
        {"kind": "error2", "text": f"{base_err}\u00a0"},
        {"kind": "error3", "text": base_err},
        {"kind": "reply1", "text": base_reply},
        {"kind": "reply2", "text": f"{base_reply}\u00a0"},
    ]
    for item in out:
        assert item["text"].strip() in (base_err, base_reply)


def test_a11y_compression_rotation_admits_continuation_and_rejects_unrelated():
    a11y_js = (ROOT / "static" / "a11y.js").read_text(encoding="utf-8")
    script = f"""
    const elements = {{
      a11yAnnouncer: {{ textContent: '', setAttribute() {{}} }},
      messages: {{
        attrs: {{}},
        setAttribute(k, v) {{ this.attrs[k] = String(v); }},
        getAttribute(k) {{ return this.attrs[k]; }},
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
    globalThis.S = {{ session: {{ session_id: 'sid_parent' }} }};
    globalThis.assistantDisplayName = () => 'Hermes';
    globalThis.t = (k) => ({{
      thinking: 'Thinking',
      a11y_response_complete: 'Response complete',
    }})[k] || k;
    globalThis.requestAnimationFrame = (fn) => fn();
    globalThis.setTimeout = () => 1;
    globalThis.clearTimeout = () => {{}};

    {a11y_js}

    const log = [];

    // 1. Live stream starts on parent session sid_parent
    a11yOnStreamStart('sid_parent', {{ streamId: 'stream_compress' }});
    log.push({{
      step: 'started_parent',
      busy: elements.messages.attrs['aria-busy'],
    }});

    // 2. Context compression rotates session: frontend done listener admits transport
    // SID 'sid_parent', assigns S.session = sid_continuation, then calls a11yOnStreamDone
    globalThis.S.session = {{ session_id: 'sid_continuation' }};
    a11yOnStreamDone('sid_parent', {{
      completedSid: 'sid_continuation',
      streamId: 'stream_compress',
      message: {{ role: 'assistant', content: 'Here is the continuation response after compression.' }},
    }});
    log.push({{
      step: 'completed_continuation',
      busy: elements.messages.attrs['aria-busy'],
      announcement: elements.a11yAnnouncer.textContent,
    }});

    // 3. Negative control: unrelated background session completion
    // User is currently viewing sid_continuation. Background session sid_other finishes.
    elements.a11yAnnouncer.textContent = 'Still continuation announcement';
    elements.messages.attrs['aria-busy'] = 'false';
    a11yOnStreamDone('sid_other', {{
      completedSid: 'sid_other',
      streamId: 'stream_other',
      message: {{ role: 'assistant', content: 'Background answer that must not appear.' }},
    }});
    log.push({{
      step: 'unrelated_background_rejected',
      announcement: elements.a11yAnnouncer.textContent,
      busy: elements.messages.attrs['aria-busy'],
    }});

    console.log(JSON.stringify(log));
    """
    out = _run_node(script)
    assert out == [
        {"step": "started_parent", "busy": "true"},
        {
            "step": "completed_continuation",
            "busy": "false",
            "announcement": "Hermes: Here is the continuation response after compression.",
        },
        {
            "step": "unrelated_background_rejected",
            "announcement": "Still continuation announcement",
            "busy": "false",
        },
    ]


def test_repeated_copy_handlers_preserve_canonical_aria_label_and_drain():
    funcs = "\n".join(
        _function_body(UI_JS, name)
        for name in (
            "_announceA11yPolite",
            "_applyButtonCopiedFeedback",
            "copyMsg",
            "copyStatusSessionId",
            "_copyThinkingText",
        )
    )
    script = f"""
    let _a11yPoliteClearTimer = null;
    const announcer = {{ textContent: '' }};
    function $(id) {{ return id === 'a11yAnnouncer' ? announcer : null; }}
    function _copyText() {{ return Promise.resolve(); }}
    function li(name) {{ return `<svg data-icon="${{name}}"></svg>`; }}
    function t(k) {{ return k === 'copied' ? 'Copied!' : k === 'copy' ? 'Copy' : k; }}
    let toastMsg = null;
    function showToast(msg) {{ toastMsg = msg; }}
    function requestAnimationFrame(fn) {{ fn(); }}

    let scheduledTimers = new Map();
    let timerCounter = 0;
    function setTimeout(fn, ms) {{
      const id = ++timerCounter;
      scheduledTimers.set(id, fn);
      return id;
    }}
    function clearTimeout(id) {{
      scheduledTimers.delete(id);
    }}

    function createMockBtn(initialLabel) {{
      const attrs = {{ 'aria-label': initialLabel }};
      const classList = new Set();
      return {{
        innerHTML: '<svg data-icon="copy"></svg>',
        style: {{ color: '' }},
        classList: {{
          add(c) {{ classList.add(c); }},
          remove(c) {{ classList.delete(c); }},
          contains(c) {{ return classList.has(c); }},
        }},
        isConnected: true,
        getAttribute(k) {{ return attrs[k]; }},
        setAttribute(k, v) {{ attrs[k] = String(v); }},
      }};
    }}

    {funcs}

    const results = [];

    // --- Case 1: copyMsg repeated click before expiry ---
    const msgBtn = createMockBtn('Copy');
    const msgRow = {{ dataset: {{ rawText: 'Hello test message' }} }};
    msgBtn.closest = () => msgRow;

    copyMsg(msgBtn);
    setImmediate(() => {{
      const firstClickLabel = msgBtn.getAttribute('aria-label');
      // Second click before 1500ms timer fires
      copyMsg(msgBtn);
      setImmediate(() => {{
        const secondClickLabel = msgBtn.getAttribute('aria-label');
        // Drain all timers (simulating timeout expiry)
        for (const [id, fn] of [...scheduledTimers.entries()]) {{
          scheduledTimers.delete(id);
          fn();
        }}
        const restoredLabel = msgBtn.getAttribute('aria-label');
        results.push({{
          handler: 'copyMsg',
          firstClickLabel,
          secondClickLabel,
          restoredLabel,
        }});

        // --- Case 2: copyStatusSessionId repeated click before expiry ---
        const statusBtn = createMockBtn('Copy session ID');
        statusBtn.getAttribute = (k) => k === 'data-copy-status-session' ? 'session_12345' : statusBtn.attrs ? statusBtn.attrs[k] : 'Copy session ID';
        statusBtn.attrs = {{ 'aria-label': 'Copy session ID' }};
        statusBtn.setAttribute = (k, v) => {{ statusBtn.attrs[k] = String(v); }};

        copyStatusSessionId(statusBtn);
        setImmediate(() => {{
          copyStatusSessionId(statusBtn);
          setImmediate(() => {{
            for (const [id, fn] of [...scheduledTimers.entries()]) {{
              scheduledTimers.delete(id);
              fn();
            }}
            results.push({{
              handler: 'copyStatusSessionId',
              restoredLabel: statusBtn.attrs['aria-label'],
            }});

            // --- Case 3: _copyThinkingText repeated click before expiry ---
            const thinkBtn = createMockBtn('Copy thinking');
            const preEl = {{ textContent: 'Thinking process...' }};
            const cardEl = {{ querySelector: () => preEl }};
            thinkBtn.closest = () => cardEl;

            _copyThinkingText(thinkBtn);
            setImmediate(() => {{
              _copyThinkingText(thinkBtn);
              setImmediate(() => {{
                for (const [id, fn] of [...scheduledTimers.entries()]) {{
                  scheduledTimers.delete(id);
                  fn();
                }}
                results.push({{
                  handler: '_copyThinkingText',
                  restoredLabel: thinkBtn.getAttribute('aria-label'),
                }});

                // --- Case 4: Disconnected control before timer expiry ---
                const disBtn = createMockBtn('Copy');
                disBtn.closest = () => msgRow;
                copyMsg(disBtn);
                setImmediate(() => {{
                  disBtn.isConnected = false;
                  for (const [id, fn] of [...scheduledTimers.entries()]) {{
                    scheduledTimers.delete(id);
                    fn();
                  }}
                  results.push({{
                    handler: 'disconnected',
                    hadError: false,
                  }});

                  console.log(JSON.stringify(results));
                }});
              }});
            }});
          }});
        }});
      }});
    }});
    """
    out = _run_node(script)
    assert out == [
        {
            "handler": "copyMsg",
            "firstClickLabel": "Copied!",
            "secondClickLabel": "Copied!",
            "restoredLabel": "Copy",
        },
        {
            "handler": "copyStatusSessionId",
            "restoredLabel": "Copy session ID",
        },
        {
            "handler": "_copyThinkingText",
            "restoredLabel": "Copy thinking",
        },
        {
            "handler": "disconnected",
            "hadError": False,
        },
    ]


def _messages_stream_slice() -> str:
    messages_js = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    start = messages_js.index("const LIVE_STREAMS={};")
    end = messages_js.index("\nfunction transcript(){", start)
    is_current = _function_body(messages_js, "_isSessionCurrentPane")
    is_viewed = _function_body(messages_js, "_isSessionActivelyViewed")
    return "\n".join([is_current, is_viewed, messages_js[start:end]])


def test_attach_live_stream_and_reconnect_preserve_active_turn_and_retire_old_stream_before_successor():
    a11y_js = (ROOT / "static" / "a11y.js").read_text(encoding="utf-8")
    stream_js = _messages_stream_slice()
    script = f"""
    const elements = {{
      a11yAnnouncer: {{ textContent: '', setAttribute() {{}} }},
      a11yAlertAnnouncer: {{ textContent: '', setAttribute() {{}} }},
      messages: {{
        attrs: {{}},
        setAttribute(k, v) {{ this.attrs[k] = String(v); }},
        getAttribute(k) {{ return this.attrs[k]; }},
      }},
      liveAssistantTurn: {{
        attrs: {{}},
        setAttribute(k, v) {{ this.attrs[k] = String(v); }},
        querySelector() {{ return null; }},
      }},
    }};
    globalThis.window = globalThis;
    globalThis.location = {{ href: 'http://localhost:8789/' }};
    globalThis.document = {{
      baseURI: 'http://localhost:8789/',
      hidden: false,
      activeElement: null,
      body: {{}},
      getElementById(id) {{ return elements[id] || null; }},
      addEventListener() {{}},
    }};
    globalThis.$ = (id) => elements[id] || null;
    globalThis.S = {{
      session: {{ session_id: 'sid-a', pending_started_at: 1 }},
      messages: [{{ role: 'user', content: 'Hello' }}],
      activeStreamId: 'stream-1',
      toolCalls: [],
      busy: true,
    }};
    globalThis.INFLIGHT = {{}};
    globalThis._desktopBackgroundedForNotifications = false;
    globalThis.assistantDisplayName = () => 'Hermes';
    globalThis.t = (k) => ({{
      thinking: 'Thinking',
      a11y_response_complete: 'Response complete',
      a11y_response_error: 'Response interrupted by an error',
    }})[k] || k;

    let rAFCallbacks = [];
    globalThis.requestAnimationFrame = (fn) => {{ rAFCallbacks.push(fn); return rAFCallbacks.length; }};
    globalThis.cancelAnimationFrame = (id) => {{ if (id > 0 && id <= rAFCallbacks.length) rAFCallbacks[id - 1] = null; }};
    function flushRaf() {{
      const pending = rAFCallbacks;
      rAFCallbacks = [];
      for (const fn of pending) {{ if (fn) fn(); }}
    }}

    let scheduledTimers = new Map();
    let timerSeq = 0;
    globalThis.setTimeout = (fn, ms) => {{
      const id = ++timerSeq;
      scheduledTimers.set(id, {{ fn, ms }});
      return id;
    }};
    globalThis.clearTimeout = (id) => {{ scheduledTimers.delete(id); }};

    for (const noopName of [
      '_resumeSessionStreamAfterLiveChat',
      '_suspendSessionStreamForLiveChat',
      'resetTurnWorkspaceMutations',
      '_resetStreamScrollFollow',
      'ensureLiveWorklogShell',
      'showLiveRunStatus',
      '_clearLiveRunStatusTimer',
      'hideLiveRunStatus',
      'snapshotLiveTurnHtmlForSession',
      'saveInflightState',
      'clearInflightState',
      'clearInflight',
      'setComposerStatus',
      'setStatus',
      'renderSessionList',
      'renderMessages',
      'syncTopbar',
      'loadDir',
      'playNotificationSound',
      'sendBrowserNotification',
      '_markSessionViewed',
      'finalizeThinkingCard',
      'clearLiveToolCards',
      'removeThinking',
      '_clearApprovalPendingForSession',
      '_clearClarifyPendingForSession',
      'stopApprovalPolling',
      'hideApprovalCard',
      'stopClarifyPolling',
      'hideClarifyCard',
    ]) {{
      globalThis[noopName] = () => {{}};
    }}
    globalThis.setBusy = (val) => {{
      S.busy = Boolean(val);
      if (!val && typeof a11ySetStreamBusy === 'function') a11ySetStreamBusy(false);
    }};
    globalThis._isDocumentVisibleAndFocused = () => true;
    globalThis._deferStreamErrorIfOffline = () => false;
    globalThis._deferStreamErrorIfPageHidden = () => false;
    globalThis._approvalSessionId = null;
    globalThis._clarifySessionId = null;
    globalThis._pendingTitleUpdates = new Map();
    globalThis._pendingTitleExpectedCurrent = new Map();
    globalThis._queueDrainSid = null;
    globalThis.localStorage = {{ setItem() {{}} }};
    globalThis.api = async (url) => {{
      if (String(url).includes('/api/chat/stream/status')) {{
        return {{ active: true, replay_available: false }};
      }}
      return {{}};
    }};

    class MockEventSource {{
      static CONNECTING = 0;
      static OPEN = 1;
      static CLOSED = 2;
      static instances = [];
      constructor(url) {{
        this.url = String(url);
        this.readyState = MockEventSource.OPEN;
        this.listeners = new Map();
        MockEventSource.instances.push(this);
      }}
      addEventListener(name, fn) {{
        const list = this.listeners.get(name) || [];
        list.push(fn);
        this.listeners.set(name, list);
      }}
      async emit(name, payload = {{}}) {{
        const ev = {{ type: name, data: JSON.stringify(payload), lastEventId: '' }};
        for (const fn of this.listeners.get(name) || []) {{
          await fn(ev);
        }}
      }}
      close() {{ this.readyState = MockEventSource.CLOSED; }}
    }}
    globalThis.EventSource = MockEventSource;

    {a11y_js}
    {stream_js}

    (async () => {{
      const log = [];

      // 1. Attach stream-1 on sid-a via real attachLiveStream
      attachLiveStream('sid-a', 'stream-1', []);
      await Promise.resolve();
      flushRaf();
      const firstSource = MockEventSource.instances.at(-1);
      log.push({{
        step: 'attached_stream1',
        busy: elements.messages.attrs['aria-busy'],
        announcement: elements.a11yAnnouncer.textContent,
      }});

      // 2. Recoverable SSE error fires on firstSource -> calls _closeSource (reconnecting=true)
      elements.a11yAnnouncer.textContent = '';
      await firstSource.emit('error', {{}});
      const busyAfterCloseSource = elements.messages.attrs['aria-busy'];

      // Run the scheduled 1500ms _probeReconnect timer, which probes status and calls _wireSSE
      for (const [id, entry] of [...scheduledTimers.entries()]) {{
        if (entry.ms === 1500) {{
          scheduledTimers.delete(id);
          await entry.fn();
        }}
      }}
      flushRaf();
      const reconnectedSource = MockEventSource.instances.at(-1);
      log.push({{
        step: 'after_reconnect_wireSSE',
        reopenedNewSource: reconnectedSource !== firstSource,
        busyAfterCloseSource,
        busyAfterReconnect: elements.messages.attrs['aria-busy'],
        announcement: elements.a11yAnnouncer.textContent,
      }});

      // 3. Same-SID successor attachment: attachLiveStream('sid-a', 'stream-2')
      // Must retire stream-1 BEFORE publishing stream-2 so stream-2's state survives.
      S.activeStreamId = 'stream-2';
      elements.a11yAnnouncer.textContent = '';
      attachLiveStream('sid-a', 'stream-2', []);
      await Promise.resolve();
      // Even if a stale reset for stream-1 runs now, stream-2 must remain untouched
      a11yResetStreamState('sid-a', {{ streamId: 'stream-1' }});
      flushRaf();
      log.push({{
        step: 'successor_stream2_attached',
        busy: elements.messages.attrs['aria-busy'],
        announcement: elements.a11yAnnouncer.textContent,
      }});

      console.log(JSON.stringify(log));
    }})();
    """
    out = _run_node(script)
    assert out == [
        {
            "step": "attached_stream1",
            "busy": "true",
            "announcement": "Hermes: Thinking…",
        },
        {
            "step": "after_reconnect_wireSSE",
            "reopenedNewSource": True,
            "busyAfterCloseSource": "true",
            "busyAfterReconnect": "true",
            "announcement": "",
        },
        {
            "step": "successor_stream2_attached",
            "busy": "true",
            "announcement": "Hermes: Thinking…\u00a0",
        },
    ]


def test_done_listener_preserves_completion_across_stream_end_before_and_after_frame_and_fences_superseded():
    a11y_js = (ROOT / "static" / "a11y.js").read_text(encoding="utf-8")
    stream_js = _messages_stream_slice()
    script = f"""
    const elements = {{
      a11yAnnouncer: {{ textContent: '', setAttribute() {{}} }},
      a11yAlertAnnouncer: {{ textContent: '', setAttribute() {{}} }},
      messages: {{
        attrs: {{}},
        setAttribute(k, v) {{ this.attrs[k] = String(v); }},
        getAttribute(k) {{ return this.attrs[k]; }},
      }},
      liveAssistantTurn: {{
        attrs: {{}},
        setAttribute(k, v) {{ this.attrs[k] = String(v); }},
        querySelector() {{ return null; }},
      }},
    }};
    globalThis.window = globalThis;
    globalThis.location = {{ href: 'http://localhost:8789/' }};
    globalThis.document = {{
      baseURI: 'http://localhost:8789/',
      hidden: false,
      activeElement: null,
      body: {{}},
      getElementById(id) {{ return elements[id] || null; }},
      addEventListener() {{}},
    }};
    globalThis.$ = (id) => elements[id] || null;
    globalThis.S = {{
      session: {{ session_id: 'sid-a', pending_started_at: 1 }},
      messages: [{{ role: 'user', content: 'Hello' }}],
      activeStreamId: 'stream-a1',
      toolCalls: [],
      busy: true,
    }};
    globalThis.INFLIGHT = {{}};
    globalThis._desktopBackgroundedForNotifications = false;
    globalThis.assistantDisplayName = () => 'Hermes';
    globalThis.t = (k) => ({{
      thinking: 'Thinking',
      a11y_response_complete: 'Response complete',
    }})[k] || k;

    let rAFCallbacks = [];
    globalThis.requestAnimationFrame = (fn) => {{ rAFCallbacks.push(fn); return rAFCallbacks.length; }};
    globalThis.cancelAnimationFrame = (id) => {{ if (id > 0 && id <= rAFCallbacks.length) rAFCallbacks[id - 1] = null; }};
    function flushRaf() {{
      const pending = rAFCallbacks;
      rAFCallbacks = [];
      for (const fn of pending) {{ if (fn) fn(); }}
    }}

    globalThis.setTimeout = () => 1;
    globalThis.clearTimeout = () => {{}};

    for (const noopName of [
      '_resumeSessionStreamAfterLiveChat',
      '_suspendSessionStreamForLiveChat',
      'resetTurnWorkspaceMutations',
      '_resetStreamScrollFollow',
      'ensureLiveWorklogShell',
      'showLiveRunStatus',
      '_clearLiveRunStatusTimer',
      'hideLiveRunStatus',
      'snapshotLiveTurnHtmlForSession',
      'saveInflightState',
      'clearInflightState',
      'clearInflight',
      'setComposerStatus',
      'setStatus',
      'renderSessionList',
      'renderMessages',
      'syncTopbar',
      'loadDir',
      'playNotificationSound',
      'sendBrowserNotification',
      '_markSessionViewed',
      'finalizeThinkingCard',
      'clearLiveToolCards',
      'removeThinking',
      '_clearApprovalPendingForSession',
      '_clearClarifyPendingForSession',
      'stopApprovalPolling',
      'hideApprovalCard',
      'stopClarifyPolling',
      'hideClarifyCard',
    ]) {{
      globalThis[noopName] = () => {{}};
    }}
    globalThis.setBusy = (val) => {{
      S.busy = Boolean(val);
      if (!val && typeof a11ySetStreamBusy === 'function') a11ySetStreamBusy(false);
    }};
    globalThis._shouldUseLiveProseFade = () => false;
    globalThis._shouldFollowMessagesOnDomReplace = () => false;
    globalThis._isMessagePaneNearBottom = () => false;
    globalThis._isDocumentVisibleAndFocused = () => true;
    globalThis._deferStreamErrorIfOffline = () => false;
    globalThis._deferStreamErrorIfPageHidden = () => false;
    globalThis._completionNotificationPreviewText = () => '';
    globalThis._extractInlineThinkingFromContent = (content, reasoning) => ({{
      content: String(content || ''),
      displayText: String(content || ''),
      reasoning: String(reasoning || ''),
    }});
    globalThis._approvalSessionId = null;
    globalThis._clarifySessionId = null;
    globalThis._pendingTitleUpdates = new Map();
    globalThis._pendingTitleExpectedCurrent = new Map();
    globalThis._queueDrainSid = null;
    globalThis.localStorage = {{ setItem() {{}} }};
    globalThis.api = async () => ({{}});

    class MockEventSource {{
      static CONNECTING = 0;
      static OPEN = 1;
      static CLOSED = 2;
      static instances = [];
      constructor(url) {{
        this.url = String(url);
        this.readyState = MockEventSource.OPEN;
        this.listeners = new Map();
        MockEventSource.instances.push(this);
      }}
      addEventListener(name, fn) {{
        const list = this.listeners.get(name) || [];
        list.push(fn);
        this.listeners.set(name, list);
      }}
      async emit(name, payload = {{}}) {{
        const ev = {{ type: name, data: JSON.stringify(payload), lastEventId: '' }};
        for (const fn of this.listeners.get(name) || []) {{
          await fn(ev);
        }}
      }}
      close() {{ this.readyState = MockEventSource.CLOSED; }}
    }}
    globalThis.EventSource = MockEventSource;

    {a11y_js}
    {stream_js}

    (async () => {{
      const log = [];

      // Case 1: Ordinary A->A completion with stream_end BEFORE the completion RAF frame
      attachLiveStream('sid-a', 'stream-a1', []);
      await Promise.resolve();
      flushRaf();
      const srcA1 = MockEventSource.instances.at(-1);

      await srcA1.emit('done', {{
        status: 'completed',
        stream_id: 'stream-a1',
        session: {{
          session_id: 'sid-a',
          messages: [
            {{ role: 'user', content: 'Hello' }},
            {{ role: 'assistant', content: 'Settled A-to-A before frame.' }},
          ],
          tool_calls: [],
        }},
      }});
      // Deliver trailing stream_end BEFORE flushRaf()
      await srcA1.emit('stream_end', {{ session_id: 'sid-a' }});
      flushRaf();
      log.push({{
        case: 'a_to_a_stream_end_before_frame',
        busy: elements.messages.attrs['aria-busy'],
        announcement: elements.a11yAnnouncer.textContent,
      }});

      // Case 2: Ordinary A->A completion with stream_end AFTER the completion RAF frame
      S.activeStreamId = 'stream-a2';
      S.busy = true;
      attachLiveStream('sid-a', 'stream-a2', []);
      await Promise.resolve();
      flushRaf();
      const srcA2 = MockEventSource.instances.at(-1);

      await srcA2.emit('done', {{
        status: 'completed',
        stream_id: 'stream-a2',
        session: {{
          session_id: 'sid-a',
          messages: [
            {{ role: 'user', content: 'Second' }},
            {{ role: 'assistant', content: 'Settled A-to-A after frame.' }},
          ],
          tool_calls: [],
        }},
      }});
      flushRaf();
      await srcA2.emit('stream_end', {{ session_id: 'sid-a' }});
      log.push({{
        case: 'a_to_a_stream_end_after_frame',
        busy: elements.messages.attrs['aria-busy'],
        announcement: elements.a11yAnnouncer.textContent,
      }});

      // Case 3: A->B compression rotation with stream_end BEFORE the completion RAF frame
      // plus an unrelated background stream finishing before the frame runs
      S.session = {{ session_id: 'sid-bg', pending_started_at: 1 }};
      S.activeStreamId = 'stream-bg';
      S.busy = true;
      attachLiveStream('sid-bg', 'stream-bg', []);
      await Promise.resolve();
      const srcBg = MockEventSource.instances.at(-1);
      // Keep srcBg alive in background while user views sid-parent
      S.session = {{ session_id: 'sid-parent', pending_started_at: 2 }};
      S.activeStreamId = 'stream-comp';
      S.busy = true;
      attachLiveStream('sid-parent', 'stream-comp', []);
      await Promise.resolve();
      flushRaf();
      const srcComp = MockEventSource.instances.at(-1);

      // Emit done on sid-parent rotating to sid-cont
      await srcComp.emit('done', {{
        status: 'completed',
        stream_id: 'stream-comp',
        session: {{
          session_id: 'sid-cont',
          messages: [
            {{ role: 'user', content: 'Compress' }},
            {{ role: 'assistant', content: 'Continuation reply after compression.' }},
          ],
          tool_calls: [],
        }},
      }});
      // Emit trailing stream_end on sid-parent BEFORE completion RAF frame
      await srcComp.emit('stream_end', {{ session_id: 'sid-parent' }});
      // Also emit done + stream_end on unrelated background stream BEFORE completion RAF frame
      await srcBg.emit('done', {{
        status: 'completed',
        stream_id: 'stream-bg',
        session: {{
          session_id: 'sid-bg',
          messages: [{{ role: 'assistant', content: 'Unrelated background reply.' }}],
          tool_calls: [],
        }},
      }});
      await srcBg.emit('stream_end', {{ session_id: 'sid-bg' }});

      flushRaf();
      log.push({{
        case: 'a_to_b_stream_end_and_bg_before_frame',
        currentSid: S.session.session_id,
        busy: elements.messages.attrs['aria-busy'],
        announcement: elements.a11yAnnouncer.textContent,
      }});

      // Case 4: Superseded view owner fences pending terminal completion RAF
      S.session = {{ session_id: 'sid-cont', pending_started_at: 3 }};
      S.activeStreamId = 'stream-superseded';
      S.busy = true;
      attachLiveStream('sid-cont', 'stream-superseded', []);
      await Promise.resolve();
      flushRaf();
      const srcSup = MockEventSource.instances.at(-1);
      elements.a11yAnnouncer.textContent = '';

      await srcSup.emit('done', {{
        status: 'completed',
        stream_id: 'stream-superseded',
        session: {{
          session_id: 'sid-cont',
          messages: [{{ role: 'assistant', content: 'Should be fenced on switch.' }}],
          tool_calls: [],
        }},
      }});
      // User switches to idle sid-other before completion RAF runs
      S.session = {{ session_id: 'sid-other' }};
      a11yResetStreamState();
      flushRaf();
      log.push({{
        case: 'superseded_view_fenced',
        announcement: elements.a11yAnnouncer.textContent,
      }});

      // Case 5: Superseded turn owner (new stream on same session before completion RAF frame)
      S.session = {{ session_id: 'sid-other', pending_started_at: 4 }};
      S.activeStreamId = 'stream-turn-1';
      S.busy = true;
      attachLiveStream('sid-other', 'stream-turn-1', []);
      await Promise.resolve();
      flushRaf();
      const srcTurn1 = MockEventSource.instances.at(-1);

      await srcTurn1.emit('done', {{
        status: 'completed',
        stream_id: 'stream-turn-1',
        session: {{
          session_id: 'sid-other',
          messages: [{{ role: 'assistant', content: 'Turn 1 answer superseded by turn 2.' }}],
          tool_calls: [],
        }},
      }});
      // Before Turn 1's completion RAF runs, Turn 2 starts on sid-other
      S.activeStreamId = 'stream-turn-2';
      S.busy = true;
      attachLiveStream('sid-other', 'stream-turn-2', []);
      await Promise.resolve();
      await srcTurn1.emit('stream_end', {{ session_id: 'sid-other' }});
      flushRaf();
      log.push({{
        case: 'superseded_turn_replaced_by_new_thinking',
        busy: elements.messages.attrs['aria-busy'],
        announcement: elements.a11yAnnouncer.textContent.trim(),
      }});

      console.log(JSON.stringify(log));
    }})();
    """
    out = _run_node(script)
    assert out == [
        {
            "case": "a_to_a_stream_end_before_frame",
            "busy": "false",
            "announcement": "Hermes: Settled A-to-A before frame.",
        },
        {
            "case": "a_to_a_stream_end_after_frame",
            "busy": "false",
            "announcement": "Hermes: Settled A-to-A after frame.",
        },
        {
            "case": "a_to_b_stream_end_and_bg_before_frame",
            "currentSid": "sid-cont",
            "busy": "false",
            "announcement": "Hermes: Continuation reply after compression.",
        },
        {
            "case": "superseded_view_fenced",
            "announcement": "",
        },
        {
            "case": "superseded_turn_replaced_by_new_thinking",
            "busy": "true",
            "announcement": "Hermes: Thinking…",
        },
    ]


def test_polite_copy_does_not_invalidate_assertive_error_before_frame_or_before_expiry():
    a11y_js = (ROOT / "static" / "a11y.js").read_text(encoding="utf-8")
    stream_js = _messages_stream_slice()
    copy_funcs = "\n".join(
        _function_body(UI_JS, name)
        for name in (
            "_announceA11yPolite",
            "_applyButtonCopiedFeedback",
            "copyMsg",
        )
    )
    script = f"""
    const elements = {{
      a11yAnnouncer: {{ textContent: '', setAttribute() {{}} }},
      a11yAlertAnnouncer: {{ textContent: '', setAttribute() {{}} }},
      messages: {{
        attrs: {{}},
        setAttribute(k, v) {{ this.attrs[k] = String(v); }},
        getAttribute(k) {{ return this.attrs[k]; }},
      }},
      liveAssistantTurn: {{
        attrs: {{}},
        setAttribute(k, v) {{ this.attrs[k] = String(v); }},
        querySelector() {{ return null; }},
      }},
    }};
    globalThis.window = globalThis;
    globalThis.location = {{ href: 'http://localhost:8789/' }};
    globalThis.document = {{
      baseURI: 'http://localhost:8789/',
      hidden: false,
      activeElement: null,
      body: {{}},
      getElementById(id) {{ return elements[id] || null; }},
      addEventListener() {{}},
    }};
    globalThis.$ = (id) => elements[id] || null;
    globalThis.S = {{
      session: {{ session_id: 'sid-1', pending_started_at: 1 }},
      messages: [{{ role: 'user', content: 'Prompt' }}],
      activeStreamId: 'stream-err-1',
      toolCalls: [],
      busy: true,
    }};
    globalThis.INFLIGHT = {{}};
    globalThis._desktopBackgroundedForNotifications = false;
    globalThis.assistantDisplayName = () => 'Hermes';
    globalThis.li = (name) => `<svg data-icon="${{name}}"></svg>`;
    globalThis._copyText = () => Promise.resolve();
    globalThis.showToast = () => {{}};
    globalThis.t = (k) => ({{
      thinking: 'Thinking',
      copied: 'Copied!',
      copy: 'Copy',
      a11y_response_error: 'Response interrupted by an error',
    }})[k] || k;

    let rAFCallbacks = [];
    globalThis.requestAnimationFrame = (fn) => {{ rAFCallbacks.push(fn); return rAFCallbacks.length; }};
    globalThis.cancelAnimationFrame = (id) => {{ if (id > 0 && id <= rAFCallbacks.length) rAFCallbacks[id - 1] = null; }};
    function flushRaf() {{
      const pending = rAFCallbacks;
      rAFCallbacks = [];
      for (const fn of pending) {{ if (fn) fn(); }}
    }}

    let scheduledTimers = new Map();
    let timerSeq = 0;
    globalThis.setTimeout = (fn, ms) => {{
      const id = ++timerSeq;
      scheduledTimers.set(id, {{ fn, ms }});
      return id;
    }};
    globalThis.clearTimeout = (id) => {{ scheduledTimers.delete(id); }};

    for (const noopName of [
      '_resumeSessionStreamAfterLiveChat',
      '_suspendSessionStreamForLiveChat',
      'resetTurnWorkspaceMutations',
      '_resetStreamScrollFollow',
      'ensureLiveWorklogShell',
      'showLiveRunStatus',
      '_clearLiveRunStatusTimer',
      'hideLiveRunStatus',
      'snapshotLiveTurnHtmlForSession',
      'saveInflightState',
      'clearInflightState',
      'clearInflight',
      'setComposerStatus',
      'setStatus',
      'renderSessionList',
      'renderMessages',
      'syncTopbar',
      'loadDir',
      'playNotificationSound',
      'sendBrowserNotification',
      '_markSessionViewed',
      'finalizeThinkingCard',
      'clearLiveToolCards',
      'removeThinking',
      '_clearApprovalPendingForSession',
      '_clearClarifyPendingForSession',
      'stopApprovalPolling',
      'hideApprovalCard',
      'stopClarifyPolling',
      'hideClarifyCard',
    ]) {{
      globalThis[noopName] = () => {{}};
    }}
    globalThis.setBusy = (val) => {{
      S.busy = Boolean(val);
      if (!val && typeof a11ySetStreamBusy === 'function') a11ySetStreamBusy(false);
    }};
    globalThis._shouldUseLiveProseFade = () => false;
    globalThis._shouldFollowMessagesOnDomReplace = () => false;
    globalThis._isMessagePaneNearBottom = () => false;
    globalThis._isDocumentVisibleAndFocused = () => true;
    globalThis._deferStreamErrorIfOffline = () => false;
    globalThis._deferStreamErrorIfPageHidden = () => false;
    globalThis._approvalSessionId = null;
    globalThis._clarifySessionId = null;
    globalThis._pendingTitleUpdates = new Map();
    globalThis._pendingTitleExpectedCurrent = new Map();
    globalThis._queueDrainSid = null;
    globalThis.localStorage = {{ setItem() {{}} }};
    globalThis.api = async () => ({{}});

    class MockEventSource {{
      static CONNECTING = 0;
      static OPEN = 1;
      static CLOSED = 2;
      static instances = [];
      constructor(url) {{
        this.url = String(url);
        this.readyState = MockEventSource.OPEN;
        this.listeners = new Map();
        MockEventSource.instances.push(this);
      }}
      addEventListener(name, fn) {{
        const list = this.listeners.get(name) || [];
        list.push(fn);
        this.listeners.set(name, list);
      }}
      async emit(name, payload = {{}}) {{
        const ev = {{ type: name, data: JSON.stringify(payload), lastEventId: '' }};
        for (const fn of this.listeners.get(name) || []) {{
          await fn(ev);
        }}
      }}
      close() {{ this.readyState = MockEventSource.CLOSED; }}
    }}
    globalThis.EventSource = MockEventSource;

    let _a11yPoliteClearTimer = null;
    {a11y_js}
    {copy_funcs}
    {stream_js}

    const msgRow = {{ dataset: {{ rawText: 'Message to copy' }} }};
    const copyBtn = {{
      innerHTML: '<svg data-icon="copy"></svg>',
      style: {{ color: '' }},
      isConnected: true,
      attrs: {{ 'aria-label': 'Copy' }},
      getAttribute(k) {{ return this.attrs[k]; }},
      setAttribute(k, v) {{ this.attrs[k] = String(v); }},
      closest() {{ return msgRow; }},
    }};

    (async () => {{
      const log = [];

      // Case 1: Copy succeeds BEFORE the pending assertive error frame runs
      attachLiveStream('sid-1', 'stream-err-1', []);
      await Promise.resolve();
      flushRaf();
      const src1 = MockEventSource.instances.at(-1);

      // Emit apperror -> schedules assertive error RAF (not yet flushed)
      await src1.emit('apperror', {{
        type: 'rate_limit',
        status: 'rate_limit',
        message: 'Rate limit reached',
        session_id: 'sid-1',
      }});
      // Trigger copyMsg and let its microtask resolve BEFORE flushing RAF
      copyMsg(copyBtn);
      await Promise.resolve();

      // Now flush RAF: both polite ("Copied!") and assertive error must publish
      flushRaf();
      log.push({{
        case: 'copy_before_error_frame',
        polite: elements.a11yAnnouncer.textContent,
        assertive: elements.a11yAlertAnnouncer.textContent,
      }});

      // Case 2: Copy succeeds AFTER assertive error publication, BEFORE assertive expiry timer
      // Trigger copyMsg again while the 4500ms assertive expiry timer is still pending
      copyMsg(copyBtn);
      await Promise.resolve();
      flushRaf();
      const assertiveBeforeExpiry = elements.a11yAlertAnnouncer.textContent;

      // Fire the 4500ms assertive expiry timer
      for (const [id, entry] of [...scheduledTimers.entries()]) {{
        if (entry.ms === 4500) {{
          scheduledTimers.delete(id);
          entry.fn();
        }}
      }}
      log.push({{
        case: 'copy_between_publication_and_expiry',
        assertiveBeforeExpiry: assertiveBeforeExpiry.trim(),
        assertiveAfterExpiry: elements.a11yAlertAnnouncer.textContent,
      }});

      // Case 3: Owner-retirement fence cancels both pending channels on session switch
      S.activeStreamId = 'stream-err-2';
      S.busy = true;
      attachLiveStream('sid-1', 'stream-err-2', []);
      await Promise.resolve();
      flushRaf();
      const src2 = MockEventSource.instances.at(-1);
      elements.a11yAnnouncer.textContent = '';
      elements.a11yAlertAnnouncer.textContent = '';

      await src2.emit('apperror', {{
        type: 'rate_limit',
        status: 'rate_limit',
        message: 'Second error',
        session_id: 'sid-1',
      }});
      copyMsg(copyBtn);
      await Promise.resolve();
      // Retire sid-1 by switching to sid-2 before RAFs run
      S.session = {{ session_id: 'sid-2' }};
      a11yResetStreamState('sid-1');
      flushRaf();
      log.push({{
        case: 'owner_retirement_fences_both_channels',
        polite: elements.a11yAnnouncer.textContent,
        assertive: elements.a11yAlertAnnouncer.textContent,
      }});

      console.log(JSON.stringify(log));
    }})();
    """
    out = _run_node(script)
    assert out == [
        {
            "case": "copy_before_error_frame",
            "polite": "Copied!",
            "assertive": "Response interrupted by an error: Rate limit reached",
        },
        {
            "case": "copy_between_publication_and_expiry",
            "assertiveBeforeExpiry": "Response interrupted by an error: Rate limit reached",
            "assertiveAfterExpiry": "",
        },
        {
            "case": "owner_retirement_fences_both_channels",
            "polite": "",
            "assertive": "",
        },
    ]




