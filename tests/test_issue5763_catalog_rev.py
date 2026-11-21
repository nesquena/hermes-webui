"""#5763 round 14 (omp review F1): server-stamped catalog revision.

The client cannot order workspace-list snapshots by request send time:
GET /api/workspaces reads without WORKSPACE_MUTATION_LOCK (it must not queue
behind the slow create_project lane), and lock-acquisition order among
concurrent mutations is scheduler-determined. So the server stamps every
catalog snapshot with a monotonic revision — bumped on each successful
save_workspaces write — and clients gate list updates on it. These tests pin
the server half of that contract.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import urlparse

from api.routes import _handle_workspace_add, handle_get
from api.workspace import catalog_revision, save_workspaces


def _handler():
    h = MagicMock()
    h.wfile = MagicMock()
    return h


def _body(handler):
    return json.loads(b"".join(c.args[0] for c in handler.wfile.write.call_args_list))


def test_save_workspaces_bumps_catalog_revision():
    before = catalog_revision()
    save_workspaces([{"path": str(Path.home()), "name": "Home"}])
    assert catalog_revision() > before


def test_failed_save_does_not_bump_revision():
    # A rejected profile name must raise before any write and leave the
    # revision untouched (the bump follows the atomic replace, not the call).
    before = catalog_revision()
    try:
        save_workspaces([], profile="../escape")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid profile must raise ValueError")
    assert catalog_revision() == before


def test_get_workspaces_payload_carries_rev():
    save_workspaces([{"path": str(Path.home()), "name": "Home"}])
    rev_before = catalog_revision()
    handler = _handler()
    assert handle_get(handler, urlparse("/api/workspaces")) is not False
    payload = _body(handler)
    assert isinstance(payload.get("rev"), int)
    # The seqlock read never reports a rev older than the snapshot's start.
    assert payload["rev"] >= rev_before


def test_add_response_carries_rev_of_its_own_commit(tmp_path):
    before = catalog_revision()
    handler = _handler()
    _handle_workspace_add(handler, {"path": str(tmp_path / "space"), "create": True})
    payload = _body(handler)
    assert payload.get("ok") is True
    assert payload["rev"] == catalog_revision()
    assert payload["rev"] > before


def test_add_response_carries_profile(tmp_path):
    """r15 P2: mutation payloads echo the profile they served, like the GET.

    The rev counter is process-global, so after a profile switch a stale
    old-profile mutation response is rev-acceptable; the profile echo is
    what lets the client drop it.
    """
    from api.profiles import get_active_profile_name
    handler = _handler()
    _handle_workspace_add(handler, {"path": str(tmp_path / "space"), "create": True})
    payload = _body(handler)
    assert payload.get("ok") is True
    assert payload.get("profile") == get_active_profile_name()


def test_get_payload_carries_boot_token_and_profile():
    # omp r14 P1-1 / P2-3: the GET carries a per-process boot token (a
    # restart resets the revision counter; clients reset their gate when
    # the token changes) and echoes the profile it served (the revision is
    # process-global, so a stale old-profile response can otherwise carry an
    # equal-or-higher rev after a profile switch).
    handler = _handler()
    assert handle_get(handler, urlparse("/api/workspaces")) is not False
    payload = _body(handler)
    assert isinstance(payload.get("boot"), str) and payload["boot"]
    assert payload["profile"]  # non-empty profile name
    handler2 = _handler()
    assert handle_get(handler2, urlparse("/api/workspaces")) is not False
    assert _body(handler2)["boot"] == payload["boot"]  # stable within a process


def test_get_giveup_stamps_lower_bound_rev(monkeypatch):
    # omp r14 P2-1: if the revision keeps moving during the read, the give-up
    # path must stamp rev_before (a sound lower bound on the snapshot), not
    # the latest rev — stamping the latest would gate-reject the in-flight
    # commits whose rows the snapshot lacks.
    import api.workspace as ws

    calls = []

    def always_moving():
        val = ws._CATALOG_REV
        calls.append(val)
        ws._CATALOG_REV += 1  # simulate a concurrent commit between the reads
        return val

    monkeypatch.setattr(ws, "catalog_revision", always_moving)
    handler = _handler()
    assert handle_get(handler, urlparse("/api/workspaces")) is not False
    payload = _body(handler)
    # Give-up consumed 3 attempts × 2 reads; the stamped rev must be the
    # LAST rev_before (second-to-last observed), never the final rev_after.
    assert payload["rev"] < calls[-1], (
        f"give-up stamped {payload['rev']}, the freshest rev — overstates "
        f"snapshot freshness (observed {calls})"
    )
