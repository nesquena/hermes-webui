"""WebUI /review: dispatch the independent reviewer subagent for a chat.

Mirrors the CLI's ``_handle_review_command`` and the gateway's handler of the
same name: the reviewer runs on the shared async-delegation rail
(``delegate_task(background=True)``) against a snapshot of the chat's cached
AIAgent conversation, and its report re-enters this chat as a normal
async-delegation completion (the WebUI's completion drain routes it back by
``origin_ui_session_id``, bound here to this chat). Refusal and
acknowledgement wording follows the CLI verbatim.
"""

from contextlib import contextmanager

NOTHING_YET_TEXT = "Nothing to review yet — send a message first."
BUSY_TEXT = "Agent is running — wait for the turn to finish, then /review."


@contextmanager
def _home(profile):
    """Point hermes_home at *profile* while the reviewer spawns.

    ``delegate_task`` resolves config, toolsets and ``auxiliary.review``
    credentials from the active home, so the dispatch must see this profile's
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


def run_review_command(session_id, args, request_profile=None):
    """``/review [focus]`` for one chat; returns the transcript text to show.

    Runs under the session's agent lock so a turn cannot start between the
    idle check, the snapshot, and the dispatch. Refusals mirror the CLI (the
    engine's own ``ValueError`` text verbatim) and the gateway's
    ``/review failed to start`` line. The per-turn session identity is bound
    so the reviewer's completion event carries this chat's
    ``origin_ui_session_id`` and routes back here (xsession wakeup routing).
    """
    from api.background_process import _session_has_active_turn
    from api.config import _get_session_agent_lock
    from api.models import get_session
    from api.profiles import _profiles_match
    from api.streaming import _bind_turn_session_identity

    sid = str(session_id or "").strip()
    prompt = str(args or "").strip()
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
            return "/review: this chat belongs to another profile; nothing was started."
        if _session_has_active_turn(sid):
            return BUSY_TEXT
        agent = _cached_agent(sid)
        if agent is None:
            return NOTHING_YET_TEXT
        messages = list(getattr(agent, "_session_messages", None) or [])
        try:
            with _home(profile), _bind_turn_session_identity(sid):
                from agent.review_engine import format_dispatch_note, start_review

                result = start_review(agent, messages, prompt)
        except ValueError as exc:
            return str(exc)
        except Exception as exc:
            return f"/review failed to start: {exc}"
        return format_dispatch_note(result, prompt)
