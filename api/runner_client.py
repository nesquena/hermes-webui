"""HTTP client for the default-off WebUI runner-local adapter backend.

This module is intentionally small: it translates RuntimeAdapter dataclasses into
HTTP/JSON calls to a separately supervised runner backend. It does not create or
own agent instances, stream maps, cancellation flags, approval queues, clarify
queues, goal state, or process-local active-run registries in the main WebUI
process.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from typing import Any
from urllib import error, parse, request

_RUNNER_BASE_URL_ENV = "HERMES_WEBUI_RUNNER_BASE_URL"
_RUNNER_TIMEOUT_ENV = "HERMES_WEBUI_RUNNER_TIMEOUT"
_NOT_CONFIGURED = "runner-local chat backend is not configured"


def runner_base_url(environ: dict[str, str] | None = None) -> str | None:
    """Return the explicitly configured runner base URL, if any."""
    source = os.environ if environ is None else environ
    raw = str(source.get(_RUNNER_BASE_URL_ENV, "") or "").strip()
    if not raw:
        return None
    return raw.rstrip("/")


def runner_client_configured(environ: dict[str, str] | None = None) -> bool:
    return runner_base_url(environ) is not None


def _runner_timeout(environ: dict[str, str] | None = None) -> float:
    source = os.environ if environ is None else environ
    raw = str(source.get(_RUNNER_TIMEOUT_ENV, "") or "").strip()
    if not raw:
        return 10.0
    try:
        return max(0.5, float(raw))
    except ValueError:
        return 10.0


def build_runner_client_from_env(environ: dict[str, str] | None = None) -> "HttpRunnerClient":
    """Build the configured runner client or keep runner-local bounded/off."""
    base_url = runner_base_url(environ)
    if not base_url:
        raise NotImplementedError(_NOT_CONFIGURED)
    return HttpRunnerClient(base_url=base_url, timeout=_runner_timeout(environ))


def _to_jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__") and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, dict):
        return dict(value)
    return value


class HttpRunnerClient:
    """RuntimeAdapter-compatible HTTP/JSON client for a runner backend."""

    def __init__(self, *, base_url: str, timeout: float = 10.0):
        self.base_url = str(base_url or "").strip().rstrip("/")
        if not self.base_url:
            raise ValueError("base_url is required")
        self.timeout = timeout

    def _url(self, path: str, query: dict[str, str] | None = None) -> str:
        url = f"{self.base_url}{path}"
        if query:
            encoded = parse.urlencode({k: v for k, v in query.items() if v not in (None, "")})
            if encoded:
                url = f"{url}?{encoded}"
        return url

    def _json_request(self, method: str, path: str, payload: Any | None = None, query: dict[str, str] | None = None) -> dict[str, Any]:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(_to_jsonable(payload)).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(self._url(path, query=query), data=body, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8") or "{}")
            except Exception:
                detail = {}
            safe = detail.get("error") or detail.get("message") or f"runner backend returned HTTP {exc.code}"
            raise RuntimeError(str(safe)) from exc
        except error.URLError as exc:
            raise RuntimeError(f"runner backend unavailable: {exc.reason}") from exc
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("runner backend returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("runner backend returned a non-object JSON payload")
        return parsed

    def start_run(self, request_payload: Any) -> dict[str, Any]:
        return self._json_request("POST", "/v1/runs", request_payload)

    def observe_run(self, run_id: str, *, cursor: str | None = None) -> dict[str, Any]:
        return self._json_request("GET", f"/v1/runs/{parse.quote(str(run_id), safe='')}/events", query={"cursor": cursor or ""})

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._json_request("GET", f"/v1/runs/{parse.quote(str(run_id), safe='')}")

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        return self._json_request("POST", f"/v1/runs/{parse.quote(str(run_id), safe='')}/cancel", {})

    def respond_approval(self, run_id: str, approval_id: str, choice: str) -> dict[str, Any]:
        return self._json_request(
            "POST",
            f"/v1/runs/{parse.quote(str(run_id), safe='')}/approval/{parse.quote(str(approval_id), safe='')}",
            {"choice": choice},
        )

    def respond_clarify(self, run_id: str, clarify_id: str, response_text: str) -> dict[str, Any]:
        return self._json_request(
            "POST",
            f"/v1/runs/{parse.quote(str(run_id), safe='')}/clarify/{parse.quote(str(clarify_id), safe='')}",
            {"response": response_text},
        )

    def queue_message(self, run_id: str, message: str, *, mode: str = "queue") -> dict[str, Any]:
        return self._json_request(
            "POST",
            f"/v1/runs/{parse.quote(str(run_id), safe='')}/queue",
            {"message": message, "mode": mode},
        )

    def update_goal(self, session_id: str, action: str, text: str = "") -> dict[str, Any]:
        return self._json_request(
            "POST",
            f"/v1/sessions/{parse.quote(str(session_id), safe='')}/goal",
            {"action": action, "text": text},
        )

    def latest_run_for_session(self, session_id: str) -> dict[str, Any]:
        return self._json_request("GET", f"/v1/sessions/{parse.quote(str(session_id), safe='')}/latest-run")
