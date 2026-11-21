"""Cross-process cron output -> opt-in, profile/device-pinned Web Push."""
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from api import web_push
from tests.test_web_push import (
    APPLE, FCM, DEV_A, OWNER_A, OWNER_B, _Capture, _H, _enable,
    _stub_pinned_session, _sub,
)


def _output(home, stamp, name="run.md"):
    path = home / "cron" / "output" / "job-one" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("## Prompt\nPRIVATE PROMPT\n## Response\nPRIVATE RESULT")
    os.utime(path, ns=(stamp, stamp))
    return path


def test_cron_requires_explicit_profile_device_opt_in(push_env, tmp_path, monkeypatch):
    from api import profiles, web_push_cron

    _enable(monkeypatch, push_env)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: a)
    web_push.add_subscription(_sub(APPLE), owner=OWNER_A)
    web_push.add_subscription(_sub(FCM), owner=OWNER_B)
    observer = web_push_cron.CronObserver(started_ns=100)
    queued = []
    monkeypatch.setattr(web_push, "enqueue", lambda p: queued.append(p) or True)
    _output(a, 200)
    _output(b, 200)
    observer.poll(now_ns=300)
    assert queued == []
    cap = _Capture(monkeypatch)
    cap.routes._handle_push_cron(_H(), {"endpoint": APPLE, "enabled": True}, now_ns=300)
    assert cap.out[-1] == (200, {"ok": True, "enabled": True})
    _output(a, 400, "next.md")
    _output(b, 400, "next.md")
    observer.poll(now_ns=500)
    assert len(queued) == 1
    assert queued[0]["_owners"] == [OWNER_A]
    assert queued[0]["_cron_home"] == str(a.resolve())
    assert "PRIVATE" not in json.dumps(queued)
    observer.poll(now_ns=600)
    assert len(queued) == 1


def test_cron_opt_in_cannot_target_another_devices_subscription(push_env, tmp_path, monkeypatch):
    from api import profiles

    _enable(monkeypatch, push_env)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    web_push.add_subscription(_sub(FCM), owner=OWNER_B)
    cap = _Capture(monkeypatch)
    cap.routes._handle_push_cron(_H(DEV_A), {"endpoint": FCM, "enabled": True})
    assert cap.out[-1][0] == 409
    assert not web_push.cron_routes()


def test_foreign_session_request_does_not_enroll_push_owner(push_env, tmp_path, monkeypatch):
    from api import models, routes

    _enable(monkeypatch, push_env)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", sessions)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda profile, handler: profile == "default")
    (sessions / "foreign.json").write_text(json.dumps({"session_id": "foreign", "profile": "private"}))
    routes._note_session_device(_H(), "foreign")
    assert web_push.session_owners("foreign") == []


@pytest.mark.parametrize("revoke", ["opt_out", "unsubscribe"])
def test_queued_cron_delivery_rechecks_consent(push_env, tmp_path, monkeypatch, revoke):
    _enable(monkeypatch, push_env)
    _stub_pinned_session(monkeypatch)
    web_push.add_subscription(_sub(APPLE), owner=OWNER_A)
    web_push.set_cron_opt_in(APPLE, OWNER_A, tmp_path, True, now_ns=100)
    payload = web_push.notification_payload("Cron output ready", "Task finished", owners=[OWNER_A])
    payload.update(_cron_home=str(tmp_path.resolve()), _cron_stamp=200)
    if revoke == "opt_out":
        assert web_push.set_cron_opt_in(APPLE, OWNER_A, tmp_path, False)
    else:
        assert web_push.remove_subscription(APPLE, OWNER_A)
    sent = []
    monkeypatch.setattr(web_push, "_pywebpush", lambda: (lambda **kw: sent.append(kw), Exception))
    assert web_push._send_to_all(payload) == 0
    assert sent == []


