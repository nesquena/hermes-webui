"""
Hermes Web UI -- Agent API Gateway provider integration.

Discovers AI model instances from agent-api-gateway and integrates them
into hermes-webui's model dropdown and routing system.

This module is self-contained and communicates with the gateway exclusively
via its HTTP admin API. No gateway source code is imported.

Gateway model IDs use the format: @gateway-{label}:{model_name}/{keyword}
This fits hermes-webui's existing @provider:model convention.

Usage in config.yaml:
    gateway_providers:
      - label: local
        url: http://localhost:3000
      - label: remote
        url: http://10.1.73.240:3000

Environment variable overrides:
    AGENT_GATEWAY_LOCAL_URL  — overrides the first gateway entry
    AGENT_GATEWAY_REMOTE_URL — overrides the second gateway entry
"""

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

GatewayInstance = Dict[str, Any]

# ---------------------------------------------------------------------------
# Discovery cache (thread-safe, TTL-based)
# ---------------------------------------------------------------------------

_CACHE_LOCK = threading.Lock()
_CACHE: Dict[str, Tuple[float, List[GatewayInstance]]] = {}
DEFAULT_CACHE_TTL_S = 30


def _cache_key(url: str) -> str:
    return url.rstrip("/")


def _read_cache(url: str, max_age_s: float = DEFAULT_CACHE_TTL_S) -> Optional[List[GatewayInstance]]:
    key = _cache_key(url)
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        fetched_at, instances = entry
        if time.time() - fetched_at > max_age_s:
            return None
        return instances


def _write_cache(url: str, instances: List[GatewayInstance]) -> None:
    key = _cache_key(url)
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), instances)


def clear_cache(url: Optional[str] = None) -> None:
    """Clear discovery cache. If url is None, clears all entries."""
    with _CACHE_LOCK:
        if url is None:
            _CACHE.clear()
        else:
            _CACHE.pop(_cache_key(url), None)


# ---------------------------------------------------------------------------
# HTTP discovery
# ---------------------------------------------------------------------------

def _fetch_instances(gateway_url: str, timeout_s: float = 5.0) -> List[GatewayInstance]:
    """Fetch active instances from a gateway's admin API."""
    import urllib.request
    import urllib.error
    import json as _json

    admin_url = gateway_url.rstrip("/") + "/admin/instances"
    req = urllib.request.Request(admin_url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, list):
                return []
            return data
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.debug("gateway discovery failed for %s: %s", admin_url, exc)
        return []


def _filter_active(instances: List[GatewayInstance]) -> List[GatewayInstance]:
    return [i for i in instances if i.get("status") in ("ready", "busy")]


def discover_instances(
    gateway_url: str,
    *,
    max_age_s: float = DEFAULT_CACHE_TTL_S,
    force_refresh: bool = False,
) -> List[GatewayInstance]:
    """Return active gateway instances, using cache when possible."""
    if not force_refresh:
        cached = _read_cache(gateway_url, max_age_s)
        if cached is not None:
            return cached

    raw = _fetch_instances(gateway_url)
    active = _filter_active(raw)

    if active:
        _write_cache(gateway_url, active)
        return active

    # Fall back to last-known-good snapshot
    stale = _read_cache(gateway_url, max_age_s=float("inf"))
    return stale or active


# ---------------------------------------------------------------------------
# Model ID encoding / decoding
# ---------------------------------------------------------------------------

GATEWAY_PROVIDER_PREFIX = "gateway-"


def build_model_id(label: str, model_name: str, keyword: str) -> str:
    """Build a hermes-webui model ID: @gateway-{label}:{model_name}/{keyword}"""
    return f"@{GATEWAY_PROVIDER_PREFIX}{label}:{model_name}/{keyword}"


