"""Idea-mode flow: session flag, ``/idea`` command, and idea capture (spec t_14ad8e89 §3).

Idea mode is a per-chat-session server-side flag. While ON, the client's
``send()`` (static/messages.js) does NOT start an agent turn — it POSTs the
utterance here instead. Read-back suppression is therefore STRUCTURAL: no
agent turn ever runs, so the only "assistant" output is the one short
confirmation line the filing stage returns (✅ / 🔁 / 🤷 / 💾, spec §3.4).

Capture accepts two shapes at ``POST /api/idea/capture``:

* ``multipart/form-data`` — a voice clip (``file`` + ``session_id``) that is
  transcribed server-side via ``tools.transcription_tools.transcribe_audio``
  (the same STT path ``/api/transcribe`` uses) and then filtered through
  ``tools.voice_mode_transcript.is_whisper_hallucination`` — the filter is
  NOT applied by ``transcribe_audio`` itself (spec §1 correction), so this
  module applies it explicitly and rejects matches as empty transcripts.
* JSON — ``{"session_id", "transcript"}`` for typed ideas (and for transcripts
  the browser already STT'd through the existing mic path).

Both shapes funnel into :func:`capture_idea`, which emits the spec §5 idea
payload (``{"session_id","source","webui"…}``) and delegates the filing to
``api.idea_filing.file_ideas`` (sibling module — imported lazily so the
webui still boots when only one of the two PRs is deployed).
"""

from __future__ import annotations

import hashlib
import tempfile
import threading
import time
from pathlib import Path

from api.helpers import bad, j, read_body
from api.models import get_session
from api.upload import (
    _read_multipart_or_reject,
    _reject_invisible_session,
    _reject_before_read,
    _sanitize_upload_name,
    _RejectBeforeRead,
)

# The filing stage lives in api/idea_filing.py (sibling task t_d21da9cd).
# Resolved lazily: the module is optional at boot, and tests stub it.
_FILING_MODULE = "api.idea_filing"

IDEA_SOURCE = "webui"
IDEA_BOARD = "ideas"

# Session idle auto-off (spec §3.5: "/idea off (or session idle > 30 min)
# ends the mode"). Enforced lazily on every read of the flag.
IDLE_TIMEOUT_S = 30 * 60

# Confirmation-line budget (spec §3.4). Server texts stay under this.
STATS_KEYS = ("filed", "dupes", "failed", "queued")

_SESSIONS: dict[str, dict] = {}
_LOCK = threading.Lock()


# ── Session-flag store ──────────────────────────────────────────────────────


def _now() -> float:
    return time.time()


def _new_state(session_id: str) -> dict:
    return {
        "session_id": session_id,
        "idea_mode": True,
        "started_at": _now(),
        "last_active": _now(),
        "stats": {key: 0 for key in STATS_KEYS},
    }


def _expire_if_stale(session_id: str, state: dict) -> None:
    if state.get("idea_mode") and _now() - float(state.get("last_active") or 0) > IDLE_TIMEOUT_S:
        _SESSIONS.pop(session_id, None)


def get_state(session_id: str) -> dict:
    """Return a COPY of the session's idea-mode state (all-off shape when absent)."""
    sid = str(session_id or "")
    with _LOCK:
        state = _SESSIONS.get(sid)
        if state is not None:
            _expire_if_stale(sid, state)
            state = _SESSIONS.get(sid)
        if state is None:
            return {
                "session_id": sid,
                "idea_mode": False,
                "started_at": None,
                "stats": {key: 0 for key in STATS_KEYS},
            }
        out = dict(state)
        out["stats"] = dict(state.get("stats") or {})
        return out


def is_idea_mode(session_id: str) -> bool:
    return bool(get_state(session_id).get("idea_mode"))


def set_idea_mode(session_id: str, on: bool) -> dict:
    sid = str(session_id or "")
    with _LOCK:
        if not on:
            state = _SESSIONS.pop(sid, None)
            if state is None:
                return {
                    "session_id": sid,
                    "idea_mode": False,
                    "started_at": None,
                    "stats": {key: 0 for key in STATS_KEYS},
                }
            out = dict(state)
            out["stats"] = dict(state.get("stats") or {})
            out["idea_mode"] = False
            return out
        state = _SESSIONS.get(sid)
        if state is None or not state.get("idea_mode"):
            _SESSIONS[sid] = _new_state(sid)
        else:
            state["last_active"] = _now()
        out = dict(_SESSIONS[sid])
        out["stats"] = dict(_SESSIONS[sid].get("stats") or {})
        return out


