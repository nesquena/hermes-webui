"""#9019: ``get_session`` doesn't find a session whose file names another session.

When a session's file is replaced with a copy of another session's file (a backup restored over
it, a manual copy), the #8852 loader marks the reloaded object (``_sidecar_names_other_session``).
That object carries the other session's ``session_id``, ``profile``, ``workspace`` and ``path``, so
anything a route did through it acted on that other conversation (the victim): ``/api/background``
ran the Agent in the victim's profile, worktree removal targeted the victim's worktree, compression
recovery created a session in the victim's profile, title regeneration sent the victim's transcript
to the title model, ``/api/goal`` and ``/api/btw`` used the victim's goal and transcript.

The generic request guard already answers 404 for such a file when the session isn't open, because
it reads the owner from disk. These cases are the ones it can't see: the session is open (resident
in the cache, so the guard reads its real owner) when its file is replaced by a longer conversation,
and the route's full load then reloads the disk copy; or the session is evicted between the guard
and the load.

``get_session`` now raises ``SessionFileNamesOtherSession`` (a ``KeyError``, which every route
already answers ``404 Session not found``) for a marked object, unless the caller passes
``allow_foreign_body=True``. Fallbacks that treat ``KeyError`` as "no sidecar" and then claim,
import or synthesize a session under the same id re-raise it first, so a refused id is never
materialized over its file.
"""

import json
from urllib.parse import urlparse

import pytest

from tests.test_issue8852_body_id_write_target import (  # noqa: F401 (fixture)
    COPY,
    VICTIM,
    _copy,
    _Handler,
    _files,
    _unchanged_victim,
    _victim,
    store,
)

OWN = "own-9019"
OWN_MESSAGES = [
    {"role": "user", "content": "own q", "timestamp": 1},
    {"role": "assistant", "content": "own a", "timestamp": 2},
]


def _own(directory, workspace=None):
    """The requester's own session (profile ``default``), shorter than the victim's."""
    from api import models

    session = models.Session(session_id=OWN, title="Own", profile="default")
    if workspace is not None:
        session.workspace = str(workspace)
    session.messages = [dict(m) for m in OWN_MESSAGES]
    session.save()
    models.SESSIONS.clear()
    return directory / f"{OWN}.json"


def _swap_while_open(directory):
    """The own session is open (a resident full object) when its file is replaced with a copy of
    the victim's longer file. The request guard reads the resident owner (``default``), and the
    route's full load reloads the disk copy because the cache lags it."""
    from api import models

    resident = models.get_session(OWN)
    assert not getattr(resident, "_sidecar_names_other_session", None)
    (directory / f"{OWN}.json").write_bytes((directory / f"{VICTIM}.json").read_bytes())
    return resident


def _post(monkeypatch, path, body):
    """``handle_post`` end to end (request guard included); returns (kind, status, payload)."""
    import api.routes as routes

    replies = []

    def _status(args, kwargs, default):
        return kwargs.get("status", args[0] if args else default)

    monkeypatch.setattr(routes, "_check_csrf", lambda _h: True, raising=False)
    monkeypatch.setattr(routes, "read_body", lambda _h: dict(body))
    monkeypatch.setattr(routes, "j", lambda _h, obj, *a, **k: replies.append(("j", _status(a, k, 200), obj)) or True)
    monkeypatch.setattr(
        routes, "bad", lambda _h, msg, *a, **k: replies.append(("bad", _status(a, k, 400), msg)) or True,
    )

    class _PostHandler:
        headers = {"Content-Type": "application/json"}
        command = "POST"
        client_address = ("127.0.0.1", 0)

    handler = _PostHandler()
    handler.path = path
    routes.handle_post(handler, urlparse(path))
    return replies


NOT_FOUND = [("bad", 404, "Session not found")]