def parse_model_id(model_id: str) -> Optional[Dict[str, str]]:
    """Parse a gateway model ID. Returns None if not a gateway model.

    Returns dict with keys: label, model_name, keyword, provider_id
    """
    if not model_id.startswith("@" + GATEWAY_PROVIDER_PREFIX):
        return None

    # Strip leading @
    rest = model_id[1:]
    if ":" not in rest:
        return None

    provider_id, model_part = rest.split(":", 1)
    label = provider_id[len(GATEWAY_PROVIDER_PREFIX):]

    if "/" in model_part:
        model_name, keyword = model_part.split("/", 1)
    else:
        model_name = model_part
        keyword = ""

    return {
        "label": label,
        "model_name": model_name,
        "keyword": keyword,
        "provider_id": provider_id,
    }


def is_gateway_model(model_id: str) -> bool:
    return parse_model_id(model_id) is not None


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _load_gateway_configs() -> List[Dict[str, str]]:
    """Load gateway provider configs from hermes config and env vars.

    Returns list of dicts: [{"label": "local", "url": "http://..."}]
    """
    configs: List[Dict[str, str]] = []

    # Try to read from hermes config.yaml
    try:
        from api.config import cfg
        gw_list = cfg.get("gateway_providers", [])
        if isinstance(gw_list, list):
            for entry in gw_list:
                if isinstance(entry, dict) and entry.get("url"):
                    configs.append({
                        "label": str(entry.get("label", f"gw{len(configs)}")),
                        "url": str(entry["url"]).rstrip("/"),
                    })
    except Exception:
        pass

    # Env var overrides / additions
    env_local = os.getenv("AGENT_GATEWAY_LOCAL_URL", "").strip()
    env_remote = os.getenv("AGENT_GATEWAY_REMOTE_URL", "").strip()

    if env_local:
        if configs:
            configs[0]["url"] = env_local
        else:
            configs.append({"label": "local", "url": env_local})

    if env_remote:
        if len(configs) > 1:
            configs[1]["url"] = env_remote
        else:
            configs.append({"label": "remote", "url": env_remote})

    return configs


# ---------------------------------------------------------------------------
# Public API for hermes-webui integration
# ---------------------------------------------------------------------------

def get_gateway_model_groups() -> List[Dict[str, Any]]:
    """Return model groups for the hermes-webui dropdown.

    Each group: {"provider": "gateway-local", "models": [{"id": ..., "label": ...}]}
    """
    configs = _load_gateway_configs()
    groups = []

    for gw in configs:
        label = gw["label"]
        url = gw["url"]
        instances = discover_instances(url)

        if not instances:
            continue

        models = []
        for inst in instances:
            model_name = inst.get("model", "unknown")
            keyword = inst.get("keyword", "default")
            cli = inst.get("cli", "cursor").upper()
            mid = build_model_id(label, model_name, keyword)
            display = f"{model_name} [{cli}:{keyword}]"
            models.append({"id": mid, "label": display})

        if models:
            groups.append({
                "provider": f"{GATEWAY_PROVIDER_PREFIX}{label}",
                "models": models,
            })

    return groups


def resolve_gateway_model(model_id: str) -> Optional[Dict[str, str]]:
    """Resolve a gateway model ID to routing parameters.

    Returns dict with: model, provider, base_url, api_key, headers
    Or None if not a gateway model.
    """
    parsed = parse_model_id(model_id)
    if parsed is None:
        return None

    label = parsed["label"]
    model_name = parsed["model_name"]
    keyword = parsed["keyword"]

    # Find the gateway URL for this label
    configs = _load_gateway_configs()
    gateway_url = None
    for gw in configs:
        if gw["label"] == label:
            gateway_url = gw["url"]
            break

    if not gateway_url:
        logger.warning("no gateway config found for label=%s", label)
        return None

    # Look up the instance to determine CLI route
    instances = discover_instances(gateway_url)
    cli_route = "cursor"
    for inst in instances:
        if inst.get("model") == model_name and inst.get("keyword") == keyword:
            cli_route = inst.get("cli", "cursor")
            break

    base_url = f"{gateway_url}/{cli_route}/v1"

    return {
        "model": model_name,
        "provider": "openai",
        "base_url": base_url,
        "api_key": "agent-gateway-no-key-required",
        "extra_headers": {
            "x-instance-keyword": keyword,
        },
    }
