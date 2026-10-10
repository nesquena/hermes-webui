"""WebUI bridge for Hermes persistent session goals."""

from __future__ import annotations

import copy
import logging
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

try:  # Exposed as a module attribute so tests can monkeypatch it directly.
    from hermes_cli.goals import (  # type: ignore
        CONTINUATION_PROMPT_TEMPLATE,
        DEFAULT_MAX_TURNS,
        GoalManager as _NativeGoalManager,
        GoalState,
        judge_goal,
    )
except Exception:  # pragma: no cover - depends on installed hermes-agent
    CONTINUATION_PROMPT_TEMPLATE = ""  # type: ignore
    DEFAULT_MAX_TURNS = 20  # type: ignore
    _NativeGoalManager = None  # type: ignore
    GoalState = None  # type: ignore
    judge_goal = None  # type: ignore

GoalManager = _NativeGoalManager  # type: ignore

_DB_CACHE: dict[str, Any] = {}


def _default_max_turns() -> int:
    """Return the configured /goal turn budget, defaulting to Hermes' 20 turns."""
    try:
        from api import config as _config

        cfg = getattr(_config, "cfg", {}) or {}
        goals_cfg = cfg.get("goals", {}) if isinstance(cfg, dict) else {}
        if not isinstance(goals_cfg, dict):
            return int(DEFAULT_MAX_TURNS or 20)
        return max(1, int(goals_cfg.get("max_turns", DEFAULT_MAX_TURNS or 20) or 20))
    except Exception:
        return int(DEFAULT_MAX_TURNS or 20)


def _meta_key(session_id: str) -> str:
    return f"goal:{session_id}"


def _profile_db(profile_home: str | Path):
    """Return a SessionDB pinned to *profile_home*, without reading HERMES_HOME.

    The upstream Hermes GoalManager persists through hermes_cli.goals.load_goal(),
    which resolves SessionDB from process-global HERMES_HOME. WebUI sessions are
    profile-scoped and can run concurrently, so the WebUI bridge uses an explicit
    state.db path whenever the caller provides the session's profile home.
    """
    home = Path(profile_home).expanduser().resolve()
    key = str(home)
    cached = _DB_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        from hermes_state import SessionDB  # type: ignore

        db = SessionDB(db_path=home / "state.db")
    except Exception as exc:  # pragma: no cover - import/env dependent
        logger.debug("GoalManager profile DB unavailable for %s: %s", home, exc)
        return None
    _DB_CACHE[key] = db
    return db


def _profile_home_context_api():
    """Return the native context-local Hermes home setters when available."""
    try:
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    except Exception:  # pragma: no cover - depends on installed hermes-agent
        return None
    return set_hermes_home_override, reset_hermes_home_override


def _native_profile_context_api(profile_home: str | Path):
    """Return context setters only when SessionDB resolves that context at call time."""
    context_api = _profile_home_context_api()
    if context_api is None:
        return None
    try:
        from hermes_state import _default_db_path  # type: ignore
    except Exception:  # pragma: no cover - depends on installed hermes-agent
        return None
    if not callable(_default_db_path):
        return None

    home = Path(profile_home).expanduser().resolve()
    set_home, reset_home = context_api
    try:
        token = set_home(home)
    except Exception:  # pragma: no cover - depends on installed hermes-agent
        return None
    try:
        resolved_db_path = Path(_default_db_path()).expanduser().resolve()
    except Exception:  # pragma: no cover - depends on installed hermes-agent
        return None
    finally:
        reset_home(token)
    if resolved_db_path != home / "state.db":
        return None
    return context_api