def _foreign_state_rows(monkeypatch):
    """state.db knows OWN (as it knows any WebUI session after a turn): the claim/import fallbacks
    would find it. Each is a recorder, so the test sees whether a refused id was materialized."""
    import api.routes as routes

    calls = []
    row = {"session_id": OWN, "title": "Own", "profile": "default", "source_tag": "webui", "model": "m"}
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid, **_k: dict(row) if sid == OWN else {})
    monkeypatch.setattr(routes, "get_cli_session_messages", lambda sid, **_k: [dict(m) for m in OWN_MESSAGES])

    def _import(sid, *a, **k):
        calls.append(("import", sid))
        raise AssertionError("imported a session over the file of a refused id")

    def _claim(sid, *a, **k):
        calls.append(("claim", sid))
        return None, "no_foreign_state"

    monkeypatch.setattr(routes, "import_cli_session", _import)
    monkeypatch.setattr(routes, "_claim_or_synthesize_cli_session", _claim)
    return calls


# --- the boundary ----------------------------------------------------------------------------


@pytest.mark.parametrize("metadata_only", [False, True])
@pytest.mark.parametrize("cached", [False, True])
def test_get_session_does_not_find_a_file_that_names_another_session(store, metadata_only, cached):  # noqa: F811 -- pytest fixture parameter
    from api import models

    _victim(store)
    _copy(store, partials=False)
    before = _files(store)
    if cached:
        models.SESSIONS[COPY] = models.Session.load(COPY)

    with pytest.raises(KeyError) as excinfo:
        models.get_session(COPY, metadata_only=metadata_only)

    assert isinstance(excinfo.value, models.SessionFileNamesOtherSession)
    assert COPY not in models.SESSIONS, "the refused object stayed in the cache"
    _unchanged_victim(store, before)
    assert _files(store) == before


def test_a_session_whose_file_is_replaced_while_open_is_not_found(store):  # noqa: F811 -- pytest fixture parameter
    from api import models

    _victim(store)
    _own(store)
    _swap_while_open(store)

    with pytest.raises(models.SessionFileNamesOtherSession):
        models.get_session(OWN)
    assert OWN not in models.SESSIONS


def test_an_ordinary_session_loads_as_before(store):  # noqa: F811 -- pytest fixture parameter
    from api import models

    _victim(store)
    session = models.get_session(VICTIM)
    assert session is models.get_session(VICTIM)
    assert session.session_id == VICTIM and len(session.messages) == 10
    assert models.get_session(VICTIM, metadata_only=True).session_id == VICTIM
    assert models.get_session(VICTIM, allow_foreign_body=True) is session
    assert models.get_session_for_scan(VICTIM).session_id == VICTIM


@pytest.mark.parametrize("metadata_only", [False, True])
def test_the_explicit_opt_in_still_gets_the_marked_object(store, metadata_only):  # noqa: F811 -- pytest fixture parameter
    """A caller that must see the object to refuse or repair it asks for it by name."""
    from api import models

    _victim(store)
    _copy(store, partials=False)
    session = models.get_session(COPY, metadata_only=metadata_only, allow_foreign_body=True)
    assert session.session_id == VICTIM
    assert session._sidecar_names_other_session == COPY
    with pytest.raises(RuntimeError, match="1558" if metadata_only else "8852"):
        session.save()


def test_the_search_scan_skips_a_file_that_names_another_session(store):  # noqa: F811 -- pytest fixture parameter
    """``get_session_for_scan`` (session content search) resolves without ``get_session``."""
    from api import models

    _victim(store)
    _copy(store, partials=False)
    assert models.get_session_for_scan(COPY) is None


def test_file_operations_never_fall_back_to_the_state_db_view(store, monkeypatch):  # noqa: F811 -- pytest fixture parameter
    """``get_session_for_file_ops`` serves a state.db-only session as a view on the active
    workspace. A refused id has a file: it is not found, not served as that view."""
    from api import models

    _victim(store)
    _copy(store, partials=False)
    monkeypatch.setattr(models, "state_db_has_session", lambda sid: True)
    with pytest.raises(models.SessionFileNamesOtherSession):
        models.get_session_for_file_ops(COPY)


def test_compression_recovery_lookup_skips_a_file_that_names_another_session(store, monkeypatch):  # noqa: F811 -- pytest fixture parameter
    from api import models

    _victim(store)
    _copy(store, partials=False)
    models.SESSIONS.clear()
    monkeypatch.setattr(models, "_persisted_session_ids_snapshot", lambda: frozenset({COPY}))
    monkeypatch.setattr(models, "_compression_recovery_child_matches", lambda *a, **k: True)
    assert models.find_compression_recovery_session("source-9019", "start_focused") is None


