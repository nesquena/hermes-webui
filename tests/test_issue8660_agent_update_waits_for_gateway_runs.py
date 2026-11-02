"""#8660: an Agent update waits for active resumable Gateway runs again.

#7293 (#8486, exp-v0.52.587) left Gateway runs whose resumable run identity is
saved out of the update blocker check, because they survive a WebUI restart.
An Agent update also cycles the Gateway that executes them, so for
``target == 'agent'`` they block an unforced update again (v0.52.544 parity).
A WebUI-only update keeps the #7293 exemption.
"""

from __future__ import annotations

import time

import pytest

from api import config
from api import gateway_chat
from api import run_drain


@pytest.fixture(autouse=True)
def _clean_registries():
    run_drain._reset_for_tests()
    with gateway_chat._DURABLE_RUN_STREAMS_LOCK:
        old_durable = set(gateway_chat._DURABLE_RUN_STREAMS)
        gateway_chat._DURABLE_RUN_STREAMS.clear()
    with config.STREAMS_LOCK:
        old_streams = dict(config.STREAMS)
        old_claims = dict(config.PRE_ADMISSION_CLAIMS)
        config.STREAMS.clear()
        config.PRE_ADMISSION_CLAIMS.clear()
    with config.ACTIVE_RUNS_LOCK:
        old_runs = dict(config.ACTIVE_RUNS)
        config.ACTIVE_RUNS.clear()
    try:
        yield
    finally:
        run_drain._reset_for_tests()
        with config.STREAMS_LOCK:
            config.STREAMS.clear()
            config.STREAMS.update(old_streams)
            config.PRE_ADMISSION_CLAIMS.clear()
            config.PRE_ADMISSION_CLAIMS.update(old_claims)
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.clear()
            config.ACTIVE_RUNS.update(old_runs)
        with gateway_chat._DURABLE_RUN_STREAMS_LOCK:
            gateway_chat._DURABLE_RUN_STREAMS.clear()
            gateway_chat._DURABLE_RUN_STREAMS.update(old_durable)


def _add_run(stream_id, *, backend="legacy", durable_gateway=False, stream=False):
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS[stream_id] = {
            "stream_id": stream_id,
            "session_id": f"sess-{stream_id}",
            "backend": backend,
            "phase": "running",
            "started_at": time.time(),
        }
    if stream:
        with config.STREAMS_LOCK:
            config.STREAMS[stream_id] = object()
    if durable_gateway:
        # As _record_gateway_run does once the run identity is saved.
        gateway_chat._mark_gateway_run_durable(stream_id)


