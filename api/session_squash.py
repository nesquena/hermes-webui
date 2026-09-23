"""Session squash: collapse an archived, idle WebUI session to one summary.

In-process counterpart of the squash-chat skill (scripts/squash.py), behind
the WebUI squash action. The operation is destructive for the live sidecar,
so it is built as an authority-bound, compare-and-swap transaction:

1. **Selection authority** (``preview_squash``): the target must be an
   archived, writable, idle WebUI session that is its own lineage tip (no
   sealed compression parent, no live descendant). The preview returns an
   immutable authority ``{profile, canonical_path, session_id, lineage_tip,
   source_sha256, message_count}``; ``start_squash_job`` recomputes it and
   refuses, with zero writes, unless the caller echoes it exactly.
2. **Detached authority**: the background job carries that frozen authority
   (not a bare session id), runs under the repository's detached-worker
   profile scope, and is keyed by ``(profile, canonical_path)``.
3. **Cross-process exclusion + CAS**: a per-session ``flock`` serializes
   squash/restore across WebUI processes. Immediately before mutation the
   live sidecar is re-hashed against the confirmed digest, then claimed by an
   atomic rename whose inode/size/mtime must still match the hashed file, and
   the replacement is published with a no-clobber ``os.link``. Any concurrent
   writer therefore makes the squash fail with zero squash writes instead of
   being overwritten.
4. **Transaction**: sidecar, sidebar index and the Agent ``state.db`` barrier
   commit in that order; the in-memory cache is only published after all of
   them succeeded. A failure at any step restores the exact original sidecar
   bytes, index entry, state rows and cache object.
5. **Durable squash generation**: the sidecar records
   ``intentional_shrink_generation`` (understood by startup ``.bak``
   recovery), the truncation watermark/boundary (understood by the
   sidecar/state.db display merge) and the Agent state rows that existed at
   squash time are soft-archived (``active=0, compacted=1``) in one SQLite
   transaction that refuses a live Agent turn lease. The archive manifest
   records the generation and archived row ids, and ``restore_squash``
   reverses the operation atomically after profile/session/digest checks.
"""

from __future__ import annotations

import contextlib
import copy
import gzip
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

try:  # POSIX cross-process lock
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None
try:  # pragma: no cover - Windows only
    import msvcrt as _msvcrt
except ImportError:
    _msvcrt = None

logger = logging.getLogger(__name__)

SQUASH_MARKER_PREFIX = "[CONTEXT COMPACTION — REFERENCE ONLY]\n"
MIN_SUMMARY_CHARS = 400
_DISTILL_BUDGET_CHARS = 100_000
_JOB_TTL_SECONDS = 3600.0
_ARCHIVE_DIR_NAME = "session-squash-archives"
_MANIFEST_FORMAT = 2


class SquashError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ── authority ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SquashAuthority:
    """Immutable authority a squash was confirmed against."""

    profile: str
    canonical_path: str
    session_id: str
    lineage_tip: str
    source_sha256: str
    message_count: int

    def as_dict(self) -> dict:
        return asdict(self)


_AUTHORITY_KEYS = ("profile", "canonical_path", "session_id", "lineage_tip", "source_sha256")


def _normalize_profile(profile) -> str:
    return str(profile or "").strip() or "default"


def _profiles_match(left, right) -> bool:
    from api.profiles import _profiles_match as _match

    return _match(_normalize_profile(left), _normalize_profile(right))


def _canonical_sidecar_path(sid: str) -> Path:
    from api import models

    session_dir = Path(models.SESSION_DIR).resolve()
    path = (session_dir / f"{sid}.json").resolve()
    if path.parent != session_dir:
        raise SquashError("Invalid session id", 400)
    return path


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

