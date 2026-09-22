"""The native-client front door for Hermes WebUI.

This module is deliberately a transport adapter, not a second agent runtime.
Profiles, transcripts, execution, approvals, and run journals remain owned by
the existing WebUI/Hermes modules.  The adapter adds only the stable origin,
device credential boundary, and the small wire projection used by Luna.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import queue
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import unquote, parse_qs

from api.config import STATE_DIR
from api.helpers import bad, j

logger = logging.getLogger(__name__)

_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_CHAT_PREFIX = "sgbot.bot-chat."
_INTERNAL_CHAT_PREFIX = "sgbot_bot_chat_"
_STORE_PATH = STATE_DIR / "frontdoor_devices.json"
_IDEMPOTENCY_PATH = STATE_DIR / "frontdoor_idempotency.json"
_STORE_LOCK = threading.RLock()
_CHAT_LOCK = threading.RLock()
_MAX_CONTENT = 256 * 1024
_MAX_IDEMPOTENCY_ENTRIES = 10_000


def _json_read(path: Path, fallback):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return fallback


def _json_write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _normalized_code(value) -> str:
    return re.sub(r"[^0-9]", "", str(value or ""))


def _pairing_codes() -> tuple[str, ...]:
    raw = os.getenv("HERMES_FRONTDOOR_PAIRING_CODES", "")
    if not raw:
        raw = os.getenv("HERMES_FRONTDOOR_PAIRING_CODE", "")
    codes = []
    for item in raw.split(","):
        code = _normalized_code(item)
        if code and code not in codes:
            codes.append(code)
    return tuple(codes)


def _request_origin(handler) -> str:
    """Resolve the externally visible origin using the existing proxy policy."""
    from api.routes import _request_base_url

    return _request_base_url(handler).rstrip("/")


@contextlib.contextmanager
def _profile_context(profile: str):
    from api.profiles import clear_request_profile, set_request_profile

    set_request_profile(profile)
    try:
        yield
    finally:
        clear_request_profile()


def _profile_rows() -> list[dict]:
    from api.profiles import list_profiles_api

    rows = list_profiles_api()
    return [row for row in rows if isinstance(row, dict)]


def _profile_row(profile: str) -> dict | None:
    if not _PROFILE_ID_RE.fullmatch(profile):
        return None
    for row in _profile_rows():
        if str(row.get("name") or "") == profile:
            return row
    return None


def _display_name(row: dict) -> str:
    value = str(row.get("display_name") or row.get("name") or "").strip()
    if not value:
        return "Hermes"
    return value.replace("_", " ").replace("-", " ").title()


def _chat_id(profile: str) -> str:
    return _CHAT_PREFIX + profile


def _internal_session_id(profile: str) -> str:
    return _INTERNAL_CHAT_PREFIX + profile


def _profile_from_chat(chat_id: str) -> str | None:
    value = str(chat_id or "")
    if not value.startswith(_CHAT_PREFIX):
        return None
    profile = value[len(_CHAT_PREFIX):]
    return profile if _profile_row(profile) else None


def _ensure_chat_session(profile: str):
    """Materialize the stable Bot Chat handle in Hermes' existing session store."""
    from api.models import LOCK, SESSIONS, Session, get_session
    from api.workspace import get_profile_default_workspace

    session_id = _internal_session_id(profile)
    with _CHAT_LOCK, _profile_context(profile):
        try:
            session = get_session(session_id)
        except KeyError:
            session = Session(
                session_id=session_id,
                title=_display_name(_profile_row(profile) or {"name": profile}),
                workspace=get_profile_default_workspace(),
                profile=profile,
                model=None,
            )
            session.save()
            with LOCK:
                SESSIONS[session_id] = session
        return session


def _message_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        pieces = []
        for item in value:
            if isinstance(item, str):
                pieces.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                pieces.append(item["text"])
        return "".join(pieces)
    if value is None:
        return ""
    return str(value)


def _history_payload(profile: str, *, cursor: str | None = None) -> dict:
    session = _ensure_chat_session(profile)
    messages = []
    for index, message in enumerate(getattr(session, "messages", []) or []):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "assistant")
        text = _message_text(message.get("content"))
        messages.append(
            {
                "id": f"{_chat_id(profile)}:message:{index}",
                "role": role,
                "text": text,
                "created_at": message.get("timestamp") or message.get("created_at"),
            }
        )
    return {"session_id": _chat_id(profile), "messages": messages, "next_cursor": None}


