"""#8517 follow-up: the memory-off notice through the REAL page scripts (Chromium).

Loads static/index.html with every real script, swaps EventSource for a deterministic
fixture and serves fixture session payloads, then drives the real ``loadSession``,
``attachLiveStream`` listeners and ``deleteSession``. The notice renders only from the
session on screen (``S.session.memory_status``, sent by the server in every session
payload); every transition below falls out of that with no special case:

* idle reload and reload during a running turn show it at once (payload);
* compression continuation through ``done`` and ``apperror``, in the foreground and in
  the background, shows the continuation's own status;
* a continuation redirect shows the continuation's status;
* deleting the selected session hides it;
* a dismissal applies to every session of that profile, not to another profile; an
  explicit ``ok`` for the profile forgets it; a new reason shows again;
* an older server (no field) shows nothing.
"""
from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
_BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]
PREFIX = "hermes-webui-memory-off-dismissed:"

_INIT = """
window.fixtureSources=[];
class FixtureEventSource {
  static OPEN=1; static CONNECTING=0; static CLOSED=2;
  constructor(url){this.url=String(url);this.readyState=1;this.listeners={};window.fixtureSources.push(this);}
  addEventListener(name,fn){(this.listeners[name]||=[]).push(fn);}
  removeEventListener(){}
  close(){this.readyState=2;}
  emit(name,data,id){for(const fn of this.listeners[name]||[])fn({data:JSON.stringify(data),lastEventId:id||''});}
}
window.EventSource=FixtureEventSource;
window.fixtureStream=(sid)=>window.fixtureSources.findLast(s=>s.url.includes('api/chat/stream?')&&(!sid||s.url.includes('stream_id='+sid)));
"""

OFF_SHARED = {"state": "off", "reason": "provider_shared_instance", "scope": "external"}
OFF_OUTDATED = {"state": "off", "reason": "agent_outdated", "scope": "all"}
_STATE = """()=>({sid:S.session?S.session.session_id:null,hidden:$('memoryOffNotice').hidden,
  reason:$('memoryOffNotice').dataset.reason||''})"""


def _session(sid, *, profile="work", status=None, **extra):
    out = {"session_id": sid, "title": f"Notice {sid}", "workspace": "/tmp/notice-gate", "messages": [],
           "message_count": 0, "model": "", "profile": profile, **extra}
    if status is not None:
        out["memory_status"] = status
    return out


class Server:
    """Fixture API: ``sessions`` maps session_id -> GET /api/session payload."""

    def __init__(self):
        self.sessions: dict[str, dict] = {}

    def route(self, r):
        url = urlsplit(r.request.url)
        path = url.path
        if path == "/":
            html = (ROOT / "static/index.html").read_text(encoding="utf-8")
            html = (html.replace("__MAX_UPLOAD_BYTES__", "20971520").replace("__CSRF_TOKEN_JSON__", '""')
                    .replace("__CSP_IMG_EXTRA_JSON__", "[]"))
            r.fulfill(body=html, content_type="text/html")
        elif path in ("/static/boot.js", "/static/pwa-startup.js"):
            r.fulfill(body="", content_type="application/javascript")
        elif path.startswith("/static/") and (ROOT / path.lstrip("/")).is_file():
            file = ROOT / path.lstrip("/")
            r.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(str(file))[0] or "application/octet-stream")
        elif path == "/api/session":
            sid = parse_qs(url.query).get("session_id", [""])[0]
            if sid in self.sessions:
                r.fulfill(json={"session": self.sessions[sid]})
            else:
                r.fulfill(status=404, json={"error": "Session not found"})
        elif path == "/api/sessions":
            r.fulfill(json={"sessions": []})
        elif path.startswith("/api/"):
            r.fulfill(json={"active": True, "ok": True, "sessions": [], "models": [], "workspaces": []})
        else:
            r.fulfill(status=404, body="")


@pytest.fixture(scope="module")
def browser():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as playwright:
        try:
            chromium = playwright.chromium.launch(headless=True, args=_BROWSER_ARGS)
        except Exception as exc:  # no browser binary in this environment
            pytest.skip(f"chromium unavailable: {exc}")
        yield chromium
        chromium.close()


