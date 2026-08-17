"""Session squash: collapse an idle WebUI session to one verified summary.

In-process counterpart of the squash-chat skill (scripts/squash.py), so the
WebUI can offer a one-click squash button:

- archive the original sidecar (gzip + manifest, layout compatible with the
  skill's ``restore`` command),
- mutate the Session object: a single visible summary message plus a single
  ``[CONTEXT COMPACTION]`` context marker, manual compression anchor,
  truncation watermark/boundary barriers against state.db replay, and
  fork/compression lineage detach (a parent lets display stitching resurrect
  the archived transcript, defeating the squash),
- save() atomically (canonical serialization + index update + #1558 .bak),
- remove the stale .bak so startup recovery cannot undo the intentional
  shrink (the #4836 intentional-compress guard also recognizes the manual
  anchor, belt-and-suspenders),
- evict the cached agent; the mutated object stays in SESSIONS so the next
  read serves the squashed state immediately.

The squash runs as a background job (auxiliary-LLM summary generation can
take minutes on long transcripts) polled via GET /api/session/squash/status.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

SQUASH_MARKER_PREFIX = "[CONTEXT COMPACTION — REFERENCE ONLY]\n"
MIN_SUMMARY_CHARS = 400
_DISTILL_BUDGET_CHARS = 100_000
_JOB_TTL_SECONDS = 3600.0


class SquashError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ── job registry ─────────────────────────────────────────────────────────

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


def _job_snapshot(job: dict) -> dict:
    return {k: v for k, v in job.items() if not k.startswith("_")}


def squash_job_status(job_id: str) -> dict | None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        return _job_snapshot(job) if job else None


def _purge_jobs() -> None:
    cutoff = time.time() - _JOB_TTL_SECONDS
    with _JOBS_LOCK:
        stale = [
            jid for jid, job in _JOBS.items()
            if job.get("status") in ("done", "error") and job.get("finished_at", 0) < cutoff
        ]
        for jid in stale:
            _JOBS.pop(jid, None)


# ── checksums / archive (ported from squash-chat scripts/squash.py) ──────

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _gzip_payload_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with gzip.open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _archive_original(session_path: Path, archive_dir: Path, original_sha: str) -> tuple[Path, Path]:
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    archive_path = archive_dir / f"{session_path.stem}-{stamp}-{original_sha[:12]}.json.gz"
    manifest_path = archive_path.with_suffix(archive_path.suffix + ".manifest.json")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{archive_path.name}.", suffix=".tmp", dir=archive_dir)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as raw_out:
            with gzip.GzipFile(filename=session_path.name, mode="wb", fileobj=raw_out, mtime=0) as gz_out:
                with session_path.open("rb") as source:
                    shutil.copyfileobj(source, gz_out, length=1024 * 1024)
            raw_out.flush()
            os.fsync(raw_out.fileno())
        os.replace(tmp_path, archive_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    manifest = {
        "format": 1,
        "session_id": session_path.stem,
        "source_name": session_path.name,
        "source_sha256": original_sha,
        "source_bytes": session_path.stat().st_size,
        "archive_name": archive_path.name,
        "created_at": time.time(),
    }
    _atomic_write(manifest_path, (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return archive_path, manifest_path


# ── summary generation ───────────────────────────────────────────────────

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


_SUMMARY_SYSTEM = """Tu es le module de compaction de conversations Hermes WebUI. Tu rédiges la synthèse d'une session qui deviendra l'UNIQUE message visible et la seule base de reprise du modèle. Rédige en français, en Markdown, uniquement la synthèse (aucun préambule ni commentaire).

Structure obligatoire :
# Synthèse — <titre de la session> — session <session_id>
## 1. Objet et résultat
## 2. État exact
## 3. Décisions validées
## 4. Sources de vérité
## 5. Mutations effectuées
## 6. Validations réelles
## 7. Risques et limites
## 8. Prochaine action
## 9. Commandes de reprise

