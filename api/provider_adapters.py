"""Provider adapter layer — test connections & discover models from self-hosted servers.

Supports Ollama, LM Studio, and custom OpenAI-compatible endpoints.
Called from API routes so the Settings → Providers panel can probe
connectivity and populate model dropdowns without restarting the server.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# ── Connection-test timeout (seconds) ──────────────────────────────────────
_CONNECTION_TEST_TIMEOUT = 5.0

# ── Model-discovery TTL cache (seconds) ────────────────────────────────────
_MODEL_CACHE_TTL_SECONDS = 300  # 5 minutes
_MODEL_CACHE_MAX_ENTRIES = 32

_model_cache: dict[tuple[str, str, str], tuple[float, list[dict[str, str]]]] = {}
_model_cache_lock = threading.Lock()


def _cache_key(provider_id: str, base_url: str) -> tuple[str, str, str]:
    return (provider_id.strip().lower(), base_url.strip().rstrip("/"), "v1")


def _get_cached_models(cache_key: tuple[str, str, str]) -> list[dict[str, str]] | None:
    now = time.monotonic()
    with _model_cache_lock:
        entry = _model_cache.get(cache_key)
        if entry is None:
            return None
        cached_at, models = entry
        if now - cached_at <= _MODEL_CACHE_TTL_SECONDS:
            return models
        _model_cache.pop(cache_key, None)
    return None


def _set_cached_models(
    cache_key: tuple[str, str, str],
    models: list[dict[str, str]],
) -> None:
    now = time.monotonic()
    with _model_cache_lock:
        _model_cache[cache_key] = (now, models)
        while len(_model_cache) > _MODEL_CACHE_MAX_ENTRIES:
            oldest = min(_model_cache, key=lambda k: _model_cache[k][0])
            _model_cache.pop(oldest, None)


def _ollama_base_url(base_url: str) -> str:
    """Strip /v1 suffix to reach the Ollama native API root."""
    url = base_url.strip().rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    return url


def _http_get_json(url: str, timeout: float = _CONNECTION_TEST_TIMEOUT) -> tuple[Any, str | None]:
    """Perform a GET request; returns (parsed_json, error_or_None)."""
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            body = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
            return body, None
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}: {exc.reason}"
    except urllib.error.URLError as exc:
        return None, f"Connection failed: {exc.reason}"
    except (TimeoutError, OSError) as exc:
        return None, f"Connection timed out: {exc}"
    except json.JSONDecodeError:
        return None, "Invalid JSON response"
    except Exception as exc:
        return None, f"Unexpected error: {exc}"


# ── Ollama adapter ──────────────────────────────────────────────────────────

def _ollama_test_connection(base_url: str) -> dict[str, Any]:
    """Test connectivity to an Ollama server.

    Calls GET {ollama_base}/api/tags — the most reliable health-check
    endpoint that doesn't require a model to be loaded.
    """
    root = _ollama_base_url(base_url)
    url = f"{root}/api/tags"
    _, error = _http_get_json(url)
    return {
        "ok": error is None,
        "endpoint": url,
        "error": error,
    }


def _ollama_list_models(base_url: str) -> list[dict[str, str]]:
    """Discover models registered with an Ollama server.

    Calls GET {ollama_base}/api/tags and extracts model names.
    """
    root = _ollama_base_url(base_url)
    url = f"{root}/api/tags"
    body, error = _http_get_json(url)
    if error or not isinstance(body, dict):
        return []
    models_list = body.get("models")
    if not isinstance(models_list, list):
        return []
    result: list[dict[str, str]] = []
    for entry in models_list:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("model") or "").strip()
        if not name:
            continue
        label = name
        # Append size/detail if available
        size = entry.get("size")
        if size is not None:
            try:
                gb = float(size) / (1024 ** 3)
                label = f"{name} ({gb:.1f} GB)"
            except (ValueError, TypeError):
                pass
        result.append({"id": name, "label": label})
    return result


# ── LM Studio adapter ───────────────────────────────────────────────────────

def _lmstudio_test_connection(base_url: str) -> dict[str, Any]:
    """Test connectivity to an LM Studio server.

    Calls GET {base_url}/v1/models — the standard OpenAI-compatible
    models endpoint that LM Studio exposes.
    """
    url = f"{base_url.strip().rstrip('/')}/v1/models"
    _, error = _http_get_json(url)
    return {
        "ok": error is None,
        "endpoint": url,
        "error": error,
    }


def _lmstudio_list_models(base_url: str) -> list[dict[str, str]]:
    """Discover models loaded in LM Studio.

    Calls GET {base_url}/v1/models and extracts model IDs.
    """
    url = f"{base_url.strip().rstrip('/')}/v1/models"
    body, error = _http_get_json(url)
    if error or not isinstance(body, dict):
        return []
    data = body.get("data")
    if not isinstance(data, list):
        return []
    result: list[dict[str, str]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        mid = str(entry.get("id") or "").strip()
        if not mid:
            continue
        label = mid
        # LM Studio may include "owned_by"
        owned = str(entry.get("owned_by") or "").strip()
        if owned:
            label = f"{mid} ({owned})"
        result.append({"id": mid, "label": label})
    return result


# ── Custom OpenAI-compatible adapter ────────────────────────────────────────

def _custom_test_connection(base_url: str) -> dict[str, Any]:
    """Test connectivity to a custom OpenAI-compatible endpoint.

    Calls GET {base_url}/models — the standard OpenAI /v1/models endpoint.
    """
    url = f"{base_url.strip().rstrip('/')}/models"
    _, error = _http_get_json(url)
    return {
        "ok": error is None,
        "endpoint": url,
        "error": error,
    }


def _custom_list_models(base_url: str) -> list[dict[str, str]]:
    """Discover models from a custom OpenAI-compatible endpoint.

    Calls GET {base_url}/models and extracts model IDs.
    """
    url = f"{base_url.strip().rstrip('/')}/models"
    body, error = _http_get_json(url)
    if error or not isinstance(body, dict):
        return []
    data = body.get("data")
    if not isinstance(data, list):
        return []
    result: list[dict[str, str]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        mid = str(entry.get("id") or "").strip()
        if not mid:
            continue
        result.append({"id": mid, "label": mid})
    return result


# ── Provider adapter registry ───────────────────────────────────────────────

_PROVIDER_ADAPTERS: dict[str, dict[str, Any]] = {
    "ollama": {
        "test_connection": _ollama_test_connection,
        "list_models": _ollama_list_models,
    },
    "lmstudio": {
        "test_connection": _lmstudio_test_connection,
        "list_models": _lmstudio_list_models,
    },
    "custom": {
        "test_connection": _custom_test_connection,
        "list_models": _custom_list_models,
    },
}


# ── Public API ──────────────────────────────────────────────────────────────


def test_connection(provider_id: str, base_url: str) -> dict[str, Any]:
    """Probe connectivity to a self-hosted provider endpoint.

    Args:
        provider_id: Canonical provider slug (ollama, lmstudio, custom).
        base_url: The endpoint URL to test (e.g. http://localhost:11434/v1).

    Returns:
        Dict with ``ok``, ``provider``, ``endpoint``, ``error`` (if any),
        and ``tested_at`` (ISO UTC timestamp).
    """
    provider_id = provider_id.strip().lower()
    adapter = _PROVIDER_ADAPTERS.get(provider_id)
    if not adapter:
        return {
            "ok": False,
            "provider": provider_id,
            "error": f"No adapter for provider '{provider_id}'.",
        }

    base_url = base_url.strip()
    if not base_url:
        return {
            "ok": False,
            "provider": provider_id,
            "error": "base_url is required.",
        }

    test_fn = adapter["test_connection"]
    try:
        result = test_fn(base_url)
    except Exception as exc:
        logger.debug("Connection test for %s failed", provider_id, exc_info=True)
        result = {"ok": False, "endpoint": base_url, "error": str(exc)}

    from datetime import datetime, timezone

    result["provider"] = provider_id
    result["tested_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return result


def list_models(
    provider_id: str,
    base_url: str,
    *,
    refresh: bool = False,
) -> dict[str, Any]:
    """Discover available models from a self-hosted provider.

    Args:
        provider_id: Canonical provider slug.
        base_url: The endpoint URL.
        refresh: If True, bypass the TTL cache and re-fetch from the provider.

    Returns:
        Dict with ``ok``, ``provider``, ``models`` (list of {id, label}),
        ``total``, ``cached``, ``fetched_at``.
    """
    provider_id = provider_id.strip().lower()
    adapter = _PROVIDER_ADAPTERS.get(provider_id)
    if not adapter:
        return {
            "ok": False,
            "provider": provider_id,
            "error": f"No adapter for provider '{provider_id}'.",
        }

    base_url = base_url.strip()
    if not base_url:
        return {
            "ok": False,
            "provider": provider_id,
            "error": "base_url is required.",
        }

    ck = _cache_key(provider_id, base_url)
    if not refresh:
        cached = _get_cached_models(ck)
        if cached is not None:
            from datetime import datetime, timezone
            return {
                "ok": True,
                "provider": provider_id,
                "models": cached,
                "total": len(cached),
                "cached": True,
                "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }

    list_fn = adapter["list_models"]
    try:
        models = list_fn(base_url)
    except Exception as exc:
        logger.debug("Model discovery for %s failed", provider_id, exc_info=True)
        return {
            "ok": False,
            "provider": provider_id,
            "error": str(exc),
        }

    _set_cached_models(ck, models)

    from datetime import datetime, timezone
    return {
        "ok": True,
        "provider": provider_id,
        "models": models,
        "total": len(models),
        "cached": False,
        "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
