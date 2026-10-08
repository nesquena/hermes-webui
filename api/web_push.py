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
* Subscriptions are stored per instance in
  ``<STATE_DIR>/webui_push_subscriptions.json`` (0600), each bound to an
  *owner*: ``sha256`` of a random per-browser device id (``localStorage``,
  sent as ``X-Hermes-Push-Device`` on same-origin API calls).  Only the hash is
  stored; neither it nor any endpoint is ever returned by an API.
* Session -> owner(s) is learned when a device starts a turn
  (``POST /api/chat/start``) or opens a session (``GET /api/session``) and kept
  in ``<STATE_DIR>/webui_push_session_owners.json`` (bounded, 0600).
  Session-done/approval/clarify/bg-task pushes go only to subscriptions whose
  owner opened that session.  A session with NO known owner (cron, gateway,
  never opened in a push-enabled browser) is NOT broadcast -- the conservative
  default.  ``HERMES_WEBUI_PUSH_BROADCAST_UNOWNED=1`` opts in to fan such
  sessions out to every subscription.  ``/api/push/test`` targets only the
  caller's own subscription(s).
* Legacy subscriptions (stored before owners existed) have ``owner == ""``.
  They receive nothing until bound: the owning device binds it on its next
  ``/api/push/status?endpoint=...`` (it proves possession of the endpoint) or
  on re-subscribe.  Nothing is lost; the user just reopens Settings (or taps
  Enable again).
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
import hashlib
import ipaddress
import json
import logging
import os
import queue
import re
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
        # The managed agent runtime relaunches with `python -I`, which ignores
        # PYTHONPATH. Allow an explicit, opt-in directory holding pywebpush
        # (installed with `pip install --target`) without touching that runtime.
        extra = os.getenv("HERMES_WEBUI_PUSH_DEPS_DIR", "").strip()
        if not extra or not os.path.isdir(extra):
            return None, None
        import sys
        if extra not in sys.path:
            sys.path.append(extra)
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


# ── owners (per-device targeting) ────────────────────────────────────────────

DEVICE_HEADER = "X-Hermes-Push-Device"
_DEVICE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_OWNER_RE = re.compile(r"^[0-9a-f]{64}$")
ALL_OWNERS = "*"


def _clean_owner(value) -> str:
    v = str(value or "").strip()
    return v if _OWNER_RE.match(v) else ""


def owner_for_device(device_id: str) -> str:
    """Stable opaque owner id for a client device id ('' when malformed)."""
    device_id = str(device_id or "").strip()
    if not _DEVICE_RE.match(device_id):
        return ""
    return hashlib.sha256(("hermes-webui-push-owner:" + device_id).encode()).hexdigest()


def owner_from_headers(headers) -> str:
    try:
        return owner_for_device(headers.get(DEVICE_HEADER) or "")
    except Exception:
        return ""


def broadcast_unowned_enabled() -> bool:
    return str(os.getenv("HERMES_WEBUI_PUSH_BROADCAST_UNOWNED", "")).strip().lower() in {"1", "true", "yes", "on"}


_SESSION_OWNERS_NAME = "webui_push_session_owners.json"
_SESSION_OWNERS_MAX = 512
_OWNERS_PER_SESSION_MAX = 8
_SESSION_LOCK = threading.Lock()
_SESSION_CACHE: "dict | None" = None  # {"path": str, "map": dict[str, list[str]]}


def _session_owner_map() -> dict:
    global _SESSION_CACHE
    path = str(_state_dir() / _SESSION_OWNERS_NAME)
    if _SESSION_CACHE is None or _SESSION_CACHE["path"] != path:
        data: dict = {}
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            for sid, owners in (raw.get("sessions") or {}).items():
                if isinstance(owners, list):
                    data[str(sid)] = [o for o in (_clean_owner(x) for x in owners) if o]
        except Exception:
            data = {}
        _SESSION_CACHE = {"path": path, "map": data}
    return _SESSION_CACHE["map"]