@pytest.mark.parametrize("revoke", ["opt_out", "unsubscribe", "transfer", "reconsent", "corrupt"])
def test_mid_fanout_revocation_skips_later_endpoint(push_env, tmp_path, monkeypatch, revoke):
    _enable(monkeypatch, push_env)
    _stub_pinned_session(monkeypatch)
    for endpoint in (APPLE, FCM):
        web_push.add_subscription(_sub(endpoint), owner=OWNER_A)
        web_push.set_cron_opt_in(endpoint, OWNER_A, tmp_path, True, now_ns=100)
    payload = web_push.notification_payload("Cron output ready", "Task finished", owners=[OWNER_A])
    payload.update(_cron_home=str(tmp_path.resolve()), _cron_stamp=200)
    sent = []

    def send(**kwargs):
        sent.append(kwargs["subscription_info"]["endpoint"])
        if len(sent) != 1:
            return
        if revoke == "opt_out":
            web_push.set_cron_opt_in(FCM, OWNER_A, tmp_path, False)
        elif revoke == "unsubscribe":
            web_push.remove_subscription(FCM, OWNER_A)
        elif revoke == "transfer":
            web_push.add_subscription(_sub(FCM), owner=OWNER_B)
        elif revoke == "reconsent":
            web_push.set_cron_opt_in(FCM, OWNER_A, tmp_path, False)
            web_push.set_cron_opt_in(FCM, OWNER_A, tmp_path, True, now_ns=300)
        else:
            (push_env / "webui_push_subscriptions.json").write_text("corrupt")

    monkeypatch.setattr(web_push, "_pywebpush", lambda: (send, Exception))
    assert web_push._send_to_all(payload) == 1
    assert sent == [APPLE]


def test_cron_delivery_strips_profile_routing_and_never_broadcasts(push_env, tmp_path, monkeypatch):
    _enable(monkeypatch, push_env)
    _stub_pinned_session(monkeypatch)
    monkeypatch.setenv("HERMES_WEBUI_PUSH_BROADCAST_UNOWNED", "1")
    web_push.add_subscription(_sub(APPLE), owner=OWNER_A)
    web_push.add_subscription(_sub(FCM), owner=OWNER_B)
    web_push.set_cron_opt_in(APPLE, OWNER_A, tmp_path, True, now_ns=100)
    payload = web_push.notification_payload("Cron output ready", "Task finished", owners=[OWNER_A])
    payload.update(_cron_home=str(tmp_path.resolve()), _cron_stamp=200)
    sent = []
    monkeypatch.setattr(web_push, "_pywebpush", lambda: (lambda **kw: sent.append(kw), Exception))
    assert web_push._send_to_all(payload) == 1
    assert [s["subscription_info"]["endpoint"] for s in sent] == [APPLE]
    assert str(tmp_path) not in sent[0]["data"]
    assert "_cron" not in sent[0]["data"] and "_owners" not in sent[0]["data"]


def test_profile_pinning_survives_env_switch_and_identical_job_names(push_env, tmp_path, monkeypatch):
    from api import web_push_cron

    _enable(monkeypatch, push_env)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    web_push.add_subscription(_sub(APPLE), owner=OWNER_A)
    web_push.add_subscription(_sub(FCM), owner=OWNER_B)
    web_push.set_cron_opt_in(APPLE, OWNER_A, a, True, now_ns=100)
    web_push.set_cron_opt_in(FCM, OWNER_B, b, True, now_ns=100)
    observer = web_push_cron.CronObserver(started_ns=100)
    monkeypatch.setenv("HERMES_HOME", str(b))
    _output(a, 200)
    _output(b, 200)
    queued = []
    monkeypatch.setattr(web_push, "enqueue", lambda p: queued.append(p) or True)
    observer.poll(now_ns=300)
    assert {(p["_cron_home"], tuple(p["_owners"])) for p in queued} == {
        (str(a.resolve()), (OWNER_A,)), (str(b.resolve()), (OWNER_B,))
    }
    assert len({p["options"]["tag"] for p in queued}) == 2