def _last_assistant_text(session) -> str:
    for message in reversed(getattr(session, "messages", []) or []):
        if isinstance(message, dict) and str(message.get("role") or "").lower() in {"assistant", "agent"}:
            return _message_text(message.get("content"))
    return ""


def _device_record(device_id: str, *, token_hash: str, origin: str, display_name: str, kind: str) -> dict:
    return {
        "device_id": device_id,
        "token_hash": token_hash,
        "server_origin": origin,
        "display_name": display_name[:120],
        "kind": kind[:32],
        "issued_at": time.time(),
    }


def _load_devices() -> dict:
    value = _json_read(_STORE_PATH, {"devices": {}})
    return value if isinstance(value, dict) and isinstance(value.get("devices"), dict) else {"devices": {}}


def _authenticate(handler):
    header = str(handler.headers.get("Authorization") or "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None, j(handler, {"error": "device credential required"}, status=401, extra_headers={"WWW-Authenticate": "Bearer"})
    token = token.strip()
    digest = _token_digest(token)
    origin = _request_origin(handler)
    with _STORE_LOCK:
        devices = _load_devices().get("devices", {})
        for device_id, record in devices.items():
            if not isinstance(record, dict) or record.get("revoked_at"):
                continue
            if not hmac.compare_digest(str(record.get("token_hash") or ""), digest):
                continue
            if not hmac.compare_digest(str(record.get("server_origin") or ""), origin):
                return None, j(handler, {"error": "device credential is bound to another origin"}, status=401)
            return {"device_id": device_id, **record}, None
    return None, j(handler, {"error": "invalid device credential"}, status=401, extra_headers={"WWW-Authenticate": "Bearer"})


def _enroll(handler, body: dict):
    codes = _pairing_codes()
    supplied = _normalized_code(body.get("code"))
    if not codes:
        return bad(handler, "front-door enrollment is not configured", 503)
    if not supplied or not any(hmac.compare_digest(supplied, code) for code in codes):
        return bad(handler, "invalid pairing code", 401)
    device_id = str(body.get("device_id") or "").strip()
    if not _DEVICE_ID_RE.fullmatch(device_id):
        return bad(handler, "invalid device_id", 400)
    origin = _request_origin(handler)
    token = secrets.token_urlsafe(32)
    record = _device_record(
        device_id,
        token_hash=_token_digest(token),
        origin=origin,
        display_name=str(body.get("display_name") or device_id).strip(),
        kind=str(body.get("kind") or "unknown").strip(),
    )
    with _STORE_LOCK:
        store = _load_devices()
        store["devices"][device_id] = record
        _json_write(_STORE_PATH, store)
    return j(handler, {
        "device_id": device_id,
        "token": token,
        "server_origin": origin,
        "issued_at": record["issued_at"],
    }, status=200)


def _idempotency_key(handler) -> str:
    return str(handler.headers.get("Idempotency-Key") or "").strip()


def _idempotency_lookup(device_id: str, chat_id: str, key: str, content: str):
    if not key:
        return None, "idempotency key required"
    identity = f"{device_id}\x1f{chat_id}\x1f{key}"
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    with _STORE_LOCK:
        store = _json_read(_IDEMPOTENCY_PATH, {})
        if not isinstance(store, dict):
            store = {}
        existing = store.get(identity)
        if existing:
            if existing.get("content_hash") != content_hash:
                return None, "idempotency key was already used for different content"
            return existing.get("run_id"), None
    return (identity, content_hash), None


def _idempotency_store(identity_and_hash, run_id: str):
    identity, content_hash = identity_and_hash
    with _STORE_LOCK:
        store = _json_read(_IDEMPOTENCY_PATH, {})
        if not isinstance(store, dict):
            store = {}
        store[identity] = {"content_hash": content_hash, "run_id": run_id, "created_at": time.time()}
        if len(store) > _MAX_IDEMPOTENCY_ENTRIES:
            oldest = sorted(store, key=lambda key: float(store[key].get("created_at") or 0))
            for key in oldest[:-_MAX_IDEMPOTENCY_ENTRIES]:
                store.pop(key, None)
        _json_write(_IDEMPOTENCY_PATH, store)


def _run_summary(run_id: str):
    from api.run_journal import find_run_summary

    try:
        return find_run_summary(run_id)
    except (ValueError, OSError):
        return None


def _run_output(summary: dict | None) -> str | None:
    if not summary:
        return None
    try:
        from api.models import get_session

        session = get_session(str(summary.get("session_id") or ""))
        output = _last_assistant_text(session)
        return output or None
    except Exception:
        return None


def _status_payload(run_id: str) -> dict | None:
    summary = _run_summary(run_id)
    if not summary:
        return None
    from api.config import STREAMS, STREAMS_LOCK

    with STREAMS_LOCK:
        active = run_id in STREAMS
    status = str(summary.get("terminal_state") or "").lower()
    if not summary.get("terminal") and not active:
        status = "failed"
    if not status or status == "unknown":
        status = "running" if active else "failed"
    return {
        "run_id": run_id,
        "status": status,
        "output": _run_output(summary),
        "error": None if status in {"completed", "cancelled", "running", "started", "stopping"} else "run ended without a terminal result",
    }


def _map_event(run_id: str, event_name: str, payload) -> tuple[str, dict, bool]:
    data = payload if isinstance(payload, dict) else {}
    if event_name == "token":
        return "message.delta", {"run_id": run_id, "delta": _message_text(data.get("text"))}, False
    if event_name == "done":
        return "run.progress", {"run_id": run_id}, False
    if event_name in {"cancel", "interrupted"}:
        return "run.completed", {"run_id": run_id, "status": "cancelled"}, True
    if event_name in {"apperror", "error"}:
        return "run.failed", {"run_id": run_id, "status": "failed", "error": data.get("message") or data.get("error") or "run failed"}, True
    if event_name == "stream_end":
        return "run.completed", {"run_id": run_id, "status": "completed"}, True
    return "run.progress", {"run_id": run_id, "event": event_name}, False


def _write_frontdoor_sse(handler, event_name: str, payload: dict, event_id: str | None = None):
    if event_id:
        handler.wfile.write(f"id: {event_id}\n".encode("utf-8"))
    handler.wfile.write(f"event: {event_name}\n".encode("utf-8"))
    handler.wfile.write(("data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode("utf-8"))
    handler.wfile.flush()


def _frontdoor_events(handler, run_id: str, parsed):
    from api.config import STREAMS, STREAMS_LOCK
    from api.run_journal import read_run_events
    from api.routes import _sse_set_write_deadline

    summary = _run_summary(run_id)
    if not summary:
        return j(handler, {"error": "run not found"}, status=404)
    try:
        after = str(parse_qs(parsed.query or "").get("cursor", [""])[0] or "").strip()
        if not after:
            after = str(handler.headers.get("Last-Event-ID") or "").strip()
        after_seq = int(after.rsplit(":", 1)[-1]) if after else 0
    except (TypeError, ValueError):
        after_seq = 0

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("X-Accel-Buffering", "no")
    handler.send_header("Connection", "close")
    from api.sse_chunked import end_sse_headers

    end_sse_headers(handler)
    _sse_set_write_deadline(handler)
    with STREAMS_LOCK:
        stream = STREAMS.get(run_id)
    subscriber = None
    replayed_event_ids: set[str] = set()
    try:
        if stream is not None:
            subscriber = (
                stream.subscribe_with_snapshot()[0]
                if hasattr(stream, "subscribe_with_snapshot")
                else stream.subscribe()
                if hasattr(stream, "subscribe")
                else stream
            )
        replay = read_run_events(str(summary.get("session_id") or ""), run_id, after_seq=after_seq)
        for entry in replay.get("events") or []:
            event_id = str(entry.get("event_id") or "")
            event_name, payload, terminal = _map_event(run_id, entry.get("event"), entry.get("payload"))
            _write_frontdoor_sse(handler, event_name, payload, event_id or None)
            if event_id:
                replayed_event_ids.add(event_id)
            if terminal:
                return True
        if stream is None:
            status = _status_payload(run_id) or {"run_id": run_id, "status": "failed"}
            event_name = "run.completed" if status.get("status") == "completed" else "run.failed"
            _write_frontdoor_sse(handler, event_name, status, None)
            return True
        while True:
            try:
                item = subscriber.get(timeout=15)
            except queue.Empty:
                handler.wfile.write(b": heartbeat\n\n")
                handler.wfile.flush()
                continue
            event_name, data = item[0], item[1]
            event_id = item[2] if len(item) >= 3 else None
            if event_id and event_id in replayed_event_ids:
                continue
            try:
                event_seq = int(str(event_id).rsplit(":", 1)[-1]) if event_id else None
            except ValueError:
                event_seq = None
            if event_seq is not None and event_seq <= after_seq:
                continue
            mapped_name, mapped_payload, terminal = _map_event(run_id, event_name, data)
            _write_frontdoor_sse(handler, mapped_name, mapped_payload, event_id)
            if terminal:
                return True
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        return True
    finally:
        if subscriber is not None and hasattr(stream, "unsubscribe"):
            try:
                stream.unsubscribe(subscriber)
            except Exception:
                pass


def _run_allowed(run_id: str) -> bool:
    summary = _run_summary(run_id)
    return bool(summary and str(summary.get("session_id") or "").startswith(_INTERNAL_CHAT_PREFIX))


def _resolve_auth(handler):
    device, response = _authenticate(handler)
    return device, response


def handle_get(handler, parsed) -> bool:
    path = parsed.path
    if not path.startswith("/v1/"):
        return False
    _device, response = _resolve_auth(handler)
    if response is not None:
        return True
    if path == "/v1/health":
        from api.updates import WEBUI_VERSION

        j(handler, {"status": "ok", "platform": "hermes-webui", "version": WEBUI_VERSION})
        return True
    if path == "/v1/capabilities":
        j(handler, {"version": "1", "supports_voice": True, "supports_approvals": True, "supports_cursor_replay": True})
        return True
    if path == "/v1/profiles":
        profiles = []
        for row in _profile_rows():
            name = str(row.get("name") or "").strip()
            if not _PROFILE_ID_RE.fullmatch(name):
                continue
            available = row.get("visible", True) is not False
            profiles.append({
                "id": name,
                "display_name": _display_name(row),
                "available": available,
                **({"reason": "Profile unavailable"} if not available else {}),
            })
        if not profiles:
            return bad(handler, "no Hermes profiles are available", 503)
        from api.profiles import get_active_profile_name

        active = get_active_profile_name()
        default = active if any(p["id"] == active for p in profiles) else profiles[0]["id"]
        j(handler, {"profiles": profiles, "default_profile_id": default})
        return True
    if path.startswith("/v1/profiles/") and path.endswith("/bot-chat"):
        profile = unquote(path[len("/v1/profiles/"):-len("/bot-chat")].strip("/"))
        row = _profile_row(profile)
        if row is None:
            return bad(handler, "profile not found", 404)
        if row.get("visible", True) is False:
            return bad(handler, "profile unavailable", 503)
        _ensure_chat_session(profile)
        j(handler, {"chat_id": _chat_id(profile), "profile_id": profile, "title": _display_name(row)})
        return True
    if path.startswith("/v1/bot-chats/") and path.endswith("/messages"):
        chat_id = unquote(path[len("/v1/bot-chats/"):-len("/messages")].strip("/"))
        profile = _profile_from_chat(chat_id)
        if profile is None:
            return bad(handler, "chat not found", 404)
        j(handler, _history_payload(profile, cursor=parse_qs(parsed.query or "").get("cursor", [None])[0]))
        return True
    if path.startswith("/v1/runs/") and path.endswith("/events"):
        run_id = unquote(path[len("/v1/runs/"):-len("/events")].strip("/"))
        if not _run_allowed(run_id):
            return bad(handler, "run not found", 404)
        return _frontdoor_events(handler, run_id, parsed)
    if path.startswith("/v1/runs/"):
        run_id = unquote(path[len("/v1/runs/"):].strip("/"))
        if not _run_allowed(run_id):
            return bad(handler, "run not found", 404)
        status = _status_payload(run_id)
        j(handler, status or {"run_id": run_id, "status": "failed"})
        return True
    return False


def _create_run(handler, chat_id: str, body: dict, device: dict):
    profile = _profile_from_chat(chat_id)
    if profile is None:
        return bad(handler, "chat not found", 404)
    if not isinstance(body, dict):
        return bad(handler, "JSON object required", 400)
    content = str(body.get("content") or "").strip()
    if not content:
        return bad(handler, "content is required", 400)
    if len(content.encode("utf-8")) > _MAX_CONTENT:
        return bad(handler, "content too large", 413)
    supplied_session = str(body.get("session_id") or "").strip()
    if supplied_session and supplied_session != chat_id:
        return bad(handler, "session_id must match chat_id", 409)
    key = _idempotency_key(handler)
    identity_and_hash, error = _idempotency_lookup(device["device_id"], chat_id, key, content)
    if error:
        return bad(handler, error, 409 if "different" in error else 400)
    if isinstance(identity_and_hash, str):
        return j(handler, {"run_id": identity_and_hash, "status": "started", "replayed": True})
    with _profile_context(profile):
        from api.routes import start_session_turn

        result = start_session_turn(_internal_session_id(profile), content, source="frontdoor")
    status = int(result.get("_status", 200) or 200)
    if status >= 400:
        return j(handler, result, status=status)
    run_id = str(result.get("stream_id") or "").strip()
    if not run_id:
        return bad(handler, "Hermes did not return a run id", 502)
    _idempotency_store(identity_and_hash, run_id)
    return j(handler, {"run_id": run_id, "status": "started", "replayed": False}, status=202)


def _steer(handler, body: dict):
    run_id = str(body.get("run_id") or "").strip()
    text = str(body.get("input") or body.get("text") or "").strip()
    if not run_id or not text or not _run_allowed(run_id):
        return j(handler, {"accepted": False, "fallback": "invalid_run"})
    from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK
    from api.run_journal import find_run_summary

    summary = find_run_summary(run_id)
    session_id = str(summary.get("session_id") or "") if summary else ""
    with SESSION_AGENT_CACHE_LOCK:
        entry = SESSION_AGENT_CACHE.get(session_id)
    agent = entry[0] if entry else None
    if agent is None or not hasattr(agent, "steer"):
        return j(handler, {"accepted": False, "fallback": "no_cached_agent", "run_id": run_id})
    try:
        agent.steer(text)
    except Exception:
        logger.debug("front-door steer failed for %s", run_id, exc_info=True)
        return j(handler, {"accepted": False, "fallback": "agent_rejected", "run_id": run_id})
    return j(handler, {"accepted": True, "run_id": run_id})


def _stop(handler, run_id: str):
    if not _run_allowed(run_id):
        return bad(handler, "run not found", 404)
    from api.streaming import cancel_stream

    cancelled = bool(cancel_stream(run_id))
    return j(handler, {"run_id": run_id, "status": "stopping" if cancelled else "completed"})


def _approval(handler, approval_id: str, body: dict):
    decision = str(body.get("decision") or "deny").strip().lower()
    if decision not in {"once", "session", "always", "deny"}:
        return bad(handler, "invalid decision", 400)
    from api import routes as route_state

    session_id = None
    with route_state._lock:
        for sid, queue in route_state._pending.items():
            entries = queue if isinstance(queue, list) else [queue] if queue else []
            if any(str(item.get("approval_id") or "") == approval_id for item in entries if isinstance(item, dict)):
                session_id = sid
                break
    if not session_id:
        return bad(handler, "approval not found", 404)
    resolved = route_state.resolve_gateway_approval(session_id, decision, resolve_all=False)
    if not resolved:
        return bad(handler, "approval is no longer pending", 409)
    return j(handler, {"ok": True, "approval_id": approval_id, "decision": decision})


def handle_post(handler, parsed, body: dict) -> bool:
    path = parsed.path
    if not path.startswith("/v1/"):
        return False
    if path == "/v1/devices/enroll":
        return _enroll(handler, body)
    device, response = _resolve_auth(handler)
    if response is not None:
        return True
    if path.startswith("/v1/bot-chats/") and path.endswith("/runs"):
        chat_id = unquote(path[len("/v1/bot-chats/"):-len("/runs")].strip("/"))
        return _create_run(handler, chat_id, body, device)
    if path.startswith("/v1/runs/") and path.endswith("/steer"):
        return _steer(handler, body)
    if path.startswith("/v1/runs/") and path.endswith("/stop"):
        return _stop(handler, unquote(path[len("/v1/runs/"):-len("/stop")].strip("/")))
    if path.startswith("/v1/approvals/") and path.endswith("/decision"):
        approval_id = unquote(path[len("/v1/approvals/"):-len("/decision")].strip("/"))
        return _approval(handler, approval_id, body)
    return False


def handle_delete(handler, parsed) -> bool:
    if parsed.path != "/v1/devices/self":
        return False
    device, response = _resolve_auth(handler)
    if response is not None:
        return True
    with _STORE_LOCK:
        store = _load_devices()
        record = store["devices"].get(device["device_id"])
        if isinstance(record, dict):
            record["revoked_at"] = time.time()
        _json_write(_STORE_PATH, store)
    j(handler, {})
    return True
