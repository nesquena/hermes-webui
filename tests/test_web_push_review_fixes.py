"""Regression tests for the PR #8101 review findings."""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from api import web_push
from tests.test_web_push import (  # noqa: F401
    APPLE, OWNER_A, _enable, _sub, push_env,
)

ROOT = Path(__file__).resolve().parent.parent
SECRET = "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"


@pytest.fixture
def env(push_env, monkeypatch):
    _enable(monkeypatch, push_env)
    monkeypatch.setattr(web_push, "_SEEN", {})
    sent = []
    monkeypatch.setattr(web_push, "enqueue", lambda p: sent.append(p) or True)
    web_push.add_subscription(_sub(APPLE), owner=OWNER_A)
    web_push.register_session_owner("s1", OWNER_A)
    return sent


# 1. credential masking -------------------------------------------------------

def test_completion_body_is_masked_even_when_api_redact_disabled(env, monkeypatch):
    monkeypatch.setattr("api.config.load_settings", lambda: {"api_redact_enabled": False})
    web_push.notify_session_done("s1", [{"role": "assistant", "content": f"your key is {SECRET} ok"}])
    assert env
    assert SECRET not in json.dumps(env[0])


def test_secret_straddling_snippet_cut_is_not_leaked(env):
    pad = "x " * 55  # secret begins just before the 120-char cut
    web_push.notify_response_complete("s1", pad + f"token {SECRET}")
    body = env[0]["options"]["body"]
    assert SECRET not in body and SECRET[:12] not in body


def test_snippets_can_be_disabled(env, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_PUSH_SNIPPETS", "0")
    web_push.notify_response_complete("s1", "private reply text")
    assert env[0]["options"]["body"] == "Task finished"


def test_approval_and_clarify_bodies_masked(env):
    web_push.notify_approval_required("s1", {"approval_id": "a1", "description": f"curl -H 'Authorization: Bearer {SECRET}'"})
    web_push.notify_clarify_required("s1", {"clarify_id": "c1", "question": f"use {SECRET}?"})
    assert SECRET not in json.dumps(env)


def test_fails_closed_when_redactor_unavailable(env, monkeypatch):
    import api.helpers as h
    monkeypatch.setattr(h, "_redact_text", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    web_push.notify_response_complete("s1", f"{SECRET}")
    assert SECRET not in json.dumps(env)


# 2. panels.js disable ordering + opt-out -------------------------------------

def _toggle_src():
    js = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    start = js.index("async function toggleWebPush()")
    return js, js[start:js.index("async function sendWebPushTest", start)]


def test_disable_unsubscribes_browser_before_deleting_server_record():
    _, src = _toggle_src()
    off = src[src.index("dataset.subscribed==='1'"):src.index("Must run inside the click gesture")]
    assert off.index("_webPushSetOptOut(true)") < off.index("existing.unsubscribe()")
    assert off.index("existing.unsubscribe()") < off.index("method:'DELETE'")
    # failure of unsubscribe aborts with an error and no success toast first
    assert "web_push_disable_failed" in off
    assert off.index("web_push_disable_failed") < off.index("method:'DELETE'")
    assert off.index("return;") < off.index("method:'DELETE'")


def test_enable_clears_opt_out_and_boot_rebind_respects_it():
    js, src = _toggle_src()
    assert "_webPushSetOptOut(false)" in src
    boot = js[js.index("async function bindWebPushOnBoot()"):js.index("async function toggleWebPush()")]
    assert "_webPushOptedOut()" in boot
    assert boot.index("_webPushOptedOut()") < boot.index("/api/push/subscribe")


def test_disable_failed_i18n_present():
    assert "web_push_disable_failed" in (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


# 3. VAPID script default state dir -------------------------------------------

def test_generate_vapid_keys_uses_server_default_state_dir(tmp_path):
    pytest.importorskip("py_vapid")
    home = tmp_path / "hh"
    e = {k: v for k, v in os.environ.items() if k != "HERMES_WEBUI_STATE_DIR"}
    e.update(HERMES_HOME=str(home), HERMES_WEBUI_VAPID_SUBJECT="mailto:t@example.com")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_vapid_keys.py")],
                       env=e, capture_output=True, text=True, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert (home / "webui" / "webui_vapid.json").exists()


def test_generate_vapid_keys_default_dir_resolution_matches_config(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    import generate_vapid_keys as g
    from api.config import STATE_DIR
    assert g._default_state_dir() == str(STATE_DIR)


def test_docs_describe_default_state_dir():
    assert "HERMES_HOME" in (ROOT / "docs" / "web-push.md").read_text(encoding="utf-8")


# 4. queued clarify / approval pushes -----------------------------------------

def test_each_queued_clarify_pushes_once_when_it_becomes_head(monkeypatch):
    from api import clarify
    pushed = []
    monkeypatch.setattr(web_push, "notify_clarify_required", lambda sk, d: pushed.append(d["clarify_id"]))
    key = "clarify-queue-test"
    try:
        e1 = clarify.submit_pending(key, {"question": "one?"})
        e2 = clarify.submit_pending(key, {"question": "two?"})
        e3 = clarify.submit_pending(key, {"question": "three?"})
        assert pushed == [e1.clarify_id]            # queued-behind entries don't push yet
        clarify.resolve_clarify(key, "a")
        assert pushed == [e1.clarify_id, e2.clarify_id]
        assert clarify.resolve_clarify_by_id(key, e3.clarify_id, "x")   # non-head: no push
        assert pushed == [e1.clarify_id, e2.clarify_id]
        e4 = clarify.submit_pending(key, {"question": "four?"})
        assert clarify.resolve_clarify_by_id(key, e2.clarify_id, "b")   # head resolved -> e4 active
        assert pushed[-1] == e4.clarify_id and len(pushed) == 3
    finally:
        clarify.resolve_clarify(key, "", resolve_all=True)


def test_clarify_push_deduped_per_request_id(env):
    web_push.notify_clarify_required("s1", {"clarify_id": "c1", "question": "q"})
    web_push.notify_clarify_required("s1", {"clarify_id": "c1", "question": "q"})
    web_push.notify_clarify_required("s1", {"clarify_id": "c2", "question": "q"})
    assert len(env) == 2


def test_each_queued_approval_pushes_when_it_becomes_head(monkeypatch):
    from api import route_approvals as ra
    pushed = []
    monkeypatch.setattr(web_push, "is_enabled", lambda: True)
    monkeypatch.setattr(web_push, "_SEEN", {})
    monkeypatch.setattr(web_push, "_targets_for_session", lambda sid: ["o"])
    monkeypatch.setattr(web_push, "enqueue", lambda p: pushed.append(p["options"]["body"]) or True)
    key = "approval-queue-test"

    def wait(n):
        for _ in range(100):
            if len(pushed) >= n:
                return
            time.sleep(0.02)

    try:
        ra.submit_pending(key, {"approval_id": "a1", "command": "x", "description": "d1"})
        ra.submit_pending(key, {"approval_id": "a2", "command": "y", "description": "d2"})
        wait(1); time.sleep(0.1)
        assert pushed == ["d1"]
        with ra._lock:
            q = ra._pending[key]
            q.pop(0)
            ra._approval_sse_notify_locked(key, q[0], len(q))
        wait(2)
        assert pushed == ["d1", "d2"]
    finally:
        with ra._lock:
            ra._pending.pop(key, None)
