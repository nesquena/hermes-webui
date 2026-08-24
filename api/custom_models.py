"""Profile-scoped custom model/provider management for Settings → Providers.

This module owns the WebUI CRUD contract for Hermes custom endpoints. It supports
both the legacy ``custom_providers`` list and the modern ``providers`` mapping,
keeps credentials in the active profile's ``.env``, and never returns secrets.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from api import config as webui_config
from api.provider_transactions import (
    FileSnapshot,
    restore_file_if_unchanged,
    snapshot_file,
)
from api.providers import _load_env_file

logger = logging.getLogger(__name__)

_API_MODE_ALIASES = {
    "": "chat_completions",
    "openai": "chat_completions",
    "openai_compatible": "chat_completions",
    "openai-chat": "chat_completions",
    "openai_chat": "chat_completions",
    "chat-completions": "chat_completions",
    "responses": "codex_responses",
    "openai_responses": "codex_responses",
    "openai-responses": "codex_responses",
    "anthropic": "anthropic_messages",
    "anthropic-messages": "anthropic_messages",
    "messages": "anthropic_messages",
}
_TEST_API_MODES = frozenset({"chat_completions", "codex_responses", "anthropic_messages"})
_MAX_RESPONSE_BYTES = 512 * 1024
_TEST_TIMEOUT_SECONDS = 30.0
_FALLBACK_PROFILE_LOCK = None


class CustomModelError(RuntimeError):
    """Expected validation/conflict failure with an HTTP status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = int(status)


def _active_home() -> Path:
    from api.profiles import get_active_hermes_home

    return Path(get_active_hermes_home()).expanduser().resolve()


def _assert_profile_home(expected: Path) -> None:
    if _active_home() != expected:
        raise CustomModelError(
            "Active profile changed before this custom model action completed. Reload and try again.",
            409,
        )


def _assert_expected_profile(body: dict[str, Any]) -> None:
    expected = str(body.get("profile") or "").strip()
    if not expected:
        return
    from api.profiles import get_active_profile_name

    actual = str(get_active_profile_name() or "default").strip() or "default"
    if expected != actual:
        raise CustomModelError(
            "Active profile changed before this custom model action completed. Reload and try again.",
            409,
        )


@contextmanager
def _shared_profile_mutation_lock(profile_key):
    """Delegate to the canonical Agent-authoritative Profile transaction."""
    from api.provider_transactions import active_profile_transaction

    expected_home = Path(profile_key).expanduser().resolve()
    stack = ExitStack()
    try:
        locked_home = stack.enter_context(active_profile_transaction(_active_home))
    except RuntimeError as exc:
        status = 503 if "unavailable" in str(exc).lower() else 409
        raise CustomModelError(str(exc), status) from exc
    with stack:
        if locked_home != expected_home:
            raise CustomModelError(
                "Active profile changed before this custom model action completed. Reload and try again.",
                409,
            )
        yield


def _config_revision(path: Path) -> FileSnapshot:
    try:
        return snapshot_file(path)
    except OSError as exc:
        raise CustomModelError(
            "Existing Hermes config could not be inspected.", 409
        ) from exc


def _load_config(home: Path) -> dict[str, Any]:
    """Load raw YAML without env expansion or ambiguous duplicate keys."""
    from api.provider_transactions import load_yaml_mapping_strict

    try:
        return load_yaml_mapping_strict(home / "config.yaml")
    except ValueError as exc:
        raise CustomModelError(
            "Existing Hermes config is invalid or ambiguous; custom model changes were not applied.",
            409,
        ) from exc


def _save_config(
    home: Path,
    config_data: dict[str, Any],
    *,
    expected_revision: FileSnapshot,
) -> None:
    path = home / "config.yaml"
    if _config_revision(path) != expected_revision:
        raise CustomModelError(
            "Hermes config changed while this edit was open. Reload and try again.",
            409,
        )
    webui_config._save_yaml_config_file(path, config_data)


def _commit_config_and_env(
    home: Path,
    config_data: dict[str, Any],
    *,
    expected_revision: FileSnapshot,
    env_updates: dict[str, str | None],
) -> None:
    """Publish profile credentials first and YAML last with safe rollback."""
    config_path = home / "config.yaml"
    env_path = home / ".env"
    intended_config = webui_config._serialize_yaml_config_file(config_data)
    original_env = snapshot_file(env_path)
    published_env = None
    save_attempted = False
    try:
        if env_updates:
            _write_env_file(env_path, env_updates)
            published_env = snapshot_file(env_path)
        save_attempted = True
        _save_config(home, config_data, expected_revision=expected_revision)
    except Exception:
        config_may_be_published = False
        if save_attempted:
            try:
                current_config = snapshot_file(config_path)
                config_may_be_published = (
                    current_config.existed
                    and current_config.data == intended_config
                )
            except Exception:
                # If publication state cannot be determined, retain credentials:
                # an extra profile-local secret is safer than committed YAML that
                # references a credential we just rolled back.
                config_may_be_published = True
        if published_env is not None and not config_may_be_published:
            try:
                if not restore_file_if_unchanged(
                    env_path, published_env, original_env
                ):
                    logger.warning(
                        "Custom-model credential rollback skipped because .env changed"
                    )
            except Exception:
                logger.exception("Failed to roll back custom-model credential update")
        raise