class _ProfileGoalManager:
    """Run the native GoalManager under a context-local profile home."""

    def __init__(
        self,
        session_id: str,
        *,
        profile_home: str | Path,
        default_max_turns: int = 20,
        context_api=None,
    ):
        if _NativeGoalManager is None:
            raise RuntimeError("Hermes goal manager unavailable")
        context_api = context_api or _profile_home_context_api()
        if context_api is None:
            raise RuntimeError("Hermes profile context unavailable")
        self.session_id = session_id
        self.profile_home = Path(profile_home).expanduser().resolve()
        self._set_home, self._reset_home = context_api
        self._manager = self._scoped(
            _NativeGoalManager,
            session_id=session_id,
            default_max_turns=default_max_turns,
        )

    def _scoped(self, func, *args, **kwargs):
        token = self._set_home(self.profile_home)
        try:
            return func(*args, **kwargs)
        finally:
            self._reset_home(token)

    @property
    def state(self):
        return self._manager.state

    def __getattr__(self, name):
        value = getattr(self._manager, name)
        if not callable(value):
            return value

        def scoped_call(*args, **kwargs):
            return self._scoped(value, *args, **kwargs)

        return scoped_call

    def _restore_state(self, snapshot) -> None:
        from hermes_cli.goals import save_goal  # type: ignore

        self._manager._state = snapshot
        self._scoped(save_goal, self.session_id, snapshot)


class _LegacyProfileGoalManager:
    """Explicit-DB fallback for Hermes versions without profile context."""

    def __init__(self, session_id: str, *, profile_home: str | Path, default_max_turns: int = 20):
        if GoalState is None:
            raise RuntimeError("Hermes goal state unavailable")
        self.session_id = session_id
        self.profile_home = Path(profile_home).expanduser().resolve()
        self.default_max_turns = int(default_max_turns or DEFAULT_MAX_TURNS or 20)
        self._state = self._load()

    @property
    def state(self):
        return self._state

    def _load(self):
        db = _profile_db(self.profile_home)
        if db is None or not self.session_id:
            return None
        try:
            raw = db.get_meta(_meta_key(self.session_id))
        except Exception as exc:
            logger.debug("GoalManager profile get_meta failed: %s", exc)
            return None
        if not raw:
            return None
        try:
            return GoalState.from_json(raw)  # type: ignore[union-attr]
        except Exception as exc:
            logger.warning("GoalManager profile state parse failed for %s: %s", self.session_id, exc)
            return None

    def _save(self, state) -> None:
        db = _profile_db(self.profile_home)
        if db is None or not self.session_id or state is None:
            return
        try:
            db.set_meta(_meta_key(self.session_id), state.to_json())
        except Exception as exc:
            logger.debug("GoalManager profile set_meta failed: %s", exc)

    def is_active(self) -> bool:
        return self._state is not None and self._state.status == "active"

    def has_goal(self) -> bool:
        return self._state is not None and self._state.status in ("active", "paused")

    def status_line(self) -> str:
        s = self._state
        if s is None or s.status in ("cleared",):
            return "No active goal. Set one with /goal <text>."
        turns = f"{s.turns_used}/{s.max_turns} turns"
        if s.status == "active":
            return f"⊙ Goal (active, {turns}): {s.goal}"
        if s.status == "paused":
            extra = f" — {s.paused_reason}" if s.paused_reason else ""
            return f"⏸ Goal (paused, {turns}{extra}): {s.goal}"
        if s.status == "done":
            return f"✓ Goal done ({turns}): {s.goal}"
        return f"Goal ({s.status}, {turns}): {s.goal}"

    def set(self, goal: str, *, max_turns: Optional[int] = None):
        goal = (goal or "").strip()
        if not goal:
            raise ValueError("goal text is empty")
        state = GoalState(  # type: ignore[operator]
            goal=goal,
            status="active",
            turns_used=0,
            max_turns=int(max_turns) if max_turns else self.default_max_turns,
            created_at=time.time(),
            last_turn_at=0.0,
        )
        self._state = state
        self._save(state)
        return state

    def pause(self, reason: str = "user-paused"):
        if not self._state:
            return None
        self._state.status = "paused"
        self._state.paused_reason = reason
        self._save(self._state)
        return self._state

    def resume(self, *, reset_budget: bool = True):
        if not self._state:
            return None
        self._state.status = "active"
        self._state.paused_reason = None
        if reset_budget:
            self._state.turns_used = 0
        self._save(self._state)
        return self._state

    def clear(self) -> None:
        if self._state is None:
            return
        self._state.status = "cleared"
        self._save(self._state)
        self._state = None

    def evaluate_after_turn(self, last_response: str, *, user_initiated: bool = True) -> Dict[str, Any]:
        state = self._state
        if state is None or state.status != "active":
            return {
                "status": state.status if state else None,
                "should_continue": False,
                "continuation_prompt": None,
                "verdict": "inactive",
                "reason": "no active goal",
                "message": "",
            }

        state.turns_used += 1
        state.last_turn_at = time.time()

        if judge_goal is None:
            verdict, reason = "continue", "goal judge unavailable"
        else:
            verdict, reason, *_ = judge_goal(state.goal, str(last_response or ""))
        state.last_verdict = verdict
        state.last_reason = reason

        if verdict == "done":
            state.status = "done"
            self._save(state)
            return {
                "status": "done",
                "should_continue": False,
                "continuation_prompt": None,
                "verdict": "done",
                "reason": reason,
                "message": f"✓ Goal achieved: {reason}",
            }

        if state.turns_used >= state.max_turns:
            state.status = "paused"
            state.paused_reason = f"turn budget exhausted ({state.turns_used}/{state.max_turns})"
            self._save(state)
            return {
                "status": "paused",
                "should_continue": False,
                "continuation_prompt": None,
                "verdict": "continue",
                "reason": reason,
                "message": (
                    f"⏸ Goal paused — {state.turns_used}/{state.max_turns} turns used. "
                    "Use /goal resume to keep going, or /goal clear to stop."
                ),
            }

        self._save(state)
        return {
            "status": "active",
            "should_continue": True,
            "continuation_prompt": self.next_continuation_prompt(),
            "verdict": "continue",
            "reason": reason,
            "message": f"↻ Continuing toward goal ({state.turns_used}/{state.max_turns}): {reason}",
        }

    def next_continuation_prompt(self) -> Optional[str]:
        if not self._state or self._state.status != "active":
            return None
        return CONTINUATION_PROMPT_TEMPLATE.format(goal=self._state.goal)


