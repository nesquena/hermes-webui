"""Regression coverage for issue #7738 — stale evicted Session object can
overwrite a newer save in /api/session/rename (and the same-shape handlers
/api/session/move and /api/session/archive).

Root cause: the handlers resolved the canonical Session object OUTSIDE the
per-session lock. Between the resolve and the lock acquire, count-cap eviction
(``_evict_sessions_over_cap``) could drop that clean, persisted object from
``SESSIONS``. A second writer (e.g. ``/api/session/draft``) would then call
``get_session()``, get a *fresh* object from disk, mutate it and save it.
When the first handler finally took the lock, it saved its detached, stale
object, and every field the second writer changed silently reverted on disk.

The fix mirrors the existing pattern in
``_persist_generated_session_title`` (api/routes.py): re-resolve the canonical
session INSIDE the lock (``SESSIONS.get(sid)`` → ``Session.load(sid)``
fallback → ``_ensure_full_session_before_mutation``), then apply the
mutation and save the FRESH object. The 404 / 403 contracts are still
served from the outside-resolve path so the responses are unchanged.

These tests simulate the race deterministically by monkeypatching
``_get_or_materialize_session`` to return a STALE object and, when the lock
acquire fires, replacing the SESSIONS entry with a NEWER object (mirroring
the eviction + reload that the second writer would do on master). The
postcondition the fix guarantees: the FRESH (newer) object's other-field
mutations survive the rename's save().
"""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import api.models as models
import api.routes as routes
from api.models import SESSIONS, Session


# ---------------------------------------------------------------------------
# Test helpers (mirrors tests/test_issue2057_worktree_lifecycle.py)
# ---------------------------------------------------------------------------


class _FakeHandler:
    """Minimal BaseHTTPRequestHandler stand-in for handle_post() direct calls.

    handle_post() / bad() / j() only need handler.send_response, send_header,
    end_headers. CSRF + read_body + j are monkeypatched in _capture_post.
    """

    def __init__(self, body: bytes = b"{}"):
        self.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
        }
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):  # noqa: D401 — protocol stub
        self.status = status

    def send_header(self, key, value):  # noqa: D401 — protocol stub
        pass

    def end_headers(self):  # noqa: D401 — protocol stub
        pass


def _capture_post(monkeypatch, body):
    """Wire up the standard ``handle_post`` bypass shims and return a recorder.

    Mirrors ``tests/test_issue2057_worktree_lifecycle.py::_capture_post``.
    """
    captured: dict = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: body)

    def _j(handler, payload, status=200, extra_headers=None, **_kw):
        captured["payload"] = payload
        captured["status"] = status
        return True

    def _bad(handler, msg, status=400, **_kw):
        captured["payload"] = {"error": msg}
        captured["status"] = status
        return True

    monkeypatch.setattr(routes, "j", _j)
    monkeypatch.setattr(routes, "bad", _bad)
    return captured


def _isolate_session_store(tmp_path, monkeypatch):
    """Point api.models and api.routes at an isolated session dir for the test.

    Mirrors ``tests/test_issue2057_worktree_lifecycle.py::_isolate_session_store``.
    """
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file)
    SESSIONS.clear()
    return session_dir


def _load_disk_session(session_dir, sid):
    """Read the on-disk sidecar for ``sid`` and return the parsed JSON dict."""
    path = session_dir / f"{sid}.json"
    return json.loads(path.read_text(encoding="utf-8"))


class _SwapLock:
    """Proxy around a real ``threading.Lock`` whose ``acquire`` swaps the
    SESSIONS entry for ``swap_with`` just before delegating.

    Mirrors the threading.Lock interface used by the handlers
    (``acquire`` / ``release`` / ``__enter__`` / ``__exit__`` /
    ``locked``) so the ``with _swap_lock:`` form continues to work.

    Why not monkeypatch ``lock.acquire``? ``_thread.lock``'s ``acquire`` is
    a C-implemented read-only slot on Python <3.13. Wrapping the lock
    object is the only way to inject the swap deterministically.
    """

    def __init__(self, inner, sid, swap_with):
        self._inner = inner
        self._sid = sid
        self._swap_with = swap_with

    def acquire(self, *args, **kwargs):
        # Replace the SESSIONS entry just before the lock is taken. This
        # stands in for a concurrent writer having evicted+reloaded+mutated
        # the session in the window between the outside resolve and our
        # lock acquire (the exact race #7738 names).
        SESSIONS[self._sid] = self._swap_with
        return self._inner.acquire(*args, **kwargs)

    def release(self):
        self._inner.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()

    @property
    def locked(self):
        return self._inner.locked


class _DropLock:
    """Proxy whose ``acquire`` evicts the SESSIONS entry entirely before
    delegating, exercising the Session.load(sid) fallback branch in the
    fix. Used for the "fully evicted, no resident copy" regression test.
    """

    def __init__(self, inner, sid):
        self._inner = inner
        self._sid = sid

    def acquire(self, *args, **kwargs):
        SESSIONS.pop(self._sid, None)
        return self._inner.acquire(*args, **kwargs)

    def release(self):
        self._inner.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()

    @property
    def locked(self):
        return self._inner.locked


