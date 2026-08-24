"""Lightweight bounded, no-redirect model endpoint probe shared by WebUI flows.

Configured self-hosted endpoints intentionally may resolve to private or loopback
addresses (Ollama, LM Studio, and vLLM are primary use cases). Consequently this
module makes no DNS-based allow/deny decision before connecting: validation and
use cannot diverge through a DNS check/use gap. Redirects are disabled so a
credential is never forwarded to a second origin.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

MAX_RESPONSE_BYTES = 256 * 1024
DEFAULT_TIMEOUT_SECONDS = 5.0


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


DEFAULT_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler(),
    urllib.request.HTTPHandler(),
    urllib.request.HTTPSHandler(),
    NoRedirectHandler(),
)


def _normalize_base_url(value: str) -> str:
    return str(value or "").strip().rstrip("/")


def _models(payload: Any) -> list[dict[str, str]] | None:
    if isinstance(payload, dict):
        rows = payload.get("data") if isinstance(payload.get("data"), list) else payload.get("models")
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = None
    if not isinstance(rows, list):
        return None
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if isinstance(row, dict):
            model_id = str(row.get("id") or row.get("name") or row.get("model") or "").strip()
            label = str(row.get("name") or row.get("model") or model_id).strip()
        else:
            model_id = str(row or "").strip()
            label = model_id
        if model_id and model_id not in seen:
            seen.add(model_id)
            result.append({"id": model_id, "label": label or model_id})
    return result


def probe_models_endpoint(
    provider: str,
    base_url: str,
    api_key: str | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    opener=None,
) -> dict[str, Any]:
    """Fetch ``<base_url>/models`` once without redirects or unbounded reads."""
    base = _normalize_base_url(base_url)
    if not base:
        return {"ok": False, "error": "invalid_url", "detail": "base_url is required"}
    parsed = urlparse(base)
    if parsed.scheme not in {"http", "https"}:
        return {"ok": False, "error": "invalid_url", "detail": "base_url must start with http:// or https://"}
    if not parsed.hostname:
        return {"ok": False, "error": "invalid_url", "detail": "base_url has no host"}
    if parsed.username or parsed.password:
        return {"ok": False, "error": "invalid_url", "detail": "base_url must not contain embedded credentials"}
    if parsed.query or parsed.fragment:
        return {"ok": False, "error": "invalid_url", "detail": "base_url must not contain a query string or fragment"}

    headers = {
        "Accept": "application/json",
        "User-Agent": "hermes-webui-model-probe",
    }
    key = str(api_key or "").strip()
    if key:
        if str(provider or "").strip().lower() == "anthropic":
            headers["x-api-key"] = key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(f"{base}/models", headers=headers, method="GET")
    active_opener = opener or DEFAULT_OPENER
    try:
        with active_opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            status = int(getattr(response, "status", 200))
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            return {
                "ok": False,
                "error": "unreachable",
                "detail": f"HTTP {exc.code} — endpoint returned a redirect (probe does not follow redirects). Point base_url at the final URL directly.",
                "status": exc.code,
            }
        return {
            "ok": False,
            "error": "http_4xx" if 400 <= exc.code < 500 else "http_5xx",
            "detail": f"HTTP {exc.code}",
            "status": exc.code,
        }
    except urllib.error.URLError as exc:
        reason = exc.reason
        text = str(reason).lower()
        if isinstance(reason, socket.timeout) or "timed out" in text:
            return {"ok": False, "error": "timeout", "detail": f"connection timed out after {timeout:g}s"}
        if isinstance(reason, ConnectionRefusedError) or "refused" in text:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            return {"ok": False, "error": "connect_refused", "detail": f"connection refused at {parsed.hostname}:{port}"}
        reserved_tld = str(parsed.hostname or "").rstrip(".").lower().rsplit(".", 1)[-1]
        if (
            isinstance(reason, socket.gaierror)
            or reserved_tld in {"invalid", "test", "example"}
            or any(
                marker in text
                for marker in ("getaddrinfo", "name or service not known", "name resolution")
            )
        ):
            return {"ok": False, "error": "dns", "detail": f"could not resolve host '{parsed.hostname}'"}
        return {"ok": False, "error": "unreachable", "detail": "connection failed"}
    except (TimeoutError, socket.timeout):
        return {"ok": False, "error": "timeout", "detail": f"connection timed out after {timeout:g}s"}
    except Exception:
        return {"ok": False, "error": "unreachable", "detail": "unexpected probe failure"}

    if status < 200 or status >= 300:
        return {"ok": False, "error": "http_5xx", "detail": f"HTTP {status}", "status": status}
    if len(raw) > MAX_RESPONSE_BYTES:
        return {"ok": False, "error": "parse", "detail": "response exceeded the 256 KB safety limit"}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return {"ok": False, "error": "parse", "detail": "response was not valid JSON"}
    models = _models(payload)
    if models is None:
        return {"ok": False, "error": "parse", "detail": "response did not match an OpenAI-compatible models shape"}
    return {"ok": True, "models": models}
