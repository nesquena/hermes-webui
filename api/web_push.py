"""Minimal Web Push support for fully closed WebUI PWAs."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import secrets
import socket
import tempfile
import threading
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import quote, urlparse


logger = logging.getLogger(__name__)
_PUSH_STORE_NAME = "webui_push_subscriptions.json"
_PUSH_OWNER_COOKIE_NAME = "hermes_push_owner"
_PUSH_OWNER_COOKIE_MAX_AGE_SECONDS = 86400 * 365
_STORE_LOCK = threading.Lock()
_WEB_PUSH_TIMEOUT_SECONDS = 10
_LOCAL_PUSH_HOST_ALIASES = {"localhost", "ip6-localhost", "ip6-loopback"}


def _subscription_store_path() -> Path:
    from api.profiles import _DEFAULT_HERMES_HOME

    base = Path(_DEFAULT_HERMES_HOME).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    return base / _PUSH_STORE_NAME


def _load_store() -> dict:
    path = _subscription_store_path()
    if not path.exists():
        return {"subscriptions": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("Failed to read Web Push store %s", path, exc_info=True)
        return {"subscriptions": []}
    subs = data.get("subscriptions")
    if not isinstance(subs, list):
        return {"subscriptions": []}
    normalized = []
    for sub in subs:
        if not isinstance(sub, dict):
            continue
        try:
            normalized.append(
                _normalize_subscription(
                    sub,
                    owner_key=sub.get("owner"),
                    validate_endpoint=False,
                )
            )
        except ValueError:
            logger.debug("Skipping malformed Web Push subscription entry", exc_info=True)
    return {"subscriptions": normalized}


def _save_store(store: dict) -> None:
    path = _subscription_store_path()
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
    if not owner:
        raise ValueError("web push owner is required")
    return owner


def _reject_unsafe_push_endpoint(endpoint: str) -> str:
    endpoint = str(endpoint or "").strip()
    if not endpoint:
        raise ValueError("subscription endpoint is required")
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
    try:
        resolved_ips = socket.getaddrinfo(host, parsed.port or 443)
    except socket.gaierror:
        return endpoint
    for _, _, _, _, addr in resolved_ips:
        try:
            addr_obj = ipaddress.ip_address(addr[0])
        except ValueError:
            continue
        if (
            addr_obj.is_private
            or addr_obj.is_loopback
            or addr_obj.is_link_local
            or addr_obj.is_multicast
            or addr_obj.is_reserved
            or addr_obj.is_unspecified
        ):
            raise ValueError(f"subscription endpoint resolved to a private IP: {addr[0]}")
    return endpoint


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
    keys = (subscription or {}).get("keys")
    if not isinstance(keys, dict):
        raise ValueError("subscription keys are required")
    p256dh = str(keys.get("p256dh") or "").strip()
    auth = str(keys.get("auth") or "").strip()
    if not p256dh or not auth:
        raise ValueError("subscription keys.p256dh and keys.auth are required")
    normalized = {
        "endpoint": endpoint,
        "keys": {"p256dh": p256dh, "auth": auth},
        "owner": _normalize_push_owner(owner_key),
    }
    expiration = (subscription or {}).get("expirationTime")
    if expiration not in (None, ""):
        normalized["expirationTime"] = expiration
    return normalized


def list_subscriptions(*, owner_key: str | None = None) -> list[dict]:
    subscriptions = list(_load_store()["subscriptions"])
    if owner_key is None:
        return subscriptions
    owner = str(owner_key or "").strip()
    if not owner:
        return []
    return [sub for sub in subscriptions if str(sub.get("owner") or "").strip() == owner]


def _mutate_store(mutator) -> tuple[object, bool]:
    with _STORE_LOCK:
        store = _load_store()
        result, changed = mutator(store)
        if changed:
            _save_store(store)
        return result, changed


def upsert_subscription(subscription: dict, *, owner_key: str | None) -> dict:
    normalized = _normalize_subscription(subscription, owner_key=owner_key)

    def _apply(store: dict) -> tuple[dict, bool]:
        subs = [sub for sub in store["subscriptions"] if sub.get("endpoint") != normalized["endpoint"]]
        subs.append(normalized)
        changed = subs != store["subscriptions"]
        store["subscriptions"] = subs
        return normalized, changed

    result, _ = _mutate_store(_apply)
    return result


def remove_subscription(endpoint: str, *, owner_key: str | None = None) -> bool:
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

    result, _ = _mutate_store(_apply)
    return result


def _session_push_owner(session_id: str) -> str | None:
    sid = str(session_id or "").strip()
    if not sid:
        return None
    try:
        from api.models import Session

        session = Session.load_metadata_only(sid)
    except Exception:
        logger.debug("Failed to load Web Push owner for session %s", sid, exc_info=True)
        return None
    owner = str(getattr(session, "push_owner", "") or "").strip()
    return owner or None


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


def send_web_push(payload: dict, *, owner_key: str | None) -> int:
    from api.config import (
        web_push_private_key,
        web_push_subject,
    )

    status = web_push_status()
    if not status["enabled"]:
        return 0
    owner = str(owner_key or "").strip()
    if not owner:
        return 0
    subscriptions = list_subscriptions(owner_key=owner)
    if not subscriptions:
        return 0
    webpush_fn, _ = _get_pywebpush_impl()
    if not webpush_fn:
        return 0
    sent = 0
    stale_endpoints: list[str] = []
    claims = {"sub": web_push_subject()}
    data = json.dumps(payload, ensure_ascii=False)
    for subscription in subscriptions:
        endpoint = str(subscription.get("endpoint") or "").strip()
        try:
            _reject_unsafe_push_endpoint(endpoint)
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
                vapid_claims=claims,
                timeout=_WEB_PUSH_TIMEOUT_SECONDS,
            )
            sent += 1
        except Exception as exc:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None) or getattr(response, "status", None)
            if status_code in (404, 410):
                stale_endpoints.append(endpoint)
            logger.debug("Web Push send failed for %s", endpoint, exc_info=True)
    for endpoint in stale_endpoints:
        remove_subscription(endpoint, owner_key=owner)
    return sent


def notify_bg_task_complete(session_id: str, payload: dict) -> int:
    title = str((payload or {}).get("title") or "Background task complete")
    body = str((payload or {}).get("message") or "Task finished")
    return send_web_push(
        _notification_payload(title, body, session_id=session_id),
        owner_key=_session_push_owner(session_id),
    )


def notify_response_complete(session_id: str, answer: str) -> int:
    text = str(answer or "").strip()
    body = text[:120] if text else "Task finished"
    return send_web_push(
        _notification_payload("Response complete", body, session_id=session_id),
        owner_key=_session_push_owner(session_id),
    )


def notify_approval_required(session_id: str, approval: dict) -> int:
    body = str((approval or {}).get("description") or "Tool approval needed")
    return send_web_push(
        _notification_payload("Approval required", body, session_id=session_id),
        owner_key=_session_push_owner(session_id),
    )


def notify_clarify_required(session_id: str, clarify: dict) -> int:
    body = str((clarify or {}).get("question") or "Tool clarification needed")
    return send_web_push(
        _notification_payload("Clarification needed", body, session_id=session_id),
        owner_key=_session_push_owner(session_id),
    )
