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
    """Raised when a configured runner endpoint rejects or fails a request.

    ``status`` and ``code`` carry the gateway's own refusal in structured form
    (for example 409 ``approval_not_pending``). Callers branch on these: the
    message quotes the whole response body and is for humans only.
    """

    def __init__(self, message: str, *, status: int | None = None, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = str(code or "").strip()


def _error_code(detail: str) -> str:
    """Extract the gateway's machine-readable refusal code from an error body."""
    try:
        payload = json.loads(detail or "{}")
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or payload.get("code") or "").strip()
        if not code:
            message = str(error.get("message") or "").strip()
            # Some gateways repeat the code as the message; never hand a
            # sentence back as a code.
            code = message if message and message.replace("_", "").isalnum() else ""
        return code
    if isinstance(error, str):
        return error.strip()
    return str(payload.get("code") or "").strip()


def runner_client_configured(environ: dict[str, str] | None = None) -> bool:
    source = os.environ if environ is None else environ
    return bool(str(source.get(_RUNNER_BASE_URL_ENV) or "").strip())


class HttpRunnerClient:
    """Small JSON HTTP client for the external/supervised runner boundary."""

    def __init__(self, *, base_url: str, api_key: str = ""):
        self.base_url = str(base_url or "").strip().rstrip("/")
        if not self.base_url:
            raise ValueError("runner base_url is required")
        # Hardening: the runner endpoint is operator-configured, but reject any
        # non-HTTP(S) scheme so a misconfigured HERMES_WEBUI_RUNNER_BASE_URL
        # (e.g. file:///etc/passwd or ftp://) can never be handed to urlopen.
        _scheme = urllib.parse.urlsplit(self.base_url).scheme.lower()
        if _scheme not in ("http", "https"):
            raise ValueError(
                f"runner base_url must be http(s); got scheme '{_scheme or '(none)'}'"
            )
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

    def get_run(self, run_id: str, *, timeout: float = 60) -> dict[str, Any]:
        return self._get(f"/v1/runs/{urllib.parse.quote(str(run_id), safe='')}", timeout=timeout)

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        return self._post(f"/v1/runs/{urllib.parse.quote(str(run_id), safe='')}/cancel", {})

    def respond_approval(
        self,
        run_id: str,
        approval_id: str,
        choice: str,
        *,
        execution_generation: int | None = None,
    ) -> dict[str, Any]:
        """Answer one pending run approval.

        Gateways that bind a prompt to an execution generation (the canonical
        Runs API contract) accept exactly ``{request_id, execution_generation,
        choice}``; older gateways accept ``{choice, approval_id}``. The caller
        picks the shape from what the gateway itself sent in the approval
        payload, never from a capability guess: a generation is only ever
        forwarded when the gateway published one for this exact prompt.
        """
        payload: dict[str, Any] = {"choice": choice}
        if approval_id and execution_generation is not None:
            payload["request_id"] = approval_id
            payload["execution_generation"] = execution_generation
        else:
            payload["approval_id"] = approval_id
        return self._post(
            f"/v1/runs/{urllib.parse.quote(str(run_id), safe='')}/approval",
            payload,
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

    def _get(self, path: str, *, timeout: float = 60) -> dict[str, Any]:
        req = urllib.request.Request(self.base_url + path, headers=self._headers(), method="GET")
        return self._request_json(req, timeout=timeout)

    def _post(self, path: str, payload: dict[str, Any], *, timeout: float = 60) -> dict[str, Any]:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        return self._request_json(req, timeout=timeout)

    def _opener(self) -> urllib.request.OpenerDirector:
        # Hardening: do NOT follow redirects. A misbehaving/compromised runner
        # returning 3xx Location could otherwise smuggle the Bearer token to
        # another host. Treat any redirect as an error instead.
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        return urllib.request.build_opener(_NoRedirect)

    def _request_json(self, req: urllib.request.Request, *, timeout: float = 60) -> dict[str, Any]:
        try:
            with self._opener().open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(2048).decode("utf-8", errors="replace")
            except Exception:
                detail = ""
            raise RunnerClientError(
                f"Runner returned HTTP {exc.code}: {detail[:500]}",
                status=exc.code,
                code=_error_code(detail),
            ) from exc
        except Exception as exc:
            raise RunnerClientError(f"Runner request failed: {exc}") from exc
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise RunnerClientError("Runner returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RunnerClientError("Runner returned a non-object JSON payload")
        return payload