def _manager(session_id: str, *, profile_home: str | Path | None = None):
    if GoalManager is None:
        return None
    if profile_home and GoalManager is _NativeGoalManager and GoalState is not None:
        try:
            context_api = _native_profile_context_api(profile_home)
            if context_api is not None:
                return _ProfileGoalManager(
                    session_id=session_id,
                    profile_home=profile_home,
                    default_max_turns=_default_max_turns(),
                    context_api=context_api,
                )
            return _LegacyProfileGoalManager(
                session_id=session_id,
                profile_home=profile_home,
                default_max_turns=_default_max_turns(),
            )
        except Exception as exc:
            logger.debug("Profile-scoped GoalManager unavailable: %s", exc)
            return None
    return GoalManager(session_id=session_id, default_max_turns=_default_max_turns())


def _state_payload(state: Any) -> Optional[Dict[str, Any]]:
    if state is None:
        return None
    return {
        "goal": getattr(state, "goal", "") or "",
        "status": getattr(state, "status", "") or "",
        "turns_used": int(getattr(state, "turns_used", 0) or 0),
        "max_turns": int(getattr(state, "max_turns", 0) or 0),
        "last_verdict": getattr(state, "last_verdict", None),
        "last_reason": getattr(state, "last_reason", None),
        "paused_reason": getattr(state, "paused_reason", None),
    }


def _payload(
    *,
    ok: bool = True,
    action: str,
    message: str,
    state: Any = None,
    error: str | None = None,
    kickoff_prompt: str | None = None,
    decision: Dict[str, Any] | None = None,
    message_key: str | None = None,
    message_args: list[Any] | None = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "ok": bool(ok),
        "action": action,
        "message": message,
        "goal": _state_payload(state),
    }
    if error:
        body["error"] = error
    if kickoff_prompt:
        body["kickoff_prompt"] = kickoff_prompt
    if decision is not None:
        body["decision"] = decision
    if message_key:
        body["message_key"] = message_key
    if message_args is not None:
        body["message_args"] = [a for a in message_args if a is not None]
    return body