def _sha256_and_signature(path: Path) -> tuple[str, tuple]:
    """Hash ``path`` and return the stat identity of the exact file hashed."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        st = os.fstat(fh.fileno())
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest(), _stat_signature(st)


def _sha256(path: Path) -> str:
    return _sha256_and_signature(path)[0]


def _stat_signature(st) -> tuple:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _gzip_payload_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with gzip.open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":  # pragma: no cover - directories cannot be fsynced on Windows
        return
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


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


def _archive_root(sid: str) -> Path:
    from api import models

    return Path(models.SESSION_DIR).resolve().parent / _ARCHIVE_DIR_NAME / sid


def _archive_file(source: Path, archive_dir: Path, source_sha: str, label: str) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    archive_path = archive_dir / f"{label}-{stamp}-{source_sha[:12]}-{uuid.uuid4().hex[:6]}.json.gz"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{archive_path.name}.", suffix=".tmp", dir=archive_dir)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as raw_out:
            with gzip.GzipFile(filename=f"{label}.json", mode="wb", fileobj=raw_out, mtime=0) as gz_out:
                with source.open("rb") as src:
                    shutil.copyfileobj(src, gz_out, length=1024 * 1024)
            raw_out.flush()
            os.fsync(raw_out.fileno())
        os.replace(tmp_path, archive_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    if _gzip_payload_sha256(archive_path) != source_sha:
        archive_path.unlink(missing_ok=True)
        raise SquashError("archive checksum verification failed", 500)
    return archive_path


def _manifest_path_for(archive_path: Path) -> Path:
    return archive_path.with_suffix(archive_path.suffix + ".manifest.json")


def _write_manifest(path: Path, manifest: dict) -> None:
    _atomic_write(path, (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


# ── cross-process exclusion + compare-and-swap ───────────────────────────

@contextlib.contextmanager
def _squash_process_lock(sid: str):
    """Non-blocking cross-process lock for squash/restore of one session."""
    lock_dir = _archive_root(sid)
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / ".squash.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+b", buffering=0) as lock_file:
        if _fcntl is not None:
            try:
                _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            except OSError:
                raise SquashError("another process is squashing or restoring this session", 409) from None
            try:
                yield
            finally:
                _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_UN)
            return
        if _msvcrt is not None:  # pragma: no cover - Windows only
            if os.fstat(lock_file.fileno()).st_size == 0:
                lock_file.write(b"\0")
            lock_file.seek(0)
            try:
                _msvcrt.locking(lock_file.fileno(), _msvcrt.LK_NBLCK, 1)
            except OSError:
                raise SquashError("another process is squashing or restoring this session", 409) from None
            try:
                yield
            finally:
                lock_file.seek(0)
                _msvcrt.locking(lock_file.fileno(), _msvcrt.LK_UNLCK, 1)
            return
        raise SquashError("cross-process squash locking is unavailable", 500)  # pragma: no cover


def _sidecar_authority_lock(sid: str):
    """Shared in-process sidecar authority when the runtime provides one."""
    from api import models

    factory = getattr(models, "_session_sidecar_authority", None)
    return factory(sid) if callable(factory) else contextlib.nullcontext()


class _CasConflict(SquashError):
    def __init__(self, message: str):
        super().__init__(message, 409)


def _cas_hook(_stage: str) -> None:
    """Test seam: called between CAS steps (no-op in production)."""


def _cas_swap(live: Path, expected_sig: tuple, replacement: Path) -> Path:
    """Replace ``live`` by ``replacement`` only if ``live`` is still the file
    identified by ``expected_sig``; never overwrite a concurrent writer.

    Returns the claim path holding the previous live file (caller keeps it
    for rollback, then deletes it). Raises ``_CasConflict`` without leaving
    the replacement published when the authority changed.
    """
    claim = live.with_name(f".{live.name}.squash-claim-{uuid.uuid4().hex[:10]}")
    try:
        os.rename(live, claim)
    except FileNotFoundError:
        raise _CasConflict("session sidecar disappeared before commit") from None
    _cas_hook("claimed")
    if _stat_signature(os.lstat(claim)) != expected_sig:
        # A writer replaced the sidecar after our digest check: give its bytes
        # back unless an even newer writer already republished the path.
        try:
            os.link(claim, live)
            claim.unlink()
        except FileExistsError:
            logger.warning("squash CAS conflict: kept concurrent sidecar copy at %s", claim)
        raise _CasConflict("session changed after confirmation (digest/identity mismatch)")
    try:
        os.link(replacement, live)
    except FileExistsError:
        # Another writer recreated the path in the claim window: it wins.
        claim.unlink(missing_ok=True)
        raise _CasConflict("a concurrent writer published the session during commit") from None
    except OSError:
        # Publication failed: put the original back (no-clobber).
        try:
            os.link(claim, live)
            claim.unlink()
        except FileExistsError:
            claim.unlink(missing_ok=True)
        raise
    _fsync_dir(live.parent)
    return claim


def _restore_claim(live: Path, published_sig: tuple, claim: Path) -> None:
    """Roll back a successful ``_cas_swap``: put ``claim`` back while ``live``
    still is the file we published."""
    try:
        _cas_swap(live, published_sig, claim).unlink(missing_ok=True)
        claim.unlink(missing_ok=True)
    except _CasConflict:
        logger.error(
            "squash rollback: session %s was rewritten concurrently; original bytes kept at %s",
            live.name, claim,
        )
        raise


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

    for i, m in enumerate(messages):
        if m.get("role") == "user":
            if not _append(i, 1200):
                break
    for i, m in enumerate(messages):
        if m.get("role") == "assistant" and "# CONCLUSION" in _message_text(m.get("content")):
            if not _append(i, 3000):
                break
    for i in list(range(min(2, len(messages)))) + list(range(max(0, len(messages) - 4), len(messages))):
        _append(i, 800)
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

Règles : distinguer faits vérifiés et hypothèses ; ne jamais annoncer un déploiement, push, commit ou test sans preuve visible dans le transcript ; conserver les identifiants exacts (session, branche, worktree, SHA) ; écrire « aucune » dans une section vide ; ne jamais inclure de secret, token ou mot de passe ; éviter les journaux bruts et les répétitions ; au moins 400 caractères."""


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


