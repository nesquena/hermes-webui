"""Session context brief for the WebUI Context panel.

Two layers, both read-only:

1. **Deterministic assembly** — goal state, todo snapshot, recent user
   requests, verified outcomes (``# CONCLUSION`` blocks), compression
   milestones and live in-flight state (active stream, background tasks).
   Always available, instant, no model call.
2. **LLM narrative** — an optional auxiliary-model brief (« Demandes /
   Accompli / Reste à faire ») generated in a background job, persisted per
   session, and flagged stale once the transcript has moved on.

The brief is display state only: it is never injected into the agent context
and never mutates the session.
"""

from __future__ import annotations

import copy
import tempfile
import os
import hashlib
import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

# Self-contained copies of the session-squash helpers (upstream lacks
# api/session_squash). Deduplicate against that module if the squash
# feature lands upstream.
_DISTILL_BUDGET_CHARS = 100_000

def _atomic_write(path: Path, payload: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        Path(tmp_name).unlink(missing_ok=True)




def _message_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return ""




def _distill_transcript(session, budget: int = _DISTILL_BUDGET_CHARS) -> str:
    """Compact, budget-bounded view of the transcript for the aux model.

    Keeps every user message and every assistant message carrying a
    ``# CONCLUSION`` block (the verified outcomes), plus the head/tail of the
    conversation; tool payloads are intentionally dropped.
    """
    messages = [m for m in (session.messages or []) if isinstance(m, dict)]
    sections: list[str] = []
    used_idx: set[int] = set()

    def _fmt(idx: int, m: dict, cap: int) -> str:
        role = str(m.get("role") or "?")
        text = _message_text(m.get("content")).strip()
        if len(text) > cap:
            text = text[:cap] + "\n[…tronqué…]"
        return f"--- [{idx}] {role} ---\n{text}"

    def _append(idx: int, cap: int) -> bool:
        nonlocal budget
        if idx in used_idx:
            return True
        chunk = _fmt(idx, messages[idx], cap)
        if budget - len(chunk) < 0:
            return False
        sections.append(chunk)
        used_idx.add(idx)
        budget -= len(chunk)
        return True

    # 1. All user messages (the requests).
    for i, m in enumerate(messages):
        if m.get("role") == "user":
            if not _append(i, 1200):
                break
    # 2. Assistant messages with a CONCLUSION block (verified outcomes).
    for i, m in enumerate(messages):
        if m.get("role") == "assistant" and "# CONCLUSION" in _message_text(m.get("content")):
            if not _append(i, 3000):
                break
    # 3. Head and tail for framing.
    for i in list(range(min(2, len(messages)))) + list(range(max(0, len(messages) - 4), len(messages))):
        _append(i, 800)
    # 4. Remaining assistant messages, newest first, while budget lasts.
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            if not _append(i, 1500):
                break

    sections.sort(key=lambda s: int(s.split("]")[0].split("[")[1]) if s.startswith("--- [") else 0)
    return "\n\n".join(sections)




def _extract_llm_content(response) -> str:
    message = response.choices[0].message
    if isinstance(message, dict):
        content = message.get("content")
    else:
        content = getattr(message, "content", message)
    if not isinstance(content, str):
        content = str(content) if content else ""
    return content.strip()




def _busy_fields(session) -> dict:
    busy = {}
    for field in ("active_stream_id", "pending_user_message", "pending_started_at", "pending_turn_id"):
        value = getattr(session, field, None)
        if value:
            busy[field] = str(value)[:80]
    attachments = getattr(session, "pending_attachments", None)
    if attachments:
        busy["pending_attachments"] = len(attachments)
    return busy


logger = logging.getLogger(__name__)

MIN_BRIEF_CHARS = 200
_JOB_TTL_SECONDS = 3600.0

_REQUEST_CAP = 8
_REQUEST_EXCERPT_CHARS = 240
_CONCLUSION_CAP = 5
_CONCLUSION_EXCERPT_CHARS = 220
# Interleaved request→conclusion thread shown by the panel. Capped on its own
# budget: it carries both roles, so it must hold more entries than either list.
_TIMELINE_CAP = _REQUEST_CAP + _CONCLUSION_CAP
_COMPRESSION_CAP = 6
_COMPRESSION_EXCERPT_CHARS = 160
_LAST_REPLY_EXCERPT_CHARS = 280


class BriefError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ── job registry (LLM narrative refresh) ─────────────────────────────────

_JOBS: dict[str, dict] = {}
_SID_GENERATIONS: dict[str, int] = {}
_DELETED_WEBUI_SIDS: set[str] = set()
_JOBS_LOCK = threading.Lock()


def _job_snapshot(job: dict) -> dict:
    return {k: v for k, v in job.items() if not k.startswith("_")}


def brief_job_status(job_id: str) -> dict | None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        return _job_snapshot(job) if job else None


def _purge_jobs() -> None:
    with _JOBS_LOCK:
        _purge_jobs_locked()


def _purge_jobs_locked() -> None:
    """Purge finished jobs past TTL. Caller must hold _JOBS_LOCK."""
    cutoff = time.time() - _JOB_TTL_SECONDS
    stale = [
        jid for jid, job in _JOBS.items()
        if job.get("status") in ("done", "error", "cancelled")
        and job.get("finished_at", 0) < cutoff
    ]
    for jid in stale:
        _JOBS.pop(jid, None)


# ── session resolution ───────────────────────────────────────────────────

def _resolve_session(sid: str):
    """Return (session_like, source). source ∈ webui | state_db.

    WebUI-local sessions resolve through the canonical store; CLI/gateway
    transcripts fall back to a read-only shim over state.db messages so the
    brief also works for sessions opened from other surfaces.

    Multi-profile limitation: the state.db fallback reads the default
    profile home (``profile=None``), so CLI sessions belonging to another
    Hermes profile resolve as 404 here and their goal/brief store lookups
    also target the default home. WebUI-local sessions are unaffected —
    their profile is read from the session itself.
    """
    from api.models import get_cli_session_messages, get_session  # late import: tests patch module attrs

    with _JOBS_LOCK:
        resolution_generation = _SID_GENERATIONS.get(sid, 0)
    try:
        session = get_session(sid)
    except KeyError:
        pass
    else:
        # A canonical WebUI session always wins.  Clearing the process-local
        # deletion marker here lets a legitimately recreated session reuse the
        # same SID; the monotonically increasing generation still fences jobs
        # that belong to the deleted lifecycle.
        with _JOBS_LOCK:
            # A lookup that began before deletion may return a now-stale
            # in-memory object. It must not clear the new lifecycle's fence.
            if _SID_GENERATIONS.get(sid, 0) == resolution_generation:
                _DELETED_WEBUI_SIDS.discard(sid)
        return session, "webui"

    if _state_db_fallback_blocked(sid):
        raise BriefError("Session not found", 404)
    messages = get_cli_session_messages(sid)
    # The state.db read can overlap WebUI deletion.  Recheck after I/O so a
    # retained row cannot be admitted after the canonical session vanished.
    if _state_db_fallback_blocked(sid):
        raise BriefError("Session not found", 404)
    if messages:
        return (
            SimpleNamespace(
                session_id=sid,
                title=None,
                workspace=None,
                model=None,
                created_at=None,
                updated_at=None,
                messages=messages,
                path=None,
                profile=None,
                active_stream_id=None,
                pending_user_message=None,
                pending_started_at=None,
                pending_turn_id=None,
                pending_attachments=[],
            ),
            "state_db",
        )
    raise BriefError("Session not found", 404)


def _state_db_fallback_blocked(sid: str) -> bool:
    """Whether a deleted WebUI lifecycle may no longer fall back to state.db."""
    with _JOBS_LOCK:
        if sid in _DELETED_WEBUI_SIDS:
            return True
    # The models layer owns a durable deletion tombstone.  Honor it so a
    # process restart cannot make an old state.db transcript eligible again.
    try:
        from api.models import _load_webui_deleted_session_tombstone

        return sid in _load_webui_deleted_session_tombstone()
    except Exception:
        logger.debug(
            "context brief: durable deletion tombstone unavailable for %s",
            sid,
            exc_info=True,
        )
        return False


def _delete_stored_brief_locked(
    state_root,
    sid: str,
    *,
    block_state_db_fallback: bool,
) -> None:
    """Fence jobs and remove the cache while the session mutation lock is held."""
    with _JOBS_LOCK:
        _SID_GENERATIONS[sid] = _SID_GENERATIONS.get(sid, 0) + 1
        if block_state_db_fallback:
            _DELETED_WEBUI_SIDS.add(sid)
        else:
            # Messaging transcripts intentionally survive WebUI deletion and
            # remain available as read-only state.db sessions.
            _DELETED_WEBUI_SIDS.discard(sid)
        for job in _JOBS.values():
            if job.get("session_id") == sid and job.get("status") == "running":
                job.update(
                    status="cancelled",
                    result=None,
                    error="session deleted",
                    finished_at=time.time(),
                )
    try:
        (Path(state_root) / "context-briefs" / f"{sid}.json").unlink(missing_ok=True)
    except Exception:
        logger.debug("failed to remove context brief for deleted session %s", sid, exc_info=True)


def delete_stored_brief(
    state_root,
    sid: str,
    *,
    block_state_db_fallback: bool = True,
    _session_lock_held: bool = False,
) -> None:
    """Best-effort removal of a stored LLM brief when its session is deleted.

    Called from POST /api/session/delete: the brief is a derivable display
    cache (transcript excerpts + LLM narrative) and must not outlive the
    session it summarizes.  Deletion advances a per-SID generation, cancels
    running jobs, and blocks state.db fallback for deleted WebUI sessions.
    A canonical session recreated with the same SID still resolves normally.

    ``block_state_db_fallback`` remains false for messaging sessions because
    their source transcript intentionally survives WebUI deletion.
    ``_session_lock_held`` is for the delete route, which already owns the
    canonical per-session mutation lock.  Direct callers acquire that same
    lock so cache unlink and worker persistence cannot cross.

    Never raises — deletion of the session itself is authoritative.
    """
    try:
        if _session_lock_held:
            _delete_stored_brief_locked(
                state_root,
                sid,
                block_state_db_fallback=block_state_db_fallback,
            )
            return
        from api.models import _get_session_agent_lock

        with _get_session_agent_lock(sid):
            _delete_stored_brief_locked(
                state_root,
                sid,
                block_state_db_fallback=block_state_db_fallback,
            )
    except Exception:
        logger.debug("failed to fence context brief for deleted session %s", sid, exc_info=True)


# ── deterministic extraction ─────────────────────────────────────────────

_WORKSPACE_TAG_RE = re.compile(r"^\s*\[Workspace::v\d+:[^\]]*\]\s*", re.I)

# Automated user-role messages injected by the runtime (goal continuations,
# delegation results, background-process wakeups, compaction handoffs).
# They are not requests from the human, so the brief's "Your requests"
# section must exclude them. Process wakeups are detected with the canonical
# helper in api.process_event_utils (it also covers the internal wrapper
# envelope); the prefixes below cover the remaining runtime injections and
# older wakeup rows that predate the `_source` marker.
_AUTOMATED_USER_PREFIXES = (
    "[continuing toward your standing goal]",
    "[async delegation batch complete",
    "[important: background process",
    "[internal background event",
    "[context compaction",
    "[prior context",
    "[system]",
    # Task-list / skill markers re-injected around compression boundaries are
    # runtime plumbing, not something Anthony typed (2026-08-18).
    "[your active task list was preserved",
    "[skills pruned during compression",
    "[skill_pruned",
)


def _strip_workspace_tag(text: str) -> str:
    return _WORKSPACE_TAG_RE.sub("", str(text or ""), count=1).lstrip()


def _is_automated_user_text(text: str) -> bool:
    """True when a user-role message was injected by the runtime, not typed."""
    try:
        from api.process_event_utils import is_wakeup_user_text

        if is_wakeup_user_text(text):
            return True
    except Exception:  # pragma: no cover - helper must never break the brief
        logger.debug("context brief: wakeup detection unavailable", exc_info=True)
    stripped = _strip_workspace_tag(text).lstrip()
    head = stripped[:200].lower()
    return any(head.startswith(prefix) for prefix in _AUTOMATED_USER_PREFIXES)


def _is_user_request(msg: dict) -> bool:
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return False
    if msg.get("_squash_summary") is True:
        return False
    if msg.get("_source") in ("process_wakeup", "goal_continuation", "delegation_result"):
        return False
    text = _message_text(msg.get("content")).strip()
    if not text:
        return False
    # Compaction handoffs, goal continuations, delegation batches and
    # process wakeups are runtime plumbing, not user demands.
    if _is_automated_user_text(text):
        return False
    return True


def _excerpt(text: str, cap: int) -> str:
    text = " ".join(str(text or "").split())
    return text[:cap] + ("…" if len(text) > cap else "")


def _conclusion_excerpt(text: str) -> str:
    """First meaningful line of a ``# CONCLUSION`` block."""
    lines = str(text or "").splitlines()
    in_block = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# CONCLUSION"):
            in_block = True
            continue
        if not in_block:
            continue
        if not stripped or stripped == "---":
            continue
        cleaned = stripped.lstrip(">").strip()
        if not cleaned:
            continue
        return _excerpt(cleaned, _CONCLUSION_EXCERPT_CHARS)
    return ""


def _compression_kind(msg: dict) -> str | None:
    if not isinstance(msg, dict):
        return None
    if msg.get("_squash_summary") is True:
        return "squash"
    if "[CONTEXT COMPACTION" in _message_text(msg.get("content")):
        return "compaction"
    return None


def _extract_todos(messages: list) -> dict | None:
    try:
        from api.todo_state import derive_todo_state

        snapshot = derive_todo_state(messages)
    except Exception:
        logger.debug("context brief: todo derivation failed", exc_info=True)
        return None
    if not isinstance(snapshot, dict):
        return None
    items = snapshot.get("todos") or snapshot.get("items")
    if not isinstance(items, list) or not items:
        return None
    counts = {"pending": 0, "in_progress": 0, "completed": 0, "cancelled": 0}
    current = None
    normalized = []
    for item in items:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "pending")
        content = str(item.get("content") or "")
        counts[status] = counts.get(status, 0) + 1
        if current is None and status == "in_progress":
            current = content
        normalized.append({"content": _excerpt(content, 160), "status": status})
    return {"items": normalized, "counts": counts, "current": current}


