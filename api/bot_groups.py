"""Opt-in presentation bridge to Hermes' process-owned Group Chat service.

No Agent imports, room database, task execution, or approval queue lives here.
The configured Dashboard is an installation-owner capability, not a tenant API.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from api.helpers import j


class BotGroupsError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


_FIELDS = {
    "capabilities": (), "profiles": (), "list": ("limit", "offset"),
    "create": ("room_id", "name", "members"), "state": ("room_id",),
    "log": ("room_id", "since_seq", "limit"),
    "send": ("room_id", "event_id", "payload"),
    "stop": ("room_id", "cancel_id"), "retry": ("room_id", "task_id"),
    "approve": ("room_id", "member_id", "task_id", "execution_generation", "request_id", "choice"),
}
_READS = {"capabilities", "profiles", "list", "state", "log"}


def enabled() -> bool:
    return os.getenv("HERMES_WEBUI_BOT_GROUPS", "").lower() in {"1", "true"}


def _gateway_url() -> str:
    raw = os.getenv("HERMES_WEBUI_BOT_GATEWAY_URL", "ws://127.0.0.1:9119/api/ws")
    token = os.getenv("HERMES_WEBUI_BOT_GATEWAY_TOKEN", "").strip()
    parsed = urlsplit(raw)
    # First slice intentionally targets a same-host Dashboard (or an SSH tunnel).
    # Public Dashboard OAuth/tickets need their own reviewed authentication flow.
    if (parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path != "/api/ws" or not token):
        raise BotGroupsError("Configure a loopback Dashboard /api/ws URL and Bot Gateway token on the WebUI server.", 503)
    return urlunsplit(parsed._replace(query=urlencode({"token": token})))


def gateway_request(method: str, params: dict) -> dict:
    try:
        from websockets.sync.client import connect
    except ImportError as exc:
        raise BotGroupsError("Bot groups require the optional websockets>=15 package in the WebUI environment.", 503) from exc
    url = _gateway_url()
    request_id = uuid.uuid4().hex
    try:
        with connect(url, open_timeout=5, close_timeout=1, max_size=4 * 1024 * 1024, proxy=None) as socket:
            socket.send(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}))
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                response = json.loads(socket.recv(timeout=max(0.01, deadline - time.monotonic())))
                if not isinstance(response, dict) or response.get("id") != request_id:
                    continue
                if "error" in response:
                    error = response["error"]
                    code = error.get("code") if isinstance(error, dict) else None
                    if code == -32601:
                        raise BotGroupsError("This Hermes Dashboard does not support Bot groups. Update Hermes Agent.", 501)
                    # Upstream errors can contain paths, commands, or credentials.
                    safe_code = code if isinstance(code, int) else "unknown"
                    raise BotGroupsError(f"Hermes rejected this group operation ({safe_code}). Refresh the room before trying again.", 409)
                result = response.get("result")
                if not isinstance(result, dict):
                    raise BotGroupsError("Hermes returned an invalid group response.", 502)
                return result
            raise TimeoutError
    except BotGroupsError:
        raise
    except Exception as exc:
        # Never log a WebSocket exception: it can contain the authenticated URL.
        raise BotGroupsError("Cannot reach the Hermes Dashboard. Check its status and Bot Gateway configuration.", 502) from exc


def _text(value, field: str, maximum: int = 256) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise BotGroupsError(f"{field} is required (maximum {maximum} characters).")


def _identifier(value, field: str) -> None:
    _text(value, field, 128)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", value):
        raise BotGroupsError(f"Invalid {field}.")


def _integer(value, field: str, maximum: int = 2 ** 53 - 1, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise BotGroupsError(f"Invalid {field}.")


def call_group_method(operation: str, params: dict) -> dict:
    fields = _FIELDS.get(operation)
    if fields is None or not isinstance(params, dict) or set(params) - set(fields):
        raise BotGroupsError("Unknown Bot group operation or parameters.")
    for field in ("room_id", "event_id", "cancel_id", "task_id", "member_id", "request_id"):
        if field in fields:
            _identifier(params.get(field), field)
    for field in ("limit", "offset", "since_seq", "execution_generation"):
        if field in params:
            _integer(params[field], field, 500 if field == "limit" else 2 ** 53 - 1,
                     1 if field in {"limit", "execution_generation"} else 0)
    if operation == "create":
        _text(params.get("name"), "name", 120)
        members = params.get("members")
        if not isinstance(members, list) or not 2 <= len(members) <= 6:
            raise BotGroupsError("Select 2–6 different Agent profiles.")
        for member in members:
            if not isinstance(member, dict) or set(member) != {"member_id", "profile", "handle", "display_name"}:
                raise BotGroupsError("Only local Agent profile members are supported.")
            for field in ("member_id", "profile", "handle"):
                _identifier(member[field], field)
            _text(member["display_name"], "display_name", 120)
            if member["handle"].lower() in {"all", "everyone"}:
                raise BotGroupsError("Reserved handle.")
        for field in ("member_id", "profile", "handle"):
            if len({m[field].casefold() for m in members}) != len(members):
                raise BotGroupsError("Each member, profile, and handle must be unique.")
    if operation == "send":
        payload = params.get("payload")
        if not isinstance(payload, dict) or set(payload) != {"text", "thread_id"}:
            raise BotGroupsError("Messages require text and thread_id only.")
        _text(payload["text"], "text", 32000)
        if len(payload["text"].encode("utf-8")) > 65536:
            raise BotGroupsError("Message exceeds the Hermes 64 KiB UTF-8 limit.")
        _identifier(payload["thread_id"], "thread_id")
    if operation == "approve":
        _integer(params.get("execution_generation"), "execution_generation", minimum=1)
        if params.get("choice") not in {"once", "deny"}:
            raise BotGroupsError("Only allow once or deny is supported.")
    if operation == "profiles":
        result = gateway_request("profiles.list", {})
        return {"profiles": [
            {"name": p["name"], "display_name": p.get("display_name") or p["name"]}
            for p in result.get("profiles", []) if isinstance(p, dict) and isinstance(p.get("name"), str)
        ]}
    result = gateway_request("groups." + operation, params)
    if operation == "capabilities":
        return {key: result.get(key) for key in ("protocol_version", "driver", "methods", "max_log_limit")}
    return result


def _owner_access(handler) -> bool:
    from api.auth import parse_cookie, session_bound_profile
    from api.profiles import _is_isolated_profile_mode

    cookie = parse_cookie(handler) or getattr(handler, "_trusted_auth_session_cookie_value", "")
    # groups.* has installation-wide state, not room-level or human-user ACLs.
    return not _is_isolated_profile_mode() and not (cookie and session_bound_profile(cookie))


def handle_bot_groups(handler, parsed, body: dict | None = None) -> bool:
    operation = parsed.path.removeprefix("/api/bot-groups/")
    if operation not in _FIELDS or (body is None) != (operation in _READS):
        j(handler, {"error": "Unknown Bot group endpoint"}, status=404)
        return True
    try:
        if not _owner_access(handler):
            raise BotGroupsError("Bot groups are installation-owner only; profile-bound logins are not supported.", 403)
        if not enabled():
            if operation == "capabilities":
                j(handler, {"enabled": False, "available": False})
                return True
            raise BotGroupsError("Bot groups are not enabled on this WebUI.", 503)
        query = parse_qs(parsed.query)
        params = body if body is not None else {k: v[0] for k, v in query.items()}
        if body is None:
            for key in {"limit", "offset", "since_seq"} & params.keys():
                try:
                    params[key] = int(params[key])
                except (ValueError, TypeError) as exc:
                    raise BotGroupsError(f"Invalid {key}.") from exc
        result = call_group_method(operation, params)
        if operation == "capabilities":
            result = {"enabled": True, "available": result.get("protocol_version") == 2, "capabilities": result}
        j(handler, result, extra_headers={"Cache-Control": "no-store"})
    except BotGroupsError as exc:
        if operation == "capabilities" and exc.status != 403:
            j(handler, {"enabled": enabled(), "available": False, "error": str(exc)}, extra_headers={"Cache-Control": "no-store"})
        else:
            j(handler, {"error": str(exc)}, status=exc.status)
    return True