# --- routes the request guard can't see -----------------------------------------------------


@pytest.mark.parametrize("swap", [False, True])
def test_background_never_runs_the_agent_for_the_other_session(store, tmp_path, monkeypatch, swap):  # noqa: F811 -- pytest fixture parameter
    """On master ``/api/background`` built the background session from the reloaded object: the
    victim's profile (``work``) and workspace, and ran the Agent there."""
    import api.background as background
    import api.models as models
    import api.routes as routes

    _victim(store, workspace=tmp_path)
    _own(store, tmp_path)
    if swap:
        _swap_while_open(store)
    created, ran = [], []
    real_new_session = models.new_session

    def _new_session(*a, **k):
        session = real_new_session(*a, **k)
        created.append(session)
        return session

    monkeypatch.setattr(models, "new_session", _new_session)
    monkeypatch.setattr(routes, "_run_agent_streaming", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(background, "track_background", lambda *a, **k: None)
    monkeypatch.setattr(background, "complete_background", lambda *a, **k: None)

    replies = _post(monkeypatch, "/api/background", {"session_id": OWN, "prompt": "bg task"})

    if swap:
        assert [s.profile for s in created] == [], "a background session was created from the other session"
        assert replies == NOT_FOUND, replies
    else:
        assert [s.profile for s in created] == ["default"], created
        assert replies and replies[0][:2] == ("j", 200), replies
    for s in created:
        models.SESSIONS.pop(s.session_id, None)


@pytest.mark.parametrize("swap", [False, True])
def test_title_regenerate_never_reads_the_other_sessions_transcript(store, monkeypatch, swap):  # noqa: F811 -- pytest fixture parameter
    """Through ``_get_or_materialize_session``. state.db knows the session too, so the boundary
    must also keep the materialize fallback from importing over the file."""
    import api.routes as routes

    _victim(store)
    _own(store)
    if swap:
        _swap_while_open(store)
    before = _files(store)
    fallbacks = _foreign_state_rows(monkeypatch)
    seen = []
    monkeypatch.setattr(
        routes, "generate_session_title_for_session",
        lambda s, **k: seen.append((s.session_id, len(s.messages))) or ("New title", "ok", ""),
    )
    monkeypatch.setattr(routes, "_persist_generated_session_title", lambda s, title, **k: title)

    replies = _post(monkeypatch, "/api/session/title/regenerate", {"session_id": OWN})

    assert fallbacks == []
    if swap:
        assert seen == [], "the other session's transcript went to the title model"
        assert replies == NOT_FOUND, replies
        assert _files(store) == before
    else:
        assert seen == [(OWN, 2)]
        assert replies and replies[0][:2] == ("j", 200), replies


@pytest.mark.parametrize("swap", [False, True])
def test_duplicate_never_copies_the_other_sessions_transcript(store, monkeypatch, swap):  # noqa: F811 -- pytest fixture parameter
    # /api/session/duplicate loads with Session.load directly, not get_session (review r1).
    _victim(store)
    _own(store)
    if swap:
        _swap_while_open(store)
    before = _files(store)

    replies = _post(monkeypatch, "/api/session/duplicate", {"session_id": OWN})

    created = set(_files(store)) - set(before)
    if swap:
        assert replies == NOT_FOUND, replies
        assert created == set()
        _unchanged_victim(store, before)
    else:
        assert replies and replies[0][:2] == ("j", 200), replies
        assert len(created) == 1


@pytest.mark.parametrize("swap", [False, True])
def test_rename_never_materializes_over_the_replaced_file(store, monkeypatch, swap):  # noqa: F811 -- pytest fixture parameter
    _victim(store)
    _own(store)
    if swap:
        _swap_while_open(store)
    before = _files(store)
    fallbacks = _foreign_state_rows(monkeypatch)

    replies = _post(monkeypatch, "/api/session/rename", {"session_id": OWN, "title": "Renamed"})

    assert fallbacks == []
    if swap:
        assert replies == NOT_FOUND, replies
        assert _files(store) == before
        _unchanged_victim(store, before)
    else:
        assert replies and replies[0][:2] == ("j", 200), replies
        assert json.loads((store / f"{OWN}.json").read_text(encoding="utf-8"))["title"] == "Renamed"


@pytest.mark.parametrize("swap", [False, True])
def test_compression_recovery_never_creates_a_session_in_the_other_profile(store, monkeypatch, swap):  # noqa: F811 -- pytest fixture parameter
    import api.routes as routes

    _victim(store)
    _own(store)
    if swap:
        _swap_while_open(store)
    before = set(_files(store))
    monkeypatch.setattr(
        routes, "compression_recovery_payload_for_session",
        lambda s: {"recommended_action": routes.COMPRESSION_RECOVERY_ACTION_START_FOCUSED},
    )

    replies = _post(monkeypatch, "/api/session/compression-recovery/start", {"session_id": OWN})

    new = [json.loads((store / name).read_text(encoding="utf-8")) for name in set(_files(store)) - before]
    if swap:
        assert [(s["profile"], s["parent_session_id"]) for s in new] == [], "a session was created from the other one"
        assert replies == NOT_FOUND, replies
    else:
        assert [(s["profile"], s["parent_session_id"]) for s in new] == [("default", OWN)], new
        assert replies and replies[0][:2] == ("j", 200), replies


@pytest.mark.parametrize("swap", [False, True])
def test_worktree_remove_never_targets_the_other_sessions_worktree(store, tmp_path, monkeypatch, swap):  # noqa: F811 -- pytest fixture parameter
    """A metadata-only load. The session is evicted between the request guard (which read the
    resident owner) and the route's load, so the load reads the replaced file."""
    import api.routes as routes
    import api.worktrees as worktrees
    from api import models

    _victim(store)
    raw = json.loads((store / f"{VICTIM}.json").read_text(encoding="utf-8"))
    raw.update(worktree_path=str(tmp_path / "victim-worktree"), worktree_branch="victim-branch")
    (store / f"{VICTIM}.json").write_text(json.dumps(raw, indent=2), encoding="utf-8")
    _own(store)
    if swap:
        _swap_while_open(store)
    else:
        models.get_session(OWN)
    readonly = routes.get_session_profile_readonly

    def _evicted_after_guard(sid):
        owner = readonly(sid)
        models.SESSIONS.clear()
        return owner

    monkeypatch.setattr(routes, "get_session_profile_readonly", _evicted_after_guard)
    removed = []
    monkeypatch.setattr(
        worktrees, "remove_worktree_for_session",
        lambda s, force=False: removed.append((s.session_id, getattr(s, "worktree_path", None))) or {"ok": True},
    )

    replies = _post(monkeypatch, "/api/session/worktree/remove", {"session_id": OWN})

    if swap:
        assert removed == [], "worktree removal ran on the other session's worktree"
        assert replies == NOT_FOUND, replies
    else:
        assert removed == [(OWN, None)]


@pytest.mark.parametrize("swap", [False, True])
def test_goal_never_runs_under_the_other_session(store, tmp_path, monkeypatch, swap):  # noqa: F811 -- pytest fixture parameter
    """Round 2's ``/api/goal`` case, over ``handle_post``: the goal writer is a recorder."""
    import api.goals as goals
    import api.profiles as profiles

    homes = {p: tmp_path / f"home-{p}" for p in ("default", "work")}
    for home in homes.values():
        home.mkdir()
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda p=None: homes[p or "default"])
    _victim(store)
    _own(store)
    if swap:
        _swap_while_open(store)
    calls = []

    def _goal_writer(session_id, args="", *, stream_running=False, profile_home=None):
        calls.append((session_id, args, profile_home))
        return {"ok": True, "action": "clear", "message": "Goal cleared."}

    monkeypatch.setattr(goals, "goal_command_payload", _goal_writer)

    replies = _post(monkeypatch, "/api/goal", {"session_id": OWN, "args": "clear", "profile": "default"})

    if swap:
        assert calls == [], "the goal control ran under the other session's id and profile"
        assert replies == NOT_FOUND, replies
    else:
        assert calls == [(OWN, "clear", homes["default"])]