def _install_swap_lock(monkeypatch, sid, swap_with):
    """Replace ``routes._get_session_agent_lock`` with a factory that
    returns a _SwapLock (or _DropLock if ``swap_with is None``) proxy.
    """
    real_lock_factory = routes._get_session_agent_lock

    def _proxy_factory(value):
        lock = real_lock_factory(value)
        if swap_with is None:
            return _DropLock(lock, value)
        return _SwapLock(lock, value, swap_with)

    monkeypatch.setattr(routes, "_get_session_agent_lock", _proxy_factory)


def _stub_post_lock_side_effects(monkeypatch):
    """Stub the rename's post-lock side effects so the test stays focused
    on the stale-overwrite race itself.
    """
    monkeypatch.setattr(routes, "_sync_session_title_to_insights", lambda s: None)
    monkeypatch.setattr(
        routes, "publish_session_list_changed", lambda *a, **kw: None,
    )


# ---------------------------------------------------------------------------
# /api/session/rename — the primary #7738 hot path
# ---------------------------------------------------------------------------


def _seed_stale_session(session_dir, sid, *, title, draft):
    """Persist a baseline on disk matching the stale object so
    Session.load(sid) finds the session, and return a STALE Session object
    the buggy code would resolve. ``stale.save()`` is what writes the
    on-disk baseline.
    """
    stale = Session(
        session_id=sid,
        title=title,
        workspace=str(session_dir.parent),
        messages=[{"role": "user", "content": "hi"}],
        composer_draft=draft,
    )
    stale.save()
    SESSIONS[sid] = stale
    return stale


def _make_newer_session(session_dir, sid, *, title, draft):
    """A fresh object that reflects what a concurrent writer would have
    produced after eviction+reload. Only the fields a different writer
    would mutate differ from the stale object — the rename target's own
    fields are the same.
    """
    return Session(
        session_id=sid,
        title=title,
        workspace=str(session_dir.parent),
        messages=[{"role": "user", "content": "hi"}],
        composer_draft=draft,
    )


def test_rename_handler_re_resolves_session_under_lock_to_avoid_stale_overwrite(
    tmp_path, monkeypatch,
):
    """#7738 primary regression: /api/session/rename must save the resident
    (newer) object, not the stale, evicted one.

    Pre-fix: the outside-resolved stale object was saved; the newer
    composer_draft value was silently clobbered back to the stale value.
    Post-fix: the inside-lock re-resolve picks up the newer object, the
    rename is applied to IT, and its (newer) composer_draft survives.
    """
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "stale-rename-1"
    body = {"session_id": sid, "title": "new_title"}
    captured = _capture_post(monkeypatch, body)

    stale = _seed_stale_session(
        session_dir, sid,
        title="old_title",
        draft={"text": "old draft", "files": ["old.txt"]},
    )
    newer = _make_newer_session(
        session_dir, sid,
        title="old_title",
        draft={"text": "new draft", "files": ["new.txt"]},
    )
    monkeypatch.setattr(
        routes, "_get_or_materialize_session", lambda value: stale,
    )
    _install_swap_lock(monkeypatch, sid, swap_with=newer)
    _stub_post_lock_side_effects(monkeypatch)

    result = routes.handle_post(
        _FakeHandler(b'{"session_id": "' + sid.encode() + b'", "title": "new_title"}'),
        SimpleNamespace(path="/api/session/rename"),
    )

    assert result is True, "rename handler must claim the request"
    assert captured["status"] == 200, f"expected 200, got {captured}"
    on_disk = _load_disk_session(session_dir, sid)
    assert on_disk["title"] == "new_title", (
        f"rename must persist the new title; on-disk: {on_disk['title']!r}"
    )
    assert on_disk["composer_draft"] == {"text": "new draft", "files": ["new.txt"]}, (
        "rename save() must not clobber a newer field write by a concurrent "
        "writer — the resident (newer) object's composer_draft must survive "
        "on disk (issue #7738). Got stale-clobbered: "
        f"{on_disk['composer_draft']!r}"
    )


