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
  else if (step.op === 'ok') ret = noteMemoryOkStatus(step.data);
  else if (step.op === 'restore') ret = restoreMemoryStatusFromSnapshot(step.sid, step.status);
  else if (step.op === 'migrate') ret = migrateMemoryOffNotice(step.from, step.to);
  else if (step.op === 'forget') store.clear();
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


def _ok(sid="s1", stream="st2"):
    return {"type": "memory_status", "state": "ok", "session_id": sid, "stream_id": stream}


def test_only_an_explicit_healthy_status_clears_it(tmp_path):
    """Codex r1 finding 2: the notice follows the server's explicit status. A healthy
    status for the session clears it (and its dismissal, so a later memory-off is news
    again); anything else (another session's status, a malformed frame) keeps it."""
    states = _drive(tmp_path, [
        {"op": "note", "data": _ev(stream="st1")},
        {"op": "ok", "data": _ok(sid="s9")},
        {"op": "ok", "data": {"type": "memory_status", "state": "unknown", "session_id": "s1"}},
        {"op": "ok", "data": None},
        {"op": "ok", "data": _ok()},
        {"op": "note", "data": _ev(stream="st3")},
    ])
    first, other, unknown, missing, cleared, again = states
    assert first["hidden"] is False
    assert other["hidden"] is False and other["ret"] is True
    assert unknown["hidden"] is False and unknown["ret"] is False
    assert missing["hidden"] is False and missing["ret"] is False
    assert cleared["hidden"] is True and cleared["ret"] is True
    assert again["hidden"] is False and again["announcements"] == 2


def test_healthy_status_forgets_the_dismissal(tmp_path):
    states = _drive(tmp_path, [
        {"op": "note", "data": _ev()},
        {"op": "dismiss"},
        {"op": "ok", "data": _ok()},
        {"op": "note", "data": _ev(stream="st3")},
    ])
    assert states[1]["hidden"] is True and states[1]["stored"]
    assert states[2]["stored"] == []
    assert states[3]["hidden"] is False


def test_restore_from_the_journal_snapshot(tmp_path):
    """Codex r1 finding 1: reload/reattach restores the status the run journal
    snapshot carries, keyed to the session being loaded, and respects dismissal."""
    snap = {"state": "external_off", "reason": "provider_shared_instance", "scope": "external"}
    restored, none, forged = _drive(tmp_path, [
        {"op": "restore", "sid": "s1", "status": snap},
        {"op": "restore", "sid": "s1", "status": None},
        {"op": "restore", "sid": "s1", "status": {"state": "off", "reason": "<img src=x>"}},
    ])
    assert restored["ret"] is True and restored["hidden"] is False
    assert restored["reason"] == "provider_shared_instance"
    assert none["ret"] is False and none["hidden"] is False, "no status in the snapshot keeps the notice"
    assert forged["hidden"] is False and forged["reason"] == "provider_shared_instance"
    states = _drive(tmp_path, [
        {"op": "note", "data": _ev(reason="provider_shared_instance")},
        {"op": "dismiss"},
        {"op": "restore", "sid": "s1", "status": snap},
    ])
    assert states[-1]["hidden"] is True, "a dismissed notice stays dismissed after a reload"
    (healthy,) = _drive(tmp_path, [{"op": "restore", "sid": "s1", "status": {"state": "ok"}}])
    assert healthy["hidden"] is True


def test_migrate_to_the_compression_continuation(tmp_path):
    """Codex r1 finding 3: s1 -> s2 carries the status and its dismissal."""
    states = _drive(tmp_path, [
        {"op": "note", "data": _ev(sid="s1")},
        {"op": "migrate", "from": "s1", "to": "s2"},
        {"op": "switch", "sid": "s2"},
    ])
    assert states[1]["ret"] is True and states[2]["hidden"] is False
    assert states[2]["announcements"] == 1, "the same notice is not announced again"
    states = _drive(tmp_path, [
        {"op": "note", "data": _ev(sid="s1")},
        {"op": "dismiss"},
        {"op": "migrate", "from": "s1", "to": "s2"},
        {"op": "switch", "sid": "s2"},
    ])
    assert states[-1]["hidden"] is True
    assert any(k.endswith(":s2:agent_outdated") for k in states[-1]["stored"])
    (nothing,) = _drive(tmp_path, [{"op": "migrate", "from": "s1", "to": "s2"}])
    assert nothing["ret"] is False


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
    # Recorded before the open-session guard (the status is session-keyed), and only
    # for this stream's own session.
    assert note < body.find("S.session.session_id!==activeSid"), body
    assert "String(_memoryOff.session_id||'')===String(activeSid)" in body


def test_wiring_explicit_status_restore_and_continuation():
    # No absence-based clearing left anywhere (Codex r1 finding 2).
    assert "settleMemoryOffNoticeAfterTurn" not in MESSAGES_JS + UI_JS
    ok = MESSAGES_JS.find("source.addEventListener('memory_status'")
    assert ok > 0 and "noteMemoryOkStatus(" in MESSAGES_JS[ok:ok + 600]
    assert "String(_memoryStatus.session_id||'')===String(activeSid)" in MESSAGES_JS[ok:ok + 600]
    # Journaled frame: advance the replay cursor past it like every other event.
    cursor = re.search(r"for\(const _runJournalEventName of \[([^\]]*)\]", MESSAGES_JS)
    assert cursor and "'memory_status'" in cursor.group(1)
    # Continuation: migrate before the completion repaint (_finishDone).
    done = MESSAGES_JS.find("source.addEventListener('done'")
    body = MESSAGES_JS[done:done + 2500]
    assert 0 < body.find("migrateMemoryOffNotice(activeSid") < body.find("const _finishDone=")
    # Reattach: the snapshot status is restored on load and on reconnect.
    sessions_js = (STATIC / "sessions.js").read_text(encoding="utf-8")
    assert "memoryStatus:" in sessions_js[sessions_js.find("function _serverLiveSnapshotInflight("):]
    assert "restoreMemoryStatusFromSnapshot(sid" in sessions_js
    attach = MESSAGES_JS.find("function attachLiveStream(")
    assert "restoreMemoryStatusFromSnapshot(activeSid" in MESSAGES_JS[attach:attach + 4000]
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


def test_server_fallback_text_matches_the_english_copy():
    """Older clients show the frame's ``message``; keep it the same words as the UI."""
    from api.memory_profile_scope import _MEMORY_OFF_MESSAGES  # noqa: PLC0415

    en = _locale_blocks()["en"]
    for reason, key in REASON_KEYS.items():
        value = re.search(rf"\n\s+{key}: '([^']*)'", en).group(1)
        assert _MEMORY_OFF_MESSAGES[reason] == value, reason