# ── admission ────────────────────────────────────────────────────────────

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

    ``cancel_stream()`` clears the busy fields eagerly while the cancelled
    worker is still unwinding; ``SESSION_WRITEBACK_OWNERS`` survives until the
    worker's own ``finally``. While present, the old worker may still persist
    its pre-squash snapshot, so admission fails closed on it.
    """
    from api.config import session_writeback_owner  # late import: tests patch module attrs

    return session_writeback_owner(sid)


def _has_live_descendant(sid: str) -> bool:
    """True when any WebUI session (fork or compression child) names ``sid``
    as its parent. Squashing such a parent would change the history the
    descendant stitches or points at, so it is refused."""
    from api import models

    return bool(models._has_compression_continuation(type("_S", (), {"session_id": sid})()))


def _state_db_path_for(profile: str) -> Path | None:
    from api import models

    path = Path(models._get_profile_home(profile)) / "state.db"
    return path if path.is_file() else None


def _resolve_lineage_tip(session) -> str:
    """Return the session id itself when it is the authoritative lineage tip;
    otherwise raise. Sealed compression parents (sidecar snapshot flag or
    Agent ``end_reason='compression'``) and parents of live descendants are
    not tips."""
    sid = str(session.session_id)
    if getattr(session, "pre_compression_snapshot", False):
        raise SquashError("session is a sealed compression snapshot — squash its continuation instead", 409)
    try:
        from api.compression_continuation import durable_compression_continuation

        sealed, tip = durable_compression_continuation(session)
    except Exception:
        sealed, tip = False, None
    if sealed:
        detail = f" (continuation {tip})" if tip else ""
        raise SquashError(f"session is a sealed compression parent{detail} — not the lineage tip", 409)
    if _has_live_descendant(sid):
        raise SquashError("session has live descendant sessions (fork or continuation) — squash refused", 409)
    return sid


def _admit(session, *, expected_profile: str | None = None) -> None:
    if getattr(session, "read_only", False):
        raise SquashError("read-only sessions cannot be squashed", 400)
    if not getattr(session, "archived", False):
        raise SquashError("only archived sessions can be squashed — archive the conversation first", 409)
    if expected_profile is not None and not _profiles_match(getattr(session, "profile", None), expected_profile):
        raise SquashError("session profile changed since confirmation", 409)
    if _busy_fields(session):
        raise SquashError("session is active (stream or pending turn) — stop it before squashing", 409)
    if _unreleased_writeback_owner(session.session_id):
        raise SquashError(
            "session writeback is still owned by a finishing turn — retry once it has unwound",
            409,
        )


def _compute_authority(session) -> SquashAuthority:
    sid = str(session.session_id)
    canonical = _canonical_sidecar_path(sid)
    if Path(session.path).resolve() != canonical:
        raise SquashError("session sidecar is not at its canonical path", 409)
    if not canonical.is_file():
        raise SquashError("session sidecar not found on disk", 404)
    lineage_tip = _resolve_lineage_tip(session)
    sha, _sig = _sha256_and_signature(canonical)
    try:
        persisted = json.loads(canonical.read_text(encoding="utf-8"))
        count = len(persisted.get("messages") or [])
    except (OSError, ValueError):
        raise SquashError("session sidecar is unreadable", 409) from None
    return SquashAuthority(
        profile=_normalize_profile(getattr(session, "profile", None)),
        canonical_path=str(canonical),
        session_id=sid,
        lineage_tip=lineage_tip,
        source_sha256=sha,
        message_count=count,
    )


def preview_squash(sid: str, *, request_profile: str | None) -> dict:
    """Read-only: validate the target and return the authority to confirm."""
    from api.models import get_session

    try:
        meta = get_session(sid, metadata_only=True)
    except KeyError:
        raise SquashError("Session not found", 404) from None
    if request_profile is not None and not _profiles_match(getattr(meta, "profile", None), request_profile):
        raise SquashError("Session belongs to a different profile", 409)
    _admit(meta)
    return _compute_authority(meta).as_dict()


def _confirmed_authority(sid: str, confirm) -> dict:
    if not isinstance(confirm, dict):
        raise SquashError("confirm must echo the squash preview authority")
    missing = [key for key in _AUTHORITY_KEYS if not confirm.get(key)]
    if missing:
        raise SquashError(f"confirm is missing {', '.join(missing)}")
    if confirm.get("session_id") != sid:
        raise SquashError("confirm.session_id does not match session_id")
    return {key: str(confirm[key]) for key in _AUTHORITY_KEYS}


# ── job orchestration ────────────────────────────────────────────────────

def start_squash_job(sid: str, *, confirm, summary: str | None, request_profile: str | None) -> dict:
    from api.models import get_session  # late import: tests patch module attrs

    _purge_jobs()
    confirmed = _confirmed_authority(sid, confirm)
    try:
        meta = get_session(sid, metadata_only=True)
    except KeyError:
        raise SquashError("Session not found", 404) from None
    if request_profile is not None and not _profiles_match(getattr(meta, "profile", None), request_profile):
        raise SquashError("Session belongs to a different profile", 409)
    _admit(meta)
    authority = _compute_authority(meta)
    current = {key: str(getattr(authority, key)) for key in _AUTHORITY_KEYS}
    if current != confirmed:
        changed = sorted(key for key in _AUTHORITY_KEYS if current[key] != confirmed[key])
        raise SquashError(f"session changed since confirmation ({', '.join(changed)}) — preview again", 409)

    job_key = (authority.profile, authority.canonical_path)
    job_id = uuid.uuid4().hex[:16]
    job = {
        "job_id": job_id,
        "session_id": sid,
        "profile": authority.profile,
        "title": getattr(meta, "title", None),
        "status": "running",
        "started_at": time.time(),
        "finished_at": None,
        "result": None,
        "error": None,
        "_authority": authority,
        "_key": job_key,
    }
    with _JOBS_LOCK:
        for other in _JOBS.values():
            if other.get("_key") == job_key and other.get("status") == "running":
                raise SquashError("a squash job is already running for this session", 409)
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
    authority: SquashAuthority = job["_authority"]
    try:
        from api.profiles import profile_scope_for_detached_worker

        with profile_scope_for_detached_worker(authority.profile, purpose="session squash"):
            result = _run_squash_authorized(job, authority, provided_summary)
        _finish_job(job, result=result)
    except SquashError as exc:
        _finish_job(job, error=str(exc))
    except Exception as exc:
        logger.exception("session squash job failed for %s", authority.session_id)
        _finish_job(job, error=f"internal error: {exc}")


def _run_squash_authorized(job: dict, authority: SquashAuthority, provided_summary: str | None) -> dict:
    from api.models import get_session
    from api.session_ops import _live_active_stream_id
    from api.routes import _get_session_agent_lock, _publish_session_list_changed

    sid = authority.session_id
    started = time.monotonic()
    lock = _get_session_agent_lock(sid)
    if not lock.acquire(timeout=0.5):
        raise SquashError("session is busy (a turn is running) — retry once it is idle", 409)
    squash_owner_token = f"squash-{job['job_id']}"
    try:
        session = get_session(sid)
        if _live_active_stream_id(session):
            raise SquashError("session is active (stream or pending turn) — stop it before squashing", 409)
        # Re-check the full admission under the per-session lock: the request
        # pre-check ran without it.
        _admit(session, expected_profile=authority.profile)
        messages = session.messages or []
        if len(messages) == 1 and isinstance(messages[0], dict) and messages[0].get("_squash_summary") is True:
            return {"session_id": sid, "already_squashed": True}
        if not messages:
            raise SquashError("nothing to squash (session has no messages)")
        summary, summary_source = _generate_summary(session, sid, provided_summary)
        # In-process tombstone: take the writeback-ownership slot for the
        # mutation so an ownership-gated finalizer fails closed against it.
        from api.config import register_session_writeback_owner
        register_session_writeback_owner(sid, squash_owner_token)
        with _squash_process_lock(sid), _sidecar_authority_lock(sid):
            stats = _commit_squash(session, authority, summary)
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

    _after_commit(sid, authority.profile, "session_squash", _publish_session_list_changed)
    return {
        "session_id": sid,
        "already_squashed": False,
        "summary_source": summary_source,
        "summary_chars": len(summary),
        "elapsed_seconds": round(time.monotonic() - started, 2),
        **stats,
    }


def _after_commit(sid: str, profile: str, reason: str, publish) -> None:
    # Provider I/O (boundary memory commit) must not hold the mutation lock.
    try:
        from api.config import _evict_session_agent
        _evict_session_agent(sid)
    except Exception:
        logger.warning("squash: agent eviction failed for %s", sid, exc_info=True)
    try:
        publish(reason, profile=profile, session_id=sid)
    except Exception:
        logger.warning("squash: session-list publish failed for %s", sid, exc_info=True)


# ── staging ──────────────────────────────────────────────────────────────

def _stage_session_file(session, staged_path: Path) -> None:
    """Serialize ``session`` with the canonical ``Session.save`` format into
    ``staged_path`` without touching the live sidecar or the index."""
    base = type(session)
    staged_cls = type(f"_Staged{base.__name__}", (base,), {"path": property(lambda _self: staged_path)})
    staged = copy.copy(session)
    staged.__class__ = staged_cls
    staged.save(touch_updated_at=False, skip_index=True)


def _squashed_copy(session, summary: str, generation: str, now: float) -> tuple[object, str | None]:
    """Return a detached copy of ``session`` carrying the squash result.

    The cached object is never mutated here; it is only replaced after the
    whole transaction committed.
    """
    from api.models import get_session

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

    squashed = copy.copy(session)
    squashed.messages = [visible_message]
    squashed.context_messages = [context_message]
    squashed.tool_calls = []
    squashed.active_stream_id = None
    squashed.active_checkpoint = None
    squashed.pending_turn_id = None
    squashed.pending_user_message = None
    squashed.pending_attachments = []
    squashed.pending_started_at = None
    squashed.pending_user_source = None
    # Only a pre-compression snapshot parent is stitched back into the
    # display (it would resurrect the archived transcript). A fork parent is
    # an independent conversation and keeps its "Forked from" link.
    detached_parent = None
    parent_sid = getattr(session, "parent_session_id", None)
    if parent_sid:
        try:
            parent = get_session(parent_sid, metadata_only=True)
            if getattr(parent, "pre_compression_snapshot", False):
                detached_parent = parent_sid
        except Exception:
            detached_parent = None
    if detached_parent:
        squashed.parent_session_id = None
    squashed.anchor_activity_scenes = {}
    squashed.compression_anchor_visible_idx = 0
    squashed.compression_anchor_message_key = {
        "role": "assistant",
        "ts": now,
        "text": summary[:160],
        "attachments": 0,
    }
    squashed.compression_anchor_summary = summary[:1000]
    squashed.compression_anchor_mode = "manual"
    squashed.truncation_watermark = now
    squashed.truncation_boundary = now
    # Durable squash generation: startup .bak recovery treats a live
    # uuid4-hex generation the backup lacks as an intentional shrink.
    squashed.intentional_shrink_generation = generation
    # A new squash starts a new state.db projection generation. Reset any
    # projection authority inherited from an earlier squash of this session
    # (fields exist when the #6600 projection contract is present).
    for field in ("squash_projection_generation", "squash_projection_cutoff",
                  "squash_projection_superseded_by"):
        if hasattr(squashed, field):
            setattr(squashed, field, None)
    squashed.updated_at = now
    return squashed, detached_parent


def _verify_squashed_payload(path: Path, *, generation: str, now: float) -> None:
    try:
        persisted = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SquashError(f"post-squash verification failed (unreadable sidecar: {exc})", 500) from exc
    ok = (
        len(persisted.get("messages") or []) == 1
        and (persisted.get("messages") or [{}])[0].get("_squash_summary") is True
        and len(persisted.get("context_messages") or []) == 1
        and persisted.get("compression_anchor_mode") == "manual"
        and persisted.get("truncation_watermark") == now
        and persisted.get("truncation_boundary") == now
        and persisted.get("intentional_shrink_generation") == generation
        and persisted.get("active_stream_id") is None
        and persisted.get("pending_user_message") is None
    )
    if not ok:
        raise SquashError("post-squash verification failed (persisted state mismatch)", 500)


# ── index / cache / state barrier ────────────────────────────────────────

def _index_entry(sid: str) -> dict | None:
    from api import models

    try:
        entries = json.loads(Path(models.SESSION_INDEX_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(entries, list):
        return None
    return next((e for e in entries if isinstance(e, dict) and e.get("session_id") == sid), None)


def _write_index_for(session) -> None:
    from api import models

    models._write_session_index(updates=[session])


def _verify_index(sid: str, expected_count: int) -> None:
    entry = _index_entry(sid)
    if entry is None or entry.get("message_count") != expected_count:
        raise SquashError("sidebar index verification failed", 500)


def _state_barrier_hook(_stage: str) -> None:
    """Test seam around the state.db barrier (no-op in production)."""


def _apply_state_barrier(sid: str, profile: str) -> dict:
    """Soft-archive the Agent state rows of ``sid`` in one IMMEDIATE txn.

    Refuses (zero writes) while a live Agent turn lease owns the
    conversation, so no in-flight writer can deliver a delayed row across the
    squash. Rows stay on disk (``active=0, compacted=1``, the Agent's own
    compaction marking) and their ids are recorded for restore.
    """
    db_path = _state_db_path_for(profile)
    if db_path is None:
        return {"state_barrier": "no-state-db", "state_archived_row_ids": []}
    _state_barrier_hook("before")
    conn = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
        if not {"id", "session_id", "active", "compacted"}.issubset(cols):
            return {"state_barrier": "unsupported-schema", "state_archived_row_ids": []}
        conn.execute("BEGIN IMMEDIATE")
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "session_turn_leases" in tables:
                keys = _lease_keys(conn, sid, tables)
                now = time.time()
                live = conn.execute(
                    f"SELECT conversation_id FROM session_turn_leases WHERE expires_at > ? "
                    f"AND conversation_id IN ({','.join('?' * len(keys))})",
                    (now, *keys),
                ).fetchone()
                if live is not None:
                    raise SquashError("an Agent turn currently owns this conversation — retry once it is idle", 409)
            ids = [int(r["id"]) for r in conn.execute(
                "SELECT id FROM messages WHERE session_id = ? AND active = 1 ORDER BY id", (sid,))]
            if ids:
                conn.execute(
                    f"UPDATE messages SET active = 0, compacted = 1 WHERE session_id = ? "
                    f"AND id IN ({','.join('?' * len(ids))})",
                    (sid, *ids),
                )
            _state_barrier_hook("before-commit")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return {"state_barrier": "applied", "state_archived_row_ids": ids}


def _lease_keys(conn, sid: str, tables: set) -> list[str]:
    keys = [sid]
    if "sessions" not in tables:
        return keys
    seen = {sid}
    current = sid
    for _ in range(64):
        row = conn.execute("SELECT parent_session_id FROM sessions WHERE id = ?", (current,)).fetchone()
        parent = row["parent_session_id"] if row else None
        if not parent or parent in seen:
            break
        prow = conn.execute("SELECT end_reason FROM sessions WHERE id = ?", (parent,)).fetchone()
        if not prow or prow["end_reason"] != "compression":
            break
        keys.append(parent)
        seen.add(parent)
        current = parent
    return keys


def _reactivate_state_rows(sid: str, profile: str, ids: list[int]) -> int:
    """Undo a state barrier: re-activate exactly the recorded rows that are
    still in the barrier's archived state."""
    if not ids:
        return 0
    db_path = _state_db_path_for(profile)
    if db_path is None:
        raise SquashError("state.db disappeared; cannot restore archived state rows", 500)
    conn = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            restored = 0
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                restored += conn.execute(
                    f"UPDATE messages SET active = 1, compacted = 0 WHERE session_id = ? "
                    f"AND active = 0 AND compacted = 1 AND id IN ({','.join('?' * len(chunk))})",
                    (sid, *chunk),
                ).rowcount
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return restored


