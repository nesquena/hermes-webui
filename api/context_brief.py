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

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from api.session_squash import (
    _atomic_write,
    _busy_fields,
    _distill_transcript,
    _extract_llm_content,
    _message_text,
)

logger = logging.getLogger(__name__)

MIN_BRIEF_CHARS = 200
_JOB_TTL_SECONDS = 3600.0

_REQUEST_CAP = 8
_REQUEST_EXCERPT_CHARS = 240
_CONCLUSION_CAP = 5
_CONCLUSION_EXCERPT_CHARS = 220
_COMPRESSION_CAP = 6
_COMPRESSION_EXCERPT_CHARS = 160
_LAST_REPLY_EXCERPT_CHARS = 280


class BriefError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ── job registry (LLM narrative refresh) ─────────────────────────────────

_JOBS: dict[str, dict] = {}
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
        if job.get("status") in ("done", "error") and job.get("finished_at", 0) < cutoff
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

    try:
        return get_session(sid), "webui"
    except KeyError:
        pass
    messages = get_cli_session_messages(sid)
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


def delete_stored_brief(state_root, sid: str) -> None:
    """Best-effort removal of a stored LLM brief when its session is deleted.

    Called from POST /api/session/delete: the brief is a derivable display
    cache (transcript excerpts + LLM narrative) and must not outlive the
    session it summarizes. Never raises — deletion of the session itself is
    the authoritative operation.
    """
    try:
        (Path(state_root) / "context-briefs" / f"{sid}.json").unlink(missing_ok=True)
    except Exception:
        logger.debug("failed to remove context brief for deleted session %s", sid, exc_info=True)


# ── deterministic extraction ─────────────────────────────────────────────

def _is_user_request(msg: dict) -> bool:
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return False
    if msg.get("_squash_summary") is True:
        return False
    if msg.get("_source") == "process_wakeup":
        return False
    text = _message_text(msg.get("content")).strip()
    if not text:
        return False
    # Compaction handoffs are reference blocks, not user demands.
    if text.startswith("[CONTEXT COMPACTION"):
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
    messages = [m for m in (getattr(session, "messages", None) or []) if isinstance(m, dict)]

    requests = []
    for msg in messages:
        if _is_user_request(msg):
            requests.append(
                {
                    "ts": msg.get("timestamp") or msg.get("_ts"),
                    "text": _excerpt(_message_text(msg.get("content")), _REQUEST_EXCERPT_CHARS),
                }
            )

    conclusions = []
    compressions = []
    last_assistant = None
    for msg in messages:
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
                conclusions.append({"ts": msg.get("timestamp") or msg.get("_ts"), "excerpt": excerpt})
        last_assistant = {"ts": msg.get("timestamp") or msg.get("_ts"), "excerpt": _excerpt(text, _LAST_REPLY_EXCERPT_CHARS)}

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
        "requests": requests[-_REQUEST_CAP:],
        "request_count": len(requests),
        "accomplished": {
            "conclusions": conclusions[-_CONCLUSION_CAP:],
            "conclusion_count": len(conclusions),
            "compressions": compressions[-_COMPRESSION_CAP:],
            "compression_count": len(compressions),
            "last_assistant": last_assistant,
        },
        "in_flight": _extract_in_flight(sid, session),
    }


# ── persisted LLM brief ──────────────────────────────────────────────────

def _brief_store_path(session, sid: str) -> Path | None:
    session_path = getattr(session, "path", None)
    if session_path is not None:
        try:
            root = Path(session_path).parent.parent
        except Exception:
            root = None
    else:
        root = None
    if root is None:
        try:
            from api.profiles import get_hermes_home_for_profile

            home = get_hermes_home_for_profile(getattr(session, "profile", None) or "default")
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


def _save_llm_brief(session, sid: str, *, text: str, source: str, message_count: int) -> dict | None:
    path = _brief_store_path(session, sid)
    if path is None:
        return None
    payload = {
        "format": 1,
        "session_id": sid,
        "generated_at": time.time(),
        "source": source,
        "message_count_at_generation": int(message_count),
        "text": text,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    except OSError:
        logger.warning("context brief: persist failed for %s", sid, exc_info=True)
        return None
    return payload


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
            "stale": llm.get("message_count_at_generation") != current_count,
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
        for existing in _JOBS.values():
            if existing.get("session_id") == sid and existing.get("status") == "running":
                raise BriefError("a context brief job is already running for this session", 409)
        _JOBS[job_id] = job

    thread = threading.Thread(
        target=_run_brief_job, args=(job,), daemon=True, name=f"context-brief-{sid[:12]}"
    )
    job["_thread"] = thread
    thread.start()
    return _job_snapshot(job)


def _finish_job(job: dict, *, result: dict | None = None, error: str | None = None) -> None:
    with _JOBS_LOCK:
        job["status"] = "done" if error is None else "error"
        job["result"] = result
        job["error"] = error
        job["finished_at"] = time.time()


def _run_brief_job(job: dict) -> None:
    sid = job["session_id"]
    started = time.monotonic()
    try:
        session, source = _resolve_session(sid)
        deterministic = build_deterministic_brief(session, sid, source=source)
        text, brief_source = _generate_llm_brief(session, sid, deterministic)
        payload = _save_llm_brief(
            session,
            sid,
            text=text,
            source=brief_source,
            message_count=deterministic["meta"]["message_count"],
        )
        _finish_job(
            job,
            result={
                "session_id": sid,
                "brief_source": brief_source,
                "brief_chars": len(text),
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "persisted": payload is not None,
                "llm_brief": {
                    "text": text,
                    "generated_at": (payload or {}).get("generated_at") or time.time(),
                    "source": brief_source,
                    "message_count_at_generation": deterministic["meta"]["message_count"],
                    "stale": False,
                },
            },
        )
    except BriefError as exc:
        _finish_job(job, error=str(exc))
    except Exception as exc:
        logger.exception("context brief job failed for %s", sid)
        _finish_job(job, error=f"internal error: {exc}")