def _write_env_file(env_path: Path, updates: dict[str, str | None]) -> None:
    """Atomically update a profile .env file without mutating os.environ."""
    from api.streaming import _ENV_LOCK

    with _ENV_LOCK:
        existing_lines: list[str] = []
        if env_path.exists():
            try:
                existing_lines = env_path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                raise CustomModelError("Profile credential file could not be read.", 409) from exc
        key_indices: dict[str, int] = {}
        for index, raw in enumerate(existing_lines):
            stripped = raw.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key_indices[stripped.split("=", 1)[0].strip()] = index
        output: list[str | None] = list(existing_lines)
        new_lines: list[str] = []
        for key, value in updates.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(key or "")):
                raise CustomModelError("Credential environment variable name is invalid.")
            if value is None:
                if key in key_indices:
                    output[key_indices[key]] = None
                continue
            clean = str(value).strip()
            if not clean:
                continue
            if "\n" in clean or "\r" in clean:
                raise CustomModelError("API key must not contain newline characters.")
            rendered = f"{key}={clean}"
            if key in key_indices:
                output[key_indices[key]] = rendered
            else:
                new_lines.append(rendered)
        final_lines = [line for line in output if line is not None]
        if new_lines:
            if final_lines and final_lines[-1].strip():
                final_lines.append("")
            final_lines.extend(new_lines)
        content = "\n".join(final_lines)
        if content:
            content += "\n"
        env_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            dir=str(env_path.parent), prefix=".env_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(temporary, env_path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


def _refresh_runtime_caches(provider_id: str | None = None) -> None:
    """Invalidate WebUI and Agent caches without interrupting active streams."""
    try:
        webui_config.reload_config()
    except Exception:
        logger.debug("Failed to reload WebUI config cache", exc_info=True)
    try:
        webui_config.invalidate_models_cache()
    except Exception:
        logger.debug("Failed to invalidate model cache", exc_info=True)
    if provider_id:
        try:
            webui_config.invalidate_provider_models_cache(provider_id)
        except Exception:
            logger.debug("Failed to invalidate provider model cache", exc_info=True)
        try:
            webui_config.invalidate_credential_pool_cache(provider_id)
        except Exception:
            logger.debug("Failed to invalidate credential pool cache", exc_info=True)
    try:
        from api.providers import (
            invalidate_account_usage_status_cache,
            invalidate_providers_cache,
        )

        if provider_id:
            invalidate_account_usage_status_cache(provider_id)
        invalidate_providers_cache()
    except Exception:
        logger.debug("Failed to invalidate providers cache", exc_info=True)
    try:
        routes_module = sys.modules.get("api.routes")
        clear_live_models = getattr(routes_module, "_clear_live_models_cache", None)
        if callable(clear_live_models):
            clear_live_models()
    except Exception:
        logger.debug("Failed to invalidate live model cache", exc_info=True)


def _provider_slug(name: object) -> str:
    raw = str(name or "").strip().lower()
    if raw.startswith("custom:"):
        raw = raw[len("custom:") :]
    slug = re.sub(r"[^a-z0-9._-]+", "-", raw).strip("-._")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        raise CustomModelError("Provider name must contain letters or numbers.")
    return f"custom:{slug}"


def _env_name(provider_id: str) -> str:
    identity = provider_id[len("custom:") :] if provider_id.startswith("custom:") else provider_id
    slug = re.sub(r"[^A-Z0-9]+", "_", identity.upper()).strip("_") or "ENDPOINT"
    digest = hashlib.sha256(provider_id.lower().encode("utf-8")).hexdigest()[:10].upper()
    return f"HERMES_CUSTOM_{slug[:40]}_{digest}_API_KEY"


def _allocate_owned_env_name(provider_id: str, blocked: set[str]) -> str:
    """Return a collision-resistant key name not owned by imported/shared config."""
    for index in range(100):
        identity = provider_id if index == 0 else f"{provider_id}:webui-{index}"
        candidate = _env_name(identity)
        if candidate not in blocked:
            return candidate
    raise CustomModelError("Could not allocate an exclusive credential name.", 409)


def _canonical_api_mode(value: object) -> str:
    raw = str(value or "").strip().lower()
    mode = _API_MODE_ALIASES.get(raw, raw)
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", mode):
        raise CustomModelError("API protocol name is invalid.")
    return mode


def _normalize_base_url(value: object) -> str:
    base_url = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise CustomModelError("Base URL must start with http:// or https:// and include a host.")
    if parsed.username or parsed.password:
        raise CustomModelError("Base URL must not contain embedded credentials.")
    if parsed.query or parsed.fragment:
        raise CustomModelError("Base URL must not contain a query string or fragment.")
    return base_url


def _origin_key(value: object) -> tuple[str, str, int]:
    normalized = _normalize_base_url(value)
    parsed = urllib.parse.urlparse(normalized)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme.lower(), str(parsed.hostname or "").lower(), int(port)


def _model_ids(raw: object) -> list[str]:
    if isinstance(raw, dict):
        values = raw.keys()
    elif isinstance(raw, list):
        values = raw
    else:
        values = []
    result: list[str] = []
    for item in values:
        if isinstance(item, dict):
            candidate = item.get("id") or item.get("model") or item.get("name")
        else:
            candidate = item
        model_id = str(candidate or "").strip()
        if model_id and model_id not in result:
            result.append(model_id)
    return result


def _entry_model_ids(entry: dict[str, Any]) -> list[str]:
    result: list[str] = []
    default_model = str(entry.get("default_model") or entry.get("model") or "").strip()
    if default_model:
        result.append(default_model)
    for model_id in _model_ids(entry.get("models")):
        if model_id not in result:
            result.append(model_id)
    return result


def _entry_key_env(entry: dict[str, Any]) -> str:
    key_env = str(entry.get("key_env") or entry.get("api_key_env") or "").strip()
    if key_env:
        return key_env
    raw_key = str(entry.get("api_key") or "").strip()
    if raw_key.startswith("${") and raw_key.endswith("}"):
        return raw_key[2:-1].strip()
    return ""