def _extract_goal(sid: str, session) -> dict | None:
    try:
        from api.goals import goal_state_snapshot
        from api.profiles import get_hermes_home_for_profile

        try:
            profile_home = get_hermes_home_for_profile(getattr(session, "profile", None) or "default")
        except Exception:
            profile_home = None
        state = goal_state_snapshot(sid, profile_home=profile_home)
    except Exception:
        logger.debug("context brief: goal snapshot failed for %s", sid, exc_info=True)
        return None
    if state is None:
        return None
    status = str(getattr(state, "status", "") or "")
    if status not in ("active", "paused"):
        return None
    text = str(getattr(state, "goal", "") or "").strip()
    if not text:
        return None
    return {
        "text": _excerpt(text, 400),
        "status": status,
        "turns_used": getattr(state, "turns_used", None),
        "max_turns": getattr(state, "max_turns", None),
    }


def _extract_in_flight(sid: str, session) -> dict:
    in_flight: dict = {"active": False, "details": {}, "background_tasks": []}
    try:
        busy = _busy_fields(session)
        if busy:
            in_flight["active"] = True
            in_flight["details"] = busy
    except Exception:
        pass
    try:
        from api.background import get_background_tasks

        for task in get_background_tasks(sid) or []:
            if not isinstance(task, dict):
                continue
            status = str(task.get("status") or "")
            if status in ("done", "completed", "failed", "error"):
                continue
            in_flight["background_tasks"].append(
                {
                    "task_id": str(task.get("task_id") or task.get("id") or ""),
                    "status": status or "running",
                    "prompt": _excerpt(task.get("prompt") or "", 120),
                }
            )
    except Exception:
        logger.debug("context brief: background task scan failed for %s", sid, exc_info=True)
    if in_flight["background_tasks"]:
        in_flight["active"] = True
    return in_flight


