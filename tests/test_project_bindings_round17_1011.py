"""Round-17 re-gate of the delete write-through (maintainer review 5482234851
@ 2026-10-11T06:53:38Z, ``CHANGES_REQUESTED`` on head ``aeb85130``).

Round 17 — [CORE] ``api/routes.py:815`` / ``:846``: "project delete can
overwrite a newly started stream's provider". ``_persist_cleared_project_ids``
took ``active_ids = _active_stream_ids()`` ONCE, before the loop that waits for
each target's own agent lock. A stream that started on a session while the
delete waited for that lock was therefore invisible to the fresh-sidecar
refresh: the stale snapshot said "not streaming", ``_delete_target_session``
reloaded the OLDER sidecar into the live worker's own object, and the worker's
next checkpoint persisted that older state. The maintainers reproduced it over
real HTTP against the real streaming worker — the resolved provider went from
``openai-codex`` to ``null`` and Chromium's next send targeted the wrong
provider, while master preserved Codex.

The fix, validated against their reproduction: read the stream set INSIDE the
per-session locked block, immediately before ``_delete_target_session``, and use
that fresh snapshot for both streaming checks. Skip the refresh and the
persistence when the read fails (fail closed: the caller's index pass and the
worker's own next save still carry the unlink).

The behavioural tests drive the real function over real ``Session`` objects on a
temporary session store and observe LOCK COVERAGE directly (the fake stream-set
reader answers from a flag the lock wrapper flips), so they fail on the pre-fix
tree for the shipped reason — not merely on a source shape.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Whitespace-tolerant guards: the 6775 P2 lesson is that a bare substring test
# stays green for a reformatting of the very code it is meant to pin.
_STREAM_SNAPSHOT = re.compile(r"active_ids\s*=\s*_active_stream_ids\(\s*\)")
_ACQUIRE = re.compile(r"lock\s*\.\s*acquire\s*\(")
_RESOLVER_CALL = re.compile(r"cached\s*=\s*_delete_target_session\([^)]*\)")
_FRESH_SNAPSHOT_CHECK = re.compile(r"in\s+active_ids")


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _function_code(name: str) -> str:
    """The CODE of one module-level ``api/routes.py`` function, docstring out.

    The docstring is excluded on purpose: it is prose ABOUT the invariant and
    must not be able to satisfy (or trip) an order guard on its own.
    """
    source = _read("api/routes.py")
    lines = source.splitlines()
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                start = node.body[1].lineno
            else:
                start = first.lineno
            return "\n".join(lines[start - 1:node.end_lineno])
    raise AssertionError(f"no module-level function {name} in api/routes.py")


def _function_source(name: str) -> str:
    """The raw text of one function, docstring included (shape guards)."""
    source = _read("api/routes.py")
    start = source.index(f"def {name}(")
    end = source.index("\n\ndef ", start + 1)
    return source[start:end]


@pytest.fixture
def _isolated_store(tmp_path, monkeypatch):
    """A private session store plus the module names the code paths read."""
    import api.config as config
    import api.models as models
    import api.routes as routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file, raising=False)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    config.SESSIONS.clear()
    try:
        yield session_dir, index_file, routes, models, config
    finally:
        config.SESSIONS.clear()


def _messages(n: int, tag: str = "m"):
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append({"role": role, "content": f"{tag}-{i}"})
    return out


def _sidecar(session_dir: Path, sid: str) -> dict:
    return json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))


def _delete_unlink(routes, pid, sid):
    """The delete's real two-step flow: clear in cache, then write through."""
    cleared_ids: list = []
    routes._clear_cached_sessions_for_project(pid, cleared_ids=cleared_ids)
    return routes._persist_cleared_project_ids(pid, cleared_ids)