def _goal_status_payload(state: Any, *, default_message: str | None = None) -> Dict[str, Any]:
    """Build localized-status style payload fields from a goal state."""
    if default_message is None:
        default_message = "No active goal. Set one with /goal <text>."
    if state is None:
        return {"message": default_message, "message_key": "goal_status_none"}
    status = str(getattr(state, "status", "") or "").strip()
    if status in ("cleared",):
        return {"message": default_message, "message_key": "goal_status_none"}
    turns_used = int(getattr(state, "turns_used", 0) or 0)
    max_turns = int(getattr(state, "max_turns", 0) or 0)
    goal = str(getattr(state, "goal", "") or "")
    if status == "active":
        return {
            "message": f"⊙ Goal (active, {turns_used}/{max_turns} turns): {goal}",
            "message_key": "goal_status_active",
            "message_args": [turns_used, max_turns, goal],
        }
    if status == "paused":
        reason = str(getattr(state, "paused_reason", "") or "")
        return {
            "message": f"⏸ Goal (paused, {turns_used}/{max_turns}{' — ' + reason if reason else ''}): {goal}",
            "message_key": "goal_status_paused",
            "message_args": [turns_used, max_turns, reason, goal],
        }
    if status == "done":
        return {
            "message": f"✓ Goal done ({turns_used}/{max_turns}): {goal}",
            "message_key": "goal_status_done",
            "message_args": [turns_used, max_turns, goal],
        }
    return {
        "message": f"Goal ({status}, {turns_used}/{max_turns}): {goal}",
        "message_args": [status, turns_used, max_turns, goal],
    }


def _extract_goal_turns_from_message(message: str) -> tuple[int, int]:
    """Best-effort extraction for continuation messages like '(1/20)'."""
    if not message:
        return 0, 0
    match = re.search(r"\((\d+)\s*/\s*(\d+)\)", message)
    if not match:
        return 0, 0
    try:
        return int(match.group(1)), int(match.group(2))
    except Exception:
        return 0, 0


def _goal_decision_payload(
    decision: Dict[str, Any],
    state: Any,
) -> Dict[str, Any]:
    """Attach goal message i18n key/args to an evaluation decision."""
    if not isinstance(decision, dict):
        return decision
    status = str(decision.get("status") or "").strip()
    reason = str(decision.get("reason") or "").strip()
    turns_used = int(getattr(state, "turns_used", 0) or 0)
    max_turns = int(getattr(state, "max_turns", 0) or 0)
    if (turns_used, max_turns) == (0, 0):
        turns_used, max_turns = _extract_goal_turns_from_message(str(decision.get("message") or ""))

    if status == "done":
        return {
            **decision,
            "message_key": "goal_achieved",
            "message_args": [reason],
        }
    if status == "paused":
        return {
            **decision,
            "message_key": "goal_paused_budget_exhausted",
            "message_args": [turns_used, max_turns],
        }
    if decision.get("should_continue"):
        return {
            **decision,
            "message_key": "goal_continuing",
            "message_args": [turns_used, max_turns, reason],
        }
    return decision


def goal_state_snapshot(session_id: str, *, profile_home: str | Path | None = None) -> Any:
    """Return a deep copy of current goal state for rollback before kickoff."""
    mgr = _manager(str(session_id or ""), profile_home=profile_home)
    if mgr is None:
        return None
    return copy.deepcopy(getattr(mgr, "state", None))


def restore_goal_state(session_id: str, snapshot: Any, *, profile_home: str | Path | None = None) -> None:
    """Restore a prior goal state after kickoff stream creation fails."""
    mgr = _manager(str(session_id or ""), profile_home=profile_home)
    if mgr is None:
        return
    if snapshot is None:
        try:
            mgr.clear()
        except Exception:
            pass
        return
    if isinstance(mgr, _ProfileGoalManager):
        mgr._restore_state(snapshot)
        return
    if isinstance(mgr, _LegacyProfileGoalManager):
        mgr._state = snapshot
        mgr._save(snapshot)
        return
    try:
        from hermes_cli.goals import save_goal  # type: ignore

        save_goal(str(session_id or ""), snapshot)
    except Exception as exc:  # pragma: no cover - native fallback only
        logger.debug("Goal state restore failed for %s: %s", session_id, exc)


