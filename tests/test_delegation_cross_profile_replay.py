"""Option A for #7214: other profiles' ledgers get a read-only replay of WebUI-origin rows.

Runs against the installed Hermes Agent's real ledger code (skipped without it): the
launch ledger keeps the Agent's recovery and 48 h drop, every other ledger is only read.
"""

from __future__ import annotations

import hashlib
import json
import queue
import sqlite3
import subprocess
import threading
import time

import pytest

ad = pytest.importorskip("tools.async_delegation")
hc = pytest.importorskip("hermes_constants")
if not hasattr(hc, "set_hermes_home_override"):
    pytest.skip("Agent core without the context-local home override", allow_module_level=True)

from api import delegation_ledger_owner as owner_mod  # noqa: E402
from api import process_event_utils as peu  # noqa: E402

_SENTINEL = "__sweep_done__"


@pytest.fixture
def world(tmp_path, monkeypatch):
    import api.models as models
    import api.profiles as profiles

    monkeypatch.delenv("HERMES_WEBUI_CHAT_BACKEND", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_RUNTIME_ADAPTER", raising=False)
    root = (tmp_path / "root").resolve()
    homes = {"default": root}
    for name in ("named", "gwprof"):
        homes[name] = root / "profiles" / name
        homes[name].mkdir(parents=True)
        (homes[name] / "config.yaml").write_text("{}\n")
    (root / "config.yaml").write_text("{}\n")
    (homes["gwprof"] / "config.yaml").write_text("webui_chat_backend: gateway\n")
    sessions = {}
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: False)
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda name: root if name in (None, "", "default") else profiles._resolve_named_profile_home(name),
    )
    monkeypatch.setattr(profiles, "get_process_profile_home", lambda: root)  # the launch profile
    monkeypatch.setattr(models, "get_session_profile_readonly", lambda sid: sessions[sid])
    peu._reset_legacy_async_delivery_dedupe_for_tests()
    yield {"root": root, "homes": homes, "sessions": sessions, "tmp": tmp_path}
    peu._reset_legacy_async_delivery_dedupe_for_tests()


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _row(home, delegation_id, *, session="s-web", state="completed", age_hours=0.0, owner_pid=None):
    now = time.time()
    at = now - age_hours * 3600
    running = state == "running"
    event = None if running else json.dumps({
        "type": "async_delegation", "delegation_id": delegation_id, "status": state,
        "origin_ui_session_id": session, "session_key": "", "goal": "g",
    })
    with owner_mod.ledger_home_scope(home), ad._DB_LOCK, ad._transaction() as conn:
        conn.execute(
            """INSERT INTO async_delegations
               (delegation_id, origin_session, origin_ui_session_id, parent_session_id, state,
                dispatched_at, updated_at, completed_at, delivery_state, delivery_attempts,
                owner_pid, owner_started_at, event_json, result_json, origin_session_id)
               VALUES (?, '', ?, NULL, ?, ?, ?, ?, 'pending', 0, ?, NULL, ?, ?, '')""",
            (delegation_id, session, state, at, at, None if running else at,
             owner_pid if owner_pid is not None else _dead_pid(), event,
             None if running else json.dumps({"status": state})),
        )


