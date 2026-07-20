"""
Hermes Web UI -- public read-only share snapshots.

Stores a sanitized, immutable snapshot of a conversation under STATE_DIR/shares.
The snapshot is intentionally narrower than a full session export so public
links do not leak local workspace paths, profile details, or raw tool payloads.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path

from api.config import STATE_DIR
from api.helpers import redact_session_data
# _redact_fn_cached is the ALWAYS-ON credential redactor (agent redactor with
# force=True + local fallback regex). Unlike redact_session_data it does NOT
# consult the user-toggleable api_redact_enabled setting — a public share is a
# hard safety boundary that must redact credentials even if the operator turned
# API-response redaction off.
from api.helpers import _redact_fn_cached as _force_redact_credentials

logger = logging.getLogger(__name__)

SHARES_DIR = STATE_DIR / "shares"
_SHARE_LOCK = threading.Lock()


def _ensure_share_dir() -> None:
    SHARES_DIR.mkdir(parents=True, exist_ok=True)


def _share_path(token: str) -> Path:
    token = str(token or "").strip()
    if not token:
        raise ValueError("share token is required")
    if not token.replace("-", "").replace("_", "").isalnum():
        raise ValueError("invalid share token")
    return SHARES_DIR / f"{token}.json"


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f"{path.stem}.",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _share_message_text(message: dict) -> str:
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                # Non-dict list items (e.g. nested structures) are NOT plain text —
                # never stringify them into the public snapshot.
                continue
            if item.get("type") == "text":
                # Only append genuine string text — a dict-valued "text" (possible
                # via /api/session/import) must NOT be str()'d into the public
                # snapshot (that would publish structured/tool payload verbatim).
                _t = item.get("text")
                if isinstance(_t, str):
                    parts.append(_t)
        return "".join(parts).strip()
    if isinstance(content, str):
        return content.strip()
    # A dict/other structured content (e.g. a tool-result object) is NOT shareable
    # text — do NOT str() it (that would publish raw structured/tool payload).
    return ""


def _strip_media_references(text: str) -> str:
    """Replace local MEDIA: tokens and file:// URLs with inert placeholders.

    Public shares must not emit links to the authenticated /api/media endpoint.
    MEDIA:<path> sentinel values and file:// URLs that renderMd() would
    convert into /api/media?path=... URLs are replaced with a non-clickable
    placeholder so anonymous recipients never see broken auth-gated media links.

    Covers every renderer-recognized file:// form (issue #6285 review):
      - Bare file:// URLs (whitespace-delimited)
      - Markdown links: [label](file://...)
      - Markdown images: ![alt](file://...)
      - file:path (no slashes), file:/path (single slash)
      - file://localhost/path, file://127.0.0.1/path
      - URL-encoded file:// variants (e.g. file://%2Ftmp%2Ffile)
    while preserving fenced and inline-code regions byte-for-byte (the
    renderer keeps file:// inert inside code/preformatted content).
    """
    if not isinstance(text, str) or not text:
        return text
    placeholder = "[Local attachment omitted from public share]"

    # Stash fenced code blocks, matching the renderer's line-anchored
    # variable-length fence grammar (#6285). Opening fence:
    # ``^[ ]{0,3}(`{3,})([^`]*)$`` — closing fence:
    # ``^[ ]{0,3}(`{3,})[ \t]*$`` with length >= opening length.
    _fenced: list[str] = []
    _lines = text.split("\n")
    _fence_parts: list[str] = []
    _i = 0
    while _i < len(_lines):
        _om = re.match(r"^[ ]{0,3}(`{3,})([^`]*)$", _lines[_i])
        if _om:
            _open_len = len(_om.group(1))
            _start = _i
            _i += 1
            while _i < len(_lines):
                _cm = re.match(r"^[ ]{0,3}(`{3,})[ \t]*$", _lines[_i])
                if _cm and len(_cm.group(1)) >= _open_len:
                    _i += 1
                    break
                _i += 1
            _block = "\n".join(_lines[_start:_i])
            _fenced.append(_block)
            _fence_parts.append(f"\x00F{len(_fenced) - 1}\x00")
        else:
            _fence_parts.append(_lines[_i])
            _i += 1
    text = "\n".join(_fence_parts)
    # Stash inline code spans (`...`) so file:// inside them is preserved.
    _inline: list[str] = []
    text = re.sub(
        r"`[^`\n]+`",
        lambda m: _inline.append(m.group(0)) or f"\x00I{len(_inline) - 1}\x00",
        text,
    )

    # MEDIA:<path-or-url> tokens (may already have their path redacted)
    text = re.sub(r"MEDIA:\S+", placeholder, text)

    # Markdown images: ![alt](file:(?://)?...) → placeholder
    text = re.sub(r"!\[[^\]]*\]\(file:(?://)?[^\s)]+\)", placeholder, text)

    # Markdown links: [label](file:(?://)?...) → placeholder
    text = re.sub(r"\[[^\]]+\]\(file:(?://)?[^\s)]+\)", placeholder, text)

    # Bare file:(?://)? URLs – preserve the leading delimiter instead of
    # consuming whitespace and unconditionally inserting a space (review
    # feedback). The file:(?://)? pattern also catches file:path and
    # file:/path forms in addition to standard file:// and file:/// variants.
    text = re.sub(r"(^|\s)file:(?://)?[^\s<>\"')\]]+", r"\1" + placeholder, text)

    # Restore stashed code regions.
    for i, s in enumerate(_fenced):
        text = text.replace(f"\x00F{i}\x00", s)
    for i, s in enumerate(_inline):
        text = text.replace(f"\x00I{i}\x00", s)

    return text


def _redact_share_paths(text: str, extra_paths) -> str:
    """Strip known local session/workspace/home paths out of public-share text.

    A workspace path or Hermes home can be embedded inside message prose (an
    agent quoting a file path, a traceback, etc.). Redact the concrete local
    paths so a public share never discloses the operator's filesystem layout.
    """
    if not isinstance(text, str) or not text:
        return text
    for p in extra_paths:
        if not p:
            continue
        p = str(p).strip()
        if len(p) >= 4 and p in text:
            text = text.replace(p, "[redacted-path]")
    return text


def _sanitize_message(message: dict, *, redact_paths=()) -> dict | None:
    if not isinstance(message, dict):
        return None
    role = str(message.get("role") or "").strip().lower()
    if role not in {"user", "assistant"}:
        return None
    text = _share_message_text(message)
    if not text:
        return None
    # ALWAYS-ON hardening for the public boundary, independent of any setting:
    # (1) force credential redaction, (2) strip known local paths.
    text = _force_redact_credentials(text)
    text = _redact_share_paths(text, redact_paths)
    # Strip MEDIA: / file:// references so the public share never renders
    # links to the authenticated /api/media endpoint (issue #6126).
    text = _strip_media_references(text)
    if not text.strip():
        return None
    sanitized = {
        "role": role,
        "content": text,
    }
    ts = message.get("timestamp")
    if isinstance(ts, (int, float)):
        sanitized["timestamp"] = ts
    return sanitized


def _public_share_payload(payload: dict) -> dict:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        messages = []
    # Sanitize each message on read so legacy snapshots stored before the
    # write-time sanitizer was introduced also have MEDIA:/file:// stripped
    # (issue #6285 review – "close the legacy-snapshot path").
    safe_messages = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            content = _strip_media_references(content)
            if content.strip():
                safe_messages.append({**msg, "content": content})
        else:
            safe_messages.append(msg)
    public = {
        "title": str(payload.get("title") or "Untitled"),
        "messages": safe_messages,
        "message_count": int(payload.get("message_count") or len(safe_messages)),
    }
    created_at = payload.get("created_at")
    updated_at = payload.get("updated_at")
    if isinstance(created_at, (int, float)):
        public["created_at"] = created_at
    if isinstance(updated_at, (int, float)):
        public["updated_at"] = updated_at
    return public


def build_share_snapshot(session) -> dict:
    raw_dict = getattr(session, "__dict__", {}) or {}
    # redact_session_data respects the api_redact_enabled setting; keep it as a
    # first pass, but the per-message sanitizer below applies ALWAYS-ON credential
    # + path redaction that does NOT depend on that setting (the public boundary
    # must hold even if the operator disabled api_redact_enabled).
    safe_session = redact_session_data(raw_dict)
    # Concrete local paths to scrub from any message prose / title.
    redact_paths = []
    for key in ("workspace", "worktree_path", "worktree_repo_root"):
        val = raw_dict.get(key)
        if val:
            redact_paths.append(str(val))
    try:
        from api.profiles import get_active_hermes_home
        redact_paths.append(str(get_active_hermes_home()))
    except Exception:
        pass
    try:
        redact_paths.append(str(Path.home()))
    except Exception:
        pass
    safe_messages = []
    for raw in safe_session.get("messages") or []:
        sanitized = _sanitize_message(raw, redact_paths=redact_paths)
        if sanitized:
            safe_messages.append(sanitized)
    if not safe_messages:
        raise ValueError("This conversation has no shareable messages yet.")
    # Only accept a genuine string title — a dict-valued title (possible via
    # /api/session/import) must not be str()'d into the public snapshot.
    _raw_title = safe_session.get("title")
    _raw_title = _raw_title if isinstance(_raw_title, str) else "Untitled"
    title = _force_redact_credentials(_raw_title or "Untitled")
    title = _redact_share_paths(title, redact_paths) or "Untitled"
    return {
        "title": title,
        "messages": safe_messages,
        "message_count": len(safe_messages),
    }


def create_or_refresh_share(session) -> dict:
    snapshot = build_share_snapshot(session)
    with _SHARE_LOCK:
        _ensure_share_dir()
        existing_token = str(getattr(session, "share_token", "") or "").strip()
        token = existing_token or secrets.token_urlsafe(18)
        now = time.time()
        payload = {
            "token": token,
            "source_session_id": str(getattr(session, "session_id", "") or ""),
            "title": snapshot["title"],
            "messages": snapshot["messages"],
            "message_count": snapshot["message_count"],
            "created_at": now,
            "updated_at": now,
            "revoked_at": None,
        }
        path = _share_path(token)
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    payload["created_at"] = existing.get("created_at") or now
            except Exception:
                logger.debug("Ignoring malformed share snapshot at %s", path, exc_info=True)
        _write_json_atomic(path, payload)
    return {
        "share_token": token,
        "share_title": payload["title"],
        "share_message_count": payload["message_count"],
        "share_created_at": payload["created_at"],
        "share_updated_at": payload["updated_at"],
    }


def load_share(token: str) -> dict | None:
    try:
        path = _share_path(token)
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Failed to read share snapshot %s", path, exc_info=True)
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("revoked_at"):
        return None
    return _public_share_payload(payload)


def revoke_share(session) -> bool:
    token = str(getattr(session, "share_token", "") or "").strip()
    if not token:
        return False
    with _SHARE_LOCK:
        try:
            path = _share_path(token)
        except ValueError:
            return False
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            payload["revoked_at"] = time.time()
            _write_json_atomic(path, payload)
    return True
