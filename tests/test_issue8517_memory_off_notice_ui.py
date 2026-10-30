"""#8517 follow-up: the memory-off notice in the chat (static/ui.js).

Drives the REAL notice block from static/ui.js through node against a tiny mocked
DOM (the approach of test_goal_command_js_behaviour.py), plus static checks of the
wiring: the SSE ``warning`` listener, turn settlement, the session-switch sync, the
markup, the CSS and the i18n keys.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC = REPO_ROOT / "static"
UI_JS = (STATIC / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (STATIC / "messages.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (STATIC / "style.css").read_text(encoding="utf-8")
I18N_JS = (STATIC / "i18n.js").read_text(encoding="utf-8")

NODE = shutil.which("node")

BLOCK_START = "// ── #8517 memory-off notice ──"
BLOCK_END = "// ── end #8517 memory-off notice ──"
REASON_KEYS = {
    "agent_outdated": "memory_off_agent_outdated",
    "agent_no_instance_claim": "memory_off_agent_no_instance_claim",
    "provider_shared_instance": "memory_off_provider_shared_instance",
}
I18N_KEYS = [*REASON_KEYS.values(), "memory_off_notice_dismiss"]


def _notice_block() -> str:
    start = UI_JS.find(BLOCK_START)
    end = UI_JS.find(BLOCK_END)
    assert start >= 0 and end > start, "the #8517 memory-off notice block is missing from static/ui.js"
    return UI_JS[start:end]


_DRIVER = r"""
const block = require('fs').readFileSync(process.argv[2], 'utf8');
const steps = JSON.parse(process.argv[3]);
const store = new Map();
global.localStorage = {
  getItem: k => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => { store.set(k, String(v)); },
  removeItem: k => { store.delete(k); },
};
function el(id){ return {id, hidden: true, textContent: '', dataset: {}, focused: false,
  focus(){ this.focused = true; }}; }
const els = {memoryOffNotice: el('memoryOffNotice'), memoryOffNoticeText: el('memoryOffNoticeText'),
  a11yAnnouncer: el('a11yAnnouncer'), msg: el('msg')};
