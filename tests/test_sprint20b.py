"""
Sprint 21 Tests: Send button polish — hidden until content, pop-in animation,
icon-only circle design.
"""
import re
import urllib.request

from tests._pytest_port import BASE


def get_text(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.read().decode(), r.status


# ── index.html ────────────────────────────────────────────────────────────


def test_send_button_present():
    """btnSend must still exist in the DOM."""
    html, status = get_text("/")
    assert status == 200
    assert 'id="btnSend"' in html


def test_send_button_disabled_by_default():
    """btnSend must start disabled — enabled only when there is content."""
    html, _ = get_text("/")
    btn_match = re.search(r'id="btnSend"[^>]*>', html)
    assert btn_match, "btnSend element not found"
    assert 'disabled' in btn_match.group(0)


def test_send_button_no_text_label():
    """Send button must be icon-only — no visible 'Send' text label."""
    html, _ = get_text("/")
    # Find the full button element (from opening tag to closing tag)
    btn_open_end = html.find('>', html.find('id="btnSend"')) + 1
    btn_end = html.find('</button>', btn_open_end) + len('</button>')
    btn_inner = html[btn_open_end:btn_end]
    # Strip SVG content and any remaining tags; check visible text
    no_svg = re.sub(r'<svg[^>]*>.*?</svg>', '', btn_inner, flags=re.DOTALL)
    visible_text = re.sub(r'<[^>]+>', '', no_svg).strip()
    assert visible_text == '', f"Send button has visible text: {visible_text!r}"


def test_send_button_has_svg_icon():
    """Send button must have an SVG icon."""
    html, _ = get_text("/")
    btn_start = html.find('id="btnSend"')
    btn_end = html.find('</button>', btn_start) + len('</button>')
    btn_html = html[btn_start:btn_end]
    assert '<svg' in btn_html


def test_send_button_has_title_attribute():
    """btnSend must have a title attribute for accessibility (replaces text label)."""
    html, _ = get_text("/")
    btn_match = re.search(r'id="btnSend"[^>]*>', html)
    assert btn_match
    assert 'title=' in btn_match.group(0)


def test_send_button_svg_arrow_up():
    """Send button SVG should use an upward arrow (line + polyline or path)."""
    html, _ = get_text("/")
    btn_start = html.find('id="btnSend"')
    btn_end = html.find('</button>', btn_start) + len('</button>')
    btn_html = html[btn_start:btn_end]
    # Must have some directional shape element
    has_shape = ('<line' in btn_html or '<polyline' in btn_html or
                 '<polygon' in btn_html or '<path' in btn_html)
    assert has_shape, "Send button SVG missing directional shape"


# ── style.css ────────────────────────────────────────────────────────────


def test_send_btn_is_circle():
    """send-btn must use border-radius:50% for the circle shape."""
    css, status = get_text("/static/style.css")
    assert status == 200
    send_idx = css.find('.send-btn{')
    brace_open = css.find('{', send_idx)
    brace_close = css.find('}', brace_open)
    rule = css[brace_open:brace_close]
    assert 'border-radius:50%' in rule or 'border-radius: 50%' in rule


def test_send_btn_fixed_dimensions():
    """send-btn must have explicit width and height (icon-circle, not text-padded)."""
    css, _ = get_text("/static/style.css")
    send_idx = css.find('.send-btn{')
    brace_open = css.find('{', send_idx)
    brace_close = css.find('}', brace_open)
    rule = css[brace_open:brace_close]
    assert 'width:' in rule or 'width :' in rule
    assert 'height:' in rule or 'height :' in rule


def test_send_btn_no_old_padding():
    """send-btn must not use text padding layout (old pill style removed)."""
    css, _ = get_text("/static/style.css")
    send_idx = css.find('.send-btn{')
    brace_open = css.find('{', send_idx)
    brace_close = css.find('}', brace_open)
    rule = css[brace_open:brace_close]
    # Old style used padding:7px 18px — should be gone
    assert 'padding:7px' not in rule and 'padding: 7px' not in rule


def test_send_btn_accent_background():
    """send-btn background must use the accent color variable."""
    css, _ = get_text("/static/style.css")
    send_idx = css.find('.send-btn{')
    brace_open = css.find('{', send_idx)
    brace_close = css.find('}', brace_open)
    rule = css[brace_open:brace_close]
    assert 'var(--accent)' in rule or 'var(--blue)' in rule or '7cb9ff' in rule


def test_send_btn_has_transition():
    """send-btn must have transition for smooth hover/active states."""
    css, _ = get_text("/static/style.css")
    send_idx = css.find('.send-btn{')
    brace_open = css.find('{', send_idx)
    brace_close = css.find('}', brace_open)
    rule = css[brace_open:brace_close]
    assert 'transition' in rule


def test_send_btn_has_box_shadow():
    """send-btn must have a box-shadow glow effect."""
    css, _ = get_text("/static/style.css")
    send_idx = css.find('.send-btn{')
    brace_open = css.find('{', send_idx)
    brace_close = css.find('}', brace_open)
    rule = css[brace_open:brace_close]
    assert 'box-shadow' in rule


def test_send_btn_hover_has_scale():
    """send-btn:hover must use transform:scale for a satisfying hover effect."""
    css, _ = get_text("/static/style.css")
    hover_idx = css.find('.send-btn:hover{')
    brace_open = css.find('{', hover_idx)
    brace_close = css.find('}', brace_open)
    rule = css[brace_open:brace_close]
    assert 'scale' in rule


def test_send_btn_active_shrinks():
    """send-btn:active must scale down slightly for tactile press feedback."""
    css, _ = get_text("/static/style.css")
    active_idx = css.find('.send-btn:active{')
    brace_open = css.find('{', active_idx)
    brace_close = css.find('}', brace_open)
    rule = css[brace_open:brace_close]
    assert 'scale' in rule


def test_send_btn_disabled_rule_exists():
    """send-btn:disabled must still be styled."""
    css, _ = get_text("/static/style.css")
    assert '.send-btn:disabled' in css


def test_send_btn_visible_class_defined():
    """.send-btn.visible class must be defined for the pop-in animation."""
    css, _ = get_text("/static/style.css")
    assert '.send-btn.visible' in css


def test_send_pop_in_keyframes_defined():
    """@keyframes send-pop-in must be defined."""
    css, _ = get_text("/static/style.css")
    assert 'send-pop-in' in css
    assert '@keyframes' in css


def _extract_keyframe(css, name):
    """Extract the full @keyframes block for the given animation name."""
    # Find '@keyframes <name>' directly (forward search) to avoid hitting
    # an earlier keyframe when multiple are defined on the same line.
    kf_start = css.find('@keyframes ' + name)
    assert kf_start != -1, f"@keyframes {name} not found in CSS"
    depth = 0
    kf_end = kf_start
    for i, ch in enumerate(css[kf_start:], kf_start):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                kf_end = i
                break
    return css[kf_start:kf_end]


def test_send_pop_in_uses_scale():
    """send-pop-in keyframe must animate from a scaled-down state."""
    css, _ = get_text("/static/style.css")
    kf_rule = _extract_keyframe(css, 'send-pop-in')
    assert 'scale' in kf_rule


def test_send_pop_in_uses_opacity():
    """send-pop-in keyframe must fade in (opacity transition)."""
    css, _ = get_text("/static/style.css")
    kf_rule = _extract_keyframe(css, 'send-pop-in')
    assert 'opacity' in kf_rule


def test_send_btn_mobile_override_no_padding():
    """Mobile override for send-btn must not add text padding (keeps circle shape)."""
    css, _ = get_text("/static/style.css")
    # Find the @media block
    media_idx = css.find('@media')
    send_mobile_idx = css.find('.send-btn', media_idx)
    if send_mobile_idx == -1:
        return  # No mobile override, fine
    brace_open = css.find('{', send_mobile_idx)
    brace_close = css.find('}', brace_open)
    rule = css[brace_open:brace_close]
    assert 'padding:' not in rule and 'font-size' not in rule


# ── ui.js ─────────────────────────────────────────────────────────────────


def test_ui_js_update_send_btn_function():
    """ui.js must define updateSendBtn() function."""
    js, status = get_text("/static/ui.js")
    assert status == 200
    assert 'function updateSendBtn' in js


def test_update_send_btn_checks_content():
    """Composer primary action helper must check textarea value length."""
    js, _ = get_text("/static/ui.js")
    fn_idx = js.find('function _composerHasContent')
    fn_end = js.find('\n}', fn_idx) + 2
    fn_body = js[fn_idx:fn_end]
    assert 'msg' in fn_body
    assert '.value' in fn_body
    assert '.length' in fn_body or '.trim()' in fn_body


def test_update_send_btn_checks_pending_files():
    """Composer primary action helper must also count attached files as content."""
    js, _ = get_text("/static/ui.js")
    fn_idx = js.find('function _composerHasContent')
    fn_end = js.find('\n}', fn_idx) + 2
    fn_body = js[fn_idx:fn_end]
    assert 'pendingFiles' in fn_body


def test_update_send_btn_uses_visible_class():
    """updateSendBtn must add .visible class to trigger the pop-in animation."""
    js, _ = get_text("/static/ui.js")
    fn_idx = js.find('function updateSendBtn')
    fn_end = js.find('\n}', fn_idx) + 2
    fn_body = js[fn_idx:fn_end]
    assert 'visible' in fn_body


def test_update_send_btn_uses_disabled():
    """updateSendBtn must disable the button when no content or busy."""
    js, _ = get_text("/static/ui.js")
    fn_idx = js.find('function updateSendBtn')
    fn_end = js.find('\n}', fn_idx) + 2
    fn_body = js[fn_idx:fn_end]
    assert 'disabled' in fn_body


def test_set_busy_calls_update_send_btn():
    """setBusy must call updateSendBtn() so button hides while agent is responding."""
    js, _ = get_text("/static/ui.js")
    busy_idx = js.find('function setBusy')
    busy_end = js.find('\n}', busy_idx) + 2
    busy_body = js[busy_idx:busy_end]
    assert 'updateSendBtn' in busy_body


def test_render_tray_calls_update_send_btn():
    """renderTray must call updateSendBtn() so button appears when files are attached."""
    js, _ = get_text("/static/ui.js")
    tray_idx = js.find('function renderTray')
    tray_end = js.find('\n}', tray_idx) + 2
    tray_body = js[tray_idx:tray_end]
    assert 'updateSendBtn' in tray_body


# ── boot.js ──────────────────────────────────────────────────────────────


def test_boot_js_input_calls_update_send_btn():
    """boot.js input event listener must call updateSendBtn()."""
    js, status = get_text("/static/boot.js")
    assert status == 200
    assert 'updateSendBtn' in js


# ── messages.js ───────────────────────────────────────────────────────────


def test_auto_resize_calls_update_send_btn():
    """autoResize() must call updateSendBtn() so button hides after send clears textarea."""
    js, status = get_text("/static/messages.js")
    assert status == 200
    assert 'updateSendBtn' in js


# ── Regression: existing behaviour unchanged ──────────────────────────────


def test_send_button_still_has_send_btn_class():
    """btnSend must still carry class='send-btn' for CSS targeting."""
    html, _ = get_text("/")
    assert 'class="send-btn"' in html


def test_ui_js_set_busy_calls_update_send_btn():
    """setBusy must call updateSendBtn to manage button disabled state."""
    js, _ = get_text("/static/ui.js")
    busy_idx = js.find('function setBusy')
    busy_end = js.find('\n}', busy_idx) + 2
    busy_body = js[busy_idx:busy_end]
    assert 'updateSendBtn' in busy_body


def test_index_html_attach_button_unchanged():
    """btnAttach must still be present (no regression)."""
    html, _ = get_text("/")
    assert 'id="btnAttach"' in html


def test_send_function_still_exists():
    """send() function must still be defined in messages.js."""
    js, _ = get_text("/static/messages.js")
    assert 'async function send()' in js