def _publish_cache(sid: str, session_obj) -> None:
    from api import models

    with models.LOCK:
        models.SESSIONS[sid] = session_obj


def _cache_snapshot(sid: str):
    from api import models

    with models.LOCK:
        return sid in models.SESSIONS, models.SESSIONS.get(sid)


def _restore_cache(sid: str, snapshot) -> None:
    from api import models

    present, obj = snapshot
    with models.LOCK:
        if present:
            models.SESSIONS[sid] = obj
        else:
            models.SESSIONS.pop(sid, None)


# ── commit ───────────────────────────────────────────────────────────────

def _commit_squash(session, authority: SquashAuthority, summary: str) -> dict:
    """Transactional squash. Caller holds the agent lock, the squash process
    lock and the sidecar authority."""
    from api.models import Session

    sid = authority.session_id
    live = Path(authority.canonical_path)
    if Path(session.path).resolve() != live:
        raise SquashError("session sidecar is not at its canonical path", 409)

    # 1. CAS precondition: the live bytes are exactly the confirmed ones.
    current_sha, live_sig = _sha256_and_signature(live)
    if current_sha != authority.source_sha256:
        raise SquashError("session changed since confirmation (digest mismatch) — preview again", 409)

    before_count = authority.message_count
    before_bytes = live_sig[2]
    cache_snapshot = _cache_snapshot(sid)
    index_before = _index_entry(sid)
    archive_dir = _archive_root(sid)
    generation = uuid.uuid4().hex
    now = time.time()

    archive_path = _archive_file(live, archive_dir, current_sha, sid)
    manifest_path = _manifest_path_for(archive_path)
    staged_path = live.with_name(f".{live.name}.squash-staged-{generation[:10]}")
    claim: Path | None = None
    published_sig: tuple | None = None
    index_written = False
    state: dict = {"state_barrier": "not-run", "state_archived_row_ids": []}
    committed = False
    try:
        squashed, detached_parent = _squashed_copy(session, summary, generation, now)
        _stage_session_file(squashed, staged_path)
        _verify_squashed_payload(staged_path, generation=generation, now=now)
        staged_sha, _ = _sha256_and_signature(staged_path)
        manifest = {
            "format": _MANIFEST_FORMAT,
            "session_id": sid,
            "profile": authority.profile,
            "source_name": live.name,
            "source_sha256": current_sha,
            "source_bytes": before_bytes,
            "source_message_count": before_count,
            "lineage_tip": authority.lineage_tip,
            "archive_name": archive_path.name,
            "squash_generation": generation,
            "squash_cutoff": now,
            "squashed_sha256": staged_sha,
            "detached_parent_session_id": detached_parent,
            "state_barrier": "pending",
            "state_archived_row_ids": [],
            "created_at": now,
        }
        _write_manifest(manifest_path, manifest)

        # 2. Sidecar: claim + verify identity + no-clobber publish.
        claim = _cas_swap(live, live_sig, staged_path)
        staged_path.unlink(missing_ok=True)
        published_sig = _stat_signature(os.lstat(live))
        _verify_squashed_payload(live, generation=generation, now=now)
        if _sha256(live) != staged_sha:
            raise SquashError("post-squash verification failed (published digest mismatch)", 500)

        # 3. Sidebar index, from a fresh load of the published bytes.
        fresh = Session.load(sid)
        if fresh is None:
            raise SquashError("post-squash verification failed (reload)", 500)
        index_written = True
        _write_index_for(fresh)
        _verify_index(sid, 1)

        # 4. Durable state barrier (atomic, refuses a live Agent turn lease).
        state = _apply_state_barrier(sid, authority.profile)

        # 5. Finalize the manifest with what restore needs.
        manifest.update(state)
        _write_manifest(manifest_path, manifest)

        # 6. Publish the cache only now that every durable step committed.
        _publish_cache(sid, fresh)
        committed = True
    finally:
        if not committed:
            _rollback_squash(
                sid=sid,
                profile=authority.profile,
                live=live,
                claim=claim,
                published_sig=published_sig,
                staged_path=staged_path,
                archive_path=archive_path,
                manifest_path=manifest_path,
                index_written=index_written,
                index_before=index_before,
                original_session=session,
                cache_snapshot=cache_snapshot,
                state=state,
            )
    if claim is not None:
        claim.unlink(missing_ok=True)
    try:
        live.with_suffix(".json.bak").unlink(missing_ok=True)
    except OSError:
        logger.warning("session squash could not remove stale backup for %s", sid, exc_info=True)

    return {
        "before": {"message_count": before_count, "bytes": before_bytes},
        "after": {"message_count": 1, "bytes": live.stat().st_size, "sha256": staged_sha},
        "original_sha256": current_sha,
        "squash_generation": generation,
        "state_barrier": state.get("state_barrier"),
        "state_archived_rows": len(state.get("state_archived_row_ids") or []),
        "archive_path": str(archive_path),
        "archive_name": archive_path.name,
        "manifest_path": str(manifest_path),
    }