def goal_command_payload(
    session_id: str,
    args: str = "",
    *,
    stream_running: bool = False,
    profile_home: str | Path | None = None,
) -> Dict[str, Any]:
    """Return the WebUI response payload for a /goal command.

    Mirrors the gateway command semantics:
    - /goal or /goal status shows status
    - /goal pause pauses
    - /goal resume resumes without auto-starting a turn
    - /goal clear|stop|done clears
    - /goal <text> sets a new active goal and returns kickoff_prompt so the
      caller can start the first normal user-role turn immediately.
    """
    sid = str(session_id or "").strip()
    if not sid:
        return _payload(ok=False, action="error", error="missing_session", message="session_id required")

    mgr = _manager(sid, profile_home=profile_home)
    if mgr is None:
        return _payload(ok=False, action="error", error="unavailable", message="Goals unavailable on this session.")

    text = str(args or "").strip()
    lower = text.lower()

    if not text or lower == "status":
        state = getattr(mgr, "state", None)
        status_payload = _goal_status_payload(state)
        return _payload(action="status", state=state, **status_payload)

    if lower == "pause":
        state = mgr.pause(reason="user-paused")
        if state is None:
            return _payload(
                ok=False,
                action="pause",
                error="no_goal",
                message="No goal set.",
                message_key="goal_no_goal",
            )
        return _payload(
            action="pause",
            message=f"⏸ Goal paused: {state.goal}",
            message_key="goal_paused",
            message_args=[str(state.goal)],
            state=state,
        )

    if lower == "resume":
        state = mgr.resume()
        if state is None:
            return _payload(
                ok=False,
                action="resume",
                error="no_goal",
                message="No goal to resume.",
                message_key="goal_no_goal",
            )
        return _payload(
            action="resume",
            message=(
                f"▶ Goal resumed: {state.goal}\n"
                "Send a new message, or type continue, to kick it off."
            ),
            message_key="goal_resumed",
            message_args=[str(state.goal)],
            state=state,
        )

    if lower in ("clear", "stop", "done"):
        had = bool(mgr.has_goal())
        mgr.clear()
        return _payload(
            action="clear",
            message="Goal cleared." if had else "No active goal.",
            message_key="goal_cleared" if had else "goal_no_goal",
            state=getattr(mgr, "state", None),
        )

    if stream_running:
        return _payload(
            ok=False,
            action="set",
            error="agent_running",
            message=(
                "Agent is running — use /goal status / pause / clear mid-run, "
                "or /stop before setting a new goal."
            ),
        )

    try:
        state = mgr.set(text)
    except ValueError as exc:
        return _payload(ok=False, action="set", error="invalid_goal", message=f"Invalid goal: {exc}")

    return _payload(
        action="set",
        message=(
            f"⊙ Goal set ({state.max_turns}-turn budget): {state.goal}\n"
            "I'll keep working until the goal is done, you pause/clear it, or the budget is exhausted.\n"
            "Controls: /goal status · /goal pause · /goal resume · /goal clear"
        ),
        message_key="goal_set",
        message_args=[state.max_turns, state.goal],
        state=state,
        kickoff_prompt=state.goal,
    )


def has_active_goal(
    session_id: str,
    *,
    profile_home: str | Path | None = None,
) -> bool:
    """Return True when the session has an active standing goal to evaluate."""
    sid = str(session_id or "").strip()
    if not sid:
        return False
    mgr = _manager(sid, profile_home=profile_home)
    if mgr is None:
        return False
    try:
        return bool(mgr.is_active())
    except Exception as exc:
        logger.debug("goal active-state check failed for session=%s: %s", sid, exc)
        return False


