"""Regression tests for #6992 — in-place updates must not serve mixed-revision static assets.

Bug shape: ``api.updates._schedule_restart()`` mutates the WebUI checkout in
place (git stash/pull/pop, or checkout/clean/reset --hard for force updates)
before re-exec'ing the server. During that window the old process keeps serving
``/static/*`` from the changing working tree, and ``_serve_static()`` marked
every non-empty ``?v=`` response ``Cache-Control: immutable`` for a year —
without any check that the served bytes actually belong to the revision the
token claims. One page load could therefore receive JS from the new revision
and CSS from the old one under the SAME versioned URL (the reported ``Refine``
button with none of its new CSS), and the mismatched pair was cached
indefinitely because the client/server skew check saw a token match.

Fix (this PR) — freeze lifecycle in ``api.updates`` + ``api.routes._serve_static()``:
  1. While an in-place WebUI update is being applied, ``_serve_static()``
     refuses to serve static bytes: 503 + no-store, so nothing mixed is ever
     served or cached. The client's post-update reload lands on the restarted
     server, which serves one coherent revision.
  2. Reader drain (TOCTOU hole): every request that may touch the tree takes a
     read ticket (``begin_static_read``/``end_static_read``); the freeze sets
     the flag and then waits for all in-flight tickets before the first
     mutating git command, so a request admitted just before the freeze always
     finishes serving the OLD revision's bytes.
  3. Generation-owned handoff (handoff hole): each freeze returns an ownership
     token; ``unfreeze_static_serving`` only clears while the token is still
     current, so an earlier update can never clear a newer update's freeze.
  4. Persist-until-replacement (restart-delay hole): a successful update
     (restart scheduled) keeps static frozen at 503 through the restart delay
     until ``os.execv`` replaces the process; failure / no-op clears the
     freeze because the working tree is coherent again.
  5. ``immutable`` is only claimed when the token provably matches the revision
     this process was started on (``v == WEBUI_VERSION``); any other token is
     served with a short ``max-age=300`` instead of a year-long immutable cache.
"""

import pathlib
import threading
import time
import urllib.parse

import pytest

from api import routes


class FakeHandler:
    """Minimal stand-in for BaseHTTPRequestHandler (pattern from
    tests/test_extension_status_endpoint.py)."""

    def __init__(self):
        self.status = None
        self.headers = {}
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def header(self, name):
        for key, value in self.sent_headers:
            if key.lower() == name.lower():
                return value
        return None


@pytest.fixture(autouse=True)
def _reset_update_flag():
    """Ensure the update-freeze state never leaks between tests."""
    import api.updates as upd

    upd.reset_update_freeze()
    yield
    upd.reset_update_freeze()


def _serve(path: str):
    """Call routes._serve_static() directly with a fake handler."""
    handler = FakeHandler()
    parsed = urllib.parse.urlsplit(path)
    routes._serve_static(handler, parsed)
    return handler


# ── Freeze during the in-place update window ───────────────────────────────


def test_static_requests_frozen_503_during_update_window():
    """While an update is in progress, /static/* must not be served at all."""
    import api.updates as upd

    upd.freeze_static_serving()
    handler = _serve("/static/style.css?v=v-test-new")

    assert handler.status == 503
    cache_control = handler.header("Cache-Control")
    assert cache_control == "no-store"
    assert "immutable" not in (cache_control or "")
    assert b"error" in handler.body


def test_no_mixed_revision_pair_served_during_update_window():
    """The issue's source-level repro: with the tree mid-mutation, JS and CSS
    requested under the SAME version token must never both be served — the
    window serves nothing, so a mixed (new JS + old CSS) pair cannot occur."""
    import api.updates as upd

    upd.freeze_static_serving()
    js_handler = _serve("/static/messages.js?v=v-test-new")
    css_handler = _serve("/static/style.css?v=v-test-new")

    assert js_handler.status == 503
    assert css_handler.status == 503
    assert js_handler.header("Cache-Control") == "no-store"
    assert css_handler.header("Cache-Control") == "no-store"


def test_static_serving_resumes_after_update_window():
    """Once the freeze is lifted (failure/no-op path), serving works again."""
    import api.updates as upd

    upd.freeze_static_serving()
    assert _serve("/static/style.css?v=v-test-new").status == 503
    upd.reset_update_freeze()

    handler = _serve("/static/style.css?v=v-test-new")
    assert handler.status == 200
    assert len(handler.body) > 0