def _rollback_squash(*, sid, profile, live, claim, published_sig, staged_path, archive_path,
                     manifest_path, index_written, index_before, original_session,
                     cache_snapshot, state) -> None:
    """Best-effort exact rollback; every step is attempted and logged."""
    staged_path.unlink(missing_ok=True)
    ids = state.get("state_archived_row_ids") or []
    if state.get("state_barrier") == "applied" and ids:
        try:
            _reactivate_state_rows(sid, profile, ids)
        except Exception:
            logger.error("squash rollback: state rows not reactivated for %s", sid, exc_info=True)
    sidecar_restored = claim is None
    if claim is not None and published_sig is not None:
        try:
            _restore_claim(live, published_sig, claim)
            sidecar_restored = True
        except Exception:
            logger.error("squash rollback: sidecar not restored for %s (original at %s)", sid, claim, exc_info=True)
    if index_written:
        try:
            if index_before is not None:
                _write_index_for(original_session)
            else:
                from api.models import prune_session_from_index
                prune_session_from_index(sid)
        except Exception:
            logger.error("squash rollback: index not restored for %s", sid, exc_info=True)
    _restore_cache(sid, cache_snapshot)
    if sidecar_restored:
        # Zero net writes: the archive of an uncommitted squash is dropped.
        archive_path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)