def test_restart_ignores_history_and_cross_process_output_reaches_queue(push_env, tmp_path, monkeypatch):
    from api import web_push_cron

    _enable(monkeypatch, push_env)
    web_push.add_subscription(_sub(APPLE), owner=OWNER_A)
    web_push.set_cron_opt_in(APPLE, OWNER_A, tmp_path, True, now_ns=50)
    _output(tmp_path, 90, "history.md")
    observer = web_push_cron.CronObserver(started_ns=100)
    queued = []
    monkeypatch.setattr(web_push, "enqueue", lambda p: queued.append(p) or True)
    observer.poll(now_ns=150)
    assert queued == []
    # A different process, no WebUI imports and no SSE browser, publishes output
    # with the Agent's save_job_output shape (atomic .md rename).
    target = tmp_path / "cron" / "output" / "job-one" / "new.md"
    subprocess.run([sys.executable, "-c",
                    "import os,sys; from pathlib import Path; p=Path(sys.argv[1]); "
                    "q=p.with_suffix('.tmp'); q.write_text('## Response\\nfixture result'); "
                    "os.utime(q, ns=(200,200)); os.replace(q,p)", str(target)], check=True, timeout=10)
    observer.poll(now_ns=300)
    observer.poll(now_ns=400)
    assert len(queued) == 1
    assert queued[0]["title"] == "Cron output ready"
    observer.close()


@pytest.mark.parametrize("link_kind", ["home", "output", "job", "file"])
def test_observer_does_not_follow_symlinks(push_env, tmp_path, monkeypatch, link_kind):
    from api import web_push_cron

    _enable(monkeypatch, push_env)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    foreign = _output(b, 200)
    web_push.add_subscription(_sub(APPLE), owner=OWNER_A)
    web_push.set_cron_opt_in(APPLE, OWNER_A, a, True, now_ns=100)
    if link_kind == "home":
        a.rmdir()
        a.symlink_to(b, target_is_directory=True)
    elif link_kind == "output":
        (a / "cron").mkdir()
        (a / "cron" / "output").symlink_to(b / "cron" / "output", target_is_directory=True)
    else:
        out = a / "cron" / "output"
        out.mkdir(parents=True)
        if link_kind == "job":
            (out / "job-one").symlink_to(foreign.parent, target_is_directory=True)
        else:
            (out / "job-one").mkdir()
            (out / "job-one" / "run.md").symlink_to(foreign)
    queued = []
    monkeypatch.setattr(web_push, "enqueue", lambda p: queued.append(p) or True)
    observer = web_push_cron.CronObserver(started_ns=100)
    observer.poll(now_ns=300)
    assert queued == []
    observer.close()


def test_bounded_scans_resume_without_duplicate_delivery_and_opt_out_closes_them(push_env, tmp_path, monkeypatch):
    from api import web_push_cron

    _enable(monkeypatch, push_env)
    monkeypatch.setattr(web_push_cron, "_BATCH", 1)
    web_push.add_subscription(_sub(APPLE), owner=OWNER_A)
    web_push.set_cron_opt_in(APPLE, OWNER_A, tmp_path, True, now_ns=100)
    for i in range(4):
        _output(tmp_path, 200, f"{i}.md")
    observer = web_push_cron.CronObserver(started_ns=100)
    queued = []
    monkeypatch.setattr(web_push, "enqueue", lambda p: queued.append(p) or False)  # queue full: dropped, not retried
    for _ in range(10):
        before = len(queued)
        observer.poll(now_ns=300)
        assert len(queued) - before <= 1
    assert len(queued) == 4
    web_push.set_cron_opt_in(APPLE, OWNER_A, tmp_path, False)
    observer.poll(now_ns=400)
    assert observer.cursors == {}


def test_consent_persists_across_rebind_rotation_but_not_device_transfer(push_env, tmp_path):
    web_push.add_subscription(_sub(APPLE), owner=OWNER_A)
    assert web_push.set_cron_opt_in(APPLE, OWNER_A, tmp_path, True, now_ns=100)
    assert web_push.set_cron_opt_in(APPLE, OWNER_A, tmp_path, True, now_ns=200)  # idempotent
    web_push.add_subscription(_sub(APPLE), owner=OWNER_A)  # silent boot rebind
    assert web_push.cron_routes() == {str(tmp_path.resolve()): [(OWNER_A, 100)]}
    web_push.add_subscription(_sub(FCM), previous_endpoint=APPLE, owner=OWNER_A)
    assert web_push.cron_routes() == {str(tmp_path.resolve()): [(OWNER_A, 100)]}
    web_push.add_subscription(_sub(FCM), owner=OWNER_B)
    assert web_push.cron_routes() == {}