# ── immutable only when the token→bytes binding is provable ────────────────


def test_static_immutable_for_proven_version_token(monkeypatch):
    """v == WEBUI_VERSION (the revision this process started on) is the only
    token the server can bind to the bytes it serves → immutable is fine."""
    import api.updates as upd

    monkeypatch.setattr(upd, "WEBUI_VERSION", "v-test-1")
    handler = _serve("/static/messages.js?v=v-test-1")

    assert handler.status == 200
    assert "immutable" in handler.header("Cache-Control")


def test_static_not_immutable_for_unproven_token(monkeypatch):
    """A non-empty v that does NOT equal the process's revision cannot be
    verified against the served bytes → short cache, never immutable."""
    import api.updates as upd

    monkeypatch.setattr(upd, "WEBUI_VERSION", "v-test-1")
    handler = _serve("/static/messages.js?v=v-test-2")

    assert handler.status == 200
    cache_control = handler.header("Cache-Control")
    assert "immutable" not in cache_control
    assert cache_control == "public, max-age=300"


def test_static_unversioned_request_keeps_short_cache(monkeypatch):
    """No v token at all → unchanged short cache (no immutable regression)."""
    import api.updates as upd

    monkeypatch.setattr(upd, "WEBUI_VERSION", "v-test-1")
    handler = _serve("/static/messages.js")

    assert handler.status == 200
    cache_control = handler.header("Cache-Control")
    assert "immutable" not in cache_control
    assert cache_control == "public, max-age=300"


# ── The freeze flag lifecycle around the apply functions ───────────────────


def test_update_progress_flag_helpers():
    import api.updates as upd

    upd.reset_update_freeze()
    assert upd.update_in_progress() is False
    token = upd.freeze_static_serving()
    assert upd.update_in_progress() is True
    assert upd.unfreeze_static_serving(token) is True
    assert upd.update_in_progress() is False
    # A stale token can never clear a newer freeze (generation ownership).
    token_a = upd.freeze_static_serving()
    token_b = upd.freeze_static_serving()
    assert upd.unfreeze_static_serving(token_a) is False
    assert upd.update_in_progress() is True
    assert upd.unfreeze_static_serving(token_b) is True
    assert upd.update_in_progress() is False


def test_apply_update_freezes_static_during_apply(monkeypatch):
    """apply_update('webui') must hold the freeze while the checkout is being
    mutated and — because the update succeeds (restart scheduled) — KEEP it
    until the replacement, per the persist-until-replacement lifecycle."""
    import api.updates as upd

    observed = {}

    def fake_inner(target, channel):
        observed["target"] = target
        observed["flag_during_apply"] = upd.update_in_progress()
        return {"ok": True, "message": "fake ok", "target": target,
                "restart_scheduled": True}

    monkeypatch.setattr(upd, "_apply_update_inner", fake_inner)

    resp = upd.apply_update("webui")
    assert resp["ok"] is True
    assert observed["target"] == "webui"
    assert observed["flag_during_apply"] is True
    # Success → freeze persists until the scheduled restart replaces the process.
    assert upd.update_in_progress() is True
    assert _serve("/static/messages.js?v=v-test-new").status == 503
    upd.reset_update_freeze()


def test_apply_update_agent_does_not_freeze_webui_static(monkeypatch):
    """Agent-target updates do not mutate the WebUI checkout, so the freeze
    flag must stay clear."""
    import api.updates as upd

    observed = {}

    def fake_inner(target, channel):
        observed["flag_during_apply"] = upd.update_in_progress()
        return {"ok": True, "message": "fake ok", "target": target}

    monkeypatch.setattr(upd, "_apply_update_inner", fake_inner)

    resp = upd.apply_update("agent")
    assert resp["ok"] is True
    assert observed["flag_during_apply"] is False
    assert upd.update_in_progress() is False