@pytest.fixture
def _lock_observed(monkeypatch):
    """Wrap the real per-session lock so a test can see whether it is held.

    Returns a dict with ``held`` (bool, flipped by the wrapper) so a fake stream
    set can answer exactly as the live one would: "this session is streaming"
    only once the delete owns the session's lock — i.e. a stream that started
    while the delete was waiting for it.
    """
    import api.routes as routes

    state = {"held": False}
    real_factory = routes._get_session_agent_lock

    class _ObservedLock:
        def __init__(self, inner):
            self._inner = inner

        def acquire(self, *args, **kwargs):
            ok = self._inner.acquire(*args, **kwargs)
            if ok:
                state["held"] = True
            return ok

        def release(self):
            state["held"] = False
            self._inner.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *exc):
            self.release()
            return False

    monkeypatch.setattr(
        routes, "_get_session_agent_lock", lambda sid: _ObservedLock(real_factory(sid))
    )
    return state


# ---------------------------------------------------------------------------
# [CORE] the stream snapshot must be taken under the session's own lock
# ---------------------------------------------------------------------------


def test_delete_write_through_leaves_a_stream_that_started_while_it_waited(
    _isolated_store, _lock_observed, monkeypatch
):
    """[CORE] A stream visible only after the lock is won must not be refreshed.

    The maintainer reproduction, in unit form: the delete's pre-lock snapshot
    says "nothing is streaming", the stream starts while the delete waits for
    the session's lock, and the fresh-sidecar resolution then reloads the OLDER
    sidecar into the live worker's object — the next checkpoint persists that
    older state (their provider went ``openai-codex`` -> ``null``; here the live
    object's newer composer draft is what gets overwritten).
    """
    session_dir, _index, routes, models, config = _isolated_store
    pid = "proj_round17_race"
    sid = "sess-round17-race"
    stream = "stream-17-race"

    # Disk: the checkpoint written BEFORE the stream resolved anything.
    disk = models.Session(
        session_id=sid,
        workspace="/ws/round17",
        messages=_messages(2, "disk"),
        project_id=pid,
    )
    disk.composer_draft = {"text": "old draft"}
    disk.save()
    assert _sidecar(session_dir, sid)["project_id"] == pid

    # The live worker's object: same id, same counts, resolved AFTER that
    # checkpoint, and streaming — the one state a delete must never reload.
    live = models.Session(
        session_id=sid,
        workspace="/ws/round17",
        messages=_messages(2, "disk"),
        project_id=pid,
        active_stream_id=stream,
    )
    live.composer_draft = {"text": "live draft"}
    config.SESSIONS[sid] = live

    state = _lock_observed
    monkeypatch.setattr(
        routes,
        "_active_stream_ids",
        lambda: {stream} if state["held"] else set(),
    )

    assert _delete_unlink(routes, pid, sid) == 0, (
        "the delete wrote the unlink through to a session that was streaming: "
        "the stream set was read before the session lock, so the stream that "
        "started while the delete waited was invisible"
    )

    assert live.composer_draft == {"text": "live draft"}, (
        "the older sidecar was reloaded into the live worker's object, whose "
        "next save persists that older state"
    )
    assert _sidecar(session_dir, sid)["project_id"] == pid, (
        "the unlink landed on a chat whose worker owns the session"
    )
    assert _sidecar(session_dir, sid)["composer_draft"] == {"text": "old draft"}


def test_delete_write_through_fails_closed_when_the_stream_set_is_unreadable(
    _isolated_store, monkeypatch
):
    """A stream set that cannot be read must skip, not read as "empty".

    The pre-fix capture swallowed the error and substituted ``set()``, i.e. "no
    session is streaming": every target was then refreshed and persisted on a
    guess. Nothing here may touch the sidecar.
    """
    session_dir, _index, routes, models, config = _isolated_store
    pid = "proj_round17_unreadable"
    sid = "sess-round17-unreadable"

    disk = models.Session(
        session_id=sid,
        workspace="/ws/round17",
        messages=_messages(2, "disk"),
        project_id=pid,
    )
    disk.save()
    config.SESSIONS[sid] = disk

    def _boom():
        raise RuntimeError("stream registry unavailable")

    monkeypatch.setattr(routes, "_active_stream_ids", _boom)

    assert _delete_unlink(routes, pid, sid) == 0, (
        "an unreadable stream set was treated as 'nothing is streaming', so the "
        "unlink was written through on a guess"
    )
    assert _sidecar(session_dir, sid)["project_id"] == pid, (
        "the sidecar was rewritten even though the streaming state was unknown"
    )
    assert _sidecar(session_dir, sid)["message_count"] == 2


