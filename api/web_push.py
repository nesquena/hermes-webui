"""Minimal Web Push support for fully closed WebUI PWAs."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import logging
import math
import multiprocessing
import os
import queue
import re
import secrets
import socket
import tempfile
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import quote, urlparse


logger = logging.getLogger(__name__)
_PUSH_STORE_NAME = "webui_push_subscriptions.json"
_PUSH_OWNER_COOKIE_NAME = "hermes_push_owner"
_PUSH_OWNER_COOKIE_MAX_AGE_SECONDS = 86400 * 365
_STORE_LOCK = threading.Lock()
_WEB_PUSH_TIMEOUT_SECONDS = 10
_WEB_PUSH_DELIVERY_MAX_WORKERS = 4
_WEB_PUSH_DELIVERY_MAX_PENDING = 32
_WEB_PUSH_DELIVERY_WALL_CLOCK_SECONDS = 15
_WEB_PUSH_DELIVERY_SHUTDOWN_WAIT_SECONDS = 5
_LOCAL_PUSH_HOST_ALIASES = {"localhost", "ip6-localhost", "ip6-loopback"}
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")
_PUSH_OWNER_RE = re.compile(r"^[0-9a-f]{64}$")
_WEB_PUSH_ENDPOINT_MAX_LENGTH = 2048
_WEB_PUSH_KEY_MAX_LENGTH = 256
_WEB_PUSH_OWNER_SUBSCRIPTION_LIMIT = 32
_WEB_PUSH_DELIVERY_QUEUE = queue.Queue(
    maxsize=_WEB_PUSH_DELIVERY_MAX_WORKERS + _WEB_PUSH_DELIVERY_MAX_PENDING
)
_WEB_PUSH_DELIVERY_STOP_EVENT = threading.Event()
_WEB_PUSH_DELIVERY_LOCK = threading.Lock()
_WEB_PUSH_DELIVERY_WORKERS: list[threading.Thread] = []
_WEB_PUSH_DELIVERY_ACTIVE_PROCESSES: dict[int, multiprocessing.Process] = {}
_WEB_PUSH_DELIVERY_SHUTDOWN_DEADLINE: float | None = None


class _PushEndpointResolutionError(ValueError):
    pass


class _PushTransportUnavailable(ValueError):
    pass


class _PushStoreUnavailable(RuntimeError):
    pass


def _subscription_store_path(profile_home: Path | None = None) -> Path:
    from api.profiles import _DEFAULT_HERMES_HOME

    base = Path(profile_home or _DEFAULT_HERMES_HOME).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    return base / _PUSH_STORE_NAME


def _load_store(*, profile_home: Path | None = None) -> dict:
    path = _subscription_store_path(profile_home) if profile_home is not None else _subscription_store_path()
    if not path.exists():
        return {"subscriptions": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("Failed to read Web Push store %s", path, exc_info=True)
        raise _PushStoreUnavailable("Web Push subscription store is unavailable") from exc
    subs = data.get("subscriptions")
    if not isinstance(subs, list):
        raise _PushStoreUnavailable("Web Push subscription store is unavailable")
    normalized = []
    for sub in subs:
        if not isinstance(sub, dict):
            raise _PushStoreUnavailable("Web Push subscription store is unavailable")
        try:
            normalized.append(
                _normalize_subscription(
                    sub,
                    owner_key=sub.get("owner"),
                    validate_endpoint=False,
                )
            )
        except ValueError as exc:
            logger.debug("Malformed Web Push subscription entry in %s", path, exc_info=True)
            raise _PushStoreUnavailable("Web Push subscription store is unavailable") from exc
    return {"subscriptions": normalized}


def _save_store(store: dict, *, profile_home: Path | None = None) -> None:
    path = _subscription_store_path(profile_home) if profile_home is not None else _subscription_store_path()
    payload = json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".web_push.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload + "\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _normalize_push_owner(owner_key: str | None) -> str:
    owner = str(owner_key or "").strip()
    if not _PUSH_OWNER_RE.fullmatch(owner):
        raise ValueError("web push owner must be 64 lowercase hex characters")
    return owner


def _push_addr_is_blocked(addr: str) -> bool:
    try:
        addr_obj = ipaddress.ip_address(addr)
    except ValueError:
        return True
    if isinstance(addr_obj, ipaddress.IPv6Address) and addr_obj.ipv4_mapped:
        addr_obj = addr_obj.ipv4_mapped
    if isinstance(addr_obj, ipaddress.IPv4Address) and addr_obj in _CGNAT_NETWORK:
        return True
    return not addr_obj.is_global


def _resolve_safe_push_addresses(hostname: str, port: int | None = None) -> list[str]:
    host = str(hostname or "").strip().lower()
    if not host:
        raise ValueError("subscription endpoint host is required")
    try:
        resolved_ips = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise _PushEndpointResolutionError("subscription endpoint host could not be resolved") from exc
    pinned_hosts = []
    for _, _, _, _, addr in resolved_ips:
        if not addr:
            continue
        pinned_host = str(addr[0])
        if _push_addr_is_blocked(pinned_host):
            raise ValueError(f"subscription endpoint resolved to a private IP: {pinned_host}")
        pinned_hosts.append(pinned_host)
    if not pinned_hosts:
        raise _PushEndpointResolutionError("subscription endpoint host could not be resolved")
    return pinned_hosts


def _parse_push_endpoint(endpoint: str):
    endpoint = str(endpoint or "").strip()
    if not endpoint:
        raise ValueError("subscription endpoint is required")
    if len(endpoint) > _WEB_PUSH_ENDPOINT_MAX_LENGTH:
        raise ValueError("subscription endpoint is too long")
    parsed = urlparse(endpoint)
    if parsed.scheme.lower() != "https":
        raise ValueError("subscription endpoint must use https")
    if parsed.username or parsed.password:
        raise ValueError("subscription endpoint must not include credentials")
    host = str(parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("subscription endpoint host is required")
    if host in _LOCAL_PUSH_HOST_ALIASES or host.endswith(".localhost"):
        raise ValueError("subscription endpoint must not target localhost")
    return endpoint, parsed, host


def _reject_unsafe_push_endpoint(endpoint: str) -> str:
    endpoint, parsed, host = _parse_push_endpoint(endpoint)
    _resolve_safe_push_addresses(host, parsed.port or 443)
    return endpoint


def _create_pinned_push_connection(
    pinned_host: str,
    port: int,
    timeout,
    source_address,
    socket_options,
):
    from urllib3.util import connection

    return connection.create_connection(
        (pinned_host, port),
        timeout,
        source_address=source_address,
        socket_options=socket_options,
    )


def _web_push_requests_session(endpoint: str):
    _, parsed, host = _parse_push_endpoint(endpoint)
    pinned_hosts = _resolve_safe_push_addresses(host, parsed.port or 443)
    try:
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3 import HTTPSConnectionPool
        from urllib3.connection import HTTPSConnection
    except ImportError as exc:
        raise _PushTransportUnavailable("Web Push requests transport is unavailable") from exc

    class _PinnedWebPushHTTPSConnection(HTTPSConnection):
        def _new_conn(self):
            last_error = None
            for pinned_host in pinned_hosts:
                try:
                    return _create_pinned_push_connection(
                        pinned_host,
                        self.port,
                        self.timeout,
                        self.source_address,
                        self.socket_options,
                    )
                except OSError as exc:
                    last_error = exc
            if last_error is not None:
                raise last_error
            raise OSError("could not connect to any pinned Web Push target")

    class _PinnedWebPushHTTPSConnectionPool(HTTPSConnectionPool):
        ConnectionCls = _PinnedWebPushHTTPSConnection

    class _PinnedWebPushHTTPAdapter(HTTPAdapter):
        def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
            super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)
            self.poolmanager.pool_classes_by_scheme = dict(self.poolmanager.pool_classes_by_scheme)
            self.poolmanager.pool_classes_by_scheme["https"] = _PinnedWebPushHTTPSConnectionPool

        def send(self, request, **kwargs):
            if kwargs.get("proxies"):
                raise ValueError("Web Push delivery does not allow proxies")
            return super().send(request, **kwargs)

        def proxy_manager_for(self, *args, **kwargs):
            raise ValueError("Web Push delivery does not allow proxies")

    class _BlockedSchemeAdapter(HTTPAdapter):
        def send(self, request, **kwargs):
            raise ValueError("Web Push delivery requires https")

    class _NoRedirectPinnedSession(requests.Session):
        def rebuild_proxies(self, prepared_request, proxies):
            return {}

        def request(self, method, url, **kwargs):
            kwargs["allow_redirects"] = False
            response = super().request(method, url, **kwargs)
            if 300 <= int(getattr(response, "status_code", 0) or 0) < 400:
                raise ValueError("Web Push delivery does not allow redirects")
            return response

    session = _NoRedirectPinnedSession()
    session.trust_env = False
    session.mount("https://", _PinnedWebPushHTTPAdapter())
    session.mount("http://", _BlockedSchemeAdapter())
    return session


def _parse_cookie_value(handler, cookie_name: str) -> str | None:
    headers = getattr(handler, "headers", None)
    cookie_header = headers.get("Cookie", "") if headers else ""
    if not cookie_header:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:
        return None
    morsel = cookie.get(cookie_name)
    if not morsel:
        return None
    value = str(morsel.value or "").strip()
    return value or None


def get_push_owner(handler) -> str | None:
    owner = _parse_cookie_value(handler, _PUSH_OWNER_COOKIE_NAME)
    if not owner:
        return None
    try:
        return _normalize_push_owner(owner)
    except ValueError:
        return None


def ensure_push_owner_cookie(handler) -> tuple[str, str | None]:
    owner = get_push_owner(handler)
    if owner:
        return owner, None
    owner = secrets.token_hex(32)
    cookie = SimpleCookie()
    cookie[_PUSH_OWNER_COOKIE_NAME] = owner
    cookie[_PUSH_OWNER_COOKIE_NAME]["httponly"] = True
    cookie[_PUSH_OWNER_COOKIE_NAME]["max-age"] = str(_PUSH_OWNER_COOKIE_MAX_AGE_SECONDS)
    cookie[_PUSH_OWNER_COOKIE_NAME]["samesite"] = "Lax"
    cookie[_PUSH_OWNER_COOKIE_NAME]["path"] = "/"
    try:
        from api.auth import _is_secure_context

        if _is_secure_context(handler):
            cookie[_PUSH_OWNER_COOKIE_NAME]["secure"] = True
    except Exception:
        logger.debug("Failed to resolve secure context for push-owner cookie", exc_info=True)
    return owner, cookie[_PUSH_OWNER_COOKIE_NAME].OutputString()


def _decode_subscription_key(value: str | None, field_name: str) -> bytes:
    raw_value = str(value or "").strip()
    if not raw_value:
        raise ValueError(f"subscription keys.{field_name} is required")
    if len(raw_value) > _WEB_PUSH_KEY_MAX_LENGTH:
        raise ValueError(f"subscription keys.{field_name} is too long")
    padded = raw_value + ("=" * (-len(raw_value) % 4))
    try:
        return base64.b64decode(padded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"subscription keys.{field_name} must be valid base64url") from exc


def _public_subscription(subscription: dict) -> dict:
    public = {
        "endpoint": str(subscription.get("endpoint") or ""),
        "keys": dict(subscription.get("keys") or {}),
    }
    if "expirationTime" in subscription:
        public["expirationTime"] = subscription["expirationTime"]
    return public


def _normalize_subscription(
    subscription: dict,
    *,
    owner_key: str | None,
    validate_endpoint: bool = True,
) -> dict:
    endpoint = str((subscription or {}).get("endpoint") or "").strip()
    if validate_endpoint:
        endpoint = _reject_unsafe_push_endpoint(endpoint)
    elif not endpoint:
        raise ValueError("subscription endpoint is required")
    elif len(endpoint) > _WEB_PUSH_ENDPOINT_MAX_LENGTH:
        raise ValueError("subscription endpoint is too long")
    keys = (subscription or {}).get("keys")
    if not isinstance(keys, dict):
        raise ValueError("subscription keys are required")
    p256dh = str(keys.get("p256dh") or "").strip()
    auth = str(keys.get("auth") or "").strip()
    p256dh_bytes = _decode_subscription_key(p256dh, "p256dh")
    auth_bytes = _decode_subscription_key(auth, "auth")
    if len(p256dh_bytes) != 65 or p256dh_bytes[0] != 0x04:
        raise ValueError("subscription keys.p256dh must be a 65-byte uncompressed public key")
    if len(auth_bytes) != 16:
        raise ValueError("subscription keys.auth must be a 16-byte auth secret")
    normalized = {
        "endpoint": endpoint,
        "keys": {"p256dh": p256dh, "auth": auth},
        "owner": _normalize_push_owner(owner_key),
    }
    expiration = (subscription or {}).get("expirationTime")
    if expiration is not None:
        if (
            expiration == ""
            or isinstance(expiration, bool)
            or not isinstance(expiration, (int, float))
            or not math.isfinite(expiration)
        ):
            raise ValueError("subscription expirationTime must be numeric or null")
        normalized["expirationTime"] = expiration
    return normalized


def list_subscriptions(*, owner_key: str | None = None, profile_home: Path | None = None) -> list[dict]:
    subscriptions = list(_load_store(profile_home=profile_home)["subscriptions"])
    if owner_key is None:
        return subscriptions
    owner = str(owner_key or "").strip()
    if not owner:
        return []
    return [sub for sub in subscriptions if str(sub.get("owner") or "").strip() == owner]


def _mutate_store(mutator, *, profile_home: Path | None = None) -> tuple[object, bool]:
    with _STORE_LOCK:
        store = _load_store(profile_home=profile_home)
        result, changed = mutator(store)
        if changed:
            _save_store(store, profile_home=profile_home)
    return result, changed


def upsert_subscription(
    subscription: dict, *, owner_key: str | None, profile_home: Path | None = None,
) -> dict:
    normalized = _normalize_subscription(subscription, owner_key=owner_key)

    def _apply(store: dict) -> tuple[dict, bool]:
        owner_count = sum(
            1
            for sub in store["subscriptions"]
            if (
                str(sub.get("owner") or "").strip() == normalized["owner"]
                and sub.get("endpoint") != normalized["endpoint"]
            )
        )
        if owner_count >= _WEB_PUSH_OWNER_SUBSCRIPTION_LIMIT:
            raise ValueError("too many Web Push subscriptions for this owner")
        subs = [sub for sub in store["subscriptions"] if sub.get("endpoint") != normalized["endpoint"]]
        subs.append(normalized)
        changed = subs != store["subscriptions"]
        store["subscriptions"] = subs
        return normalized, changed

    result, _ = _mutate_store(_apply, profile_home=profile_home)
    return result


def remove_subscription(
    endpoint: str, *, owner_key: str | None = None, profile_home: Path | None = None,
) -> bool:
    endpoint = str(endpoint or "").strip()
    if not endpoint:
        return False
    owner = str(owner_key or "").strip()
    if not owner:
        return False

    def _apply(store: dict) -> tuple[bool, bool]:
        before = len(store["subscriptions"])
        store["subscriptions"] = [
            sub
            for sub in store["subscriptions"]
            if not (
                sub.get("endpoint") == endpoint
                and str(sub.get("owner") or "").strip() == owner
            )
        ]
        changed = len(store["subscriptions"]) != before
        return changed, changed

    result, _ = _mutate_store(_apply, profile_home=profile_home)
    return result


def _upsert_subscription_replacing_previous(
    normalized: dict, *, previous_endpoint: str | None = None, profile_home: Path | None = None,
) -> dict:
    previous = str(previous_endpoint or "").strip()

    def _apply(store: dict) -> tuple[dict, bool]:
        owner = normalized["owner"]
        owner_count = sum(
            1
            for sub in store["subscriptions"]
            if (
                str(sub.get("owner") or "").strip() == owner
                and sub.get("endpoint") not in {normalized["endpoint"], previous}
            )
        )
        if owner_count >= _WEB_PUSH_OWNER_SUBSCRIPTION_LIMIT:
            raise ValueError("too many Web Push subscriptions for this owner")
        subs = [
            sub
            for sub in store["subscriptions"]
            if not (
                str(sub.get("owner") or "").strip() == owner
                and sub.get("endpoint") in {normalized["endpoint"], previous}
            )
        ]
        subs.append(normalized)
        changed = subs != store["subscriptions"]
        store["subscriptions"] = subs
        return normalized, changed

    result, _ = _mutate_store(_apply, profile_home=profile_home)
    return result


def upsert_subscription_for_owner_profiles(
    subscription: dict,
    *,
    owner_key: str | None,
    previous_endpoint: str | None = None,
    profile_home: Path | None = None,
) -> dict:
    normalized = _normalize_subscription(subscription, owner_key=owner_key)
    previous = str(previous_endpoint or "").strip()
    if not previous:
        return _upsert_subscription_replacing_previous(normalized, profile_home=profile_home)

    matched_home = False
    for home in _iter_push_store_homes():
        if any(sub.get("endpoint") == previous for sub in list_subscriptions(owner_key=normalized["owner"], profile_home=home)):
            _upsert_subscription_replacing_previous(
                normalized,
                previous_endpoint=previous,
                profile_home=home,
            )
            matched_home = True
    if not matched_home:
        return _upsert_subscription_replacing_previous(
            normalized,
            previous_endpoint=previous,
            profile_home=profile_home,
        )
    return normalized


def remove_subscription_for_owner_profiles(endpoint: str, *, owner_key: str | None = None) -> bool:
    removed = False
    for home in _iter_push_store_homes():
        removed = remove_subscription(endpoint, owner_key=owner_key, profile_home=home) or removed
    return removed


def _session_push_target(session_id: str) -> tuple[str, Path] | None:
    sid = str(session_id or "").strip()
    if not sid:
        return None
    try:
        from api.models import Session
        from api.profiles import get_hermes_home_for_profile

        session = Session.load_metadata_only(sid)
    except Exception:
        logger.debug("Failed to load Web Push owner for session %s", sid, exc_info=True)
        return None
    owner = str(getattr(session, "push_owner", "") or "").strip()
    if not owner:
        return None
    return owner, get_hermes_home_for_profile(str(getattr(session, "profile", "") or ""))


def _iter_push_store_homes():
    from api.profiles import (
        _DEFAULT_HERMES_HOME,
        _INITIAL_HERMES_HOME,
        _is_isolated_profile_mode,
    )

    if _is_isolated_profile_mode():
        yield Path(_INITIAL_HERMES_HOME).expanduser()
        return

    base = Path(_DEFAULT_HERMES_HOME).expanduser()
    seen = set()
    candidates = [base]
    profiles_dir = base / "profiles"
    if profiles_dir.exists():
        for child in sorted(profiles_dir.iterdir(), key=lambda item: item.name):
            if child.is_dir():
                candidates.append(child)
    for candidate in candidates:
        try:
            key = str(candidate.resolve())
        except OSError:
            key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        yield candidate


def web_push_client_status(
    owner_key: str | None,
    *,
    endpoint: str | None = None,
    profile_home: Path | None = None,
) -> dict:
    endpoint = str(endpoint or "").strip()
    try:
        owner = _normalize_push_owner(owner_key)
    except ValueError:
        return {
            "active_profile_subscribed": False,
            "any_profile_subscribed": False,
        }
    current_subscriptions = list_subscriptions(owner_key=owner, profile_home=profile_home)
    if endpoint:
        active_profile_subscribed = any(sub.get("endpoint") == endpoint for sub in current_subscriptions)
        any_profile_subscribed = False
        for home in _iter_push_store_homes():
            if any(sub.get("endpoint") == endpoint for sub in list_subscriptions(owner_key=owner, profile_home=home)):
                any_profile_subscribed = True
                break
    else:
        active_profile_subscribed = bool(current_subscriptions)
        any_profile_subscribed = False
        for home in _iter_push_store_homes():
            if list_subscriptions(owner_key=owner, profile_home=home):
                any_profile_subscribed = True
                break
    return {
        "active_profile_subscribed": active_profile_subscribed,
        "any_profile_subscribed": any_profile_subscribed,
    }


def _get_pywebpush_impl():
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        return None, None
    return webpush, WebPushException


def web_push_status() -> dict:
    from api.config import web_push_configured

    webpush_fn, _ = _get_pywebpush_impl()
    configured = web_push_configured()
    dependency_available = webpush_fn is not None
    return {
        "configured": configured,
        "dependency_available": dependency_available,
        "enabled": bool(configured and dependency_available),
    }


def _notification_payload(title: str, body: str, *, session_id: str | None = None) -> dict:
    url = f"session/{quote(str(session_id or '').strip(), safe='')}" if session_id else "./"
    return {
        "title": str(title or "Hermes"),
        "options": {
            "body": str(body or ""),
            "tag": f"hermes-{session_id}" if session_id else "hermes-webui",
            "renotify": False,
            "icon": "static/favicon-192.png",
            "badge": "static/favicon-32.png",
            "data": {"url": url},
        },
    }


def send_web_push(
    payload: dict, *, owner_key: str | None, profile_home: Path | None = None,
) -> int:
    sent, stale_endpoints = _send_web_push_attempt(
        payload,
        owner_key=owner_key,
        profile_home=profile_home,
    )
    _prune_stale_endpoints(
        stale_endpoints,
        owner_key=owner_key,
        profile_home=profile_home,
    )
    return sent


def _send_web_push_attempt(
    payload: dict, *, owner_key: str | None, profile_home: Path | None = None,
) -> tuple[int, list[str]]:
    from api.config import (
        web_push_private_key,
        web_push_subject,
    )

    status = web_push_status()
    if not status["enabled"]:
        return 0, []
    owner = str(owner_key or "").strip()
    if not owner:
        return 0, []
    subscriptions = list_subscriptions(owner_key=owner, profile_home=profile_home)
    if not subscriptions:
        return 0, []
    webpush_fn, _ = _get_pywebpush_impl()
    if not webpush_fn:
        return 0, []
    sent = 0
    stale_endpoints: list[str] = []
    data = json.dumps(payload, ensure_ascii=False)
    for subscription in subscriptions:
        if _WEB_PUSH_DELIVERY_STOP_EVENT.is_set():
            break
        endpoint = str(subscription.get("endpoint") or "").strip()
        try:
            requests_session = _web_push_requests_session(endpoint)
        except (_PushEndpointResolutionError, _PushTransportUnavailable):
            logger.debug("Skipping temporarily unresolved Web Push endpoint %s", endpoint, exc_info=True)
            continue
        except ValueError:
            if endpoint:
                stale_endpoints.append(endpoint)
            logger.debug("Skipping unsafe Web Push endpoint %s", endpoint, exc_info=True)
            continue
        try:
            webpush_fn(
                subscription_info=subscription,
                data=data,
                vapid_private_key=web_push_private_key(),
                vapid_claims={"sub": web_push_subject()},
                requests_session=requests_session,
                timeout=_WEB_PUSH_TIMEOUT_SECONDS,
            )
            sent += 1
        except Exception as exc:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None) or getattr(response, "status", None)
            if status_code in (404, 410):
                stale_endpoints.append(endpoint)
            logger.debug("Web Push send failed for %s", endpoint, exc_info=True)
    return sent, stale_endpoints


def _prune_stale_endpoints(
    stale_endpoints: list[str],
    *,
    owner_key: str | None,
    profile_home: Path | None = None,
) -> None:
    owner = str(owner_key or "").strip()
    if not owner:
        return
    for endpoint in stale_endpoints:
        remove_subscription(endpoint, owner_key=owner, profile_home=profile_home)


def _run_enqueued_web_push(
    payload: dict, *, owner_key: str, profile_home: Path | None = None,
) -> tuple[int, list[str]]:
    try:
        if _WEB_PUSH_DELIVERY_STOP_EVENT.is_set():
            return 0, []
        return _send_web_push_attempt(
            payload,
            owner_key=owner_key,
            profile_home=profile_home,
        )
    except Exception:
        logger.debug("Web Push background delivery failed", exc_info=True)
        return 0, []


def _web_push_delivery_job(
    payload: dict,
    *,
    session_id: str | None = None,
    owner_key: str | None = None,
    profile_home: Path | None = None,
) -> dict | None:
    payload_copy = dict(payload or {})
    sid = str(session_id or "").strip()
    owner = str(owner_key or "").strip()
    if sid:
        return {"payload": payload_copy, "session_id": sid}
    if owner:
        return {"payload": payload_copy, "owner_key": owner, "profile_home": profile_home}
    return None


def _deliver_web_push_job_result(job: dict) -> dict:
    payload = dict((job or {}).get("payload") or {})
    owner = str((job or {}).get("owner_key") or "").strip()
    if owner:
        profile_home = (job or {}).get("profile_home")
        expanded_profile_home = Path(profile_home).expanduser() if profile_home else None
        sent, stale_endpoints = _run_enqueued_web_push(
            payload,
            owner_key=owner,
            profile_home=expanded_profile_home,
        )
        return {
            "owner_key": owner,
            "profile_home": str(expanded_profile_home) if expanded_profile_home else None,
            "sent": sent,
            "stale_endpoints": stale_endpoints,
        }
    session_id = str((job or {}).get("session_id") or "").strip()
    if not session_id:
        return {"owner_key": "", "profile_home": None, "sent": 0, "stale_endpoints": []}
    target = _session_push_target(session_id)
    if not target:
        return {"owner_key": "", "profile_home": None, "sent": 0, "stale_endpoints": []}
    sent, stale_endpoints = _run_enqueued_web_push(
        payload,
        owner_key=target[0],
        profile_home=target[1],
    )
    return {
        "owner_key": target[0],
        "profile_home": str(target[1]) if target[1] else None,
        "sent": sent,
        "stale_endpoints": stale_endpoints,
    }


def _deliver_web_push_job(job: dict) -> int:
    return int(_deliver_web_push_job_result(job).get("sent") or 0)


def _web_push_delivery_process_main(job: dict, result_queue) -> None:
    try:
        result_queue.put(_deliver_web_push_job_result(job))
    except Exception:
        logger.debug("Web Push delivery subprocess failed", exc_info=True)


def _effective_web_push_delivery_timeout() -> float:
    timeout = _WEB_PUSH_DELIVERY_WALL_CLOCK_SECONDS
    deadline = _WEB_PUSH_DELIVERY_SHUTDOWN_DEADLINE
    if _WEB_PUSH_DELIVERY_STOP_EVENT.is_set() and deadline is not None:
        timeout = min(timeout, max(0.0, deadline - time.monotonic()))
    return max(0.0, timeout)


def _start_web_push_delivery_process(job: dict) -> multiprocessing.Process:
    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue(maxsize=1)
    process = ctx.Process(
        target=_web_push_delivery_process_main,
        args=(job, result_queue),
        daemon=True,
        name="web-push-delivery",
    )
    process._web_push_result_queue = result_queue
    process.start()
    return process


def _pop_web_push_delivery_result(process: multiprocessing.Process) -> dict | None:
    result_queue = getattr(process, "_web_push_result_queue", None)
    if result_queue is None:
        return None
    try:
        return result_queue.get_nowait()
    except queue.Empty:
        return None
    except Exception:
        logger.debug("Failed to read Web Push delivery result", exc_info=True)
        return None
    finally:
        try:
            result_queue.close()
        except Exception:
            pass
        try:
            result_queue.join_thread()
        except Exception:
            pass


def _run_web_push_delivery_job(job: dict) -> None:
    process: multiprocessing.Process | None = None
    result: dict | None = None
    try:
        process = _start_web_push_delivery_process(job)
        with _WEB_PUSH_DELIVERY_LOCK:
            _WEB_PUSH_DELIVERY_ACTIVE_PROCESSES[process.pid] = process
        timeout = _effective_web_push_delivery_timeout()
        process.join(timeout)
        if process.is_alive():
            process.terminate()
            process.join(timeout=1.0)
            logger.debug(
                "Web Push delivery terminated after %.1fs for session %s",
                timeout,
                (job or {}).get("session_id"),
            )
        else:
            result = _pop_web_push_delivery_result(process)
    except Exception:
        logger.debug("Failed to run Web Push delivery job", exc_info=True)
        if process and process.is_alive():
            try:
                process.terminate()
                process.join(timeout=1.0)
            except Exception:
                logger.debug("Failed to terminate Web Push delivery process", exc_info=True)
    finally:
        if process is not None:
            with _WEB_PUSH_DELIVERY_LOCK:
                _WEB_PUSH_DELIVERY_ACTIVE_PROCESSES.pop(process.pid, None)
    if result:
        profile_home = str(result.get("profile_home") or "").strip()
        _prune_stale_endpoints(
            list(result.get("stale_endpoints") or []),
            owner_key=str(result.get("owner_key") or ""),
            profile_home=Path(profile_home).expanduser() if profile_home else None,
        )


def _web_push_delivery_worker() -> None:
    while True:
        try:
            job = _WEB_PUSH_DELIVERY_QUEUE.get(timeout=0.5)
        except queue.Empty:
            if _WEB_PUSH_DELIVERY_STOP_EVENT.is_set():
                return
            continue
        if job is None:
            return
        if _WEB_PUSH_DELIVERY_STOP_EVENT.is_set():
            continue
        _run_web_push_delivery_job(job)


def _ensure_web_push_delivery_workers() -> None:
    with _WEB_PUSH_DELIVERY_LOCK:
        if _WEB_PUSH_DELIVERY_WORKERS or _WEB_PUSH_DELIVERY_STOP_EVENT.is_set():
            return
        for idx in range(_WEB_PUSH_DELIVERY_MAX_WORKERS):
            worker = threading.Thread(
                target=_web_push_delivery_worker,
                name=f"web-push-{idx + 1}",
                daemon=True,
            )
            worker.start()
            _WEB_PUSH_DELIVERY_WORKERS.append(worker)


def _enqueue_web_push(
    payload: dict,
    *,
    session_id: str | None = None,
    owner_key: str | None = None,
    profile_home: Path | None = None,
) -> int:
    if _WEB_PUSH_DELIVERY_STOP_EVENT.is_set():
        return 0
    job = _web_push_delivery_job(
        payload,
        session_id=session_id,
        owner_key=owner_key,
        profile_home=profile_home,
    )
    if not job:
        return 0
    _ensure_web_push_delivery_workers()
    try:
        _WEB_PUSH_DELIVERY_QUEUE.put_nowait(job)
    except queue.Full:
        logger.debug("Skipping Web Push delivery because the background queue is full")
        return 0
    return 1


def shutdown_web_push_delivery() -> None:
    _WEB_PUSH_DELIVERY_STOP_EVENT.set()
    deadline = time.monotonic() + _WEB_PUSH_DELIVERY_SHUTDOWN_WAIT_SECONDS
    with _WEB_PUSH_DELIVERY_LOCK:
        global _WEB_PUSH_DELIVERY_SHUTDOWN_DEADLINE
        _WEB_PUSH_DELIVERY_SHUTDOWN_DEADLINE = deadline
        workers = list(_WEB_PUSH_DELIVERY_WORKERS)
    try:
        while True:
            _WEB_PUSH_DELIVERY_QUEUE.get_nowait()
    except queue.Empty:
        pass
    for _ in workers:
        try:
            _WEB_PUSH_DELIVERY_QUEUE.put_nowait(None)
        except queue.Full:
            break
    for worker in workers:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        worker.join(timeout=remaining)
    with _WEB_PUSH_DELIVERY_LOCK:
        active_processes = list(_WEB_PUSH_DELIVERY_ACTIVE_PROCESSES.values())
    for process in active_processes:
        if process.is_alive():
            try:
                process.terminate()
            except Exception:
                logger.debug("Failed to terminate Web Push delivery subprocess", exc_info=True)
    for process in active_processes:
        try:
            process.join(timeout=1.0)
        except Exception:
            logger.debug("Failed to join Web Push delivery subprocess", exc_info=True)


def notify_bg_task_complete(session_id: str, payload: dict) -> int:
    title = str((payload or {}).get("title") or "Background task complete")
    body = str((payload or {}).get("message") or "Task finished")
    return _enqueue_web_push(
        _notification_payload(title, body, session_id=session_id),
        session_id=session_id,
    )


def notify_response_complete(session_id: str, answer: str) -> int:
    text = str(answer or "").strip()
    body = text[:120] if text else "Task finished"
    return _enqueue_web_push(
        _notification_payload("Response complete", body, session_id=session_id),
        session_id=session_id,
    )


def notify_approval_required(session_id: str, approval: dict) -> int:
    body = str((approval or {}).get("description") or "Tool approval needed")
    return _enqueue_web_push(
        _notification_payload("Approval required", body, session_id=session_id),
        session_id=session_id,
    )


def notify_clarify_required(session_id: str, clarify: dict) -> int:
    body = str((clarify or {}).get("question") or "Tool clarification needed")
    return _enqueue_web_push(
        _notification_payload("Clarification needed", body, session_id=session_id),
        session_id=session_id,
    )
