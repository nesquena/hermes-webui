"""HTTP adapter client for the Hermes Agent /v1/runs runtime contract.

Translates the WebUI ``RuntimeAdapter`` protocol into JSON-over-HTTP calls
against a configured Hermes Agent runtime endpoint.  Designed to be testable
with fake/mocked HTTP so live server integration can be deferred.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from api.runtime_adapter import (
    ControlResult,
    RunStartResult,
    RunStatus,
    RunEventStream,
    StartRunRequest,
    _active_control_result,
)

_AGENT_RUNS_BASE_URL_ENV = "HERMES_WEBUI_AGENT_RUNS_BASE_URL"
_AGENT_RUNS_API_KEY_ENV = "HERMES_WEBUI_AGENT_RUNS_API_KEY"

_SAFE_TIMEOUT_SECONDS = 60

_CLIENT_VERSION = "unknown"


class AgentRunsError(RuntimeError):
    """Structured error from Hermes Agent runtime API."""

    def __init__(self, error: str, message: str, safe_to_retry: bool = True, http_status: int = 0):
        self.error = error
        self.message = message
        self.safe_to_retry = safe_to_retry
        self.http_status = http_status
        super().__init__(message)

    def to_dict(self) -> dict:
        return {
            "error": self.error,
            "message": self.message,
            "safe_to_retry": self.safe_to_retry,
        }


def _redact_header_value(name: str, value: str) -> str:
    low = str(name or "").lower()
    if low in ("authorization", "api-key", "x-api-key"):
        return "[REDACTED]"
    return str(value or "")


def _agent_runs_error_from_urllib(exc: Exception) -> AgentRunsError:
    if isinstance(exc, urllib.error.HTTPError):
        code = int(getattr(exc, "code", 0) or 0)
        if code in (401, 403):
            return AgentRunsError(
                "agent_runtime_auth_error",
                "Hermes Agent runtime API rejected authentication.",
                safe_to_retry=False,
                http_status=code,
            )
        try:
            body = exc.read(2048).decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return AgentRunsError(
            "agent_runtime_bad_response",
            f"Hermes Agent runtime API returned HTTP {code}.",
            safe_to_retry=code >= 500,
            http_status=code,
        )
    if isinstance(exc, TimeoutError):
        return AgentRunsError(
            "agent_runtime_timeout",
            "Hermes Agent runtime API timed out.",
            safe_to_retry=True,
        )
    if isinstance(exc, (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError, OSError)):
        return AgentRunsError(
            "agent_runtime_unreachable",
            "Hermes Agent runtime API is not reachable at configured base URL.",
            safe_to_retry=True,
        )
    raw = str(exc or "").lower()
    if "timeout" in raw or "timed out" in raw:
        return AgentRunsError(
            "agent_runtime_timeout",
            "Hermes Agent runtime API timed out.",
            safe_to_retry=True,
        )
    if "refused" in raw or "unreachable" in raw or "resolve" in raw:
        return AgentRunsError(
            "agent_runtime_unreachable",
            "Hermes Agent runtime API is not reachable at configured base URL.",
            safe_to_retry=True,
        )
    return AgentRunsError(
        "agent_runtime_bad_response",
        "Hermes Agent runtime API returned an invalid response.",
        safe_to_retry=True,
    )


class AgentRunsClient:
    """JSON-over-HTTP client for the Hermes Agent /v1/runs contract."""

    def __init__(self, *, base_url: str, api_key: str = ""):
        self.base_url = str(base_url or "").strip().rstrip("/")
        if not self.base_url:
            raise ValueError("agent-runs base_url is required")
        scheme = urllib.parse.urlsplit(self.base_url).scheme.lower()
        if scheme not in ("http", "https"):
            raise ValueError(f"agent-runs base_url must be http(s); got scheme '{scheme or '(none)'}'")
        self.api_key = str(api_key or "").strip()

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "AgentRunsClient":
        source = os.environ if environ is None else environ
        base_url = str(source.get(_AGENT_RUNS_BASE_URL_ENV) or "").strip()
        if not base_url:
            raise ValueError(
                "HERMES_WEBUI_AGENT_RUNS_BASE_URL is required for agent-runs adapter"
            )
        return cls(
            base_url=base_url,
            api_key=str(source.get(_AGENT_RUNS_API_KEY_ENV) or ""),
        )

    def start_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post("/v1/runs", payload)

    def get_status(self, run_id: str) -> dict[str, Any]:
        return self._get(f"/v1/runs/{urllib.parse.quote(str(run_id), safe='')}")

    def observe_events(
        self,
        run_id: str,
        *,
        after_seq: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        params: list[str] = []
        if after_seq is not None:
            params.append(f"after_seq={int(after_seq)}")
        if limit is not None and limit >= 0:
            params.append(f"limit={int(limit)}")
        qs = ("?" + "&".join(params)) if params else ""
        return self._get(
            f"/v1/runs/{urllib.parse.quote(str(run_id), safe='')}/events{qs}"
        )

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        return self._post(
            f"/v1/runs/{urllib.parse.quote(str(run_id), safe='')}/stop",
            {},
        )

    def resolve_approval(
        self,
        run_id: str,
        approval_id: str,
        choice: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"approval_id": approval_id}
        if choice is not None:
            body["choice"] = choice
        if payload is not None:
            body.update(payload)
        return self._post(
            f"/v1/runs/{urllib.parse.quote(str(run_id), safe='')}/approval",
            body,
        )

    def resolve_clarify(
        self,
        run_id: str,
        clarify_id: str,
        answer: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"clarify_id": clarify_id}
        if answer is not None:
            body["answer"] = answer
        if payload is not None:
            body.update(payload)
        return self._post(
            f"/v1/runs/{urllib.parse.quote(str(run_id), safe='')}/clarify",
            body,
        )

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Hermes-WebUI-AgentRunsClient",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _get(self, path: str) -> dict[str, Any]:
        req = urllib.request.Request(
            self.base_url + path, headers=self._headers(), method="GET"
        )
        return self._request_json(req)

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        return self._request_json(req)

    def _opener(self) -> urllib.request.OpenerDirector:
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        return urllib.request.build_opener(_NoRedirect)

    def _request_json(self, req: urllib.request.Request) -> dict[str, Any]:
        try:
            with self._opener().open(req, timeout=_SAFE_TIMEOUT_SECONDS) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise _agent_runs_error_from_urllib(exc)
        except Exception as exc:
            raise _agent_runs_error_from_urllib(exc)
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError:
            raise AgentRunsError(
                "agent_runtime_bad_response",
                "Hermes Agent runtime API returned an invalid response.",
                safe_to_retry=True,
            )
        if not isinstance(payload, dict):
            raise AgentRunsError(
                "agent_runtime_bad_response",
                "Hermes Agent runtime API returned an invalid response.",
                safe_to_retry=True,
            )
        return payload


class AgentRunsAdapter:
    """Protocol-translator facade over the Hermes Agent /v1/runs contract.

    Uses ``AgentRunsClient`` for HTTP transport. Does not own process-local
    streams, cancellation flags, approval queues, or agent instances.
    """

    def __init__(self, *, base_url: str, api_key: str = ""):
        self._client = AgentRunsClient(base_url=base_url, api_key=api_key)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "AgentRunsAdapter":
        source = os.environ if environ is None else environ
        base_url = str(source.get(_AGENT_RUNS_BASE_URL_ENV) or "").strip()
        if not base_url:
            raise ValueError(
                "HERMES_WEBUI_AGENT_RUNS_BASE_URL is required for agent-runs adapter"
            )
        return cls(
            base_url=base_url,
            api_key=str(source.get(_AGENT_RUNS_API_KEY_ENV) or ""),
        )

    def start_run(self, request: StartRunRequest) -> RunStartResult:
        try:
            payload = {
                "session_id": request.session_id,
                "message": request.message,
                "workspace": request.workspace,
                "profile": request.profile,
                "model": request.model,
                "toolsets": list(request.toolsets or []),
                "metadata": {
                    "client": "webui",
                    "client_version": _CLIENT_VERSION,
                    **(request.metadata or {}),
                },
            }
            resp = self._client.start_run(payload)
        except AgentRunsError as exc:
            return RunStartResult(
                run_id="",
                session_id=request.session_id,
                stream_id="",
                status="error",
                payload=exc.to_dict(),
            )
        run_id = str(resp.get("run_id") or resp.get("stream_id") or "")
        stream_id = str(resp.get("stream_id") or run_id)
        return RunStartResult(
            run_id=run_id,
            session_id=str(resp.get("session_id") or request.session_id),
            stream_id=stream_id,
            status=str(resp.get("status") or "started"),
            started_at=resp.get("started_at"),
            cursor=resp.get("cursor"),
            active_controls=list(resp.get("controls") or resp.get("active_controls") or []),
            payload=resp,
        )

    def observe_run(self, run_id: str, *, cursor: str | None = None) -> RunEventStream:
        after_seq = None
        if cursor not in (None, ""):
            try:
                text = str(cursor)
                if ":" in text:
                    text = text.rsplit(":", 1)[-1]
                after_seq = max(0, int(text))
            except (TypeError, ValueError):
                after_seq = 0
        try:
            resp = self._client.observe_events(run_id, after_seq=after_seq)
        except AgentRunsError:
            return RunEventStream(run_id=run_id, events=[], cursor=cursor, last_event_id=None)
        raw_events = resp.get("events") if isinstance(resp.get("events"), list) else []
        events = [_map_agent_event_to_dict(e) for e in raw_events]
        last_event_id = resp.get("last_event_id") or (
            events[-1].get("event_id") if events else None
        )
        next_cursor = resp.get("cursor")
        if next_cursor is None and events:
            next_cursor = str(events[-1].get("seq") or "")
        return RunEventStream(
            run_id=str(resp.get("run_id") or run_id),
            events=events,
            cursor=str(next_cursor) if next_cursor is not None else cursor,
            last_event_id=last_event_id,
        )

    def get_run(self, run_id: str) -> RunStatus:
        try:
            resp = self._client.get_status(run_id)
        except AgentRunsError:
            return RunStatus(run_id=run_id, status="unknown")
        controls = resp.get("controls") if isinstance(resp.get("controls"), list) else []
        status = str(resp.get("status") or "unknown")
        terminal = status in ("completed", "failed", "cancelled", "expired")
        return RunStatus(
            run_id=str(resp.get("run_id") or run_id),
            session_id=str(resp.get("session_id") or "") or None,
            status=status,
            last_event_id=resp.get("last_event_id"),
            terminal_state=status if terminal else None,
            active_controls=list(controls),
            pending_approval_id=resp.get("pending_approval_id"),
            pending_clarify_id=resp.get("pending_clarify_id"),
        )

    def cancel_run(self, run_id: str) -> ControlResult:
        try:
            resp = self._client.cancel_run(run_id)
        except AgentRunsError as exc:
            return ControlResult(
                False,
                status="error",
                safe_message=exc.message,
                payload=exc.to_dict(),
            )
        if isinstance(resp.get("error"), str) and resp.get("error") == "not_supported":
            return ControlResult(
                False,
                status="not_supported",
                safe_message=str(resp.get("message") or "Cancel is not supported."),
            )
        return _active_control_result(resp)

    def respond_approval(self, run_id: str, approval_id: str, choice: str) -> ControlResult:
        try:
            resp = self._client.resolve_approval(run_id, approval_id, choice)
        except AgentRunsError as exc:
            return ControlResult(
                False,
                status="error",
                safe_message=exc.message,
                payload=exc.to_dict(),
            )
        if isinstance(resp.get("error"), str) and resp.get("error") == "not_supported":
            return ControlResult(
                False,
                status="not_supported",
                safe_message=str(resp.get("message") or "Approval is not supported."),
            )
        return _active_control_result(resp)

    def respond_clarify(self, run_id: str, clarify_id: str, response: str) -> ControlResult:
        try:
            resp = self._client.resolve_clarify(run_id, clarify_id, answer=response)
        except AgentRunsError as exc:
            return ControlResult(
                False,
                status="error",
                safe_message=exc.message,
                payload=exc.to_dict(),
            )
        if isinstance(resp.get("error"), str) and resp.get("error") == "not_supported":
            return ControlResult(
                False,
                status="not_supported",
                safe_message=str(resp.get("message") or "Clarify is not supported."),
            )
        return _active_control_result(resp)

    def queue_message(self, run_id: str, message: str, *, mode: str = "queue") -> ControlResult:
        return ControlResult(
            False,
            status="not_supported",
            safe_message="Queue is not supported by the agent-runs adapter.",
        )

    def update_goal(
        self,
        session_id: str,
        action: str,
        text: str = "",
    ) -> ControlResult:
        return ControlResult(
            False,
            status="not_supported",
            safe_message="Goal is not supported by the agent-runs adapter.",
        )


def _map_agent_event_to_dict(raw: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    payload = raw.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "event_id": str(raw.get("event_id") or ""),
        "seq": int(raw.get("seq", 0)),
        "run_id": str(raw.get("run_id") or ""),
        "session_id": str(raw.get("session_id") or ""),
        "type": str(raw.get("type") or ""),
        "created_at": float(raw.get("created_at", 0.0)),
        "terminal": bool(raw.get("terminal", False)),
        "payload": payload,
    }
