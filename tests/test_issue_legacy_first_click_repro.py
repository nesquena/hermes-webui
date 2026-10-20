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
    # _ApprovalEntry.__init__ always stamps a request_id via
    # `data.setdefault("request_id", uuid.uuid4().hex)`, and notify_cb is
    # handed `dict(entry.data)` — a copy that carries it. Mirror that
    # contract here so the mirror's identity match (request_id/approval_id)
    # can find this producer; without it, the fail-closed fallback (no
    # inferring ownership from "first unclaimed"/head) would correctly
    # refuse to bind a token, but that never happens for a real entry.
    live.data["request_id"] = "req-" + uuid.uuid4().hex[:8]
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


def test_legacy_non_head_producer_unblocks_only_its_producer():
    """A no-run mirror for a NON-head producer must unblock that producer, not the head."""
    from api import routes
    from api import route_approvals as ra

    sid = "sess-first-click-nonhead-" + uuid.uuid4().hex[:8]

    head = SimpleNamespace(
        data=_live_head_data(approval_id="", run_id=""),
        event=threading.Event(),
        result=None,
    )
    non_head = SimpleNamespace(
        data=_live_head_data(approval_id="", run_id=""),
        event=threading.Event(),
        result=None,
    )
    # _ApprovalEntry.__init__ stamps a request_id; copy that contract so the
    # request_id fallback (#7093) links the mirror to its own producer.
    head.data["request_id"] = "req-head-" + uuid.uuid4().hex[:8]
    non_head.data["request_id"] = "req-nonhead-" + uuid.uuid4().hex[:8]
    assert non_head.data["request_id"] != head.data["request_id"]

    with ra._lock:
        ra._gateway_queues.pop(sid, None)
        ra._pending.pop(sid, None)
        ra._gateway_queues.setdefault(sid, []).extend([head, non_head])

    try:
        # Mirror the NON-head producer (it is not the queue head).
        mirror_approval = copy.deepcopy(non_head.data)
        ra.submit_gateway_pending_mirror(sid, mirror_approval)

        with ra._lock:
            # The head is mirrored too; pick the mirror bound to the NON-head
            # producer by its token.
            non_head_token = str(non_head.data.get("_webui_mirror_token") or "").strip()
            matches = [
                m for m in ra._pending[sid]
                if m.get(ra._GATEWAY_MIRROR_FLAG)
                and not str(m.get("run_id") or "").strip()
                and str(m.get(ra._GATEWAY_MIRROR_TOKEN) or "").strip() == non_head_token
            ]
        assert len(matches) == 1, f"expected exactly one non-head mirror, got {len(matches)}"
        non_head_mirror = matches[0]
        approval_id = non_head_mirror["approval_id"]
        assert str(non_head_mirror.get(ra._GATEWAY_MIRROR_TOKEN) or "").strip(), (
            "no-run mirror must carry a token to be resolvable on the first click"
        )

        ok = routes._resolve_approval_legacy(sid, approval_id, "once")

        assert ok is True
        # The NON-HEAD producer must be the one unblocked.
        assert non_head.event.is_set(), (
            "BUG: click on a non-head mirror did not unblock its own producer. "
            "_resolve_approval_legacy must match the mirror token against every "
            "live producer, not only the queue head."
        )
        assert non_head.result == "once"
        # The HEAD producer must remain untouched.
        assert not head.event.is_set()
        assert head.result is None
    finally:
        with ra._lock:
            ra._gateway_queues.pop(sid, None)
            ra._pending.pop(sid, None)


def test_legacy_stale_request_id_does_not_resolve_unrelated_live_producer():
    """A no-run mirror whose identity matches NO live producer must neither
    resolve nor mask a different, unrelated live producer.

    Regression for two defects on the same boundary:
      * the approval-integrity violation found in review of 818fd2fd — the
        old fallback bound an unmatched mirror to "the first unclaimed token"
        (or the queue head), so approving a stale/foreign command A could
        silently execute an unrelated live command B;
      * the liveness/masking hole the fail-closed fix opened in its place —
        the unmatched tokenless mirror A was still appended and retained,
        suppressing B's token so the real pending approval never surfaced and
        could not be actioned.

    The invariant asserted here is displayed == resolvable == resolved: the
    authoritative live producer is what the UI shows, responding to the stale
    id fails without waking anything, and approving the displayed entry wakes
    exactly its own producer.
    """
    from api import routes
    from api import route_approvals as ra

    sid = "sess-stale-reqid-" + uuid.uuid4().hex[:8]

    producer_b = SimpleNamespace(
        data=_live_head_data(approval_id="", run_id=""),
        event=threading.Event(),
        result=None,
    )
    producer_b.data["command"] = "rm -rf /tmp/b-command"
    producer_b.data["request_id"] = "req-b-" + uuid.uuid4().hex[:8]

    with ra._lock:
        ra._gateway_queues.pop(sid, None)
        ra._pending.pop(sid, None)
        ra._gateway_queues.setdefault(sid, []).append(producer_b)

    try:
        # A stale/foreign approval copy: a distinct request_id that matches
        # no live producer (e.g. a duplicate resubmit after B replaced A, or
        # a cross-session replay).
        stale_approval_a = copy.deepcopy(producer_b.data)
        stale_approval_a["command"] = "rm -rf /tmp/a-command-DIFFERENT"
        stale_approval_a["request_id"] = "req-a-stale-" + uuid.uuid4().hex[:8]
        stale_approval_a["approval_id"] = "appr-a-" + uuid.uuid4().hex[:8]
        stale_approval_id = stale_approval_a["approval_id"]

        head, total = ra.submit_gateway_pending_mirror(sid, stale_approval_a)

        # The stale copy must not be displayed, and above all must not mask B.
        with ra._lock:
            pending_now = list(ra._pending.get(sid) or [])
        assert all(
            m.get("command") != stale_approval_a["command"] for m in pending_now
        ), (
            "BUG: the unmatched stale mirror A was retained; it masks live "
            "producer B, which can then never surface or be actioned."
        )
        assert head is not None, "live producer B must still surface a head"
        assert head["command"] == producer_b.data["command"], (
            "BUG: the displayed head is not the authoritative live producer."
        )
        assert total == 1
        b_approval_id = str(head["approval_id"]).strip()
        assert b_approval_id and b_approval_id != stale_approval_id

        # Responding with A's stale id must fail closed — and must NOT wake B.
        ok_stale = routes._resolve_approval_legacy(sid, stale_approval_id, "once")
        assert ok_stale is False, (
            "a stale approval_id must not report success"
        )
        assert not producer_b.event.is_set(), (
            "BUG: approving unrelated stale command A resolved live "
            "producer B instead."
        )
        assert producer_b.result is None

        # Approving the entry the user was actually shown wakes exactly it.
        ok_b = routes._resolve_approval_legacy(sid, b_approval_id, "once")
        assert ok_b is True
        assert producer_b.event.is_set(), (
            "BUG: approving the displayed head did not unblock its producer."
        )
        assert producer_b.result == "once"
    finally:
        with ra._lock:
            ra._gateway_queues.pop(sid, None)
            ra._pending.pop(sid, None)