def _bump(session_id: str, counter: str, amount: int = 1) -> None:
    with _LOCK:
        state = _SESSIONS.get(session_id)
        if state is not None and counter in state["stats"]:
            state["stats"][counter] += amount
        if state is not None:
            state["last_active"] = _now()


def summary_line(state: dict) -> str:
    """One-line end-of-mode summary (spec §3.5): ``3 filed, 1 dupe, 0 lost. Board: ideas.``"""
    stats = state.get("stats") or {}
    filed = int(stats.get("filed") or 0)
    dupes = int(stats.get("dupes") or 0)
    lost = int(stats.get("failed") or 0)
    return f"{filed} filed, {dupes} dupe{'' if dupes == 1 else 's'}, {lost} lost. Board: {IDEA_BOARD}."


# ── /idea command handling (typed in the chat box; client calls the mode route) ──


def handle_idea_command(session_id: str, arg: str) -> dict:
    """Handle ``/idea on|off|status`` semantics. Returns a payload whose
    ``text`` is the one-line reply rendered into the chat."""
    arg = str(arg or "").strip().lower()
    if arg not in ("on", "off", "status"):
        return {"ok": False, "error": "usage: /idea on|off|status"}

    if arg == "on":
        state = set_idea_mode(session_id, True)
        return {"ok": True, "idea_mode": True, "state": state,
                "text": f"💡 Idea mode on — speak or type ideas. Board: {IDEA_BOARD}."}

    if arg == "off":
        state = set_idea_mode(session_id, False)
        return {"ok": True, "idea_mode": False, "state": state, "text": summary_line(state)}

    state = get_state(session_id)
    if state.get("idea_mode"):
        stats = state.get("stats") or {}
        text = (f"Idea mode on: {int(stats.get('filed') or 0)} filed, "
                f"{int(stats.get('dupes') or 0)} dupe, {int(stats.get('queued') or 0)} queued.")
    else:
        text = "Idea mode off."
    return {"ok": True, "idea_mode": bool(state.get("idea_mode")), "state": state, "text": text}


# ── STT + hallucination filter (spec §2) ───────────────────────────────────


def _is_hallucination(transcript: str) -> bool:
    """Whisper-hallucination check via the agent's own filter (spec §1
    correction: ``transcribe_audio`` does NOT filter these itself)."""
    try:
        from tools.voice_mode_transcript import is_whisper_hallucination
    except ImportError:
        return False
    try:
        return bool(is_whisper_hallucination(transcript))
    except Exception:
        return False


def _clip_suffix(audio_name: str | None) -> str:
    """Temp-file suffix for a voice clip (spec §3.2).

    ``transcribe_audio`` dispatches on the file EXTENSION (its validator
    rejects an empty one), so the clip must keep its upload suffix —
    same pattern as the /api/transcribe handler (upload.py).
    """
    try:
        safe_name = _sanitize_upload_name(str(audio_name or ""))
    except ValueError:
        return ".webm"
    return Path(safe_name).suffix or ".webm"


def _transcribe_file(temp_path: str) -> tuple[str, str | None]:
    """Transcribe one audio file. Returns ``(transcript, error)``; a
    hallucination match on silence yields ``("", None)`` — i.e. an EMPTY
    transcript that flows to the filing stage's 🤷 path, never a card."""
    try:
        from tools.transcription_tools import transcribe_audio
    except ImportError:
        return "", "Speech-to-text is unavailable on this server"
    result = transcribe_audio(temp_path)
    if not result.get("success"):
        return "", str(result.get("error") or "Transcription failed")
    transcript = str(result.get("transcript") or "").strip()
    if transcript and _is_hallucination(transcript):
        return "", None
    return transcript, None


# ── Capture pipeline (spec §3.2–§3.4 + §5 contract) ─────────────────────────


def build_payload(session_id: str, transcript: str, audio_sha16: str | None = None,
                  attempts: int = 0, ts: float | None = None) -> dict:
    """The idea payload contract (spec §5) — the capture stage's exit
    criterion. Key order is part of the contract; do not reorder."""
    return {
        "session_id": str(session_id or ""),
        "source": IDEA_SOURCE,
        "transcript": str(transcript or ""),
        "ts": float(ts if ts is not None else _now()),
        "audio_sha16": audio_sha16,
        "attempts": int(attempts or 0),
    }