def test_rename_handler_falls_back_to_session_load_when_sessions_cache_empty(
    tmp_path, monkeypatch,
):
    """#7738: when the SESSIONS cache no longer holds the session (fully
    evicted, no in-memory copy), the inside-lock re-resolve must fall back
    to Session.load(sid) and still apply the rename to the freshly-loaded
    object.
    """
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "stale-rename-2"
    body = {"session_id": sid, "title": "new_title"}
    captured = _capture_post(monkeypatch, body)

    # Persist baseline so Session.load(sid) has something to find.
    baseline = Session(
        session_id=sid,
        title="baseline_title",
        workspace=str(session_dir.parent),
        messages=[{"role": "user", "content": "hi"}],
        composer_draft={"text": "baseline draft"},
    )
    baseline.save()
    SESSIONS.pop(sid, None)

    # Outside-resolve returns a "ghost" object whose state is intentionally
    # wrong (it must NOT be the source of truth for the save). The
    # fix's inside-lock re-resolve must discard it.
    ghost = Session(
        session_id=sid,
        title="ghost_title",
        workspace=str(session_dir.parent),
        messages=[{"role": "user", "content": "hi"}],
        composer_draft={"text": "ghost draft"},
    )
    monkeypatch.setattr(
        routes, "_get_or_materialize_session", lambda value: ghost,
    )
    # Drop SESSIONS[sid] entirely on lock acquire (swap_with=None is the
    # _DropLock sentinel). The fix must fall back to Session.load(sid).
    _install_swap_lock(monkeypatch, sid, swap_with=None)
    _stub_post_lock_side_effects(monkeypatch)

    result = routes.handle_post(
        _FakeHandler(b'{"session_id": "' + sid.encode() + b'", "title": "new_title"}'),
        SimpleNamespace(path="/api/session/rename"),
    )

    assert result is True
    assert captured["status"] == 200, f"expected 200, got {captured}"
    on_disk = _load_disk_session(session_dir, sid)
    assert on_disk["title"] == "new_title"
    # The Session.load(sid) path must have picked up the baseline draft —
    # the ghost outside-resolved object must NOT have leaked onto disk.
    assert on_disk["composer_draft"] == {"text": "baseline draft"}, (
        "Session.load() fallback must read the real on-disk state, not the "
        "ghost outside-resolved object (issue #7738). Got: "
        f"{on_disk['composer_draft']!r}"
    )


def test_rename_handler_preserves_404_for_unknown_session(tmp_path, monkeypatch):
    """#7738: the 404 contract for an unknown sid must be preserved by the
    outside-resolve pre-validation, even though the lock-held re-resolve
    is now the source of truth for the mutation.
    """
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "stale-rename-missing"
    captured = _capture_post(
        monkeypatch, {"session_id": sid, "title": "new_title"},
    )
    monkeypatch.setattr(
        routes, "_get_or_materialize_session",
        lambda value: (_ for _ in ()).throw(KeyError(sid)),
    )

    result = routes.handle_post(
        _FakeHandler(b'{"session_id": "' + sid.encode() + b'", "title": "new_title"}'),
        SimpleNamespace(path="/api/session/rename"),
    )

    assert result is True
    assert captured["status"] == 404, f"expected 404, got {captured}"
    # No session file should have been written.
    assert not (session_dir / f"{sid}.json").exists(), (
        "a 404 must not create a session file on disk"
    )


def test_rename_handler_preserves_403_for_read_only_session(tmp_path, monkeypatch):
    """#7738: the 403 contract for a read-only imported session must be
    preserved. The outside-resolve pre-validation raises PermissionError,
    which the rename handler maps to a 403.
    """
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "stale-rename-readonly"
    captured = _capture_post(
        monkeypatch, {"session_id": sid, "title": "new_title"},
    )
    monkeypatch.setattr(
        routes, "_get_or_materialize_session",
        lambda value: (_ for _ in ()).throw(PermissionError("read-only imported session")),
    )

    result = routes.handle_post(
        _FakeHandler(b'{"session_id": "' + sid.encode() + b'", "title": "new_title"}'),
        SimpleNamespace(path="/api/session/rename"),
    )

    assert result is True
    assert captured["status"] == 403, f"expected 403, got {captured}"
    # No session file should have been written (the 403 path returns
    # before the lock; verify the isolated session dir is still empty).
    assert session_dir.exists() and not any(session_dir.iterdir()), (
        "a 403 must not create a session file on disk"
    )


# ---------------------------------------------------------------------------
# /api/session/move — same-shape fix
# ---------------------------------------------------------------------------


def test_move_handler_re_resolves_session_under_lock_to_avoid_stale_overwrite(
    tmp_path, monkeypatch,
):
    """#7738 same-shape regression: /api/session/move must also re-resolve
    under the lock so a stale, evicted object does not clobber a newer save.
    """
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "stale-move-1"
    captured = _capture_post(
        monkeypatch, {"session_id": sid, "project_id": None},
    )

    # No projects involved: stub load_projects (and the profile match
    # check) so we don't have to seed a project.
    monkeypatch.setattr(routes, "load_projects", lambda: [])

    stale = _seed_stale_session(
        session_dir, sid,
        title="move title",
        draft={"text": "stale draft (clobbered pre-fix)"},
    )
    newer = _make_newer_session(
        session_dir, sid,
        title="move title",
        draft={"text": "newer draft (must survive)"},
    )
    monkeypatch.setattr(
        routes, "_get_or_materialize_session", lambda value: stale,
    )
    _install_swap_lock(monkeypatch, sid, swap_with=newer)
    monkeypatch.setattr(
        routes, "publish_session_list_changed", lambda *a, **kw: None,
    )

    result = routes.handle_post(
        _FakeHandler(b'{"session_id": "' + sid.encode() + b'"}'),
        SimpleNamespace(path="/api/session/move"),
    )

    assert result is True, "move handler must claim the request"
    assert captured["status"] == 200, f"expected 200, got {captured}"
    on_disk = _load_disk_session(session_dir, sid)
    # The newer's composer_draft must have survived the move's save().
    assert on_disk["composer_draft"] == {"text": "newer draft (must survive)"}, (
        "move save() must not clobber a newer field write — the resident "
        "(newer) object's composer_draft must survive on disk (issue #7738). "
        f"Got stale-clobbered: {on_disk['composer_draft']!r}"
    )