_ORIGIN_BOUND_ADVANCED_FIELDS = (
    "extra_headers",
    "extra_body",
    "key_cmd",
    "credentials",
    "credential_pool",
    "client_cert",
    "client_key",
    "tls",
    "proxy",
)


def _entry_has_origin_bound_request_metadata(entry: dict[str, Any]) -> bool:
    return any(entry.get(field) not in (None, "", {}, []) for field in _ORIGIN_BOUND_ADVANCED_FIELDS)


def _profile_scoped_env_value(
    key: str, env_values: dict[str, str]
) -> str:
    if key in env_values:
        return str(env_values.get(key) or "").strip()
    try:
        return str(webui_config._thread_local_env_value(key) or "").strip()
    except Exception:
        return ""


def _entry_has_key(entry: dict[str, Any], env_values: dict[str, str]) -> bool:
    key_env = _entry_key_env(entry)
    if key_env and _profile_scoped_env_value(key_env, env_values):
        return True
    if str(entry.get("key_cmd") or "").strip():
        return True
    if entry.get("credentials") or entry.get("credential_pool"):
        return True
    raw_key = str(entry.get("api_key") or "").strip()
    return bool(raw_key and not (raw_key.startswith("${") and raw_key.endswith("}")))


def _entry_api_key(entry: dict[str, Any], env_values: dict[str, str]) -> str:
    key_env = _entry_key_env(entry)
    if key_env:
        return _profile_scoped_env_value(key_env, env_values)
    raw_key = str(entry.get("api_key") or "").strip()
    if raw_key.startswith("${") and raw_key.endswith("}"):
        return ""
    return raw_key


def _validate_config_containers(config_data: dict[str, Any]) -> None:
    if "providers" in config_data and not isinstance(config_data.get("providers"), dict):
        raise CustomModelError("Hermes providers config must be a mapping; no changes were applied.", 409)
    if "custom_providers" in config_data and not isinstance(
        config_data.get("custom_providers"), list
    ):
        raise CustomModelError(
            "Hermes custom_providers config must be a list; no changes were applied.",
            409,
        )
    if "model" in config_data and not isinstance(config_data.get("model"), dict):
        raise CustomModelError("Hermes model config must be a mapping; no changes were applied.", 409)


def _modern_custom_entries(config_data: dict[str, Any]):
    providers = config_data.get("providers")
    if not isinstance(providers, dict):
        return
    for storage_key, raw_entry in providers.items():
        if not isinstance(raw_entry, dict):
            continue
        base_url = raw_entry.get("base_url") or raw_entry.get("url") or raw_entry.get("api")
        if not str(base_url or "").strip():
            continue
        key = str(storage_key or "").strip()
        # Built-in provider override blocks are owned by their built-in cards;
        # this manager owns explicit custom identities and unknown endpoints.
        is_known = False
        try:
            is_known = webui_config._is_known_model_provider(key)
        except Exception:
            pass
        if is_known and not key.lower().startswith("custom:"):
            continue
        provider_id = key if key.lower().startswith("custom:") else f"custom:{key}"
        yield f"providers:{key}", "providers", provider_id, raw_entry


def _legacy_custom_entries(config_data: dict[str, Any]):
    entries = config_data.get("custom_providers")
    if not isinstance(entries, list):
        return
    normalized: list[tuple[int, str, dict[str, Any]]] = []
    counts: dict[str, int] = {}
    for index, raw_entry in enumerate(entries):
        if not isinstance(raw_entry, dict) or not str(raw_entry.get("name") or "").strip():
            continue
        provider_id = _provider_slug(raw_entry.get("name"))
        normalized.append((index, provider_id, raw_entry))
        counts[provider_id] = counts.get(provider_id, 0) + 1
    for index, provider_id, raw_entry in normalized:
        suffix = f":{index}" if counts[provider_id] > 1 else ""
        yield (
            f"custom_providers:{provider_id}{suffix}",
            "custom_providers",
            provider_id,
            raw_entry,
        )


def _all_entries(config_data: dict[str, Any]):
    yield from _legacy_custom_entries(config_data) or ()
    yield from _modern_custom_entries(config_data) or ()


def _find_entry(config_data: dict[str, Any], uid: object):
    wanted = str(uid or "").strip()
    if not wanted:
        return None
    matches = [item for item in _all_entries(config_data) if item[0] == wanted]
    if not matches:
        return None
    if len(matches) > 1:
        raise CustomModelError("Custom provider identity is ambiguous.", 409)
    return matches[0]


def _base_url_key(value: object) -> str:
    return str(value or "").strip().rstrip("/").lower()


def _provider_aliases(provider_id: str, entry: dict[str, Any]) -> set[str]:
    canonical = provider_id.strip().lower()
    aliases = {canonical}
    if canonical.startswith("custom:"):
        aliases.add(canonical[len("custom:") :])
    name = str(entry.get("name") or "").strip()
    if name:
        aliases.add(name.lower())
        try:
            normalized = _provider_slug(name).lower()
            aliases.add(normalized)
            aliases.add(normalized[len("custom:") :])
        except CustomModelError:
            pass
    return aliases


def _active_custom_provider_ids(config_data: dict[str, Any]) -> set[str]:
    model_cfg = config_data.get("model")
    if not isinstance(model_cfg, dict):
        return set()
    active_provider = str(model_cfg.get("provider") or "").strip().lower()
    entries = list(_all_entries(config_data))
    exact = {
        provider_id
        for _uid, _source, provider_id, entry in entries
        if active_provider in _provider_aliases(provider_id, entry)
    }
    if exact or active_provider != "custom":
        return exact
    active_base_url = _base_url_key(
        model_cfg.get("base_url") or model_cfg.get("url") or model_cfg.get("api")
    )
    if not active_base_url:
        return set()
    return {
        provider_id
        for _uid, _source, provider_id, entry in entries
        if _base_url_key(
            entry.get("base_url") or entry.get("url") or entry.get("api")
        )
        == active_base_url
    }


