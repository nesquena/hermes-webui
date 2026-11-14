"""Server-to-server client for the central control-api (plan Appendix B, W7).

The WebUI talks to control-api with two credentials, both read from secret
files that only the webui container mounts (Appendix D):

* the **control key** (``SP_CONTROL_KEY_FILE``) signs every request with
  ``SP-HMAC-SHA256`` over the method, the path with its query, a timestamp, a
  nonce, the three acting-user headers and the body hash. The acting headers
  are passed in by the caller; this module signs them and never computes them
  from the governance policy (that is W9a's helper).
* the **ingest key** (``SP_INGEST_KEY_FILE``) is sent as
  ``Authorization: SP-Ingest <sentry_key>`` on ``POST /v1/events`` only (the
  ops reporter).

Public surface for the Wave 2 packages (W9a, W13):

* :func:`configured` tells whether the control key and the base URL are
  usable; it never raises.
* :func:`call` sends one signed request and returns ``(status, json)``. It
  never raises either. ``status`` is the HTTP status of control-api's answer;
  ``0`` means no HTTP answer was obtained (not configured, invalid arguments,
  network failure, timeout, refused redirect). The JSON is then
  ``{"error": {"code": ..., "message": ...}}`` in Appendix B's error shape,
  with one of the codes in :data:`LOCAL_ERROR_CODES`.

Key files hold one entry of the ``POST /v1/enroll`` answer, either as the JSON
object ``{"kind", "key_id", "secret", "allowed_ingress"}`` or as one line
``<key_id>:<secret>``. The secret is hex; its bytes are the HMAC key, the same
bytes control-api derives as ``HMAC-SHA256(KEK_v, "sp-key-v1|" + key_id)``.
An ingest key file may also hold the 32-hex ``sentry_key`` itself.

Key material never leaves this module: no function returns it, logs it, or
puts it in an error message, and redirects are never followed, so the
``Authorization`` header only ever reaches the configured control-api host.
"""

from __future__ import annotations

import hashlib
import http.client
import socket
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Mapping
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

__all__ = [
    "configured",
    "ingest_configured",
    "call",
    "post_events",
    "sign",
    "LOCAL_ERROR_CODES",
]

CONTROL_URL_ENV = "SP_CONTROL_URL"
CONTROL_KEY_FILE_ENV = "SP_CONTROL_KEY_FILE"
INGEST_KEY_FILE_ENV = "SP_INGEST_KEY_FILE"

AUTH_SCHEME = "SP-HMAC-SHA256"
INGEST_SCHEME = "SP-Ingest"
SIGNATURE_VERSION = "v1"

# Appendix B acting-user headers, in string_to_sign order (the same constants
# as STACK's app/control/contract.py).
ACTING_USER_HEADER = "X-SP-Acting-User"
ACTING_MEMBER_OF_HEADER = "X-SP-Acting-Member-Of"
ACTING_ADMIN_OF_HEADER = "X-SP-Acting-Admin-Of"
ACTING_HEADERS = (ACTING_USER_HEADER, ACTING_MEMBER_OF_HEADER, ACTING_ADMIN_OF_HEADER)
_ACTING_ALIASES = {
    "user": ACTING_USER_HEADER,
    "member_of": ACTING_MEMBER_OF_HEADER,
    "admin_of": ACTING_ADMIN_OF_HEADER,
}

DEFAULT_TIMEOUT_S = 10.0
MAX_TIMEOUT_S = 60.0
MAX_REQUEST_BYTES = 256 * 1024        # the /v1/events cap; nothing we send is larger
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_KEY_FILE_BYTES = 4096
MAX_EMAIL_LENGTH = 254
MAX_DEPARTMENT_HEADER_LENGTH = 4096
MAX_DEPARTMENTS = 64

LOCAL_ERROR_CODES = frozenset({
    "not_configured",
    "invalid_request",
    "unreachable",
    "timeout",
    "redirect_refused",
    "invalid_response",
})

_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_KEY_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_SECRET_RE = re.compile(r"^(?:[0-9a-f]{2}){16,64}$")
_SENTRY_KEY_RE = re.compile(r"^[0-9a-f]{32}$")
# Only /v1/ routes, RFC 3986 path and query characters, no dot segments.
_PATH_RE = re.compile(r"^/v1/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*(?:\?[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)?$")
_DEPARTMENT_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_EMAIL_RE = re.compile(r"^[^@\s,;:<>()\[\]\"'\\]+@[^@\s,;:<>()\[\]\"'\\]+$")