# ---------------------------------------------------------------------------
# /api/session/archive — same-shape fix
# ---------------------------------------------------------------------------


def test_archive_handler_re_resolves_session_under_lock_to_avoid_stale_overwrite(
    tmp_path, monkeypatch,
):
    """#7738 same-shape regression: /api/session/archive must also re-resolve
    under the lock so the archived state goes onto the resident (newer)
    object, not the stale one. The materialize-fallback path is not
    exercised here — this is the common in-cache path.
    """
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "stale-archive-1"
    captured = _capture_post(
        monkeypatch, {"session_id": sid, "archived": True},
    )
    # Stub the subagent-view-only gate so we don't have to wire state.db.
    monkeypatch.setattr(
        routes, "_session_is_subagent_view_only", lambda value: False,
    )

    stale = _seed_stale_session(
        session_dir, sid,
        title="archive title",
        draft={"text": "stale draft (clobbered pre-fix)"},
    )
    newer = _make_newer_session(
        session_dir, sid,
        title="archive title",
        draft={"text": "newer draft (must survive)"},
    )

    # The archive handler does NOT call _get_or_materialize_session; it
    # uses get_session directly. We hook _get_session_agent_lock instead
    # so the swap fires when the lock is taken.
    def _get_session(sid, **_kwargs):
        return stale

    monkeypatch.setattr(models, "get_session", _get_session)
    monkeypatch.setattr(routes, "get_session", _get_session)
    _install_swap_lock(monkeypatch, sid, swap_with=newer)
    monkeypatch.setattr(
        routes, "publish_session_list_changed", lambda *a, **kw: None,
    )
    # _worktree_retained_payload reads from the session; not relevant for
    # this test. Stub it to {}.
    monkeypatch.setattr(routes, "_worktree_retained_payload", lambda s: {})

    result = routes.handle_post(
        _FakeHandler(b'{"session_id": "' + sid.encode() + b'", "archived": true}'),
        SimpleNamespace(path="/api/session/archive"),
    )

    assert result is True, "archive handler must claim the request"
    assert captured["status"] == 200, f"expected 200, got {captured}"
    on_disk = _load_disk_session(session_dir, sid)
    assert on_disk["archived"] is True, (
        f"archive must persist the new archived=True state; on-disk: {on_disk.get('archived')!r}"
    )
    # The newer's composer_draft must have survived the archive's save().
    assert on_disk["composer_draft"] == {"text": "newer draft (must survive)"}, (
        "archive save() must not clobber a newer field write — the resident "
        "(newer) object's composer_draft must survive on disk (issue #7738). "
        f"Got stale-clobbered: {on_disk['composer_draft']!r}"
    )


# ---------------------------------------------------------------------------
# PR #7776 Finding 1: /api/session/move must re-run _profiles_match inside
# the lock with the canonical session's profile, not the stale pre-lock one.
# ---------------------------------------------------------------------------


def test_move_handler_reruns_profile_match_under_lock_for_canonical_session(
    tmp_path, monkeypatch,
):
    """#7776 Finding 1 regression: /api/session/move's pre-lock profile
    check used the stale, evicted session (issue #7738's exact race), so
    a profile-beta session resolved pre-lock could be assigned to a
    profile-alpha project — origin/master would reject this but the
    stale-PR head returned 200 and persisted the cross-profile move.

    The fix re-runs ``_profiles_match`` inside the lock with the canonical
    session's profile. This test stages the race:
      * pre-lock session (returned by ``_get_or_materialize_session``)
        has profile=alpha (matches the project — pre-lock check passes)
      * lock acquire swaps the SESSIONS entry to the canonical session
        with profile=beta (does NOT match the project)
      * the lock-held re-run must reject with 404
    """
    from api import profiles as _profiles_mod

    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "stale-move-profile-1"
    target_pid = "project-alpha-1"

    # Active profile is "alpha" so the request-level
    # _session_id_visible_to_request_profile guard (api/routes.py:~596)
    # passes for the alpha-owned session. The fix's #1614 profile-match
    # gate is the only thing left that can reject the cross-profile move.
    # routes.py imports get_active_profile_name as _get_active_profile_name
    # at module load (routes.py:~479), so we must patch the alias to
    # actually flip the guard's view of the active profile.
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "alpha")
    monkeypatch.setattr(
        _profiles_mod, "get_active_profile_name", lambda: "alpha",
    )
    # Project belongs to profile=alpha. The pre-lock session will also be
    # profile=alpha, so the pre-lock check passes; the canonical session
    # resolved inside the lock is profile=beta and must fail.
    monkeypatch.setattr(
        routes, "load_projects",
        lambda: [
            {
                "project_id": target_pid,
                "name": "Alpha project",
                "profile": "alpha",
            },
        ],
    )

    # Pre-lock session: profile=alpha (stale, passes pre-lock check).
    stale = _seed_stale_session(
        session_dir, sid,
        title="move title",
        draft={"text": "pre-lock draft"},
    )
    stale.profile = "alpha"

    # Canonical (lock-held) session: profile=beta (should be rejected).
    newer = _make_newer_session(
        session_dir, sid,
        title="move title",
        draft={"text": "canonical draft"},
    )
    newer.profile = "beta"

    monkeypatch.setattr(
        routes, "_get_or_materialize_session", lambda value: stale,
    )
    _install_swap_lock(monkeypatch, sid, swap_with=newer)
    monkeypatch.setattr(
        routes, "publish_session_list_changed", lambda *a, **kw: None,
    )
    captured = _capture_post(
        monkeypatch, {"session_id": sid, "project_id": target_pid},
    )

    result = routes.handle_post(
        _FakeHandler(
            b'{"session_id": "' + sid.encode() + b'", "project_id": "' +
            target_pid.encode() + b'"}',
        ),
        SimpleNamespace(path="/api/session/move"),
    )

    assert result is True, "move handler must claim the request"
    # The lock-held re-run must reject the cross-profile move with 404.
    assert captured["status"] == 404, (
        f"move must reject cross-profile assignment against the canonical "
        f"session's profile (beta != project alpha); got {captured}"
    )
    on_disk = _load_disk_session(session_dir, sid)
    # The cross-profile project_id must NOT have been written to disk.
    assert on_disk.get("project_id") in (None, ""), (
        "a 404 must not persist the cross-profile project_id onto disk; "
        f"got project_id={on_disk.get('project_id')!r}"
    )