def _entry_context_length(entry: dict[str, Any], default_model: str) -> object:
    if entry.get("context_length") not in (None, ""):
        return entry.get("context_length")
    models = entry.get("models")
    if isinstance(models, dict):
        metadata = models.get(default_model)
        if isinstance(metadata, dict):
            return metadata.get("context_length")
    elif isinstance(models, list):
        for item in models:
            if (
                isinstance(item, dict)
                and _model_ids([item]) == [default_model]
            ):
                return item.get("context_length")
    return None


def _serialize_entry(
    uid: str,
    source: str,
    provider_id: str,
    entry: dict[str, Any],
    *,
    active_provider_ids: set[str],
    env_values: dict[str, str],
) -> dict[str, Any]:
    models = _entry_model_ids(entry)
    default_model = str(entry.get("default_model") or entry.get("model") or "").strip()
    if not default_model and models:
        default_model = models[0]
    raw_mode = (
        entry.get("api_mode")
        if "api_mode" in entry
        else entry.get("transport")
        if "transport" in entry
        else ""
    )
    api_mode_explicit = bool(str(raw_mode or "").strip())
    try:
        api_mode = _canonical_api_mode(raw_mode) if api_mode_explicit else "auto"
    except CustomModelError:
        api_mode = str(raw_mode or "").strip() or "auto"
    return {
        "uid": uid,
        "source": source,
        "provider_id": provider_id,
        "name": str(entry.get("name") or provider_id.removeprefix("custom:")).strip(),
        "base_url": str(
            entry.get("base_url") or entry.get("url") or entry.get("api") or ""
        ).strip().rstrip("/"),
        "api_mode": api_mode,
        "api_mode_explicit": api_mode_explicit,
        "models": models,
        "default_model": default_model,
        "context_length": _entry_context_length(entry, default_model),
        "discover_models": bool(entry.get("discover_models", True)),
        "enabled": entry.get("enabled") is not False,
        "test_supported": api_mode in _TEST_API_MODES,
        "has_api_key": _entry_has_key(entry, env_values),
        "is_active": provider_id in active_provider_ids,
    }


def _custom_models_payload(
    config_data: dict[str, Any], env_values: dict[str, str]
) -> dict[str, Any]:
    model_cfg = (
        config_data.get("model")
        if isinstance(config_data.get("model"), dict)
        else {}
    )
    active_provider = str(model_cfg.get("provider") or "")
    active_provider_ids = _active_custom_provider_ids(config_data)
    rows = [
        _serialize_entry(
            uid,
            source,
            provider_id,
            entry,
            active_provider_ids=active_provider_ids,
            env_values=env_values,
        )
        for uid, source, provider_id, entry in _all_entries(config_data)
    ]
    rows.sort(
        key=lambda row: (
            not row["is_active"],
            row["name"].lower(),
            row["provider_id"],
        )
    )
    return {
        "providers": rows,
        "active_provider": active_provider or None,
        "active_custom_provider": next(iter(active_provider_ids), None)
        if len(active_provider_ids) == 1
        else None,
        "active_model": str(
            model_cfg.get("default") or model_cfg.get("model") or ""
        )
        or None,
        "api_modes": sorted(_TEST_API_MODES),
    }


def list_custom_models() -> dict[str, Any]:
    home = _active_home()
    with _shared_profile_mutation_lock(home):
        _assert_profile_home(home)
        config_data = _load_config(home)
        _validate_config_containers(config_data)
        env_values = _load_env_file(home / ".env")
        return _custom_models_payload(config_data, env_values)


def _validated_models(body: dict[str, Any]) -> tuple[list[str], str]:
    models = _model_ids(body.get("models"))
    default_model = str(body.get("default_model") or body.get("model") or "").strip()
    if default_model and default_model not in models:
        models.insert(0, default_model)
    if not models:
        raise CustomModelError("Add at least one model ID.")
    if not default_model:
        default_model = models[0]
    return models, default_model


def _merge_models(existing: object, model_ids: list[str]) -> object:
    if isinstance(existing, list):
        by_id: dict[str, object] = {}
        for item in existing:
            ids = _model_ids([item])
            if ids:
                by_id[ids[0]] = item
        return [
            copy.deepcopy(by_id[model_id])
            if model_id in by_id
            else {"id": model_id}
            for model_id in model_ids
        ]
    existing_map = existing if isinstance(existing, dict) else {}
    return {
        model_id: copy.deepcopy(existing_map.get(model_id))
        if isinstance(existing_map.get(model_id), dict)
        else {}
        for model_id in model_ids
    }


def _clear_model_context_metadata(models: object) -> None:
    if isinstance(models, dict):
        for metadata in models.values():
            if isinstance(metadata, dict):
                metadata.pop("context_length", None)
    elif isinstance(models, list):
        for item in models:
            if isinstance(item, dict):
                item.pop("context_length", None)


def _set_default_model_context(models: object, default_model: str, context: int) -> None:
    if isinstance(models, dict):
        metadata = models.setdefault(default_model, {})
        if isinstance(metadata, dict):
            metadata["context_length"] = context
    elif isinstance(models, list):
        for item in models:
            if isinstance(item, dict) and _model_ids([item]) == [default_model]:
                item["context_length"] = context
                break


def _active_provider(config_data: dict[str, Any]) -> str:
    model_cfg = config_data.get("model")
    if not isinstance(model_cfg, dict):
        return ""
    return str(model_cfg.get("provider") or "").strip()


