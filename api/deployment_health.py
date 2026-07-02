"""Deployment health diagnostics endpoint.

GET /api/deployment/health returns a read-only snapshot of server setup
safety, runtime readiness, auth exposure risk, workspace readiness, and
runtime adapter status. No secrets, keys, tokens, passwords, or raw
environment dumps are exposed.
"""
from __future__ import annotations

import ipaddress
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_TAILSCALE_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def _is_tailscale_ip(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr) in _TAILSCALE_NETWORK
    except ValueError:
        return False


def _resolve_os_isolation_status() -> str:
    try:
        from api.routes import _terminal_remote_backend_enabled

        if _terminal_remote_backend_enabled():
            return "isolated"
    except Exception:
        pass
    try:
        from api.config import get_config as _get_config

        terminal_cfg = _get_config().get("terminal", {})
        if isinstance(terminal_cfg, dict) and terminal_cfg.get("backend") in (
            "docker",
            "container",
            "sandbox",
        ):
            return "isolated"
    except Exception:
        pass
    return "not_isolated"


def _resolve_terminal_backend() -> str | None:
    try:
        from api.routes import _terminal_remote_backend_enabled

        if _terminal_remote_backend_enabled():
            return "remote"
    except Exception:
        pass
    try:
        from api.config import get_config as _get_config

        terminal_cfg = _get_config().get("terminal", {})
        if isinstance(terminal_cfg, dict):
            backend = terminal_cfg.get("backend")
            if isinstance(backend, str) and backend.strip():
                return backend.strip().lower()
    except Exception:
        pass
    return "local"


def _providers_configured() -> bool:
    try:
        from api.config import get_config as _get_config

        cfg = _get_config()
        providers = cfg.get("providers", {}) if isinstance(cfg, dict) else {}
        if isinstance(providers, dict) and providers:
            return True
    except Exception:
        pass
    try:
        custom = os.getenv("HERMES_WEBUI_AGENT_RUNS_BASE_URL", "").strip()
        if custom:
            return True
    except Exception:
        pass
    return False


def _resolve_workspace_info() -> dict:
    try:
        from api.config import DEFAULT_WORKSPACE

        ws_path = str(DEFAULT_WORKSPACE) if DEFAULT_WORKSPACE is not None else None
    except Exception:
        ws_path = None

    if ws_path is None:
        return {"path": None, "exists": False, "writable": False}
    try:
        p = Path(ws_path).expanduser()
        exists = p.exists()
        writable = exists and os.access(str(p), os.W_OK)
        return {"path": str(p), "exists": exists, "writable": writable}
    except Exception:
        return {"path": ws_path, "exists": False, "writable": False}


def _resolve_webui_version() -> str | None:
    try:
        from api.updates import WEBUI_VERSION

        if WEBUI_VERSION:
            return str(WEBUI_VERSION)
    except Exception:
        pass
    return None