@pytest.mark.parametrize("swap", [False, True])
def test_btw_never_seeds_a_side_question_from_the_other_session(store, tmp_path, monkeypatch, swap):  # noqa: F811 -- pytest fixture parameter
    """Round 2's ``/api/btw`` case, over ``handle_post``."""
    import api.background as background
    import api.config as config
    import api.models as models
    import api.routes as routes

    _victim(store, workspace=tmp_path)
    _own(store, tmp_path)
    if swap:
        _swap_while_open(store)
    created, started = [], []
    real_new_session = models.new_session

    def _new_session(*a, **k):
        session = real_new_session(*a, **k)
        created.append(session)
        return session

    monkeypatch.setattr(models, "new_session", _new_session)
    monkeypatch.setattr(routes, "_run_agent_streaming", lambda *a, **k: started.append(a))
    monkeypatch.setattr(background, "track_btw", lambda *a, **k: None)
    streams_before = set(config.STREAMS)
    try:
        replies = _post(monkeypatch, "/api/btw", {"session_id": OWN, "question": "side q"})
    finally:
        for stream_id in set(config.STREAMS) - streams_before:
            for registry in vars(config).values():
                if isinstance(registry, dict):
                    registry.pop(stream_id, None)

    if swap:
        assert [s.profile for s in created] == [], "a side session was built from the other session"
        assert replies == NOT_FOUND, replies
    else:
        assert [[m["content"] for m in s.messages] for s in created] == [["own q", "own a"]]
    for s in created:
        models.SESSIONS.pop(s.session_id, None)


