"""Regression coverage for #1879 — gateway liveness across PID namespaces.

The gateway's ``get_running_pid()`` uses ``fcntl.flock`` and ``os.kill(pid, 0)``,
both of which require the caller to share a PID namespace with the gateway
process. In multi-container deployments (gateway in one container, WebUI in
another, no ``pid: "service:hermes-agent"`` workaround) those checks always
fail and the dashboard incorrectly reports "Gateway not running".

The fix in ``api/agent_health.py`` adds a freshness fallback: when
``get_running_pid()`` returns ``None`` but ``gateway_state.json`` reports
``gateway_state == "running"`` AND ``updated_at`` is within
``GATEWAY_FRESHNESS_THRESHOLD_S`` (two cron ticks), trust the timestamp as a
cross-container liveness signal.

These tests pin every behavior the fix promises:

  * fresh + running gateway_state, no PID  → alive (cross-container path)
  * stale updated_at + running              → down (no false positives)
  * fresh updated_at + non-running state    → down (crash-without-cleanup case)
  * stale updated_at + stopped state        → unknown (old root gateway was
    intentionally stopped; do not nag profile-gateway users)
  * malformed / missing / naive timestamp   → down (no parser-quirk false alive)
  * future timestamp within threshold       → alive (clock skew tolerance)
  * future timestamp beyond threshold       → down (broken clock rejected)
  * PID-based path still wins when PID exists (no behavior change for
    same-namespace deployments — backward compat with #716 contract)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


class _FakeGatewayStatus:
    def __init__(self, runtime_status, running_pid):
        self._runtime_status = runtime_status
        self._running_pid = running_pid

    def read_runtime_status(self):
        return self._runtime_status

    def get_running_pid(self, cleanup_stale=False):
        assert cleanup_stale is False
        return self._running_pid


def _runtime_status(updated_at: str | None, **overrides):
    payload = {
        "gateway_state": "running",
        "updated_at": updated_at,
        "active_agents": 1,
        "platforms": {"telegram": {"state": "connected"}},
    }
    payload.update(overrides)
    return payload


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# -- Fresh updated_at, no PID -------------------------------------------------


def test_fresh_runtime_status_reports_alive_when_pid_lookup_returns_none(monkeypatch):
    """Container A's WebUI cannot see Container B's PID, but sees the file."""
    from api import agent_health

    fresh_ts = _iso(datetime.now(timezone.utc) - timedelta(seconds=30))

    monkeypatch.setattr(
        agent_health,
        "_gateway_status_module",
        lambda: _FakeGatewayStatus(_runtime_status(fresh_ts), running_pid=None),
    )

    payload = agent_health.build_agent_health_payload()

    assert payload["alive"] is True
    assert payload["details"]["state"] == "alive"
    assert payload["details"]["reason"] == "cross_container_freshness"
    assert payload["details"]["gateway_state"] == "running"
    assert payload["details"]["updated_at"] == fresh_ts


def test_cross_container_alive_path_does_not_leak_raw_process_fields(monkeypatch):
    """Same redaction guarantees as the in-namespace alive path (#716)."""
    from api import agent_health

    fresh_ts = _iso(datetime.now(timezone.utc) - timedelta(seconds=10))
    runtime = _runtime_status(
        fresh_ts,
        pid=7,
        argv=["hermes", "gateway", "--token", "secret-token"],
        command="hermes gateway --token secret-token",
        executable="/opt/hermes/.venv/bin/python",
        env={"OPENAI_API_KEY": "sk-secret"},
    )
    monkeypatch.setattr(
        agent_health,
        "_gateway_status_module",
        lambda: _FakeGatewayStatus(runtime, running_pid=None),
    )

    payload = agent_health.build_agent_health_payload()
    rendered = repr(payload)

    assert payload["alive"] is True
    for forbidden in ("secret-token", "sk-secret", "argv", "command", "executable"):
        assert forbidden not in rendered
    assert "pid" not in payload["details"]


# -- Stale / missing / malformed timestamps -----------------------------------


def test_stale_updated_at_reports_down_even_when_gateway_state_running(monkeypatch):
    """A long-dead gateway with a fossilised state file must surface as down."""
    from api import agent_health

    stale_ts = _iso(datetime.now(timezone.utc) - timedelta(seconds=300))

    monkeypatch.setattr(
        agent_health,
        "_gateway_status_module",
        lambda: _FakeGatewayStatus(_runtime_status(stale_ts), running_pid=None),
    )

    payload = agent_health.build_agent_health_payload()

    assert payload["alive"] is False
    assert payload["details"]["state"] == "down"
    assert payload["details"]["reason"] == "gateway_not_running"


def test_fresh_updated_at_with_non_running_state_reports_down(monkeypatch):
    """Crash-without-cleanup: file is fresh but gateway said it was stopping."""
    from api import agent_health

    fresh_ts = _iso(datetime.now(timezone.utc) - timedelta(seconds=10))
    runtime = _runtime_status(fresh_ts, gateway_state="stopping")

    monkeypatch.setattr(
        agent_health,
        "_gateway_status_module",
        lambda: _FakeGatewayStatus(runtime, running_pid=None),
    )

    payload = agent_health.build_agent_health_payload()

    assert payload["alive"] is False
    assert payload["details"]["state"] == "down"


def test_stale_stopped_runtime_status_reports_unknown_not_down(monkeypatch):
    """#1944: a fossilized clean-stop root state should not trigger the alert.

    Users can run profile-scoped gateways without a root gateway. If an old
    root gateway_state.json says "stopped", treating it as down makes the
    heartbeat banner fire forever even though no root gateway is configured.
    """
    from api import agent_health

    stale_ts = _iso(datetime.now(timezone.utc) - timedelta(days=7))
    runtime = _runtime_status(stale_ts, gateway_state="stopped", active_agents=0)

    monkeypatch.setattr(
        agent_health,
        "_gateway_status_module",
        lambda: _FakeGatewayStatus(runtime, running_pid=None),
    )

    payload = agent_health.build_agent_health_payload()

    assert payload["alive"] is None
    assert payload["details"]["state"] == "unknown"
    assert payload["details"]["reason"] == "gateway_stale_stopped_state"
    assert payload["details"]["gateway_state"] == "stopped"


def test_fresh_stopped_runtime_status_still_reports_down(monkeypatch):
    """A recent stopped state still means the configured gateway is down."""
    from api import agent_health

    fresh_ts = _iso(datetime.now(timezone.utc) - timedelta(seconds=10))
    runtime = _runtime_status(fresh_ts, gateway_state="stopped", active_agents=0)

    monkeypatch.setattr(
        agent_health,
        "_gateway_status_module",
        lambda: _FakeGatewayStatus(runtime, running_pid=None),
    )

    payload = agent_health.build_agent_health_payload()

    assert payload["alive"] is False
    assert payload["details"]["state"] == "down"
    assert payload["details"]["reason"] == "gateway_not_running"


@pytest.mark.parametrize(
    "broken_value",
    [
        None,
        "",
        "not-a-timestamp",
        "2026-13-40T99:99:99",  # parse error
        12345,  # wrong type
        "2026-05-08T12:00:00",  # naive (no tz) — refuse to guess
    ],
)
def test_malformed_or_naive_updated_at_does_not_report_alive(monkeypatch, broken_value):
    """Any non-aware ISO-8601 UTC timestamp is treated as not fresh."""
    from api import agent_health

    monkeypatch.setattr(
        agent_health,
        "_gateway_status_module",
        lambda: _FakeGatewayStatus(_runtime_status(broken_value), running_pid=None),
    )

    payload = agent_health.build_agent_health_payload()

    assert payload["alive"] is False
    assert payload["details"]["state"] == "down"


# -- Clock-skew tolerance -----------------------------------------------------


def test_slightly_future_updated_at_is_accepted_for_clock_skew(monkeypatch):
    """Containers may have small clock drift; <=threshold future is fresh."""
    from api import agent_health

    near_future = _iso(datetime.now(timezone.utc) + timedelta(seconds=15))
    monkeypatch.setattr(
        agent_health,
        "_gateway_status_module",
        lambda: _FakeGatewayStatus(_runtime_status(near_future), running_pid=None),
    )

    payload = agent_health.build_agent_health_payload()

    assert payload["alive"] is True
    assert payload["details"]["reason"] == "cross_container_freshness"


def test_far_future_updated_at_is_rejected(monkeypatch):
    """A timestamp implausibly far in the future signals a broken clock."""
    from api import agent_health

    far_future = _iso(datetime.now(timezone.utc) + timedelta(hours=1))
    monkeypatch.setattr(
        agent_health,
        "_gateway_status_module",
        lambda: _FakeGatewayStatus(_runtime_status(far_future), running_pid=None),
    )

    payload = agent_health.build_agent_health_payload()

    assert payload["alive"] is False


# -- Backward compatibility with #716 PID path --------------------------------


def test_pid_based_alive_path_unchanged_when_namespace_is_shared(monkeypatch):
    """In-namespace deployments must keep the existing #716 contract: when
    ``get_running_pid`` returns a real PID, ``reason`` is NOT set (only the
    cross-container path adds a reason key on success)."""
    from api import agent_health

    runtime = _runtime_status(_iso(datetime.now(timezone.utc)))
    monkeypatch.setattr(
        agent_health,
        "_gateway_status_module",
        lambda: _FakeGatewayStatus(runtime, running_pid=4242),
    )

    payload = agent_health.build_agent_health_payload()

    assert payload["alive"] is True
    assert payload["details"]["state"] == "alive"
    assert "reason" not in payload["details"]


def test_no_runtime_status_still_reports_unknown(monkeypatch):
    """No runtime status + no PID = WebUI-only deployment, still ``unknown``."""
    from api import agent_health

    monkeypatch.setattr(
        agent_health,
        "_gateway_status_module",
        lambda: _FakeGatewayStatus(runtime_status=None, running_pid=None),
    )

    payload = agent_health.build_agent_health_payload()

    assert payload["alive"] is None
    assert payload["details"] == {"state": "unknown", "reason": "gateway_not_configured"}


# -- _runtime_status_is_fresh unit-level coverage -----------------------------


def test_runtime_status_is_fresh_unit_helper():
    """Direct coverage of the boundary helper for future maintainers."""
    from api import agent_health

    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)

    # Boundary: exactly threshold = fresh.
    on_boundary = _iso(now - timedelta(seconds=agent_health.GATEWAY_FRESHNESS_THRESHOLD_S))
    assert agent_health._runtime_status_is_fresh(
        {"gateway_state": "running", "updated_at": on_boundary},
        now=now,
    )

    # Just past threshold = not fresh.
    just_past = _iso(
        now - timedelta(seconds=agent_health.GATEWAY_FRESHNESS_THRESHOLD_S + 0.001)
    )
    assert not agent_health._runtime_status_is_fresh(
        {"gateway_state": "running", "updated_at": just_past},
        now=now,
    )

    # gateway_state must be exactly "running" — anything else is not fresh.
    assert not agent_health._runtime_status_is_fresh(
        {"gateway_state": "RUNNING", "updated_at": _iso(now)},
        now=now,
    )

    # Non-dict input rejected.
    assert not agent_health._runtime_status_is_fresh(None, now=now)
    assert not agent_health._runtime_status_is_fresh("running", now=now)