def test_apply_force_update_freezes_static_during_apply(monkeypatch):
    """apply_force_update('webui') must hold the freeze through the
    checkout/clean/reset mutation and — because the update succeeds (restart
    scheduled) — KEEP it until the replacement."""
    import api.updates as upd

    observed = {}

    def fake_run_git(args, cwd, timeout=10):
        if args and args[0] == "reset":
            observed["flag_during_reset"] = upd.update_in_progress()
        return ("", True)

    monkeypatch.setattr(upd, "_run_git", fake_run_git)
    monkeypatch.setattr(upd, "_select_apply_compare_ref", lambda path, channel, target: "origin/master")
    monkeypatch.setattr(upd, "_head_contains_ref", lambda path, ref: False)
    monkeypatch.setattr(upd, "_can_fast_forward_to", lambda path, ref: True)
    monkeypatch.setattr(upd, "_schedule_restart", lambda *a, **k: None)

    resp = upd.apply_force_update("webui")

    assert resp["ok"] is True
    assert resp.get("restart_scheduled") is True
    assert observed.get("flag_during_reset") is True
    # Success → freeze persists until the scheduled restart replaces the process.
    assert upd.update_in_progress() is True
    assert _serve("/static/messages.js?v=v-test-new").status == 503
    upd.reset_update_freeze()


# ── Maintainer-review regressions: lifecycle holes #1-#3 ───────────────────


def test_freeze_drains_inflight_reader_before_mutation(monkeypatch):
    """TOCTOU hole #1: a request admitted just before the freeze must finish
    serving the OLD revision's bytes before the updater mutates the tree; the
    updater's freeze waits (drain) for the reader, and no request admitted
    after the freeze reads anything (503). Exercises the real production path
    (_serve_static → read_bytes) with an event-gated file read."""
    import api.updates as upd
    from api import config as api_config
    from api import routes

    # Use the real static tree with a fresh cache so the gated read_bytes is
    # actually exercised (cache hits would bypass the file read entirely).
    monkeypatch.setattr(routes, "_STATIC_CACHE", {}, raising=True)
    expected_old_bytes = (api_config.get_static_root() / "messages.js").read_bytes()
    monkeypatch.setattr(upd, "WEBUI_VERSION", "v-test-1")

    reader_started = threading.Event()
    reader_release = threading.Event()
    real_read_bytes = pathlib.Path.read_bytes

    def gated_read_bytes(self, *args, **kwargs):
        if str(self).endswith("messages.js"):
            reader_started.set()
            assert reader_release.wait(10), "test timed out releasing the reader"
        return real_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_bytes", gated_read_bytes)

    result = {}

    def do_request():
        handler = _serve("/static/messages.js?v=v-test-1")
        result["status"] = handler.status
        result["body"] = bytes(handler.body)

    reader = threading.Thread(target=do_request)
    reader.start()
    assert reader_started.wait(10), "reader never reached read_bytes"

    freeze_result = {}

    def do_freeze():
        freeze_result["token"] = upd.freeze_static_serving()
        freeze_result["done"] = True

    freezer = threading.Thread(target=do_freeze)
    freezer.start()

    # Wait for the freeze flag to be set, then verify the updater is blocked
    # in the drain (not done) and that new requests are rejected (503).
    deadline = time.monotonic() + 10
    while not upd.update_in_progress() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert upd.update_in_progress() is True, "freeze flag never set"
    time.sleep(0.2)
    assert not freeze_result.get("done"), \
        "freeze returned before the in-flight reader drained"
    assert _serve("/static/messages.js?v=v-test-1").status == 503

    # Release the reader: it serves the pre-mutation bytes untouched, then the
    # freeze drain completes and the updater may safely mutate.
    reader_release.set()
    reader.join(10)
    assert result["status"] == 200
    assert result["body"] == expected_old_bytes
    freezer.join(10)
    assert freeze_result.get("done") is True
    assert upd.update_in_progress() is True


def test_apply_update_success_keeps_freeze_until_replacement(monkeypatch):
    """Restart-delay hole #3: a successful update (restart scheduled) must keep
    /static/* frozen at 503 until the replacement happens; the replacement
    (new process) starts unfrozen."""
    import api.updates as upd

    monkeypatch.setattr(
        upd, "_apply_update_inner",
        lambda target, channel: {"ok": True, "message": "ok", "target": target,
                                 "restart_scheduled": True},
    )
    resp = upd.apply_update("webui")
    assert resp["ok"] is True and resp.get("restart_scheduled") is True
    assert upd.update_in_progress() is True
    assert _serve("/static/messages.js?v=v-test-new").status == 503

    # Simulate the replacement: the new process starts unfrozen.
    upd.reset_update_freeze()
    assert _serve("/static/messages.js?v=v-test-new").status == 200


