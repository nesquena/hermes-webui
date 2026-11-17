"""WebUI-held session/always grants land only in the session's own profile.

For a raw ``_pending`` entry no Agent waits on, WebUI saves the grant itself.
It used to call the Agent's grant functions in the HTTP thread's unscoped home
(the launch profile) and save the whole in-memory permanent set, so a grant in
one profile was written into another profile's ``command_allowlist``.
``_resolve_approval_legacy`` now binds the Agent's context-local Hermes home to
the session's own profile around those calls, saves nothing when that profile
can't be established, and saves nothing for a gateway mirror whose producer is
gone. A "session" answer uses the Agent's own ``_persist_choice`` when it takes
plain keys. With an Agent bound, a WebUI-held "always" answer is granted for the
session only: no permanent grant and no save, so it can never write back an entry
removed from ``command_allowlist`` by hand, and it is asked again in a later
session. The Agent's own "always" saves are unchanged and serialized, so two of
them in one profile can't overwrite each other.

These tests use the real Agent ``tools.approval`` in process and read each
profile's ``config.yaml``.
"""
from __future__ import annotations

import inspect
import logging
import sys
import threading
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import yaml

from api import models
from api import profiles
from api import route_approvals
from api import routes

agent_approval = pytest.importorskip("tools.approval")
hermes_constants = pytest.importorskip("hermes_constants")

pytestmark = pytest.mark.skipif(
    not (
        route_approvals.APPROVAL_BRIDGE["agent"]
        and route_approvals.APPROVAL_BRIDGE["scopes_available"]
        and hasattr(hermes_constants, "set_hermes_home_override")
    ),
    reason="needs an Agent with the approval grant functions and the Hermes-home override",
)

KEY = "recursive delete"
KEY_2 = "world/other-writable permissions"
LATER = "a-later-session"  # a session with no session grants: only permanent ones count


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """A root home (the launch profile) with named profiles alpha and beta."""
    root = (tmp_path / "root").resolve()
    found = {"default": root, "alpha": root / "profiles" / "alpha", "beta": root / "profiles" / "beta"}
    for home in found.values():
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("approvals:\n  mode: manual\n", encoding="utf-8")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setenv("HERMES_HOME", str(root))
    baselines = getattr(agent_approval, "_permanent_baseline_by_home", {})
    with routes._lock:
        launch_grants = set(agent_approval._permanent_approved)
        launch_baseline = baselines.get("")
    yield found
    with routes._lock:  # leave the launch profile's in-memory grants as they were
        agent_approval._permanent_approved.clear()
        agent_approval._permanent_approved.update(launch_grants)
        if launch_baseline is None:
            baselines.pop("", None)
        else:
            baselines[""] = launch_baseline


@pytest.fixture
def sessions(monkeypatch):
    made = []

    def make(profile):
        sid = f"approval-profile-{uuid.uuid4().hex[:8]}"
        if profile is not None:
            monkeypatch.setitem(models.SESSIONS, sid, SimpleNamespace(session_id=sid, profile=profile))
        made.append(sid)
        return sid

    yield make
    with routes._lock:  # the Agent's own lock; older Agents' is not re-entrant
        for sid in made:
            routes._pending.pop(sid, None)
        for grant_key in list(agent_approval._session_approved):
            if grant_key in made or (isinstance(grant_key, tuple) and grant_key[0] in made):
                agent_approval._session_approved.pop(grant_key, None)


def _queue(sid, key=KEY, **extra):
    entry = {
        "command": "rm -rf /tmp/approval-profile",
        "pattern_key": key,
        "pattern_keys": [key],
        "description": key,
        "approval_id": f"aid-{sid}",
        **extra,
    }
    with routes._lock:
        routes._pending.setdefault(sid, []).append(entry)
    return entry["approval_id"]


def _allowlist(home):
    data = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
    return sorted(data.get("command_allowlist") or [])


@contextmanager
def _in_home(home):
    token = hermes_constants.set_hermes_home_override(str(home))
    try:
        yield
    finally:
        hermes_constants.reset_hermes_home_override(token)