# ── restore ──────────────────────────────────────────────────────────────

def restore_squash(sid: str, *, archive_name: str, confirm, request_profile: str | None) -> dict:
    """Atomically restore the transcript a squash archived.

    ``confirm`` must carry ``session_id``, ``source_sha256`` (the archived
    original) and ``current_sha256`` (the live squashed sidecar). The live
    sidecar must still be exactly the squash result of that archive (same
    generation, single summary message), so no post-squash turn is lost.
    """
    from api.models import get_session
    from api.routes import _get_session_agent_lock, _publish_session_list_changed

    if not isinstance(confirm, dict) or confirm.get("session_id") != sid:
        raise SquashError("confirm.session_id does not match session_id")
    for key in ("source_sha256", "current_sha256"):
        if not confirm.get(key):
            raise SquashError(f"confirm is missing {key}")
    name = str(archive_name or "")
    if not name or Path(name).name != name or not name.endswith(".json.gz"):
        raise SquashError("invalid archive_name")
    archive_dir = _archive_root(sid)
    archive_path = archive_dir / name
    manifest_path = _manifest_path_for(archive_path)
    if not archive_path.is_file() or not manifest_path.is_file():
        raise SquashError("archive not found", 404)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SquashError("archive manifest is unreadable", 409) from None
    if manifest.get("session_id") != sid or manifest.get("archive_name") != name:
        raise SquashError("archive manifest does not belong to this session", 409)
    if not manifest.get("squash_generation"):
        raise SquashError("archive manifest has no squash generation (legacy archive — use the squash-chat skill)", 409)
    profile = _normalize_profile(manifest.get("profile"))
    if request_profile is not None and not _profiles_match(profile, request_profile):
        raise SquashError("archive belongs to a different profile", 409)
    if manifest.get("source_sha256") != confirm["source_sha256"]:
        raise SquashError("archive digest does not match confirmation", 409)

    try:
        meta = get_session(sid, metadata_only=True)
    except KeyError:
        raise SquashError("Session not found", 404) from None
    if not _profiles_match(getattr(meta, "profile", None), profile):
        raise SquashError("session profile does not match the archive", 409)

    lock = _get_session_agent_lock(sid)
    if not lock.acquire(timeout=0.5):
        raise SquashError("session is busy (a turn is running) — retry once it is idle", 409)
    try:
        session = get_session(sid)
        if _busy_fields(session) or _unreleased_writeback_owner(sid):
            raise SquashError("session is active — stop it before restoring", 409)
        with _squash_process_lock(sid), _sidecar_authority_lock(sid):
            result = _commit_restore(session, manifest, archive_path, profile, confirm)
    finally:
        try:
            lock.release()
        except RuntimeError:
            pass
    _after_commit(sid, profile, "session_squash_restore", _publish_session_list_changed)
    return result


