"""WebUI /refine: spawn the memory/skill review fork for a chat.

Mirrors the CLI's ``_handle_refine_command`` and the gateway's handler of the
same name: the review runs on a daemon thread against a snapshot of the chat's
cached AIAgent conversation, so the live session and prompt cache are never
touched. Refusal and acknowledgement wording follows the CLI verbatim.

Completions arrive on ``agent.background_review_callback``; the sink wired here
appends the summary to the chat transcript (the gate lives upstream in
``summarize_background_review_actions`` — e.g. display.memory_notifications
"off" produces no summary at all).
"""

import logging
import time
from contextlib import contextmanager

logger = logging.getLogger(__name__)

NOTHING_YET_TEXT = "Nothing to refine yet — send a message first."
EMPTY_TEXT = "Nothing to refine yet — the conversation is empty."
BUSY_TEXT = "Agent is running — wait for the turn to finish, then /refine."


@contextmanager
def _home(profile):
    """Point hermes_home at *profile* while the review fork spawns.

    ``_spawn_background_review`` captures the active context into its thread,
    so the fork's config/memory/skill reads and writes must see this profile's
    home (same pattern as the WebUI's other agent-side spawns).
    """
    from api.profiles import get_hermes_home_for_profile
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(str(get_hermes_home_for_profile(profile)))
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def _cached_agent(session_id):
    """The idle cached AIAgent for a chat (WebUI agent cache), or None."""
    from api import config

    with config.SESSION_AGENT_CACHE_LOCK:
        entry = config.SESSION_AGENT_CACHE.get(session_id)
        if entry is None:
            return None
        config.SESSION_AGENT_CACHE.move_to_end(session_id)
        return entry[0] if isinstance(entry, tuple) else entry


def make_background_review_callback(session_id):
    """Completion sink for a chat's cached agent: append the summary to the transcript."""

    def _deliver(message: str) -> None:
        deliver_review_summary(session_id, message)

    return _deliver


def deliver_review_summary(session_id: str, message: str) -> None:
    """Append a finished review's summary row to its chat and refresh the sidebar.

    Mirrors ``_persist_handoff_summary_locally``: full session load, one appended
    row, atomic save. Failures only log — a completion must never crash the fork.
    """
    text = str(message or "").strip()
    sid = str(session_id or "").strip()
    if not text or not sid:
        return
    try:
        from api.models import get_session

        session = get_session(sid)
    except Exception:
        return
    try:
        session.messages.append(
            {
                "role": "assistant",
                "content": text,
                "timestamp": time.time(),
            }
        )
        session.save()
    except Exception as exc:
        logger.warning(
            "Failed to record background review summary in session %s: %s", sid, exc
        )
        return
    try:
        from api.session_events import publish_session_list_changed

        publish_session_list_changed("background_review", session_id=sid)
    except Exception:
        logger.debug("Session-list refresh after review summary failed", exc_info=True)


def run_refine_command(session_id, args, request_profile=None):
    """``/refine [focus]`` for one chat; returns the transcript text to show.

    Runs under the session's agent lock so a turn cannot start between the
    idle check, the snapshot, and the spawn. Refusals mirror the CLI/gateway:
    a live turn, no cached agent yet, or an empty conversation.
    """
    from api.background_process import _session_has_active_turn
    from api.config import _get_session_agent_lock
    from api.models import get_session
    from api.profiles import _profiles_match

    sid = str(session_id or "").strip()
    focus = str(args or "").strip() or None
    if not sid:
        return NOTHING_YET_TEXT
    with _get_session_agent_lock(sid):
        try:
            profile = get_session(sid, metadata_only=True).profile
        except KeyError:
            return NOTHING_YET_TEXT
        if request_profile is not None and not _profiles_match(
            profile, request_profile
        ):
            return "/refine: this chat belongs to another profile; nothing was started."
        if _session_has_active_turn(sid):
            return BUSY_TEXT
        agent = _cached_agent(sid)
        if agent is None:
            return NOTHING_YET_TEXT
        snapshot = list(getattr(agent, "_session_messages", None) or [])
        if not snapshot:
            return EMPTY_TEXT
        if (
            hasattr(agent, "background_review_callback")
            and getattr(agent, "background_review_callback", None) is None
        ):
            # Deliver the eventual completion into this chat. The CLI prints it
            # and the gateway forwards it as a message; without this sink the
            # WebUI would drop it.
            agent.background_review_callback = make_background_review_callback(sid)
        try:
            with _home(profile):
                agent._spawn_background_review(
                    messages_snapshot=snapshot,
                    review_memory=True,
                    review_skills="skill_manage"
                    in getattr(agent, "valid_tool_names", set()),
                    focus=focus,
                    explicit=True,
                )
        except Exception as exc:
            return f"/refine failed to start: {exc}"
        tail = f" (focus: {focus})" if focus else ""
        return (
            f"⚗ Reviewing this conversation in the background{tail} — "
            f"any memory/skill updates will be reported when done."
        )
