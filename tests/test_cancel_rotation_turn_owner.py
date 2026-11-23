"""A Stop that lands inside a retry must settle on the turn it started on (#8011).

A credential self-heal retry runs its provider call with the per-session agent
lock released, so a Stop can interleave while the Agent is mid-rotation: the
retry may already carry a continuation id the turn has not published yet (no
session file, no writeback ownership). The cancel path used to resolve its
session from the *live* Agent object after the interrupt, so it bound to that
unpublished id: it registered a brand-new per-session agent lock under it and
published a terminal ``cancel`` for a turn it did not own, while the worker that
does own the writeback settled the same Stop and published a second one.

The ownership rule the tests pin: session cleanup, the terminal frame and the
lock registry all follow the session that currently owns the stream's writeback
(``active_stream_id == stream_id``), never whatever id the Agent carries at the
moment the cancel happens to read it.
"""
import threading
from concurrent.futures import ThreadPoolExecutor

from api import config, models, streaming
from tests.test_steer_worker_boundaries import worker_scene as worker_scene

TERMINAL_EVENTS = ("cancel", "done", "apperror")


def _install_rotation_scene(scene, monkeypatch, *, second_rotation):
    """First attempt rotates to continuation-one and 401s; the retry runs on.

    ``second_rotation`` makes the rebuilt Agent carry ``continuation-two``, the
    id a retry that compressed again would already hold before the worker's
    post-retry ownership migration publishes it.
    """
    monkeypatch.setattr(config, "SESSION_WRITEBACK_OWNERS", {})
    # routes.py claims the writeback for the stream under the chat-start id
    # before the worker starts; the turn's rotation migrates it to the
    # continuation as the first attempt compresses.
    config.register_session_writeback_owner("original", "run")
    entered, release = threading.Event(), threading.Event()
    built = []

    def _get_session(session_id):
        cached = streaming.SESSIONS.get(session_id)
        return cached if cached is not None else models.Session.load(session_id)

    monkeypatch.setattr(streaming, "get_session", _get_session)

    def on_init():
        built.append(scene.agent)

    def on_run():
        if len(built) == 1:
            scene.agent.session_id = "continuation-one"
            scene.result = {"error": "401 unauthorized", "messages": []}
            return
        if second_rotation:
            scene.agent.session_id = "continuation-two"
        entered.set()
        assert release.wait(8), "retry barrier timed out"
        scene.result = {"messages": [
            {"role": "user", "content": "Do the task."},
            {"role": "assistant", "content": "Healed answer."},
        ]}

    scene.on_init, scene.on_run = on_init, on_run
    monkeypatch.setattr(streaming, "_attempt_credential_self_heal", lambda *a, **k: {
        "provider": "openai",
        "api_key": "synthetic-self-heal-fixture",
        "base_url": "http://127.0.0.1:1/v1",
    })
    return entered, release


def test_stop_inside_a_retry_that_rotated_again_settles_one_owned_turn(
    worker_scene, monkeypatch,
):
    scene = worker_scene
    entered, release = _install_rotation_scene(scene, monkeypatch, second_rotation=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(scene.run)
        assert entered.wait(8), "the heal retry never ran"
        stop_future = pool.submit(streaming.cancel_stream, "run")
        try:
            assert stop_future.result(timeout=5) is True
        finally:
            release.set()
        future.result(timeout=15)

    assert "continuation-two" not in config.SESSION_AGENT_LOCKS, (
        "the cancel registered a per-session agent lock for a session id the "
        "turn never owned, which blocks the retry's own ownership migration"
    )
    assert models.Session.load("continuation-two") is None, (
        "the cancel wrote a session file for an unpublished continuation id"
    )

    names = [event for event, _payload in list(scene.events.queue)]
    terminal = [name for name in names if name in TERMINAL_EVENTS]
    assert terminal and set(terminal) == {"cancel"}, (
        f"a cancelled turn must only publish cancel frames, got {terminal}"
    )
    first_terminal = min(i for i, name in enumerate(names) if name in TERMINAL_EVENTS)
    late_handoffs = [
        i for i, name in enumerate(names) if name == "compressed" and i > first_terminal
    ]
    assert not late_handoffs, (
        "the session handoff must precede the terminal frame: a rotate-after-cancel "
        "ordering leaves the client on the id the turn already left "
        f"({names})"
    )

    continuation = models.Session.load("continuation-one")
    assert continuation is not None, "the cancelled turn must land on the turn's session"
    saved = [str(row.get("content") or "") for row in continuation.messages]
    assert saved.count("Do the task.") == 1, "the cancelled turn's user row must exist once"
    assert sum(1 for row in continuation.messages if row.get("_error")) == 1, (
        "the cancelled turn must be marked interrupted exactly once"
    )
    assert "Healed answer." not in saved, "the cancelled turn persisted the retry's answer"
    assert config.SESSION_WRITEBACK_OWNERS == {}, (
        "the turn's teardown must release the writeback claim it held"
    )


def test_stop_on_the_live_turn_still_writes_and_announces_once(worker_scene, monkeypatch):
    """The opposite direction: a legitimate Stop of the live turn still settles."""
    scene = worker_scene
    monkeypatch.setattr(config, "SESSION_WRITEBACK_OWNERS", {})
    config.register_session_writeback_owner("original", "run")
    entered, release = threading.Event(), threading.Event()

    def _get_session(session_id):
        cached = streaming.SESSIONS.get(session_id)
        return cached if cached is not None else models.Session.load(session_id)

    monkeypatch.setattr(streaming, "get_session", _get_session)

    def on_run():
        entered.set()
        assert release.wait(8), "turn barrier timed out"

    scene.on_run = on_run
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(scene.run)
        assert entered.wait(8), "the turn never ran"
        stop_future = pool.submit(streaming.cancel_stream, "run")
        try:
            assert stop_future.result(timeout=5) is True
        finally:
            release.set()
        future.result(timeout=15)

    names = [event for event, _payload in list(scene.events.queue)]
    terminal = [name for name in names if name in TERMINAL_EVENTS]
    assert terminal and set(terminal) == {"cancel"}, (
        f"a live Stop must announce its cancellation, got {terminal}"
    )
    session = models.Session.load("original")
    assert session is not None
    saved = [str(row.get("content") or "") for row in session.messages]
    assert saved.count("Do the task.") == 1, "the stopped turn's user row must exist once"
    assert sum(1 for row in session.messages if row.get("_error")) == 1, (
        "a live Stop must mark the turn interrupted exactly once"
    )
    assert getattr(session, "active_stream_id", None) is None, (
        "a stopped turn must release the stream"
    )
