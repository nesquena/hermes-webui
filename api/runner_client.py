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

    ``retryable``/``ambiguous`` carry reconciliation semantics for the caller:
    ``ambiguous=True`` means the failure happened after the POST may have
    reached the runner, so the caller must reconcile idempotently instead of
    asserting that no run started.
    """

    retryable = False
    ambiguous = False


class RunnerFenceRefused(RunnerClientError):
    """The runner did not receiver-compare-and-accept the exact owner fence.

    The response could not be bound to the claimed nonce/version and complete
    generation/route claim (``accepted: true`` + SID + profile/home +
    generation + route + lease), so the run must NOT be treated as started.
    Retryable: the caller requeues/reconciles under the current owner.
    """

    retryable = True
    ambiguous = False


def runner_client_configured(environ: dict[str, str] | None = None) -> bool:
    source = os.environ if environ is None else environ
    return bool(str(source.get(_RUNNER_BASE_URL_ENV) or "").strip())


def _runner_owner_fence_schema_error(fence) -> str | None:
    """Return an error string when *fence* is not a COMPLETE owner claim.

    #6327: transport (a non-empty dict) is not acceptance — every field the
    receiver needs to compare-and-accept the run under owner authority must
    be present: the exact SID, profile + home generation, the credential-state
    generation, the full route lane, and the run claim version.
    """
    if not isinstance(fence, dict) or not fence:
        return "owner_fence must be a non-empty generation/route claim"
    for field in ("session_id", "profile", "profile_home", "generation", "version"):
        if not str(fence.get(field) or "").strip():
            return f"owner_fence missing required field '{field}'"
    route = fence.get("route")
    if not isinstance(route, dict):
        return "owner_fence missing required 'route' object"
    for field in ("workspace", "model", "provider"):
        if not str(route.get(field) or "").strip():
            return f"owner_fence.route missing required field '{field}'"
    return None


def _runner_fence_accepted(fence, accepted) -> str | None:
    """Return None when *accepted* receiver-binds the run to the exact *fence*.

    #6327 receiver-authoritative compare-and-accept: the echoed owner_fence
    must carry ``accepted: true`` and match EVERY field of the claimed fence —
    the exact SID, profile + home generation, credential-state generation,
    the per-run nonce/version, the full route lane, and the per-session lease.
    A reflected payload that only matches ``session_id`` + ``generation`` is
    transport, not acceptance: it does not prove the runner compared owner
    authority before creating the run or contacting the provider.
    """
    if not isinstance(accepted, dict):
        return "runner did not echo an owner_fence object"
    if accepted.get("accepted") is not True:
        return "runner did not mark the owner fence accepted:true"
    for field in ("session_id", "profile", "profile_home", "generation", "version"):
        if str(accepted.get(field) or "") != str(fence.get(field) or ""):
            return f"runner echoed a mismatched owner_fence.{field}"
    if "lease" in fence and str(accepted.get("lease") or "") != str(fence.get("lease") or ""):
        return "runner echoed a mismatched owner_fence.lease"
    claimed_route = fence.get("route")
    accepted_route = accepted.get("route")
    if not isinstance(claimed_route, dict) or not isinstance(accepted_route, dict):
        return "runner did not echo the owner_fence.route lane"
    for field in ("workspace", "model", "provider"):
        if str(accepted_route.get(field) or "") != str(claimed_route.get(field) or ""):
            return f"runner echoed a mismatched owner_fence.route.{field}"
    if bool(accepted_route.get("normalized_model")) != bool(claimed_route.get("normalized_model")):
        return "runner echoed a mismatched owner_fence.route.normalized_model"
    return None


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
        # #6327 runner acceptance (fail closed): the owner fence is claimed
        # under the WebUI per-session AGENT lock right before this call; the
        # runner validates/records the generation so an unowned run is never
        # acknowledged.  A non-empty JSON dictionary is TRANSPORT, not
        # acceptance: the fence must carry the complete generation/route
        # schema (SID + profile/home + generation + route lane + claim
        # version), and the runner must echo the accepted fence back in its
        # response before the run is treated as started.
        fence = request.owner_fence
        schema_error = _runner_owner_fence_schema_error(fence)
        if schema_error is not None:
            raise RunnerClientError(
                f"refusing to start an unowned run: {schema_error}"
            )
        payload = self._post("/v1/runs", {
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
            "owner_fence": dict(fence),
        })
        # Receiver compare-and-accept: the run is NOT started unless the
        # runner echoes the COMPLETE claimed fence (accepted:true + SID +
        # profile/home + generation + route lane + per-run claim version +
        # per-session lease) — a reflected payload that only matches
        # session_id + generation is transport, not acceptance.  Every
        # mismatch raises RunnerFenceRefused (retryable, never ambiguous) so
        # the route requeues/reconciles under the current owner instead of
        # treating the run as started.
        accepted = payload.get("owner_fence") if isinstance(payload, dict) else None
        mismatch = _runner_fence_accepted(fence, accepted)
        if mismatch is not None:
            raise RunnerFenceRefused(mismatch)
        return payload

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
            f"/v1/runs/{urllib.parse.quote(str(run_id), safe='')}/approval",
            {"choice": choice, "approval_id": approval_id},
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

    def _opener(self) -> urllib.request.OpenerDirector:
        # Hardening: do NOT follow redirects. A misbehaving/compromised runner
        # returning 3xx Location could otherwise smuggle the Bearer token to
        # another host. Treat any redirect as an error instead.
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        return urllib.request.build_opener(_NoRedirect)

    def _request_json(self, req: urllib.request.Request) -> dict[str, Any]:
        try:
            with self._opener().open(req, timeout=60) as resp:
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
