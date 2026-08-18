"""Profile-bound configured-provider discovery primitives.

This module deliberately has no import of :mod:`api.config`.  Callers provide
the small config/environment seams they already own, which keeps the raw
snapshot and the credentialed transport independently testable.
"""

from __future__ import annotations

import dataclasses
import http.client
import ipaddress
import json
import logging
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

logger = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT = 5.0


@dataclasses.dataclass(frozen=True, slots=True)
class ProviderCredential:
    """Redacted credential resolution result."""

    state: str
    source: str | None = None
    value: str | None = dataclasses.field(default=None, repr=False, compare=False)

    def __bool__(self) -> bool:
        return self.state == "resolved" and bool(self.value)


@dataclasses.dataclass(frozen=True, slots=True)
class ProviderConnection:
    """The complete authority tuple for one configured provider probe."""

    profile: str
    provider_id: str
    raw_config_key: str
    base_url: str
    discovery_policy: str
    model_options: tuple[dict[str, Any], ...]
    credential: ProviderCredential
    vetted_addresses: tuple[str, ...] = ()


class ProviderDiscoveryError(Exception):
    """An intentionally public-safe discovery failure."""

    def __init__(self, kind: str, status: int | None = None):
        super().__init__(kind)
        self.kind = kind
        self.status = status


def resolve_credential(
    raw: dict[str, Any],
    *,
    provider_hint: str = "",
    env_value: Callable[[str], str] | None = None,
    fallback_value: Callable[[str], str | None] | None = None,
) -> ProviderCredential:
    """Resolve declared sources without collapsing absent and unavailable."""
    env_value = env_value or (lambda name: __import__("os").getenv(name, ""))
    has_api_key = "api_key" in raw
    has_key_env = "key_env" in raw
    key_env_result: ProviderCredential | None = None
    if has_key_env:
        name = str(raw.get("key_env") or "").strip()
        if name:
            key_env_result = _from_env(name, "key_env", env_value)
            if key_env_result.state == "resolved":
                return key_env_result
        else:
            key_env_result = ProviderCredential("declared_unavailable", "key_env")
    if has_api_key:
        value = raw.get("api_key")
        value_text = str(value or "").strip()
        if value_text:
            if value_text.startswith("${") and value_text.endswith("}"):
                name = value_text[2:-1].strip()
                resolved = _from_env(name, "api_key:${...}", env_value)
                if resolved.state == "resolved":
                    return resolved
            elif value_text.startswith("${"):
                if not has_key_env:
                    return ProviderCredential("declared_unavailable", "api_key:${...}")
            else:
                return ProviderCredential("resolved", "api_key", value_text)
        if key_env_result is not None:
            return key_env_result
        return ProviderCredential("declared_unavailable", "api_key")
    if key_env_result is not None:
        return key_env_result
    if (provider_hint == "custom" or provider_hint.startswith("custom:")) and fallback_value is not None:
        value = fallback_value(provider_hint)
        if value and str(value).strip():
            return ProviderCredential("resolved", "derived_custom_env", str(value).strip())
    return ProviderCredential("absent")


def _from_env(name: str, source: str, env_value: Callable[[str], str]) -> ProviderCredential:
    if not name:
        return ProviderCredential("declared_unavailable", source)
    value = str(env_value(name) or "").strip()
    return ProviderCredential("resolved", source, value) if value else ProviderCredential("declared_unavailable", source)


def build_connection(
    *,
    profile: str,
    provider_id: str,
    raw_config_key: str,
    raw: dict[str, Any],
    env_value: Callable[[str], str] | None = None,
    fallback_value: Callable[[str], str | None] | None = None,
) -> ProviderConnection:
    options = raw.get("models")
    if isinstance(options, dict):
        model_options = tuple(
            {"id": str(key).strip(), "label": str(key).strip()}
            for key in options
            if isinstance(key, str) and key.strip()
        )
    elif isinstance(options, list):
        rows = []
        for item in options:
            if isinstance(item, dict):
                model_id = str(item.get("id") or item.get("model") or item.get("name") or "").strip()
                label = str(item.get("label") or model_id).strip() or model_id
            else:
                model_id = str(item or "").strip()
                label = model_id
            if model_id and not any(row["id"] == model_id for row in rows):
                rows.append({"id": model_id, "label": label})
        model_options = tuple(rows)
    elif isinstance(options, str) and options.strip():
        model_options = ({"id": options.strip(), "label": options.strip()},)
    else:
        model_options = ()
    policy = "disabled" if raw.get("discover_models") is False else "allow"
    return ProviderConnection(
        profile=str(profile or ""),
        provider_id=str(provider_id or "").strip().lower(),
        raw_config_key=str(raw_config_key),
        base_url=str(raw.get("base_url") or "").strip().rstrip("/"),
        discovery_policy=policy,
        model_options=model_options,
        credential=resolve_credential(raw, provider_hint=provider_id, env_value=env_value, fallback_value=fallback_value),
    )


