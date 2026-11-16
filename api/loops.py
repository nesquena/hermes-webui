"""WebUI /loop: hermes_cli.loops owns the state; one server thread fires and judges ticks."""
import json
import logging
import threading
import weakref
from contextlib import contextmanager
from pathlib import Path

_WAKE = threading.Event()
_LOCKS = weakref.WeakValueDictionary()
_LOCKS_GUARD = threading.Lock()
_TURN_PREFIX = "webui_loop_turn:"


def _session_lock(session_id):
    """One lock per session: the /loop command, the scheduler and profile retagging all take it."""
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(session_id, threading.Lock())


@contextmanager
def _profile_env(profile):
    from api.profiles import profile_env_for_background_worker
    with profile_env_for_background_worker(profile or "", "/loop judge"):
        yield


@contextmanager
def _home(profile):
    from api.profiles import get_hermes_home_for_profile
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(str(get_hermes_home_for_profile(profile)))
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def _cas(db, key, expected, value):
    """Write state_meta[key]=value only if it still holds `expected` (another writer wins otherwise)."""
    def _do(conn):
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        if (row[0] if row else None) != expected:
            return False
        conn.execute("INSERT INTO state_meta (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        return True
    return db._execute_write(_do)


def _wakeup_outcome(messages, turn):
    """(cancelled, reply) of the recorded wakeup turn, read from that turn's own rows only.

    `cancelled` is "server" when the WebUI server, not the user, cancelled the turn.
    """
    from types import SimpleNamespace
    from api.streaming import _cancel_initiator_is_server, _session_has_cancel_marker
    msgs, turn = list(messages or []), turn or {}
    token, at = turn.get("token"), turn.get("started_at")
    for i, m in enumerate(msgs):
        # Stop's recovered row carries the same token; a pre-upgrade row only the exact pending time.
        if m.get("role") == "user" and ((token and m.get("_active_turn_token") == token)
                                        or (at and m.get("timestamp") == at)):
            # Mid-run Steer rows and Stop's same-token recovery row belong to this turn; only a
            # real user row (a new execution boundary) ends it.
            end = next((j for j in range(i + 1, len(msgs)) if msgs[j].get("role") == "user"
                        and msgs[j].get("display_kind") != "steer"
                        and not (token and msgs[j].get("_active_turn_token") == token)), len(msgs))
            rows = msgs[i + 1:end]
            replies = [n for n in rows if n.get("role") == "assistant"]
            cancelled = _session_has_cancel_marker(SimpleNamespace(messages=[n for n in rows if n.get("role") != "user"]))
            if cancelled and any(_cancel_initiator_is_server(n.get("cancel_initiator")) for n in replies):
                cancelled = "server"
            return cancelled, str(replies[-1].get("content") or "") if replies else ""
    return False, ""


def _turn_record(db, sid):
    """The wakeup turn fired for this loop; walks the whole compression parent chain, then re-keys it to sid."""
    raw, seen, cur = db.get_meta(_TURN_PREFIX + sid), {sid}, sid
    while raw is None:  # several rotations in one wakeup leave the record on an older ancestor
        cur = (db.get_session(cur) or {}).get("parent_session_id")
        if not cur or cur in seen:
            break
        seen.add(cur)
        raw = db.get_meta(_TURN_PREFIX + cur)
    if raw is not None and cur != sid:
        db.set_meta(_TURN_PREFIX + sid, raw)
    return json.loads(raw or "{}")


def _turn_running(turn):
    """True while the wakeup turn's own stream is alive, whatever session id it is filed under now."""
    from api import config
    stream_id = turn.get("stream_id")
    with config.STREAMS_LOCK:
        live = stream_id in config.STREAMS
    with config.ACTIVE_RUNS_LOCK:
        return bool(stream_id) and (live or stream_id in config.ACTIVE_RUNS)


@contextmanager
def _loop_manager(session_id, profile):
    """The session's LoopManager in its profile's store; None when the agent isn't installed."""
    try:
        from hermes_cli.loops import LoopManager
    except ImportError:  # no agent installed: no loop store
        yield None
        return
    with _home(profile):
        yield LoopManager(session_id=session_id)


def retag_session_profile(session, profile, retag):
    """Run `retag(session, profile)` under the loop lock; a real move clears the loop left in the old profile."""
    with _session_lock(session.session_id):
        old = getattr(session, "profile", None)
        session_profile, result = retag(session, profile)
        if result == "retagged":
            with _loop_manager(session.session_id, old) as mgr:
                if mgr:
                    mgr.clear()
        return session_profile, result


@contextmanager
def archive_guard(session_id):
    """Hold the loop lock (taken before the agent lock) across an archive; pause the loop if it archived.

    Yields a dict; the caller stores the archived session under "session" once it is saved.
    """
    out = {}
    with _session_lock(session_id):
        yield out
        s = out.get("session")
        if s is not None and getattr(s, "archived", False):
            with _loop_manager(session_id, getattr(s, "profile", None)) as mgr:
                if mgr:
                    mgr.pause(reason="session archived")


def _chat_writable(session):
    """chat/start's write admission for a stored session (read-only, subagent, foreign file, gateway)."""
    from api.config import get_config
    from api.gateway_chat import gateway_session_chat_read_only_reason, webui_gateway_chat_enabled
    from api.gateway_sessions import configured
    from api.routes import _is_subagent_child_session_id
    fields = dict(vars(session))
    source = str(fields.get("source_tag") or fields.get("raw_source") or "").strip().lower()
    canonical = webui_gateway_chat_enabled(get_config()) and configured(fields)
    return not (getattr(session, "_sidecar_names_other_session", None) or gateway_session_chat_read_only_reason(fields)
                or (getattr(session, "read_only", False) and not canonical)
                or source == "subagent" or _is_subagent_child_session_id(session.session_id))


def run_loop_command(session_id, args, request_profile=None):
    """`request_profile`: the caller's profile; re-checked under the lock a retag takes."""
    from api.models import get_session
    from api.profiles import _profiles_match
    from hermes_cli.loops import _CONTROL_COMMANDS, LoopManager, dispatch_loop_command, parse_loop_args
    p = parse_loop_args(args)
    if not p["error"] and p["prompt"].startswith("/"):  # WebUI slash commands run in the browser, not the agent
        return "/loop: looping slash commands isn't supported in the WebUI."
    with _session_lock(session_id):
        try:
            session = get_session(session_id, metadata_only=True)
            profile = session.profile
        except KeyError:  # fail closed: never fall back to another profile's state.db
            return "/loop: open a saved chat first."
        if request_profile is not None and not _profiles_match(profile, request_profile):
            return "/loop: this chat moved to another profile; nothing was changed."
        # A new loop sends turns into this chat: same write admission as chat/start. Controls stay allowed.
        if args.strip().lower() not in _CONTROL_COMMANDS and not _chat_writable(session):
            return "/loop: this chat is read-only in the WebUI, so it can't run a loop."
        with _home(profile):
            out = dispatch_loop_command(LoopManager(session_id=session_id), args,
                                        route={"platform": "webui", "chat_id": session_id})
    _WAKE.set()
    return out["output"]


def _conditional_manager(session_id, db):
    """LoopManager for one session whose saves only land if the row is still the one this pass read."""
    from hermes_cli import loops as cli

    class _CasLoop(cli.LoopManager):
        def __init__(self):
            self.session_id, self._db, self._key = session_id, db, cli._meta_key(session_id)
            self._raw = db.get_meta(self._key)
            self._state = cli._parse_state(self._raw, session_id) if self._raw else None
            self.lost = False

        def _save(self):
            new = self._state.to_json()
            if not self.lost and _cas(self._db, self._key, self._raw, new):
                self._raw = new
            else:
                self.lost = True  # a /loop command or another process changed it first
            return self._state
    return _CasLoop()


def _same_home(a, b):
    from api.profiles import get_hermes_home_for_profile
    return Path(get_hermes_home_for_profile(a)).resolve() == Path(get_hermes_home_for_profile(b)).resolve()


def _live_profiles():
    """Root first, then live named profiles (valid id, not tombstoned), each home scanned once."""
    from api.profiles import _PROFILE_ID_RE, _profiles_root, get_hermes_home_for_profile
    root = _profiles_root()
    names = sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    out, seen = [], set()
    for name in [None] + names:
        if name is not None and (not _PROFILE_ID_RE.fullmatch(name) or (root / ".deleted" / name).exists()):
            continue
        home = Path(get_hermes_home_for_profile(name)).resolve()
        if home not in seen:
            seen.add(home)
            out.append(name)
    return out


def _run_one(profile, sid):
    from api.background_process import _session_has_active_turn
    from api.models import get_session
    from api.process_event_utils import build_active_turn_token
    from api.routes import start_session_turn
    from hermes_cli import loops as cli
    db = cli._get_session_db()
    if db is None:
        return
    mgr = _conditional_manager(sid, db)
    state = mgr.state
    if (state is None or state.status != "active" or (state.route or {}).get("platform") != "webui"
            or _session_has_active_turn(sid)):
        return
    try:
        if state.awaiting_response:  # judge only once the wakeup turn settled: same verdicts as the CLI hook
            turn = _turn_record(db, sid)
            if _turn_running(turn):  # e.g. compression moved the loop to a child id mid-turn
                return
            if turn.get("tick") != state.ticks_fired or not turn.get("launched"):
                mgr.abandon_tick()  # the process died between fire_tick and launch: this tick never ran
                return
            cancelled, reply = _wakeup_outcome(get_session(sid).messages, turn)
            if cancelled == "server":
                mgr.abandon_tick()  # a server restart/settle, not the user's Stop: run the tick again
            elif cancelled:
                with _profile_env(profile):
                    mgr.pause(reason="user-interrupted (Stop)")
            else:
                with _profile_env(profile):  # the --until judge reads this profile's provider credentials
                    mgr.complete_tick(reply)
            return
        if cli.goal_blocks_loop_tick(sid):
            return
        meta = get_session(sid, metadata_only=True)
        if not _same_home(meta.profile, profile):
            # Never launch here; a real retag already cleared under the loop lock, so only pause.
            mgr.pause(reason="session belongs to another profile")
            return
        if getattr(meta, "archived", False):
            mgr.pause(reason="session archived")  # a hidden chat must not keep spending provider calls
            return
        msg = mgr.fire_tick()
        if not msg or mgr.lost:
            return
        db.set_meta(_TURN_PREFIX + sid, json.dumps({"tick": mgr.state.ticks_fired}))
        try:
            resp = start_session_turn(sid, msg, source="loop_wakeup")
        except Exception:  # nothing ran: roll the tick back so it is never judged
            mgr.abandon_tick()
            raise
        status = resp.get("_status", 200)
        if status >= 400:
            mgr.clear() if status == 404 else mgr.abandon_tick()
            return
        at, stream = resp.get("pending_started_at"), resp.get("stream_id")
        db.set_meta(_TURN_PREFIX + sid, json.dumps(
            {"token": build_active_turn_token(stream, at), "started_at": at, "stream_id": stream,
             "tick": mgr.state.ticks_fired, "launched": True}))
    except KeyError:  # session deleted
        mgr.clear()


def run_due_loops():
    from api.profiles import _is_isolated_profile_mode, _isolated_profile_name
    from hermes_cli.loops import list_active_loops
    profiles = [_isolated_profile_name()] if _is_isolated_profile_mode() else _live_profiles()
    for profile in profiles:
        with _home(profile):
            for sid, state in list_active_loops():
                if (state.route or {}).get("platform") != "webui":
                    continue
                try:
                    with _session_lock(sid):
                        _run_one(profile, sid)
                except Exception:
                    logging.getLogger(__name__).warning("/loop tick failed for %s", sid, exc_info=True)


def start_loop_scheduler():
    """Rollback: HERMES_WEBUI_LOOP_SCHEDULER=0 never starts the thread; saved loops just stay due."""
    import os
    if os.environ.get("HERMES_WEBUI_LOOP_SCHEDULER", "1").strip().lower() in ("0", "false", "off", "no"):
        logging.getLogger(__name__).info("/loop scheduler disabled by HERMES_WEBUI_LOOP_SCHEDULER")
        return False

    def _run():
        while True:
            _WAKE.wait(15)
            _WAKE.clear()
            try:
                run_due_loops()
            except Exception:
                logging.getLogger(__name__).debug("/loop scheduler pass failed", exc_info=True)
    threading.Thread(target=_run, name="webui-loop-scheduler", daemon=True).start()
    return True