let announcements = 0;
let announced = '';
Object.defineProperty(els.a11yAnnouncer, 'textContent', {
  get(){ return announced; }, set(v){ announced = v; if (v) announcements++; },
});
global.$ = id => els[id] || null;
global.S = {session: {session_id: 's1'}};
global.t = k => 'T:' + k;
(0, eval)(block);
const out = [];
for (const step of steps) {
  let ret = null;
  if (step.op === 'note') ret = noteMemoryOffStatus(step.data);
  else if (step.op === 'sync') syncMemoryOffNotice();
  else if (step.op === 'dismiss') dismissMemoryOffNotice();
  else if (step.op === 'switch') { S.session = step.sid ? {session_id: step.sid} : null; syncMemoryOffNotice(); }
  else if (step.op === 'settle') settleMemoryOffNoticeAfterTurn(step.sid, step.stream);
  const n = els.memoryOffNotice;
  out.push({op: step.op, ret, hidden: n.hidden, text: els.memoryOffNoticeText.textContent,
    reason: n.dataset.reason || '', announcements, composerFocused: els.msg.focused,
    stored: [...store.keys()]});
}
process.stdout.write(JSON.stringify(out));
"""


def _drive(tmp_path, steps):
    if NODE is None:
        pytest.skip("node not on PATH")
    block = tmp_path / "notice_block.js"
    block.write_text(_notice_block(), encoding="utf-8")
    driver = tmp_path / "driver.js"
    driver.write_text(_DRIVER, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(driver), str(block), json.dumps(steps)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _ev(reason="agent_outdated", sid="s1", stream="st1", **extra):
    return {"type": "memory_off", "session_id": sid, "stream_id": stream, "reason": reason, **extra}


# ── behaviour ─────────────────────────────────────────────────────────────────

def test_absent_status_renders_nothing(tmp_path):
    (state,) = _drive(tmp_path, [{"op": "sync"}])
    assert state["hidden"] is True and state["text"] == "" and state["announcements"] == 0


@pytest.mark.parametrize("reason,key", sorted(REASON_KEYS.items()))
def test_each_reason_shows_its_copy_once(tmp_path, reason, key):
    first, second = _drive(tmp_path, [{"op": "note", "data": _ev(reason)}, {"op": "note", "data": _ev(reason, stream="st2")}])
    assert first["ret"] is True and first["hidden"] is False
    assert first["text"] == "T:" + key and first["reason"] == reason
    # A repeat for the same session and reason (every later turn) changes nothing
    # and is not announced again.
    assert second["hidden"] is False and second["text"] == first["text"]
    assert first["announcements"] == 1 and second["announcements"] == 1


def test_dismissal_sticks_per_session_and_reason(tmp_path):
    states = _drive(tmp_path, [
        {"op": "note", "data": _ev()},
        {"op": "dismiss"},
        {"op": "note", "data": _ev(stream="st2")},
        {"op": "switch", "sid": "s2"},
        {"op": "note", "data": _ev(sid="s2", stream="st3")},
        {"op": "switch", "sid": "s1"},
    ])
    shown, dismissed, repeat, switched, other_session, back = states
    assert shown["hidden"] is False
    assert dismissed["hidden"] is True and dismissed["composerFocused"] is True
    assert any(k.endswith(":s1:agent_outdated") for k in dismissed["stored"])
    assert repeat["hidden"] is True, "a dismissed notice must not come back for that session"
    assert switched["hidden"] is True, "another session without a status shows nothing"
    assert other_session["hidden"] is False, "once per session: a new session gets its own notice"
    assert back["hidden"] is True


def test_other_sessions_status_does_not_touch_the_open_session(tmp_path):
    (state,) = _drive(tmp_path, [{"op": "note", "data": _ev(sid="s9")}])
    assert state["ret"] is True and state["hidden"] is True and state["announcements"] == 0
    states = _drive(tmp_path, [{"op": "note", "data": _ev(sid="s9")}, {"op": "switch", "sid": "s9"}])
    assert states[-1]["hidden"] is False


def test_unknown_reason_and_other_warnings(tmp_path):
    unknown, other, missing = _drive(tmp_path, [
        {"op": "note", "data": _ev(reason="something_new")},
        {"op": "note", "data": {"type": "fallback", "message": "x"}},
        {"op": "note", "data": None},
    ])
    assert unknown["ret"] is True and unknown["hidden"] is True, "unknown reason: consumed, not rendered"
    assert other["ret"] is False and missing["ret"] is False, "other warnings keep their old path"


def test_turn_without_a_status_clears_it(tmp_path):
    """The status follows the server: a completed turn of that session that reported
    no memory_off (memory is back, e.g. after an upgrade + restart) hides it."""
    states = _drive(tmp_path, [
        {"op": "note", "data": _ev(stream="st1")},
        {"op": "settle", "sid": "s1", "stream": "st1"},
        {"op": "settle", "sid": "s1", "stream": "st2"},
        {"op": "note", "data": _ev(stream="st3")},
    ])
    kept, cleared, again = states[1], states[2], states[3]
    assert kept["hidden"] is False
    assert cleared["hidden"] is True
    assert again["hidden"] is False and again["announcements"] == 2


def test_no_session_open(tmp_path):
    (state,) = _drive(tmp_path, [{"op": "switch", "sid": ""}])
    assert state["hidden"] is True


# ── wiring ────────────────────────────────────────────────────────────────────

def test_markup_is_a_hidden_polite_status_region_with_a_real_button():
    m = re.search(r'<div[^>]*id="memoryOffNotice"[^>]*>', INDEX_HTML)
    assert m, "#memoryOffNotice missing from static/index.html"
    tag = m.group(0)
    assert 'role="status"' in tag and 'aria-live="polite"' in tag and " hidden" in tag
    assert 'id="memoryOffNoticeText"' in INDEX_HTML
    btn = re.search(r'<button[^>]*id="memoryOffNoticeDismiss"[^>]*>', INDEX_HTML)
    assert btn and 'type="button"' in btn.group(0)
    assert 'data-i18n-aria-label="memory_off_notice_dismiss"' in btn.group(0)
    assert "dismissMemoryOffNotice()" in btn.group(0)
    # Above the composer, next to the other chat-level banners.
    assert INDEX_HTML.find('id="memoryOffNotice"') < INDEX_HTML.find('id="composerWrap"')


def test_css_hides_it_and_gives_a_44px_tap_target_on_touch():
    assert re.search(r"\.memory-off-notice\[hidden\]\s*\{\s*display:\s*none", STYLE_CSS)
    mobile = re.search(r"@media[^{]*(max-width:\s*700px|pointer:\s*coarse)[^{]*\{[^}]*\.memory-off-notice-dismiss\{[^}]*\}", STYLE_CSS)
    assert mobile and "44px" in mobile.group(0)


def test_warning_listener_routes_memory_off_before_the_composer_status():
    start = MESSAGES_JS.find("source.addEventListener('warning'")
    body = MESSAGES_JS[start:start + 1500]
    note = body.find("noteMemoryOffStatus(")
    assert note > 0 and note < body.find("setComposerStatus("), body


def test_done_settles_and_session_switch_syncs():
    start = MESSAGES_JS.find("source.addEventListener('done'")
    assert "settleMemoryOffNoticeAfterTurn(activeSid" in MESSAGES_JS[start:start + 1500]
    sync = UI_JS.find("function syncTopbar(")
    assert "syncMemoryOffNotice()" in UI_JS[sync:sync + 400]


def _locale_blocks():
    starts = [(m.start(), m.group(1)) for m in re.finditer(r"\n  ('?[a-zA-Z-]+'?): \{\n", I18N_JS)]
    return {name.strip("'"): I18N_JS[s:(starts[i + 1][0] if i + 1 < len(starts) else len(I18N_JS))]
            for i, (s, name) in enumerate(starts)}


def test_i18n_keys_in_every_locale_and_copy_is_sentence_case():
    blocks = _locale_blocks()
    assert "en" in blocks and len(blocks) >= 15
    for name, block in blocks.items():
        for key in I18N_KEYS:
            assert re.search(rf"\n\s+{key}: ", block), f"{key} missing from locale {name}"
    en = blocks["en"]
    for key in I18N_KEYS:
        value = re.search(rf"\n\s+{key}: '([^']*)'", en).group(1)
        assert value[:1].isupper() and not re.search(r"\b[A-Z][a-z]+ [A-Z][a-z]+ [A-Z]", value.replace("Hermes Agent", "")), value