def test_apply_update_failure_clears_freeze(monkeypatch):
    """A failed update leaves the tree coherent again → freeze must be lifted."""
    import api.updates as upd

    monkeypatch.setattr(
        upd, "_apply_update_inner",
        lambda target, channel: {"ok": False, "message": "fetch failed",
                                 "target": target},
    )
    resp = upd.apply_update("webui")
    assert resp["ok"] is False
    assert upd.update_in_progress() is False
    assert _serve("/static/messages.js?v=v-test-new").status == 200


def test_apply_update_noop_clears_freeze(monkeypatch):
    """An up-to-date no-op schedules no restart → freeze must be lifted."""
    import api.updates as upd

    monkeypatch.setattr(
        upd, "_apply_update_inner",
        lambda target, channel: {"ok": True, "message": "already up to date",
                                 "target": target, "up_to_date": True},
    )
    resp = upd.apply_update("webui")
    assert resp["ok"] is True and resp.get("restart_scheduled") is None
    assert upd.update_in_progress() is False
    assert _serve("/static/messages.js?v=v-test-new").status == 200


def test_handoff_a_cannot_clear_b_freeze():
    """Handoff hole #2: update A's stale unfreeze must never clear update B's
    freeze — ownership is generation-scoped."""
    import api.updates as upd

    # A freezes (e.g. successful update awaiting its scheduled restart).
    token_a = upd.freeze_static_serving()
    assert upd.update_in_progress() is True
    # B starts while A's freeze persists (restart-delay window) → B owns now.
    token_b = upd.freeze_static_serving()
    # A's stale unfreeze is a no-op.
    assert upd.unfreeze_static_serving(token_a) is False
    assert upd.update_in_progress() is True
    assert _serve("/static/messages.js?v=v-test-new").status == 503
    # B clears its own freeze (e.g. B failed or was a no-op).
    assert upd.unfreeze_static_serving(token_b) is True
    assert upd.update_in_progress() is False
    assert _serve("/static/messages.js?v=v-test-new").status == 200


def test_sequential_successful_updates_keep_freeze_until_replacement(monkeypatch):
    """A→B lock handoff through the real apply_update path: B takes over the
    freeze while A's is still pending and neither clears the other's."""
    import api.updates as upd

    def ok_inner(target, channel):
        return {"ok": True, "message": "ok", "target": target,
                "restart_scheduled": True}

    monkeypatch.setattr(upd, "_apply_update_inner", ok_inner)

    resp_a = upd.apply_update("webui")  # A: success, freeze persists
    assert resp_a["ok"] is True
    assert upd.update_in_progress() is True
    resp_b = upd.apply_update("webui")  # B: success, takes over the freeze
    assert resp_b["ok"] is True
    assert upd.update_in_progress() is True
    assert _serve("/static/messages.js?v=v-test-new").status == 503
    upd.reset_update_freeze()


def test_apply_force_update_failure_clears_freeze(monkeypatch):
    """Force-update failure (fetch) → freeze must be lifted."""
    import api.updates as upd

    def fake_run_git(args, cwd, timeout=10):
        if args and args[0] == "fetch":
            return ("could not resolve host: origin", False)
        return ("", True)

    monkeypatch.setattr(upd, "_run_git", fake_run_git)

    resp = upd.apply_force_update("webui")
    assert resp["ok"] is False
    assert upd.update_in_progress() is False
    assert _serve("/static/messages.js?v=v-test-new").status == 200


def test_apply_force_update_noop_clears_freeze(monkeypatch):
    """Force-update no-op (already up to date, nothing to force) schedules no
    restart → freeze must be lifted."""
    import api.updates as upd

    def fake_run_git(args, cwd, timeout=10):
        return ("", True)

    monkeypatch.setattr(upd, "_run_git", fake_run_git)
    monkeypatch.setattr(upd, "_select_apply_compare_ref", lambda path, channel, target: None)

    resp = upd.apply_force_update("webui")
    assert resp["ok"] is True and resp.get("up_to_date") is True
    assert resp.get("restart_scheduled") is None
    assert upd.update_in_progress() is False
    assert _serve("/static/messages.js?v=v-test-new").status == 200