# ---------------------------------------------------------------------------
# PR #7776 Finding 2: rename/move/archive must preserve CLI source identity
# on the on-disk sidecar after the lock-held reload.
# ---------------------------------------------------------------------------


def _seed_sidecar_without_source_meta(
    session_dir, sid, *, title="cli title",
    profile="alpha",
):
    """Write a sidecar that looks like the bug-state — a WebUI session
    object that was materialized from CLI/agent metadata but whose
    ``import_cli_session`` save() ran BEFORE the source-meta assignment,
    so the on-disk file has the default source identity (is_cli_session
    False, null source_tag/raw_source/session_source/source_label).
    """
    baseline = Session(
        session_id=sid,
        title=title,
        workspace=str(session_dir.parent),
        messages=[{"role": "user", "content": "hi"}],
        composer_draft={"text": "baseline draft"},
        profile=profile,
    )
    # Default is_cli_session/source_* — mirrors the import_cli_session
    # save() output that the materialize path produces.
    baseline.save()
    return baseline


def test_rename_handler_preserves_cli_source_identity_across_lock_reload(
    tmp_path, monkeypatch,
):
    """#7776 Finding 2: /api/session/rename's lock-held Session.load(sid)
    must not drop the CLI source identity that the pre-lock
    ``_get_or_materialize_session`` materialize path established in
    state.db. The on-disk sidecar after rename must keep
    ``is_cli_session=True`` + non-null source identity.
    """
    from api import profiles as _profiles_mod

    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "cli-source-rename-1"

    # Active profile is "alpha" so the request-level
    # _session_id_visible_to_request_profile guard passes for the
    # alpha-owned sidecar the test seeds.
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "alpha")
    monkeypatch.setattr(
        _profiles_mod, "get_active_profile_name", lambda: "alpha",
    )

    # Simulate the bug-state sidecar: materialized by import_cli_session
    # (which saves with default source identity) but NOT yet re-stamped
    # with the CLI source meta.
    _seed_sidecar_without_source_meta(session_dir, sid, title="cli title")

    # State.db says this session is a real CLI session. The handler must
    # re-apply this identity after the lock-held reload.
    cli_meta = {
        "session_id": sid,
        "title": "cli title",
        "is_cli_session": True,
        "source_tag": "hermes-cli",
        "raw_source": "hermes-cli",
        "session_source": "cli",
        "source_label": "Hermes CLI",
    }
    monkeypatch.setattr(
        routes, "_lookup_cli_session_metadata",
        lambda value, *, all_profiles=False: dict(cli_meta),
    )
    # Pre-validation must pass: the sidecar exists.
    captured = _capture_post(
        monkeypatch, {"session_id": sid, "title": "new title"},
    )
    _stub_post_lock_side_effects(monkeypatch)

    result = routes.handle_post(
        _FakeHandler(
            b'{"session_id": "' + sid.encode() + b'", "title": "new title"}',
        ),
        SimpleNamespace(path="/api/session/rename"),
    )

    assert result is True, "rename handler must claim the request"
    assert captured["status"] == 200, f"expected 200, got {captured}"
    on_disk = _load_disk_session(session_dir, sid)
    # The CLI source identity must have been re-stamped onto the on-disk
    # sidecar; otherwise rename silently converted a CLI session into a
    # WebUI-native one (Finding 2).
    assert on_disk.get("is_cli_session") is True, (
        "rename must preserve is_cli_session=True; on-disk "
        f"is_cli_session={on_disk.get('is_cli_session')!r}"
    )
    assert on_disk.get("source_tag") == "hermes-cli", (
        "rename must preserve source_tag; on-disk "
        f"source_tag={on_disk.get('source_tag')!r}"
    )
    assert on_disk.get("raw_source") == "hermes-cli", (
        "rename must preserve raw_source; on-disk "
        f"raw_source={on_disk.get('raw_source')!r}"
    )
    assert on_disk.get("source_label") == "Hermes CLI", (
        "rename must preserve source_label; on-disk "
        f"source_label={on_disk.get('source_label')!r}"
    )