# --- fallbacks that would materialize a session under a refused id ----------------------------


def test_chat_start_never_claims_over_the_replaced_file(store, monkeypatch):  # noqa: F811 -- pytest fixture parameter
    """Guards the boundary's own hazard (master answered 404 here through its marker check):
    chat/start treats ``KeyError`` as "no sidecar" and claims the id from state.db."""
    import api.routes as routes

    _victim(store)
    _own(store)
    _swap_while_open(store)
    before = _files(store)
    fallbacks = _foreign_state_rows(monkeypatch)
    ran = []
    monkeypatch.setattr(routes, "_run_agent_streaming", lambda *a, **k: ran.append(a))

    handler = _Handler()
    routes._handle_chat_start(handler, {"session_id": OWN, "message": "hi", "profile": "default"})

    assert handler.status == 404, bytes(handler.body)
    assert (fallbacks, ran) == ([], [])
    assert _files(store) == before


def test_branch_never_claims_over_the_replaced_file(store, monkeypatch):  # noqa: F811 -- pytest fixture parameter
    import api.routes as routes

    _victim(store)
    _copy(store, partials=False)
    fallbacks = _foreign_state_rows(monkeypatch)
    handler = _Handler()

    assert routes._load_branch_source_or_refuse(handler, COPY) is None
    assert handler.status == 404
    assert fallbacks == []


def test_share_never_synthesizes_over_the_replaced_file(store, monkeypatch):  # noqa: F811 -- pytest fixture parameter
    import api.routes as routes

    _victim(store)
    _copy(store, partials=False)
    fallbacks = _foreign_state_rows(monkeypatch)
    monkeypatch.setattr(
        routes, "_lookup_cli_session_metadata", lambda sid, **_k: {"session_id": sid, "profile": "default"},
    )

    with pytest.raises(KeyError):
        routes._resolve_share_session_pair(COPY, _Handler())
    assert fallbacks == []


def test_archive_never_imports_over_the_replaced_file(store, monkeypatch):  # noqa: F811 -- pytest fixture parameter
    """``/api/session/archive`` is exempt from the request guard and imports a state.db session
    on ``KeyError``."""
    _victim(store)
    _own(store)
    _swap_while_open(store)
    before = _files(store)
    fallbacks = _foreign_state_rows(monkeypatch)

    replies = _post(monkeypatch, "/api/session/archive", {"session_id": OWN, "archived": True})

    assert fallbacks == []
    assert replies == NOT_FOUND, replies
    assert _files(store) == before


# --- view-only guard reads a swapped-in body (Codex review r2) -------------------------------------
# ``_session_is_subagent_view_only`` guards routes that never call get_session themselves. Master read
# the OTHER session's source tag from a swapped file and refused a subagent body. The guard opts in with
# ``allow_foreign_body`` so it still can; without it, delete/handoff-summary mutated this id.


def _swapped(directory, subagent):
    _victim(directory)
    if subagent:
        path = directory / f"{VICTIM}.json"
        raw = json.loads(path.read_text())
        raw["source_tag"] = "subagent"
        path.write_text(json.dumps(raw))
    _own(directory)
    _swap_while_open(directory)