@pytest.fixture
def server():
    return Server()


@pytest.fixture
def page(browser, server):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, service_workers="block")
    ctx.add_init_script(_INIT)
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.route("**/*", server.route)
    pg.goto("http://notice-gate.test/", wait_until="load")
    assert pg.evaluate("()=>$('memoryOffNotice').hidden") is True
    yield pg
    assert errors == []
    ctx.close()


def _load(page, sid):
    page.evaluate("async(sid)=>{await loadSession(sid);}", sid)
    page.wait_for_timeout(50)
    return page.evaluate(_STATE)


def _stream_session(page, sid="s1", stream="st1"):
    """The session on screen with a running turn whose stream is attached."""
    page.evaluate("""([sid,stream])=>{S.busy=true;S.activeStreamId=stream;window._streamingFade=false;
      attachLiveStream(sid,stream);}""", [sid, stream])


def _emit(page, name, data, eid=""):
    page.evaluate("([n,d,i])=>fixtureStream().emit(n,d,i)", [name, data, eid])
    page.wait_for_timeout(30)


def _payload(sid, status=None, **extra):
    s = _session(sid, status=status, messages=[{"role": "assistant", "content": "Complete", "timestamp": 123}],
                 message_count=1, **extra)
    return s


# ── reload ────────────────────────────────────────────────────────────────────

def test_idle_reload_shows_the_notice_at_once(page, server):
    server.sessions["s1"] = _session("s1", status=OFF_SHARED)
    assert _load(page, "s1") == {"sid": "s1", "hidden": False, "reason": "provider_shared_instance"}


def test_reload_during_a_running_turn_shows_the_notice_at_once(page, server):
    server.sessions["s1"] = _session(
        "s1", status=OFF_OUTDATED, active_stream_id="st1", pending_user_message="hi", pending_started_at=1,
        runtime_journal={"active": True, "stream_id": "st1"})
    assert _load(page, "s1") == {"sid": "s1", "hidden": False, "reason": "agent_outdated"}
    urls = page.evaluate("()=>fixtureSources.map(s=>s.url)")
    assert any("api/chat/stream?" in u and "st1" in u for u in urls), urls


def test_older_server_without_the_field_shows_nothing(page, server):
    server.sessions["s1"] = _session("s1")
    assert _load(page, "s1")["hidden"] is True
    _stream_session(page)
    _emit(page, "done", {"session": _payload("s1")})
    assert page.evaluate(_STATE)["hidden"] is True


# ── live updates for the session on screen ────────────────────────────────────

def test_stream_status_updates_the_session_on_screen_only(page, server):
    server.sessions["s1"] = _session("s1")
    _load(page, "s1")
    _stream_session(page)
    _emit(page, "warning", {"type": "memory_off", "session_id": "other", "stream_id": "st1",
                            "reason": "agent_outdated", "scope": "all", "message": "x"})
    assert page.evaluate(_STATE)["hidden"] is True
    _emit(page, "warning", {"type": "memory_off", "session_id": "s1", "stream_id": "st1",
                            "reason": "agent_outdated", "scope": "all", "message": "x"})
    assert page.evaluate(_STATE) == {"sid": "s1", "hidden": False, "reason": "agent_outdated"}
    composer_status = page.evaluate("()=>($('composerStatus')||{}).textContent||''")
    assert "Memory" not in composer_status
    _emit(page, "memory_status", {"type": "memory_status", "state": "ok", "session_id": "s1", "stream_id": "st1"})
    assert page.evaluate(_STATE)["hidden"] is True


# ── compression continuation: the continuation's own payload ─────────────────

_COMPRESSED = {"session_id": "s1", "old_session_id": "s1", "new_session_id": "s2", "continuation_session_id": "s2"}


def test_compression_continuation_through_done(page, server):
    server.sessions["s1"] = _session("s1", status=OFF_SHARED)
    _load(page, "s1")
    _stream_session(page)
    _emit(page, "compressed", _COMPRESSED)
    _emit(page, "done", {"session": _payload("s2", status=OFF_SHARED)})
    assert page.evaluate(_STATE) == {"sid": "s2", "hidden": False, "reason": "provider_shared_instance"}


