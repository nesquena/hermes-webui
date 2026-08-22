"""Regression tests for the #6992 re-gate: per-attempt mutation lifecycle.

The maintainer re-gate (2026-08-19, exact head ``347b1b1d``) found two
objective blockers:

1. ``api/updates.py::_locked()`` called the undefined
   ``_mark_update_mutation_started()`` — every force update that reached the
   mutation path raised ``NameError`` before checkout/reset, and the new
   exception handler then treated the attempt as pre-mutation (the
   ``_update_mutation_may_have_started`` flag was never set), unfroze static
   serving and re-raised.
2. The prior exception/failure contract was still open: ``apply_update()``
   had only ``finally: _apply_lock.release()`` — an unexpected exception
   after ``freeze_static_serving()`` left static serving permanently 503 —
   and both wrappers unfroze every ordinary ``restart_scheduled == false``
   response, including failures after checkout/clean/reset or
   stash/pull/restore may have changed the tree.

Fix (this PR): a per-attempt lifecycle authority owned by the exact freeze
generation token, shared by ``apply_force_update()``, ``apply_update()`` and
the WebUI-mutating clear-lock retry. States distinguish pre-mutation,
mutation-may-have-started, verified-coherent-recovery, and
replacement-owned. Pre-mutation failures (or verified rollbacks) may
generation-unfreeze; once mutation may have started with no verified
rollback the freeze is NEVER released — ownership transfers to a bounded
replacement (``_schedule_restart``) or the freeze persists fail-closed.

This file exercises every schedule the fix-spec requires: exception before
mutation, exception after mutation begins, each returned partial-mutation
failure, restart-thread start/worker failure, exact generation ownership,
lock reuse, and a bounded terminal outcome. Deleting the mutation-state
transition (``_mark_mutation_started``) makes the post-mutation tests fail.
"""

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
def _reset_update_state():
    """Ensure the update-freeze and lifecycle state never leaks between tests."""
    import api.updates as upd

    upd.reset_update_freeze()
    upd._reset_mutation_lifecycle()
    yield
    upd.reset_update_freeze()
    upd._reset_mutation_lifecycle()


def _serve(path: str):
    """Call routes._serve_static() directly with a fake handler."""
    handler = FakeHandler()
    parsed = urllib.parse.urlsplit(path)
    routes._serve_static(handler, parsed)
    return handler


def _force_update_git_stubs(monkeypatch):
    """Standard stubs so apply_force_update('webui') reaches _locked()."""
    import api.updates as upd

    monkeypatch.setattr(upd, "_run_git", lambda args, cwd, timeout=10: ("", True))
    monkeypatch.setattr(
        upd, "_select_apply_compare_ref",
        lambda path, channel, target: "origin/master",
    )
    monkeypatch.setattr(upd, "_head_contains_ref", lambda path, ref: False)
    monkeypatch.setattr(upd, "_can_fast_forward_to", lambda path, ref: True)


# ── apply_update: exception before / after mutation ────────────────────────


def test_apply_update_exception_before_mutation_unfreezes(monkeypatch):
    """Pre-mutation exception (inner raises before any mutating git command):
    the tree is untouched, so the freeze is generation-unfrozen and static
    serving resumes; the exception still propagates to the caller."""
    import api.updates as upd

    def boom_inner(target, channel):
        raise RuntimeError("fetch exploded before any mutation")

    monkeypatch.setattr(upd, "_apply_update_inner", boom_inner)

    with pytest.raises(RuntimeError):
        upd.apply_update("webui")

    assert upd.update_in_progress() is False
    assert _serve("/static/messages.js?v=v-test-new").status == 200


def test_apply_update_exception_after_mutation_begins_keeps_freeze(monkeypatch):
    """Post-mutation exception (inner raised after stash/pull may have run):
    NEVER unfreeze into a possibly-mixed tree — transfer ownership to a
    bounded replacement (_schedule_restart) and keep the freeze."""
    import api.updates as upd

    restart_calls = []

    def fake_schedule_restart(*a, **k):
        restart_calls.append(1)

    def boom_inner(target, channel):
        upd._mark_mutation_started()  # stash push / pull already ran
        raise RuntimeError("pull exploded mid-write")

    monkeypatch.setattr(upd, "_apply_update_inner", boom_inner)
    monkeypatch.setattr(upd, "_schedule_restart", fake_schedule_restart)

    with pytest.raises(RuntimeError):
        upd.apply_update("webui")

    assert restart_calls == [1], "post-mutation exception must schedule a replacement"
    assert upd.update_in_progress() is True
    assert _serve("/static/messages.js?v=v-test-new").status == 503
    upd.reset_update_freeze()


