"""#8517 follow-up: the memory-off notice in the chat (static/ui.js), node-driven.

The notice renders ONLY from the session on screen: ``S.session.memory_status``, which
the server sends in every session payload it knows it for (GET /api/session, the
``session`` of stream ``done`` / ``apperror``), and which stream events for that same
session update. No per-session client map, no hand-off between sessions. A dismissal
is remembered per profile and reason, so it applies to every conversation of that
profile; an explicit healthy status for the profile forgets it.

Drives the REAL notice block from static/ui.js through node against a tiny mocked DOM,
plus static checks of the wiring, markup, CSS and i18n. The real-page paths (loadSession,
attachLiveStream, deleteSession) are in test_issue8517_memory_off_notice_browser.py.
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
SESSIONS_JS = (STATIC / "sessions.js").read_text(encoding="utf-8")
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
PREFIX = "hermes-webui-memory-off-dismissed:"


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
global.S = {session: null, messages: []};
global.t = k => 'T:' + k;
(0, eval)(block);
(async () => {
  const out = [];
  for (const step of steps) {
    let ret = null;
    if (step.op === 'set') S.session = step.session;
    else if (step.op === 'event') ret = noteSessionMemoryStatus(step.sid, step.status);
    else if (step.op === 'dismiss') dismissMemoryOffNotice();
    else if (step.op === 'sync') syncMemoryOffNotice();
    else if (step.op === 'store') store.set(step.key, '1');
    await new Promise(r => setTimeout(r, 0));
    const n = els.memoryOffNotice;
    out.push({op: step.op, ret, hidden: n.hidden, text: els.memoryOffNoticeText.textContent,
      reason: n.dataset.reason || '', announcements, composerFocused: els.msg.focused,
      stored: [...store.keys()].sort(),
      status: S.session ? (S.session.memory_status || null) : null});
  }
  process.stdout.write(JSON.stringify(out));
})();
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


def _off(reason="agent_outdated", scope="all"):
    return {"state": "off", "reason": reason, "scope": scope}


def _sess(sid="s1", profile="work", status=None, **extra):
    out = {"session_id": sid, "profile": profile, **extra}
    if status is not None:
        out["memory_status"] = status
    return out


def _set(**kw):
    return {"op": "set", "session": _sess(**kw)}


# ── rendering from the session on screen ──────────────────────────────────────

def test_no_session_and_older_server_render_nothing(tmp_path):
    a, b, c = _drive(tmp_path, [{"op": "sync"}, _set(), {"op": "set", "session": None}])
    for state in (a, b, c):
        assert state["hidden"] is True and state["text"] == "" and state["announcements"] == 0


@pytest.mark.parametrize("reason,key", sorted(REASON_KEYS.items()))
def test_each_reason_renders_its_copy_and_is_announced_once(tmp_path, reason, key):
    states = _drive(tmp_path, [_set(status=_off(reason)), {"op": "sync"}, _set(sid="s2", status=_off(reason))])
    for state in states:
        assert (state["hidden"], state["text"], state["reason"]) == (False, f"T:{key}", reason)
    assert states[-1]["announcements"] == 1, "one announcement per profile and reason per page"


def test_ok_unknown_reason_and_garbage_render_nothing(tmp_path):
    states = _drive(tmp_path, [
        _set(status={"state": "ok"}),
        _set(status={"state": "off", "reason": "/etc/passwd"}),
        _set(status="off"),
        _set(status={"state": "off"}),
    ])
    assert all(s["hidden"] is True and s["announcements"] == 0 for s in states), states


def test_assigning_the_session_on_screen_rerenders_without_any_call(tmp_path):
    """Session switch, reload, continuation and deletion all assign S.session; the notice
    follows the assignment itself (no syncTopbar/syncMemoryOffNotice call needed)."""
    a, b, c, d = _drive(tmp_path, [
        _set(sid="s1", status=_off()),
        _set(sid="s2"),
        _set(sid="s3", status=_off("provider_shared_instance", "external")),
        {"op": "set", "session": None},
    ])
    assert (a["hidden"], b["hidden"], c["hidden"], d["hidden"]) == (False, True, False, True)
    assert c["reason"] == "provider_shared_instance"


def test_a_same_session_payload_without_the_field_keeps_the_status(tmp_path):
    """Endpoints that do not report memory (rename, toolsets, ...) replace S.session for
    the SAME session; what the server said about it stays. Another session never
    inherits it, and an explicit status always wins."""
    a, b, c, d = _drive(tmp_path, [
        _set(sid="s1", status=_off()),
        _set(sid="s1", title="renamed"),
        _set(sid="s2"),
        _set(sid="s2", status={"state": "ok"}),
    ])
    assert (a["hidden"], b["hidden"], c["hidden"], d["hidden"]) == (False, False, True, True)
    assert b["status"] == _off() and c["status"] is None


def test_stream_events_update_only_the_session_on_screen(tmp_path):
    states = _drive(tmp_path, [
        _set(sid="s1"),
        {"op": "event", "sid": "s2", "status": _off()},
        {"op": "event", "sid": "s1", "status": _off()},
        {"op": "event", "sid": "s1", "status": {"state": "ok"}},
    ])
    assert [s["ret"] for s in states[1:]] == [False, True, True]
    assert [s["hidden"] for s in states] == [True, True, False, True]
    assert states[1]["status"] is None


# ── dismissal: per profile and reason ─────────────────────────────────────────

def test_dismissal_applies_to_every_session_of_the_profile_and_only_that_reason(tmp_path):
    states = _drive(tmp_path, [
        _set(sid="s1", profile="work", status=_off()),
        {"op": "dismiss"},
        _set(sid="s2", profile="work", status=_off()),
        _set(sid="s3", profile="home", status=_off()),
        _set(sid="s4", profile="work", status=_off("provider_shared_instance", "external")),
    ])
    dismissed = states[1]
    assert dismissed["hidden"] is True and dismissed["composerFocused"] is True
    assert dismissed["stored"] == [f"{PREFIX}work:agent_outdated"]
    assert states[2]["hidden"] is True, "same profile and reason: still dismissed in another session"
    assert states[3]["hidden"] is False, "another profile is not dismissed"
    assert states[4]["hidden"] is False, "a different reason shows again"


def test_missing_profile_is_the_default_profile(tmp_path):
    states = _drive(tmp_path, [
        {"op": "set", "session": {"session_id": "s1", "memory_status": _off()}},
        {"op": "dismiss"},
        _set(sid="s2", profile="default", status=_off()),
    ])
    assert states[1]["stored"] == [f"{PREFIX}default:agent_outdated"]
    assert states[2]["hidden"] is True


def test_ok_for_the_profile_forgets_its_dismissals_and_reannounces(tmp_path):
    states = _drive(tmp_path, [
        _set(sid="s1", profile="work", status=_off()),
        {"op": "dismiss"},
        {"op": "store", "key": f"{PREFIX}home:agent_outdated"},
        _set(sid="s2", profile="work", status={"state": "ok"}),
        _set(sid="s1", profile="work", status=_off()),
    ])
    assert states[3]["stored"] == [f"{PREFIX}home:agent_outdated"], "only that profile's dismissals go"
    assert states[4]["hidden"] is False
    assert states[4]["announcements"] == 2, "a recurrence after ok is news again"


def test_stream_ok_forgets_the_dismissal_too(tmp_path):
    states = _drive(tmp_path, [
        _set(sid="s1", profile="work", status=_off()),
        {"op": "dismiss"},
        {"op": "event", "sid": "s1", "status": {"state": "ok"}},
        {"op": "event", "sid": "s1", "status": _off()},
    ])
    assert states[2]["stored"] == []
    assert states[3]["hidden"] is False


# ── wiring ────────────────────────────────────────────────────────────────────

def test_no_per_session_hand_off_machinery_is_left():
    js = UI_JS + MESSAGES_JS + SESSIONS_JS
    for gone in ("migrateMemoryOffNotice", "restoreMemoryStatusFromSnapshot", "_memoryOffStatusBySession",
                 "memoryStatus:", "noteMemoryOkStatus", "noteMemoryOffStatus"):
        assert gone not in js, gone


def test_stream_listeners_route_memory_status_to_the_session_on_screen():
    start = MESSAGES_JS.find("source.addEventListener('warning'")
    body = MESSAGES_JS[start:start + 1500]
    note = body.find("noteSessionMemoryStatus(")
    assert note > 0 and note < body.find("setComposerStatus("), body
    ok = MESSAGES_JS.find("source.addEventListener('memory_status'")
    assert ok > 0 and "noteSessionMemoryStatus(" in MESSAGES_JS[ok:ok + 600]
    cursor = re.search(r"for\(const _runJournalEventName of \[([^\]]*)\]", MESSAGES_JS)
    assert cursor and "'memory_status'" in cursor.group(1)


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
    assert INDEX_HTML.find('id="memoryOffNotice"') < INDEX_HTML.find('id="composerWrap"')


def test_css_hides_it_and_gives_a_44px_tap_target_on_touch():
    assert re.search(r"\.memory-off-notice\[hidden\]\s*\{\s*display:\s*none", STYLE_CSS)
    assert re.search(r"\.memory-off-notice-dismiss\{[^}]*width:32px;height:32px", STYLE_CSS)
    mobile = re.search(r"@media[^{]*(max-width:\s*700px|pointer:\s*coarse)[^{]*\{[^}]*\.memory-off-notice-dismiss\{[^}]*\}", STYLE_CSS)
    assert mobile and "44px" in mobile.group(0)


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
        assert _MEMORY_OFF_MESSAGES[reason] == value.replace("\\'", "'"), reason