def capture_raw_profile_snapshot(config_module: Any) -> dict[str, Any]:
    """Load raw YAML and expand it only after the caller's profile scope exists."""
    path = config_module._get_config_path()
    raw = config_module._load_yaml_config_file_raw(path)
    if config_module._cfg_has_in_memory_overrides():
        raw = config_module.cfg
    snapshot = config_module.copy.deepcopy(raw) if isinstance(raw, dict) else {}
    config_module._apply_config_defaults(snapshot)
    return snapshot


def _resolve_addresses(host: str, port: int, resolver: Callable[..., Any] | None) -> tuple[str, ...]:
    resolver = resolver or socket.getaddrinfo
    infos = resolver(host, port, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for info in infos:
        value = info[4][0]
        if not ipaddress.ip_address(value).is_global:
            raise ProviderDiscoveryError("blocked_destination")
        if value not in addresses:
            addresses.append(value)
    if not addresses:
        raise ProviderDiscoveryError("unresolved_destination")
    return tuple(addresses)


def prepare_connection(connection: ProviderConnection, *, resolver: Callable[..., Any] | None = None) -> ProviderConnection:
    try:
        parsed = urllib.parse.urlsplit(connection.base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ProviderDiscoveryError("invalid_endpoint")
        addresses = _resolve_addresses(parsed.hostname, parsed.port or 443, resolver)
    except ProviderDiscoveryError:
        raise
    except Exception:
        raise ProviderDiscoveryError("network") from None
    return dataclasses.replace(connection, vetted_addresses=addresses)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, *, port: int, timeout: float, context: ssl.SSLContext):
        super().__init__(host, port=port, timeout=timeout, context=context)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, connection: ProviderConnection, timeout: float):
        super().__init__(context=ssl.create_default_context())
        self.connection = connection
        self.timeout = timeout

    def https_open(self, req):
        parsed = urllib.parse.urlsplit(req.full_url)
        last_error = None
        for address in self.connection.vetted_addresses:
            try:
                return self.do_open(
                    lambda host, address=address, **kwargs: _PinnedHTTPSConnection(
                        host, address, port=parsed.port or 443, timeout=self.timeout, context=self._context
                    ),
                    req,
                )
            except (OSError, urllib.error.URLError) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise urllib.error.URLError("no vetted destination")


def fetch_models(
    connection: ProviderConnection,
    *,
    resolver: Callable[..., Any] | None = None,
    opener: Callable[..., Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> list[dict[str, str]]:
    """Fetch a catalog after exactly one DNS resolution and with no redirects."""
    prepared = connection if connection.vetted_addresses else prepare_connection(connection, resolver=resolver)
    endpoint = prepared.base_url.rstrip("/")
    endpoint = endpoint + ("/models" if endpoint.endswith("/v1") else "/v1/models")
    request = urllib.request.Request(endpoint, method="GET")
    request.add_header("User-Agent", "OpenAI/Python 1.0")
    if prepared.credential.value:
        request.add_header("Authorization", f"Bearer {prepared.credential.value}")
    try:
        if opener is None:
            opener_obj = urllib.request.build_opener(_PinnedHTTPSHandler(prepared, timeout), urllib.request.ProxyHandler({}), _NoRedirect())
            response = opener_obj.open(request, timeout=timeout)
        else:
            response = opener(request, timeout=timeout)
        with response:
            status = getattr(response, "status", None)
            if status is not None and status != 200:
                kind = "auth" if status in (401, 403) else "http"
                raise ProviderDiscoveryError(kind, int(status))
            payload = response.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ProviderDiscoveryError("response_too_large")
        decoded = json.loads(payload.decode("utf-8"))
        if isinstance(decoded, dict):
            rows = decoded.get("data")
            if not isinstance(rows, list):
                rows = decoded.get("models")
        else:
            rows = decoded
        if not isinstance(rows, list):
            raise ProviderDiscoveryError("invalid_payload")
        result = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            model_id = row.get("id") or row.get("name") or row.get("model")
            if not model_id:
                continue
            model_id = str(model_id)
            result.append(
                {
                    "id": model_id,
                    "label": str(row.get("name") or row.get("model") or model_id),
                }
            )
        return result
    except ProviderDiscoveryError:
        raise
    except urllib.error.HTTPError as exc:
        code = getattr(exc, "code", None)
        raise ProviderDiscoveryError("auth" if code in (401, 403) else "http", code) from None
    except Exception:
        raise ProviderDiscoveryError("network") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderDiscoveryError("redirect")