# ── apply_update: returned partial-mutation failures ───────────────────────


def test_apply_update_returned_pre_mutation_failure_unfreezes(monkeypatch):
    """Ordinary returned failure BEFORE any mutation (e.g. fetch lock error):
    pre-mutation → generation-unfreeze, no replacement scheduled."""
    import api.updates as upd

    restart_calls = []

    def fake_schedule_restart(*a, **k):
        restart_calls.append(1)

    def pre_mutation_failure_inner(target, channel):
        return {
            "ok": False,
            "message": "Fetch failed due to a repository lock: index.lock",
            "lock_conflict": True,
        }

    monkeypatch.setattr(upd, "_apply_update_inner", pre_mutation_failure_inner)
    monkeypatch.setattr(upd, "_schedule_restart", fake_schedule_restart)

    resp = upd.apply_update("webui")

    assert resp["ok"] is False
    assert resp.get("restart_scheduled") is None
    assert restart_calls == [], "pre-mutation failure must not schedule a replacement"
    assert upd.update_in_progress() is False
    assert _serve("/static/messages.js?v=v-test-new").status == 200


def test_apply_update_returned_partial_mutation_failure_keeps_freeze(monkeypatch):
    """Ordinary returned failure AFTER mutation may have started (stash
    pushed, pull failed, stash restore failed — tree may be mixed): the
    freeze MUST NOT be inferred coherent from the absence of
    restart_scheduled; ownership transfers to a bounded replacement."""
    import api.updates as upd

    restart_calls = []

    def fake_schedule_restart(*a, **k):
        restart_calls.append(1)

    def partial_mutation_failure_inner(target, channel):
        upd._mark_mutation_started()
        return {
            "ok": False,
            "message": (
                "Pull failed, and failed to clean up a stash-apply conflict "
                "while restoring local changes. Manual intervention needed: "
                "run git reset --hard HEAD to remove conflict markers."
            ),
            "stash_conflict": True,
        }

    monkeypatch.setattr(upd, "_apply_update_inner", partial_mutation_failure_inner)
    monkeypatch.setattr(upd, "_schedule_restart", fake_schedule_restart)

    resp = upd.apply_update("webui")

    assert resp["ok"] is False
    assert resp.get("restart_scheduled") is None
    assert restart_calls == [1], "partial-mutation failure must schedule a replacement"
    assert upd.update_in_progress() is True
    assert _serve("/static/messages.js?v=v-test-new").status == 503
    upd.reset_update_freeze()


def test_apply_update_verified_rollback_failure_unfreezes(monkeypatch):
    """Returned failure AFTER mutation but with a VERIFIED rollback (stash
    restored / reset completed): coherent again → generation-unfreeze."""
    import api.updates as upd

    restart_calls = []

    def fake_schedule_restart(*a, **k):
        restart_calls.append(1)

    def rollback_failure_inner(target, channel):
        upd._mark_mutation_started()
        upd._mark_coherent_recovery()  # stash pop restored the pre-update tree
        return {"ok": False, "message": "pull diverged", "diverged": True}

    monkeypatch.setattr(upd, "_apply_update_inner", rollback_failure_inner)
    monkeypatch.setattr(upd, "_schedule_restart", fake_schedule_restart)

    resp = upd.apply_update("webui")

    assert resp["ok"] is False
    assert resp.get("restart_scheduled") is None
    assert restart_calls == [], "verified rollback must not schedule a replacement"
    assert upd.update_in_progress() is False
    assert _serve("/static/messages.js?v=v-test-new").status == 200


# ── apply_force_update: exception before / after mutation ──────────────────


def test_apply_force_update_exception_before_mutation_unfreezes(monkeypatch):
    """Force update: exception during the pre-flight fetch (before the
    mutation mark) → pre-mutation, generation-unfreeze, exception propagates."""
    import api.updates as upd

    def exploding_git(args, cwd, timeout=10):
        if args and args[0] == "fetch":
            raise RuntimeError("git fetch crashed")
        return ("", True)

    monkeypatch.setattr(upd, "_run_git", exploding_git)

    with pytest.raises(RuntimeError):
        upd.apply_force_update("webui")

    assert upd.update_in_progress() is False
    assert _serve("/static/messages.js?v=v-test-new").status == 200