def test_move_handler_preserves_cli_source_identity_across_lock_reload(
    tmp_path, monkeypatch,
):
    """#7776 Finding 2: /api/session/move must keep CLI source identity
    on the on-disk sidecar after the lock-held reload.
    """
    from api import profiles as _profiles_mod

    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "cli-source-move-1"
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "alpha")
    monkeypatch.setattr(
        _profiles_mod, "get_active_profile_name", lambda: "alpha",
    )
    _seed_sidecar_without_source_meta(session_dir, sid, title="cli title")

    cli_meta = {
        "session_id": sid,
        "is_cli_session": True,
        "source_tag": "cli",
        "raw_source": "cli",
        "session_source": "cli",
        "source_label": "CLI",
    }
    monkeypatch.setattr(
        routes, "_lookup_cli_session_metadata",
        lambda value, *, all_profiles=False: dict(cli_meta),
    )
    monkeypatch.setattr(routes, "load_projects", lambda: [])
    captured = _capture_post(
        monkeypatch, {"session_id": sid, "project_id": None},
    )
    monkeypatch.setattr(
        routes, "publish_session_list_changed", lambda *a, **kw: None,
    )

    result = routes.handle_post(
        _FakeHandler(b'{"session_id": "' + sid.encode() + b'"}'),
        SimpleNamespace(path="/api/session/move"),
    )

    assert result is True, "move handler must claim the request"
    assert captured["status"] == 200, f"expected 200, got {captured}"
    on_disk = _load_disk_session(session_dir, sid)
    assert on_disk.get("is_cli_session") is True, (
        "move must preserve is_cli_session=True; on-disk "
        f"is_cli_session={on_disk.get('is_cli_session')!r}"
    )
    assert on_disk.get("source_tag") == "cli", (
        "move must preserve source_tag; on-disk "
        f"source_tag={on_disk.get('source_tag')!r}"
    )
    assert on_disk.get("raw_source") == "cli", (
        "move must preserve raw_source; on-disk "
        f"raw_source={on_disk.get('raw_source')!r}"
    )


def test_archive_handler_preserves_cli_source_identity_across_lock_reload(
    tmp_path, monkeypatch,
):
    """#7776 Finding 2: /api/session/archive must keep CLI source identity
    on the on-disk sidecar after the lock-held reload.

    Archive is the most-affected handler: its own pre-lock materialize
    fallback (api/routes.py:~17543-17590) uses ``import_cli_session``
    (which saves WITHOUT source meta) and then mutates the in-memory
    object without a follow-up save. The lock-held Session.load(sid)
    reload then reads the source-stripped sidecar. The fix re-stamps
    CLI source identity after the reload and before the save().
    """
    from api import profiles as _profiles_mod

    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "cli-source-archive-1"
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "alpha")
    monkeypatch.setattr(
        _profiles_mod, "get_active_profile_name", lambda: "alpha",
    )
    # Bug-state sidecar: import_cli_session output, default source identity.
    _seed_sidecar_without_source_meta(session_dir, sid, title="cli title")

    cli_meta = {
        "session_id": sid,
        "is_cli_session": True,
        "source_tag": "hermes-tui",
        "raw_source": "hermes-tui",
        "session_source": "tui",
        "source_label": "Hermes TUI",
    }
    monkeypatch.setattr(
        routes, "_lookup_cli_session_metadata",
        lambda value, *, all_profiles=False: dict(cli_meta),
    )
    monkeypatch.setattr(
        routes, "_session_is_subagent_view_only", lambda value: False,
    )
    captured = _capture_post(
        monkeypatch, {"session_id": sid, "archived": True},
    )
    monkeypatch.setattr(
        routes, "publish_session_list_changed", lambda *a, **kw: None,
    )
    monkeypatch.setattr(routes, "_worktree_retained_payload", lambda s: {})

    result = routes.handle_post(
        _FakeHandler(
            b'{"session_id": "' + sid.encode() + b'", "archived": true}',
        ),
        SimpleNamespace(path="/api/session/archive"),
    )

    assert result is True, "archive handler must claim the request"
    assert captured["status"] == 200, f"expected 200, got {captured}"
    on_disk = _load_disk_session(session_dir, sid)
    assert on_disk.get("archived") is True
    assert on_disk.get("is_cli_session") is True, (
        "archive must preserve is_cli_session=True; on-disk "
        f"is_cli_session={on_disk.get('is_cli_session')!r}"
    )
    assert on_disk.get("source_tag") == "hermes-tui", (
        "archive must preserve source_tag; on-disk "
        f"source_tag={on_disk.get('source_tag')!r}"
    )
    assert on_disk.get("raw_source") == "hermes-tui", (
        "archive must preserve raw_source; on-disk "
        f"raw_source={on_disk.get('raw_source')!r}"
    )