def _load_filer():
    import importlib

    module = importlib.import_module(_FILING_MODULE)
    return module.file_ideas


def _stat_for_kind(kind: str, card_count: int) -> tuple[str, int] | None:
    if kind == "filed":
        return "filed", max(1, card_count)
    if kind == "dupe":
        return "dupes", 1
    if kind == "spooled":
        return "queued", 1
    return None  # nothing_heard is not a failure — nothing was captured


def capture_idea(session_id: str, transcript: str | None = None, *,
                 audio_bytes: bytes | None = None, audio_sha16: str | None = None,
                 audio_name: str | None = None,
                 attempts: int = 0, ts: float | None = None) -> dict:
    """Capture one utterance end-to-end. Never starts an agent turn.

    Returns the JSON payload for the HTTP layer:
    ``{"ok","kind","text","card_ids","idea","filing","stats"}`` where
    ``idea`` is the exact §5 contract payload emitted for the filing stage.
    ``kind``/``text`` come from the filing result; STT failure surfaces as
    ``{"ok": False, "kind": "stt_error", "error": ...}``.
    """
    if not is_idea_mode(session_id):
        return {"ok": False, "kind": "mode_off", "error": "idea mode is off"}

    if audio_bytes is not None and not transcript:
        digest = hashlib.sha256(audio_bytes).hexdigest()[:16]
        audio_sha16 = audio_sha16 or digest
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(prefix="webui-idea-",
                                             suffix=_clip_suffix(audio_name),
                                             delete=False) as tmp:
                temp_path = tmp.name
                tmp.write(audio_bytes)
            transcript, stt_error = _transcribe_file(temp_path)
        finally:
            if temp_path:
                try:
                    Path(temp_path).unlink(missing_ok=True)
                except Exception:
                    pass
        if stt_error:
            _bump(session_id, "failed")
            return {"ok": False, "kind": "stt_error", "error": stt_error}

    payload = build_payload(session_id, transcript or "", audio_sha16=audio_sha16,
                            attempts=attempts, ts=ts)

    try:
        file_ideas = _load_filer()
    except ImportError as exc:
        return {"ok": False, "kind": "filing_unavailable", "error": f"idea filing unavailable: {exc}"}

    result = file_ideas(payload)
    filing = result.to_payload() if hasattr(result, "to_payload") else dict(result or {})
    kind = str(getattr(result, "kind", "") or "")
    text = str(getattr(result, "text", "") or "")
    card_ids = list(getattr(result, "card_ids", []) or [])

    stat = _stat_for_kind(kind, len(card_ids))
    if stat is not None:
        _bump(session_id, stat[0], stat[1])
    else:
        _bump(session_id, "filed", 0)  # touch last_active only

    # Spec §0/§6.6: a spooled result arms the in-session retry loop. The
    # precheck is cheap (one cached import + spool stat), so arming it on
    # EVERY capture also gives the lazy drain: the first capture after a
    # board restore retries what earlier rounds left spooled.
    _schedule_spool_retry()

    return {
        "ok": bool(getattr(result, "ok", False)),
        "kind": kind,
        "text": text,
        "card_ids": card_ids,
        "idea": payload,
        "filing": filing,
        "stats": get_state(session_id).get("stats"),
    }


# ── HTTP handlers ───────────────────────────────────────────────────────────

_SPOOL_RETRY_LOCK = threading.Lock()
_SPOOL_RETRY_ARMED = False


def _schedule_spool_retry() -> None:
    """Arm the in-session spool retry (spec §0, §6.6) — at most one runner.

    ``retry_spooled`` lives in the filing stage and runs its own 5 s/30 s/2 m
    schedule on a DAEMON thread (never on the request thread — the first
    round sleeps before it tries the board); anything still spooled after
    the last round stays queued for the external batch intake lane (never
    dropped). Cheap enough to call from every capture: a runner is only
    started while records sit in the spool, and never two at once — so the
    first capture after a board restore also drains what earlier rounds
    left spooled.
    """
    global _SPOOL_RETRY_ARMED
    try:
        from api.idea_filing import retry_spooled, spool_has_records
    except ImportError:
        return  # filing stage absent — the batch intake lane owns the spool

    try:
        with _SPOOL_RETRY_LOCK:
            if _SPOOL_RETRY_ARMED or not spool_has_records():
                return
            _SPOOL_RETRY_ARMED = True
    except Exception:  # spool unreadable — treat as empty, never 500 a capture
        return

    def _run() -> None:
        global _SPOOL_RETRY_ARMED
        try:
            retry_spooled()
        except Exception as exc:  # never let the retry loop kill the session
            print(f"[webui] idea spool retry error: {exc}", flush=True)
        finally:
            with _SPOOL_RETRY_LOCK:
                _SPOOL_RETRY_ARMED = False

    threading.Thread(target=_run, name="webui-idea-spool-retry",
                     daemon=True).start()


