"""Opt-in Web Push (closed-PWA notifications, iOS/iPadOS 16.4+ compatible).

Design notes
------------
* Opt-in and silent: without ``pywebpush`` or without VAPID keys every public
  function is a no-op and the endpoints report ``enabled: false``.
* VAPID keys come from ``HERMES_WEBUI_VAPID_PUBLIC_KEY`` /
  ``HERMES_WEBUI_VAPID_PRIVATE_KEY`` / ``HERMES_WEBUI_VAPID_SUBJECT`` or, if
  unset, from ``<STATE_DIR>/webui_vapid.json`` (mode 0600, created by
  ``scripts/generate_vapid_keys.py``).  The private key is never returned by any
  API and never logged.
* One WebUI instance is one trust domain (one password), so subscriptions are
  stored per instance in ``<STATE_DIR>/webui_push_subscriptions.json`` (0600)
  and every notification fans out to all of them.  There is no per-browser
  owner token.
* Subscription endpoints are SSRF-guarded: https only, no credentials, no
  localhost, and every resolved address must be globally routable (CGNAT
  100.64/10 and IPv4-mapped IPv6 are rejected too).  Delivery re-resolves,
  re-checks and pins the connection to the checked addresses, disables
  redirects and proxies.  Public push services (web.push.apple.com, FCM,
  Mozilla autopush) resolve to global addresses and pass.
* Delivery never runs on the caller's thread.  Producers call ``notify_*`` which
  does a non-blocking ``put_nowait`` onto a bounded queue drained by a small
  daemon worker pool; a full queue drops the push rather than delaying a
  terminal event.
* An unreadable/corrupt subscription store fails closed (no delivery, 503 on
  the API) and is never overwritten.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import logging
import os
import queue
import socket
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote, urlparse

logger = logging.getLogger(__name__)

_STORE_NAME = "webui_push_subscriptions.json"
_VAPID_FILE_NAME = "webui_vapid.json"
_STORE_LOCK = threading.Lock()
_TIMEOUT_SECONDS = 10
_MAX_WORKERS = 2
_MAX_PENDING = 32
_ENDPOINT_MAX_LENGTH = 2048
_KEY_MAX_LENGTH = 256
_MAX_SUBSCRIPTIONS = 32
_LOCAL_HOST_ALIASES = {"localhost", "ip6-localhost", "ip6-loopback"}
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")

_QUEUE: "queue.Queue[dict | None]" = queue.Queue(maxsize=_MAX_PENDING)
_QUEUE_LOCK = threading.Lock()
_WORKERS: list[threading.Thread] = []
_STOP = threading.Event()


class PushStoreUnavailable(RuntimeError):
    """The subscription store exists but cannot be trusted/read."""


class _EndpointResolutionError(ValueError):
    """Transient DNS failure (not proof the endpoint is unsafe)."""


# ── configuration ───────────────────────────────────────────────────────────

def _state_dir() -> Path:
    from api.config import STATE_DIR

    return Path(STATE_DIR)


def _vapid_file_values() -> dict:
    path = _state_dir() / _VAPID_FILE_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _setting(env_name: str, file_key: str) -> str:
    value = str(os.getenv(env_name, "") or "").strip()
    if value:
        return value
    return str(_vapid_file_values().get(file_key) or "").strip()


def public_key() -> str:
    return _setting("HERMES_WEBUI_VAPID_PUBLIC_KEY", "public_key")


def _private_key() -> str:
    return _setting("HERMES_WEBUI_VAPID_PRIVATE_KEY", "private_key")


def subject() -> str:
    raw = _setting("HERMES_WEBUI_VAPID_SUBJECT", "subject")
    if raw and "://" not in raw and not raw.startswith("mailto:"):
        return f"mailto:{raw}"
    return raw


def _pywebpush():
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        return None, None
    return webpush, WebPushException


def status() -> dict:
    """Public status. Never includes key material other than nothing at all."""
    configured = bool(public_key() and _private_key() and subject())
    dependency = _pywebpush()[0] is not None
    return {
        "configured": configured,
        "dependency_available": dependency,
        "enabled": bool(configured and dependency),
    }


def is_enabled() -> bool:
    return bool(status()["enabled"])


# ── SSRF guard ───────────────────────────────────────────────────────────────

def _addr_is_blocked(addr: str) -> bool:
    try:
        obj = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return True
    if isinstance(obj, ipaddress.IPv6Address) and obj.ipv4_mapped:
        obj = obj.ipv4_mapped
    if isinstance(obj, ipaddress.IPv4Address) and obj in _CGNAT_NETWORK:
        return True
    return not obj.is_global


def _parse_endpoint(endpoint: str):
    endpoint = str(endpoint or "").strip()
    if not endpoint:
        raise ValueError("subscription endpoint is required")
    if len(endpoint) > _ENDPOINT_MAX_LENGTH:
        raise ValueError("subscription endpoint is too long")
    parsed = urlparse(endpoint)
    if parsed.scheme.lower() != "https":
        raise ValueError("subscription endpoint must use https")
    if parsed.username or parsed.password:
        raise ValueError("subscription endpoint must not include credentials")
    host = str(parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("subscription endpoint host is required")
    if host in _LOCAL_HOST_ALIASES or host.endswith(".localhost"):
        raise ValueError("subscription endpoint must not target localhost")
    return endpoint, parsed, host


def _resolve_safe_addresses(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise _EndpointResolutionError("subscription endpoint host could not be resolved") from exc
    pinned: list[str] = []
    for _, _, _, _, addr in infos:
        if not addr:
            continue
        ip = str(addr[0])
        if _addr_is_blocked(ip):
            raise ValueError("subscription endpoint resolves to a non-public address")
        if ip not in pinned:
            pinned.append(ip)
    if not pinned:
        raise _EndpointResolutionError("subscription endpoint host could not be resolved")
    return pinned


def validate_endpoint(endpoint: str) -> str:
    endpoint, parsed, host = _parse_endpoint(endpoint)
    _resolve_safe_addresses(host, parsed.port or 443)
    return endpoint


def _pinned_requests_session(endpoint: str):
    """requests.Session pinned to the just-validated addresses (anti-rebind)."""
    _, parsed, host = _parse_endpoint(endpoint)
    pinned_hosts = _resolve_safe_addresses(host, parsed.port or 443)
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3 import HTTPSConnectionPool
    from urllib3.connection import HTTPSConnection
    from urllib3.util import connection as _u3conn

    class _PinnedConn(HTTPSConnection):
        def _new_conn(self):
            last = None
            for ip in pinned_hosts:
                try:
                    return _u3conn.create_connection(
                        (ip, self.port),
                        self.timeout,
                        source_address=self.source_address,
                        socket_options=self.socket_options,
                    )
                except OSError as exc:
                    last = exc
            raise last or OSError("no pinned Web Push target reachable")

    class _PinnedPool(HTTPSConnectionPool):
        ConnectionCls = _PinnedConn

    class _PinnedAdapter(HTTPAdapter):
        def init_poolmanager(self, connections, maxsize, block=False, **kw):
            super().init_poolmanager(connections, maxsize, block=block, **kw)
            self.poolmanager.pool_classes_by_scheme = dict(self.poolmanager.pool_classes_by_scheme)
            self.poolmanager.pool_classes_by_scheme["https"] = _PinnedPool

        def send(self, request, **kwargs):
            if kwargs.get("proxies"):
                raise ValueError("Web Push delivery does not allow proxies")
            return super().send(request, **kwargs)

        def proxy_manager_for(self, *a, **kw):
            raise ValueError("Web Push delivery does not allow proxies")

    class _BlockedAdapter(HTTPAdapter):
        def send(self, request, **kwargs):
            raise ValueError("Web Push delivery requires https")

    class _Session(requests.Session):
        def rebuild_proxies(self, prepared_request, proxies):
            return {}

        def request(self, method, url, **kwargs):
            kwargs["allow_redirects"] = False
            resp = super().request(method, url, **kwargs)
            if 300 <= int(getattr(resp, "status_code", 0) or 0) < 400:
                raise ValueError("Web Push delivery does not allow redirects")
            return resp

    session = _Session()
    session.trust_env = False
    session.mount("https://", _PinnedAdapter())
    session.mount("http://", _BlockedAdapter())
    return session


# ── subscription store ───────────────────────────────────────────────────────

def _store_path() -> Path:
    return _state_dir() / _STORE_NAME


def _b64url_decode(value: str, field: str) -> bytes:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"subscription keys.{field} is required")
    if len(raw) > _KEY_MAX_LENGTH:
        raise ValueError(f"subscription keys.{field} is too long")
    try:
        return base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"subscription keys.{field} must be valid base64url") from exc


def _normalize(subscription: dict, *, check_endpoint: bool) -> dict:
    if not isinstance(subscription, dict):
        raise ValueError("subscription must be an object")
    endpoint = str(subscription.get("endpoint") or "").strip()
    if check_endpoint:
        endpoint = validate_endpoint(endpoint)
    elif not endpoint or len(endpoint) > _ENDPOINT_MAX_LENGTH:
        raise ValueError("invalid subscription endpoint")
    keys = subscription.get("keys")
    if not isinstance(keys, dict):
        raise ValueError("subscription keys are required")
    p256dh = str(keys.get("p256dh") or "").strip()
    auth = str(keys.get("auth") or "").strip()
    p_bytes = _b64url_decode(p256dh, "p256dh")
    a_bytes = _b64url_decode(auth, "auth")
    if len(p_bytes) != 65 or p_bytes[0] != 0x04:
        raise ValueError("subscription keys.p256dh must be a 65-byte uncompressed public key")
    if len(a_bytes) != 16:
        raise ValueError("subscription keys.auth must be a 16-byte auth secret")
    return {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}


def _load() -> list[dict]:
    path = _store_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        subs = data["subscriptions"]
        if not isinstance(subs, list):
            raise ValueError("subscriptions is not a list")
        return [_normalize(s, check_endpoint=False) for s in subs]
    except Exception as exc:
        logger.warning("Web Push subscription store %s is unreadable; failing closed", path.name)
        raise PushStoreUnavailable("Web Push subscription store is unavailable") from exc


def _save(subs: list[dict]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"subscriptions": subs}, indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".web_push.tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def list_subscriptions() -> list[dict]:
    with _STORE_LOCK:
        return _load()


def subscription_count() -> int:
    return len(list_subscriptions())


def has_subscription(endpoint: str) -> bool:
    endpoint = str(endpoint or "").strip()
    return bool(endpoint) and any(s["endpoint"] == endpoint for s in list_subscriptions())


def add_subscription(subscription: dict, *, previous_endpoint: str | None = None) -> dict:
    """Validate and upsert. ``previous_endpoint`` replaces a rotated endpoint."""
    normalized = _normalize(subscription, check_endpoint=True)
    previous = str(previous_endpoint or "").strip()
    with _STORE_LOCK:
        subs = _load()
        drop = {normalized["endpoint"], previous} - {""}
        others = [s for s in subs if s["endpoint"] not in drop]
        if len(others) >= _MAX_SUBSCRIPTIONS:
            raise ValueError("too many Web Push subscriptions")
        others.append(normalized)
        if others != subs:
            _save(others)
    return normalized


def remove_subscription(endpoint: str) -> bool:
    endpoint = str(endpoint or "").strip()
    if not endpoint:
        return False
    with _STORE_LOCK:
        subs = _load()
        kept = [s for s in subs if s["endpoint"] != endpoint]
        if len(kept) == len(subs):
            return False
        _save(kept)
        return True


# ── delivery ─────────────────────────────────────────────────────────────────

def notification_payload(title: str, body: str, *, session_id: str | None = None) -> dict:
    sid = str(session_id or "").strip()
    return {
        "title": str(title or "Hermes")[:120],
        "options": {
            "body": str(body or "")[:240],
            "tag": f"hermes-{sid}" if sid else "hermes-webui",
            "renotify": True,
            "icon": "static/favicon-192.png",
            "badge": "static/favicon-32.png",
            "data": {"url": f"session/{quote(sid, safe='')}" if sid else "./"},
        },
    }


def _send_to_all(payload: dict) -> int:
    """Blocking delivery to every subscription. Only call from a worker."""
    if not is_enabled():
        return 0
    try:
        subs = list_subscriptions()
    except PushStoreUnavailable:
        return 0
    webpush_fn, _ = _pywebpush()
    if not webpush_fn or not subs:
        return 0
    data = json.dumps(payload, ensure_ascii=False)
    sent = 0
    for sub in subs:
        if _STOP.is_set():
            break
        endpoint = sub["endpoint"]
        try:
            session = _pinned_requests_session(endpoint)
        except _EndpointResolutionError:
            logger.debug("Web Push endpoint temporarily unresolvable", exc_info=True)
            continue
        except Exception:
            logger.debug("Web Push endpoint rejected by SSRF guard", exc_info=True)
            continue
        try:
            webpush_fn(
                subscription_info=sub,
                data=data,
                vapid_private_key=_private_key(),
                vapid_claims={"sub": subject()},
                requests_session=session,
                timeout=_TIMEOUT_SECONDS,
            )
            sent += 1
        except Exception as exc:  # pywebpush raises WebPushException
            resp = getattr(exc, "response", None)
            code = getattr(resp, "status_code", None)
            if code in (404, 410):
                try:
                    remove_subscription(endpoint)
                except Exception:
                    logger.debug("Failed to prune stale Web Push subscription", exc_info=True)
            logger.debug("Web Push send failed (status=%s)", code)
        finally:
            try:
                session.close()
            except Exception:
                pass
    return sent


def _worker() -> None:
    while True:
        try:
            job = _QUEUE.get(timeout=0.5)
        except queue.Empty:
            if _STOP.is_set():
                return
            continue
        if job is None:
            return
        if _STOP.is_set():
            continue
        try:
            _send_to_all(job)
        except Exception:
            logger.debug("Web Push background delivery failed", exc_info=True)


def _ensure_workers() -> None:
    with _QUEUE_LOCK:
        if _WORKERS or _STOP.is_set():
            return
        for i in range(_MAX_WORKERS):
            t = threading.Thread(target=_worker, name=f"web-push-{i + 1}", daemon=True)
            t.start()
            _WORKERS.append(t)


def enqueue(payload: dict) -> bool:
    """Non-blocking hand-off. Returns False when disabled/stopped/queue full."""
    if _STOP.is_set() or not is_enabled():
        return False
    _ensure_workers()
    try:
        _QUEUE.put_nowait(dict(payload))
    except queue.Full:
        logger.debug("Web Push queue full; dropping notification")
        return False
    return True


def shutdown(wait: float = 3.0) -> None:
    _STOP.set()
    try:
        while True:
            _QUEUE.get_nowait()
    except queue.Empty:
        pass
    with _QUEUE_LOCK:
        workers = list(_WORKERS)
    for t in workers:
        t.join(timeout=wait / max(1, len(workers)))


def send_test() -> bool:
    return enqueue(notification_payload("Hermes test", "Web Push is working."))


def _safe(fn):
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            logger.debug("Web Push notify failed", exc_info=True)
            return False

    wrapper.__name__ = fn.__name__
    return wrapper


def _message_text(message) -> str:
    content = message.get("content", "") if isinstance(message, dict) else ""
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if text:
                    parts.append(str(text))
            elif isinstance(part, str) and part:
                parts.append(part)
        return "\n".join(parts)
    return str(content or "")


@_safe
def notify_session_done(session_id: str, messages) -> bool:
    """Completion push from a message list (last assistant message is the body)."""
    if not is_enabled():
        return False
    answer = ""
    for message in reversed(messages or []):
        if isinstance(message, dict) and message.get("role") == "assistant":
            answer = _message_text(message)
            break
    return notify_response_complete(session_id, answer)


@_safe
def notify_response_complete(session_id: str, answer: str) -> bool:
    text = " ".join(str(answer or "").split())
    return enqueue(notification_payload("Response complete", text[:120] or "Task finished", session_id=session_id))


_SEEN: "dict[tuple, None]" = {}
_SEEN_LOCK = threading.Lock()
_SEEN_MAX = 256


def _first_time(*key) -> bool:
    """True once per key (bounded memory) so repeated mirrors don't re-push."""
    with _SEEN_LOCK:
        if key in _SEEN:
            return False
        _SEEN[key] = None
        while len(_SEEN) > _SEEN_MAX:
            _SEEN.pop(next(iter(_SEEN)))
        return True


@_safe
def notify_approval_required(session_id: str, approval: dict) -> bool:
    approval = approval or {}
    ident = approval.get("approval_id") or approval.get("request_id") or approval.get("description")
    if not _first_time("approval", session_id, str(ident)):
        return False
    body = str(approval.get("description") or "Tool approval needed")
    return enqueue(notification_payload("Approval required", body, session_id=session_id))


@_safe
def notify_clarify_required(session_id: str, clarify: dict) -> bool:
    ident = (clarify or {}).get("clarify_id") or (clarify or {}).get("question")
    if not _first_time("clarify", session_id, str(ident)):
        return False
    body = str((clarify or {}).get("question") or "Clarification needed")
    return enqueue(notification_payload("Clarification needed", body, session_id=session_id))


@_safe
def notify_bg_task_complete(session_id: str, payload: dict) -> bool:
    title = str((payload or {}).get("title") or "Background task complete")
    body = str((payload or {}).get("message") or "Task finished")
    return enqueue(notification_payload(title, body, session_id=session_id))