# ---------------------------------------------------------------------------
# PR #7776 Finding 3 (SILENT regression): rename/move/archive must not
# re-stamp a WebUI fork as session_source="webui".
#
# ``_apply_cli_source_meta_to_session`` re-stamps every source field from
# ``_lookup_cli_session_metadata(sid)``. That lookup walks
# ``get_cli_sessions()``, which projects state.db rows for EVERY source —
# including WebUI-origin rows (session_source="webui"). A WebUI fork is one
# of those rows, so the blanket re-stamp overwrote the fork's identity.
#
# These tests seed a REAL isolated SQLite state.db with a WebUI-origin row
# (so the actual get_cli_sessions() projection is exercised, not a patched
# ``_lookup_cli_session_metadata``) and then drive rename / move / archive
# through the real POST handlers, asserting session_source stays "fork".
# ---------------------------------------------------------------------------


WEBUI_STATE_DB_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    model TEXT,
    message_count INTEGER DEFAULT 0,
    actual_message_count INTEGER DEFAULT 0,
    started_at REAL,
    last_activity REAL,
    source TEXT,
    session_source TEXT,
    cwd TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    timestamp REAL NOT NULL
);
"""


def _seed_webui_state_db_row(tmp_path, sid, *, title="Fork chat"):
    """Create an isolated state.db whose ``sessions`` row for ``sid`` is
    WebUI-origin (source='webui'), exactly what a WebUI fork session writes.

    Returns the ``state.db`` path. Point ``models._active_state_db_path`` /
    ``routes`` at it so ``get_cli_sessions()`` projects this row.
    """
    import sqlite3

    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(WEBUI_STATE_DB_SCHEMA)
        conn.execute(
            "INSERT INTO sessions "
            "(id, title, model, message_count, actual_message_count, started_at, "
            " last_activity, source, session_source, cwd) "
            "VALUES (?, ?, 'gpt-test', 2, 2, 100.0, 200.0, 'webui', 'webui', ?)",
            (sid, title, str(tmp_path)),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES (?, 'user', 'hello', 101.0)",
            (sid,),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES (?, 'assistant', 'hi there', 102.0)",
            (sid,),
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _seed_fork_sidecar(session_dir, sid, *, title="Fork chat", profile="alpha"):
    """Persist a WebUI-origin fork sidecar (what /api/session/branch writes):
    session_source='fork', parent_session_id set, default is_cli_session."""
    fork = Session(
        session_id=sid,
        title=title,
        workspace=str(session_dir.parent),
        messages=[{"role": "user", "content": "hello"}],
        composer_draft={"text": "fork draft"},
        profile=profile,
        session_source="fork",
        parent_session_id="fork-parent-1",
    )
    fork.save()
    return fork


def _isolate_state_db_for_cli_projection(tmp_path, monkeypatch, db_path):
    """Point the state.db resolution at an isolated DB and clear caches.

    ``_resolve_cli_sessions_context`` reads ``profiles.get_active_hermes_home``
    and appends ``/state.db``, so the DB must live inside the redirected home.
    """
    from api import profiles as _profiles_mod

    hermes_home = db_path.parent
    monkeypatch.setattr(
        _profiles_mod, "get_active_hermes_home", lambda: str(hermes_home),
    )
    monkeypatch.setattr(
        _profiles_mod, "get_active_profile_name", lambda: "alpha",
    )
    # No Claude Code project dir on the test box — return an empty projection.
    monkeypatch.setattr(models, "get_claude_code_sessions", lambda: [])
    monkeypatch.setenv("HERMES_WEBUI_CLAUDE_PROJECTS_DIR", str(hermes_home / "no-claude"))
    models.clear_cli_sessions_cache()
    models.clear_sidecar_metadata_cache()
    return db_path


def _setup_fork_rename_scenario(tmp_path, monkeypatch, *, sid):
    """Shared wiring for a fork rename through the real handler."""
    from api import profiles as _profiles_mod

    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "alpha")
    monkeypatch.setattr(
        _profiles_mod, "get_active_profile_name", lambda: "alpha",
    )
    db_path = _seed_webui_state_db_row(tmp_path, sid)
    _isolate_state_db_for_cli_projection(tmp_path, monkeypatch, db_path)
    _seed_fork_sidecar(session_dir, sid)
    return session_dir


def _assert_fork_identity_survived(on_disk, sid, *, context):
    """The fork's WebUI-owned identity must be byte-identical after the op."""
    assert on_disk.get("session_source") == "fork", (
        f"{context} must not re-stamp a WebUI fork's session_source; "
        f"on-disk session_source={on_disk.get('session_source')!r}"
    )
    assert on_disk.get("parent_session_id") == "fork-parent-1", (
        f"{context} must preserve the fork's parent link; "
        f"on-disk parent_session_id={on_disk.get('parent_session_id')!r}"
    )
    assert on_disk.get("is_cli_session") is not True, (
        f"{context} must not mark a WebUI fork as a CLI-imported session; "
        f"on-disk is_cli_session={on_disk.get('is_cli_session')!r}"
    )