def evaluate_goal_after_turn(
    session_id: str,
    last_response: str,
    *,
    user_initiated: bool = True,
    profile_home: str | Path | None = None,
) -> Dict[str, Any]:
    """Evaluate a completed turn against the standing goal, if any."""
    sid = str(session_id or "").strip()
    if not sid:
        return {
            "status": None,
            "should_continue": False,
            "continuation_prompt": None,
            "verdict": "inactive",
            "reason": "missing session_id",
            "message": "",
        }
    mgr = _manager(sid, profile_home=profile_home)
    if mgr is None:
        return {
            "status": None,
            "should_continue": False,
            "continuation_prompt": None,
            "verdict": "inactive",
            "reason": "goals unavailable",
            "message": "",
        }
    try:
        if not mgr.is_active():
            return {
                "status": getattr(getattr(mgr, "state", None), "status", None),
                "should_continue": False,
                "continuation_prompt": None,
                "verdict": "inactive",
                "reason": "no active goal",
                "message": "",
            }
        decision = mgr.evaluate_after_turn(str(last_response or ""), user_initiated=user_initiated)
    except Exception as exc:
        logger.debug("goal evaluation failed for session=%s: %s", sid, exc)
        return {
            "status": None,
            "should_continue": False,
            "continuation_prompt": None,
            "verdict": "error",
            "reason": f"goal evaluation failed: {type(exc).__name__}",
            "message": "",
        }
    if not isinstance(decision, dict):
        decision = {}
    decision.setdefault("should_continue", False)
    decision.setdefault("continuation_prompt", None)
    decision.setdefault("message", "")
    decision = dict(decision)
    decision = _goal_decision_payload(decision, getattr(mgr, "state", None))
    return decision


# ── #6885 goal-continuation admission (single-record lifecycle) ─────────────
# Round 2 (maintainer review 2026-09-26): the marker set and the prompt map
# are one logical record. Writers register both under the lock; readers admit
# a turn only after the wire text is normalized to the canonical semantic
# user text. Helpers live here (not routes.py) because streaming.py,
# gateway_chat.py and routes.py all mutate the pair — a shared module owned by
# the goal domain keeps add/consume/sweep atomic in one place.
from api import config as _cfg


def _goal_continuation_normalize_wire_text(text: str) -> str:
    """Strip the browser's transport decorations from an outgoing /chat/start
    message, yielding the canonical semantic user text (#6885 round 2).

    The browser rewrites the composer text before POSTing (static/messages.js
    ``send()``): ``/use <skill>`` arms a directive, the forced-skill envelope
    and the queued-continuation tail are all prepended to whatever the user
    (or the queue) submitted, and file uploads append an attached-files
    marker. The goal-continuation prompt recorded server-side is the RAW
    semantic text, so the admission check must compare like with like or a
    legitimate continuation fails the match and the marker pair leaks.

    Server-side equivalent of static/sessions.js ``_stripForcedSkillEnvelope``
    / ``_stripAttachedFilesMarker``; deliberately anchored so a user-authored
    lookalike envelope in the middle of a message is NOT stripped (the front
    text, before the first envelope, is only admitted when what remains is
    exactly the continuation prompt).
    """
    value = str(text or "").strip()
    if not value:
        return ""
    # Leading `[USER OVERRIDE] ...` directive line (`/use`), line-anchored.
    value = re.sub(r"^\[USER OVERRIDE\][^\n]*\n*", "", value).strip()
    # Forced-skill envelope blocks, line-anchored at the front (repeat: a
    # second consecutive envelope is still transport, not user content).
    while True:
        matched = re.match(
            r"\[FORCED SKILL CONTEXT:[^\]]+\][\s\S]*?\[/FORCED SKILL CONTEXT\]\s*",
            value,
        )
        if not matched:
            break
        value = value[matched.end():].strip()
    # Attached-files tail appended by the upload path.
    value = re.sub(r"\n\n\[Attached files: [^\]]+\]$", "", value).strip()
    return value


