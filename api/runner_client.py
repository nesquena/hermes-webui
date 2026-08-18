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


def _canonical_runner_profile(profile) -> str:
    """Canonicalize the wire profile identity: root/empty -> 'default'."""
    return str(profile or "").strip() or "default"


def _runner_owner_fence_schema_error(fence) -> str | None:
    """Return an error string when *fence* is not a COMPLETE owner claim.

    #6327: transport (a non-empty dict) is not acceptance — every field the
    receiver needs to compare-and-accept the run under owner authority must
    be present: the exact SID, profile + home generation, the credential-state
    generation, the run claim version, the per-session lease, and the full
    route lane (including the type-checked ``normalized_model`` flag).
    """
    if not isinstance(fence, dict) or not fence:
        return "owner_fence must be a non-empty generation/route claim"
    for field in ("session_id", "profile", "profile_home", "generation", "version", "lease"):
        if not str(fence.get(field) or "").strip():
            return f"owner_fence missing required field '{field}'"
    route = fence.get("route")
    if not isinstance(route, dict):
        return "owner_fence missing required 'route' object"
    for field in ("workspace", "model", "provider"):
        if not str(route.get(field) or "").strip():
            return f"owner_fence.route missing required field '{field}'"
    if not isinstance(route.get("normalized_model"), bool):
        return "owner_fence.route missing required 'normalized_model' (bool)"
    return None


def _runner_fence_accepted(fence, accepted) -> str | None:
    """Return None when *accepted* receiver-binds the run to the exact *fence*.

    #6327 receiver-authoritative compare-and-accept: the echoed owner_fence
    must carry ``accepted: true`` and match EVERY field of the claimed fence —
    the exact SID, profile + home generation, credential-state generation,
    the per-run nonce/version, the full route lane, and the per-session lease
    (lease is REQUIRED and compared unconditionally — an absent echo can
    never be treated as equal).  ``route.normalized_model`` is compared
    EXACTLY as a bool: a missing echo must not equal a claimed ``false``
    value (``bool()`` coercion would treat absent == false).  A reflected
    payload that only matches ``session_id`` + ``generation`` is transport,
    not acceptance: it does not prove the runner compared owner authority
    before creating the run or contacting the provider.
    """
    if not isinstance(accepted, dict):
        return "runner did not echo an owner_fence object"
    if accepted.get("accepted") is not True:
        return "runner did not mark the owner fence accepted:true"
    for field in ("session_id", "profile", "profile_home", "generation", "version", "lease"):
        if str(accepted.get(field) or "") != str(fence.get(field) or ""):
            return f"runner echoed a mismatched owner_fence.{field}"
    claimed_route = fence.get("route")
    accepted_route = accepted.get("route")
    if not isinstance(claimed_route, dict) or not isinstance(accepted_route, dict):
        return "runner did not echo the owner_fence.route lane"
    for field in ("workspace", "model", "provider"):
        if str(accepted_route.get(field) or "") != str(claimed_route.get(field) or ""):
            return f"runner echoed a mismatched owner_fence.route.{field}"
    claimed_nm = claimed_route.get("normalized_model")
    accepted_nm = accepted_route.get("normalized_model")
    if (
        not isinstance(claimed_nm, bool)
        or not isinstance(accepted_nm, bool)
        or accepted_nm is not claimed_nm
    ):
        return "runner echoed a mismatched owner_fence.route.normalized_model"
    return None


def _runner_request_fence_lane_error(request, fence, canonical_profile) -> str | None:
    """Return an error string when the request's top-level lane diverges.

    #6327: the top-level request route is cross-bound to the fence route
    BEFORE the POST — the run must be created for the exact SID/profile/
    workspace/model/provider the fence claims under owner authority, never a
    different lane the fence did not authorize.
    """
    if str(getattr(request, "session_id", "") or "") != str(fence.get("session_id") or ""):
        return "request.session_id diverges from the owner_fence lane"
    if canonical_profile != str(fence.get("profile") or ""):
        return "request.profile diverges from the owner_fence lane"
    route = fence.get("route")
    if not isinstance(route, dict):
        return "owner_fence missing required 'route' object"
    if str(getattr(request, "workspace", "") or "") != str(route.get("workspace") or ""):
        return "request.workspace diverges from the owner_fence lane"
    if str(getattr(request, "model", "") or "") != str(route.get("model") or ""):
        return "request.model diverges from the owner_fence lane"
    if str(getattr(request, "provider", "") or "") != str(route.get("provider") or ""):
        return "request.provider diverges from the owner_fence lane"
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
        # version + per-session lease), and the runner must echo the accepted
        # fence back in its response before the run is treated as started.
        # Every schema/lane/acceptance rejection raises RunnerFenceRefused
        # (retryable, never ambiguous) so the route requeues/reconciles
        # instead of treating the run as started.
        fence = request.owner_fence
        if isinstance(fence, dict):
            # Defensive canonicalization: a root/empty profile serializes as
            # the canonical 'default' wire identity so a valid root session
            # never falls into the generic schema-error path.
            fence = dict(fence)
            if not str(fence.get("profile") or "").strip():
                fence["profile"] = "default"
        schema_error = _runner_owner_fence_schema_error(fence)
        if schema_error is not None:
            raise RunnerFenceRefused(
                f"refusing to start an unowned run: {schema_error}"
            )
        # Cross-bind the top-level request route to the fence lane BEFORE the
        # POST: the run must be created for the exact SID/profile/workspace/
        # model/provider the fence authorizes, never a different lane.
        canonical_profile = _canonical_runner_profile(getattr(request, "profile", None))
        lane_error = _runner_request_fence_lane_error(request, fence, canonical_profile)
        if lane_error is not None:
            raise RunnerFenceRefused(lane_error)
        payload = self._post("/v1/runs", {
            "session_id": request.session_id,
            "message": request.message,
            "attachments": list(request.attachments or []),
            "workspace": request.workspace,
            "profile": canonical_profile,
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