def test_rename_handler_does_not_restamp_fork_as_webui(tmp_path, monkeypatch):
    """#7776 Finding 3: rename of a WebUI fork must keep session_source='fork'.

    get_cli_sessions() returns the WebUI-origin state.db row for this sid, so
    the pre-fix helper overwrote the fork's identity with the row's
    session_source='webui' and is_cli_session=False on the save.
    """
    sid = "webui-fork-rename-1"
    session_dir = _setup_fork_rename_scenario(tmp_path, monkeypatch, sid=sid)
    captured = _capture_post(
        monkeypatch, {"session_id": sid, "title": "Renamed fork"},
    )
    _stub_post_lock_side_effects(monkeypatch)

    result = routes.handle_post(
        _FakeHandler(
            b'{"session_id": "' + sid.encode() + b'", "title": "Renamed fork"}',
        ),
        SimpleNamespace(path="/api/session/rename"),
    )

    assert result is True, "rename handler must claim the request"
    assert captured["status"] == 200, f"expected 200, got {captured}"
    on_disk = _load_disk_session(session_dir, sid)
    assert on_disk["title"] == "Renamed fork"
    _assert_fork_identity_survived(on_disk, sid, context="rename")


def test_move_handler_does_not_restamp_fork_as_webui(tmp_path, monkeypatch):
    """#7776 Finding 3: move of a WebUI fork must keep session_source='fork'."""
    sid = "webui-fork-move-1"
    session_dir = _setup_fork_rename_scenario(tmp_path, monkeypatch, sid=sid)
    monkeypatch.setattr(routes, "load_projects", lambda: [])
    captured = _capture_post(
        monkeypatch, {"session_id": sid, "project_id": None},
    )
    monkeypatch.setattr(
        routes, "publish_session_list_changed", lambda *a, **kw: None,
    )

    result = routes.handle_post(
        _FakeHandler(b'{"session_id": "' + sid.encode() + b'"}'),
        SimpleNamespace(path="/api/session/move"),
    )

    assert result is True, "move handler must claim the request"
    assert captured["status"] == 200, f"expected 200, got {captured}"
    on_disk = _load_disk_session(session_dir, sid)
    _assert_fork_identity_survived(on_disk, sid, context="move")


def test_archive_handler_does_not_restamp_fork_as_webui(tmp_path, monkeypatch):
    """#7776 Finding 3: archive of a WebUI fork must keep session_source='fork'."""
    sid = "webui-fork-archive-1"
    session_dir = _setup_fork_rename_scenario(tmp_path, monkeypatch, sid=sid)
    monkeypatch.setattr(
        routes, "_session_is_subagent_view_only", lambda value: False,
    )
    captured = _capture_post(
        monkeypatch, {"session_id": sid, "archived": True},
    )
    monkeypatch.setattr(
        routes, "publish_session_list_changed", lambda *a, **kw: None,
    )
    monkeypatch.setattr(routes, "_worktree_retained_payload", lambda s: {})

    result = routes.handle_post(
        _FakeHandler(
            b'{"session_id": "' + sid.encode() + b'", "archived": true}',
        ),
        SimpleNamespace(path="/api/session/archive"),
    )

    assert result is True, "archive handler must claim the request"
    assert captured["status"] == 200, f"expected 200, got {captured}"
    on_disk = _load_disk_session(session_dir, sid)
    assert on_disk.get("archived") is True, (
        "archive must still persist archived=True on the fork"
    )
    _assert_fork_identity_survived(on_disk, sid, context="archive")


def test_apply_cli_source_meta_skips_webui_origin_row():
    """#7776 Finding 3 unit edge: a WebUI-origin cli_meta must be a no-op,
    even when the in-memory session currently has null source fields."""
    import api.routes as routes
    from types import SimpleNamespace as _NS

    session = _NS(
        session_id="s1",
        is_cli_session=False,
        source_tag=None,
        raw_source=None,
        session_source=None,
        source_label=None,
    )
    routes._apply_cli_source_meta_to_session(
        session,
        {
            "session_id": "s1",
            "source_tag": "webui",
            "raw_source": "webui",
            "session_source": "webui",
            "source_label": "WebUI",
        },
    )
    assert session.session_source is None, (
        "a WebUI-origin cli_meta row must not stamp the session at all"
    )
    assert session.source_tag is None
    assert session.is_cli_session is False


def test_apply_cli_source_meta_never_overwrites_existing_fork():
    """#7776 Finding 3 unit edge: an existing session_source='fork' survives a
    non-WebUI cli_meta (defense in depth for a mislabelled state.db row)."""
    import api.routes as routes
    from types import SimpleNamespace as _NS

    session = _NS(
        session_id="fork1",
        is_cli_session=False,
        source_tag=None,
        raw_source=None,
        session_source="fork",
        source_label=None,
        parent_session_id="parent1",
    )
    routes._apply_cli_source_meta_to_session(
        session,
        {
            "session_id": "fork1",
            "is_cli_session": True,
            "source_tag": "cli",
            "raw_source": "cli",
            "session_source": "cli",
            "source_label": "CLI",
        },
    )
    assert session.session_source == "fork", (
        "an existing session_source='fork' must never be overwritten"
    )
    assert session.is_cli_session is False
    assert session.source_tag is None