def register_pending_goal_continuation(session_id: str, continuation_prompt: str) -> str | None:
    """Record a pending goal continuation as ONE marker+prompt+ID record.

    Returns the continuation ID when a record was written (non-empty prompt),
    or None when there was nothing to register — callers must only emit the
    goal_continue SSE event when this returns a truthy ID so the frontend
    queue and the server record cannot disagree.

    The ID is the admission token (#7855): the browser carries it on the
    queued entry through inline edits and combines, and /api/chat/start
    validates it. A genuine user message never carries it, which is the
    #6885 distinction. Matching on the prompt text is what previously
    dropped the goal after an edit or a late send.

    No wall-clock expiry (#7855): the browser deliberately keeps a queued
    continuation — it survives a refresh and is restored into the composer
    for the user to send later — so a TTL retired a continuation that was
    still perfectly usable. A record now lives until it is consumed
    (admitted-and-consumed by its ID) or explicitly retired (goal cleared /
    paused / session retired).
    """
    session_id = str(session_id or "").strip()
    prompt = str(continuation_prompt or "").strip()
    if not session_id or not prompt:
        return None
    record = {
        "prompt": prompt,
        "continuation_id": uuid.uuid4().hex,
    }
    with _cfg.PENDING_GOAL_CONTINUATION_LOCK:
        _cfg.PENDING_GOAL_CONTINUATION.add(session_id)
        _cfg.PENDING_GOAL_CONTINUATION_PROMPTS[session_id] = record
    return str(record["continuation_id"])


def peek_pending_goal_continuation_id(session_id: str) -> str | None:
    """Return the pending continuation ID for *session_id* without consuming.

    Used by the gateway when it re-emits the goal_continue event for a
    reconnecting stream so the browser and the server record agree.
    """
    session_id = str(session_id or "").strip()
    if not session_id:
        return None
    with _cfg.PENDING_GOAL_CONTINUATION_LOCK:
        record = _cfg.PENDING_GOAL_CONTINUATION_PROMPTS.get(session_id)
        if not isinstance(record, dict):
            return None
        cont_id = str(record.get("continuation_id") or "").strip()
        return cont_id or None


def consume_pending_goal_continuation(session_id: str, msg: str) -> str | dict | bool | None:
    """Admit the pending continuation record for *session_id* by its ID.

    *msg* is the continuation ID issued by
    :func:`register_pending_goal_continuation`. On a match the record is
    removed, the turn becomes goal-related, and the consumed record is
    returned as a **rollback receipt** (a dict, or the session id when only
    the in-memory marker had no record half). Any other value (a genuine user
    turn, or a queued turn whose entry lost its ID) keeps user priority and
    the record stays for the browser's real dispatch; ``None`` or ``False``
    is returned. Fails closed when no record is present.

    Matching on the ID rather than the prompt text is the #7855 fix: the
    browser's queue lets the user edit or combine a queued continuation, and
    an exact-text comparison silently turned those into ordinary turns.
    There is no wall-clock expiry — a late send (after any amount of time,
    including across a refresh) is still admitted, because the browser
    deliberately keeps queued continuations alive.

    The receipt exists because a chat start can be rejected *after*
    admission (#7249 rolls back rejected starts). Re-adding only the marker
    on rollback — the pre-#7855 shape — left the retry unmatchable, because
    admission also pops the record half. Returning both halves lets
    :func:`restore_pending_goal_continuation` put the pair back together (see
    its "newer continuation armed" guard).
    """
    session_id = str(session_id or "").strip()
    if not session_id:
        return False
    incoming = str(msg or "").strip()
    if not incoming:
        return False
    # A genuine user turn is arbitrary text (any language). Admission is by
    # ID, so a non-ASCII-ineligible value can never match — reject it before
    # compare_digest, which raises TypeError on non-ASCII input.
    if any(c not in "0123456789abcdef" for c in incoming.lower()) or len(incoming) != 32:
        return False
    with _cfg.PENDING_GOAL_CONTINUATION_LOCK:
        if session_id not in _cfg.PENDING_GOAL_CONTINUATION:
            return False
        record = _cfg.PENDING_GOAL_CONTINUATION_PROMPTS.get(session_id)
        if not isinstance(record, dict):
            # Marker without record (legacy/abnormal state): fail closed and
            # clear the broken half so the session is not stuck (#6885).
            _drop_pending_goal_continuation(session_id)
            return False
        cont_id = str(record.get("continuation_id") or "").strip()
        if not cont_id:
            # Legacy record without an ID (pre-#7855 process): fail closed and
            # clear, so the session is not stuck with an unmatchable record.
            _drop_pending_goal_continuation(session_id)
            return False
        if not secrets.compare_digest(cont_id, incoming):
            # Mismatch: keep the record for the browser's real dispatch.
            return False
        _drop_pending_goal_continuation(session_id)
        return dict(record)