def test_legacy_identityless_mirror_with_multiple_producers_does_not_bind_to_head():
    """A tokenless, identity-less no-run mirror submitted while multiple
    producers are parked must neither bind to the queue head nor mask it.

    Regression for the approval-integrity violation found in review of
    818fd2fd (inferring ownership from the queue head let approving an
    unmatched mirror resolve the wrong producer) AND for the masking hole the
    fail-closed fix opened (the unmatched mirror was retained and suppressed
    the authoritative head's token). Asserts the same displayed == resolved
    invariant as the stale-id case above, with two producers parked.
    """
    from api import routes
    from api import route_approvals as ra

    sid = "sess-identityless-multi-" + uuid.uuid4().hex[:8]

    head = SimpleNamespace(
        data=_live_head_data(approval_id="", run_id=""),
        event=threading.Event(),
        result=None,
    )
    non_head = SimpleNamespace(
        data=_live_head_data(approval_id="", run_id=""),
        event=threading.Event(),
        result=None,
    )
    head.data["command"] = "rm -rf /tmp/head-command"
    head.data["request_id"] = "req-head-" + uuid.uuid4().hex[:8]
    non_head.data["command"] = "rm -rf /tmp/nonhead-command"
    non_head.data["request_id"] = "req-nonhead-" + uuid.uuid4().hex[:8]

    with ra._lock:
        ra._gateway_queues.pop(sid, None)
        ra._pending.pop(sid, None)
        ra._gateway_queues.setdefault(sid, []).extend([head, non_head])

    try:
        # A copy carrying no identity at all: no request_id, no approval_id.
        # Must NOT be inferred as belonging to `head`.
        identityless_copy = {
            "command": "rm -rf /tmp/mystery-command",
            "description": "mystery",
            "pattern_key": "dangerous_command",
            "pattern_keys": ["dangerous_command"],
            "choices": ["once", "session", "always", "deny"],
        }

        mirror_head, total = ra.submit_gateway_pending_mirror(sid, identityless_copy)
        identityless_approval_id = str(identityless_copy["approval_id"]).strip()

        # The identity-less copy must not be displayed, and must not suppress
        # the authoritative head's mirror.
        with ra._lock:
            pending_now = list(ra._pending.get(sid) or [])
        assert all(
            m.get("command") != "rm -rf /tmp/mystery-command" for m in pending_now
        ), (
            "BUG: an identity-less mirror was retained while producers were "
            "parked; it masks the authoritative head."
        )
        assert mirror_head is not None
        assert mirror_head["command"] == head.data["command"], (
            "BUG: the displayed head is not the authoritative live producer."
        )
        assert total == 1
        head_approval_id = str(mirror_head["approval_id"]).strip()
        assert head_approval_id and head_approval_id != identityless_approval_id

        # Responding with the identity-less id must wake nobody.
        ok_identityless = routes._resolve_approval_legacy(
            sid, identityless_approval_id, "once"
        )
        assert ok_identityless is False
        assert not head.event.is_set(), (
            "BUG: an identity-less mirror resolved the queue HEAD instead "
            "of failing closed."
        )
        assert not non_head.event.is_set()
        assert head.result is None
        assert non_head.result is None

        # Approving the displayed head resolves exactly that producer and
        # leaves the non-head producer parked.
        ok_head = routes._resolve_approval_legacy(sid, head_approval_id, "once")
        assert ok_head is True
        assert head.event.is_set()
        assert head.result == "once"
        assert not non_head.event.is_set()
        assert non_head.result is None
    finally:
        with ra._lock:
            ra._gateway_queues.pop(sid, None)
            ra._pending.pop(sid, None)