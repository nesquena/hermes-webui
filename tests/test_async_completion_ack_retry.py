"""Durable async-delegation completion ACK under ledger errors (real Agent core).

WebUI acknowledges an accepted async-delegation completion through the core's
``complete_event_delivery(evt, claim_id)``. When that raised, WebUI used to fall
back to the core's unconditional ``mark_completion_delivered`` marker, which kept
delivery exactly-once. Current cores no longer ship that marker, so a transient
SQLite lock on the ACK left the row pending with the claim held; the caller then
released it and the already-started delivery was claimed and delivered again.

Each scenario runs in a fresh interpreter against the configured Agent checkout
with an isolated ``HERMES_HOME`` ledger, mirroring the real-core tests in
``test_async_delegation_webui_bridge.py``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


_SCENARIO = r'''
import json
import sqlite3
import sys
import time

from api import process_event_utils as peu
from tools import async_delegation as ad
from tools.process_registry import process_registry

cfg = json.loads(sys.argv[1])

if cfg["marker"] == "absent" and hasattr(ad, "mark_completion_delivered"):
    del ad.mark_completion_delivered  # the current core's surface
elif cfg["marker"] == "legacy" and not hasattr(ad, "mark_completion_delivered"):
    def mark_completion_delivered(delegation_id):
        # The marker exactly as older cores shipped it (unconditional on the claim).
        now = time.time()
        return ad._update_delivery(
            """UPDATE async_delegations SET delivery_state='delivered', delivered_at=?, updated_at=?
               WHERE delegation_id=? AND delivery_state!='delivered'""",
            (now, now, delegation_id),
        )
    ad.mark_completion_delivered = mark_completion_delivered

record = ad.dispatch_async_delegation(
    goal="ack-retry", context=None, toolsets=None, role="leaf", model="m",
    session_key="webui-session-1", parent_session_id="durable-parent",
    runner=lambda: {"status": "completed", "summary": "done"},
)
deadline = time.time() + 10
while ad.active_count() and time.time() < deadline:
    time.sleep(.01)
assert not ad.active_count()
evt = process_registry.completion_queue.get_nowait()
delegation_id = record["delegation_id"]
claim = peu.claim_async_delegation_delivery(evt, "webui")
assert claim is not None and claim.durable, claim

errors = {
    "locked": lambda: sqlite3.OperationalError("database is locked"),
    "malformed": lambda: sqlite3.DatabaseError("database disk image is malformed"),
    "readonly": lambda: sqlite3.OperationalError("attempt to write a readonly database"),
}
real_complete = ad.complete_event_delivery
calls = []

def flaky_complete(event, claim_id):
    calls.append(claim_id)
    if len(calls) <= cfg["failures"]:
        raise errors[cfg["error"]]()
    return real_complete(event, claim_id)

ad.complete_event_delivery = flaky_complete
raised = None
started = time.monotonic()
try:
    peu.complete_async_delegation_delivery(evt, claim)
except Exception as exc:
    raised = [type(exc).__name__, str(exc)]
    # What both WebUI callers do when the ACK raises: release, retry later.
    peu.release_async_delegation_delivery(evt, claim)
elapsed = time.monotonic() - started
ad.complete_event_delivery = real_complete

reclaim = peu.claim_async_delegation_delivery(evt, "webui-retry")
row = ad.get_durable_delegation(delegation_id)
print(json.dumps({
    "raised": raised,
    "ack_calls": len(calls),
    "same_claim": len(set(calls)) == 1 and calls[0] == claim.claim_id,
    "elapsed": elapsed,
    "reclaimed": reclaim is not None,
    "delivery_state": row["delivery_state"],
    "delivery_attempts": row["delivery_attempts"],
}))
'''


def _run(tmp_path: Path, **cfg) -> dict:
    try:
        from tools import async_delegation as ad
    except Exception as exc:
        pytest.skip(f"hermes-agent async delegation module unavailable: {exc}")
    required = (
        "claim_event_delivery",
        "complete_event_delivery",
        "release_event_delivery",
        "get_durable_delegation",
        "dispatch_async_delegation",
    )
    if not all(hasattr(ad, name) for name in required):
        pytest.skip("installed hermes-agent predates durable completion claims")
    if cfg.get("marker") == "legacy" and not (
        hasattr(ad, "mark_completion_delivered") or hasattr(ad, "_update_delivery")
    ):
        pytest.skip("installed hermes-agent cannot host the legacy marker")

    webui_repo = Path(__file__).resolve().parents[1]
    agent_repo = Path(ad.__file__).resolve().parents[1]
    env = dict(os.environ)
    env["HERMES_HOME"] = str(tmp_path)
    env["HERMES_BASE_HOME"] = str(tmp_path)
    env["HERMES_CONFIG_PATH"] = str(tmp_path / "config.yaml")
    env["HERMES_WEBUI_STATE_DIR"] = str(tmp_path)
    env["HERMES_WEBUI_TEST_STATE_DIR"] = str(tmp_path)
    env["HERMES_WEBUI_NO_DOTENV"] = "1"
    env["HERMES_WEBUI_AGENT_DIR"] = str(agent_repo)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(webui_repo), str(agent_repo), env.get("PYTHONPATH", "")) if part
    )
    python_executable = os.environ.get("HERMES_WEBUI_PYTHON") or sys.executable
    proc = subprocess.run(
        [python_executable, "-c", _SCENARIO, json.dumps(cfg)],
        cwd=webui_repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("marker", ["shipped", "absent"])
def test_transient_ack_lock_is_retried_and_delivers_exactly_once(tmp_path, marker):
    """A lock blip on the first ACK must not leave the completion re-claimable."""
    result = _run(tmp_path, marker=marker, error="locked", failures=1)

    assert result["raised"] is None
    assert result["ack_calls"] == 2
    assert result["same_claim"] is True
    assert result["delivery_state"] == "delivered"
    assert result["delivery_attempts"] == 1
    assert result["reclaimed"] is False


def test_persistent_ack_lock_is_bounded_then_reraised(tmp_path):
    """Retries are bounded; a lock that never clears still fails as before."""
    result = _run(tmp_path, marker="absent", error="locked", failures=99)

    assert result["raised"] == ["OperationalError", "database is locked"]
    assert 1 < result["ack_calls"] <= 4
    assert result["same_claim"] is True
    assert result["elapsed"] < 2.0
    assert result["delivery_state"] == "pending"
    assert result["reclaimed"] is True


@pytest.mark.parametrize(
    ("error", "raised"),
    [
        ("malformed", ["DatabaseError", "database disk image is malformed"]),
        ("readonly", ["OperationalError", "attempt to write a readonly database"]),
    ],
)
def test_permanent_ack_error_is_not_retried_and_reraised(tmp_path, error, raised):
    result = _run(tmp_path, marker="absent", error=error, failures=99)

    assert result["raised"] == raised
    assert result["ack_calls"] == 1
    assert result["delivery_state"] == "pending"
    assert result["reclaimed"] is True


@pytest.mark.parametrize(
    ("error", "failures"),
    [("malformed", 99), ("locked", 99), ("locked", 1)],
)
def test_core_with_legacy_marker_still_settles_without_raising(tmp_path, error, failures):
    """Cores that still ship ``mark_completion_delivered`` keep their outcome."""
    result = _run(tmp_path, marker="legacy", error=error, failures=failures)

    assert result["raised"] is None
    assert result["delivery_state"] == "delivered"
    assert result["delivery_attempts"] == 1
    assert result["reclaimed"] is False