def _assign_default(
    config_data: dict[str, Any], provider_id: str, model: str, entry: dict[str, Any]
) -> None:
    model_cfg = config_data.get("model")
    if not isinstance(model_cfg, dict):
        model_cfg = {}
    model_cfg["provider"] = provider_id
    model_cfg["default"] = model
    model_cfg["base_url"] = str(
        entry.get("base_url") or entry.get("api") or entry.get("url") or ""
    ).strip().rstrip("/")
    key_env = _entry_key_env(entry)
    if key_env:
        model_cfg["key_env"] = key_env
        model_cfg.pop("api_key", None)
    else:
        model_cfg.pop("key_env", None)
        model_cfg.pop("api_key", None)
    config_data["model"] = model_cfg


def _referenced_env_names(config_data: dict[str, Any], *, excluding_uid: str = "") -> set[str]:
    """Collect credential references anywhere in config, conservatively.

    Garbage collection is allowed only for manager-owned keys that are no
    longer referenced by any provider, model, fallback, auxiliary task, or
    future schema container. The edited entry itself is skipped by object
    identity when ``excluding_uid`` is supplied.
    """
    excluded_entry = None
    if excluding_uid:
        ref = _find_entry(config_data, excluding_uid)
        if ref:
            excluded_entry = ref[3]
    refs: set[str] = set()
    seen: set[int] = set()

    def visit(value: object) -> None:
        if value is excluded_entry:
            return
        if isinstance(value, dict):
            identity = id(value)
            if identity in seen:
                return
            seen.add(identity)
            key_env = _entry_key_env(value)
            if key_env:
                refs.add(key_env)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            identity = id(value)
            if identity in seen:
                return
            seen.add(identity)
            for child in value:
                visit(child)

    visit(config_data)
    return refs


def _entry_key_owned(entry: dict[str, Any]) -> bool:
    return entry.get("webui_managed_key_env") is True


def _plan_entry_key(
    config_data: dict[str, Any],
    uid: str,
    entry: dict[str, Any],
    provider_id: str,
    body: dict[str, Any],
) -> tuple[dict[str, str | None], set[str]]:
    """Mutate credential references and return file updates plus cleanup candidates."""
    old_key_env = _entry_key_env(entry)
    old_owned = _entry_key_owned(entry)
    submitted_raw = body.get("api_key")
    submitted = str(submitted_raw or "").strip()
    clear_key = bool(body.get("clear_api_key"))
    if submitted and clear_key:
        raise CustomModelError("Choose either credential replacement or removal, not both.")
    if submitted:
        if "\n" in submitted or "\r" in submitted:
            raise CustomModelError("API key must not contain newline characters.")
        if len(submitted) < 8:
            raise CustomModelError("API key appears too short.")

    updates: dict[str, str | None] = {}
    cleanup: set[str] = set()
    other_refs = _referenced_env_names(config_data, excluding_uid=uid)
    if clear_key:
        entry.pop("key_env", None)
        entry.pop("api_key_env", None)
        entry.pop("api_key", None)
        entry.pop("webui_managed_key_env", None)
        if old_key_env and old_owned:
            cleanup.add(old_key_env)
        return updates, cleanup

    if submitted:
        if old_owned and old_key_env and old_key_env not in other_refs:
            key_env = old_key_env
        else:
            blocked = set(other_refs)
            if old_key_env and not old_owned:
                blocked.add(old_key_env)
            key_env = _allocate_owned_env_name(provider_id, blocked)
        updates[key_env] = submitted
        entry["key_env"] = key_env
        entry["webui_managed_key_env"] = True
        entry.pop("api_key_env", None)
        entry.pop("api_key", None)
        if key_env != old_key_env and old_key_env and old_owned:
            cleanup.add(old_key_env)
        return updates, cleanup

    raw_key = str(entry.get("api_key") or "").strip()
    if raw_key and not (raw_key.startswith("${") and raw_key.endswith("}")):
        if old_owned and old_key_env and old_key_env not in other_refs:
            key_env = old_key_env
        else:
            blocked = set(other_refs)
            if old_key_env and not old_owned:
                blocked.add(old_key_env)
            key_env = _allocate_owned_env_name(provider_id, blocked)
        updates[key_env] = raw_key
        entry["key_env"] = key_env
        entry["webui_managed_key_env"] = True
        entry.pop("api_key", None)
    elif raw_key.startswith("${") and raw_key.endswith("}"):
        entry["key_env"] = raw_key[2:-1].strip()
        entry.pop("api_key", None)
    return updates, cleanup


