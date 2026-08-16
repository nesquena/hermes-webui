"""Reproduction for: first approval click silently fails in legacy (in-process) mode.

BuggY branch reached by submit_gateway_pending_mirror():
`elif not exact_local_entry` (route_approvals.py). This branch runs when:
  * run_id is empty, AND
  * the incoming approval dict is NOT the same object as a live entry's data
    and its approval_id does not match any live entry's data.approval_id.

In that branch the mirror is created WITHOUT `_GATEWAY_MIRROR_TOKEN`. Later,
_resolve_approval_legacy() must bind the no-run mirror back to the parked
_ApprovalEntry via token match (routes.py `elif not run_id and found_target
and pending:` -> `pending_token == gateway_token`). Without the mirror token
the match fails, resolve_gateway_pending_local() is never called,
live.event.set() is never invoked -> the agent thread stays blocked even
though the endpoint returns ok:true and the UI clears the card (only a ~1.5s
reconcile fallback re-creates the card -> requiring a 2nd click).

The fix stamps `_GATEWAY_MIRROR_TOKEN` from the live head when creating the
no-run mirror, so the FIRST click unblocks the agent.
"""
from __future__ import annotations

import copy
import threading
import uuid
from types import SimpleNamespace


def _live_head_data(approval_id: str = "", run_id: str = ""):
    return {
        "command": "rm -rf /tmp/test",
        "description": "Delete temporary files",
        "pattern_key": "dangerous_command",
        "pattern_keys": ["dangerous_command"],
        "approval_id": approval_id,
        "run_id": run_id,
        "choices": ["once", "session", "always", "deny"],
    }


def test_legacy_no_run_mirror_first_click_unblocks_agent():
    """First 'once' click on a no-run legacy mirror must set the live agent event."""
    from api import routes
    from api import route_approvals as ra

    sid = "sess-first-click-" + uuid.uuid4().hex[:8]

    # Live _ApprovalEntry parked by the in-process legacy agent: NO run_id,
    # NO approval_id yet (UUID is assigned to the mirror). The incoming mirror
    # approval is a SEPARATE dict (copy) so exact_local_entry stays None and we
    # land in the buggy `elif not exact_local_entry` branch.
    live = SimpleNamespace(
        data=_live_head_data(approval_id="", run_id=""),
        event=threading.Event(),
        result=None,
    )
    with ra._lock:
        ra._gateway_queues.pop(sid, None)
        ra._pending.pop(sid, None)
        ra._gateway_queues.setdefault(sid, []).append(live)

    try:
        mirror_approval = copy.deepcopy(live.data)
        mirror_approval["approval_id"] = "appr-" + uuid.uuid4().hex[:8]
        head, _total = ra.submit_gateway_pending_mirror(sid, mirror_approval)

        with ra._lock:
            mirrored = ra._pending[sid][0]
        approval_id = mirrored["approval_id"]
        # Verify we actually reached the no-run mirror branch.
        assert mirrored.get(ra._GATEWAY_MIRROR_FLAG) is True
        assert not str(mirrored.get("run_id") or "").strip()

        # User clicks "Allow once" through the real legacy resolution path.
        ok = routes._resolve_approval_legacy(sid, approval_id, "once")

        # The hard requirement: the parked agent thread must be unblocked.
        assert ok is True
        assert live.event.is_set(), (
            "BUG: first click returned ok but the agent thread was never "
            "unblocked (live entry event not set). Need to build the token "
            "link at mirror creation."
        )
        assert live.result == "once"
    finally:
        with ra._lock:
            ra._gateway_queues.pop(sid, None)
            ra._pending.pop(sid, None)