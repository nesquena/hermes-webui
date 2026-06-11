"""Hermes gateway connector configuration helpers.

This module intentionally writes to Hermes' native ``config.yaml`` instead of
creating a WebUI-specific connector store.  The manifest below is conservative:
each configurable field maps to an upstream Hermes ``platforms`` entry that was
verified in Hermes Agent gateway configuration/adapters.  Connectors that need
runtime environment variables are shown as read-only until Hermes exposes a
safe WebUI-editable mechanism for them.
"""

from __future__ import annotations

import copy
import os
import re
import time
from typing import Any

from api.config import _get_config_path, reload_config


MASKED_PLACEHOLDER = "••••••"
_SECRET_KEY_RE = re.compile(r"(secret|token|password|api[_-]?key|credential|key)$", re.I)
_CONNECTOR_ID_RE = re.compile(r"^[a-z0-9_:-]{1,64}$")


class ConnectorError(ValueError):
    """User-facing connector API error with an HTTP status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


CONNECTOR_MANIFESTS: dict[str, dict[str, Any]] = {
    "telegram": {
        "id": "telegram",
        "label": "Telegram",
        "kind": "messaging",
        "description": "Telegram bot channel handled by the Hermes gateway.",
        "docs_url": "https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram",
        "configuration_supported": True,
        "toggle_supported": True,
        "test_supported": True,
        "required": ["token"],
        "fields": [
            {
                "name": "token",
                "label": "Bot token",
                "type": "secret",
                "path": ["token"],
                "required": True,
                "env": ["TELEGRAM_BOT_TOKEN"],
            },
            {
                "name": "reply_to_mode",
                "label": "Reply mode",
                "type": "select",
                "path": ["reply_to_mode"],
                "default": "first",
                "options": [
                    {"value": "first", "label": "First matching message"},
                    {"value": "all", "label": "All matching messages"},
                    {"value": "off", "label": "Do not reply automatically"},
                ],
            },
            {
                "name": "require_mention",
                "label": "Require mention in groups",
                "type": "boolean",
                "path": ["extra", "require_mention"],
            },
            {
                "name": "allowed_chats",
                "label": "Allowed chat IDs",
                "type": "list",
                "path": ["extra", "allowed_chats"],
                "placeholder": "One chat id per line",
            },
            {
                "name": "group_allowed_chats",
                "label": "Allowed group chat IDs",
                "type": "list",
                "path": ["extra", "group_allowed_chats"],
                "placeholder": "One group chat id per line",
            },
            {
                "name": "allowed_topics",
                "label": "Allowed topic IDs",
                "type": "list",
                "path": ["extra", "allowed_topics"],
                "placeholder": "One topic id per line",
            },
            {
                "name": "observe_unmentioned_group_messages",
                "label": "Observe unmentioned group messages",
                "type": "boolean",
                "path": ["extra", "observe_unmentioned_group_messages"],
            },
        ],
    },
    "discord": {
        "id": "discord",
        "label": "Discord",
        "kind": "messaging",
        "description": "Discord bot channel handled by the Hermes gateway.",
        "docs_url": "https://hermes-agent.nousresearch.com/docs/user-guide/messaging/discord",
        "configuration_supported": True,
        "toggle_supported": True,
        "test_supported": True,
        "required": ["token"],
        "fields": [
            {
                "name": "token",
                "label": "Bot token",
                "type": "secret",
                "path": ["token"],
                "required": True,
                "env": ["DISCORD_BOT_TOKEN"],
            },
            {
                "name": "reply_to_mode",
                "label": "Reply mode",
                "type": "select",
                "path": ["reply_to_mode"],
                "default": "first",
                "options": [
                    {"value": "first", "label": "First matching message"},
                    {"value": "all", "label": "All matching messages"},
                    {"value": "off", "label": "Do not reply automatically"},
                ],
            },
            {
                "name": "require_mention",
                "label": "Require mention in channels",
                "type": "boolean",
                "path": ["extra", "require_mention"],
            },
            {
                "name": "free_response_channels",
                "label": "Free response channels",
                "type": "list",
                "path": ["extra", "free_response_channels"],
                "placeholder": "One channel id per line",
            },
        ],
    },
    "webhook": {
        "id": "webhook",
        "label": "Webhook",
        "kind": "http",
        "description": "Inbound webhook gateway surface. Route definitions remain in config.yaml.",
        "docs_url": "https://hermes-agent.nousresearch.com/docs/user-guide/messaging/webhooks",
        "configuration_supported": True,
        "toggle_supported": True,
        "test_supported": True,
        "required": ["secret"],
        "fields": [
            {
                "name": "host",
                "label": "Host",
                "type": "text",
                "path": ["extra", "host"],
                "default": "127.0.0.1",
            },
            {
                "name": "port",
                "label": "Port",
                "type": "number",
                "path": ["extra", "port"],
                "default": 8765,
                "min": 1,
                "max": 65535,
            },
            {
                "name": "secret",
                "label": "Default secret",
                "type": "secret",
                "path": ["extra", "secret"],
                "required": True,
            },
        ],
        "notes": [
            "Webhook route definitions are still edited in config.yaml or with Hermes gateway tooling.",
        ],
    },
    "api_server": {
        "id": "api_server",
        "label": "API Server",
        "kind": "http",
        "description": "OpenAI-compatible Hermes API server exposed by the gateway.",
        "docs_url": "https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server",
        "configuration_supported": True,
        "toggle_supported": True,
        "test_supported": True,
        "required": ["key"],
        "fields": [
            {
                "name": "key",
                "label": "API key",
                "type": "secret",
                "path": ["extra", "key"],
                "required": True,
                "env": ["API_SERVER_KEY"],
            },
            {
                "name": "host",
                "label": "Host",
                "type": "text",
                "path": ["extra", "host"],
                "default": "127.0.0.1",
            },
            {
                "name": "port",
                "label": "Port",
                "type": "number",
                "path": ["extra", "port"],
                "default": 8080,
                "min": 1,
                "max": 65535,
            },
            {
                "name": "model_name",
                "label": "Model name",
                "type": "text",
                "path": ["extra", "model_name"],
                "placeholder": "hermes-agent",
            },
            {
                "name": "cors_origins",
                "label": "CORS origins",
                "type": "list",
                "path": ["extra", "cors_origins"],
                "placeholder": "One origin per line",
            },
        ],
    },
    "slack": {
        "id": "slack",
        "label": "Slack",
        "kind": "messaging",
        "description": "Slack is supported by Hermes, but its adapter requires SLACK_BOT_TOKEN and SLACK_APP_TOKEN in the runtime environment.",
        "docs_url": "https://hermes-agent.nousresearch.com/docs/user-guide/messaging/slack",
        "configuration_supported": False,
        "toggle_supported": False,
        "test_supported": False,
        "required_env": ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"],
        "fields": [],
    },
    "email": {
        "id": "email",
        "label": "Email",
        "kind": "messaging",
        "description": "Email is supported by Hermes, but its adapter reads EMAIL_* credentials from the runtime environment.",
        "docs_url": "https://hermes-agent.nousresearch.com/docs/user-guide/messaging/email",
        "configuration_supported": False,
        "toggle_supported": False,
        "test_supported": False,
        "required_env": ["EMAIL_ADDRESS", "EMAIL_PASSWORD", "EMAIL_IMAP_HOST", "EMAIL_SMTP_HOST"],
        "fields": [],
    },
}


def _load_yaml_config_raw() -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - yaml is required by WebUI
        raise ConnectorError("YAML support is not available in this WebUI runtime.", 500) from exc

    path = _get_config_path()
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConnectorError("Hermes config.yaml could not be parsed.", 500) from exc
    return loaded if isinstance(loaded, dict) else {}


def _save_yaml_config_raw(config: dict[str, Any]) -> None:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - yaml is required by WebUI
        raise ConnectorError("YAML support is not available in this WebUI runtime.", 500) from exc

    path = _get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
    reload_config()


def _manifest(connector_id: str) -> dict[str, Any]:
    if not _CONNECTOR_ID_RE.fullmatch(connector_id or ""):
        raise ConnectorError("Invalid connector id.", 400)
    manifest = CONNECTOR_MANIFESTS.get(connector_id)
    if not manifest:
        raise ConnectorError("Connector not found.", 404)
    return manifest


def _platforms(config: dict[str, Any], *, create: bool = False) -> dict[str, Any]:
    platforms = config.get("platforms")
    if not isinstance(platforms, dict):
        if not create:
            return {}
        platforms = {}
        config["platforms"] = platforms
    return platforms


def _platform_config(config: dict[str, Any], connector_id: str, *, create: bool = False) -> dict[str, Any]:
    platforms = _platforms(config, create=create)
    current = platforms.get(connector_id)
    if not isinstance(current, dict):
        if not create:
            return {}
        current = {}
        platforms[connector_id] = current
    return current


def _get_path(data: dict[str, Any], path: list[str]) -> Any:
    current: Any = data
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _set_path(data: dict[str, Any], path: list[str], value: Any) -> None:
    current = data
    for part in path[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[path[-1]] = value


def _delete_path(data: dict[str, Any], path: list[str]) -> None:
    current = data
    for part in path[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            return
        current = child
    current.pop(path[-1], None)


def _is_secret_field(field: dict[str, Any]) -> bool:
    return field.get("type") == "secret" or bool(field.get("secret"))


def _mask_secret(value: Any) -> str:
    if value in (None, ""):
        return ""
    text = str(value)
    suffix = text[-4:] if len(text) >= 4 else ""
    return MASKED_PLACEHOLDER + suffix


def redact_secrets(value: Any) -> Any:
    """Redact secrets recursively for API responses."""
    if isinstance(value, dict):
        out = {}
        for key, child in value.items():
            if _SECRET_KEY_RE.search(str(key)):
                out[key] = _mask_secret(child)
            else:
                out[key] = redact_secrets(child)
        return out
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value


def _field_value_for_response(field: dict[str, Any], platform_cfg: dict[str, Any]) -> Any:
    value = _get_path(platform_cfg, field.get("path") or [])
    if value is None and "default" in field:
        value = field["default"]
    if _is_secret_field(field):
        return _mask_secret(value)
    if field.get("type") == "list" and value is None:
        return []
    return redact_secrets(value)


def _env_available(field: dict[str, Any]) -> bool:
    return any(os.getenv(name, "").strip() for name in field.get("env") or [])


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _required_missing(manifest: dict[str, Any], platform_cfg: dict[str, Any]) -> list[str]:
    missing = []
    required = set(manifest.get("required") or [])
    for field in manifest.get("fields") or []:
        name = str(field.get("name") or "")
        if not name or (name not in required and not field.get("required")):
            continue
        if _has_value(_get_path(platform_cfg, field.get("path") or [])) or _env_available(field):
            continue
        missing.append(name)
    return missing


def _gateway_runtime_status() -> dict[str, Any]:
    try:
        from api.routes import _gateway_status_payload

        status = _gateway_status_payload()
    except Exception:
        return {"available": False}
    health = status.get("health") if isinstance(status.get("health"), dict) else {}
    return {
        "available": True,
        "alive": True if status.get("running") else False if status.get("configured") else None,
        "running": bool(status.get("running")),
        "configured": bool(status.get("configured")),
        "platforms": status.get("platforms") if isinstance(status.get("platforms"), list) else [],
        "session_count": int(status.get("session_count") or 0),
        "state": health.get("gateway_state") or health.get("state"),
        "reason": health.get("reason"),
    }


def _connector_status(manifest: dict[str, Any], platform_cfg: dict[str, Any], runtime: dict[str, Any]) -> str:
    if not manifest.get("configuration_supported"):
        return "unknown"
    enabled = platform_cfg.get("enabled") is True
    missing = _required_missing(manifest, platform_cfg)
    configured = not missing
    if not configured:
        return "not_configured"
    if not enabled:
        return "configured"
    if runtime.get("available") and runtime.get("alive") is False:
        return "error"
    return "enabled"


def _connector_summary(manifest: dict[str, Any], config: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    cid = manifest["id"]
    platform_cfg = _platform_config(config, cid)
    enabled = platform_cfg.get("enabled") is True
    fields = []
    for field in manifest.get("fields") or []:
        safe_field = {k: v for k, v in field.items() if k not in {"path", "env"}}
        safe_field["value"] = _field_value_for_response(field, platform_cfg)
        safe_field["configured"] = _has_value(_get_path(platform_cfg, field.get("path") or [])) or _env_available(field)
        fields.append(safe_field)
    extra = platform_cfg.get("extra") if isinstance(platform_cfg.get("extra"), dict) else {}
    return {
        "id": cid,
        "label": manifest.get("label") or cid,
        "kind": manifest.get("kind") or "connector",
        "description": manifest.get("description") or "",
        "docs_url": manifest.get("docs_url") or "",
        "configuration_supported": bool(manifest.get("configuration_supported")),
        "toggle_supported": bool(manifest.get("toggle_supported")),
        "test_supported": bool(manifest.get("test_supported")),
        "enabled": enabled,
        "status": _connector_status(manifest, platform_cfg, runtime),
        "missing_required": _required_missing(manifest, platform_cfg),
        "fields": fields,
        "notes": list(manifest.get("notes") or []),
        "required_env": list(manifest.get("required_env") or []),
        "route_count": len(extra.get("routes") or []) if isinstance(extra.get("routes"), list) else None,
        "raw": redact_secrets(platform_cfg),
    }


def list_connectors() -> dict[str, Any]:
    config = _load_yaml_config_raw()
    runtime = _gateway_runtime_status()
    connectors = [
        _connector_summary(manifest, config, runtime)
        for manifest in CONNECTOR_MANIFESTS.values()
    ]
    return {
        "connectors": connectors,
        "config_path": str(_get_config_path()),
        "runtime": runtime,
    }


def get_connector(connector_id: str) -> dict[str, Any]:
    manifest = _manifest(connector_id)
    config = _load_yaml_config_raw()
    return {
        "connector": _connector_summary(manifest, config, _gateway_runtime_status()),
        "config_path": str(_get_config_path()),
    }


def _coerce_field_value(field: dict[str, Any], value: Any) -> Any:
    field_type = field.get("type") or "text"
    if field_type == "boolean":
        return value is True or str(value).strip().lower() in {"1", "true", "yes", "on"}
    if field_type == "number":
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ConnectorError(f"{field.get('label') or field.get('name')} must be a number.") from exc
        if "min" in field and number < int(field["min"]):
            raise ConnectorError(f"{field.get('label') or field.get('name')} is below the allowed minimum.")
        if "max" in field and number > int(field["max"]):
            raise ConnectorError(f"{field.get('label') or field.get('name')} is above the allowed maximum.")
        return number
    if field_type == "list":
        if isinstance(value, list):
            items = value
        else:
            text = str(value or "")
            items = re.split(r"[\n,]+", text)
        return [str(item).strip() for item in items if str(item).strip()]
    if field_type == "select":
        text = str(value or "").strip()
        allowed = {str(opt.get("value")) for opt in field.get("options") or [] if isinstance(opt, dict)}
        if text and text not in allowed:
            raise ConnectorError(f"{field.get('label') or field.get('name')} has an unsupported value.")
        return text
    return str(value or "").strip()


def _submitted_fields(body: dict[str, Any]) -> dict[str, Any]:
    fields = body.get("fields")
    if isinstance(fields, dict):
        return fields
    # Accept flat payloads for tests/simple clients while keeping API responses
    # consistent around the explicit fields object.
    return {k: v for k, v in body.items() if k not in {"enabled"}}


def save_connector(connector_id: str, body: dict[str, Any]) -> dict[str, Any]:
    manifest = _manifest(connector_id)
    if not manifest.get("configuration_supported"):
        raise ConnectorError("This connector is visible but must be configured from the Hermes runtime environment.", 400)
    if not isinstance(body, dict):
        raise ConnectorError("JSON body is required.", 400)

    config = _load_yaml_config_raw()
    platform_cfg = copy.deepcopy(_platform_config(config, connector_id, create=True))
    submitted = _submitted_fields(body)
    known_fields = {field["name"]: field for field in manifest.get("fields") or [] if field.get("name")}
    unknown = sorted(set(submitted) - set(known_fields))
    if unknown:
        raise ConnectorError("Unsupported connector field.", 400)

    for name, raw_value in submitted.items():
        field = known_fields[name]
        path = field.get("path") or []
        if not path:
            continue
        if _is_secret_field(field):
            text = str(raw_value or "").strip()
            existing = _get_path(platform_cfg, path)
            if not text or text.startswith(MASKED_PLACEHOLDER):
                if _has_value(existing):
                    continue
                if field.get("required"):
                    continue
                _delete_path(platform_cfg, path)
                continue
        value = _coerce_field_value(field, raw_value)
        if value in ("", [], {}) and not field.get("required"):
            _delete_path(platform_cfg, path)
        else:
            _set_path(platform_cfg, path, value)

    platforms = _platforms(config, create=True)
    platforms[connector_id] = platform_cfg
    _save_yaml_config_raw(config)
    refreshed = _load_yaml_config_raw()
    return {
        "ok": True,
        "connector": _connector_summary(manifest, refreshed, _gateway_runtime_status()),
    }


def toggle_connector(connector_id: str, body: dict[str, Any]) -> dict[str, Any]:
    manifest = _manifest(connector_id)
    if not manifest.get("toggle_supported"):
        raise ConnectorError("This connector cannot be toggled from WebUI.", 400)
    if not isinstance(body, dict) or "enabled" not in body:
        raise ConnectorError("enabled field is required.", 400)
    enabled = body.get("enabled") is True
    config = _load_yaml_config_raw()
    platform_cfg = _platform_config(config, connector_id, create=True)
    missing = _required_missing(manifest, platform_cfg)
    if enabled and missing:
        raise ConnectorError("Required connector fields are missing: " + ", ".join(missing), 400)
    platform_cfg["enabled"] = enabled
    _save_yaml_config_raw(config)
    refreshed = _load_yaml_config_raw()
    return {
        "ok": True,
        "connector": _connector_summary(manifest, refreshed, _gateway_runtime_status()),
    }


def test_connector(connector_id: str) -> dict[str, Any]:
    """Run the strongest safe test available without starting connectors."""
    manifest = _manifest(connector_id)
    if not manifest.get("test_supported"):
        return {
            "ok": False,
            "status": "unsupported",
            "test_level": "none",
            "message": "Hermes does not expose a WebUI-safe connection test for this connector.",
        }
    config = _load_yaml_config_raw()
    platform_cfg = _platform_config(config, connector_id)
    missing = _required_missing(manifest, platform_cfg)
    if missing:
        return {
            "ok": False,
            "status": "not_configured",
            "test_level": "configuration",
            "missing_required": missing,
            "message": "Required connector fields are missing: " + ", ".join(missing),
        }

    config_validation = "manifest"
    try:
        from gateway.config import GatewayConfig

        GatewayConfig.from_dict({"platforms": {connector_id: platform_cfg}})
        config_validation = "hermes_gateway_config"
    except ImportError:
        config_validation = "manifest"
    except Exception as exc:
        raise ConnectorError("Hermes rejected this connector configuration.", 400) from exc

    return {
        "ok": True,
        "status": "configured",
        "test_level": "configuration",
        "validation": config_validation,
        "message": "Connector configuration is valid. Restart or reload the Hermes gateway for runtime connection changes.",
    }