def test_compression_continuation_through_apperror(page, server):
    server.sessions["s1"] = _session("s1", status=OFF_SHARED)
    _load(page, "s1")
    _stream_session(page)
    _emit(page, "compressed", _COMPRESSED)
    _emit(page, "apperror", {"type": "provider_error", "message": "Provider failed", "session_id": "s2",
                             "old_session_id": "s1", "new_session_id": "s2", "continuation_session_id": "s2",
                             "session": _payload("s2", status=OFF_SHARED)}, "st1:9")
    assert page.evaluate(_STATE) == {"sid": "s2", "hidden": False, "reason": "provider_shared_instance"}


@pytest.mark.parametrize("terminal", ["done", "apperror"])
def test_background_compression_then_opening_the_continuation(page, server, terminal):
    server.sessions["s1"] = _session("s1", status=OFF_SHARED)
    server.sessions["s3"] = _session("s3", status={"state": "ok"}, profile="home")
    _load(page, "s1")
    page.evaluate("()=>dismissMemoryOffNotice()")
    _stream_session(page)
    _load(page, "s3")  # the user moved on; s1 keeps running in the background
    _emit(page, "compressed", _COMPRESSED)
    payload = {"session": _payload("s2", status=OFF_SHARED)}
    if terminal == "apperror":
        payload.update(type="provider_error", message="Provider failed", session_id="s2", old_session_id="s1",
                       new_session_id="s2", continuation_session_id="s2")
    _emit(page, terminal, payload)
    assert page.evaluate(_STATE) == {"sid": "s3", "hidden": True, "reason": ""}
    server.sessions["s2"] = _session("s2", status=OFF_SHARED)
    assert _load(page, "s2") == {"sid": "s2", "hidden": True, "reason": ""}, "the profile's dismissal applies"
    page.evaluate("(k)=>localStorage.removeItem(k)", f"{PREFIX}work:provider_shared_instance")
    _load(page, "s3")
    assert _load(page, "s2") == {"sid": "s2", "hidden": False, "reason": "provider_shared_instance"}


def test_continuation_redirect_shows_the_continuations_status(page, server):
    server.sessions["s1"] = _session("s1", continuation_session_id="s2")
    server.sessions["s2"] = _session("s2", status=OFF_OUTDATED)
    assert _load(page, "s1") == {"sid": "s2", "hidden": False, "reason": "agent_outdated"}


# ── clearing the selected session ─────────────────────────────────────────────

def test_deleting_the_selected_session_hides_the_notice(page, server):
    server.sessions["s1"] = _session("s1", status=OFF_SHARED)
    assert _load(page, "s1")["hidden"] is False
    page.evaluate("async()=>{window.showConfirmDialog=async()=>true; await deleteSession('s1');}")
    page.wait_for_timeout(50)
    assert page.evaluate(_STATE) == {"sid": None, "hidden": True, "reason": ""}


# ── dismissal per profile and reason ──────────────────────────────────────────

def test_dismissal_is_shared_by_the_profiles_sessions_not_by_other_profiles(page, server):
    server.sessions["s1"] = _session("s1", status=OFF_SHARED)
    server.sessions["s2"] = _session("s2", status=OFF_SHARED)
    server.sessions["s3"] = _session("s3", status=OFF_SHARED, profile="home")
    server.sessions["s4"] = _session("s4", status=OFF_OUTDATED)
    _load(page, "s1")
    focused = page.evaluate("()=>{dismissMemoryOffNotice(); return document.activeElement&&document.activeElement.id;}")
    assert focused == "msg"
    stored = page.evaluate("(p)=>Object.keys(localStorage).filter(k=>k.startsWith(p))", PREFIX)
    assert stored == [f"{PREFIX}work:provider_shared_instance"]
    assert _load(page, "s2")["hidden"] is True
    assert _load(page, "s3")["hidden"] is False
    assert _load(page, "s4") == {"sid": "s4", "hidden": False, "reason": "agent_outdated"}