def _approved_in(home, sid, key=KEY):
    with _in_home(home):
        return agent_approval.is_approved(sid, key)


def _config_bytes(homes):
    return {name: (home / "config.yaml").read_bytes() for name, home in homes.items()}


def _fresh_session_approved(home, key):
    """Whether a session with no grants of its own is approved for ``key`` in ``home``.
    An Agent without ``_sync_permanent_from_disk`` (v2026.9.24) applies hand edits to
    command_allowlist only when it loads or saves the file, so it loads first."""
    with _in_home(home):
        if not hasattr(agent_approval, "_sync_permanent_from_disk"):
            agent_approval.load_permanent_allowlist()
        return agent_approval.is_approved(LATER, key)


def _permanent_in_memory(home):
    with _in_home(home):
        with agent_approval._lock:
            return set(agent_approval._permanent_set())


def test_always_in_one_profile_is_granted_for_that_session_only(homes, sessions):
    alpha, beta = sessions("alpha"), sessions("beta")
    alpha_id = _queue(alpha)
    _queue(beta)
    before = _config_bytes(homes)

    assert routes._resolve_approval_legacy(alpha, alpha_id, "always") is True

    assert _config_bytes(homes) == before  # no profile's config.yaml is written
    assert _approved_in(homes["alpha"], alpha)
    for home in homes.values():
        assert not _approved_in(home, LATER)  # asked again in a later session
    assert not _approved_in(homes["beta"], beta)
    with routes._lock:
        assert routes._pending.get(beta)  # beta's card is still waiting for its own answer


def test_session_outside_the_launch_profile_is_granted_in_its_own_profile(homes, sessions, monkeypatch):
    # The server runs as alpha (launch home and active profile); the session belongs to beta.
    monkeypatch.setenv("HERMES_HOME", str(homes["alpha"]))
    monkeypatch.setattr(profiles, "_active_profile", "alpha")
    beta = sessions("beta")
    before = _config_bytes(homes)

    assert routes._resolve_approval_legacy(beta, _queue(beta), "always") is True

    assert _approved_in(homes["beta"], beta)
    assert _config_bytes(homes) == before
    assert not _approved_in(homes["beta"], LATER) and not _approved_in(homes["alpha"], LATER)


def test_always_writes_no_unsaved_grant_from_elsewhere(homes, sessions):
    """An unsaved in-memory grant from elsewhere is not written into this profile."""
    other = f"other key {uuid.uuid4().hex[:6]}"
    with agent_approval._lock:
        agent_approval._permanent_approved.add(other)
    try:
        root_session = sessions("default")
        assert routes._resolve_approval_legacy(root_session, _queue(root_session), "always") is True
    finally:
        with agent_approval._lock:
            agent_approval._permanent_approved.discard(other)

    assert _allowlist(homes["default"]) == []
    assert _approved_in(homes["default"], root_session)
    assert not _approved_in(homes["default"], LATER)


def test_unresolvable_session_profile_persists_nothing(homes, sessions, caplog):
    orphan = sessions(None)  # no WebUI session record at all

    with caplog.at_level(logging.WARNING, logger=routes.logger.name):
        assert routes._resolve_approval_legacy(orphan, _queue(orphan), "always") is True

    for home in homes.values():
        assert _allowlist(home) == []
        assert not _approved_in(home, orphan)
    assert not agent_approval.is_approved(orphan, KEY)
    assert any("not saved" in r.getMessage() and orphan in r.getMessage() for r in caplog.records)


def test_orphan_gateway_mirror_answered_always_persists_nothing(homes, sessions):
    """The producer is gone (timed out and denied), so the answer was never used."""
    alpha = sessions("alpha")
    approval_id = _queue(alpha, **{route_approvals._GATEWAY_MIRROR_FLAG: True})

    assert routes._resolve_approval_legacy(alpha, approval_id, "always") is True

    for home in homes.values():
        assert _allowlist(home) == []
        assert not _approved_in(home, alpha)
    with routes._lock:
        assert not routes._pending.get(alpha)