@pytest.fixture
def upd(tmp_path, monkeypatch):
    import api.updates as upd

    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(upd, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(upd, "_AGENT_DIR", tmp_path)
    return upd


# Every Agent-update entry point goes through _run_fenced_update.
ENTRY_POINTS = [
    ("apply_update", "_apply_update_inner", ("agent",)),
    ("apply_force_update", "_apply_force_update_inner", ("agent",)),
    ("apply_clear_lock", "_apply_clear_lock_inner", ("agent",)),
]


@pytest.mark.parametrize("entry,inner,args", ENTRY_POINTS, ids=[e[0] for e in ENTRY_POINTS])
def test_agent_update_refuses_while_a_resumable_gateway_run_is_active(upd, monkeypatch, entry, inner, args):
    monkeypatch.setattr(upd, inner, lambda *a, **k: pytest.fail(f"{inner} must not run"))
    _add_run("gw-1", backend="gateway", durable_gateway=True, stream=True)
    result = getattr(upd, entry)(*args)
    assert result["ok"] is False
    assert result["restart_blocked"] is True
    assert result["active_runs"] == 1
    assert result["blocking_run_ids"] == ["gw-1"]
    assert result["force_available"] is True
    assert run_drain.run_admission_closed_response() is None, "a refusal must not leave admission fenced"


def test_agent_refusal_counts_webui_and_resumable_gateway_runs_together(upd, monkeypatch):
    monkeypatch.setattr(upd, "_apply_update_inner", lambda *a, **k: pytest.fail("must not update"))
    _add_run("legacy-1", stream=True)
    _add_run("gw-1", backend="gateway", durable_gateway=True)
    result = upd.apply_update("agent")
    assert result["restart_blocked"] is True
    assert result["active_runs"] == 2
    assert sorted(result["blocking_run_ids"]) == ["gw-1", "legacy-1"]


def test_webui_update_still_ignores_a_resumable_gateway_run(upd, monkeypatch):
    """#7293 stays: a WebUI-only restart leaves the Gateway, and its run, running."""
    seen = []
    monkeypatch.setattr(upd, "_apply_update_inner", lambda target, channel: seen.append(target) or {"ok": False, "message": "pull failed"})
    _add_run("gw-1", backend="gateway", durable_gateway=True, stream=True)
    result = upd.apply_update("webui")
    assert seen == ["webui"]
    assert result == {"ok": False, "message": "pull failed"}


def test_agent_update_rechecks_resumable_gateway_runs_after_fencing(upd, monkeypatch):
    """A Gateway run that becomes resumable between the first check and the fence still refuses."""
    monkeypatch.setattr(upd, "_apply_update_inner", lambda *a, **k: pytest.fail("must not update"))
    real_hold = run_drain.hold_run_admission

    def hold_after_race(owner, *, force=False):
        _add_run("gw-raced-in", backend="gateway", durable_gateway=True)
        return real_hold(owner, force=force)

    monkeypatch.setattr(run_drain, "hold_run_admission", hold_after_race)
    result = upd.apply_update("agent")
    assert result["ok"] is False
    assert result["restart_blocked"] is True
    assert result["blocking_run_ids"] == ["gw-raced-in"]
    assert run_drain.run_admission_closed_response() is None


def test_forced_agent_update_proceeds_with_resumable_gateway_runs(upd, monkeypatch):
    seen = []
    monkeypatch.setattr(upd, "_apply_update_inner", lambda target, channel: seen.append(target) or {"ok": False, "message": "pull failed"})
    _add_run("gw-1", backend="gateway", durable_gateway=True)
    result = upd.apply_update("agent", force_active_runs=True)
    assert seen == ["agent"]
    assert result == {"ok": False, "message": "pull failed"}
    assert run_drain.run_admission_closed_response() is None


def test_all_active_runs_is_webui_owned_runs_plus_resumable_gateway_runs():
    _add_run("legacy-1", stream=True)
    _add_run("gw-pending", backend="gateway")  # no saved run identity yet: WebUI-owned
    _add_run("gw-durable", backend="gateway", durable_gateway=True, stream=True)
    with config.STREAMS_LOCK:
        config.STREAMS["stream-only"] = object()  # a registered stream whose worker is still launching
    owned = {r["stream_id"]: r for r in run_drain.webui_owned_active_runs()}
    every = {r["stream_id"]: r for r in run_drain.all_active_runs()}
    assert set(owned) == {"legacy-1", "gw-pending", "stream-only"}
    assert set(every) == set(owned) | {"gw-durable"}
    assert every["gw-durable"]["registered_stream"] is True
    assert every["gw-durable"]["backend"] == "gateway"
    for sid in owned:
        assert every[sid] == owned[sid]


def test_no_runs_means_no_agent_update_blocker(upd):
    snap = upd._update_blocker_snapshot("agent")
    assert snap["restart_blocked"] is False
    assert snap["active_runs"] == 0


def test_agent_snapshot_does_not_depend_on_the_durable_mark(upd, monkeypatch):
    """One pass with no durability check: a mark that changes mid-read cannot drop or double a run.

    Both round-1 gates reproduced a double count from two reads (handoff between them), and the round-2
    senior gate a run in neither read (mark released before the row is unregistered). A single pass that
    never asks whether a run is durable has neither window.
    """
    _add_run("gw-1", backend="gateway", durable_gateway=True, stream=True)
    _add_run("legacy-1", stream=True)

    def must_not_ask(sid):
        pytest.fail("the Agent snapshot must not depend on whether a Gateway run is durable")

    monkeypatch.setattr(run_drain, "_gateway_run_durably_resumable", must_not_ask)
    snap = upd._update_blocker_snapshot("agent")
    assert snap["active_runs"] == 2
    assert sorted(snap["blocking_run_ids"]) == ["gw-1", "legacy-1"]


@pytest.mark.parametrize("durable", [True, False], ids=["durable", "mark-released-before-unregister"])
def test_a_gateway_run_counts_exactly_once_whatever_its_durable_mark(upd, durable):
    """Worker teardown releases the durable mark before it unregisters the row; both states block once."""
    _add_run("gw-teardown", backend="gateway", durable_gateway=durable, stream=True)
    snap = upd._update_blocker_snapshot("agent")
    assert snap["active_runs"] == 1
    assert snap["active_streams"] == 1
    assert snap["blocking_run_ids"] == ["gw-teardown"]
    assert snap["blocking_stream_ids"] == ["gw-teardown"]
    assert snap["restart_blocked"] is True


def test_agent_snapshot_counts_every_run_and_truncates_ids_to_ten_unique(upd):
    _add_run("gw-pending", backend="gateway", stream=True)
    for i in range(3):
        _add_run(f"legacy-{i}", stream=True)
    for i in range(8):
        _add_run(f"gw-durable-{i}", backend="gateway", durable_gateway=True, stream=True)
    snap = upd._update_blocker_snapshot("agent")
    assert snap["active_runs"] == 12
    assert snap["active_streams"] == 12
    for key in ("blocking_run_ids", "blocking_stream_ids"):
        assert len(snap[key]) == 10, key
        assert len(set(snap[key])) == 10, key
        assert snap[key][0] == "gw-pending", key


def test_webui_target_snapshot_is_the_restart_snapshot(upd):
    _add_run("legacy-1", stream=True)
    _add_run("gw-1", backend="gateway", durable_gateway=True, stream=True)
    assert upd._update_blocker_snapshot("webui") == upd._restart_blocker_snapshot()
    assert upd._update_blocker_snapshot("webui")["blocking_run_ids"] == ["legacy-1"]
