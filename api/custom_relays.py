"""CRUD for ``custom_providers`` entries via the WebUI.

The Hermes Agent already consumes ``~/.hermes/config.yaml`` ``custom_providers``
end-to-end (see ``api/config.py:1599`` and ``api/config.py:1722``
``resolve_custom_provider_connection``).  This module exposes the missing
``GET / POST / DELETE / PROBE`` endpoints so users can manage OpenAI-compatible
relay endpoints (CLI Proxy API Management Center, LiteLLM, OneAPI, ...) from
Settings → Providers instead of hand-editing yaml.

Storage layout (matches the schema other code already reads):

    # ~/.hermes/config.yaml
    custom_providers:
      - name: "CLI Proxy (local)"
        base_url: "http://127.0.0.1:8317/v1"
        api_key: ${CUSTOM_RELAY_KEY_CLI_PROXY_LOCAL}
        models:
          - "gemini-2.5-pro"

    # ~/.hermes/.env  (mode 0600)
    CUSTOM_RELAY_KEY_CLI_PROXY_LOCAL=sk-xxx
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

from api.config import (
    _custom_provider_slug_from_name,
    _save_yaml_config_file,
    get_config,
    invalidate_models_cache,
    reload_config,
)
from api.onboarding import _normalize_base_url, probe_provider_endpoint
from api.providers import _get_hermes_home, _load_env_file, _write_env_file

logger = logging.getLogger(__name__)

KEY_ENV_PREFIX = "CUSTOM_RELAY_KEY_"
_MAX_NAME_LENGTH = 64
_MAX_BASE_URL_LENGTH = 512
_MAX_MODELS = 200
_MAX_MODEL_ID_LENGTH = 200


def _slug_tail(name: str) -> str:
    """Return the bare slug (without the ``custom:`` prefix)."""
    full = _custom_provider_slug_from_name(name)
    return full.split(":", 1)[1] if full.startswith("custom:") else full


def _key_env_var(name: str) -> str:
    """Derive a deterministic env-var name from a relay display name."""
    tail = _slug_tail(name)
    if not tail:
        return ""
    cleaned = re.sub(r"[^A-Z0-9]+", "_", tail.upper()).strip("_")
    return f"{KEY_ENV_PREFIX}{cleaned}" if cleaned else ""


def _is_custom_relay_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    name = str(entry.get("name") or "").strip()
    if not name:
        return False
    base_url = str(entry.get("base_url") or "").strip()
    return bool(base_url)


def _entry_models(entry: dict) -> list[str]:
    raw = entry.get("models")
    out: list[str] = []
    if isinstance(raw, list):
        for m in raw:
            if isinstance(m, str) and m.strip():
                out.append(m.strip())
    elif isinstance(raw, dict):
        for k in raw.keys():
            if isinstance(k, str) and k.strip():
                out.append(k.strip())
    single = entry.get("model")
    if isinstance(single, str) and single.strip() and single.strip() not in out:
        out.append(single.strip())
    return out


def _entry_has_key(entry: dict) -> tuple[bool, str]:
    """Resolve the entry's api_key/${ENV_VAR}/key_env reference.

    Returns ``(has_key, key_env_name)``.  ``key_env_name`` is the env-var
    actually consulted (or ``""`` when the api_key is a literal).
    """
    import os

    raw = entry.get("api_key")
    key_env = str(entry.get("key_env") or "").strip()
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("${") and text.endswith("}") and len(text) > 3:
            env_var = text[2:-1].strip()
            return bool(os.getenv(env_var, "").strip()) or _env_file_has(env_var), env_var
        if text:
            return True, key_env
    if key_env:
        return bool(os.getenv(key_env, "").strip()) or _env_file_has(key_env), key_env
    return False, key_env


def _env_file_has(var_name: str) -> bool:
    if not var_name:
        return False
    try:
        values = _load_env_file(_get_hermes_home() / ".env")
        return bool(values.get(var_name, "").strip())
    except Exception:
        return False


# ── Public API ──────────────────────────────────────────────────────────────

def list_relays() -> dict[str, Any]:
    """Return every ``custom_providers`` entry as a relay record."""
    cfg = get_config()
    entries = cfg.get("custom_providers", [])
    relays: list[dict[str, Any]] = []
    if isinstance(entries, list):
        for entry in entries:
            if not _is_custom_relay_entry(entry):
                continue
            name = str(entry["name"]).strip()
            base_url = str(entry.get("base_url") or "").strip()
            has_key, key_env = _entry_has_key(entry)
            relays.append({
                "id": _custom_provider_slug_from_name(name),
                "name": name,
                "base_url": base_url,
                "key_env": key_env or _key_env_var(name),
                "has_key": has_key,
                "models": _entry_models(entry),
            })
    return {"ok": True, "relays": relays}


def _validate(name: str, base_url: str, models: list[str]) -> str | None:
    if not name or not name.strip():
        return "name is required"
    if len(name) > _MAX_NAME_LENGTH:
        return f"name exceeds {_MAX_NAME_LENGTH} chars"
    if not _slug_tail(name):
        return "name produces an empty slug; use letters/numbers/dashes"

    if not base_url or not base_url.strip():
        return "base_url is required"
    if len(base_url) > _MAX_BASE_URL_LENGTH:
        return f"base_url exceeds {_MAX_BASE_URL_LENGTH} chars"
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"}:
        return "base_url must start with http:// or https://"
    if not parsed.hostname:
        return "base_url has no host"

    if len(models) > _MAX_MODELS:
        return f"too many models ({len(models)} > {_MAX_MODELS})"
    for m in models:
        if not isinstance(m, str) or not m.strip():
            return "model ids must be non-empty strings"
        if len(m) > _MAX_MODEL_ID_LENGTH:
            return f"model id exceeds {_MAX_MODEL_ID_LENGTH} chars: {m[:32]}…"
    return None


def upsert_relay(
    name: object,
    base_url: object,
    api_key: object,
    models: object,
    original_name: object = None,
) -> dict[str, Any]:
    """Create or update a relay entry.

    Matches an existing entry by ``original_name`` (when renaming) or ``name``.
    Always rewrites the entry in-place; new entries are appended.

    API key handling:
      * ``api_key`` empty/None → leave the existing key untouched (rename / model
        update without re-entering the key).
      * ``api_key`` non-empty   → write to ``~/.hermes/.env`` under the derived
        env-var name; the yaml entry stores ``${ENV_VAR}``.
    """
    name_str = str(name or "").strip()
    base_url_str = _normalize_base_url(str(base_url or ""))
    models_list = list(models) if isinstance(models, list) else []
    models_list = [str(m).strip() for m in models_list if isinstance(m, str) and m.strip()]

    err = _validate(name_str, base_url_str, models_list)
    if err:
        return {"ok": False, "error": err}

    env_var = _key_env_var(name_str)
    if not env_var:
        return {"ok": False, "error": "failed to derive env-var name from relay name"}

    api_key_str: str | None = None
    if isinstance(api_key, str):
        ks = api_key.strip()
        if ks:
            if "\n" in ks or "\r" in ks:
                return {"ok": False, "error": "API key must not contain newline characters"}
            if len(ks) < 8:
                return {"ok": False, "error": "API key appears too short"}
            api_key_str = ks

    home = _get_hermes_home()
    config_path = home / "config.yaml"

    try:
        import yaml as _yaml  # noqa: F401 — surface PyYAML missing as a 500
    except ImportError:
        return {"ok": False, "error": "PyYAML is required to manage custom relays"}

    # Load fresh from disk rather than relying on the in-memory cfg cache, so
    # concurrent CLI edits aren't clobbered.
    if config_path.exists():
        try:
            cfg_data = _yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            return {"ok": False, "error": f"failed to parse config.yaml: {exc}"}
    else:
        cfg_data = {}
    if not isinstance(cfg_data, dict):
        cfg_data = {}

    entries = cfg_data.get("custom_providers")
    if not isinstance(entries, list):
        entries = []
        cfg_data["custom_providers"] = entries

    match_name = str(original_name or name_str).strip()
    match_slug = _slug_tail(match_name)
    target_idx: int | None = None
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            continue
        if _slug_tail(str(e.get("name") or "")) == match_slug:
            target_idx = i
            break

    # Rename collision: trying to rename onto another existing entry.
    new_slug = _slug_tail(name_str)
    if target_idx is None or new_slug != match_slug:
        for i, e in enumerate(entries):
            if i == target_idx or not isinstance(e, dict):
                continue
            if _slug_tail(str(e.get("name") or "")) == new_slug:
                return {"ok": False, "error": f"a relay named '{name_str}' already exists"}

    # Preserve existing api_key reference when not rotating the key.
    preserved_api_key_ref: Any = None
    preserved_key_env: str = ""
    if target_idx is not None:
        old = entries[target_idx]
        preserved_api_key_ref = old.get("api_key")
        preserved_key_env = str(old.get("key_env") or "")

    new_entry: dict[str, Any] = {
        "name": name_str,
        "base_url": base_url_str,
        "api_key": f"${{{env_var}}}",
    }
    if models_list:
        new_entry["models"] = models_list

    # Write the new key (or keep the old reference if no new key supplied).
    env_path = home / ".env"
    env_updates: dict[str, str | None] = {}
    if api_key_str is not None:
        env_updates[env_var] = api_key_str
    else:
        # No new key. If we're renaming (env_var changed), migrate the old
        # value over so the relay keeps working.
        old_env_var = preserved_key_env
        if not old_env_var and isinstance(preserved_api_key_ref, str):
            text = preserved_api_key_ref.strip()
            if text.startswith("${") and text.endswith("}") and len(text) > 3:
                old_env_var = text[2:-1].strip()
        if old_env_var and old_env_var != env_var:
            existing = _load_env_file(env_path).get(old_env_var, "").strip()
            if existing:
                env_updates[env_var] = existing
                env_updates[old_env_var] = None
        # No new key AND no existing env_var to migrate from AND the relay
        # has no key on file yet — that's fine; the user can set it later.

    if env_updates:
        try:
            _write_env_file(env_path, env_updates)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            logger.exception("Failed to write env file for custom relay %s", name_str)
            return {"ok": False, "error": f"failed to save API key: {exc}"}

    if target_idx is not None:
        entries[target_idx] = new_entry
        action = "updated"
    else:
        entries.append(new_entry)
        action = "created"

    try:
        _save_yaml_config_file(config_path, cfg_data)
    except Exception as exc:
        logger.exception("Failed to write config.yaml for custom relay %s", name_str)
        return {"ok": False, "error": f"failed to save config.yaml: {exc}"}

    # Refresh in-memory caches so the next /api/models call sees the new entry.
    try:
        reload_config()
    except Exception:
        logger.debug("reload_config() failed after custom relay upsert", exc_info=True)
    try:
        invalidate_models_cache()
    except Exception:
        logger.debug("invalidate_models_cache() failed after custom relay upsert", exc_info=True)

    return {
        "ok": True,
        "action": action,
        "relay": {
            "id": _custom_provider_slug_from_name(name_str),
            "name": name_str,
            "base_url": base_url_str,
            "key_env": env_var,
            "has_key": api_key_str is not None or _env_file_has(env_var),
            "models": models_list,
        },
    }


def delete_relay(name: object) -> dict[str, Any]:
    """Remove a relay entry from yaml and its key from .env."""
    name_str = str(name or "").strip()
    if not name_str:
        return {"ok": False, "error": "name is required"}

    home = _get_hermes_home()
    config_path = home / "config.yaml"
    if not config_path.exists():
        return {"ok": False, "error": "config.yaml not found"}

    try:
        import yaml as _yaml
    except ImportError:
        return {"ok": False, "error": "PyYAML is required to manage custom relays"}

    try:
        cfg_data = _yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return {"ok": False, "error": f"failed to parse config.yaml: {exc}"}
    if not isinstance(cfg_data, dict):
        cfg_data = {}

    entries = cfg_data.get("custom_providers")
    if not isinstance(entries, list):
        return {"ok": False, "error": f"no relay named '{name_str}'"}

    match_slug = _slug_tail(name_str)
    removed_entry: dict | None = None
    keep: list = []
    for e in entries:
        if isinstance(e, dict) and _slug_tail(str(e.get("name") or "")) == match_slug:
            removed_entry = e
            continue
        keep.append(e)
    if removed_entry is None:
        return {"ok": False, "error": f"no relay named '{name_str}'"}

    cfg_data["custom_providers"] = keep

    try:
        _save_yaml_config_file(config_path, cfg_data)
    except Exception as exc:
        logger.exception("Failed to write config.yaml during relay delete")
        return {"ok": False, "error": f"failed to save config.yaml: {exc}"}

    # Drop the linked env var (best-effort; never block deletion on env failure).
    env_var = _key_env_var(name_str)
    raw_key = removed_entry.get("api_key") if isinstance(removed_entry, dict) else None
    if isinstance(raw_key, str):
        text = raw_key.strip()
        if text.startswith("${") and text.endswith("}") and len(text) > 3:
            env_var = text[2:-1].strip() or env_var
    if not env_var:
        existing_key_env = removed_entry.get("key_env") if isinstance(removed_entry, dict) else None
        if isinstance(existing_key_env, str):
            env_var = existing_key_env.strip()
    if env_var:
        try:
            _write_env_file(home / ".env", {env_var: None})
        except Exception:
            logger.debug("Failed to remove env var %s for deleted relay", env_var, exc_info=True)

    try:
        reload_config()
    except Exception:
        logger.debug("reload_config() failed after custom relay delete", exc_info=True)
    try:
        invalidate_models_cache()
    except Exception:
        logger.debug("invalidate_models_cache() failed after custom relay delete", exc_info=True)

    return {"ok": True, "removed": name_str}


def probe_relay(base_url: object, api_key: object) -> dict[str, Any]:
    """Hit ``{base_url}/models`` with an optional Bearer key and return ids.

    Thin wrapper around ``onboarding.probe_provider_endpoint`` so the WebUI's
    relay editor can validate the user's input before saving.
    """
    base_url_str = _normalize_base_url(str(base_url or ""))
    if not base_url_str:
        return {"ok": False, "error": "invalid_url", "detail": "base_url is required"}

    key_str: str | None = None
    if isinstance(api_key, str) and api_key.strip():
        key_str = api_key.strip()

    result = probe_provider_endpoint("custom", base_url_str, api_key=key_str)
    if not isinstance(result, dict):
        return {"ok": False, "error": "parse", "detail": "probe returned non-dict"}
    if not result.get("ok"):
        return result

    models_raw = result.get("models")
    ids: list[str] = []
    if isinstance(models_raw, list):
        for m in models_raw:
            if isinstance(m, dict):
                mid = m.get("id") or m.get("label")
                if isinstance(mid, str) and mid.strip():
                    ids.append(mid.strip())
            elif isinstance(m, str) and m.strip():
                ids.append(m.strip())
    # Deduplicate while preserving order.
    seen: set[str] = set()
    uniq: list[str] = []
    for mid in ids:
        if mid not in seen:
            seen.add(mid)
            uniq.append(mid)
    return {"ok": True, "models": uniq}
