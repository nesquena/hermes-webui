"""Custom endpoint management for the WebUI Providers settings panel.

Ports the core ``hermes_cli.web_server`` custom-endpoint contract
(``/api/providers/custom-endpoints``) into the WebUI so arbitrary
OpenAI-compatible endpoints can be created / edited / deleted / activated
from the browser WITHOUT hand-editing ``config.yaml`` or ``.env``.

Storage format (identical to core v12+):

- ``providers.<endpoint_id>`` blocks in ``config.yaml`` carry
  ``name`` / ``base_url`` / ``model`` / ``models`` / ``discover_models``
  / optional ``context_length``.
- API keys never live in ``config.yaml`` (#69449). They are written to
  ``~/.hermes/.env`` (profile home) under ``HERMES_CUSTOM_<SLUG>_API_KEY``
  and referenced from the entry via ``key_env``, exactly like Hermes core.
- ``model.provider`` / ``model.default`` / ``model.base_url`` mirror the
  activated endpoint (the "Use for new chats" assignment).

The two route prefixes are aliases: the WebUI-native ``/api/custom-endpoints``
and the core contract ``/api/providers/custom-endpoints``.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from api.onboarding import (
    _get_active_hermes_home,
    _get_config_path,
    _load_yaml_config,
    _normalize_base_url,
    _save_yaml_config,
    probe_provider_endpoint,
)


# ── env var naming (core convention, hermes_cli/config.py) ────────────────


def custom_endpoint_key_env(identity: str) -> str:
    """Env var name holding a custom endpoint's API key.

    ``identity`` is whatever names the endpoint on the calling path — the
    settings panel's endpoint id, or ``host:port`` for the CLI setup flow.
    The fixed ``HERMES_CUSTOM_`` prefix keeps the result a valid POSIX name
    even when the slug starts with a digit, which every IP-based local
    endpoint does (``127.0.0.1`` → ``127_0_0_1``).
    """
    slug = re.sub(r"[^A-Z0-9]+", "_", str(identity or "").upper()).strip("_")
    return f"HERMES_CUSTOM_{slug}_API_KEY" if slug else "HERMES_CUSTOM_API_KEY"


# ── helpers (ported 1:1 from hermes_cli/web_server.py) ─────────────────────


def _custom_endpoint_id(raw: str | None, fallback: str = "custom") -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", (raw or "").strip()).strip("-_").lower()
    return slug or fallback


def _models_from_custom_endpoint_entry(entry: dict) -> list[str]:
    models: list[str] = []
    raw_models = entry.get("models")
    if isinstance(raw_models, dict):
        models.extend(str(model).strip() for model in raw_models.keys())
    elif isinstance(raw_models, list):
        models.extend(str(model).strip() for model in raw_models)

    default_model = str(entry.get("model") or entry.get("default_model") or "").strip()
    if default_model:
        models.insert(0, default_model)

    seen: set[str] = set()
    return [model for model in models if model and not (model in seen or seen.add(model))]


def _redact_key(value: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        return ""
    if len(clean) <= 8:
        return "*" * len(clean)
    return f"{clean[:4]}…{clean[-4:]}"


def _api_key_display(entry: dict) -> tuple[bool, str | None]:
    """Return ``(has_api_key, preview)`` for a provider entry.

    Keys live in ``.env`` behind ``key_env``; entries written before the
    env-var migration still carry a plaintext ``api_key``. Checking both
    keeps the panel honest either way.
    """
    plaintext = str(entry.get("api_key") or "").strip()
    if plaintext and not plaintext.startswith("${"):
        return True, _redact_key(plaintext)
    plaintext = str(entry.get("api_key") or "").strip()
    if plaintext:
        return True, f"${{{plaintext[2:-1]}}}"
    key_env = str(entry.get("key_env") or "").strip()
    if key_env:
        return True, f"${{{key_env}}}"
    return False, None


def _custom_endpoint_response(cfg: dict) -> dict:
    model_cfg = cfg.get("model", {}) if isinstance(cfg.get("model"), dict) else {}
    current_provider = str(model_cfg.get("provider", "") or "")
    current_model = str(model_cfg.get("default", model_cfg.get("name", "")) or "")
    current_base_url = str(model_cfg.get("base_url", "") or "")

    endpoints: list[dict[str, Any]] = []
    providers = cfg.get("providers")
    if isinstance(providers, dict):
        for provider_id, raw_entry in providers.items():
            if not isinstance(raw_entry, dict):
                continue
            base_url = (
                str(raw_entry.get("base_url") or raw_entry.get("url") or raw_entry.get("api") or "").strip()
            )
            if not base_url:
                continue
            endpoint_id = str(provider_id)
            models = _models_from_custom_endpoint_entry(raw_entry)
            endpoint_model = str(
                raw_entry.get("model") or raw_entry.get("default_model") or (models[0] if models else "")
            )
            has_api_key, api_key_preview = _api_key_display(raw_entry)
            endpoints.append(
                {
                    "id": endpoint_id,
                    "name": str(raw_entry.get("name") or endpoint_id),
                    "base_url": base_url,
                    "model": endpoint_model,
                    "models": models,
                    "context_length": raw_entry.get("context_length"),
                    "discover_models": bool(raw_entry.get("discover_models", True)),
                    "has_api_key": has_api_key,
                    "api_key_preview": api_key_preview,
                    "is_current": endpoint_id == current_provider,
                    "source": "providers",
                }
            )

    # Legacy direct-config endpoint: model.provider == custom with a base_url
    # but no matching providers.<id> block.
    if (
        current_provider.lower() == "custom"
        and current_base_url
        and not any(e["id"] == "custom" for e in endpoints)
    ):
        has_api_key, api_key_preview = _api_key_display(model_cfg)
        endpoints.insert(
            0,
            {
                "id": "custom",
                "name": "Custom",
                "base_url": current_base_url,
                "model": current_model,
                "models": [current_model] if current_model else [],
                "context_length": model_cfg.get("context_length"),
                "discover_models": True,
                "has_api_key": has_api_key,
                "api_key_preview": api_key_preview,
                "is_current": True,
                "source": "direct-config",
            },
        )

    return {
        "endpoints": endpoints,
        "current": {
            "provider": current_provider,
            "model": current_model,
            "base_url": current_base_url,
        },
    }


def _apply_main_model_assignment(
    model_cfg: Any, provider: str, model: str, base_url: str = "", api_key: str = ""
) -> dict:
    """Apply a main-slot model assignment to a ``model`` config dict.

    Sets ``provider``/``default``, then reconciles ``base_url``: an
    explicitly supplied ``base_url`` is always persisted; otherwise a stale
    ``base_url`` is cleared only when switching to a *different* provider.
    """
    if not isinstance(model_cfg, dict):
        model_cfg = {}
    merged = dict(model_cfg)
    prev_provider = str(merged.get("provider", "") or "")
    merged["provider"] = provider
    merged["default"] = model
    if base_url:
        merged["base_url"] = base_url
    elif prev_provider.strip().lower() != provider.strip().lower():
        merged.pop("base_url", None)
    if api_key:
        merged["api_key"] = api_key
    return merged


def _detach_main_model_from_provider(cfg: dict, provider_key: str) -> None:
    """Drop the main-slot mirror of a provider that no longer exists."""
    model_cfg = cfg.get("model")
    if not isinstance(model_cfg, dict):
        return
    if str(model_cfg.get("provider") or "").strip().lower() != provider_key:
        return
    for field in ("provider", "base_url", "api_key", "key_env"):
        model_cfg.pop(field, None)
    cfg["model"] = model_cfg


def _write_env(updates: dict[str, str | None]) -> None:
    from api.providers import _write_env_file  # thread-safe, comment-preserving

    _write_env_file(_get_active_hermes_home() / ".env", updates)


def _invalidate_caches() -> None:
    """Refresh model/provider lists after a config or env change."""
    try:
        from api.config import invalidate_models_cache
        from api.providers import invalidate_providers_cache

        invalidate_models_cache()
        invalidate_providers_cache()
    except Exception:
        pass


# ── CRUD ────────────────────────────────────────────────────────────────────


def list_custom_endpoints() -> dict:
    """Return configured OpenAI-compatible custom endpoints (GET)."""
    return _custom_endpoint_response(_load_yaml_config(_get_config_path()))


def upsert_custom_endpoint(body: dict | None) -> dict:
    """Create or update a ``providers.<id>`` custom endpoint entry (POST)."""
    payload = body if isinstance(body, dict) else {}
    endpoint_id = _custom_endpoint_id(payload.get("id") or payload.get("name"))
    name = str(payload.get("name") or "").strip()
    base_url = _normalize_base_url(str(payload.get("base_url") or ""))
    model = str(payload.get("model") or "").strip()

    if not name:
        raise ValueError("name required")
    if not base_url:
        raise ValueError("base_url required")
    parsed = urllib.parse.urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("base_url must include scheme and host")
    if not model:
        raise ValueError("model required")

    config_path = _get_config_path()
    cfg = _load_yaml_config(config_path)
    providers = cfg.get("providers")
    if not isinstance(providers, dict):
        providers = {}
    existing = providers.get(endpoint_id)
    if not isinstance(existing, dict):
        existing = {}

    # Merge onto the existing entry rather than replacing it: a providers.<id>
    # block may carry hand-written keys this panel has no field for
    # (``api_mode``, ``extra_headers``, ``request_overrides`` ...).
    entry: dict[str, Any] = dict(existing)
    entry.update(
        {
            "name": name,
            "base_url": base_url,
            "model": model,
            "discover_models": bool(payload.get("discover_models", True)),
        }
    )

    # Same for the model map: merge, so existing models keep their context
    # lengths. ``body.models`` is the catalogue the panel's Test button
    # already discovered.
    existing_models = entry.get("models")
    models_map: dict[str, Any] = dict(existing_models) if isinstance(existing_models, dict) else {}
    candidates = list(payload.get("models") or ()) + [model]
    for candidate in candidates:
        model_id = str(candidate).strip()
        if not model_id:
            continue
        current = models_map.get(model_id)
        models_map[model_id] = dict(current) if isinstance(current, dict) else {}
    entry["models"] = models_map

    context_length = payload.get("context_length")
    if context_length is not None:
        try:
            cl = int(context_length)
        except (TypeError, ValueError):
            cl = 0
        if cl > 0:
            entry["context_length"] = cl
            current_model_entry = models_map.get(model)
            if isinstance(current_model_entry, dict):
                current_model_entry["context_length"] = cl
            else:
                models_map[model] = {"context_length": cl}

    # API keys never belong in config.yaml (#69449). Write to .env and
    # reference it via ``key_env``.
    env_var = custom_endpoint_key_env(endpoint_id)
    submitted_key = str(payload.get("api_key") or "").strip() if payload.get("api_key") is not None else None
    if submitted_key:
        _write_env({env_var: submitted_key})
        entry["key_env"] = env_var
        entry.pop("api_key", None)
    elif submitted_key is not None:
        # Blank field means "clear the key", not "leave it alone".
        _write_env({env_var: None})
        entry.pop("key_env", None)
        entry.pop("api_key", None)
    elif str(entry.get("api_key") or "").strip() and not str(entry.get("api_key") or "").startswith("${"):
        # No new key submitted, but the entry still carries one an earlier
        # release wrote in plaintext — migrate it on the next save.
        _write_env({env_var: str(entry["api_key"]).strip()})
        entry["key_env"] = env_var
        entry.pop("api_key", None)

    providers[endpoint_id] = entry
    cfg["providers"] = providers

    if payload.get("make_default"):
        cfg["model"] = _apply_main_model_assignment(cfg.get("model", {}), endpoint_id, model, base_url)
        if entry.get("key_env") and isinstance(cfg["model"], dict):
            cfg["model"]["key_env"] = entry["key_env"]
            cfg["model"].pop("api_key", None)

    _save_yaml_config(config_path, cfg)
    _invalidate_caches()
    return {"ok": True, "id": endpoint_id}


def activate_custom_endpoint(endpoint_id: str) -> dict:
    """Set a configured custom endpoint as the default model provider (POST)."""
    config_path = _get_config_path()
    cfg = _load_yaml_config(config_path)
    provider_key = _custom_endpoint_id(endpoint_id)
    providers = cfg.get("providers")
    entry = providers.get(provider_key) if isinstance(providers, dict) else None
    if not isinstance(entry, dict):
        raise LookupError("custom endpoint not found")

    models = _models_from_custom_endpoint_entry(entry)
    model = str(entry.get("model") or (models[0] if models else "")).strip()
    base_url = str(entry.get("base_url") or "").strip()
    if not model or not base_url:
        raise ValueError("custom endpoint is incomplete")

    model_cfg = _apply_main_model_assignment(cfg.get("model", {}), provider_key, model, base_url)
    if entry.get("key_env"):
        model_cfg["key_env"] = entry["key_env"]
        model_cfg.pop("api_key", None)
    elif entry.get("api_key"):
        model_cfg["api_key"] = entry["api_key"]
    cfg["model"] = model_cfg
    _save_yaml_config(config_path, cfg)
    _invalidate_caches()
    return {"ok": True, "provider": provider_key, "model": model}


def delete_custom_endpoint(endpoint_id: str) -> dict:
    """Remove a configured custom endpoint from ``providers`` (DELETE)."""
    config_path = _get_config_path()
    cfg = _load_yaml_config(config_path)
    provider_key = _custom_endpoint_id(endpoint_id)
    providers = cfg.get("providers")
    if not isinstance(providers, dict) or provider_key not in providers:
        raise LookupError("custom endpoint not found")
    providers.pop(provider_key, None)
    cfg["providers"] = providers
    _detach_main_model_from_provider(cfg, provider_key)
    _write_env({custom_endpoint_key_env(provider_key): None})
    _save_yaml_config(config_path, cfg)
    _invalidate_caches()
    return {"ok": True}


def validate_custom_endpoint(body: dict | None) -> dict:
    """Probe a custom endpoint by calling its OpenAI-compatible /models URL.

    Response matches the core contract: ``{ok, reachable, message, models}``
    where ``ok=False`` + ``reachable=True`` means the key was rejected and
    ``reachable=False`` means the network probe couldn't run.
    """
    payload = body if isinstance(body, dict) else {}
    base_url = _normalize_base_url(str(payload.get("base_url") or ""))
    api_key = str(payload.get("api_key") or "").strip() or None
    if not base_url:
        return {"ok": False, "reachable": True, "message": "Enter an endpoint URL first.", "models": []}

    result = probe_provider_endpoint("custom", base_url, api_key)
    if result.get("ok"):
        models = [
            str(item.get("id") or "").strip()
            for item in result.get("models", [])
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        ]
        return {"ok": True, "reachable": True, "message": "", "models": models}

    error = str(result.get("error") or "unreachable")
    detail = str(result.get("detail") or result.get("message") or "")
    if error in ("invalid_url", "dns", "connect_refused", "timeout", "unreachable"):
        return {
            "ok": False,
            "reachable": False,
            "message": detail or f"Could not reach {base_url}.",
            "models": [],
        }
    if error == "http_4xx":
        return {"ok": False, "reachable": True, "message": "The endpoint rejected the API key.", "models": []}
    return {
        "ok": False,
        "reachable": True,
        "message": detail or "Endpoint returned an error.",
        "models": [],
    }