def test_delete_write_through_still_lands_for_an_inactive_session(
    _isolated_store, _lock_observed, monkeypatch
):
    """Control: the fresh snapshot must not block the ordinary delete.

    An unrelated stream being live (read under the lock, as shipped) leaves this
    session's unlink exactly as it was.
    """
    session_dir, _index, routes, models, config = _isolated_store
    pid = "proj_round17_control"
    sid = "sess-round17-control"

    disk = models.Session(
        session_id=sid,
        workspace="/ws/round17",
        messages=_messages(3, "disk"),
        project_id=pid,
    )
    disk.save()
    config.SESSIONS[sid] = disk

    state = _lock_observed
    monkeypatch.setattr(
        routes,
        "_active_stream_ids",
        lambda: {"stream-17-elsewhere"} if state["held"] else set(),
    )

    assert _delete_unlink(routes, pid, sid) == 1
    assert _sidecar(session_dir, sid)["project_id"] is None
    assert _sidecar(session_dir, sid)["message_count"] == 3


# ---------------------------------------------------------------------------
# Source guards — the shipped shape, not a copy of it
# ---------------------------------------------------------------------------


def test_stream_snapshot_is_read_inside_the_session_lock():
    """The snapshot must sit AFTER the bounded acquire and BEFORE the resolver."""
    body = _function_code("_persist_cleared_project_ids")

    snapshot = _STREAM_SNAPSHOT.search(body)
    assert snapshot, (
        "the write-through must read the live stream set itself "
        "(_active_stream_ids) instead of trusting a caller-supplied one"
    )
    acquire = _ACQUIRE.search(body)
    assert acquire, "the bounded per-session acquire disappeared"
    assert acquire.start() < snapshot.start(), (
        "the stream snapshot is taken BEFORE the session lock: a stream that "
        "started while the delete waited for the lock reads as 'not streaming' "
        "and its live object gets reloaded from the older sidecar"
    )

    resolver = _RESOLVER_CALL.search(body)
    assert resolver, "the write-through must resolve through _delete_target_session"
    assert snapshot.start() < resolver.start(), (
        "the snapshot must be the one the resolver (and the re-check) use"
    )

    # Between the snapshot and the resolver, a failure must SKIP the session
    # rather than fall through with an empty set (fail closed).
    between = body[snapshot.end():resolver.start()]
    assert re.search(r"\bcontinue\b", between), (
        "an unreadable stream set must skip the refresh AND the persistence"
    )
    assert "active_ids = set()" not in body, (
        "an unreadable stream set must not be substituted with an empty one"
    )
    # ...and the re-check still reads the FRESH variable.
    assert _FRESH_SNAPSHOT_CHECK.search(body[resolver.end():]), (
        "the streaming re-check must use the same fresh snapshot"
    )


def test_no_stream_snapshot_is_left_before_the_target_loop():
    """No pre-loop capture may come back: it is stale for every later target."""
    body = _function_code("_persist_cleared_project_ids")
    loop = body.index("for sid in sids:")
    assert body.index("_active_stream_ids") > loop, (
        "the stream set is read before the per-target loop again; every session "
        "after the first is judged against a snapshot older than its own lock"
    )


def test_the_guard_reads_real_code_not_the_docstring():
    """Guard-the-guard: the slice is the function's code, not its prose."""
    code = _function_code("_persist_cleared_project_ids")
    assert "_active_stream_ids" in code
    assert '"""' not in code, "the docstring leaked into the guarded slice"
    assert "maintainer re-gate 2026-10-11T06:53:38Z" in _function_source(
        "_persist_cleared_project_ids"
    ), "the rationale must stay recorded next to the code"