def _own_state_db(tmp_path, monkeypatch, directory, message_count=None):
    import sqlite3

    from api import models, profiles
    import api.routes as routes

    monkeypatch.setattr(routes, "SESSION_DIR", directory)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db")
    db_path = tmp_path / "state.db"
    if message_count:
        from tests.test_5307_subagent_child_transcript import _make_state_db

        _make_state_db(db_path, OWN, source="webui", message_count=message_count)
    else:
        with sqlite3.connect(db_path) as db:
            db.executescript(
                "CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, source TEXT, title TEXT,"
                " message_count INTEGER);"
                "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,"
                " timestamp REAL);"
            )
            db.execute("INSERT INTO sessions VALUES (?,NULL,?,?,1)", (OWN, "webui", "Own"))
            db.execute("INSERT INTO messages VALUES (1,?,?,?,1)", (OWN, "user", "own transcript"))

    def own_rows():
        with sqlite3.connect(db_path) as db:
            return db.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (OWN,)).fetchone()[0]

    return own_rows


@pytest.mark.parametrize("subagent", [True, False], ids=["subagent-body", "ordinary-body"])
def test_view_only_guard_reads_the_swapped_body_as_master_did(store, monkeypatch, subagent):  # noqa: F811
    import api.routes as routes

    _swapped(store, subagent)
    # Subagent body: refused (view-only), as on master. Ordinary body: not view-only, so the route's
    # own get_session answers 404 for the swapped file.
    assert routes._session_is_subagent_view_only(OWN) is subagent


def test_view_only_guard_still_allows_an_ordinary_session(store):  # noqa: F811 -- pytest fixture parameter
    import api.routes as routes

    _own(store)
    assert routes._session_is_subagent_view_only(OWN) is False


def test_delete_never_removes_a_swapped_subagent_file_or_its_transcript(store, tmp_path, monkeypatch):  # noqa: F811
    _swapped(store, subagent=True)
    own_rows = _own_state_db(tmp_path, monkeypatch, store)
    before = _files(store)

    replies = _post(monkeypatch, "/api/session/delete", {"session_id": OWN})

    assert replies and replies[0][1] == 400, replies
    assert _files(store) == before
    assert own_rows() == 1


def test_view_only_guard_leaves_no_foreign_object_in_the_cache(store, monkeypatch):  # noqa: F811
    from api import models
    import api.routes as routes

    _swapped(store, subagent=True)
    routes._session_is_subagent_view_only(OWN)
    assert OWN not in models.SESSIONS, "the guard's opt-in left the foreign object cached under this id"


def test_delete_of_a_swapped_ordinary_file_never_touches_the_other_session(store, tmp_path, monkeypatch):  # noqa: F811
    import sqlite3

    _swapped(store, subagent=False)
    _own_state_db(tmp_path, monkeypatch, store)
    with sqlite3.connect(tmp_path / "state.db") as db:
        db.execute("INSERT INTO sessions VALUES (?,NULL,?,?,1)", (VICTIM, "webui", "Victim"))
        db.execute("INSERT INTO messages VALUES (2,?,?,?,1)", (VICTIM, "user", "victim transcript"))
    before = _files(store)

    replies = _post(monkeypatch, "/api/session/delete", {"session_id": OWN})

    assert replies and replies[0][1] == 200, replies  # not view-only: delete proceeds, as on master
    _unchanged_victim(store, before)
    with sqlite3.connect(tmp_path / "state.db") as db:
        assert db.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (VICTIM,)).fetchone()[0] == 1


def test_handoff_summary_never_writes_into_a_swapped_session(store, tmp_path, monkeypatch):  # noqa: F811
    import api.routes as routes

    _swapped(store, subagent=True)
    own_rows = _own_state_db(tmp_path, monkeypatch, store, message_count=24)

    def no_model():
        raise RuntimeError("isolated test: model runtime unavailable")

    monkeypatch.setattr(routes, "ensure_agent_runtime_current", no_model)
    before = _files(store)

    replies = _post(monkeypatch, "/api/session/handoff-summary", {"session_id": OWN})

    assert replies and replies[0][1] == 400, replies
    assert own_rows() == 24
    assert _files(store) == before