@pytest.mark.parametrize("body", [{}, {"endpoint": APPLE, "enabled": "false"}, {"endpoint": APPLE, "enabled": 1}])
def test_cron_opt_in_rejects_malformed_consent(push_env, monkeypatch, body):
    _enable(monkeypatch, push_env)
    cap = _Capture(monkeypatch)
    cap.routes._handle_push_cron(_H(), body)
    assert cap.out[-1][0] == 400
    assert web_push.cron_routes() == {}


def test_corrupt_opt_in_store_fails_closed_without_rewrite(push_env, monkeypatch):
    from api import web_push_cron

    _enable(monkeypatch, push_env)
    path = push_env / "webui_push_subscriptions.json"
    path.write_text('{"subscriptions": "corrupt"}')
    before = path.read_bytes()
    with pytest.raises(web_push.PushStoreUnavailable):
        web_push_cron.CronObserver().poll()
    cap = _Capture(monkeypatch)
    cap.routes._handle_push_cron(_H(), {"endpoint": APPLE, "enabled": True})
    assert cap.out[-1][0] == 503
    assert path.read_bytes() == before


def test_disabled_cron_observer_is_inert(push_env, monkeypatch):
    from api import web_push_cron

    monkeypatch.setattr(web_push, "cron_routes", lambda: pytest.fail("disabled feature read the store"))
    web_push_cron.CronObserver().poll()


def test_cron_dispatch_has_strict_csrf_gate(monkeypatch):
    from api import routes
    from tests.test_web_push_owners import _auth_on, _dispatch

    _auth_on(monkeypatch)
    calls = []
    monkeypatch.setattr(routes, "_handle_push_cron", lambda *a, **kw: calls.append(True))
    out = _dispatch(monkeypatch, "POST", "/api/push/cron", {"Cookie": "c=cookieVALID"}, calls)
    assert (403, {"error": "Session expired - reload the page"}) in out
    assert calls == []
    _dispatch(monkeypatch, "POST", "/api/push/cron",
              {"Cookie": "c=cookieVALID", "X-Hermes-CSRF-Token": "goodtoken"}, calls)
    assert calls == [True]


def test_disabled_start_creates_no_background_thread(push_env, monkeypatch):
    from api import web_push_cron

    monkeypatch.setattr(web_push_cron, "_THREAD", None)
    monkeypatch.setattr(web_push_cron, "_STOP", threading.Event())
    web_push_cron.start()
    try:
        assert web_push_cron._THREAD is None
    finally:
        web_push_cron.stop()