def _rows(home):
    with sqlite3.connect(f"file:{home / 'state.db'}?mode=ro", uri=True) as conn:
        return {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT delegation_id, state, delivery_state FROM async_delegations")}


def _sweep(*homes):
    """Run one shared restore sweep over *homes* and return what it enqueued."""
    q = queue.Queue()
    for home in homes:
        assert peu._arm_async_delegation_restore_sweep(q, 0.2, ledger_home=home)
    # Deferred events are put after every ledger is processed: a completion marker.
    assert peu._arm_async_delegation_restore_sweep(
        q, 0.2, deferred_event={"type": "async_delegation", "delegation_id": _SENTINEL})
    seen = []
    while True:
        evt = q.get(timeout=10)
        if evt.get("delegation_id") == _SENTINEL:
            return seen
        seen.append(evt)


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def test_sweep_never_writes_another_profiles_ledger(world):
    named = world["homes"]["named"]
    world["sessions"]["s-web"] = "named"
    _row(named, "d-run", state="running", owner_pid=_dead_pid())
    _row(named, "d-old", age_hours=49)
    # A profile whose state.db is a symlink to a ledger outside the Hermes home.
    outside = world["tmp"] / "elsewhere"
    outside.mkdir()
    _row(outside, "d-ext-run", state="running", owner_pid=_dead_pid())
    _row(outside, "d-ext-old", age_hours=49)
    linked = world["root"] / "profiles" / "linked"
    linked.mkdir()
    (linked / "config.yaml").write_text("{}\n")
    (linked / "state.db").symlink_to(outside / "state.db")
    before_named = _rows(named)
    before_outside = _digest(outside / "state.db")
    before_listing = sorted(p.name for p in outside.iterdir())

    assert _sweep(named, linked) == []

    assert _rows(named) == before_named == {"d-run": ("running", "pending"), "d-old": ("completed", "pending")}
    assert _digest(outside / "state.db") == before_outside
    assert sorted(p.name for p in outside.iterdir()) == before_listing


def test_webui_origin_row_in_a_named_ledger_is_replayed_and_delivered_once(world):
    named = world["homes"]["named"]
    world["sessions"]["s-web"] = "named"
    _row(named, "d-1")
    q = queue.Queue()
    registry = type("Registry", (), {"restore_completions": lambda self: None, "completion_queue": q})()

    peu.restore_durable_process_completions(registry)
    evt = q.get(timeout=10)
    assert evt["delegation_id"] == "d-1" and evt.get("restored") is True
    time.sleep(0.3)
    assert q.empty()  # one copy
    assert not (world["root"] / "state.db").exists()  # the launch replay created no ledger

    claim = peu.claim_async_delegation_delivery(evt, "webui-test")
    assert claim is not None and claim.durable and claim.ledger_home == named
    peu.complete_async_delegation_delivery(evt, claim)
    assert _rows(named)["d-1"] == ("completed", "delivered")
    assert _sweep(named) == []  # a delivered row is never replayed again


def test_two_consumers_racing_for_a_replayed_row_claim_it_exactly_once(world):
    named = world["homes"]["named"]
    world["sessions"]["s-web"] = "named"
    _row(named, "d-1")
    assert [e["delegation_id"] for e in _sweep(named)] == ["d-1"]
    barrier = threading.Barrier(2)
    results = []

    def consumer(claim_id):
        with owner_mod.ledger_home_scope(named):
            barrier.wait()
            results.append(ad.claim_completion_delivery("d-1", claim_id))

    threads = [threading.Thread(target=consumer, args=(f"consumer-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert sorted(results) == [False, True]


def test_replayed_row_is_offered_once_until_released(world):
    named = world["homes"]["named"]
    world["sessions"]["s-web"] = "named"
    _row(named, "d-1")
    (evt,) = _sweep(named)
    assert _sweep(named) == []  # its copy is still out: no duplicate
    claim = peu.claim_async_delegation_delivery(evt, "webui-test")
    assert claim is not None
    peu.release_async_delegation_delivery(evt, claim, retryable=True)
    assert [e["delegation_id"] for e in _sweep(named)] == ["d-1"]  # handed back, offered again


def test_rows_not_from_an_in_process_webui_session_are_left_alone(world, monkeypatch):
    named, gwprof = world["homes"]["named"], world["homes"]["gwprof"]
    world["sessions"].update({"s-web": "named", "s-gw": "gwprof", "s-bad": "../etc"})
    _row(named, "d-web")
    _row(named, "d-platform", session="")  # Gateway/platform origin: no WebUI session
    _row(named, "d-unknown", session="s-gone")  # not a WebUI session
    _row(named, "d-bad", session="s-bad")  # session profile cannot be resolved
    _row(gwprof, "d-gw", session="s-gw")  # WebUI session on the Gateway backend
    before = (_rows(named), _rows(gwprof))

    assert [e["delegation_id"] for e in _sweep(named, gwprof)] == ["d-web"]
    assert (_rows(named), _rows(gwprof)) == before
    # Skipped because the profile really runs on the Gateway, not because the probe failed.
    from api.gateway_delegation_wakeup import _profile_uses_gateway

    assert _profile_uses_gateway("gwprof") and not _profile_uses_gateway("named")

    # Runner-owned turns do not run in this process either.
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "runner-local")
    peu._reset_legacy_async_delivery_dedupe_for_tests()
    assert _sweep(named) == []


def test_launch_ledger_keeps_the_agents_recovery_and_48h_drop(world):
    root = world["root"]
    _row(root, "d-run", state="running", owner_pid=_dead_pid())
    _row(root, "d-old", age_hours=49)
    _row(root, "d-platform", session="")  # the Agent's own replay is unfiltered, as on master

    replayed = {e["delegation_id"] for e in _sweep(root)}

    rows = _rows(root)
    assert rows["d-run"][0] == "unknown"
    assert rows["d-old"][1] == "dropped"
    assert "d-platform" in replayed and "d-old" not in replayed
