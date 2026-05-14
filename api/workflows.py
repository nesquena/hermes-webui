"""Read-only proxy helpers for Hermes Core workflow APIs.

The workflow source of truth lives in Hermes Core/dashboard.  WebUI only
forwards canonical read-model requests and normalizes unavailable/unsupported
backend failures so the browser never falls through to an HTML shell or treats
an upstream dashboard 401 as a WebUI login expiry.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse

from api.helpers import bad, j

logger = logging.getLogger(__name__)

_WORKFLOW_TIMEOUT_SECONDS = 2.0
_WORKFLOW_PROXY_RE = re.compile(
    r"^/api/workflows(?:$|/inbox(?:$|/[^/]+$)|/[^/]+(?:$|/dag$|/events$|/artifacts$|/nodes/[^/]+$))"
)
_WORKFLOW_POST_RE = re.compile(r"^/api/workflows(?:/inbox(?:$|/[^/]+/(?:shape|promote)$)|/[^/]+/(?:approve|materialize|gates/[^/]+/resolve))$")
_WORKFLOW_PATCH_RE = re.compile(r"^/api/workflows/inbox/[^/]+$")


def is_workflow_proxy_path(path: str) -> bool:
    """Return True for the small canonical workflow read-model route set."""
    value = str(path or "")
    if ".." in value:
        return False
    return bool(_WORKFLOW_PROXY_RE.fullmatch(value))


def handle_workflow_get(handler, parsed) -> bool:
    """Proxy a canonical workflow GET request to Hermes Core/dashboard."""
    if not is_workflow_proxy_path(parsed.path):
        return bad(handler, f"unknown workflow endpoint: GET {parsed.path}", status=404) or True
    return _proxy_workflow_request(handler, parsed, method="GET")


def handle_workflow_post(handler, parsed, body: dict | None = None) -> bool:
    """Proxy canonical workflow mutation requests to Hermes Core/dashboard."""
    value = str(parsed.path or "")
    if ".." in value or not _WORKFLOW_POST_RE.fullmatch(value):
        return bad(handler, f"unknown workflow endpoint: POST {parsed.path}", status=404) or True
    data = json.dumps(body or {}, separators=(",", ":")).encode("utf-8")
    return _proxy_workflow_request(handler, parsed, method="POST", data=data)


def handle_workflow_patch(handler, parsed, body: dict | None = None) -> bool:
    """Proxy canonical workflow patch requests to Hermes Core/dashboard."""
    value = str(parsed.path or "")
    if ".." in value or not _WORKFLOW_PATCH_RE.fullmatch(value):
        return bad(handler, f"unknown workflow endpoint: PATCH {parsed.path}", status=404) or True
    data = json.dumps(body or {}, separators=(",", ":")).encode("utf-8")
    return _proxy_workflow_request(handler, parsed, method="PATCH", data=data)


def _proxy_workflow_request(handler, parsed, *, method: str, data: bytes | None = None) -> bool:
    from api import dashboard_probe

    status = dashboard_probe.get_dashboard_status()
    if not status.get("running") or not status.get("url"):
        return _workflow_unavailable(handler, backend=status, reason="dashboard_unavailable")

    base_url = str(status.get("url") or "").rstrip("/")
    try:
        # Re-validate the dashboard URL before using it as a proxy target. This
        # inherits the loopback-only SSRF guard from dashboard_probe.
        normalized = dashboard_probe.normalize_dashboard_url(base_url)
        if normalized is None:
            raise ValueError("missing dashboard url")
        _host, _port, _scheme, safe_base = normalized
    except ValueError:
        logger.warning("refusing unsafe workflow backend URL", extra={"url": base_url})
        return _workflow_unavailable(handler, backend={"running": False, "error": "invalid dashboard url"}, reason="invalid_dashboard_url")

    upstream_url = f"{safe_base}{parsed.path}"
    if parsed.query:
        upstream_url = f"{upstream_url}?{parsed.query}"

    headers = {"Accept": "application/json", "User-Agent": "hermes-webui-workflow-proxy"}
    token = _dashboard_session_token(safe_base)
    if token:
        headers["X-Hermes-Session-Token"] = token
    request = urllib.request.Request(upstream_url, headers=headers, data=data, method=method)
    try:
        with urllib.request.urlopen(request, timeout=_WORKFLOW_TIMEOUT_SECONDS) as response:
            payload = _read_json_response(response)
            return j(handler, payload, status=getattr(response, "status", 200) or 200) or True
    except urllib.error.HTTPError as exc:
        # Upstream dashboard /api endpoints are session-token gated. Returning
        # 401 from WebUI would incorrectly send the user to WebUI login, so turn
        # upstream auth/capability misses into the stable unsupported state.
        if exc.code in (401, 403):
            return _workflow_unavailable(handler, backend={"running": True, "status": exc.code}, reason="dashboard_auth_failed")
        payload = _read_error_payload(exc)
        return j(handler, payload, status=exc.code) or True
    except Exception as exc:
        logger.debug("workflow proxy request failed", exc_info=True)
        return _workflow_unavailable(handler, backend={"running": False, "error": exc.__class__.__name__}, reason="proxy_request_failed")


def _dashboard_session_token(safe_base: str) -> str | None:
    """Fetch the dashboard page token needed by protected workflow APIs."""
    request = urllib.request.Request(
        f"{safe_base}/workflows",
        headers={"Accept": "text/html", "User-Agent": "hermes-webui-workflow-proxy"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_WORKFLOW_TIMEOUT_SECONDS) as response:
            html = response.read().decode("utf-8", "replace")
    except Exception:
        logger.debug("workflow proxy could not fetch dashboard session token", exc_info=True)
        return None
    match = re.search(r"__HERMES_SESSION_TOKEN__\s*=\s*[\"']([^\"']+)[\"']", html)
    if not match:
        return None
    return match.group(1)


def _read_json_response(response) -> object:
    raw = response.read()
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def _read_error_payload(exc: urllib.error.HTTPError) -> object:
    try:
        raw = exc.read()
        if raw:
            return json.loads(raw.decode("utf-8"))
    except Exception:
        pass
    return {"error": exc.reason or f"upstream workflow API returned HTTP {exc.code}"}


def _workflow_unavailable(handler, *, backend: dict | None = None, reason: str = "dashboard_unavailable"):
    return j(
        handler,
        {
            "error": "Workflow API is not available on this Hermes backend.",
            "capability": "workflows",
            "reason": reason,
            "recovery": "Start or restart Hermes dashboard, then refresh Workflows. If the dashboard is running, verify its workflow API session token is accepted.",
            "backend": backend or {"running": False},
        },
        status=503,
    ) or True