def handle_deployment_health(handler, parsed):
    from api.helpers import j as json_response
    from api.config import HOST, PORT
    from api.runtime_adapter import runtime_adapter_mode, runtime_adapter_agent_runs_enabled

    try:
        from api.auth import get_password_hash, _is_secure_context

        password_auth_enabled = get_password_hash() is not None
        secure_context = _is_secure_context(handler)
    except Exception:
        password_auth_enabled = False
        secure_context = False

    https = secure_context
    secure_cookie = secure_context

    client_addr = ""
    try:
        client_addr = str(getattr(handler, "client_address", ("",))[0] or "")
    except Exception:
        pass

    cf_headers = {}
    try:
        cf_headers = {
            key: value
            for key, value in handler.headers.items()
            if key.lower().startswith("cf-")
        }
    except Exception:
        pass
    x_forwarded_proto = ""
    try:
        x_forwarded_proto = str(
            handler.headers.get("X-Forwarded-Proto") or ""
        ).strip().lower()
    except Exception:
        pass

    tailscale_likely = bool(
        _is_tailscale_ip(client_addr)
        or (HOST and _is_tailscale_ip(HOST))
    )
    cloudflare_tunnel_likely = bool(
        cf_headers
        or "cf-ray" in (str(k).lower() for k in cf_headers)
        or "cf-connecting-ip" in (str(k).lower() for k in cf_headers)
        or x_forwarded_proto == "https" and "cf-ray" in str(cf_headers).lower()
    )

    public_bind = str(HOST or "").strip() in ("0.0.0.0", "::", "")
    public_bind_warning = public_bind and not password_auth_enabled

    mode = runtime_adapter_mode()
    is_agent_runs = runtime_adapter_agent_runs_enabled()

    agent_runtime_reachable = None
    agent_api_version = None
    if is_agent_runs:
        agent_base_url = os.getenv("HERMES_WEBUI_AGENT_RUNS_BASE_URL", "").strip()
        if agent_base_url:
            import urllib.request
            import urllib.error

            try:
                health_url = agent_base_url.rstrip("/") + "/v1/health"
                req = urllib.request.Request(health_url, method="GET")
                api_key = os.getenv("HERMES_WEBUI_AGENT_RUNS_API_KEY", "").strip()
                if api_key:
                    req.add_header("Authorization", "Bearer " + api_key)
                with urllib.request.urlopen(req, timeout=5) as resp:
                    agent_runtime_reachable = resp.status < 500
                    if agent_runtime_reachable and resp.status < 400:
                        import json

                        try:
                            body = json.loads(resp.read().decode("utf-8", errors="replace"))
                            agent_api_version = str(
                                body.get("version") or body.get("api_version") or ""
                            ) or None
                        except Exception:
                            pass
            except Exception:
                agent_runtime_reachable = False
        else:
            agent_runtime_reachable = False

    warnings: list[str] = []

    if public_bind_warning:
        warnings.append(
            "Server is bound to a public interface without password authentication."
        )

    if (
        not https
        and not tailscale_likely
        and not cloudflare_tunnel_likely
        and not (client_addr and (
            client_addr.startswith("127.") or client_addr == "localhost" or client_addr == "::1"
        ))
    ):
        warnings.append(
            "HTTP is being used for likely non-local access. "
            "Prefer Tailscale private access or HTTPS."
        )

    if mode == "legacy-direct":
        warnings.append(
            "Runtime adapter is legacy-direct; resumable runtime APIs may be limited."
        )

    if is_agent_runs and agent_runtime_reachable is False:
        warnings.append(
            "Hermes Agent runtime API is not reachable at the configured base URL."
        )

    os_isolation = _resolve_os_isolation_status()
    terminal_backend = _resolve_terminal_backend()
    if os_isolation == "not_isolated" and terminal_backend == "local":
        warnings.append("Local terminal backend is not an OS-level sandbox.")

    ws_info = _resolve_workspace_info()
    if ws_info.get("path") is None or not ws_info.get("exists"):
        warnings.append("Workspace path is missing or unavailable.")
    elif not ws_info.get("writable"):
        warnings.append("Workspace path is not writable.")

    top_status = "ok"
    if warnings:
        top_status = "warning"

    payload = {
        "status": top_status,
        "server": {
            "host": str(HOST or ""),
            "port": PORT,
            "password_auth_enabled": password_auth_enabled,
            "https": https,
            "secure_cookie": secure_cookie,
        },
        "network": {
            "tailscale_likely": tailscale_likely,
            "cloudflare_tunnel_likely": cloudflare_tunnel_likely,
            "public_bind_warning": public_bind_warning,
        },
        "runtime": {
            "webui_version": _resolve_webui_version(),
            "runtime_adapter": mode,
            "agent_runtime_reachable": agent_runtime_reachable,
            "agent_api_version": agent_api_version,
        },
        "providers": {
            "configured": _providers_configured(),
            "model_list_reachable": None,
        },
        "workspace": ws_info,
        "security": {
            "os_isolation_status": os_isolation,
            "terminal_backend": terminal_backend,
            "warnings": warnings,
        },
    }

    return json_response(handler, payload)