def restore_pending_goal_continuation(session_id: str, receipt) -> bool:
    """Re-arm an admitted continuation from *receipt* (rollback path).

    Called when a chat start that admitted a continuation is rejected
    (#7249's transactional rollback). Restoring the marker without the
    record — the pre-#7855 shape — left the retry unable to match, because
    admission pops both halves.

    The *newer continuation* guard is what keeps rollback honest: if another
    ``register_pending_goal_continuation`` armed this session while the start
    was in flight, this receipt is stale and must NOT overwrite the newer
    record (that would burn a continuation the browser has not sent yet).
    Restoring an older ID would also resurrect a consumed one.

    ``receipt`` is what
    :func:`consume_pending_goal_continuation` returned: the record dict, or
    the session id when only the marker was present.
    """
    session_id = str(session_id or "").strip()
    if not session_id or receipt is None or receipt is False:
        return False
    with _cfg.PENDING_GOAL_CONTINUATION_LOCK:
        if session_id in _cfg.PENDING_GOAL_CONTINUATION_PROMPTS:
            # A newer continuation is armed; keep it and drop the stale receipt.
            return False
        if isinstance(receipt, dict):
            record = dict(receipt)
            if not str(record.get("continuation_id") or "").strip():
                return False
        else:
            # Marker-only admission (no popped record): re-arm the marker
            # alone, matching #7249's rollback for the legacy half-pair.
            record = None
        _cfg.PENDING_GOAL_CONTINUATION.add(session_id)
        if record is not None:
            _cfg.PENDING_GOAL_CONTINUATION_PROMPTS[session_id] = record
        return True


def _drop_pending_goal_continuation(session_id: str) -> None:
    """Remove both halves of the record. Caller holds the lock."""
    _cfg.PENDING_GOAL_CONTINUATION.discard(session_id)
    _cfg.PENDING_GOAL_CONTINUATION_PROMPTS.pop(session_id, None)


def clear_pending_goal_continuation(session_id: str) -> None:
    """Drop any pending continuation record for *session_id* (goal clear /
    pause / session-retirement paths)."""
    session_id = str(session_id or "").strip()
    if not session_id:
        return
    with _cfg.PENDING_GOAL_CONTINUATION_LOCK:
        _drop_pending_goal_continuation(session_id)


def sweep_expired_goal_continuations() -> int:
    """Remove every orphaned continuation record; return count.

    An orphan is a marker in the set whose record is missing — the reverse
    (record without marker) is cleared here too, so the two halves cannot
    drift apart. Safe to call from any thread; called from the /goal command
    handler so the sweep runs on live traffic without a background timer.

    There is no wall-clock sweep (#7855): the browser deliberately keeps a
    queued continuation (it survives a refresh and is restored into the
    composer for a later send), so a TTL retired continuations that were
    still usable. Records end when they are consumed or the goal/session
    explicitly retires them.
    """
    swept = 0
    with _cfg.PENDING_GOAL_CONTINUATION_LOCK:
        for session_id in list(_cfg.PENDING_GOAL_CONTINUATION_PROMPTS.keys()):
            record = _cfg.PENDING_GOAL_CONTINUATION_PROMPTS.get(session_id)
            if not isinstance(record, dict) or not str(record.get("continuation_id") or "").strip():
                _drop_pending_goal_continuation(session_id)
                swept += 1
        for session_id in list(_cfg.PENDING_GOAL_CONTINUATION):
            if session_id not in _cfg.PENDING_GOAL_CONTINUATION_PROMPTS:
                _drop_pending_goal_continuation(session_id)
                swept += 1
    return swept