# ── Re-gate: drain bound must FAIL CLOSED, never proceed with mutation ──────
#
# Original defect: freeze_static_serving() waited at most 30s for active
# readers, then logged "proceeding with mutation", broke out of the drain and
# returned a token while _active_readers > 0 — both update callers then ran
# their mutating git commands unconditionally. A reader held beyond the bound
# could observe the NEW bytes under the OLD process version URL with
# Cache-Control: immutable (mixed-revision cache poisoning, same class as the
# issue). Required fix shape (maintainer re-gate): fail closed — raise a
# dedicated timeout, cancel before the first mutating git operation, release
# only the caller's freeze generation, and return a retryable non-success.


def test_freeze_drain_timeout_raises_fail_closed(monkeypatch):
    """freeze_static_serving() must NEVER return mutation authority while an
    active reader remains: at the drain bound it raises a dedicated timeout
    carrying the caller's own generation token. The freeze stays in place
    until the caller releases exactly that generation; a later freeze/update
    succeeds once the reader releases."""
    import api.updates as upd

    upd.reset_update_freeze()
    # A reader that never drains within the bound.
    assert upd.begin_static_read() is True
    try:
        with pytest.raises(upd.StaticFreezeDrainTimeoutError) as excinfo:
            upd.freeze_static_serving(max_drain_wait_s=0.1)
        assert excinfo.value.token > 0
        # The freeze is still set — the timeout did not silently clear it.
        assert upd.update_in_progress() is True
        assert _serve("/static/style.css?v=v-test-new").status == 503
        # Release ONLY this caller's generation → coherent and retryable.
        assert upd.unfreeze_static_serving(excinfo.value.token) is True
        assert upd.update_in_progress() is False
        assert _serve("/static/style.css?v=v-test-new").status == 200
    finally:
        upd.end_static_read()
    # Once the reader released, a fresh freeze+unfreeze cycle works normally.
    token = upd.freeze_static_serving(max_drain_wait_s=1.0)
    assert upd.update_in_progress() is True
    assert upd.unfreeze_static_serving(token) is True
    assert upd.update_in_progress() is False


def test_apply_update_drain_timeout_fails_closed(monkeypatch):
    """Normal update path: with a real _serve_static() reader held past the
    drain bound, apply_update('webui') must abort BEFORE any mutating
    operation, release its own freeze generation, and return a retryable
    non-success — the old-version immutable URL keeps serving the OLD bytes,
    and a later update succeeds once the reader releases."""
    import api.updates as upd
    from api import config as api_config
    from api import routes

    monkeypatch.setattr(routes, "_STATIC_CACHE", {}, raising=True)
    monkeypatch.setattr(upd, "WEBUI_VERSION", "v-test-1")
    # Production-composed: the real callers call freeze_static_serving() with
    # the default bound, resolved at call time from this constant.
    monkeypatch.setattr(upd, "_FREEZE_DRAIN_MAX_WAIT_S", 0.2)
    expected_old_bytes = (api_config.get_static_root() / "messages.js").read_bytes()

    reader_started = threading.Event()
    reader_release = threading.Event()
    real_read_bytes = pathlib.Path.read_bytes

    def gated_read_bytes(self, *args, **kwargs):
        if str(self).endswith("messages.js"):
            reader_started.set()
            assert reader_release.wait(10), "test timed out releasing the reader"
        return real_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_bytes", gated_read_bytes)

    inner_calls = []

    def recording_inner(target, channel):
        inner_calls.append((target, channel))
        return {"ok": True, "message": "fake ok", "target": target,
                "restart_scheduled": True}

    monkeypatch.setattr(upd, "_apply_update_inner", recording_inner)

    result = {}

    def do_request():
        handler = _serve("/static/messages.js?v=v-test-1")
        result["status"] = handler.status
        result["body"] = bytes(handler.body)

    reader = threading.Thread(target=do_request)
    reader.start()
    assert reader_started.wait(10), "reader never reached read_bytes"

    resp = upd.apply_update("webui")

    # Fail-closed: retryable non-success, and NOTHING mutating ran.
    assert resp["ok"] is False
    assert resp.get("drain_timeout") is True
    assert resp.get("retryable") is True
    assert "reader" in resp["message"].lower()
    assert inner_calls == [], \
        f"update proceeded despite an undrained reader: {inner_calls}"
    # Freeze released for THIS caller's generation → serving is coherent again
    # (the tree was never touched). style.css is not gated, so this read is
    # safe while the messages.js reader is still held.
    assert upd.update_in_progress() is False
    assert _serve("/static/style.css?v=v-test-new").status == 200

    # Release the reader: it finishes serving the OLD revision's bytes under
    # the old token — the mixed-revision immutable exposure cannot happen.
    reader_release.set()
    reader.join(10)
    assert result["status"] == 200
    assert result["body"] == expected_old_bytes
    old_token = _serve("/static/messages.js?v=v-test-1")
    assert old_token.status == 200
    assert old_token.body == expected_old_bytes
    assert "immutable" in old_token.header("Cache-Control")

    # A later update succeeds once the reader released.
    resp2 = upd.apply_update("webui")
    assert resp2["ok"] is True and resp2.get("restart_scheduled") is True
    assert len(inner_calls) == 1
    upd.reset_update_freeze()


