"""#8517 follow-up, Codex r1: the memory-off notice through the REAL page scripts.

Loads static/index.html with every real script in Chromium, swaps EventSource for a
deterministic fixture, and drives the real ``attachLiveStream`` listeners (warning,
memory_status, compressed, done) and the real journal-snapshot recovery helper:

1. reload/reattach resumes after the warning's seq and still shows the notice, from
   the status the run journal snapshot carries (dismissal respected);
2. a turn that reports no status (transient) keeps the notice; only an explicit
   healthy ``memory_status`` clears it; a server that never sends one keeps it;
3. compression continuation (s1 -> s2) keeps the notice on the continuing session.
"""
from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
_BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]

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
window.fixtureStream=()=>window.fixtureSources.findLast(s=>s.url.includes('api/chat/stream?'));
"""

_SETUP = """()=>{
  S.session={session_id:'s1',title:'Notice gate',workspace:'/tmp/notice-gate',messages:[],message_count:0,model:''};
  S.messages=[];S.busy=true;S.activeStreamId='st1';
  window._streamingFade=false;
  renderMessages(); syncTopbar();
  return $('memoryOffNotice').hidden;
}"""

_OFF = "{type:'memory_off',session_id:'s1',stream_id:'st1',reason:'provider_shared_instance',scope:'external',message:'x'}"
_STATE = "()=>({sid:S.session&&S.session.session_id,hidden:$('memoryOffNotice').hidden,reason:$('memoryOffNotice').dataset.reason||''})"


def _route(r):
    path = urlsplit(r.request.url).path
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
def page(browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, service_workers="block")
    ctx.add_init_script(_INIT)
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.route("**/*", _route)
    pg.goto("http://notice-gate.test/", wait_until="load")
    assert pg.evaluate(_SETUP) is True
    yield pg
    assert errors == []
    ctx.close()


def _done(sid="s1"):
    return ("fixtureStream().emit('done',{session:{...S.session,session_id:'%s',"
            "messages:[{role:'assistant',content:'Complete',timestamp:123}],message_count:1}});" % sid)


def test_reattach_after_the_warning_restores_the_notice_from_the_journal_snapshot(page):
    state = page.evaluate("""()=>{
      const snapshot={stream_id:'st1',last_seq:20,last_event_id:'st1:20',last_assistant_text:'Working',
        messages:[],tool_calls:[],memory_status:{state:'external_off',reason:'provider_shared_instance',scope:'external'}};
      INFLIGHT.s1=_serverLiveSnapshotInflight(snapshot,[]);
      attachLiveStream('s1','st1',[],{reconnecting:true});
      return {hidden:$('memoryOffNotice').hidden,reason:$('memoryOffNotice').dataset.reason,cursor:INFLIGHT.s1.lastRunJournalSeq};
    }""")
    assert state == {"hidden": False, "reason": "provider_shared_instance", "cursor": 20}
    page.wait_for_timeout(50)
    urls = page.evaluate("()=>fixtureSources.map(s=>s.url)")
    assert any("after_seq=20" in u for u in urls), urls


def test_reload_through_load_session_restores_the_notice(browser):
    """The real reload path: ``loadSession`` reads the session payload's run journal
    snapshot, resumes the stream after the warning (after_seq=20), and shows the
    notice from the snapshot's memory status."""
    snapshot = {
        "session_id": "s1", "stream_id": "st1", "last_seq": 20, "last_event_id": "st1:20",
        "last_assistant_text": "Working", "messages": [], "tool_calls": [],
        "memory_status": {"state": "external_off", "reason": "provider_shared_instance", "scope": "external"},
    }
    session = {
        "session_id": "s1", "title": "Notice gate", "workspace": "/tmp/notice-gate", "messages": [],
        "message_count": 0, "model": "", "active_stream_id": "st1", "pending_user_message": "hi",
        "pending_started_at": 1, "runtime_journal": {"active": True, "stream_id": "st1"},
        "runtime_journal_snapshot": snapshot,
    }

    def route(r):
        if urlsplit(r.request.url).path == "/api/session":
            r.fulfill(json={"session": session})
        else:
            _route(r)

    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, service_workers="block")
    ctx.add_init_script(_INIT)
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.route("**/*", route)
    try:
        pg.goto("http://notice-gate.test/", wait_until="load")
        assert pg.evaluate("()=>$('memoryOffNotice').hidden") is True
        pg.evaluate("async()=>{await loadSession('s1');}")
        pg.wait_for_timeout(200)
        state = pg.evaluate("""()=>({sid:S.session&&S.session.session_id,hidden:$('memoryOffNotice').hidden,
          reason:$('memoryOffNotice').dataset.reason||'',urls:fixtureSources.map(s=>s.url)})""")
        assert any("api/chat/stream?" in u and "after_seq=20" in u for u in state["urls"]), state
        assert (state["sid"], state["hidden"], state["reason"]) == ("s1", False, "provider_shared_instance"), state
        assert errors == []
    finally:
        ctx.close()


def test_reattach_respects_a_dismissal(page):
    page.evaluate("()=>localStorage.setItem('hermes-webui-memory-off-dismissed:s1:provider_shared_instance','1')")
    hidden = page.evaluate("""()=>{
      const snapshot={stream_id:'st1',last_seq:20,last_event_id:'st1:20',last_assistant_text:'Working',
        messages:[],tool_calls:[],memory_status:{state:'external_off',reason:'provider_shared_instance',scope:'external'}};
      INFLIGHT.s1=_serverLiveSnapshotInflight(snapshot,[]);
      attachLiveStream('s1','st1',[],{reconnecting:true});
      return $('memoryOffNotice').hidden;
    }""")
    assert hidden is True


def test_transient_turn_keeps_the_notice_and_only_an_explicit_ok_clears_it(page):
    page.evaluate("()=>{attachLiveStream('s1','st1'); fixtureStream().emit('warning',%s,'st1:2');}" % _OFF)
    assert page.evaluate(_STATE)["hidden"] is False
    # Transient turn (or an older server): the turn finishes with no memory status.
    page.evaluate("()=>{S.busy=true;S.activeStreamId='st2';attachLiveStream('s1','st2');%s}" % _done())
    page.wait_for_timeout(50)
    kept = page.evaluate(_STATE)
    assert kept == {"sid": "s1", "hidden": False, "reason": "provider_shared_instance"}, kept
    # Memory is back: the server says so explicitly.
    page.evaluate("""()=>{S.busy=true;S.activeStreamId='st3';attachLiveStream('s1','st3');
      fixtureStream().emit('memory_status',{type:'memory_status',state:'ok',session_id:'s1',stream_id:'st3'},'st3:1');}""")
    assert page.evaluate(_STATE)["hidden"] is True


def test_compression_continuation_keeps_the_notice(page):
    page.evaluate("()=>{attachLiveStream('s1','st1'); fixtureStream().emit('warning',%s,'st1:2');}" % _OFF)
    page.evaluate("""()=>{fixtureStream().emit('compressed',{session_id:'s1',old_session_id:'s1',new_session_id:'s2',continuation_session_id:'s2'});
      %s}""" % _done("s2"))
    page.wait_for_timeout(50)
    after = page.evaluate(_STATE)
    assert after == {"sid": "s2", "hidden": False, "reason": "provider_shared_instance"}, after