def test_ok_for_the_profile_forgets_its_dismissals(page, server):
    server.sessions["s1"] = _session("s1", status=OFF_SHARED)
    server.sessions["s5"] = _session("s5", status={"state": "ok"})
    _load(page, "s1")
    page.evaluate("()=>dismissMemoryOffNotice()")
    assert _load(page, "s5")["hidden"] is True
    assert page.evaluate("(p)=>Object.keys(localStorage).filter(k=>k.startsWith(p))", PREFIX) == []
    assert _load(page, "s1")["hidden"] is False


def test_payload_json_shape_is_codes_only():
    """The fixtures above use the exact shape the server emits."""
    assert set(OFF_SHARED) == {"state", "reason", "scope"}
    assert json.dumps(OFF_SHARED).count("/") == 0


# ── Fable r1 UX fixes ─────────────────────────────────────────────────────────

_FADE = "()=>getComputedStyle($('composerWrap'),'::before').display"
_NOTICE_VISIBILITY = "()=>getComputedStyle($('memoryOffNotice')).visibility"


def test_composer_fade_never_covers_a_shown_notice(page, server):
    """The composer's top gradient faded the notice's last line below WCAG AA (3.9:1)."""
    assert page.evaluate(_FADE) != "none"
    server.sessions["s1"] = _session("s1", status=OFF_SHARED)
    _load(page, "s1")
    assert page.evaluate(_FADE) == "none"
    server.sessions["s2"] = _session("s2", status={"state": "ok"})
    _load(page, "s2")
    assert page.evaluate(_FADE) != "none"


_NOTICE_DISPLAY = "()=>getComputedStyle($('memoryOffNotice')).display"


@pytest.mark.parametrize("card", ["queueCard", "approvalCard", "clarifyCard"])
def test_an_open_composer_flyout_collapses_the_notice(page, server, card):
    """Fable r1/r2: flyouts rise over the notice; each already reserves its own space, so keeping the
    notice's band (visibility:hidden) doubled the blank above every card. Collapse it instead."""
    server.sessions["s1"] = _session("s1", status=OFF_SHARED)
    _load(page, "s1")
    assert page.evaluate(_NOTICE_DISPLAY) == "flex"
    page.evaluate("(id)=>$(id).classList.add('visible')", card)
    assert page.evaluate(_NOTICE_DISPLAY) == "none"
    page.evaluate("(id)=>$(id).classList.remove('visible')", card)
    assert page.evaluate(_NOTICE_DISPLAY) == "flex"


def test_notice_matches_sibling_banners_and_uses_the_strong_focus_ring(page, server):
    server.sessions["s1"] = _session("s1", status=OFF_SHARED)
    _load(page, "s1")
    style = page.evaluate("()=>{const s=getComputedStyle($('memoryOffNotice'));return [s.fontSize,s.marginTop];}")
    assert style == ["13px", "10px"]
    rule = page.evaluate("""()=>{for(const sh of document.styleSheets){let rs;try{rs=sh.cssRules}catch(e){continue}
      for(const r of rs){if(r.selectorText==='.memory-off-notice-dismiss:focus-visible')return r.style.cssText}}return ''}""")
    assert "--focus-ring-strong" in rule


# ── Fable r2: a notice appearing mid-turn keeps a bottom-pinned reader pinned ──────

def _long_session(sid, status=None):
    msgs = []
    for i in range(30):
        msgs.append({"role": "user", "content": f"Question {i}: " + "word " * 40, "timestamp": 100 + 2 * i})
        msgs.append({"role": "assistant", "content": f"Answer {i}:\n\n" + "detail " * 80, "timestamp": 101 + 2 * i})
    return _session(sid, status=status, messages=msgs, message_count=len(msgs))


_BOTTOM = "()=>{const el=$('messages');return Math.round(el.scrollHeight-el.scrollTop-el.clientHeight);}"
_SHOW_NOTICE = "()=>noteSessionMemoryStatus('s1',%s)" % json.dumps(OFF_SHARED)