# ── Configuration ────────────────────────────────────────────────────────────

def _base_url() -> str | None:
    """The validated control-api base URL without a trailing slash, or None."""
    raw = str(os.environ.get(CONTROL_URL_ENV) or ("http://localhost" if os.environ.get("SP_CONTROL_SOCKET") else "")).strip()
    if not raw or len(raw) > 512 or any(ord(ch) < 33 or ord(ch) > 126 for ch in raw):
        return None
    try:
        parts = urlsplit(raw)
        port = parts.port  # raises ValueError on a bad port
    except ValueError:
        return None
    del port
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if "@" in parts.netloc or parts.query or parts.fragment:
        return None
    return raw.rstrip("/")


class _KeyCache:
    """Parsed key files, re-read when a file's inode, size or mtime changes."""

    def __init__(self):
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], tuple[tuple, Any]] = {}

    def get(self, env_name: str, kind: str):
        path = str(os.environ.get(env_name) or "").strip()
        if not path:
            return None
        try:
            st = os.stat(path)
        except OSError:
            return None
        stamp = (st.st_ino, st.st_size, st.st_mtime_ns)
        cache_key = (kind, path)
        with self._lock:
            cached = self._entries.get(cache_key)
            if cached is not None and cached[0] == stamp:
                return cached[1]
        value = None
        if 0 < st.st_size <= MAX_KEY_FILE_BYTES:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    text = fh.read(MAX_KEY_FILE_BYTES + 1)
                value = _parse_key_text(text, kind)
            except (OSError, UnicodeDecodeError, ValueError):
                value = None
        with self._lock:
            self._entries[cache_key] = (stamp, value)
        return value

    def clear(self):
        with self._lock:
            self._entries.clear()


_KEYS = _KeyCache()


def _parse_key_text(text: str, kind: str):
    """Parse one key file. Returns ``(key_id, secret_bytes)`` for the control
    key and the 32-hex sentry key for the ingest key; None when unusable."""
    text = (text or "").strip()
    if not text:
        return None
    key_id = secret_hex = sentry_key = None
    if text.startswith("{"):
        data = json.loads(text)
        if not isinstance(data, dict):
            return None
        file_kind = data.get("kind")
        if file_kind is not None and file_kind != kind:
            return None
        key_id = data.get("key_id")
        secret_hex = data.get("secret")
        sentry_key = data.get("sentry_key")
    elif ":" in text:
        key_id, _, secret_hex = text.partition(":")
    else:
        sentry_key = text
    if kind == "ingest":
        if isinstance(key_id, str) and isinstance(secret_hex, str):
            key_id, secret_hex = key_id.strip().lower(), secret_hex.strip().lower()
            if _KEY_ID_RE.fullmatch(key_id) and _SECRET_RE.fullmatch(secret_hex):
                return key_id + secret_hex[:16]
            return None
        if isinstance(sentry_key, str) and _SENTRY_KEY_RE.fullmatch(sentry_key.strip().lower()):
            return sentry_key.strip().lower()
        return None
    if not isinstance(key_id, str) or not isinstance(secret_hex, str):
        return None
    key_id, secret_hex = key_id.strip().lower(), secret_hex.strip().lower()
    if not _KEY_ID_RE.fullmatch(key_id) or not _SECRET_RE.fullmatch(secret_hex):
        return None
    return key_id, bytes.fromhex(secret_hex)


def _control_key():
    return _KEYS.get(CONTROL_KEY_FILE_ENV, "control")


def _ingest_key():
    return _KEYS.get(INGEST_KEY_FILE_ENV, "ingest")


def configured() -> bool:
    """Whether signed control-key calls can be made. Never raises."""
    try:
        return _base_url() is not None and _control_key() is not None
    except Exception:
        return False


def ingest_configured() -> bool:
    """Whether events can be posted with the ingest key. Never raises."""
    try:
        return _base_url() is not None and _ingest_key() is not None
    except Exception:
        return False


def clear_cache() -> None:
    """Forget parsed key files (tests, and after a key rotation on disk)."""
    _KEYS.clear()


# ── Signing (Appendix B) ─────────────────────────────────────────────────────

def string_to_sign(method: str, path_with_query: str, ts: str, nonce: str,
                   acting_values: tuple[str, str, str], body: bytes) -> str:
    return "\n".join((
        SIGNATURE_VERSION,
        method,
        path_with_query,
        ts,
        nonce,
        acting_values[0],
        acting_values[1],
        acting_values[2],
        hashlib.sha256(body).hexdigest(),
    ))