def test_background_observer_to_delivery_worker_without_sse(push_env, tmp_path, monkeypatch):
    from api import web_push_cron

    _enable(monkeypatch, push_env)
    _stub_pinned_session(monkeypatch)
    web_push.add_subscription(_sub(APPLE), owner=OWNER_A)
    web_push.set_cron_opt_in(APPLE, OWNER_A, tmp_path, True)
    monkeypatch.setattr(web_push_cron, "_THREAD", None)
    monkeypatch.setattr(web_push_cron, "_STOP", threading.Event())
    monkeypatch.setattr(web_push_cron, "_INTERVAL", 0.01)
    monkeypatch.setattr(web_push, "_WORKERS", [])
    monkeypatch.setattr(web_push, "_STOP", threading.Event())
    import queue
    monkeypatch.setattr(web_push, "_QUEUE", queue.Queue(maxsize=web_push._MAX_PENDING))
    delivered = threading.Event()
    scanned = threading.Event()
    wait_seconds = 10  # Fixture-only scheduling budget, not a delivery deadline.
    poll = web_push_cron.CronObserver.poll

    def note_scan(observer):
        poll(observer)
        scanned.set()  # Observe a completed real poll, not just store-read entry.

    monkeypatch.setattr(web_push_cron.CronObserver, "poll", note_scan)
    senders = []

    def send(**kwargs):
        senders.append((threading.current_thread(), kwargs))
        delivered.set()

    monkeypatch.setattr(web_push, "_pywebpush", lambda: (send, Exception))
    try:
        web_push_cron.start()
        # Ensure the observer's startup baseline precedes the new cron output.
        assert scanned.wait(wait_seconds), "cron observer never completed its first poll"
        _output(tmp_path, time.time_ns())
        assert delivered.wait(wait_seconds), "cron output never reached the delivery worker"
    finally:
        web_push_cron.stop()
        if web_push_cron._THREAD is not None:
            web_push_cron._THREAD.join(timeout=wait_seconds)
        # shutdown divides its budget between workers. The old 1s budget gave
        # each only 0.5s, racing their real 0.5s idle queue wait at teardown.
        web_push.shutdown(wait=wait_seconds)
    assert not web_push_cron._THREAD.is_alive()
    assert all(not worker.is_alive() for worker in web_push._WORKERS)
    assert len(senders) == 1
    sender, kwargs = senders[0]
    assert sender in web_push._WORKERS
    assert sender is not web_push_cron._THREAD
    assert kwargs["subscription_info"]["endpoint"] == APPLE
    assert json.loads(kwargs["data"])["title"] == "Cron output ready"


def test_late_atomic_publish_is_not_lost_to_scan_wall_time(push_env, tmp_path, monkeypatch):
    from api import web_push_cron

    _enable(monkeypatch, push_env)
    web_push.add_subscription(_sub(APPLE), owner=OWNER_A)
    web_push.set_cron_opt_in(APPLE, OWNER_A, tmp_path, True, now_ns=100)
    observer = web_push_cron.CronObserver(started_ns=100)
    queued = []
    monkeypatch.setattr(web_push, "enqueue", lambda p: queued.append(p) or True)
    # The producer prepared bytes at 200, but published them after a scan at 300.
    observer.poll(now_ns=300)
    _output(tmp_path, 200)
    observer.poll(now_ns=400)
    observer.poll(now_ns=500)
    assert len(queued) == 1
    observer.close()


def test_recent_identity_memory_is_bounded_without_replay(push_env, tmp_path, monkeypatch):
    from api import web_push_cron

    _enable(monkeypatch, push_env)
    monkeypatch.setattr(web_push_cron, "_RECENT", 2)
    web_push.add_subscription(_sub(APPLE), owner=OWNER_A)
    web_push.set_cron_opt_in(APPLE, OWNER_A, tmp_path, True, now_ns=100)
    observer = web_push_cron.CronObserver(started_ns=100)
    queued = []
    monkeypatch.setattr(web_push, "enqueue", lambda p: queued.append(p) or True)
    for stamp in (200, 300, 400, 500):
        _output(tmp_path, stamp, f"{stamp}.md")
        observer.poll(now_ns=600)
        assert len(observer.cursors[str(tmp_path.resolve())]["seen"]) <= 2
    observer.poll(now_ns=700)
    observer.poll(now_ns=800)
    assert len(queued) == 4
    observer.close()


def test_store_cannot_expand_observer_beyond_profile_limit(push_env, monkeypatch):
    monkeypatch.setattr(web_push, "_MAX_CRON_PROFILES", 1)
    records = [dict(_sub(APPLE), owner=OWNER_A, cron_profiles={"/profile/a": 100}),
               dict(_sub(FCM), owner=OWNER_B, cron_profiles={"/profile/b": 100})]
    (push_env / "webui_push_subscriptions.json").write_text(json.dumps({"subscriptions": records}))
    with pytest.raises(web_push.PushStoreUnavailable):
        web_push.cron_routes()