def test_apply_force_update_exception_after_mutation_begins_keeps_freeze(monkeypatch):
    """Force update: exception DURING checkout/clean/reset (after the
    mutation mark in _locked) → never unfreeze; schedule a bounded
    replacement and keep the freeze."""
    import api.updates as upd

    restart_calls = []

    def fake_schedule_restart(*a, **k):
        restart_calls.append(1)

    def exploding_discard(path, reset_ref):
        raise RuntimeError("git reset crashed mid-mutation")

    _force_update_git_stubs(monkeypatch)
    monkeypatch.setattr(upd, "_discard_local_changes", exploding_discard)
    monkeypatch.setattr(upd, "_schedule_restart", fake_schedule_restart)

    with pytest.raises(RuntimeError):
        upd.apply_force_update("webui")

    assert restart_calls == [1], "post-mutation exception must schedule a replacement"
    assert upd.update_in_progress() is True
    assert _serve("/static/messages.js?v=v-test-new").status == 503
    upd.reset_update_freeze()


# ── apply_force_update: returned partial-mutation failures ─────────────────


def test_apply_force_update_returned_pre_mutation_failure_unfreezes(monkeypatch):
    """Force update: ordinary returned failure at the fetch (pre-mutation) →
    generation-unfreeze, no replacement scheduled."""
    import api.updates as upd

    restart_calls = []

    def fake_schedule_restart(*a, **k):
        restart_calls.append(1)

    def failing_git(args, cwd, timeout=10):
        if args and args[0] == "fetch":
            return ("could not resolve host: origin", False)
        return ("", True)

    monkeypatch.setattr(upd, "_run_git", failing_git)
    monkeypatch.setattr(upd, "_schedule_restart", fake_schedule_restart)

    resp = upd.apply_force_update("webui")

    assert resp["ok"] is False
    assert restart_calls == [], "pre-mutation failure must not schedule a replacement"
    assert upd.update_in_progress() is False
    assert _serve("/static/messages.js?v=v-test-new").status == 200


def test_apply_force_update_returned_partial_mutation_failure_keeps_freeze(monkeypatch):
    """Force update: ordinary returned failure AFTER checkout/clean/reset may
    have run (reset --hard failed) → tree may be mixed; do NOT infer
    coherence from the absence of restart_scheduled — keep the freeze and
    schedule a bounded replacement."""
    import api.updates as upd

    restart_calls = []

    def fake_schedule_restart(*a, **k):
        restart_calls.append(1)

    _force_update_git_stubs(monkeypatch)
    monkeypatch.setattr(upd, "_discard_local_changes", lambda path, reset_ref: False)
    monkeypatch.setattr(upd, "_schedule_restart", fake_schedule_restart)

    resp = upd.apply_force_update("webui")

    assert resp["ok"] is False
    assert resp.get("restart_scheduled") is None
    assert restart_calls == [1], "partial-mutation failure must schedule a replacement"
    assert upd.update_in_progress() is True
    assert _serve("/static/messages.js?v=v-test-new").status == 503
    upd.reset_update_freeze()


# ── Restart-thread start/worker failure → bounded terminal outcome ─────────


def test_restart_thread_start_failure_keeps_freeze_fail_closed(monkeypatch):
    """Restart-thread start failure (thread AND synchronous fallback both
    fail): the shared authority must NOT unfreeze — fail-closed terminal
    outcome (503) until a later update or manual restart clears it."""
    import api.updates as upd

    def failing_thread_start(target):
        raise RuntimeError("thread start failed")

    def failing_sync_fallback(**kwargs):
        raise RuntimeError("synchronous fallback failed")

    def boom_inner(target, channel):
        upd._mark_mutation_started()
        raise RuntimeError("post-mutation failure")

    monkeypatch.setattr(upd, "_apply_update_inner", boom_inner)
    monkeypatch.setattr(upd, "_start_restart_thread", failing_thread_start)
    monkeypatch.setattr(upd, "_wait_until_restart_safe", failing_sync_fallback)

    with pytest.raises(RuntimeError):
        upd.apply_update("webui")

    # Bounded terminal outcome: freeze persists (fail-closed), static is 503,
    # and the state survives until the process is replaced.
    assert upd.update_in_progress() is True
    assert _serve("/static/messages.js?v=v-test-new").status == 503
    upd.reset_update_freeze()


# ── Exact generation ownership / lock reuse ────────────────────────────────