def upsert_custom_model(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise CustomModelError("JSON object required.")
    _assert_expected_profile(body)
    home = _active_home()
    with _shared_profile_mutation_lock(home):
        _assert_profile_home(home)
        config_path = home / "config.yaml"
        revision = _config_revision(config_path)
        config_data = _load_config(home)
        _validate_config_containers(config_data)
        env_values = _load_env_file(home / ".env")
        existing_ref = _find_entry(config_data, body.get("uid"))
        name = str(body.get("name") or "").strip()
        if not name:
            raise CustomModelError("Provider name is required.")
        base_url = _normalize_base_url(body.get("base_url"))
        api_mode_supplied = "api_mode" in body and body.get("api_mode") is not None
        api_mode = (
            _canonical_api_mode(body.get("api_mode"))
            if api_mode_supplied or not existing_ref
            else None
        )
        models, default_model = _validated_models(body)

        if existing_ref:
            uid, source, provider_id, existing = existing_ref
            entry = copy.deepcopy(existing)
            storage_key = uid.split(":", 1)[1] if source == "providers" else provider_id
            existing_base_url = (
                existing.get("base_url") or existing.get("api") or existing.get("url")
            )
            submitted_key = str(body.get("api_key") or "").strip()
            origin_changed = bool(
                existing_base_url
                and _origin_key(existing_base_url) != _origin_key(base_url)
            )
            if origin_changed and _entry_has_origin_bound_request_metadata(existing):
                raise CustomModelError(
                    "Endpoint origin changed, but this provider has advanced request metadata bound to the original origin. Remove or update that metadata before changing the endpoint.",
                    409,
                )
            if (
                origin_changed
                and _entry_has_key(existing, env_values)
                and not submitted_key
                and not body.get("clear_api_key")
            ):
                raise CustomModelError(
                    "Endpoint origin changed. Re-enter or remove the stored credential before saving.",
                    409,
                )
        else:
            source = "providers"
            provider_id = _provider_slug(body.get("provider_id") or name)
            storage_key = provider_id
            uid = f"providers:{storage_key}"
            if any(
                candidate[2].lower() == provider_id.lower()
                for candidate in _all_entries(config_data)
            ):
                raise CustomModelError("A custom provider with this ID already exists.", 409)
            entry = {}

        was_active = provider_id in _active_custom_provider_ids(config_data)
        enabled = entry.get("enabled") is not False
        if "enabled" in body:
            enabled = bool(body.get("enabled"))
        elif not existing_ref:
            enabled = True
        if was_active and not enabled:
            raise CustomModelError(
                "Choose another default provider before disabling this provider.", 409
            )

        entry["name"] = name
        if enabled:
            entry.pop("enabled", None)
        else:
            entry["enabled"] = False
        entry["models"] = _merge_models(entry.get("models"), models)
        if source == "providers":
            entry["api"] = base_url
            if api_mode is not None:
                entry["transport"] = api_mode
            entry["default_model"] = default_model
            for stale_key in ("base_url", "url", "api_mode", "model"):
                entry.pop(stale_key, None)
        else:
            entry["base_url"] = base_url
            if api_mode is not None:
                entry["api_mode"] = api_mode
            entry["model"] = default_model
            entry.pop("default_model", None)
        if "discover_models" in body:
            entry["discover_models"] = bool(body.get("discover_models"))
        elif not existing_ref:
            entry["discover_models"] = True

        if "context_length" in body:
            context_length = body.get("context_length")
            if context_length in (None, ""):
                entry.pop("context_length", None)
                _clear_model_context_metadata(entry.get("models"))
            else:
                try:
                    parsed_context = int(context_length)
                except (TypeError, ValueError) as exc:
                    raise CustomModelError(
                        "Context length must be a positive integer."
                    ) from exc
                if parsed_context <= 0:
                    raise CustomModelError("Context length must be a positive integer.")
                entry["context_length"] = parsed_context
                _clear_model_context_metadata(entry.get("models"))
                _set_default_model_context(
                    entry.get("models"), default_model, parsed_context
                )

        env_updates, cleanup_candidates = _plan_entry_key(
            config_data, uid, entry, provider_id, body
        )
        migrate_legacy = (
            source == "custom_providers" and _provider_slug(name) != provider_id
        )
        if migrate_legacy and sum(
            1 for candidate in _all_entries(config_data) if candidate[2] == provider_id
        ) > 1:
            raise CustomModelError(
                "Duplicate legacy provider names must be made unique before renaming.",
                409,
            )
        if source == "providers" or migrate_legacy:
            providers = config_data.get("providers")
            if not isinstance(providers, dict):
                providers = {}
            if migrate_legacy and storage_key in providers:
                raise CustomModelError(
                    "A modern provider already owns this stable identity.", 409
                )
            providers[storage_key] = entry
            config_data["providers"] = providers
            if migrate_legacy:
                legacy = config_data.get("custom_providers")
                if not isinstance(legacy, list):
                    raise CustomModelError("Legacy custom provider list is invalid.", 409)
                config_data["custom_providers"] = [
                    candidate for candidate in legacy if candidate is not existing_ref[3]
                ]
                source = "providers"
                uid = f"providers:{storage_key}"
                # Migrated entries must use the modern schema.
                entry["api"] = entry.pop("base_url")
                if "api_mode" in entry:
                    entry["transport"] = entry.pop("api_mode")
                entry["default_model"] = entry.pop("model")
        else:
            legacy = config_data.get("custom_providers")
            if not isinstance(legacy, list):
                raise CustomModelError("Legacy custom provider list is invalid.", 409)
            replaced = False
            for index, candidate in enumerate(legacy):
                if candidate is existing_ref[3]:
                    legacy[index] = entry
                    replaced = True
                    break
            if not replaced:
                raise CustomModelError("Custom provider no longer exists.", 409)
            config_data["custom_providers"] = legacy

        if was_active or body.get("make_default"):
            _assign_default(config_data, provider_id, default_model, entry)
        final_refs = _referenced_env_names(config_data)
        for candidate in cleanup_candidates:
            if candidate not in final_refs:
                env_updates[candidate] = None

        _commit_config_and_env(
            home,
            config_data,
            expected_revision=revision,
            env_updates=env_updates,
        )
        payload = _custom_models_payload(
            config_data, _load_env_file(home / ".env")
        )
        provider = next(
            (row for row in payload["providers"] if row["uid"] == uid), None
        )

    _refresh_runtime_caches(provider_id)
    return {"ok": True, "provider": provider, **payload}


def activate_custom_model(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise CustomModelError("JSON object required.")
    _assert_expected_profile(body)
    home = _active_home()
    with _shared_profile_mutation_lock(home):
        _assert_profile_home(home)
        revision = _config_revision(home / "config.yaml")
        config_data = _load_config(home)
        _validate_config_containers(config_data)
        ref = _find_entry(config_data, body.get("uid"))
        if not ref:
            raise CustomModelError("Custom provider not found.", 404)
        uid, _source, provider_id, entry = ref
        if entry.get("enabled") is False:
            raise CustomModelError("Enable this provider before making it default.", 409)
        models = _entry_model_ids(entry)
        model = str(
            body.get("model") or entry.get("default_model") or entry.get("model") or ""
        ).strip()
        if not model or model not in models:
            raise CustomModelError("Choose a configured model before making it default.")
        _assign_default(config_data, provider_id, model, entry)
        _save_config(home, config_data, expected_revision=revision)
    _refresh_runtime_caches(provider_id)
    return {"ok": True, "uid": uid, "provider": provider_id, "model": model}


def delete_custom_model(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise CustomModelError("JSON object required.")
    _assert_expected_profile(body)
    home = _active_home()
    with _shared_profile_mutation_lock(home):
        _assert_profile_home(home)
        revision = _config_revision(home / "config.yaml")
        config_data = _load_config(home)
        _validate_config_containers(config_data)
        ref = _find_entry(config_data, body.get("uid"))
        if not ref:
            raise CustomModelError("Custom provider not found.", 404)
        uid, source, provider_id, entry = ref
        active_provider = _active_provider(config_data).lower()
        active_custom_ids = _active_custom_provider_ids(config_data)
        active_identity_unknown = active_provider == "custom" and not active_custom_ids
        if provider_id in active_custom_ids or active_identity_unknown:
            raise CustomModelError(
                "This provider may be the current default. Choose another default model before deleting it.",
                409,
            )
        key_env = _entry_key_env(entry)
        key_owned = _entry_key_owned(entry)
        if source == "providers":
            providers = config_data.get("providers")
            if not isinstance(providers, dict):
                raise CustomModelError("Custom provider no longer exists.", 409)
            storage_key = uid.split(":", 1)[1]
            providers.pop(storage_key, None)
            config_data["providers"] = providers
        else:
            legacy = config_data.get("custom_providers")
            if not isinstance(legacy, list):
                raise CustomModelError("Custom provider no longer exists.", 409)
            config_data["custom_providers"] = [
                candidate for candidate in legacy if candidate is not entry
            ]
        env_updates: dict[str, str | None] = {}
        if (
            key_owned
            and key_env
            and key_env not in _referenced_env_names(config_data)
        ):
            env_updates[key_env] = None
        _commit_config_and_env(
            home,
            config_data,
            expected_revision=revision,
            env_updates=env_updates,
        )
        payload = _custom_models_payload(
            config_data, _load_env_file(home / ".env")
        )
    _refresh_runtime_caches(provider_id)
    return {"ok": True, "deleted": uid, **payload}


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_JSON_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler(),
    urllib.request.HTTPHandler(),
    urllib.request.HTTPSHandler(),
    _NoRedirectHandler(),
)


def _json_request(
    url: str,
    *,
    method: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
) -> tuple[int, dict[str, Any]]:
    encoded = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=encoded, headers=headers, method=method)
    try:
        with _JSON_OPENER.open(req, timeout=timeout) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            status = int(getattr(response, "status", 200))
    except urllib.error.HTTPError as exc:
        try:
            exc.read(4096)
        except Exception:
            pass
        raise CustomModelError(f"Endpoint returned HTTP {exc.code}.", 400) from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            message = "Endpoint test timed out."
        elif isinstance(reason, ConnectionRefusedError):
            message = "Endpoint refused the connection."
        else:
            message = "Could not reach endpoint."
        raise CustomModelError(message, 400) from exc
    except TimeoutError as exc:
        raise CustomModelError("Endpoint test timed out.", 400) from exc
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise CustomModelError("Endpoint response was too large.")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise CustomModelError("Endpoint returned invalid JSON.") from exc
    if not isinstance(decoded, dict):
        raise CustomModelError("Endpoint returned an unexpected JSON shape.")
    return status, decoded


def _stored_test_values(
    config_data: dict[str, Any], env_values: dict[str, str], uid: object
):
    ref = _find_entry(config_data, uid)
    if not ref:
        return {}, "", ""
    _uid, _source, provider_id, entry = ref
    owned_entry = copy.deepcopy(entry)
    return owned_entry, _entry_api_key(owned_entry, env_values), provider_id


def discover_custom_models(body: dict[str, Any]) -> dict[str, Any]:
    """Discover model IDs from a draft or stored custom provider endpoint."""
    if not isinstance(body, dict):
        raise CustomModelError("JSON object required.")
    _assert_expected_profile(body)
    home = _active_home()
    with _shared_profile_mutation_lock(home):
        _assert_profile_home(home)
        config_data = _load_config(home)
        _validate_config_containers(config_data)
        env_values = _load_env_file(home / ".env")
        stored_entry, stored_key, _provider_id = _stored_test_values(
            config_data, env_values, body.get("uid")
        )

    stored_base_url = (
        stored_entry.get("base_url")
        or stored_entry.get("api")
        or stored_entry.get("url")
    )
    base_url = _normalize_base_url(body.get("base_url") or stored_base_url)
    api_mode = _canonical_api_mode(
        body.get("api_mode")
        or stored_entry.get("api_mode")
        or stored_entry.get("transport")
        or "chat_completions"
    )
    submitted_key = str(body.get("api_key") or "").strip()
    if submitted_key and body.get("clear_api_key"):
        raise CustomModelError(
            "Choose either credential replacement or removal, not both."
        )
    if (
        stored_key
        and stored_base_url
        and _origin_key(stored_base_url) != _origin_key(base_url)
        and not submitted_key
        and not body.get("clear_api_key")
    ):
        raise CustomModelError(
            "Endpoint origin changed. Re-enter the credential before fetching models.",
            409,
        )
    api_key = submitted_key or ("" if body.get("clear_api_key") else stored_key)
    if api_key and ("\n" in api_key or "\r" in api_key):
        raise CustomModelError("API key must not contain newline characters.")

    from api.provider_endpoint_probe import probe_models_endpoint

    probe_provider = "anthropic" if api_mode == "anthropic_messages" else "custom"
    result = probe_models_endpoint(
        probe_provider,
        base_url,
        api_key or None,
        timeout=8.0,
    )
    if not result.get("ok"):
        detail = str(result.get("detail") or "Model endpoint could not be queried.")
        raise CustomModelError(f"Model discovery failed: {detail}", 400)
    models = _model_ids(result.get("models"))
    if not models:
        raise CustomModelError("The endpoint returned no model IDs.", 400)
    return {"ok": True, "models": models, "count": len(models)}


def test_custom_model(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise CustomModelError("JSON object required.")
    _assert_expected_profile(body)
    home = _active_home()
    with _shared_profile_mutation_lock(home):
        _assert_profile_home(home)
        config_data = _load_config(home)
        _validate_config_containers(config_data)
        env_values = _load_env_file(home / ".env")
        stored_entry, stored_key, provider_id = _stored_test_values(
            config_data, env_values, body.get("uid")
        )
    stored_base_url = (
        stored_entry.get("base_url")
        or stored_entry.get("api")
        or stored_entry.get("url")
    )
    base_url = _normalize_base_url(body.get("base_url") or stored_base_url)
    api_mode = _canonical_api_mode(
        body.get("api_mode")
        or stored_entry.get("api_mode")
        or stored_entry.get("transport")
    )
    if api_mode not in _TEST_API_MODES:
        raise CustomModelError(
            "This protocol is preserved, but the lightweight model test does not support it.",
            409,
        )
    model = str(
        body.get("model")
        or body.get("default_model")
        or stored_entry.get("default_model")
        or stored_entry.get("model")
        or ""
    ).strip()
    if not model:
        raise CustomModelError("Model ID is required for an inference test.")
    submitted_key = str(body.get("api_key") or "").strip()
    if submitted_key and body.get("clear_api_key"):
        raise CustomModelError(
            "Choose either credential replacement or removal, not both."
        )
    origin_changed = bool(
        stored_base_url
        and _origin_key(stored_base_url) != _origin_key(base_url)
    )
    if origin_changed and _entry_has_origin_bound_request_metadata(stored_entry):
        raise CustomModelError(
            "Endpoint origin changed, but this provider has advanced request metadata bound to the original origin. Save compatible metadata for the new endpoint before testing it.",
            409,
        )
    if (
        stored_key
        and origin_changed
        and not submitted_key
        and not body.get("clear_api_key")
    ):
        raise CustomModelError(
            "Endpoint origin changed. Re-enter the credential before testing this draft.",
            409,
        )
    api_key = submitted_key or ("" if body.get("clear_api_key") else stored_key)
    if api_key and ("\n" in api_key or "\r" in api_key):
        raise CustomModelError("API key must not contain newline characters.")

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Hermes-WebUI/1.0",
    }
    if api_mode == "anthropic_messages":
        url = f"{base_url}/messages"
        if api_key:
            headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
        payload = {
            "model": model,
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "Reply with exactly OK"}],
        }
    elif api_mode == "codex_responses":
        url = f"{base_url}/responses"
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": model,
            "input": "Reply with exactly OK",
            "max_output_tokens": 8,
            "stream": False,
        }
    else:
        url = f"{base_url}/chat/completions"
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with exactly OK"}],
            "max_tokens": 8,
            "stream": False,
        }

    extra_headers = stored_entry.get("extra_headers")
    if isinstance(extra_headers, dict):
        for key, value in extra_headers.items():
            header_name = str(key or "").strip()
            header_value = str(value or "").strip()
            if (
                header_name
                and header_value
                and header_name.lower()
                not in (
                    {"host", "content-length", "authorization", "x-api-key"}
                    if api_key
                    else {"host", "content-length"}
                )
                and "\n" not in header_value
                and "\r" not in header_value
            ):
                headers[header_name] = header_value
    extra_body = stored_entry.get("extra_body")
    if isinstance(extra_body, dict):
        payload.update(copy.deepcopy(extra_body))
        payload["model"] = model
        payload["stream"] = False

    started = time.monotonic()
    try:
        status, response = _json_request(
            url,
            method="POST",
            headers=headers,
            payload=payload,
            timeout=_TEST_TIMEOUT_SECONDS,
        )
    except CustomModelError as exc:
        message = str(exc)
        if api_key:
            message = message.replace(api_key, "[redacted]")
        return {
            "ok": False,
            "inference_ok": False,
            "provider_id": provider_id or None,
            "model": model,
            "api_mode": api_mode,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "error": message,
        }

    shape_ok = False
    if api_mode == "chat_completions":
        shape_ok = isinstance(response.get("choices"), list) and bool(response["choices"])
    elif api_mode == "codex_responses":
        shape_ok = bool(response.get("output_text") or response.get("output") or response.get("id"))
    elif api_mode == "anthropic_messages":
        shape_ok = isinstance(response.get("content"), list) and bool(response["content"])
    if not shape_ok:
        return {
            "ok": False,
            "inference_ok": False,
            "provider_id": provider_id or None,
            "model": model,
            "api_mode": api_mode,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "status": status,
            "error": "Endpoint responded, but the response shape did not match the selected API mode.",
        }
    return {
        "ok": True,
        "inference_ok": True,
        "provider_id": provider_id or None,
        "model": model,
        "api_mode": api_mode,
        "latency_ms": round((time.monotonic() - started) * 1000),
        "status": status,
        "message": "Model returned a valid inference response.",
    }