def _commit_restore(session, manifest: dict, archive_path: Path, profile: str, confirm: dict) -> dict:
    from api.models import Session

    sid = manifest["session_id"]
    live = _canonical_sidecar_path(sid)
    current_sha, live_sig = _sha256_and_signature(live)
    if current_sha != confirm["current_sha256"]:
        raise SquashError("session changed since confirmation (digest mismatch)", 409)
    try:
        persisted = json.loads(live.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SquashError("live sidecar is unreadable", 409) from None
    messages = persisted.get("messages") or []
    if not (
        persisted.get("intentional_shrink_generation") == manifest["squash_generation"]
        and len(messages) == 1
        and isinstance(messages[0], dict)
        and messages[0].get("_squash_summary") is True
    ):
        raise SquashError("live session is no longer the untouched result of this squash — restore refused", 409)
    if _gzip_payload_sha256(archive_path) != manifest["source_sha256"]:
        raise SquashError("archive payload digest mismatch", 409)

    cache_snapshot = _cache_snapshot(sid)
    index_before = _index_entry(sid)
    token = uuid.uuid4().hex[:10]
    staged_path = live.with_name(f".{live.name}.restore-staged-{token}")
    with gzip.open(archive_path, "rb") as src, staged_path.open("wb") as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())
    # Keep the squashed state restorable as well (skill-compatible layout).
    compact_archive = _archive_file(live, archive_path.parent, current_sha, f"{sid}-squashed")
    claim = None
    published_sig = None
    index_written = False
    reactivated = 0
    committed = False
    try:
        if _sha256(staged_path) != manifest["source_sha256"]:
            raise SquashError("staged restore digest mismatch", 500)
        claim = _cas_swap(live, live_sig, staged_path)
        staged_path.unlink(missing_ok=True)
        published_sig = _stat_signature(os.lstat(live))
        if _sha256(live) != manifest["source_sha256"]:
            raise SquashError("restored sidecar digest mismatch", 500)
        restored = Session.load(sid)
        if restored is None:
            raise SquashError("restored sidecar failed to load", 500)
        index_written = True
        _write_index_for(restored)
        _verify_index(sid, int(manifest.get("source_message_count") or len(restored.messages or [])))
        reactivated = _reactivate_state_rows(sid, profile, list(manifest.get("state_archived_row_ids") or []))
        _publish_cache(sid, restored)
        committed = True
    finally:
        if not committed:
            staged_path.unlink(missing_ok=True)
            if claim is not None and published_sig is not None:
                try:
                    _restore_claim(live, published_sig, claim)
                except Exception:
                    logger.error("restore rollback: squashed sidecar kept at %s", claim, exc_info=True)
            if index_written:
                try:
                    if index_before is not None:
                        _write_index_for(session)
                    else:
                        from api.models import prune_session_from_index
                        prune_session_from_index(sid)
                except Exception:
                    logger.error("restore rollback: index not restored for %s", sid, exc_info=True)
            _restore_cache(sid, cache_snapshot)
            compact_archive.unlink(missing_ok=True)
    if claim is not None:
        claim.unlink(missing_ok=True)
    return {
        "session_id": sid,
        "restored_message_count": len(restored.messages or []),
        "restored_sha256": manifest["source_sha256"],
        "state_rows_reactivated": reactivated,
        "squashed_archive_path": str(compact_archive),
    }
