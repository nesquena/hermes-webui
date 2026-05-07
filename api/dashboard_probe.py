"""Safe server-side probe for the official Hermes Agent dashboard.

The official `hermes dashboard` binds to 127.0.0.1:9119 by default and exposes
GET /api/status as a public, read-only identity/status endpoint.  Keep all
probing server-side to avoid browser CORS/mixed-content failures, and only allow
loopback targets so a user-controlled setting cannot become an SSRF primitive.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_DASHBOARD_PORT = 9119
DEFAULT_DASHBOARD_TIMEOUT = 0.5
DEFAULT_DASHBOARD_TARGETS = (("127.0.0.1", DEFAULT_DASHBOARD_PORT), ("localhost", DEFAULT_DASHBOARD_PORT))
_DASHBOARD_ENABLED_VALUES = {"auto", "always", "never"}
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _base_url(host: str, port: int, scheme: str = "http") -> str:
    display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{scheme}://{display_host}:{port}"


def normalize_dashboard_url(raw_url: str | None) -> tuple[str, int, str, str] | None:
    """Return (host, port, scheme, base_url) for a safe loopback dashboard URL.

    Overrides intentionally accept only scheme + loopback host + explicit port.
    Paths, query strings, fragments, and credentials are rejected: the probe
    appends the official `/api/status` fingerprint itself and must not become an
    arbitrary local URL fetcher.
    """
    raw = str(raw_url or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("invalid dashboard URL scheme")
    if parsed.username or parsed.password:
        raise ValueError("invalid dashboard URL credentials")
    host = parsed.hostname or ""
    normalized_host = host.strip().lower()
    if normalized_host not in _LOOPBACK_HOSTS:
        raise ValueError("invalid dashboard URL host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid dashboard URL port") from exc
    if not isinstance(port, int) or not (1 <= port <= 65535):
        raise ValueError("invalid dashboard URL port")
    path = parsed.path or ""
    if path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("invalid dashboard URL path")
    base = _base_url(normalized_host, port, parsed.scheme)
    return normalized_host, port, parsed.scheme, base


def _looks_like_official_dashboard(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    version = payload.get("version")
    if not isinstance(version, str) or not version.strip():
        return False
    # Verified against current Hermes Agent `hermes_cli.web_server.get_status()`:
    # /api/status returns version plus these Hermes-specific fields. Requiring at
    # least one avoids treating any generic {version: ...} local service as the
    # official dashboard.
    return any(key in payload for key in ("release_date", "hermes_home", "config_path", "gateway_running"))


def probe_official_dashboard(
    host: str,
    port: int,
    timeout: float = DEFAULT_DASHBOARD_TIMEOUT,
    scheme: str = "http",
) -> dict:
    """Best-effort check that `hermes dashboard` is running on host:port."""
    try:
        normalized_host = str(host or "").strip().lower()
        if normalized_host not in _LOOPBACK_HOSTS:
            raise ValueError("dashboard probe host must be loopback")
        port = int(port)
        if not (1 <= port <= 65535):
            raise ValueError("dashboard probe port out of range")
        if scheme not in {"http", "https"}:
            raise ValueError("dashboard probe scheme must be http or https")
        base = _base_url(normalized_host, port, scheme)
        request = urllib.request.Request(
            f"{base}/api/status",
            headers={"Accept": "application/json", "User-Agent": "hermes-webui-dashboard-probe"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if getattr(response, "status", None) != 200:
                return {"running": False}
            payload = json.loads(response.read().decode("utf-8"))
        if not _looks_like_official_dashboard(payload):
            return {"running": False}
        result = {"running": True, "host": normalized_host, "port": port, "url": base}
        version = payload.get("version")
        if isinstance(version, str) and version.strip():
            result["version"] = version.strip()
        return result
    except Exception:
        logger.debug("official Hermes dashboard probe failed", exc_info=True)
        return {"running": False}


def _dashboard_config(config_data: dict | None = None) -> dict:
    if config_data is None:
        try:
            from api.config import get_config

            config_data = get_config()
        except Exception:
            config_data = {}
    webui_cfg = config_data.get("webui", {}) if isinstance(config_data, dict) else {}
    dashboard_cfg = webui_cfg.get("dashboard", {}) if isinstance(webui_cfg, dict) else {}
    return dashboard_cfg if isinstance(dashboard_cfg, dict) else {}


def get_dashboard_config(config_data: dict | None = None) -> dict:
    """Return normalized profile config for the Settings → System controls."""
    dashboard_cfg = _dashboard_config(config_data)
    enabled = str(dashboard_cfg.get("enabled", "auto") or "auto").strip().lower()
    if enabled not in _DASHBOARD_ENABLED_VALUES:
        enabled = "auto"
    raw_url = str(dashboard_cfg.get("url") or "").strip()
    if raw_url:
        # Normalize before echoing so the UI never displays unsafe/stale values.
        _host, _port, _scheme, raw_url = normalize_dashboard_url(raw_url)
    return {"enabled": enabled, "url": raw_url}


def save_dashboard_config(payload: dict) -> dict:
    """Persist dashboard link settings under webui.dashboard in config.yaml."""
    enabled = str((payload or {}).get("enabled", "auto") or "auto").strip().lower()
    if enabled not in _DASHBOARD_ENABLED_VALUES:
        raise ValueError("invalid dashboard enabled mode")
    raw_url = str((payload or {}).get("url", "") or "").strip()
    normalized_url = ""
    if raw_url:
        _host, _port, _scheme, normalized_url = normalize_dashboard_url(raw_url)

    from api import config as webui_config

    config_path = webui_config._get_config_path()
    config_data = webui_config._load_yaml_config_file(config_path)
    webui_section = config_data.get("webui")
    if not isinstance(webui_section, dict):
        webui_section = {}
        config_data["webui"] = webui_section
    dashboard_section = webui_section.get("dashboard")
    if not isinstance(dashboard_section, dict):
        dashboard_section = {}
        webui_section["dashboard"] = dashboard_section
    dashboard_section["enabled"] = enabled
    if normalized_url:
        dashboard_section["url"] = normalized_url
    else:
        dashboard_section.pop("url", None)
    webui_config._save_yaml_config_file(config_path, config_data)
    webui_config.reload_config()
    return {"enabled": enabled, "url": normalized_url}


def _webui_bind_host_allows_auto_probe() -> bool:
    raw_host = str(os.environ.get("HERMES_WEBUI_HOST") or "127.0.0.1").strip().lower()
    host = raw_host.replace("[", "").replace("]", "")
    return host in _LOOPBACK_HOSTS


def get_dashboard_status(config_data: dict | None = None) -> dict:
    """Return the safe status payload consumed by GET /api/dashboard/status."""
    dashboard_cfg = _dashboard_config(config_data)
    enabled = str(dashboard_cfg.get("enabled", "auto") or "auto").strip().lower()
    if enabled not in _DASHBOARD_ENABLED_VALUES:
        enabled = "auto"
    if enabled == "never":
        return {"running": False, "enabled": "never"}

    raw_url = dashboard_cfg.get("url") or dashboard_cfg.get("target") or ""
    try:
        override = normalize_dashboard_url(raw_url)
    except ValueError:
        return {"running": False, "enabled": enabled, "error": "invalid dashboard url"}

    targets: list[tuple[str, int, str, str]]
    if override:
        targets = [override]
    else:
        targets = [(host, port, "http", _base_url(host, port)) for host, port in DEFAULT_DASHBOARD_TARGETS]

    if enabled == "always":
        host, port, scheme, base = targets[0]
        return {"running": True, "enabled": enabled, "host": host, "port": port, "url": base}

    if not _webui_bind_host_allows_auto_probe():
        return {"running": False, "enabled": enabled}

    for host, port, scheme, _base in targets:
        result = probe_official_dashboard(host, port, timeout=DEFAULT_DASHBOARD_TIMEOUT, scheme=scheme)
        if result.get("running"):
            result["enabled"] = enabled
            return result
    return {"running": False, "enabled": enabled}