Règles : distinguer faits vérifiés et hypothèses ; ne jamais annoncer un déploiement, push, commit ou test sans preuve visible dans le transcript ; conserver les identifiants exacts (session, branche, worktree, SHA) ; écrire « aucune » dans une section vide ; ne jamais inclure de secret, token ou mot de passe ; éviter les journaux bruts et les répétitions ; au moins 400 caractères. Pour une session MES, rappeler si pertinent : approbation du plan ≠ acceptation du candidat ; tests verts ≠ autorisation de production."""


def _extract_llm_content(response) -> str:
    message = response.choices[0].message
    if isinstance(message, dict):
        content = message.get("content")
    else:
        content = getattr(message, "content", message)
    if not isinstance(content, str):
        content = str(content) if content else ""
    return content.strip()


def _fallback_summary(session, sid: str, reason: str) -> str:
    messages = [m for m in (session.messages or []) if isinstance(m, dict)]
    title = getattr(session, "title", None) or sid
    first_user = next((_message_text(m.get("content")).strip() for m in messages if m.get("role") == "user"), "")
    last_assistant = next((_message_text(m.get("content")).strip() for m in reversed(messages) if m.get("role") == "assistant"), "")
    created = getattr(session, "created_at", None)
    updated = getattr(session, "updated_at", None)

    def _fmt_ts(ts) -> str:
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
        except (TypeError, ValueError, OSError):
            return "inconnue"

    def _clip(text: str, cap: int) -> str:
        text = " ".join(text.split())
        return text[:cap] + ("…" if len(text) > cap else "")

    return (
        f"# Synthèse — {title} — session {sid}\n\n"
        f"## 1. Objet et résultat\n\n"
        f"Synthèse automatique de secours ({reason}) : le contenu n'a pas été analysé par un modèle. "
        f"Session de {len(messages)} messages, créée le {_fmt_ts(created)}, dernière activité le {_fmt_ts(updated)}.\n\n"
        f"Premier message utilisateur : « {_clip(first_user, 500) or 'indisponible'} »\n\n"
        f"Dernier message assistant : « {_clip(last_assistant, 500) or 'indisponible'} »\n\n"
        f"## 2. État exact\n\nInconnu — synthèse de secours sans analyse du transcript.\n\n"
        f"## 3. Décisions validées\n\nAucune identifiable sans analyse ; ne présumer d'aucune validation.\n\n"
        f"## 4. Sources de vérité\n\nWorkspace : {getattr(session, 'workspace', None) or 'inconnu'}. "
        f"Historique intégral archivé (voir le rapport du squash).\n\n"
        f"## 5. Mutations effectuées\n\nInconnues.\n\n"
        f"## 6. Validations réelles\n\nInconnues.\n\n"
        f"## 7. Risques et limites\n\nCette synthèse n'est PAS fiable pour reprendre un travail : "
        f"elle n'a pas été générée par un modèle. Restaurer l'archive ou consulter l'historique avant toute reprise critique.\n\n"
        f"## 8. Prochaine action\n\nAucune déterminée — relire l'archive si une reprise est nécessaire.\n\n"
        f"## 9. Commandes de reprise\n\nAucune."
    )


def _generate_summary(session, sid: str, provided: str | None) -> tuple[str, str]:
    """Return (summary_text, source). source ∈ provided | auxiliary-llm | fallback-template."""
    if isinstance(provided, str) and len(provided.strip()) >= MIN_SUMMARY_CHARS:
        return provided.strip(), "provided"
    distilled = _distill_transcript(session)
    title = getattr(session, "title", None) or sid
    prompt = (
        f"Session à compacter : titre « {title} », identifiant {sid}, "
        f"workspace {getattr(session, 'workspace', None) or 'inconnu'}, "
        f"{len(session.messages or [])} messages.\n\n"
        f"Transcript distillé (demandes utilisateur, conclusions vérifiées, début et fin) :\n\n"
        f"{distilled}"
    )
    try:
        from agent.auxiliary_client import call_llm
        response = call_llm(
            task="compression",
            messages=[
                {"role": "system", "content": _SUMMARY_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            max_tokens=4096,
            timeout=180,
        )
        text = _extract_llm_content(response)
        if len(text) >= MIN_SUMMARY_CHARS:
            return text, "auxiliary-llm"
        logger.warning("squash summary from aux model too short (%d chars), falling back", len(text))
        return _fallback_summary(session, sid, "réponse du modèle auxiliaire trop courte"), "fallback-template"
    except Exception as exc:
        logger.warning("squash summary via auxiliary model failed: %s", exc)
        return _fallback_summary(session, sid, "modèle auxiliaire indisponible"), "fallback-template"


# ── apply ────────────────────────────────────────────────────────────────

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


def _unreleased_writeback_owner(sid: str) -> str | None:
    """Return the stream that still OWNS the session's writeback, if any.

    Busy indicators are not sufficient admission evidence: cancel_stream()
    eagerly clears ``active_stream_id`` and the pending_* fields so the UI can
    accept a follow-up turn while the cancelled worker is still unwinding. The
    ``SESSION_WRITEBACK_OWNERS`` record (#6623 re-gate) deliberately survives
    that cleanup — it is released only by the owning worker's own ``finally``,
    after its last possible save. While the record exists, the old worker may
    still persist its pre-squash snapshot, which would silently restore the
    archived transcript after the squash reported success. Squash admission
    must therefore fail closed on the ownership record, not on busy fields.
    """
    from api.config import session_writeback_owner  # late import: tests patch module attrs

    return session_writeback_owner(sid)


def _apply_squash(session, sid: str, summary: str) -> dict:
    """Archive, mutate, save, verify. Caller holds the per-session agent lock."""
    session_path = session.path
    before_count = len(session.messages or [])
    before_bytes = session_path.stat().st_size
    original_sha = _sha256(session_path)
    archive_root = session_path.parent.parent / "session-squash-archives" / sid
    archive_path, manifest_path = _archive_original(session_path, archive_root, original_sha)
    if _gzip_payload_sha256(archive_path) != original_sha:
        archive_path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        raise SquashError("archive checksum verification failed", 500)

    now = time.time()
    visible_message = {
        "id": f"squash-{int(now * 1_000_000)}",
        "role": "assistant",
        "content": summary,
        "timestamp": now,
        "_ts": now,
        "_squash_summary": True,
    }
    context_message = dict(visible_message)
    context_message["content"] = SQUASH_MARKER_PREFIX + summary

    session.messages = [visible_message]
    session.context_messages = [context_message]
    session.tool_calls = []
    session.active_stream_id = None
    session.active_checkpoint = None
    session.pending_turn_id = None
    session.pending_user_message = None
    session.pending_attachments = []
    session.pending_started_at = None
    session.pending_user_source = None
    # A squashed transcript is a new standalone display/context root. Any
    # parent (fork or pre-compression snapshot) lets WebUI stitch the archived
    # lineage back in before the _squash_summary marker is noticed, defeating
    # the squash and hanging the browser on "Loading conversation".
    session.parent_session_id = None
    session.anchor_activity_scenes = {}
    session.compression_anchor_visible_idx = 0
    session.compression_anchor_message_key = {
        "role": "assistant",
        "ts": now,
        "text": summary[:160],
        "attachments": 0,
    }
    session.compression_anchor_summary = summary[:1000]
    session.compression_anchor_mode = "manual"
    session.truncation_watermark = now
    session.truncation_boundary = now
    session.updated_at = now
    session.save()

    # Persisted-state verification (read back from disk, mirroring the clear
    # route), then drop the stale #1558 .bak so startup recovery can never
    # resurrect the pre-squash transcript.
    try:
        persisted = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SquashError(
            f"post-squash verification failed (unreadable sidecar: {exc})", 500
        ) from exc
    ok = (
        len(persisted.get("messages") or []) == 1
        and (persisted.get("messages") or [{}])[0].get("_squash_summary") is True
        and len(persisted.get("context_messages") or []) == 1
        and persisted.get("compression_anchor_mode") == "manual"
        and persisted.get("truncation_watermark") == now
        and persisted.get("truncation_boundary") == now
        and persisted.get("parent_session_id") is None
        and persisted.get("active_stream_id") is None
        and persisted.get("pending_user_message") is None
    )
    if not ok:
        raise SquashError("post-squash verification failed (persisted state mismatch)", 500)
    try:
        session_path.with_suffix(".json.bak").unlink(missing_ok=True)
    except OSError:
        logger.warning("session squash could not remove stale backup for %s", sid, exc_info=True)

    return {
        "before": {"message_count": before_count, "bytes": before_bytes},
        "after": {"message_count": 1, "bytes": session_path.stat().st_size},
        "original_sha256": original_sha,
        "archive_path": str(archive_path),
        "manifest_path": str(manifest_path),
    }


# ── job orchestration ────────────────────────────────────────────────────

def start_squash_job(sid: str, *, confirm_session_id: str | None, summary: str | None) -> dict:
    from api.models import get_session  # late import: tests patch module attrs

    _purge_jobs()
    if confirm_session_id != sid:
        raise SquashError("confirm_session_id does not match session_id")
    with _JOBS_LOCK:
        for job in _JOBS.values():
            if job.get("session_id") == sid and job.get("status") == "running":
                raise SquashError("a squash job is already running for this session", 409)

    try:
        meta = get_session(sid, metadata_only=True)
    except KeyError:
        raise SquashError("Session not found", 404) from None
    if getattr(meta, "read_only", False):
        raise SquashError("read-only sessions cannot be squashed", 400)
    if _busy_fields(meta):
        raise SquashError("session is active (stream or pending turn) — stop it before squashing", 409)
    if _unreleased_writeback_owner(sid):
        raise SquashError(
            "session writeback is still owned by a finishing turn — retry once it has unwound",
            409,
        )

    job_id = uuid.uuid4().hex[:16]
    job = {
        "job_id": job_id,
        "session_id": sid,
        "title": getattr(meta, "title", None),
        "status": "running",
        "started_at": time.time(),
        "finished_at": None,
        "result": None,
        "error": None,
    }
    with _JOBS_LOCK:
        _JOBS[job_id] = job

    thread = threading.Thread(target=_run_squash_job, args=(job, summary), daemon=True,
                              name=f"session-squash-{sid[:12]}")
    job["_thread"] = thread
    thread.start()
    return _job_snapshot(job)


def _finish_job(job: dict, *, result: dict | None = None, error: str | None = None) -> None:
    with _JOBS_LOCK:
        job["status"] = "done" if error is None else "error"
        job["result"] = result
        job["error"] = error
        job["finished_at"] = time.time()


def _run_squash_job(job: dict, provided_summary: str | None) -> None:
    sid = job["session_id"]
    started = time.monotonic()
    try:
        from api.models import get_session
        from api.session_ops import _live_active_stream_id
        from api.routes import _get_session_agent_lock, _publish_session_list_changed

        lock = _get_session_agent_lock(sid)
        acquired = lock.acquire(timeout=0.5)
        if not acquired:
            raise SquashError("session is busy (a turn is running) — retry once it is idle", 409)
        squash_owner_token = f"squash-{job['job_id']}"
        try:
            session = get_session(sid)
            if _live_active_stream_id(session) or _busy_fields(session):
                raise SquashError("session is active (stream or pending turn) — stop it before squashing", 409)
            # P1 (Greptile, #6704): cancelled writeback must NOT survive squash.
            # cancel_stream() clears the live/busy indicators above eagerly,
            # BEFORE the cancelled worker relinquishes writeback ownership. If
            # the squash were admitted on those indicators alone, the old
            # worker could later save its pre-squash snapshot and silently
            # restore the archived transcript. The ownership record is released
            # only by the owning worker's own ``finally`` — after its last
            # possible save — so a present record means a writer may still
            # save, and admission must fail closed (re-checked here under the
            # per-session agent lock; the start_squash_job pre-check is only a
            # fast path).
            owner = _unreleased_writeback_owner(sid)
            if owner:
                raise SquashError(
                    "session writeback is still owned by a finishing turn — retry once it has unwound",
                    409,
                )
            messages = session.messages or []
            if len(messages) == 1 and isinstance(messages[0], dict) and messages[0].get("_squash_summary") is True:
                _finish_job(job, result={"session_id": sid, "already_squashed": True})
                return
            if not messages:
                raise SquashError("nothing to squash (session has no messages)")
            summary, summary_source = _generate_summary(session, sid, provided_summary)
            # Tombstone (in-process exclusion): take the writeback-ownership
            # slot for the duration of the mutation, so any ownership-gated
            # finalizer that fires concurrently fails closed against the
            # squash token instead of matching a stale/absent record. A
            # successor turn admitted after the squash simply replaces the
            # entry; our release below is conditional and never clobbers it.
            from api.config import register_session_writeback_owner
            register_session_writeback_owner(sid, squash_owner_token)
            stats = _apply_squash(session, sid, summary)
        finally:
            try:
                from api.config import clear_session_writeback_owner_if_owned
                clear_session_writeback_owner_if_owned(sid, squash_owner_token)
            except Exception:
                logger.warning("squash: writeback-owner release failed for %s", sid, exc_info=True)
            try:
                lock.release()
            except RuntimeError:
                pass

        # Evict the cached agent OUTSIDE the per-session lock (provider I/O for
        # boundary memory commits must not hold the mutation lock). The mutated
        # session object stays in SESSIONS — it IS the squashed state.
        try:
            from api.config import _evict_session_agent
            _evict_session_agent(sid)
        except Exception:
            logger.warning("squash: agent eviction failed for %s", sid, exc_info=True)
        try:
            _publish_session_list_changed(
                "session_squash",
                profile=getattr(session, "profile", None),
                session_id=sid,
            )
        except Exception:
            logger.warning("squash: session-list publish failed for %s", sid, exc_info=True)

        _finish_job(job, result={
            "session_id": sid,
            "already_squashed": False,
            "summary_source": summary_source,
            "summary_chars": len(summary),
            "elapsed_seconds": round(time.monotonic() - started, 2),
            **stats,
        })
    except SquashError as exc:
        _finish_job(job, error=str(exc))
    except Exception as exc:
        logger.exception("session squash job failed for %s", sid)
        _finish_job(job, error=f"internal error: {exc}")