def _load_long_and_pin(page, server):
    server.sessions["s1"] = _long_session("s1")
    _load(page, "s1")
    page.wait_for_timeout(300)  # let deferred rendering settle so it cannot re-pin for us
    page.evaluate("()=>{const el=$('messages');el.scrollTop=el.scrollHeight;}")
    page.wait_for_timeout(100)
    assert page.evaluate("()=>{const el=$('messages');return el.scrollHeight>el.clientHeight+200;}")
    assert page.evaluate(_BOTTOM) <= 1


def test_notice_appearing_mid_turn_keeps_a_pinned_reader_at_the_bottom(page, server):
    """Fable r2: the notice shrank the transcript viewport and stranded a pinned reader above the tail."""
    _load_long_and_pin(page, server)
    assert page.evaluate(_SHOW_NOTICE) is True
    page.wait_for_timeout(100)
    assert page.evaluate("()=>$('memoryOffNotice').hidden") is False
    assert page.evaluate(_BOTTOM) <= 1


def test_notice_appearing_never_yanks_a_reader_who_scrolled_away(page, server):
    _load_long_and_pin(page, server)
    page.evaluate("""()=>{_scrollPinned=false;_messageUserUnpinned=true;
      const el=$('messages');el.scrollTop=Math.round(el.scrollHeight/3);}""")  # what a user scroll-away records
    page.wait_for_timeout(100)
    before = page.evaluate("()=>$('messages').scrollTop")
    page.evaluate(_SHOW_NOTICE)
    page.wait_for_timeout(100)
    assert page.evaluate("()=>$('messages').scrollTop") == before


def test_notice_appearing_respects_auto_scroll_follow_off(page, server):
    _load_long_and_pin(page, server)
    page.evaluate("()=>{window._autoScrollFollow=false;}")
    before = page.evaluate("()=>$('messages').scrollTop")
    page.evaluate(_SHOW_NOTICE)
    page.wait_for_timeout(100)
    assert page.evaluate("()=>$('messages').scrollTop") == before


# ── Codex UX-delta r3: collapsing the notice under a flyout must not yank a reader who scrolled away ──

_FLYOUT_SYNC = {"approvalCard": "_syncApprovalTranscriptSpace", "clarifyCard": "_syncClarifyTranscriptSpace"}


def _open_flyout(page, card):
    page.evaluate("([id,fn])=>{const c=$(id);c.classList.add('visible');window[fn](c);}", [card, _FLYOUT_SYNC[card]])
    page.wait_for_timeout(150)  # the helpers re-snap in requestAnimationFrame


@pytest.mark.parametrize("card", sorted(_FLYOUT_SYNC))
def test_opening_a_flyout_over_the_notice_keeps_a_scrolled_away_reader_in_place(page, server, card):
    _load_long_and_pin(page, server)
    page.evaluate(_SHOW_NOTICE)
    page.wait_for_timeout(100)
    # A genuine user scroll-away ~180 px above the tail; the notice's collapse would bring it under 150 px.
    page.evaluate("""()=>{_scrollPinned=false;_messageUserUnpinned=true;
      const el=$('messages');el.scrollTop=el.scrollHeight-el.clientHeight-180;}""")
    page.wait_for_timeout(100)
    before = page.evaluate("()=>$('messages').scrollTop")
    _open_flyout(page, card)
    assert page.evaluate("()=>getComputedStyle($('memoryOffNotice')).display") == "none"
    assert page.evaluate("()=>$('messages').scrollTop") == before
    assert page.evaluate("()=>_messageUserUnpinned") is True


@pytest.mark.parametrize("card", sorted(_FLYOUT_SYNC))
def test_opening_a_flyout_over_the_notice_still_keeps_a_pinned_reader_at_the_tail(page, server, card):
    _load_long_and_pin(page, server)
    page.evaluate(_SHOW_NOTICE)
    page.wait_for_timeout(100)
    assert page.evaluate(_BOTTOM) <= 1
    _open_flyout(page, card)
    assert page.evaluate(_BOTTOM) <= 1
