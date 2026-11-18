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
    routes = web_push.cron_routes


    def note_scan():
        scanned.set()
        return routes()

    monkeypatch.setattr(web_push, "cron_routes", note_scan)
    thread_names = []

    def send(**kwargs):
        thread_names.append(threading.current_thread().name)
        delivered.set()

    monkeypatch.setattr(web_push, "_pywebpush", lambda: (send, Exception))
    web_push_cron.start()
    try:
        # Ensure the observer's startup baseline precedes the new cron output.
        assert scanned.wait(3)
        _output(tmp_path, time.time_ns())
        assert delivered.wait(3), "cron output never reached the delivery worker"
        assert thread_names[0].startswith("web-push-")
        assert thread_names[0] != "web-push-cron"
    finally:
        web_push_cron.stop()
        web_push.shutdown(wait=1)
    assert not web_push_cron._THREAD.is_alive()
    assert all(not worker.is_alive() for worker in web_push._WORKERS)
