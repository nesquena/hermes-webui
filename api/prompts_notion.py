"""Notion-backed prompt library for the WebUI saved-prompts feature.

Reads Tim's "Prompt Library — slash-ready" Notion database so its rows show up
in the composer's saved-prompts popup and as slash-command expansion targets,
and writes new Draft rows back for "save current input".

Design constraints (from docs/CONTRACTS.md composer contract and CONTRIBUTING):
  - stdlib only (urllib.request), no new dependencies
  - read the row, not the page: palette metadata comes from database
    properties; the page body (the actual prompt text in a code block under
    ``**The prompt**``) is fetched only when a prompt is invoked
  - degrade gracefully: no token / unreachable Notion -> empty palette and a
    clear error payload, never a crash of /api/prompts itself

Notion API version 2025-09-03: databases are exposed as data_sources; queries
go to POST /v1/data_sources/{id}/query.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_NOTION_API = "https://api.notion.com/v1"
_NOTION_VERSION = "2025-09-03"
_DEFAULT_DATA_SOURCE_ID = "590c1287-48ed-4bed-bfc4-eaa13cdc0b40"
# How it works contract: publish when Status is Ready or Tested AND the
# Surfaces multi-select contains "Toolbelt".
_PUBLISH_STATUSES = frozenset({"ready", "tested"})
_PUBLISH_SURFACE = "Toolbelt"
_PALETTE_TTL_SECONDS = 60.0
_BODY_CACHE_TTL_SECONDS = 300.0
_HTTP_TIMEOUT = 12.0

_palette_cache: Dict[str, Any] = {"ts": 0.0, "rows": []}
_body_cache: Dict[str, Any] = {}
_cache_lock = threading.Lock()


def _notion_token() -> str:
    """Resolve the Notion integration token without printing it.

    Order: WEBUI_NOTION_API_KEY env (explicit override), the WebUI env file,
    then the agent's ~/.hermes/.env (shared integration, workspace bot
    "Toolbelt").
    """
    import os

    tok = str(os.environ.get("WEBUI_NOTION_API_KEY") or "").strip()
    if tok:
        return tok
    try:
        from api.providers import _load_env_file

        for candidate in _env_file_candidates():
            try:
                vals = _load_env_file(Path(candidate).expanduser())
            except Exception:
                continue
            tok = str(vals.get("NOTION_API_KEY") or "").strip()
            if tok:
                return tok
    except Exception:
        pass
    return ""


def _env_file_candidates() -> List[str]:
    """Candidate .env paths: active profile first, then default HERMES_HOME."""
    out: List[str] = []
    try:
        from api.profiles import get_active_hermes_home

        home = get_active_hermes_home()
        if home:
            out.append(str(Path(home).expanduser() / ".env"))
    except Exception:
        pass
    import os

    fallback = os.environ.get("HERMES_HOME")
    if fallback:
        p = str(Path(fallback).expanduser() / ".env")
        if p not in out:
            out.append(p)
    default = str(Path.home() / ".hermes" / ".env")
    if default not in out:
        out.append(default)
    return out


def _data_source_id() -> str:
    import os

    return str(os.environ.get("WEBUI_NOTION_PROMPT_DATA_SOURCE_ID") or "").strip() or _DEFAULT_DATA_SOURCE_ID


def _notion_request(method: str, path: str, body: Optional[dict] = None) -> dict:
    token = _notion_token()
    if not token:
        raise RuntimeError("notion token not configured")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        _NOTION_API + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": _NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _rich_text(prop: Optional[dict]) -> str:
    if not isinstance(prop, dict):
        return ""
    return "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))


def _row_to_palette_entry(row: dict) -> Optional[dict]:
    props = row.get("properties", {})
    status = ((props.get("Status") or {}).get("select") or {}).get("name", "")
    surfaces = [s.get("name", "") for s in (props.get("Surfaces") or {}).get("multi_select", [])]
    trigger = _rich_text(props.get("Trigger")).strip()
    if status.strip().lower() not in _PUBLISH_STATUSES:
        return None
    if _PUBLISH_SURFACE not in surfaces:
        return None
    if not trigger:
        return None
    title = "".join(t.get("plain_text", "") for t in (props.get("Prompt") or {}).get("title", []))
    variables_raw = _rich_text(props.get("Variables"))
    variables = [v.strip() for v in variables_raw.split(",") if v.strip()]
    return {
        "id": row.get("id"),
        "trigger": trigger if trigger.startswith("/") else "/" + trigger,
        "label": title or trigger,
        "use_when": _rich_text(props.get("Use when")).strip(),
        "variables": variables,
        "status": status,
        "category": ((props.get("Category") or {}).get("select") or {}).get("name", ""),
        "url": row.get("url", ""),
        "source": "notion",
    }


def notion_palette(force_refresh: bool = False) -> dict:
    """Return the published prompt palette from Notion (cached, TTL 60s).

    Returns ``{"ok": True, "prompts": [...]}`` or ``{"ok": False, "error":
    "..."}`` — never raises, so /api/prompts stays up when Notion is down.
    """
    now = time.time()
    with _cache_lock:
        if (
            not force_refresh
            and _palette_cache["rows"]
            and now - _palette_cache["ts"] < _PALETTE_TTL_SECONDS
        ):
            return {"ok": True, "prompts": list(_palette_cache["rows"]), "cached": True}

    try:
        payload = _notion_request(
            "POST",
            f"/data_sources/{_data_source_id()}/query",
            {"page_size": 100},
        )
    except Exception as exc:
        logger.warning("notion palette query failed: %s", exc)
        # Serve stale cache rather than nothing when Notion is unreachable.
        with _cache_lock:
            if _palette_cache["rows"]:
                return {"ok": True, "prompts": list(_palette_cache["rows"]), "stale": True}
        return {"ok": False, "error": f"Notion unavailable: {exc}"}

    entries: List[dict] = []
    for row in payload.get("results", []):
        entry = _row_to_palette_entry(row)
        if entry is not None:
            entries.append(entry)
    with _cache_lock:
        _palette_cache["ts"] = now
        _palette_cache["rows"] = entries
    return {"ok": True, "prompts": entries}


_BODY_HEADING_RE = re.compile(r"\*\*\s*The prompt\s*\*\*", re.IGNORECASE)


def _extract_prompt_body(markdown: str) -> str:
    """Pull the prompt text out of a Notion page body.

    The library's page layout is fixed: a ``**The prompt**`` bold heading
    followed by a fenced code block holding the raw prompt with
    ``{{variable}}`` placeholders. Fall back to the text after the heading
    when no fence is present.
    """
    m = _BODY_HEADING_RE.search(markdown)
    if not m:
        return ""
    rest = markdown[m.end():]
    fence = re.search(r"```[a-zA-Z0-9_-]*\n(.*?)```", rest, re.DOTALL)
    if fence:
        return fence.group(1).strip("\n")
    lines: List[str] = []
    for line in rest.splitlines():
        stripped = line.strip()
        if not stripped:
            if lines:
                break
            continue
        if stripped.startswith("**") or stripped.startswith("#"):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def notion_prompt_body(page_id: str) -> dict:
    """Fetch and cache a single prompt page body (TTL 5 min)."""
    now = time.time()
    with _cache_lock:
        hit = _body_cache.get(page_id)
        if hit and now - hit["ts"] < _BODY_CACHE_TTL_SECONDS:
            return {"ok": True, "body": hit["body"], "cached": True}
    try:
        payload = _notion_request("GET", f"/pages/{page_id}/markdown")
    except Exception as exc:
        logger.warning("notion page fetch failed for %s: %s", page_id, exc)
        with _cache_lock:
            hit = _body_cache.get(page_id)
            if hit:
                return {"ok": True, "body": hit["body"], "stale": True}
        return {"ok": False, "error": f"Notion unavailable: {exc}"}
    body = _extract_prompt_body(payload.get("markdown", "") or "")
    with _cache_lock:
        _body_cache[page_id] = {"ts": now, "body": body}
    return {"ok": True, "body": body}


def fill_variables(body: str, values: Dict[str, str]) -> str:
    """Replace {{var}} placeholders; unfilled ones stay visible for the agent."""
    def repl(m: "re.Match[str]") -> str:
        name = m.group(1).strip()
        return str(values.get(name, m.group(0)))

    return re.sub(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}", repl, body)


def notion_save_draft(label: str, text: str) -> dict:
    """Create a Draft row in the prompt library (write-back path).

    Only creates rows with Status=Draft — per the library contract a Draft is
    invisible in the palette until promoted by hand, so a save from the WebUI
    can never pollute the published set.
    """
    trigger = _derive_trigger(label or text)
    properties: Dict[str, Any] = {
        "Prompt": {"title": [{"text": {"content": label or text[:60]}}]},
        "Trigger": {"rich_text": [{"text": {"content": trigger}}]},
        "Variables": {"rich_text": [{"text": {"content": _derive_variables(text)}}]},
        "Status": {"select": {"name": "Draft"}},
        "Surfaces": {"multi_select": [{"name": _PUBLISH_SURFACE}]},
    }
    body = {
        "parent": {"database_id": _database_id_for_create()},
        "properties": properties,
        "markdown": f"**The prompt**\n```\n{text}\n```",
    }
    payload = _notion_request("POST", "/pages", body)
    row_id = payload.get("id", "")
    with _cache_lock:
        _palette_cache["ts"] = 0.0
    return {"ok": True, "id": row_id, "url": payload.get("url", ""), "trigger": trigger}


def _database_id_for_create() -> str:
    """Resolve the database_id for page creation.

    API 2025-09-03 splits databases into database_id + data_source_id; page
    creation needs the former, queries the latter. Resolved once from the data
    source's parent and cached; WEBUI_NOTION_PROMPT_DATABASE_ID overrides.
    """
    import os

    env_id = str(os.environ.get("WEBUI_NOTION_PROMPT_DATABASE_ID") or "").strip()
    if env_id:
        return env_id
    with _cache_lock:
        if _palette_cache.get("database_id"):
            return _palette_cache["database_id"]
    ds = _notion_request("GET", f"/data_sources/{_data_source_id()}")
    db_id = str((ds.get("parent") or {}).get("database_id") or "").strip()
    with _cache_lock:
        _palette_cache["database_id"] = db_id
    return db_id


def _derive_trigger(label: str) -> str:
    """Derive a short unique-ish trigger from a label ('Code Review' -> /cr-2)."""
    words = re.findall(r"[A-Za-z0-9]+", label.lower())
    if not words:
        return "/" + uuid.uuid4().hex[:6]
    if len(words) == 1:
        return "/" + words[0][:14]
    return "/" + "".join(w[0] for w in words[:3]) + (words[-1][1:2] if len(words) == 1 else "")


def _derive_variables(text: str) -> str:
    return ",".join(sorted(set(re.findall(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}", text))))
