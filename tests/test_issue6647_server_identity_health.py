"""Static contract tests for #6647 — /health server identity block.

HerMex (iOS companion) validates a server by probing GET /health and reading
`status == "ok"`. To let the app surface the active server at a glance and
support a one-tap server switch, /health must also report the canonical server
URL/name/version the client is actually talking to, plus a lightweight
gateway_running flag — all in the same round-trip the app already performs.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")


def _health_payload_src() -> str:
    """Return the /health payload construction block inside `_handle_health`."""
    health_start = ROUTES_PY.index("def _handle_health")
    payload_start = ROUTES_PY.index("payload = {", health_start)
    payload_end = ROUTES_PY.index('if "oldest_run_age_seconds" in run_check:', payload_start)
    return ROUTES_PY[payload_start:payload_end]


def test_health_exposes_server_identity_block():
    payload = _health_payload_src()
    assert 'payload["server"] = _health_server_block' in payload, (
        "/health must expose a server identity block for companion clients (#6647)"
    )


def test_server_identity_url_derived_from_request():
    payload = _health_payload_src()
    assert "_health_server_url = _request_base_url(handler)" in payload, (
        "server.url must be derived from the request Host header so reverse-proxy "
        "deployments report the URL the client actually used"
    )


def test_server_identity_includes_name_and_version():
    payload = _health_payload_src()
    assert '"url": _health_server_url' in payload
    assert '"name": _health_server_url.split("://", 1)[-1].split("/", 1)[0]' in payload
    assert '"version": _health_server_version or ""' in payload
    assert "from api.updates import WEBUI_VERSION as _health_server_version" in payload


def test_server_identity_includes_gateway_running_flag():
    payload = _health_payload_src()
    assert '_health_server_block["gateway_running"]' in payload, (
        "server block must include a lightweight gateway_running flag"
    )
    assert "get_active_profile_gateway_running_pid() is not None" in payload


def test_health_keeps_existing_contract_fields():
    payload = _health_payload_src()
    assert '"status": "ok" if stream_check.get("status") == "ok" else "degraded"' in payload
    assert '"server_started_at": SERVER_START_TIME' in payload
    assert '"uptime_seconds": round(time.time() - SERVER_START_TIME, 1)' in payload
    assert '"accept_loop": _accept_loop_health(handler)' in payload


def test_server_identity_failure_degrades_gracefully():
    payload = _health_payload_src()
    # The block is wrapped so any failure omits the key rather than breaking
    # the health endpoint (which load balancers and the iOS app rely on).
    assert "payload[\"server\"] = _health_server_block" in payload
    assert "except Exception:" in payload
