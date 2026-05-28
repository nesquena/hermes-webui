"""HTTP client boundary for a supervised Hermes WebUI runner backend.

This module intentionally contains no process-local run maps, stream queues,
cancellation registries, approval/clarify queues, or cached agent instances. It
is only a JSON-over-HTTP transport used by ``RunnerRuntimeAdapter`` when an
operator explicitly configures a runner endpoint.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


_RUNNER_BASE_URL_ENV = "HERMES_WEBUI_RUNNER_BASE_URL"
_RUNNER_API_KEY_ENV = "HERMES_WEBUI_RUNNER_API_KEY"


class RunnerClientError(RuntimeError):
    """Raised when a configured runner endpoint rejects or fails a request."""


def runner_client_configured(environ: dict[str, str] | None = None) -> bool:
    source = os.environ if environ is None else environ
    return bool(str(source.get(_RUNNER_BASE_URL_ENV) or "").strip())


class HttpRunnerClient:
    """Small JSON HTTP client for the external/supervised runner boundary."""

    def __init__(self, *, base_url: str, api_key: str = ""):
        self.base_url = str(base_url or "").strip().rstrip("/")
        if not self.base_url:
            raise ValueError("runner base_url is required")
        self.api_key = str(api_key or "").strip()

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "HttpRunnerClient":
        source = os.environ if environ is None else environ
        base_url = str(source.get(_RUNNER_BASE_URL_ENV) or "").strip()
        if not base_url:
            raise NotImplementedError("runner-local chat backend is not configured")
        return cls(base_url=base_url, api_key=str(source.get(_RUNNER_API_KEY_ENV) or ""))

    def start_run(self, request) -> dict[str, Any]:
        return self._post("/v1/runs", {
            "session_id": request.session_id,
            "message": request.message,
            "attachments": list(request.attachments or []),
            "workspace": request.workspace,
            "profile": request.profile,
            "provider": request.provider,
            "model": request.model,
            "toolsets": list(request.toolsets or []),
            "source": request.source,
            "metadata": dict(request.metadata or {}),
        })

    def observe_run(self, run_id: str, *, cursor: str | None = None) -> dict[str, Any]:
        query = ""
        if cursor not in (None, ""):
            query = "?cursor=" + urllib.parse.quote(str(cursor), safe="")
        return self._get(f"/v1/runs/{urllib.parse.quote(str(run_id), safe='')}/events{query}")

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._get(f"/v1/runs/{urllib.parse.quote(str(run_id), safe='')}")

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        return self._post(f"/v1/runs/{urllib.parse.quote(str(run_id), safe='')}/cancel", {})

    def respond_approval(self, run_id: str, approval_id: str, choice: str) -> dict[str, Any]:
        return self._post(
            f"/v1/runs/{urllib.parse.quote(str(run_id), safe='')}/approvals/{urllib.parse.quote(str(approval_id), safe='')}/respond",
            {"choice": choice},
        )

    def respond_clarify(self, run_id: str, clarify_id: str, response: str) -> dict[str, Any]:
        return self._post(
            f"/v1/runs/{urllib.parse.quote(str(run_id), safe='')}/clarifications/{urllib.parse.quote(str(clarify_id), safe='')}/respond",
            {"response": response},
        )

    def queue_message(self, run_id: str, message: str, *, mode: str = "queue") -> dict[str, Any]:
        return self._post(
            f"/v1/runs/{urllib.parse.quote(str(run_id), safe='')}/messages",
            {"message": message, "mode": mode},
        )

    def update_goal(self, session_id: str, action: str, text: str = "") -> dict[str, Any]:
        return self._post(
            f"/v1/sessions/{urllib.parse.quote(str(session_id), safe='')}/goal",
            {"action": action, "text": text},
        )

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Hermes-WebUI-RunnerClient",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _get(self, path: str) -> dict[str, Any]:
        req = urllib.request.Request(self.base_url + path, headers=self._headers(), method="GET")
        return self._request_json(req)

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        return self._request_json(req)

    def _request_json(self, req: urllib.request.Request) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(2048).decode("utf-8", errors="replace")
            except Exception:
                detail = ""
            raise RunnerClientError(f"Runner returned HTTP {exc.code}: {detail[:500]}") from exc
        except Exception as exc:
            raise RunnerClientError(f"Runner request failed: {exc}") from exc
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise RunnerClientError("Runner returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RunnerClientError("Runner returned a non-object JSON payload")
        return payload