def _persist_session_owners(m: dict) -> None:
    path = _state_dir() / _SESSION_OWNERS_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".web_push.tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"sessions": m}, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def register_session_owner(session_id: str, owner: str) -> bool:
    """Record that ``owner`` (device) owns/opened ``session_id``. Cheap no-op if known."""
    sid = str(session_id or "").strip()
    owner = _clean_owner(owner)
    if not sid or not owner or len(sid) > 256:
        return False
    try:
        with _SESSION_LOCK:
            m = _session_owner_map()
            owners = m.get(sid)
            if owners and owner in owners:
                return False
            owners = list(owners or [])
            owners.append(owner)
            m.pop(sid, None)
            m[sid] = owners[-_OWNERS_PER_SESSION_MAX:]
            while len(m) > _SESSION_OWNERS_MAX:
                m.pop(next(iter(m)))
            _persist_session_owners(m)
            return True
    except Exception:
        logger.debug("Web Push session-owner registry write failed", exc_info=True)
        return False


def session_owners(session_id: str) -> list[str]:
    sid = str(session_id or "").strip()
    with _SESSION_LOCK:
        return list(_session_owner_map().get(sid, []))


def _targets_for_session(session_id: str):
    """Owners to notify, ALL_OWNERS (explicit opt-in), or None (send nothing)."""
    owners = session_owners(session_id)
    if owners:
        return owners
    if broadcast_unowned_enabled():
        return ALL_OWNERS
    logger.debug("Web Push: session has no known owner device; not broadcasting")
    return None


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
        out = []
        for s in subs:
            n = _normalize(s, check_endpoint=False)
            n["owner"] = _clean_owner(s.get("owner"))
            out.append(n)
        return out
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
    """Internal only: includes ``owner``. Never return this from an API."""
    with _STORE_LOCK:
        return _load()


def subscription_count(owner: str | None = None) -> int:
    subs = list_subscriptions()
    if owner is None:
        return len(subs)
    owner = _clean_owner(owner)
    return sum(1 for s in subs if owner and s["owner"] == owner)


def has_subscription(endpoint: str, owner: str | None = None) -> bool:
    """True if ``endpoint`` is stored (and, when ``owner`` is given, owned by it)."""
    endpoint = str(endpoint or "").strip()
    if not endpoint:
        return False
    owner_c = None if owner is None else _clean_owner(owner)
    return any(
        s["endpoint"] == endpoint and (owner_c is None or (owner_c and s["owner"] == owner_c))
        for s in list_subscriptions()
    )


def adopt_legacy_subscription(endpoint: str, owner: str) -> bool:
    """Bind a pre-owner (``owner == ""``) subscription to ``owner``.

    Possession of the exact endpoint URL proves the caller is that device.
    Subscriptions already owned by someone else are never reassigned here.
    """
    endpoint = str(endpoint or "").strip()
    owner = _clean_owner(owner)
    if not endpoint or not owner:
        return False
    with _STORE_LOCK:
        subs = _load()
        changed = False
        for s in subs:
            if s["endpoint"] == endpoint and not s["owner"]:
                s["owner"] = owner
                changed = True
        if changed:
            _save(subs)
        return changed


def add_subscription(subscription: dict, *, previous_endpoint: str | None = None, owner: str = "") -> dict:
    """Validate and upsert for ``owner``. ``previous_endpoint`` replaces a rotated
    endpoint, but only if it is the caller's own (or a legacy unowned) entry."""
    normalized = _normalize(subscription, check_endpoint=True)
    owner = _clean_owner(owner)
    previous = str(previous_endpoint or "").strip()
    with _STORE_LOCK:
        subs = _load()
        drop_new = normalized["endpoint"]
        others = []
        for s in subs:
            if s["endpoint"] == drop_new:
                continue
            if previous and s["endpoint"] == previous and (not s["owner"] or s["owner"] == owner):
                continue
            others.append(s)
        if len(others) >= _MAX_SUBSCRIPTIONS:
            raise ValueError("too many Web Push subscriptions")
        record = dict(normalized, owner=owner)
        others.append(record)
        if others != subs:
            _save(others)
    return normalized


def remove_subscription(endpoint: str, owner: str | None = None) -> bool:
    """Remove ``endpoint``. With ``owner`` given, only that owner's (or a legacy
    unowned) entry is removed -- other devices' subscriptions are untouched."""
    endpoint = str(endpoint or "").strip()
    if not endpoint:
        return False
    owner_c = None if owner is None else _clean_owner(owner)
    with _STORE_LOCK:
        subs = _load()

        def _match(s):
            if s["endpoint"] != endpoint:
                return False
            return owner_c is None or not s["owner"] or (bool(owner_c) and s["owner"] == owner_c)

        kept = [s for s in subs if not _match(s)]
        if len(kept) == len(subs):
            return False
        _save(kept)
        return True