def build_deterministic_brief(session, sid: str, *, source: str) -> dict:
    messages = _session_messages(session)

    requests = []
    seen_requests: set[str] = set()
    for idx, msg in enumerate(messages):
        if not _is_user_request(msg):
            continue
        # The workspace tag is runtime plumbing prepended to every message;
        # showing it wastes excerpt budget and hides the actual ask.
        text = _excerpt(
            _strip_workspace_tag(_message_text(msg.get("content"))), _REQUEST_EXCERPT_CHARS
        )
        if not text:
            continue
        ts = msg.get("timestamp") or msg.get("_ts")
        # The same turn can be persisted several times (api_content mirror,
        # recovery replay, lineage/state.db merge where parent and child copies
        # carry slightly different timestamps); dedupe on the text alone so one
        # ask shows once.
        key = text
        if key in seen_requests:
            continue
        seen_requests.add(key)
        requests.append({"ts": ts, "text": text, "_idx": idx})

    conclusions = []
    compressions = []
    last_assistant = None
    for idx, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        text = _message_text(msg.get("content")).strip()
        if not text or text == "[[SILENT]]":
            continue
        kind = _compression_kind(msg)
        if kind:
            compressions.append(
                {
                    "ts": msg.get("timestamp") or msg.get("_ts"),
                    "kind": kind,
                    "excerpt": _excerpt(text, _COMPRESSION_EXCERPT_CHARS),
                }
            )
        if "# CONCLUSION" in text:
            excerpt = _conclusion_excerpt(text)
            if excerpt:
                conclusions.append(
                    {"ts": msg.get("timestamp") or msg.get("_ts"), "excerpt": excerpt, "_idx": idx}
                )
        last_assistant = {"ts": msg.get("timestamp") or msg.get("_ts"), "excerpt": _excerpt(text, _LAST_REPLY_EXCERPT_CHARS)}

    # Preserve the mission's ORIGIN under capping: a long session can carry
    # dozens of requests/conclusions, and keeping only the tail would drop the
    # initial ask and its first conclusion — the prompt→conclusion sequence the
    # brief exists to preserve across compactions. Keep the FIRST entry plus
    # the most recent tail when over the cap.
    def _cap_keep_first(items: list, cap: int) -> list:
        if cap <= 0 or len(items) <= cap:
            return items[-cap:] if cap > 0 else []
        return [items[0]] + items[-(cap - 1):]

    # Conversation thread: the panel reads the mission as a dialogue
    # (ask → answer → next ask), so the pairing must be computed here, on the
    # transcript order, not re-derived client-side from two capped lists that
    # each drop different turns. Order on the transcript INDEX, never on the
    # timestamp: replayed/merged copies carry missing or duplicated stamps.
    timeline = sorted(
        [{"role": "request", "ts": r["ts"], "text": r["text"], "_idx": r["_idx"]} for r in requests]
        + [
            {"role": "conclusion", "ts": c["ts"], "text": c["excerpt"], "_idx": c["_idx"]}
            for c in conclusions
        ],
        key=lambda item: item["_idx"],
    )
    timeline = [
        {"role": item["role"], "ts": item["ts"], "text": item["text"]}
        for item in _cap_keep_first(timeline, _TIMELINE_CAP)
    ]

    return {
        "session_id": sid,
        "generated_at": time.time(),
        "meta": {
            "title": getattr(session, "title", None),
            "model": getattr(session, "model", None),
            "workspace": getattr(session, "workspace", None),
            "created_at": getattr(session, "created_at", None),
            "updated_at": getattr(session, "updated_at", None),
            "message_count": len(messages),
            "source": source,
        },
        "goal": _extract_goal(sid, session),
        "todos": _extract_todos(messages),
        "timeline": timeline,
        "requests": [
            {"ts": r["ts"], "text": r["text"]} for r in _cap_keep_first(requests, _REQUEST_CAP)
        ],
        "request_count": len(requests),
        "accomplished": {
            "conclusions": [
                {"ts": c["ts"], "excerpt": c["excerpt"]}
                for c in _cap_keep_first(conclusions, _CONCLUSION_CAP)
            ],
            "conclusion_count": len(conclusions),
            "compressions": compressions[-_COMPRESSION_CAP:],
            "compression_count": len(compressions),
            "last_assistant": last_assistant,
        },
        "in_flight": _extract_in_flight(sid, session),
    }


