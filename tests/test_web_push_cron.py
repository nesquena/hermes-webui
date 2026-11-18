"""Cross-process cron output -> opt-in, profile/device-pinned Web Push."""
import json
import os

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