def test_apply_force_update_drain_timeout_fails_closed(monkeypatch):
    """Force update path: with a real _serve_static() reader held past the
    drain bound, apply_force_update('webui') must abort before ANY git command
    (not even the pre-flight fetch), release its own freeze generation, and
    return a retryable non-success — old-version immutable serving keeps
    binding to the old bytes, and a later force update succeeds once the
    reader releases."""
    import api.updates as upd
    from api import config as api_config
    from api import routes

    monkeypatch.setattr(routes, "_STATIC_CACHE", {}, raising=True)
    monkeypatch.setattr(upd, "WEBUI_VERSION", "v-test-1")
    monkeypatch.setattr(upd, "_FREEZE_DRAIN_MAX_WAIT_S", 0.2)
    expected_old_bytes = (api_config.get_static_root() / "messages.js").read_bytes()

    reader_started = threading.Event()
    reader_release = threading.Event()
    real_read_bytes = pathlib.Path.read_bytes

    def gated_read_bytes(self, *args, **kwargs):
        if str(self).endswith("messages.js"):
            reader_started.set()
            assert reader_release.wait(10), "test timed out releasing the reader"
        return real_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_bytes", gated_read_bytes)

    git_calls = []

    def recording_run_git(args, cwd, timeout=10):
        git_calls.append(list(args))
        return ("", True)

    monkeypatch.setattr(upd, "_run_git", recording_run_git)
    monkeypatch.setattr(
        upd, "_select_apply_compare_ref",
        lambda path, channel, target: "origin/master",
    )
    monkeypatch.setattr(upd, "_head_contains_ref", lambda path, ref: False)
    monkeypatch.setattr(upd, "_can_fast_forward_to", lambda path, ref: True)
    monkeypatch.setattr(upd, "_schedule_restart", lambda *a, **k: None)

    result = {}

    def do_request():
        handler = _serve("/static/messages.js?v=v-test-1")
        result["status"] = handler.status
        result["body"] = bytes(handler.body)

    reader = threading.Thread(target=do_request)
    reader.start()
    assert reader_started.wait(10), "reader never reached read_bytes"

    resp = upd.apply_force_update("webui")

    # Fail-closed: retryable non-success, and NO git command ran at all — the
    # abort happens before the first mutating operation inside _locked().
    assert resp["ok"] is False
    assert resp.get("drain_timeout") is True
    assert resp.get("retryable") is True
    assert "reader" in resp["message"].lower()
    assert git_calls == [], \
        f"force update ran git despite an undrained reader: {git_calls}"
    assert upd.update_in_progress() is False
    assert _serve("/static/style.css?v=v-test-new").status == 200

    reader_release.set()
    reader.join(10)
    assert result["status"] == 200
    assert result["body"] == expected_old_bytes
    old_token = _serve("/static/messages.js?v=v-test-1")
    assert old_token.status == 200
    assert old_token.body == expected_old_bytes
    assert "immutable" in old_token.header("Cache-Control")

    # A later force update succeeds once the reader released.
    resp2 = upd.apply_force_update("webui")
    assert resp2["ok"] is True and resp2.get("restart_scheduled") is True
    assert any(args[0] == "reset" for args in git_calls)
    assert upd.update_in_progress() is True
    upd.reset_update_freeze()