# ── persisted LLM brief ──────────────────────────────────────────────────

def _session_attr(session, name, default=None):
    if isinstance(session, dict):
        return session.get(name, default)
    return getattr(session, name, default)


def _brief_store_path(session, sid: str) -> Path | None:
    session_path = _session_attr(session, "path")
    if session_path is not None:
        try:
            root = Path(session_path).parent.parent
        except Exception:
            root = None
    else:
        root = None
    if root is None:
        try:
            # Prefer the runtime state dir (what the writer uses for resolved
            # sessions) so dict-shaped probes from the auto worker hit the
            # same store; profile home remains the last-resort fallback.
            from api.config import STATE_DIR
            root = Path(STATE_DIR)
        except Exception:
            root = None
    if root is None:
        try:
            from api.profiles import get_hermes_home_for_profile

            home = get_hermes_home_for_profile(_session_attr(session, "profile") or "default")
            root = Path(home) / "webui"
        except Exception:
            return None
    return root / "context-briefs" / f"{sid}.json"


def load_llm_brief(session, sid: str) -> dict | None:
    path = _brief_store_path(session, sid)
    if path is None or not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("format") != 1:
        return None
    text = str(payload.get("text") or "")
    if not text.strip():
        return None
    return payload


def _session_messages(session) -> list[dict]:
    """Return the DISPLAY transcript for brief building and revision hashing.

    WebUI compression continuations keep older turns in ``pre_compression_snapshot``
    parent sidecars; the child's own ``messages`` array starts at the anchor.
    Building the brief from the child alone loses the ORIGINAL user request and
    every conclusion emitted before the compaction — exactly the sequence the
    brief exists to preserve. Stitch the lineage through the same memoized
    helper the transcript display uses so brief and transcript agree.

    LIVE compaction goes further: the agent runtime persists the pre-compaction
    turns only in state.db — the snapshot parent sidecar often holds just the
    anchor summary (observed 2026-08-17: parent sidecars with 1-3 messages while
    state.db held the full 1,500-row history). The sidecar stitch alone still
    loses the original ask, so lineage sessions additionally merge the state.db
    continuation history (text-deduped, chronologically ordered).

    State.db shims (SimpleNamespace) and forks pass through unchanged — the
    stitcher only follows ``pre_compression_snapshot`` parents. Any stitch or
    state.db failure falls back to the session's own messages (never a hard
    error).
    """
    own = [
        message
        for message in (getattr(session, "messages", None) or [])
        if isinstance(message, dict)
    ]
    if not str(getattr(session, "parent_session_id", "") or "").strip():
        return own
    try:
        from api.routes import _webui_sidecar_lineage_messages_for_display

        stitched = _webui_sidecar_lineage_messages_for_display(session)
    except Exception:  # pragma: no cover - brief must never break on stitch
        logger.debug("context brief: lineage stitch failed", exc_info=True)
        stitched = None
    stitched = [m for m in (stitched or []) if isinstance(m, dict)]
    # Fail closed: the stitched view can only extend the transcript. If it
    # comes back shorter than the child's own messages, distrust it.
    base = stitched if len(stitched) >= len(own) else own
    return _merge_lineage_with_state_db(session, base)