def test_generation_ownership_stale_token_cannot_clear_newer_freeze():
    """A stale attempt settling with its own (superseded) freeze generation
    must never clear a newer attempt's freeze — exact generation ownership."""
    import api.updates as upd

    token_a = upd.freeze_static_serving()
    upd._mark_freeze_acquired(token_a)
    # A newer attempt takes over the freeze while A is still in flight.
    token_b = upd.freeze_static_serving()
    upd._mark_freeze_acquired(token_b)

    # A settles with its own (now stale) token → unfreeze is a no-op.
    upd._settle_update_lifecycle(token_a)
    assert upd.update_in_progress() is True
    assert _serve("/static/messages.js?v=v-test-new").status == 503

    # B settles with the current generation → freeze clears.
    upd._settle_update_lifecycle(token_b)
    assert upd.update_in_progress() is False
    assert _serve("/static/messages.js?v=v-test-new").status == 200


def test_apply_lock_released_after_exception_for_reuse(monkeypatch):
    """After a pre-mutation exception the wrapper's finally must release
    _apply_lock, so a subsequent update attempt can proceed (lock reuse)."""
    import api.updates as upd

    def boom_inner(target, channel):
        raise RuntimeError("boom before mutation")

    monkeypatch.setattr(upd, "_apply_update_inner", boom_inner)

    with pytest.raises(RuntimeError):
        upd.apply_update("webui")

    # The lock was released by the wrapper's finally.
    assert upd._apply_lock.acquire(blocking=False) is True
    upd._apply_lock.release()

    # A subsequent attempt runs normally.
    def ok_inner(target, channel):
        return {"ok": True, "message": "ok", "target": target,
                "restart_scheduled": True}

    monkeypatch.setattr(upd, "_apply_update_inner", ok_inner)
    resp = upd.apply_update("webui")
    assert resp["ok"] is True and resp.get("restart_scheduled") is True
    assert upd.update_in_progress() is True
    upd.reset_update_freeze()


# ── Clear-lock retry shares the lifecycle authority ────────────────────────


def test_apply_clear_lock_webui_retry_uses_lifecycle_authority(monkeypatch):
    """The WebUI-mutating clear-lock retry re-runs _apply_update_inner and
    must use the SAME per-attempt lifecycle authority: freeze before the
    mutating path, settle on ordinary failure returns."""
    import api.updates as upd

    restart_calls = []

    def fake_schedule_restart(*a, **k):
        restart_calls.append(1)

    def no_lock_inventory(path):
        return {
            "well_known_lock_present": False,
            "well_known_lock_path": str(path / ".git" / "index.lock"),
            "other_locks": [],
        }

    def partial_mutation_failure_inner(target, channel):
        upd._mark_mutation_started()
        return {
            "ok": False,
            "message": "stash restore failed; changes remain in git stash",
            "stash_conflict": True,
        }

    monkeypatch.setattr(upd, "_restart_blocker_snapshot", lambda: {})
    monkeypatch.setattr(upd, "_inventory_locks", no_lock_inventory)
    monkeypatch.setattr(upd, "_apply_update_inner", partial_mutation_failure_inner)
    monkeypatch.setattr(upd, "_schedule_restart", fake_schedule_restart)
    monkeypatch.setattr(upd, "_read_update_channel", lambda: "stable")

    resp = upd.apply_clear_lock("webui")

    assert resp["ok"] is False
    assert resp.get("lock_recovery", {}).get("action") == "no-lock-found"
    assert restart_calls == [1], "clear-lock retry must schedule a replacement"
    assert upd.update_in_progress() is True
    assert _serve("/static/messages.js?v=v-test-new").status == 503
    upd.reset_update_freeze()


def test_apply_clear_lock_agent_retry_does_not_freeze(monkeypatch):
    """Agent-target clear-lock retries never touch the WebUI tree — no
    freeze, no lifecycle authority needed."""
    import api.updates as upd

    def no_lock_inventory(path):
        return {
            "well_known_lock_present": False,
            "well_known_lock_path": str(path / ".git" / "index.lock"),
            "other_locks": [],
        }

    calls = []

    def recording_inner(target, channel):
        calls.append((target, channel))
        return {"ok": True, "message": "ok", "target": target}

    monkeypatch.setattr(upd, "_restart_blocker_snapshot", lambda: {})
    monkeypatch.setattr(upd, "_inventory_locks", no_lock_inventory)
    monkeypatch.setattr(upd, "_apply_update_inner", recording_inner)
    monkeypatch.setattr(upd, "_read_update_channel", lambda: "stable")

    resp = upd.apply_clear_lock("agent")

    assert resp["ok"] is True
    assert calls == [("agent", "stable")]
    assert upd.update_in_progress() is False