def handle_idea_capture(handler):
    """POST /api/idea/capture — multipart voice clip OR JSON typed idea.

    Dispatched BEFORE routes.py's ``read_body`` (the request may be
    multipart), mirroring the /api/upload + /api/transcribe block.
    """
    import traceback as _tb

    try:
        content_type = str(handler.headers.get("Content-Type") or "")
        if content_type.startswith("multipart/"):
            try:
                fields, files = _read_multipart_or_reject(handler)
            except _RejectBeforeRead as rejection:  # type: ignore[misc]
                _reject_before_read(handler, rejection.message, rejection.status)  # type: ignore[misc]
                return True
            session_id = str(fields.get("session_id") or "")
            if not session_id:
                return bad(handler, "session_id is required")
            try:
                session = get_session(session_id)
            except KeyError:
                return bad(handler, "Session not found", status=404)
            if _reject_invisible_session(handler, session):
                return True
            if "file" not in files:
                return bad(handler, "No file field in request")
            filename, file_bytes = files["file"]
            if not file_bytes:
                return bad(handler, "Empty audio clip")
            result = capture_idea(session_id, audio_bytes=file_bytes,
                                  audio_sha16=None, audio_name=filename)
        else:
            body = read_body(handler)
            session_id = str(body.get("session_id") or "")
            transcript = str(body.get("transcript") or "").strip()
            if not session_id:
                return bad(handler, "session_id is required")
            if not transcript:
                return bad(handler, "transcript is required")
            try:
                session = get_session(session_id)
            except KeyError:
                return bad(handler, "Session not found", status=404)
            if _reject_invisible_session(handler, session):
                return True
            result = capture_idea(session_id, transcript)

        kind = str(result.get("kind") or "")
        if not result.get("ok") and kind == "mode_off":
            return j(handler, {"error": result.get("error") or "idea mode is off",
                               "idea_mode": False}, status=409)
        if not result.get("ok") and kind == "stt_error":
            status = 503 if "unavailable" in str(result.get("error", "")).lower() else 400
            return j(handler, {"error": result.get("error"), "kind": kind}, status=status)
        if not result.get("ok") and kind == "filing_unavailable":
            return j(handler, {"error": result.get("error"), "kind": kind}, status=503)
        return j(handler, result)
    except ValueError as e:
        return bad(handler, str(e))
    except Exception:
        print("[webui] idea capture error: " + _tb.format_exc(), flush=True)
        return bad(handler, "Idea capture failed", status=500)


def handle_idea_mode(handler, body):
    """POST /api/idea/mode — {"session_id", "on": bool} (or "off": bool)."""
    session_id = str((body or {}).get("session_id") or "")
    if not session_id:
        return bad(handler, "session_id is required")
    body = body or {}
    if "on" not in body and "off" not in body:
        return bad(handler, "on or off is required")
    # "on" is authoritative when present: the client sends {"on": false} for
    # `/idea off`, so a truthy-coalescing read would flip the mode ON.
    if "on" in body:
        on = bool(body.get("on"))
    else:
        on = not bool(body.get("off"))
    state = set_idea_mode(session_id, on)
    text = (f"💡 Idea mode on — speak or type ideas. Board: {IDEA_BOARD}." if on
            else summary_line(state))
    return j(handler, {"ok": True, "idea_mode": on, "state": state, "text": text})


def handle_idea_status(handler, parsed):
    """GET /api/idea/status?session_id=... — flag + counters (spec §3.1 shape)."""
    from urllib.parse import parse_qs

    query = parse_qs(getattr(parsed, "query", "") or "")
    session_id = str((query.get("session_id") or [""])[0])
    if not session_id:
        return bad(handler, "session_id is required")
    state = get_state(session_id)
    state["stats"] = state.get("stats") or {key: 0 for key in STATS_KEYS}
    return j(handler, state)