# ── delivery ─────────────────────────────────────────────────────────────────

def _mask(text: str) -> str:
    """Mask credentials before text leaves for Apple/Google push services.

    Always on, independent of ``api_redact_enabled`` (that setting governs what
    the user's own browser sees; push bodies transit third-party services). If
    the redactor is unavailable we fail closed and drop the text.
    """
    text = str(text or "")
    if not text:
        return ""
    try:
        from api.helpers import _redact_text

        return _redact_text(text, _enabled=True)
    except Exception:
        logger.debug("Web Push: redaction unavailable; dropping body text", exc_info=True)
        return ""


def snippets_enabled() -> bool:
    """Set HERMES_WEBUI_PUSH_SNIPPETS=0 to send generic bodies with no reply text."""
    return os.getenv("HERMES_WEBUI_PUSH_SNIPPETS", "1").strip().lower() not in ("0", "false", "no", "off")


def notification_payload(title: str, body: str, *, session_id: str | None = None, owners=None) -> dict:
    """Build a payload. ``owners`` (list of owner ids, or ALL_OWNERS) is routing
    metadata kept under ``_owners``; it is stripped before delivery. Without it
    the payload is delivered to nobody."""
    sid = str(session_id or "").strip()
    payload = {
        "title": _mask(str(title or "Hermes"))[:120] or "Hermes",
        "options": {
            "body": _mask(str(body or ""))[:240],
            "tag": f"hermes-{sid}" if sid else "hermes-webui",
            "renotify": True,
            "icon": "static/favicon-192.png",
            "badge": "static/favicon-32.png",
            "data": {"url": f"session/{quote(sid, safe='')}" if sid else "./"},
        },
    }
    if owners:
        payload["_owners"] = ALL_OWNERS if owners == ALL_OWNERS else list(owners)
    return payload


def _send_to_all(payload: dict) -> int:
    """Blocking delivery to the subscriptions selected by ``payload['_owners']``.

    A payload with no ``_owners`` is delivered to nobody. Only call from a worker.
    """
    payload = dict(payload)
    owners = payload.pop("_owners", None)
    if not owners or not is_enabled():
        return 0
    try:
        subs = list_subscriptions()
    except PushStoreUnavailable:
        return 0
    if owners != ALL_OWNERS:
        wanted = {_clean_owner(o) for o in owners} - {""}
        subs = [s for s in subs if s["owner"] in wanted]
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
                subscription_info={"endpoint": sub["endpoint"], "keys": sub["keys"]},
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


def send_test(owner: str = "") -> bool:
    """Test push to ``owner``'s own subscription(s) only (never a broadcast)."""
    owner = _clean_owner(owner)
    if not owner:
        return False
    return enqueue(notification_payload("Hermes test", "Web Push is working.", owners=[owner]))


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
    owners = _targets_for_session(session_id)
    if not owners:
        return False
    # Mask the full text first, then shorten, so a secret straddling the cut
    # can't leak a partial token.
    text = " ".join(_mask(answer if snippets_enabled() else "").split())
    return enqueue(
        notification_payload("Response complete", text[:120] or "Task finished", session_id=session_id, owners=owners)
    )


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
    owners = _targets_for_session(session_id)
    if not owners:
        return False
    body = str(approval.get("description") or "Tool approval needed")
    return enqueue(notification_payload("Approval required", body, session_id=session_id, owners=owners))


@_safe
def notify_clarify_required(session_id: str, clarify: dict) -> bool:
    ident = (clarify or {}).get("clarify_id") or (clarify or {}).get("question")
    if not _first_time("clarify", session_id, str(ident)):
        return False
    owners = _targets_for_session(session_id)
    if not owners:
        return False
    body = str((clarify or {}).get("question") or "Clarification needed")
    return enqueue(notification_payload("Clarification needed", body, session_id=session_id, owners=owners))


@_safe
def notify_bg_task_complete(session_id: str, payload: dict) -> bool:
    title = str((payload or {}).get("title") or "Background task complete")
    owners = _targets_for_session(session_id)
    if not owners:
        return False
    body = str((payload or {}).get("message") or "Task finished")
    return enqueue(notification_payload(title, body, session_id=session_id, owners=owners))
