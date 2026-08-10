"""Durable, server-owned continuation scheduler for WebUI ``/goal`` turns.

The browser may observe ``goal_continue`` events, but it is never the authority
that starts the next turn.  A small atomic registry survives tab closure, SSE
reconnects, and WebUI restarts.  Each record represents one logical continuation
and is claimed before ``routes.start_session_turn`` is called.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from api.config import STATE_DIR

logger = logging.getLogger(__name__)

REGISTRY_PATH = Path(STATE_DIR) / "goal-continuations.json"
REGISTRY_VERSION = 1
_OWNER_PID = os.getpid()
OWNER_ID = f"webui-{_OWNER_PID}-{uuid.uuid4().hex}"
DEFAULT_MAX_ATTEMPTS = 3
_BUSY_RETRY_SECONDS = 0.25
_IDLE_POLL_SECONDS = 0.5
_MAX_START_FAILURES = 5
_STARTING_LEASE_SECONDS = 30.0
_RUNNING_HEARTBEAT_GRACE_SECONDS = 30.0

_REGISTRY_LOCK = threading.RLock()
_REGISTRY: dict[str, Any] | None = None
_WORKER_WAKE = threading.Event()
_WORKER_STOP = threading.Event()
_WORKER_THREAD: threading.Thread | None = None
_WORKER_LIFECYCLE_LOCK = threading.Lock()
_WORKER_LEADER_FD: int | None = None
_WORKER_LEADER_PID: int | None = None


def _current_owner_id() -> str:
    """Return a process-unique owner, regenerating after ``fork()``."""
    global OWNER_ID, _OWNER_PID
    pid = os.getpid()
    if pid != _OWNER_PID:
        _OWNER_PID = pid
        OWNER_ID = f"webui-{pid}-{uuid.uuid4().hex}"
    return OWNER_ID


def _after_fork_child() -> None:
    """Discard process-local locks and worker ownership inherited by fork."""
    global _REGISTRY_LOCK, _REGISTRY, _WORKER_STOP, _WORKER_WAKE
    global _WORKER_THREAD, _WORKER_LIFECYCLE_LOCK
    global _WORKER_LEADER_FD, _WORKER_LEADER_PID
    global _OWNER_PID, OWNER_ID

    if _WORKER_LEADER_FD is not None:
        try:
            os.close(_WORKER_LEADER_FD)
        except OSError:
            pass
    _REGISTRY_LOCK = threading.RLock()
    _REGISTRY = None
    _WORKER_STOP = threading.Event()
    _WORKER_WAKE = threading.Event()
    _WORKER_THREAD = None
    _WORKER_LIFECYCLE_LOCK = threading.Lock()
    _WORKER_LEADER_FD = None
    _WORKER_LEADER_PID = None
    _OWNER_PID = os.getpid()
    OWNER_ID = f"webui-{_OWNER_PID}-{uuid.uuid4().hex}"


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_child)


def _worker_leader_lock_path() -> Path:
    path = Path(REGISTRY_PATH)
    return path.with_name(f".{path.name}.worker.lock")


def _try_acquire_worker_leadership() -> bool:
    """Hold one process-wide scheduler lease for a shared registry."""
    global _WORKER_LEADER_FD, _WORKER_LEADER_PID
    pid = os.getpid()
    if _WORKER_LEADER_FD is not None and _WORKER_LEADER_PID == pid:
        return True
    if _WORKER_LEADER_FD is not None:
        try:
            os.close(_WORKER_LEADER_FD)
        except OSError:
            pass
        _WORKER_LEADER_FD = None
        _WORKER_LEADER_PID = None

    path = _worker_leader_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(path, 0o600)
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        os.close(fd)
        return False
    _WORKER_LEADER_FD = fd
    _WORKER_LEADER_PID = pid
    return True


def _release_worker_leadership() -> None:
    global _WORKER_LEADER_FD, _WORKER_LEADER_PID
    fd = _WORKER_LEADER_FD
    if fd is None:
        return
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        logger.debug("Goal continuation worker leadership unlock failed", exc_info=True)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        _WORKER_LEADER_FD = None
        _WORKER_LEADER_PID = None


@contextmanager
def _registry_process_lock():
    """Serialize registry transactions across WebUI processes.

    Atomic rename prevents torn JSON but not lost updates: every writer must hold
    this lock while reloading, mutating, and replacing the registry.
    """
    lock_path = Path(REGISTRY_PATH).with_name(f".{Path(REGISTRY_PATH).name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size < 1:
                os.write(fd, b"\0")
                os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@contextmanager
def _registry_transaction():
    """Hold both process-local and OS locks and refresh durable truth."""
    global _REGISTRY
    with _REGISTRY_LOCK:
        with _registry_process_lock():
            _REGISTRY = None
            registry = _load_locked()
            try:
                yield registry
            except BaseException:
                # Never retain a mutation that failed before durable replacement.
                _REGISTRY = None
                raise


def _empty_registry() -> dict[str, Any]:
    return {"version": REGISTRY_VERSION, "intents": {}}


def _validated_registry(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("version") != REGISTRY_VERSION:
        return _empty_registry()
    intents = raw.get("intents")
    if not isinstance(intents, dict):
        return _empty_registry()
    clean: dict[str, dict[str, Any]] = {}
    for sid, record in intents.items():
        sid = str(sid or "").strip()
        if not sid or not isinstance(record, dict):
            continue
        if str(record.get("session_id") or "").strip() != sid:
            continue
        prompt = str(record.get("prompt") or "").strip()
        continuation_id = str(record.get("continuation_id") or "").strip()
        if not prompt or not continuation_id:
            continue
        clean[sid] = copy.deepcopy(record)
    return {"version": REGISTRY_VERSION, "intents": clean}


def _load_locked() -> dict[str, Any]:
    global _REGISTRY
    if _REGISTRY is not None:
        return _REGISTRY
    try:
        text = Path(REGISTRY_PATH).read_text(encoding="utf-8")
    except FileNotFoundError:
        _REGISTRY = _empty_registry()
        return _REGISTRY
    except OSError:
        logger.error("Goal continuation registry cannot be read", exc_info=True)
        raise
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        path = Path(REGISTRY_PATH)
        quarantine = path.with_name(
            f"{path.name}.corrupt-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        )
        try:
            os.replace(path, quarantine)
            os.chmod(quarantine, 0o600)
            _fsync_parent(path)
            logger.error(
                "Goal continuation registry contained invalid JSON and was quarantined at %s",
                quarantine,
                exc_info=True,
            )
        except Exception:
            logger.error(
                "Goal continuation registry is corrupt and could not be quarantined",
                exc_info=True,
            )
        raw = _empty_registry()
    _REGISTRY = _validated_registry(raw)
    return _REGISTRY


def _fsync_parent(path: Path) -> None:
    try:
        fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _save_locked() -> None:
    global _REGISTRY
    registry = _load_locked()
    path = Path(REGISTRY_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(registry, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        _fsync_parent(path)
    except BaseException:
        _REGISTRY = None
        raise
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def get_goal_continuation(session_id: str) -> dict[str, Any] | None:
    sid = str(session_id or "").strip()
    if not sid:
        return None
    with _registry_transaction() as registry:
        record = registry["intents"].get(sid)
        return copy.deepcopy(record) if isinstance(record, dict) else None


def _record_now(record: dict[str, Any], now: float) -> dict[str, Any]:
    record["updated_at"] = float(now)
    return record


def schedule_goal_continuation(
    session_id: str,
    prompt: str,
    *,
    source_stream_id: str,
    profile_home: str | Path | None,
    goal_turns_used: int,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    now: float | None = None,
) -> dict[str, Any]:
    """Persist one logical continuation before emitting ``goal_continue``.

    Repeated scheduling from the same judged stream is idempotent.  A later
    judged stream replaces the preceding running record with the next logical
    continuation.
    """
    sid = str(session_id or "").strip()
    text = str(prompt or "").strip()
    source_stream = str(source_stream_id or "").strip()
    if not sid or not text or not source_stream:
        raise ValueError("session_id, prompt, and source_stream_id are required")
    timestamp = float(time.time() if now is None else now)
    attempts_limit = max(1, int(max_attempts or DEFAULT_MAX_ATTEMPTS))
    with _registry_transaction() as registry:
        existing = registry["intents"].get(sid)
        if (
            isinstance(existing, dict)
            and existing.get("source_stream_id") == source_stream
            and existing.get("prompt") == text
        ):
            return copy.deepcopy(existing)
        record = {
            "version": REGISTRY_VERSION,
            "session_id": sid,
            "continuation_id": uuid.uuid4().hex,
            "source_stream_id": source_stream,
            "stream_id": None,
            "prompt": text,
            "profile_home": str(Path(profile_home).expanduser().resolve()) if profile_home else None,
            "goal_turns_used": max(0, int(goal_turns_used or 0)),
            "status": "pending",
            "owner_id": _current_owner_id(),
            "claim_id": None,
            "claim_started_at": None,
            "admitted_at": None,
            "last_heartbeat_at": None,
            "attempts": 0,
            "max_attempts": attempts_limit,
            "start_failures": 0,
            "available_at": timestamp,
            "created_at": timestamp,
            "updated_at": timestamp,
            "last_error": None,
        }
        registry["intents"][sid] = record
        _save_locked()
    _WORKER_WAKE.set()
    return copy.deepcopy(record)


def bind_goal_continuation_stream(
    session_id: str,
    stream_id: str,
    *,
    claim_id: str,
) -> bool:
    """Fence and bind an admitted stream before its worker can finish."""
    sid = str(session_id or "").strip()
    stream = str(stream_id or "").strip()
    claim = str(claim_id or "").strip()
    if not sid or not stream or not claim:
        return False
    timestamp = time.time()
    with _registry_transaction() as registry:
        record = registry["intents"].get(sid)
        if not isinstance(record, dict) or record.get("status") != "starting":
            return False
        if str(record.get("claim_id") or "") != claim:
            return False
        if str(record.get("owner_id") or "") != _current_owner_id():
            return False
        record["status"] = "running"
        record["stream_id"] = stream
        record["attempts"] = int(record.get("attempts") or 0) + 1
        record["admitted_at"] = timestamp
        record["last_heartbeat_at"] = timestamp
        record["updated_at"] = timestamp
        registry["intents"][sid] = record
        _save_locked()
        return True

def adopt_legacy_browser_goal_stream(session_id: str, stream_id: str, prompt: str) -> bool:
    """Attach an old-tab continuation POST to the matching durable intent.

    A record already in ``starting`` belongs to the server scheduler and must not
    be stolen; the browser request is then rejected by the route so only one
    admission can win.  The prompt must also match exactly so an unrelated human
    message can never consume a legacy goal marker or burn the goal budget.
    """
    sid = str(session_id or "").strip()
    stream = str(stream_id or "").strip()
    expected_prompt = str(prompt or "").strip()
    if not sid or not stream or not expected_prompt:
        return False
    with _registry_transaction() as registry:
        record = registry["intents"].get(sid)
        if (
            not isinstance(record, dict)
            or record.get("status") != "pending"
            or str(record.get("prompt") or "").strip() != expected_prompt
        ):
            return False
        record["status"] = "running"
        record["stream_id"] = stream
        record["claim_id"] = None
        record["claim_started_at"] = None
        record["attempts"] = int(record.get("attempts") or 0) + 1
        record["owner_id"] = _current_owner_id()
        timestamp = time.time()
        record["admitted_at"] = timestamp
        record["last_heartbeat_at"] = timestamp
        _record_now(record, timestamp)
        _save_locked()
        return True


def legacy_browser_goal_prompt_matches(session_id: str, prompt: str) -> bool:
    """Return true only for an old-tab replay of the current durable intent."""
    sid = str(session_id or "").strip()
    expected_prompt = str(prompt or "").strip()
    if not sid or not expected_prompt:
        return False
    with _registry_transaction() as registry:
        record = registry["intents"].get(sid)
        return bool(
            isinstance(record, dict)
            and record.get("status") in {"pending", "starting", "running"}
            and str(record.get("prompt") or "").strip() == expected_prompt
        )


def _default_goal_active(session_id: str, *, profile_home: str | None = None) -> bool:
    from api.goals import has_active_goal
    from api.models import get_session

    try:
        get_session(session_id)
    except (KeyError, FileNotFoundError):
        return False
    return bool(has_active_goal(session_id, profile_home=profile_home))


def _call_goal_active(check: Callable[..., bool], record: dict[str, Any]) -> bool:
    try:
        return bool(check(record["session_id"], profile_home=record.get("profile_home")))
    except TypeError:
        return bool(check(record["session_id"]))


def _release_start_claim(
    session_id: str,
    claim_id: str,
    *,
    now: float,
    error: str,
    busy: bool,
) -> None:
    with _registry_transaction() as registry:
        record = registry["intents"].get(session_id)
        if not isinstance(record, dict) or record.get("claim_id") != claim_id:
            return
        # bind_goal_continuation_stream may already have moved the record to
        # running; a route-start exception still returns ownership to pending.
        if record.get("status") == "running":
            record["attempts"] = max(0, int(record.get("attempts") or 0) - 1)
        record["status"] = "pending"
        record["stream_id"] = None
        record["claim_id"] = None
        record["claim_started_at"] = None
        record["admitted_at"] = None
        record["last_heartbeat_at"] = None
        if not busy:
            record["start_failures"] = int(record.get("start_failures") or 0) + 1
        failures = int(record.get("start_failures") or 0)
        if failures >= _MAX_START_FAILURES:
            record["status"] = "failed"
        record["available_at"] = now + (_BUSY_RETRY_SECONDS if busy else min(30.0, 2.0 ** max(0, failures - 1)))
        record["last_error"] = str(error or "turn start failed")[:500]
        _record_now(record, now)
        _save_locked()
    _WORKER_WAKE.set()


def drain_goal_continuations_once(
    *,
    start_turn: Callable[..., dict[str, Any]] | None = None,
    is_goal_active: Callable[..., bool] | None = None,
    now: float | None = None,
) -> int:
    """Claim and dispatch at most one due continuation.

    Returns one only after a server-side turn was accepted.  Busy sessions
    release the claim without consuming a provider attempt.
    """
    timestamp = float(time.time() if now is None else now)
    active_check = is_goal_active or _default_goal_active
    selected: dict[str, Any] | None = None
    with _registry_transaction() as registry:
        for sid in sorted(registry["intents"]):
            record = registry["intents"].get(sid)
            if not isinstance(record, dict) or record.get("status") != "pending":
                continue
            if float(record.get("available_at") or 0.0) > timestamp:
                continue
            if not _call_goal_active(active_check, record):
                registry["intents"].pop(sid, None)
                _save_locked()
                continue
            claim_id = uuid.uuid4().hex
            record["status"] = "starting"
            record["claim_id"] = claim_id
            record["claim_started_at"] = timestamp
            record["owner_id"] = _current_owner_id()
            record["last_error"] = None
            _record_now(record, timestamp)
            _save_locked()
            selected = copy.deepcopy(record)
            break
    if selected is None:
        return 0

    sid = selected["session_id"]
    claim_id = selected["claim_id"]
    if _WORKER_STOP.is_set() and start_turn is None:
        _release_start_claim(
            sid,
            claim_id,
            now=time.time(),
            error="WebUI shutdown in progress",
            busy=True,
        )
        return 0
    if start_turn is None:
        from api.routes import start_session_turn as start_turn
    try:
        response = start_turn(
            sid,
            selected["prompt"],
            source="goal_continuation",
            continuation_claim_id=claim_id,
        ) or {}
    except Exception as exc:
        _release_start_claim(sid, claim_id, now=timestamp, error=f"{type(exc).__name__}: {exc}", busy=False)
        logger.warning("Goal continuation start raised for session %s", sid, exc_info=True)
        return 0

    try:
        status = int(response.get("_status", 200) or 200)
    except (TypeError, ValueError):
        status = 500
    stream_id = str(response.get("stream_id") or "").strip()
    if status == 409:
        _release_start_claim(sid, claim_id, now=timestamp, error="session busy", busy=True)
        return 0
    if status >= 400 or not stream_id:
        _release_start_claim(
            sid,
            claim_id,
            now=timestamp,
            error=str(response.get("error") or f"turn start returned HTTP {status}"),
            busy=False,
        )
        return 0

    with _registry_transaction() as registry:
        record = registry["intents"].get(sid)
        if not isinstance(record, dict) or record.get("claim_id") != claim_id:
            # A very fast worker may already have replaced the record with the
            # next continuation.  Never overwrite that newer generation.
            return 1
        if record.get("status") == "starting":
            record["status"] = "running"
            record["stream_id"] = stream_id
            record["attempts"] = int(record.get("attempts") or 0) + 1
            record["owner_id"] = _current_owner_id()
            record["admitted_at"] = timestamp
            record["last_heartbeat_at"] = timestamp
            _record_now(record, timestamp)
            _save_locked()
        elif record.get("status") == "running" and record.get("stream_id") == stream_id:
            pass
        else:
            return 1
    return 1


def _retry_delay(attempts: int) -> float:
    return min(30.0, 2.0 ** max(0, int(attempts or 0) - 1))


def requeue_goal_continuation_after_no_response(
    session_id: str,
    stream_id: str,
    *,
    had_activity: bool,
    cancellation_check: Callable[[], bool] | None = None,
    now: float | None = None,
) -> bool:
    """Requeue a truly empty goal turn without replaying an active turn.

    Any token, reasoning, tool, approval, or partial signal makes replay unsafe;
    the record then becomes terminally failed for explicit user inspection.
    """
    sid = str(session_id or "").strip()
    stream = str(stream_id or "").strip()
    timestamp = float(time.time() if now is None else now)
    with _registry_transaction() as registry:
        record = registry["intents"].get(sid)
        if (
            not isinstance(record, dict)
            or record.get("status") != "running"
            or record.get("stream_id") != stream
        ):
            return False
        attempts = int(record.get("attempts") or 0)
        max_attempts = max(1, int(record.get("max_attempts") or DEFAULT_MAX_ATTEMPTS))
        try:
            cancelled = bool(cancellation_check and cancellation_check())
        except Exception:
            logger.warning("Goal continuation cancellation check failed closed", exc_info=True)
            cancelled = True
        if cancelled:
            record["status"] = "failed"
            record["claim_id"] = None
            record["last_error"] = "goal continuation was cancelled; automatic replay refused"
            _record_now(record, timestamp)
            _save_locked()
            return False
        if had_activity:
            record["status"] = "failed"
            record["last_error"] = "empty terminal response after observable activity; automatic replay refused"
            _record_now(record, timestamp)
            _save_locked()
            return False
        if attempts >= max_attempts:
            record["status"] = "failed"
            record["last_error"] = f"empty provider response after {attempts}/{max_attempts} attempts"
            _record_now(record, timestamp)
            _save_locked()
            return False
        record["status"] = "pending"
        record["stream_id"] = None
        record["claim_id"] = None
        record["claim_started_at"] = None
        record["admitted_at"] = None
        record["last_heartbeat_at"] = None
        record["owner_id"] = _current_owner_id()
        record["available_at"] = timestamp + _retry_delay(attempts)
        record["last_error"] = "empty provider response; automatic retry scheduled"
        _record_now(record, timestamp)
        _save_locked()
    _WORKER_WAKE.set()
    return True


def complete_goal_continuation(session_id: str, stream_id: str | None = None) -> bool:
    sid = str(session_id or "").strip()
    expected_stream = str(stream_id or "").strip()
    with _registry_transaction() as registry:
        record = registry["intents"].get(sid)
        if not isinstance(record, dict):
            return False
        if expected_stream and record.get("stream_id") != expected_stream:
            return False
        registry["intents"].pop(sid, None)
        _save_locked()
        return True


def fail_goal_continuation(session_id: str, stream_id: str, reason: str) -> bool:
    """Settle a claimed intent that ended without a judge-owned successor."""
    sid = str(session_id or "").strip()
    expected_stream = str(stream_id or "").strip()
    with _registry_transaction() as registry:
        record = registry["intents"].get(sid)
        if (
            not isinstance(record, dict)
            or record.get("status") != "running"
            or record.get("stream_id") != expected_stream
        ):
            return False
        record["status"] = "failed"
        record["claim_id"] = None
        record["last_error"] = str(reason or "goal continuation ended without settlement")[:500]
        _record_now(record, time.time())
        _save_locked()
        return True


def _replace_goal_continuation_for_test(record: dict[str, Any]) -> None:
    """Test-only durable replacement helper (kept explicit to avoid raw writes)."""
    sid = str(record.get("session_id") or "").strip()
    if not sid:
        raise ValueError("record session_id required")
    with _registry_transaction() as registry:
        registry["intents"][sid] = copy.deepcopy(record)
        _save_locked()


def _default_goal_state_loader(session_id: str, *, profile_home: str | None = None) -> dict[str, Any]:
    from api.goals import CONTINUATION_PROMPT_TEMPLATE, goal_state_snapshot
    from api.models import get_session

    try:
        get_session(session_id)
    except (KeyError, FileNotFoundError):
        return {"status": "missing", "turns_used": 0}
    state = goal_state_snapshot(session_id, profile_home=profile_home)
    goal_text = str(getattr(state, "goal", "") or "").strip()
    continuation_prompt = (
        CONTINUATION_PROMPT_TEMPLATE.format(goal=goal_text)
        if goal_text and CONTINUATION_PROMPT_TEMPLATE
        else None
    )
    return {
        "status": str(getattr(state, "status", "") or ""),
        "turns_used": int(getattr(state, "turns_used", 0) or 0),
        "continuation_prompt": continuation_prompt,
    }


def _default_run_summary_loader(session_id: str, stream_id: str) -> dict[str, Any]:
    if not stream_id:
        return {"terminal_state": "unknown", "observable_activity": False}
    from api.run_journal import latest_run_summary, read_run_events

    summary = latest_run_summary(session_id, stream_id)
    events = (read_run_events(session_id, stream_id) or {}).get("events") or []
    activity_prefixes = (
        "token",
        "reason",
        "tool",
        "progress",
        "approval",
        "clarify",
        "assistant",
        "process",
        "bg_",
    )
    summary["observable_activity"] = any(
        str(event.get("event") or "").lower().startswith(activity_prefixes)
        for event in events
        if isinstance(event, dict)
    )
    return summary


def _default_run_active(session_id: str, stream_id: str) -> bool:
    """Return whether the admitted stream still has a live WebUI worker."""
    sid = str(session_id or "").strip()
    stream = str(stream_id or "").strip()
    if not sid or not stream:
        return False
    try:
        from api import config

        with config.ACTIVE_RUNS_LOCK:
            run = (config.ACTIVE_RUNS or {}).get(stream)
            if isinstance(run, dict) and str(run.get("session_id") or "") == sid:
                return True
    except Exception:
        logger.debug("Goal continuation live-run check failed", exc_info=True)
        raise
    return False


def reconcile_goal_continuations_once(
    *,
    active_run_check: Callable[[str, str], bool] | None = None,
    run_summary_loader: Callable[[str, str], Any] | None = None,
    now: float | None = None,
) -> int:
    """Reconcile expired admission leases and orphaned running streams.

    A successful ``start_session_turn`` response is only admission, not proof
    that a worker ever started. ``starting`` claims therefore expire, while a
    ``running`` record must periodically prove liveness through the in-process
    run registries. Automatic replay is allowed only when the journal proves
    there was no observable model/tool activity; otherwise the record fails
    closed for explicit inspection.
    """
    timestamp = float(time.time() if now is None else now)
    active_check = active_run_check or _default_run_active
    summary_loader = run_summary_loader or _default_run_summary_loader
    changed = 0

    with _registry_transaction() as registry:
        candidates = [
            copy.deepcopy(record)
            for record in registry["intents"].values()
            if isinstance(record, dict) and record.get("status") in {"starting", "running"}
        ]

    for snapshot in candidates:
        sid = str(snapshot.get("session_id") or "")
        status = str(snapshot.get("status") or "")
        continuation_id = str(snapshot.get("continuation_id") or "")
        stream_id = str(snapshot.get("stream_id") or "")
        if status == "starting":
            lease_anchor = float(
                snapshot.get("claim_started_at")
                or snapshot.get("updated_at")
                or snapshot.get("created_at")
                or 0.0
            )
            if lease_anchor and timestamp - lease_anchor < _STARTING_LEASE_SECONDS:
                continue
            with _registry_transaction() as registry:
                record = registry["intents"].get(sid)
                if (
                    not isinstance(record, dict)
                    or record.get("status") != "starting"
                    or str(record.get("continuation_id") or "") != continuation_id
                    or str(record.get("claim_id") or "")
                    != str(snapshot.get("claim_id") or "")
                ):
                    continue
                record["status"] = "pending"
                record["claim_id"] = None
                record["claim_started_at"] = None
                record["owner_id"] = _current_owner_id()
                record["available_at"] = timestamp
                record["last_error"] = "starting claim lease expired before stream admission"
                _record_now(record, timestamp)
                _save_locked()
                changed += 1
            continue

        heartbeat_anchor = float(
            snapshot.get("last_heartbeat_at")
            or snapshot.get("admitted_at")
            or snapshot.get("updated_at")
            or 0.0
        )
        if heartbeat_anchor and timestamp - heartbeat_anchor < _RUNNING_HEARTBEAT_GRACE_SECONDS:
            continue
        try:
            live = bool(active_check(sid, stream_id))
        except Exception:
            logger.warning("Goal continuation liveness check failed for %s", sid, exc_info=True)
            continue
        if live:
            with _registry_transaction() as registry:
                record = registry["intents"].get(sid)
                if (
                    not isinstance(record, dict)
                    or record.get("status") != "running"
                    or str(record.get("continuation_id") or "") != continuation_id
                    or str(record.get("stream_id") or "") != stream_id
                ):
                    continue
                record["last_heartbeat_at"] = timestamp
                _record_now(record, timestamp)
                _save_locked()
                changed += 1
            continue

        try:
            summary = summary_loader(sid, stream_id) or {}
            observable_activity = bool(summary.get("observable_activity"))
            terminal_state = str(summary.get("terminal_state") or "unknown").strip().lower()
            evidence_error = None
        except Exception as exc:
            logger.warning("Goal continuation orphan evidence failed for %s", sid, exc_info=True)
            observable_activity = True
            terminal_state = "unknown"
            evidence_error = f"run evidence unavailable: {type(exc).__name__}: {exc}"

        with _registry_transaction() as registry:
            record = registry["intents"].get(sid)
            if (
                not isinstance(record, dict)
                or record.get("status") != "running"
                or str(record.get("continuation_id") or "") != continuation_id
                or str(record.get("stream_id") or "") != stream_id
            ):
                continue
            attempts = int(record.get("attempts") or 0)
            max_attempts = max(1, int(record.get("max_attempts") or DEFAULT_MAX_ATTEMPTS))
            if evidence_error:
                record["status"] = "failed"
                record["last_error"] = evidence_error[:500]
            elif observable_activity:
                record["status"] = "failed"
                record["last_error"] = (
                    "orphaned goal continuation emitted observable activity; automatic replay refused"
                )
            elif terminal_state in {"completed", "done", "cancelled"}:
                record["status"] = "failed"
                record["last_error"] = (
                    f"orphaned goal continuation reached {terminal_state} without a durable judge verdict"
                )
            elif attempts >= max_attempts:
                record["status"] = "failed"
                record["last_error"] = f"orphan recovery exhausted {attempts}/{max_attempts} attempts"
            else:
                record["status"] = "pending"
                record["stream_id"] = None
                record["claim_id"] = None
                record["claim_started_at"] = None
                record["admitted_at"] = None
                record["last_heartbeat_at"] = None
                record["owner_id"] = _current_owner_id()
                record["available_at"] = timestamp + _retry_delay(attempts)
                record["last_error"] = (
                    "admitted stream had no live worker or observable activity "
                    f"({terminal_state}); retry scheduled"
                )
            _record_now(record, timestamp)
            _save_locked()
            changed += 1

    if changed:
        _WORKER_WAKE.set()
    return changed


def _call_loader(loader: Callable[..., Any], record: dict[str, Any]) -> Any:
    try:
        return loader(record["session_id"], profile_home=record.get("profile_home"))
    except TypeError:
        return loader(record["session_id"])


def recover_goal_continuations(
    *,
    goal_state_loader: Callable[..., Any] | None = None,
    run_summary_loader: Callable[..., Any] | None = None,
    now: float | None = None,
) -> int:
    """Recover records owned by a previous WebUI process.

    A goal whose turn counter advanced has already been judged: a fresh pending
    generation is derived and the old stream is never replayed.  An unjudged
    empty/interrupted turn is made pending only while its bounded attempt budget
    remains.  Completed-but-unjudged turns fail closed rather than duplicating a
    possibly side-effectful turn.
    """
    timestamp = float(time.time() if now is None else now)
    state_loader = goal_state_loader or _default_goal_state_loader
    summary_loader = run_summary_loader or _default_run_summary_loader
    recovered = 0
    with _registry_transaction() as registry:
        for sid in list(registry["intents"]):
            record = registry["intents"].get(sid)
            if not isinstance(record, dict):
                continue
            if record.get("status") not in {"starting", "running"}:
                continue
            if record.get("owner_id") == _current_owner_id():
                continue
            state = _call_loader(state_loader, record)
            state_status = str((state or {}).get("status") or "") if isinstance(state, dict) else ""
            turns_used = int((state or {}).get("turns_used") or 0) if isinstance(state, dict) else 0
            if state_status != "active":
                registry["intents"].pop(sid, None)
                recovered += 1
                continue
            origin_turns = int(record.get("goal_turns_used") or 0)
            if turns_used > origin_turns:
                current_prompt = str((state or {}).get("continuation_prompt") or "").strip()
                if not current_prompt:
                    record["status"] = "failed"
                    record["last_error"] = (
                        "goal advanced but no current continuation prompt could be derived; replay refused"
                    )
                    _record_now(record, timestamp)
                    recovered += 1
                    continue
                record.update(
                    {
                        "continuation_id": uuid.uuid4().hex,
                        "source_stream_id": f"recovery:{record.get('stream_id') or record.get('source_stream_id')}",
                        "stream_id": None,
                        "prompt": current_prompt,
                        "goal_turns_used": turns_used,
                        "status": "pending",
                        "owner_id": _current_owner_id(),
                        "claim_id": None,
                        "claim_started_at": None,
                        "admitted_at": None,
                        "last_heartbeat_at": None,
                        "attempts": 0,
                        "start_failures": 0,
                        "available_at": timestamp,
                        "last_error": None,
                    }
                )
                _record_now(record, timestamp)
                recovered += 1
                continue
            try:
                summary = summary_loader(sid, str(record.get("stream_id") or "")) or {}
            except Exception:
                logger.warning("Goal continuation run-summary recovery failed for %s", sid, exc_info=True)
                record["status"] = "failed"
                record["claim_id"] = None
                record["last_error"] = (
                    "run evidence was unavailable during recovery; automatic replay refused"
                )
                _record_now(record, timestamp)
                recovered += 1
                continue
            terminal_state = str(summary.get("terminal_state") or "unknown").strip().lower()
            observable_activity = bool(summary.get("observable_activity"))
            attempts = int(record.get("attempts") or 0)
            max_attempts = max(1, int(record.get("max_attempts") or DEFAULT_MAX_ATTEMPTS))
            if observable_activity:
                record["status"] = "failed"
                record["last_error"] = (
                    "interrupted goal turn emitted observable activity; automatic replay refused"
                )
            elif terminal_state in {"completed", "done"}:
                record["status"] = "failed"
                record["last_error"] = "completed goal turn lacks a durable judge verdict; replay refused"
            elif attempts >= max_attempts:
                record["status"] = "failed"
                record["last_error"] = f"goal continuation recovery exhausted {attempts}/{max_attempts} attempts"
            else:
                record["status"] = "pending"
                record["stream_id"] = None
                record["claim_id"] = None
                record["claim_started_at"] = None
                record["admitted_at"] = None
                record["last_heartbeat_at"] = None
                record["owner_id"] = _current_owner_id()
                record["available_at"] = timestamp
                record["last_error"] = f"recovered after {terminal_state} terminal state"
            _record_now(record, timestamp)
            recovered += 1
        if recovered:
            _save_locked()
    if recovered:
        _WORKER_WAKE.set()
    return recovered


def wake_goal_continuation_worker() -> None:
    _WORKER_WAKE.set()


def _worker_loop() -> None:
    try:
        recover_goal_continuations()
    except Exception:
        logger.warning("Goal continuation startup recovery failed", exc_info=True)
    while not _WORKER_STOP.is_set():
        try:
            reconcile_goal_continuations_once()
        except Exception:
            logger.warning("Goal continuation reconciliation failed", exc_info=True)
        try:
            started = drain_goal_continuations_once()
        except Exception:
            logger.warning("Goal continuation drain failed", exc_info=True)
            started = 0
        if started:
            continue
        _WORKER_WAKE.wait(_IDLE_POLL_SECONDS)
        _WORKER_WAKE.clear()


def start_goal_continuation_worker() -> bool:
    global _WORKER_THREAD
    with _WORKER_LIFECYCLE_LOCK:
        if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
            return False
        if not _try_acquire_worker_leadership():
            logger.info("Goal continuation worker leadership is held by another process")
            return False
        _WORKER_STOP.clear()
        _WORKER_WAKE.clear()
        _WORKER_THREAD = threading.Thread(
            target=_worker_loop,
            name="hermes-webui-goal-continuations",
            daemon=True,
        )
        try:
            _WORKER_THREAD.start()
        except BaseException:
            _WORKER_THREAD = None
            _release_worker_leadership()
            raise
        return True


def stop_goal_continuation_worker(timeout: float = 2.0) -> bool:
    global _WORKER_THREAD
    _WORKER_STOP.set()
    _WORKER_WAKE.set()
    thread = _WORKER_THREAD
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
    alive = bool(thread is not None and thread.is_alive())
    with _WORKER_LIFECYCLE_LOCK:
        if not alive and thread is _WORKER_THREAD:
            _WORKER_THREAD = None
            _release_worker_leadership()
    return not alive