def sign(secret: bytes, key_id: str, method: str, path_with_query: str, *, body: bytes = b"",
         acting_values: tuple[str, str, str] = ("", "", ""), ts: str | None = None,
         nonce: str | None = None) -> str:
    """The ``Authorization`` header value for one request (Appendix B)."""
    ts = ts if ts is not None else str(int(time.time()))
    nonce = nonce if nonce is not None else secrets.token_hex(16)
    message = string_to_sign(method, path_with_query, ts, nonce, acting_values, body)
    sig = hmac.new(secret, message.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{AUTH_SCHEME} kid={key_id}, ts={ts}, nonce={nonce}, sig={sig}"


def _department_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        items = value.split(",") if value.strip() else []
    elif isinstance(value, (list, tuple)):
        items = list(value)
    elif isinstance(value, (set, frozenset)):
        items = sorted(value, key=str)
    else:
        raise ValueError("department list must be a string or a list")
    if len(items) > MAX_DEPARTMENTS:
        raise ValueError("too many departments")
    if not all(isinstance(item, str) and _DEPARTMENT_KEY_RE.fullmatch(item.strip()) for item in items):
        raise ValueError("invalid department key")
    joined = ",".join(item.strip() for item in items)
    if len(joined) > MAX_DEPARTMENT_HEADER_LENGTH:
        raise ValueError("department header too long")
    return joined


def _acting_values(acting) -> tuple[str, str, str] | None:
    """Validated acting header values in signing order, or None for none."""
    if acting is None:
        return None
    if not isinstance(acting, Mapping):
        raise ValueError("acting must be a mapping")
    values = {name: None for name in ACTING_HEADERS}
    for key, value in acting.items():
        header = _ACTING_ALIASES.get(key, key)
        canonical = next((name for name in ACTING_HEADERS if name.lower() == str(header).lower()), None)
        if canonical is None:
            raise ValueError("unknown acting field")
        values[canonical] = value
    user = values[ACTING_USER_HEADER]
    if user is None:
        user = ""
    if not isinstance(user, str):
        raise ValueError("acting user must be a string")
    user = user.strip().lower()
    if user and (len(user) > MAX_EMAIL_LENGTH or not user.isascii() or not _EMAIL_RE.fullmatch(user)):
        raise ValueError("invalid acting user")
    return (
        user,
        _department_value(values[ACTING_MEMBER_OF_HEADER]),
        _department_value(values[ACTING_ADMIN_OF_HEADER]),
    )


# ── Transport ────────────────────────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the signed Authorization header must not
    reach any other host or path."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _opener():
    return urllib.request.build_opener(_NoRedirect())


def _local_error(code: str, message: str) -> tuple[int, dict]:
    return 0, {"error": {"code": code, "message": message}}


def _parse_json_body(status: int, raw: bytes) -> tuple[int, Any]:
    if not raw:
        return status, {}
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return status, {"error": {"code": "invalid_response", "message": "The control plane sent a response that is not JSON."}}


def _read_limited(fp) -> bytes | None:
    data = fp.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        return None
    return data


def _send(method: str, url: str, headers: dict, body: bytes, timeout: float) -> tuple[int, Any]:
    unix_socket = os.environ.get("SP_CONTROL_SOCKET", "")
    if unix_socket:
        connection = None
        try:
            if not os.path.isabs(unix_socket):
                return _local_error("not_configured", "Control socket must be an absolute path.")
            connection = http.client.HTTPConnection("localhost", timeout=timeout)
            connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.sock.settimeout(timeout)
            connection.sock.connect(unix_socket)
            parts = urlsplit(url)
            target = parts.path + ("?" + parts.query if parts.query else "")
            connection.request(method, target, body=body or None, headers=headers)
            response = connection.getresponse()
            if 300 <= response.status < 400:
                return _local_error("redirect_refused", "Control redirect refused.")
            raw = _read_limited(response)
            if raw is None:
                return _local_error("invalid_response", "Control response is too large.")
            return _parse_json_body(response.status, raw)
        except (OSError, http.client.HTTPException):
            return _local_error("unreachable", "The control plane could not be reached.")
        finally:
            if connection is not None:
                connection.close()
    request = urllib.request.Request(url, data=body if body else None, method=method, headers=headers)
    try:
        with _opener().open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", None) or response.getcode())
            raw = _read_limited(response)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        if 300 <= status < 400:
            try:
                exc.close()
            except Exception:
                pass
            return _local_error("redirect_refused", "The control plane answered with a redirect, which is not followed.")
        try:
            raw = _read_limited(exc)
        except Exception:
            raw = b""
        finally:
            try:
                exc.close()
            except Exception:
                pass
    except (TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            return _local_error("timeout", "The control plane did not answer in time.")
        return _local_error("unreachable", "The control plane could not be reached.")
    except Exception:
        return _local_error("unreachable", "The control plane could not be reached.")
    if raw is None:
        return status, {"error": {"code": "invalid_response", "message": "The control plane response was too large."}}
    return _parse_json_body(status, raw)


def _timeout(value) -> float:
    try:
        timeout = float(value) if value is not None else DEFAULT_TIMEOUT_S
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_S
    if not 0 < timeout <= MAX_TIMEOUT_S:
        timeout = DEFAULT_TIMEOUT_S
    return timeout


def _encode_body(body) -> bytes:
    if body is None:
        return b""
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def call(method: str, path: str, *, body=None, acting=None, timeout: float | None = None, cloud_session: str | None = None) -> tuple[int, Any]:
    """Send one control-key request signed per Appendix B. Never raises.

    ``path`` starts with ``/v1/`` and may carry a query string, already
    percent-encoded; it is signed exactly as sent. ``body`` is JSON-encoded
    (``None`` sends no body and signs the hash of the empty string).
    ``acting`` is ``None`` or a mapping with ``user``, ``member_of`` and
    ``admin_of`` (or the ``X-SP-Acting-*`` header names); department lists may
    be lists or comma-separated strings. Given acting headers are sent and
    signed; empty ones are signed as empty strings.
    """
    try:
        method = str(method or "").upper()
        if method not in _METHODS:
            return _local_error("invalid_request", "Unsupported method.")
        path = str(path or "")
        if not _PATH_RE.fullmatch(path) or "/../" in path or path.endswith("/..") or "/./" in path or "//" in path:
            return _local_error("invalid_request", "The path must be a /v1/ control-api route.")
        try:
            acting_values = _acting_values(acting)
            payload = _encode_body(body)
        except (TypeError, ValueError):
            return _local_error("invalid_request", "The request could not be encoded.")
        if len(payload) > MAX_REQUEST_BYTES:
            return _local_error("invalid_request", "The request body is too large.")
        base = _base_url()
        key = _control_key()
        if base is None or key is None:
            return _local_error("not_configured", "The control plane is not configured on this installation.")
        key_id, secret = key
        target = urlsplit(base).path.rstrip("/") + path
        headers = {
            "Accept": "application/json",
            "User-Agent": "synthpulse-webui",
            "Authorization": sign(secret, key_id, method, target, body=payload,
                                  acting_values=acting_values or ("", "", "")),
        }
        if payload:
            headers["Content-Type"] = "application/json"
        if acting_values is not None:
            for name, value in zip(ACTING_HEADERS, acting_values, strict=True):
                headers[name] = value
        if cloud_session is not None:
            if not re.fullmatch(r'[a-f0-9]{64}',cloud_session):
                return _local_error("invalid_request","Invalid personal Cloud session.")
            headers["X-SP-Cloud-Session"] = cloud_session
        return _send(method, base + path, headers, payload, _timeout(timeout))
    except Exception:
        logger.debug("control-api call failed", exc_info=False)
        return _local_error("unreachable", "The control plane could not be reached.")


def post_events(events: list, *, timeout: float | None = None) -> tuple[int, Any]:
    """``POST /v1/events`` with the ingest key (``SP-Ingest``). Never raises."""
    try:
        if not isinstance(events, list) or not events or len(events) > 100:
            return _local_error("invalid_request", "Between 1 and 100 events are sent at a time.")
        try:
            payload = _encode_body({"events": events})
        except (TypeError, ValueError):
            return _local_error("invalid_request", "The events could not be encoded.")
        if len(payload) > MAX_REQUEST_BYTES:
            return _local_error("invalid_request", "The request body is too large.")
        base = _base_url()
        sentry_key = _ingest_key()
        if base is None or sentry_key is None:
            return _local_error("not_configured", "The control plane is not configured on this installation.")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "synthpulse-webui",
            "Authorization": f"{INGEST_SCHEME} {sentry_key}",
        }
        return _send("POST", base + "/v1/events", headers, payload, _timeout(timeout))
    except Exception:
        logger.debug("control-api event post failed", exc_info=False)
        return _local_error("unreachable", "The control plane could not be reached.")