# state.db serializes rich content as a NUL-prefixed JSON envelope (see
# hermes_state._CONTENT_JSON_PREFIX). Decode it so text extraction sees the
# actual blocks instead of the raw envelope string.
_STATE_DB_JSON_PREFIX = "\x00json:"


def _decode_state_db_content(content):
    if isinstance(content, str) and content.startswith(_STATE_DB_JSON_PREFIX):
        try:
            return json.loads(content[len(_STATE_DB_JSON_PREFIX):])
        except ValueError:
            return content
    return content


def _lineage_merge_key(msg: dict) -> tuple:
    text = _message_text(_decode_state_db_content(msg.get("content")))
    normalized = " ".join(_strip_workspace_tag(text).split())
    digest = hashlib.sha1(normalized.encode("utf-8", "replace")).hexdigest()
    return (str(msg.get("role") or ""), digest)


def _merge_lineage_with_state_db(session, base: list[dict]) -> list[dict]:
    """Union the sidecar lineage view with the state.db continuation history.

    Both inputs are chronological within themselves; rows are deduped on
    (role, normalized-text hash) — the state.db mirror of a sidecar turn can
    carry a slightly different timestamp and a ``\\x00json:`` content envelope,
    so timestamp-based keys miss them. Ordering merges the two lists by
    carry-forward timestamp (undated rows inherit their predecessor's ts) with
    the sidecar side winning ties. Any failure returns ``base`` unchanged.
    """
    sid = str(getattr(session, "session_id", "") or "")
    if not sid:
        return base
    try:
        from api.models import get_state_db_session_messages

        db_rows = get_state_db_session_messages(sid, stitch_continuations=True) or []
    except Exception:
        logger.debug("context brief: state.db lineage read failed for %s", sid, exc_info=True)
        return base
    if not db_rows:
        return base
    try:
        seen = {_lineage_merge_key(m) for m in base}
        added: list[dict] = []
        for row in db_rows:
            if not isinstance(row, dict):
                continue
            decoded = row
            content = row.get("content")
            if isinstance(content, str) and content.startswith(_STATE_DB_JSON_PREFIX):
                decoded = dict(row)
                decoded["content"] = _decode_state_db_content(content)
            key = _lineage_merge_key(decoded)
            if key in seen:
                continue
            seen.add(key)
            added.append(decoded)
        if not added:
            return base
        def _effective_ts(rows: list[dict]) -> list[float]:
            last = 0.0
            out = []
            for m in rows:
                ts = m.get("timestamp")
                try:
                    if ts is not None:
                        last = float(ts)
                except (TypeError, ValueError):
                    pass
                out.append(last)
            return out
        base_ts = _effective_ts(base)
        added_ts = _effective_ts(added)
        merged: list[dict] = []
        i = j = 0
        while i < len(base) and j < len(added):
            if base_ts[i] <= added_ts[j]:
                merged.append(base[i]); i += 1
            else:
                merged.append(added[j]); j += 1
        merged.extend(base[i:])
        merged.extend(added[j:])
        return merged
    except Exception:  # pragma: no cover - merge must never break the brief
        logger.debug("context brief: state.db lineage merge failed for %s", sid, exc_info=True)
        return base