def test_status_reports_only_callers_current_profile_consent(push_env, tmp_path, monkeypatch):
    from api import profiles
    from urllib.parse import urlparse

    _enable(monkeypatch, push_env)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    web_push.add_subscription(_sub(APPLE), owner=OWNER_A)
    web_push.set_cron_opt_in(APPLE, OWNER_A, a, True)
    cap = _Capture(monkeypatch)
    query = urlparse("/api/push/status?endpoint=" + APPLE)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: b)
    cap.routes._handle_push_status(_H(), query)
    assert cap.out[-1][1]["cron_subscribed"] is False
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: a)
    cap.routes._handle_push_status(_H(), query)
    assert cap.out[-1][1]["cron_subscribed"] is True
    assert str(a) not in json.dumps(cap.out)
    assert OWNER_A not in json.dumps(cap.out)


# Settings control for cron opt-in (static-source wiring) ----------------------

def _static(name):
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / "static" / name).read_text(encoding="utf-8")


def _fn_src(js, start, end):
    i = js.index(start)
    return js[i:js.index(end, i + len(start))]


def test_settings_cron_checkbox_lives_inside_closed_app_push_block():
    html = _static("index.html")
    block = html[html.index('id="webPushSettings"'):]
    assert 'id="webPushCronToggle"' in block
    assert block.index('id="webPushStatus"') < block.index('id="webPushCronToggle"')
    # hidden until updateWebPushStatus() proves the device is subscribed
    assert 'id="webPushCronRow" style="display:none' in html
    assert 'onchange="toggleWebPushCron(this.checked)"' in html
    assert 'data-i18n="web_push_cron_label"' in html


def test_cron_checkbox_is_gated_on_push_enabled_and_subscribed_and_reads_cron_subscribed():
    js = _static("panels.js")
    upd = _fn_src(js, "async function updateWebPushStatus()", "// Boot-time silent re-bind")
    # hidden on entry, and the early returns (push disabled / unsupported) never reveal it
    assert upd.index("_webPushSetCronRow(false,false)") < upd.index("!info.enabled")
    assert upd.index("!info.enabled") < upd.index("_webPushSetCronRow(subscribed,cronOn)")
    assert upd.index("_webPushSupported()") < upd.index("_webPushSetCronRow(subscribed,cronOn)")
    assert "cronOn=!!(st&&st.cron_subscribed)" in upd
    assert "/api/push/status?endpoint=" in upd
    # a superseded refresh (e.g. after a profile switch) must not paint stale state
    assert "seq!==_webPushStatusSeq" in upd
    row = _fn_src(js, "function _webPushSetCronRow(", "let _webPushStatusSeq")
    assert "subscribed?'flex':'none'" in row
    assert "box.checked=!!(subscribed&&cronOn)" in row


def test_cron_toggle_posts_with_push_headers_disables_in_flight_and_reverts_on_failure():
    js = _static("panels.js")
    fn = _fn_src(js, "async function toggleWebPushCron(", "async function sendWebPushTest")
    assert "api('/api/push/cron',{method:'POST',headers:_webPushHeaders()" in fn
    assert "JSON.stringify({endpoint:sub.endpoint,enabled:want})" in fn
    # nothing profile- or path-shaped is client-supplied
    assert "profile" not in fn.lower()
    # in-flight lock is released in finally, after being taken before the request
    assert fn.index("box.disabled=true") < fn.index("api('/api/push/cron'")
    assert "finally{" in fn and fn.index("finally{") < fn.index("box.disabled=false")
    # success commits only after the server accepted; failure reverts + inline status line
    assert fn.index("api('/api/push/cron'") < fn.index("box.checked=want")
    catch = fn[fn.index("catch(e){"):fn.index("finally{")]
    assert "box.checked=!want" in catch
    assert "_webPushSetStatus(t('web_push_cron_failed'))" in catch


def test_cron_strings_ride_the_en_fallback_not_pasted_into_every_locale():
    i18n = _static("i18n.js")
    en = i18n[i18n.index("  en: {"):i18n.index("\n  it: {")]
    for key in ("web_push_cron_label", "web_push_cron_failed"):
        assert en.count(f"    {key}:") == 1
        assert i18n.count(f"    {key}:") == 1, f"{key} must live in en only (docs/GUIDELINES.md #8)"