def _fail_first_save(monkeypatch):
    """The first ``save_config`` raises once (the Agent's save logs it and goes on)."""
    config = pytest.importorskip("hermes_cli.config")
    original = config.save_config
    calls = []

    def flaky(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise OSError("one-time save failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(config, "save_config", flaky)
    return calls


def _agent_takes_keys():
    """Whether this Agent's ``_persist_choice`` takes plain keys (not v2026.9.24's tuples)."""
    persist = getattr(agent_approval, "_persist_choice", None)
    try:
        return list(inspect.signature(persist).parameters)[2] == "keys"
    except Exception:
        return False


def test_webui_binds_the_agents_persist_choice_only_for_its_keys_shape():
    expected = agent_approval._persist_choice if _agent_takes_keys() else None
    assert route_approvals.persist_grant_choice is expected
    assert routes.persist_grant_choice is expected


def _spy_grant_paths(monkeypatch):
    """Record what reaches the Agent's ``_persist_choice`` and WebUI's save, calling through."""
    persisted, saved = [], []
    persist, save = routes.persist_grant_choice, routes.save_permanent_allowlist
    if persist is not None:
        monkeypatch.setattr(routes, "persist_grant_choice", lambda *a: persisted.append(a) or persist(*a))
    monkeypatch.setattr(routes, "save_permanent_allowlist", lambda keys: saved.append(set(keys)) or save(keys))
    return persisted, saved


def _spy_permanent_grants(monkeypatch):
    """Record WebUI's permanent grant calls, calling through."""
    granted = []
    real = routes.approve_permanent
    monkeypatch.setattr(routes, "approve_permanent", lambda key: granted.append(key) or real(key))
    return granted


def test_session_answer_uses_the_agents_grant_and_always_is_granted_for_the_session_only(
    homes, sessions, monkeypatch
):
    """Runs on every Agent. "session" goes through a keys-shaped ``_persist_choice`` (WebUI's
    own grant on v2026.9.24); "always" grants the session only: no permanent grant, no save."""
    persisted, saved = _spy_grant_paths(monkeypatch)
    granted = _spy_permanent_grants(monkeypatch)
    by_session, by_always = sessions("alpha"), sessions("alpha")

    assert routes._resolve_approval_legacy(by_session, _queue(by_session), "session") is True
    assert _approved_in(homes["alpha"], by_session)
    assert not _approved_in(homes["alpha"], LATER)
    assert _allowlist(homes["alpha"]) == []
    assert persisted == ([(by_session, "session", [KEY])] if _agent_takes_keys() else [])

    before = _config_bytes(homes)
    assert routes._resolve_approval_legacy(by_always, _queue(by_always, KEY_2), "always") is True
    assert _approved_in(homes["alpha"], by_always, KEY_2)
    assert not _approved_in(homes["alpha"], LATER, KEY_2)
    assert len(persisted) == (1 if _agent_takes_keys() else 0)  # not used for "always"
    assert granted == [] and saved == []
    assert _config_bytes(homes) == before


def test_always_keeps_a_content_scanner_key_session_only(homes, sessions):
    """A raw entry (v2026.9.24's no-notifier ``_pending``) carries no tirith flag. Every
    key, ``tirith:`` or not, is granted for the session only."""
    tirith = "tirith:some_rule"
    sid = sessions("alpha")
    entry_id = _queue(sid, tirith, pattern_keys=[tirith, KEY])

    assert routes._resolve_approval_legacy(sid, entry_id, "always") is True

    assert _allowlist(homes["alpha"]) == []
    assert _approved_in(homes["alpha"], sid, tirith) and _approved_in(homes["alpha"], sid, KEY)
    assert not _approved_in(homes["alpha"], LATER, tirith)
    assert not _approved_in(homes["alpha"], LATER, KEY)
    assert _allowlist(homes["beta"]) == [] and _allowlist(homes["default"]) == []


def test_always_saves_nothing_and_grants_no_other_profiles_key(homes, sessions, monkeypatch):
    """No WebUI-held "always" reaches a save (so none can fail or carry another grant), and
    in-memory grants of other profiles are not granted here."""
    beta_only, root_only = f"beta key {uuid.uuid4().hex[:6]}", f"root key {uuid.uuid4().hex[:6]}"
    with _in_home(homes["beta"]):
        agent_approval.approve_permanent(beta_only)
    agent_approval.approve_permanent(root_only)  # the unscoped launch profile's set
    saves = _fail_first_save(monkeypatch)
    first, second = sessions("alpha"), sessions("alpha")

    assert routes._resolve_approval_legacy(first, _queue(first), "always") is True
    assert routes._resolve_approval_legacy(second, _queue(second, KEY_2), "always") is True

    assert saves == []
    assert _allowlist(homes["alpha"]) == []
    for key in (KEY, KEY_2, beta_only, root_only):
        assert not _approved_in(homes["alpha"], LATER, key)
    assert _approved_in(homes["alpha"], first, KEY) and _approved_in(homes["alpha"], second, KEY_2)
    assert _allowlist(homes["beta"]) == []
    assert _allowlist(homes["default"]) == []
    assert not _approved_in(homes["beta"], LATER, KEY)


def _agent_answer(home, sid, key):
    """The Agent's own grant for an approval it waited on, in its own thread."""
    with _in_home(home):
        if _agent_takes_keys():
            agent_approval._persist_choice(sid, "always", [key])
        else:  # v2026.9.24: (key, description, is_tirith)
            agent_approval._persist_choice(sid, "always", [(key, key, False)])


@pytest.mark.parametrize("path", ["persist_choice", "fallback"])
def test_always_does_not_restore_an_entry_removed_by_hand(homes, sessions, monkeypatch, path):
    """``persist_choice``: this Agent's binding as is; ``fallback``: no ``_persist_choice``.
    The Agent saved X; the operator removes it; a WebUI-held "always" for Y writes nothing."""
    if path == "fallback":
        monkeypatch.setattr(routes, "persist_grant_choice", None)
    first, second = sessions("alpha"), sessions("alpha")
    _agent_answer(homes["alpha"], first, KEY)
    assert _allowlist(homes["alpha"]) == [KEY]
    persisted, saved = _spy_grant_paths(monkeypatch)
    granted = _spy_permanent_grants(monkeypatch)

    # The operator withdraws it in config.yaml.
    (homes["alpha"] / "config.yaml").write_text(
        "approvals:\n  mode: manual\ncommand_allowlist: []\n", encoding="utf-8"
    )
    assert routes._resolve_approval_legacy(second, _queue(second, KEY_2), "always") is True

    assert _allowlist(homes["alpha"]) == []
    assert _approved_in(homes["alpha"], second, KEY_2)
    assert not _fresh_session_approved(homes["alpha"], KEY)
    assert not _approved_in(homes["alpha"], LATER, KEY_2)
    assert persisted == [] and saved == [] and granted == []


def test_overlapping_always_answers_do_not_restore_an_entry_removed_by_hand(homes, sessions, monkeypatch):
    """A (the Agent's own "always" for X) is held in its save; B (a WebUI-held "always"
    for Y) answers meanwhile and finishes without saving. A then saves X, the operator
    removes X from config.yaml, and nothing writes it back."""
    serialized_save = agent_approval.save_permanent_allowlist
    reached, resume = threading.Event(), threading.Event()
    passed = {}

    def held_save(patterns):
        name = threading.current_thread().name
        passed[name] = sorted(patterns)
        if name == "answer-A":
            reached.set()
            resume.wait(10)
        return serialized_save(patterns)

    monkeypatch.setattr(agent_approval, "save_permanent_allowlist", held_save)  # the Agent's own path
    monkeypatch.setattr(routes, "save_permanent_allowlist", held_save)
    sid_a, sid_b = sessions("alpha"), sessions("alpha")
    errors = []

    def answer(sid, key, by_agent):
        try:
            if by_agent:
                _agent_answer(homes["alpha"], sid, key)
            else:
                assert routes._resolve_approval_legacy(sid, _queue(sid, key), "always") is True
        except Exception as exc:  # surfaced below
            errors.append(repr(exc))

    a = threading.Thread(target=answer, args=(sid_a, KEY, True), name="answer-A", daemon=True)
    b = threading.Thread(target=answer, args=(sid_b, KEY_2, False), name="answer-B", daemon=True)
    try:
        a.start()
        assert reached.wait(10)
        b.start()
        b.join(10)
        assert not b.is_alive()  # never waits for the save lock: it does not save
        resume.set()
        a.join(10)
        assert not a.is_alive() and _allowlist(homes["alpha"]) == [KEY]

        (homes["alpha"] / "config.yaml").write_text(
            "approvals:\n  mode: manual\ncommand_allowlist: []\n", encoding="utf-8"
        )
    finally:
        resume.set()

    assert errors == []
    assert "answer-B" not in passed
    assert _allowlist(homes["alpha"]) == []
    assert _approved_in(homes["alpha"], sid_b, KEY_2)
    assert not _fresh_session_approved(homes["alpha"], KEY)
    assert not _approved_in(homes["alpha"], LATER, KEY_2)
    assert _allowlist(homes["beta"]) == [] and _allowlist(homes["default"]) == []


def _hold_webui_session_grant(monkeypatch):
    """Hold WebUI's session grant (called on every version of the WebUI-held path) right
    after it runs; returns (granted, resume)."""
    granted, resume = threading.Event(), threading.Event()
    real_approve_session = routes.approve_session

    def held_approve_session(sid, key):
        real_approve_session(sid, key)
        if threading.current_thread().name == "webui-answer":
            granted.set()
            assert resume.wait(15)

    monkeypatch.setattr(routes, "approve_session", held_approve_session)
    return granted, resume


def test_always_for_a_key_removed_by_hand_while_it_is_granted_stays_removed(homes, sessions, monkeypatch):
    """The Agent saved X. A WebUI-held "always" for X is held right after its session grant
    while the operator removes X from command_allowlist and a check reads that; finishing
    the answer must not write X back or grant it to a later session."""
    first, second = sessions("alpha"), sessions("alpha")
    _agent_answer(homes["alpha"], first, KEY)
    assert _allowlist(homes["alpha"]) == [KEY]
    second_id = _queue(second)
    granted, resume = _hold_webui_session_grant(monkeypatch)
    errors = []

    def answer():
        try:
            assert routes._resolve_approval_legacy(second, second_id, "always") is True
        except Exception as exc:  # surfaced below
            errors.append(repr(exc))

    thread = threading.Thread(target=answer, name="webui-answer", daemon=True)
    try:
        thread.start()
        assert granted.wait(10)
        (homes["alpha"] / "config.yaml").write_text(
            "approvals:\n  mode: manual\ncommand_allowlist: []\n", encoding="utf-8"
        )
        assert not _fresh_session_approved(homes["alpha"], KEY)  # the revocation is in effect
    finally:
        resume.set()
    thread.join(10)

    assert not thread.is_alive() and errors == []
    assert _allowlist(homes["alpha"]) == []
    assert not _approved_in(homes["alpha"], LATER, KEY)  # no reload since the check above
    assert _approved_in(homes["alpha"], second, KEY)  # this session only
    assert _allowlist(homes["beta"]) == [] and _allowlist(homes["default"]) == []


def test_a_key_removed_by_hand_while_it_is_granted_is_dropped_from_memory_too(homes, sessions, monkeypatch):
    """As above, but nothing re-reads command_allowlist before the answer finishes. No
    WebUI grant reaches the Agent's permanent set or the file, so once the Agent applies
    the edit (at once, or at its next load on v2026.9.24) X is asked again."""
    first, second = sessions("alpha"), sessions("alpha")
    _agent_answer(homes["alpha"], first, KEY)
    assert _allowlist(homes["alpha"]) == [KEY]
    second_id = _queue(second)
    granted, resume = _hold_webui_session_grant(monkeypatch)
    permanent_grants = _spy_permanent_grants(monkeypatch)
    _persisted, saved = _spy_grant_paths(monkeypatch)
    errors = []

    def answer():
        try:
            assert routes._resolve_approval_legacy(second, second_id, "always") is True
        except Exception as exc:  # surfaced below
            errors.append(repr(exc))

    thread = threading.Thread(target=answer, name="webui-answer", daemon=True)
    try:
        thread.start()
        assert granted.wait(10)
        (homes["alpha"] / "config.yaml").write_text(
            "approvals:\n  mode: manual\ncommand_allowlist: []\n", encoding="utf-8"
        )
        held_in_memory = _permanent_in_memory(homes["alpha"])
    finally:
        resume.set()
    thread.join(10)

    assert not thread.is_alive() and errors == []
    assert permanent_grants == [] and saved == []
    assert _permanent_in_memory(homes["alpha"]) <= held_in_memory  # nothing added by WebUI
    assert _allowlist(homes["alpha"]) == []
    data = yaml.safe_load((homes["alpha"] / "config.yaml").read_text(encoding="utf-8"))
    assert data["approvals"] == {"mode": "manual"}  # the rest of the file is kept
    assert not _fresh_session_approved(homes["alpha"], KEY)
    assert _allowlist(homes["beta"]) == [] and _allowlist(homes["default"]) == []


@pytest.mark.parametrize("pause_at", ["grant", "save"])
def test_webui_always_racing_an_agent_always_never_restores_an_entry_removed_by_hand(
    homes, sessions, monkeypatch, pause_at
):
    """The Agent saved X. A WebUI-held "always" for X is held after its session grant; the
    Agent's own "always" for Y (its own save path; v2026.9.24 saves a copy of its whole
    in-memory set) is held at its grant or its save. The operator then removes X and adds
    an entry by hand, and both answers finish. X stays removed: not on disk, and neither
    the real command guard nor is_approved approves it in a fresh session."""
    seed = sessions("alpha")
    _agent_answer(homes["alpha"], seed, KEY)
    assert _allowlist(homes["alpha"]) == [KEY]
    webui_sid, agent_sid = sessions("alpha"), sessions("alpha")
    webui_id = _queue(webui_sid)
    granted, resume_webui = _hold_webui_session_grant(monkeypatch)
    agent_reached, resume_agent = threading.Event(), threading.Event()
    real_grant = agent_approval.approve_permanent
    real_save = agent_approval.save_permanent_allowlist
    saves, errors = [], []

    def held_agent_grant(key):
        real_grant(key)
        if threading.current_thread().name == "agent-answer" and pause_at == "grant":
            agent_reached.set()
            assert resume_agent.wait(15)

    def held_agent_save(keys):
        saves.append((threading.current_thread().name, sorted(keys)))
        if threading.current_thread().name == "agent-answer" and pause_at == "save":
            agent_reached.set()
            assert resume_agent.wait(15)
        return real_save(keys)

    monkeypatch.setattr(agent_approval, "approve_permanent", held_agent_grant)
    monkeypatch.setattr(agent_approval, "save_permanent_allowlist", held_agent_save)

    def webui_answer():
        try:
            assert routes._resolve_approval_legacy(webui_sid, webui_id, "always") is True
        except BaseException as exc:  # surfaced below
            errors.append(repr(exc))

    def agent_answer():
        try:
            _agent_answer(homes["alpha"], agent_sid, KEY_2)
        except BaseException as exc:  # surfaced below
            errors.append(repr(exc))

    a = threading.Thread(target=webui_answer, name="webui-answer", daemon=True)
    b = threading.Thread(target=agent_answer, name="agent-answer", daemon=True)
    try:
        a.start()
        assert granted.wait(15)
        b.start()
        assert agent_reached.wait(15)
        (homes["alpha"] / "config.yaml").write_text(
            "approvals:\n  mode: manual\ncommand_allowlist: [operator-added]\n", encoding="utf-8"
        )
        resume_webui.set()
        a.join(15)
        assert not a.is_alive()
    finally:
        resume_webui.set()
        resume_agent.set()
    for t in (a, b):
        t.join(15)
        assert not t.is_alive(), t.name
    assert errors == []

    from tools import approval_context

    fresh = f"approval-profile-fresh-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "webui")
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")

    def refusing_notify(_data):
        raise RuntimeError("the notifier refuses, so no wait is started")

    agent_approval.register_gateway_notify(fresh, refusing_notify)
    try:
        with _in_home(homes["alpha"]):
            approval_context.set_current_session_key(fresh)
            # Only the guard runs; the command is never executed.
            guard = agent_approval.check_all_command_guards("rm -rf ./approval-profile-never-run", "local")
    finally:
        agent_approval.unregister_gateway_notify(fresh)
    detail = {"disk": _allowlist(homes["alpha"]), "saves": saves, "guard": guard}
    assert guard["approved"] is False, detail
    assert set(_allowlist(homes["alpha"])) == {"operator-added", KEY_2}, detail  # the Agent's own save
    assert not _approved_in(homes["alpha"], fresh, KEY)
    assert not _approved_in(homes["alpha"], LATER, KEY)
    assert _approved_in(homes["alpha"], LATER, KEY_2)
    assert all(name == "agent-answer" for name, _keys in saves), detail
    assert _allowlist(homes["beta"]) == [] and _allowlist(homes["default"]) == []


# ── Two "always" saves in one profile at once ───────────────────────────────


def test_the_agents_save_is_wrapped_once_and_webui_binds_the_wrapper():
    save = agent_approval.save_permanent_allowlist
    assert getattr(save, "_webui_serialized", False) is True
    assert route_approvals.save_permanent_allowlist is save
    assert routes.save_permanent_allowlist is save
    assert save._webui_save_lock is route_approvals._allowlist_save_lock
    assert not getattr(save._webui_original, "_webui_serialized", False)
    # A second bind (a reload or second import) keeps the same wrapper and lock.
    assert route_approvals._serialize_allowlist_saves(agent_approval) is save._webui_save_lock
    assert agent_approval.save_permanent_allowlist is save


@pytest.mark.parametrize("second_by", ["agent", "webui"])
def test_concurrent_always_answers_in_one_profile_keep_both_grants(homes, sessions, monkeypatch, second_by):
    """A (the Agent's own "always") reads config.yaml in its save and is held there; B saves
    meanwhile (or waits for A, once saves are serialized); then A writes. Without
    serialization A writes what it read before B's write, and both grants are lost. A
    WebUI-held B is granted for its session only and never waits for A's save."""
    config = pytest.importorskip("hermes_cli.config")
    original_load = config.load_config
    a_read, a_resume = threading.Event(), threading.Event()

    def load_config(*args, **kwargs):
        loaded = original_load(*args, **kwargs)
        if (
            threading.current_thread().name == "always-A"
            and sys._getframe(1).f_code.co_name == "save_permanent_allowlist"
            and not a_read.is_set()
        ):
            a_read.set()
            a_resume.wait(10)
        return loaded

    monkeypatch.setattr(config, "load_config", load_config)
    sid_a, sid_b = sessions("alpha"), sessions("alpha")
    errors = []

    def answer(sid, key, by):
        try:
            if by == "agent":
                _agent_answer(homes["alpha"], sid, key)
            else:
                assert routes._resolve_approval_legacy(sid, _queue(sid, key), "always") is True
        except Exception as exc:  # surfaced below
            errors.append(repr(exc))

    a = threading.Thread(target=answer, args=(sid_a, KEY, "agent"), name="always-A", daemon=True)
    b = threading.Thread(target=answer, args=(sid_b, KEY_2, second_by), name="always-B", daemon=True)
    try:
        a.start()
        assert a_read.wait(10)
        b.start()
        b.join(1.5)  # finishes here without serialization; waits for A's save with it
        if second_by == "webui":
            assert not b.is_alive()
    finally:
        a_resume.set()
    a.join(10)
    b.join(10)

    assert not a.is_alive() and not b.is_alive()
    assert errors == []
    assert _approved_in(homes["alpha"], LATER, KEY)
    if second_by == "agent":
        assert _allowlist(homes["alpha"]) == sorted([KEY, KEY_2])
        assert _approved_in(homes["alpha"], LATER, KEY_2)
    else:
        assert _allowlist(homes["alpha"]) == [KEY]
        assert _approved_in(homes["alpha"], sid_b, KEY_2)
        assert not _approved_in(homes["alpha"], LATER, KEY_2)
    assert _allowlist(homes["beta"]) == [] and _allowlist(homes["default"]) == []