def _transcript_revision(session) -> str:
    """Stable content revision for same-count rewrites and state.db sessions."""
    encoded = json.dumps(
        _session_messages(session),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _session_revision(session) -> dict:
    messages = _session_messages(session)
    try:
        updated_at = float(getattr(session, "updated_at", 0.0) or 0.0)
    except (TypeError, ValueError):
        updated_at = 0.0
    return {
        "message_count": len(messages),
        "updated_at": updated_at or None,
        "transcript": _transcript_revision(session),
    }


def _snapshot_session(session):
    """Freeze mutable transcript state before the auxiliary-model call."""
    snapshot = copy.copy(session)
    snapshot.messages = copy.deepcopy(_session_messages(session))
    return snapshot


def _same_transcript_revision(left: dict, right: dict) -> bool:
    return (
        left.get("message_count") == right.get("message_count")
        and left.get("transcript") == right.get("transcript")
    )


def _save_llm_brief(
    session,
    sid: str,
    *,
    text: str,
    source: str,
    message_count: int,
    revision: dict | None = None,
) -> dict | None:
    path = _brief_store_path(session, sid)
    if path is None:
        return None
    revision = dict(revision or _session_revision(session))
    payload = {
        "format": 1,
        "session_id": sid,
        "generated_at": time.time(),
        "source": source,
        "message_count_at_generation": int(message_count),
        "session_updated_at_at_generation": revision.get("updated_at"),
        "transcript_revision_at_generation": revision.get("transcript"),
        "text": text,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    except OSError:
        logger.warning("context brief: persist failed for %s", sid, exc_info=True)
        return None
    return payload


def _llm_brief_is_current(session, payload: dict | None, message_count: int) -> bool:
    """Return whether a stored brief matches the targeted session revision."""
    if not payload:
        return False
    try:
        if int(payload.get("message_count_at_generation") or 0) != int(message_count):
            return False
    except (TypeError, ValueError):
        return False
    stored_transcript_revision = str(
        payload.get("transcript_revision_at_generation") or ""
    ).strip()
    if stored_transcript_revision:
        return stored_transcript_revision == _transcript_revision(session)
    # Legacy payloads without an immutable transcript digest are unverifiable.
    # Message count and mutable ``updated_at`` cannot prove freshness after a
    # same-count rewrite; state.db shims do not even expose updated_at.
    return False


def _persist_revalidated_llm_brief(
    sid: str,
    *,
    expected_generation: int,
    expected_source: str,
    expected_revision: dict,
    text: str,
    brief_source: str,
    message_count: int,
) -> tuple[dict, object, bool]:
    """Revalidate and persist under the route's canonical admission fence."""
    from api.models import _get_session_agent_lock

    with _get_session_agent_lock(sid):
        with _JOBS_LOCK:
            if _SID_GENERATIONS.get(sid, 0) != expected_generation:
                raise BriefError("context brief job cancelled", 409)
        fresh_session, fresh_source = _resolve_session(sid)
        if fresh_source != expected_source:
            raise BriefError("session source changed during brief generation", 409)
        if _session_has_active_run(sid, session=fresh_session):
            raise BriefError("session resumed during brief generation", 409)
        if not _same_transcript_revision(
            expected_revision, _session_revision(fresh_session)
        ):
            raise BriefError("session changed during brief generation", 409)
        payload = _save_llm_brief(
            fresh_session,
            sid,
            text=text,
            source=brief_source,
            message_count=message_count,
            revision=expected_revision,
        )
        if payload is None:
            raise BriefError("brief persistence failed", 500)
        return (
            payload,
            fresh_session,
            _llm_brief_is_current(fresh_session, payload, message_count),
        )


def get_brief_payload(sid: str) -> dict:
    """Deterministic brief + persisted LLM brief annotated with staleness."""
    session, source = _resolve_session(sid)
    brief = build_deterministic_brief(session, sid, source=source)
    llm = load_llm_brief(session, sid)
    if llm is not None:
        current_count = brief["meta"]["message_count"]
        brief["llm_brief"] = {
            "text": llm["text"],
            "generated_at": llm.get("generated_at"),
            "source": llm.get("source"),
            "message_count_at_generation": llm.get("message_count_at_generation"),
            "session_updated_at_at_generation": llm.get("session_updated_at_at_generation"),
            "transcript_revision_at_generation": llm.get(
                "transcript_revision_at_generation"
            ),
            "stale": not _llm_brief_is_current(session, llm, current_count),
        }
    else:
        brief["llm_brief"] = None
    return brief


# ── LLM narrative generation ─────────────────────────────────────────────

_BRIEF_SYSTEM = """Tu es le module de brief de contexte de la WebUI Hermes. Tu rédiges le brief d'une session pour le dirigeant qui rouvre une longue conversation : il doit comprendre en trente secondes ce qu'il a demandé, ce qui a été accompli et ce qu'il reste à faire. Rédige en français, en Markdown, uniquement le brief (aucun préambule ni commentaire).

Structure obligatoire, dans cet ordre exact :
## Demandes
## Accompli
## Reste à faire

Règles :
- « Demandes » : les demandes de l'utilisateur, une puce courte par demande, de la plus ancienne à la plus récente (maximum 8 puces).
- « Accompli » : faits vérifiés uniquement — livraisons, fichiers produits, validations, déploiements visibles dans le transcript ; écris « Rien d'accompli de vérifiable pour l'instant. » si la section est vide.
- « Reste à faire » : travail en cours, prochaines actions explicites, blocages éventuels ; écris « Aucun travail restant identifié. » si la section est vide.
- Ne jamais inventer un fait absent du transcript ; distinguer fait vérifié et intention annoncée.
- Ne jamais inclure de secret, token ou mot de passe ; pas de journaux bruts.
- Au plus 500 mots."""


def _fallback_brief_text(deterministic: dict, reason: str) -> str:
    """Honest deterministic fallback when the auxiliary model is unavailable."""
    lines = [
        f"_Brief automatique de secours ({reason}) — contenu non analysé par un modèle._",
        "",
        "## Demandes",
        "",
    ]
    requests = deterministic.get("requests") or []
    if requests:
        lines.extend(f"- {req['text']}" for req in requests)
    else:
        lines.append("- Aucune demande identifiable.")
    lines.extend(["", "## Accompli", ""])
    conclusions = (deterministic.get("accomplished") or {}).get("conclusions") or []
    if conclusions:
        lines.extend(f"- {c['excerpt']}" for c in conclusions)
    else:
        lines.append("Rien d'accompli de vérifiable pour l'instant.")
    lines.extend(["", "## Reste à faire", ""])
    todos = deterministic.get("todos") or {}
    current = todos.get("current")
    counts = todos.get("counts") or {}
    pending = int(counts.get("pending") or 0) + int(counts.get("in_progress") or 0)
    if current:
        lines.append(f"- En cours : {current}")
    if pending:
        lines.append(f"- {pending} tâche(s) non terminée(s) dans la liste de travail.")
    in_flight = deterministic.get("in_flight") or {}
    if in_flight.get("active"):
        lines.append("- Un traitement est actif ou en arrière-plan au moment de ce brief.")
    if not current and not pending and not in_flight.get("active"):
        lines.append("Aucun travail restant identifié.")
    return "\n".join(lines)


def _generate_llm_brief(session, sid: str, deterministic: dict) -> tuple[str, str]:
    """Return (text, source). source ∈ auxiliary-llm | fallback-template."""
    distilled = _distill_transcript(session)
    title = (deterministic.get("meta") or {}).get("title") or sid
    prompt = (
        f"Session à briefer : titre « {title} », identifiant {sid}, "
        f"{(deterministic.get('meta') or {}).get('message_count', 0)} messages.\n\n"
        f"Transcript distillé (demandes utilisateur, conclusions vérifiées, début et fin) :\n\n"
        f"{distilled}"
    )
    try:
        from agent.auxiliary_client import call_llm

        # Provider, model and effort are resolved canonically by Hermes Agent's
        # auxiliary.compression task; the WebUI must not duplicate that routing.
        response = call_llm(
            task="compression",
            messages=[
                {"role": "system", "content": _BRIEF_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            max_tokens=2048,
            timeout=180,
        )
        text = _extract_llm_content(response)
        if len(text) >= MIN_BRIEF_CHARS:
            return text, "auxiliary-llm"
        logger.warning("context brief from aux model too short (%d chars), falling back", len(text))
        return _fallback_brief_text(deterministic, "réponse du modèle auxiliaire trop courte"), "fallback-template"
    except Exception as exc:
        logger.warning("context brief via auxiliary model failed: %s", exc)
        return _fallback_brief_text(deterministic, "modèle auxiliaire indisponible"), "fallback-template"


# ── job orchestration ────────────────────────────────────────────────────

def start_brief_job(sid: str) -> dict:
    """Start (or refuse to duplicate) the background LLM-narrative job.

    The session resolution I/O happens before the registry lock; the
    duplicate-running check and the registration form a single critical
    section so two concurrent POSTs cannot both enqueue (409 contract).
    """
    # Snapshot the lifecycle generation before session resolution.  If delete
    # overlaps that I/O, registration refuses the stale admission rather than
    # binding the request to whichever lifecycle happens to reuse the SID.
    with _JOBS_LOCK:
        admission_generation = _SID_GENERATIONS.get(sid, 0)
    session, _source = _resolve_session(sid)  # 404 before registering a job
    if not (getattr(session, "messages", None) or []):
        raise BriefError("nothing to brief (session has no messages)")
    job_id = uuid.uuid4().hex[:16]
    job = {
        "job_id": job_id,
        "session_id": sid,
        "status": "running",
        "started_at": time.time(),
        "finished_at": None,
        "result": None,
        "error": None,
    }
    with _JOBS_LOCK:
        _purge_jobs_locked()
        if _SID_GENERATIONS.get(sid, 0) != admission_generation:
            raise BriefError("session changed during context brief admission", 409)
        for existing in _JOBS.values():
            if existing.get("session_id") == sid and existing.get("status") == "running":
                raise BriefError("a context brief job is already running for this session", 409)
        job["_generation"] = admission_generation
        _JOBS[job_id] = job

    thread = threading.Thread(
        target=_run_brief_job, args=(job,), daemon=True, name=f"context-brief-{sid[:12]}"
    )
    job["_thread"] = thread
    try:
        thread.start()
    except BaseException:
        with _JOBS_LOCK:
            if _JOBS.get(job_id) is job:
                _JOBS.pop(job_id, None)
        raise
    return _job_snapshot(job)


def _finish_job(job: dict, *, result: dict | None = None, error: str | None = None) -> None:
    with _JOBS_LOCK:
        # Deletion owns the terminal cancelled state.  A worker returning from
        # a long auxiliary call must not overwrite it with done/error.
        if job.get("status") == "cancelled":
            return
        job["status"] = "done" if error is None else "error"
        job["result"] = result
        job["error"] = error
        job["finished_at"] = time.time()


def _run_brief_job(job: dict) -> None:
    sid = job["session_id"]
    started = time.monotonic()
    try:
        session, source = _resolve_session(sid)
        snapshot = _snapshot_session(session)
        revision = _session_revision(snapshot)
        deterministic = build_deterministic_brief(snapshot, sid, source=source)
        # Provider, model and effort are resolved canonically by the
        # auxiliary.compression task (post-02aefee1); no per-call override here.
        text, brief_source = _generate_llm_brief(snapshot, sid, deterministic)
        payload, _fresh_session, payload_is_current = _persist_revalidated_llm_brief(
            sid,
            expected_generation=job["_generation"],
            expected_source=source,
            expected_revision=revision,
            text=text,
            brief_source=brief_source,
            message_count=deterministic["meta"]["message_count"],
        )
        _finish_job(
            job,
            result={
                "session_id": sid,
                "brief_source": brief_source,
                "brief_chars": len(text),
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "persisted": True,
                "llm_brief": {
                    "text": text,
                    "generated_at": payload.get("generated_at") or time.time(),
                    "source": brief_source,
                    "message_count_at_generation": deterministic["meta"]["message_count"],
                    "session_updated_at_at_generation": payload.get(
                        "session_updated_at_at_generation"
                    ),
                    "transcript_revision_at_generation": payload.get(
                        "transcript_revision_at_generation"
                    ),
                    "stale": not payload_is_current,
                },
            },
        )
    except BriefError as exc:
        _finish_job(job, error=str(exc))
    except Exception as exc:
        logger.exception("context brief job failed for %s", sid)
        _finish_job(job, error=f"internal error: {exc}")


def _session_has_active_run(sid: str, *, session=None) -> bool:
    """True when the run registry or durable admission state is active.

    Fail-closed: any registry error means 'assume active' so a turn in
    progress is never interrupted by a brief job competing for the session.
    """
    try:
        from api import config as _cfg

        with _cfg.ACTIVE_RUNS_LOCK:
            for raw in (_cfg.ACTIVE_RUNS or {}).values():
                if isinstance(raw, dict) and raw.get("session_id") == sid:
                    return True
    except Exception:
        return True
    if session is None:
        try:
            session, _source = _resolve_session(sid)
        except Exception:
            return True
    return bool(
        getattr(session, "active_stream_id", None)
        or getattr(session, "pending_user_message", None)
        or getattr(session, "pending_turn_id", None)
        or getattr(session, "pending_started_at", None)
    )
