"""WebUI-held session/always grants land only in the session's own profile.

For a raw ``_pending`` entry no Agent waits on, WebUI saves the grant itself.
It used to call the Agent's grant functions in the HTTP thread's unscoped home
(the launch profile) and save the whole in-memory permanent set, so a grant in
one profile was written into another profile's ``command_allowlist``.
``_resolve_approval_legacy`` now binds the Agent's context-local Hermes home to
the session's own profile around those calls, saves nothing when that profile
can't be established, and saves nothing for a gateway mirror whose producer is
gone. A "session" answer uses the Agent's own ``_persist_choice`` when it takes
plain keys. An "always" answer grants and saves only the keys granted now that the
Agent still holds as unsaved (session only when it can't tell), so a concurrent
answer can't write back an entry removed by hand. Every save in the
process is serialized, so two "always" answers in one profile can't overwrite
each other.

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


def test_always_in_one_profile_writes_only_that_profiles_allowlist(homes, sessions):
    alpha, beta = sessions("alpha"), sessions("beta")
    alpha_id = _queue(alpha)
    _queue(beta)

    assert routes._resolve_approval_legacy(alpha, alpha_id, "always") is True

    assert _allowlist(homes["alpha"]) == [KEY]
    assert _allowlist(homes["beta"]) == []
    assert _allowlist(homes["default"]) == []
    assert _approved_in(homes["alpha"], alpha)
    assert not _approved_in(homes["beta"], beta)
    with routes._lock:
        assert routes._pending.get(beta)  # beta's card is still waiting for its own answer


def test_session_outside_the_launch_profile_saves_into_its_own_profile(homes, sessions, monkeypatch):
    # The server runs as alpha (launch home and active profile); the session belongs to beta.
    monkeypatch.setenv("HERMES_HOME", str(homes["alpha"]))
    monkeypatch.setattr(profiles, "_active_profile", "alpha")
    beta = sessions("beta")

    assert routes._resolve_approval_legacy(beta, _queue(beta), "always") is True

    assert _allowlist(homes["beta"]) == [KEY]
    assert _allowlist(homes["alpha"]) == []


def test_always_saves_only_its_own_keys(homes, sessions):
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

    assert _allowlist(homes["default"]) == [KEY]


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


def test_session_answer_uses_the_agents_grant_and_always_saves_the_granted_keys(homes, sessions, monkeypatch):
    """Runs on every Agent. "session" goes through a keys-shaped ``_persist_choice`` (WebUI's
    own grant on v2026.9.24); "always" grants and saves only the keys granted now."""
    persisted, saved = _spy_grant_paths(monkeypatch)
    by_session, by_always = sessions("alpha"), sessions("alpha")

    assert routes._resolve_approval_legacy(by_session, _queue(by_session), "session") is True
    assert _approved_in(homes["alpha"], by_session)
    assert not _approved_in(homes["alpha"], LATER)
    assert _allowlist(homes["alpha"]) == []
    assert persisted == ([(by_session, "session", [KEY])] if _agent_takes_keys() else [])

    assert routes._resolve_approval_legacy(by_always, _queue(by_always, KEY_2), "always") is True
    assert _allowlist(homes["alpha"]) == [KEY_2]
    assert _approved_in(homes["alpha"], LATER, KEY_2)
    assert len(persisted) == (1 if _agent_takes_keys() else 0)  # not used for "always"
    assert saved == [{KEY_2}]
    assert _allowlist(homes["beta"]) == [] and _allowlist(homes["default"]) == []


def test_always_keeps_a_content_scanner_key_session_only(homes, sessions):
    """A raw entry (v2026.9.24's no-notifier ``_pending``) carries no tirith flag. As in the
    Agent's own ``_persist_choice``, an "always" grants a ``tirith:`` key for the session
    only and saves just the other keys."""
    tirith = "tirith:some_rule"
    sid = sessions("alpha")
    entry_id = _queue(sid, tirith, pattern_keys=[tirith, KEY])

    assert routes._resolve_approval_legacy(sid, entry_id, "always") is True

    assert _allowlist(homes["alpha"]) == [KEY]
    assert _approved_in(homes["alpha"], sid, tirith)
    assert not _approved_in(homes["alpha"], LATER, tirith)
    assert _approved_in(homes["alpha"], LATER, KEY)
    assert _allowlist(homes["beta"]) == [] and _allowlist(homes["default"]) == []


def test_always_after_a_failed_save_saves_only_its_own_key(homes, sessions, monkeypatch):
    """Fails closed: a grant whose save failed is not saved by the next "always" either.
    That save writes only its own key and the Agent replaces the profile's in-memory set
    with what it wrote, so the earlier command is asked again. Nothing else is granted."""
    beta_only, root_only = f"beta key {uuid.uuid4().hex[:6]}", f"root key {uuid.uuid4().hex[:6]}"
    with _in_home(homes["beta"]):
        agent_approval.approve_permanent(beta_only)
    agent_approval.approve_permanent(root_only)  # the unscoped launch profile's set
    saves = _fail_first_save(monkeypatch)
    first, second = sessions("alpha"), sessions("alpha")

    assert routes._resolve_approval_legacy(first, _queue(first), "always") is True
    assert _allowlist(homes["alpha"]) == []
    assert routes._resolve_approval_legacy(second, _queue(second, KEY_2), "always") is True

    assert len(saves) == 2
    assert _allowlist(homes["alpha"]) == [KEY_2]
    assert not _approved_in(homes["alpha"], LATER, KEY)
    assert _approved_in(homes["alpha"], LATER, KEY_2)
    assert not _approved_in(homes["alpha"], LATER, beta_only)
    assert _allowlist(homes["beta"]) == []
    assert _allowlist(homes["default"]) == []
    assert not _approved_in(homes["beta"], LATER, KEY)


@pytest.mark.parametrize("path", ["persist_choice", "fallback"])
def test_always_does_not_restore_an_entry_removed_by_hand(homes, sessions, monkeypatch, path):
    """``persist_choice``: this Agent's binding as is; ``fallback``: no ``_persist_choice``.
    Either way "always" saves the granted keys, not through ``_persist_choice``."""
    if path == "fallback":
        monkeypatch.setattr(routes, "persist_grant_choice", None)
    persisted, saved = _spy_grant_paths(monkeypatch)
    first, second = sessions("alpha"), sessions("alpha")
    assert routes._resolve_approval_legacy(first, _queue(first), "always") is True
    assert _allowlist(homes["alpha"]) == [KEY]

    # The operator withdraws it in config.yaml.
    (homes["alpha"] / "config.yaml").write_text(
        "approvals:\n  mode: manual\ncommand_allowlist: []\n", encoding="utf-8"
    )
    assert routes._resolve_approval_legacy(second, _queue(second, KEY_2), "always") is True

    assert _allowlist(homes["alpha"]) == [KEY_2]
    assert not _approved_in(homes["alpha"], LATER, KEY)
    assert _approved_in(homes["alpha"], LATER, KEY_2)
    assert persisted == [] and saved == [{KEY}, {KEY_2}]


def test_overlapping_always_answers_do_not_restore_an_entry_removed_by_hand(homes, sessions, monkeypatch):
    """A and B answer "always" at once and both reach the save; A saves X, the operator then
    removes X from config.yaml (the next check reads that), and B saves. B passes only its
    own key, so X stays removed. Passing a copy of the in-memory set taken before A's save
    wrote X back."""
    serialized_save = agent_approval.save_permanent_allowlist
    held = {"answer-A": (threading.Event(), threading.Event()), "answer-B": (threading.Event(), threading.Event())}
    passed = {}

    def held_save(patterns):
        name = threading.current_thread().name
        passed[name] = sorted(patterns)
        if name in held:
            reached, resume = held[name]
            reached.set()
            resume.wait(10)
        return serialized_save(patterns)

    monkeypatch.setattr(agent_approval, "save_permanent_allowlist", held_save)  # the Agent's own path
    monkeypatch.setattr(routes, "save_permanent_allowlist", held_save)
    sid_a, sid_b = sessions("alpha"), sessions("alpha")
    errors = []

    def answer(sid, key):
        try:
            assert routes._resolve_approval_legacy(sid, _queue(sid, key), "always") is True
        except Exception as exc:  # surfaced below
            errors.append(repr(exc))

    a = threading.Thread(target=answer, args=(sid_a, KEY), name="answer-A", daemon=True)
    b = threading.Thread(target=answer, args=(sid_b, KEY_2), name="answer-B", daemon=True)
    try:
        a.start()
        assert held["answer-A"][0].wait(10)
        b.start()
        assert held["answer-B"][0].wait(10)
        held["answer-A"][1].set()
        a.join(10)
        assert not a.is_alive() and _allowlist(homes["alpha"]) == [KEY]

        (homes["alpha"] / "config.yaml").write_text(
            "approvals:\n  mode: manual\ncommand_allowlist: []\n", encoding="utf-8"
        )
        if hasattr(agent_approval, "_sync_permanent_from_disk"):  # v2026.9.24 reads it at the save
            assert not _approved_in(homes["alpha"], LATER, KEY)
    finally:
        for _reached, resume in held.values():
            resume.set()
    b.join(10)

    assert not b.is_alive() and errors == []
    assert passed["answer-B"] == [KEY_2]
    assert _allowlist(homes["alpha"]) == [KEY_2]
    assert not _approved_in(homes["alpha"], LATER, KEY)
    assert _approved_in(homes["alpha"], LATER, KEY_2)
    assert _allowlist(homes["beta"]) == [] and _allowlist(homes["default"]) == []


def test_always_for_a_key_removed_by_hand_while_it_is_granted_stays_removed(homes, sessions, monkeypatch):
    """Two queued approvals for one key. The first "always" saves it. The second is held
    right after its approve_permanent while the operator removes the key from
    command_allowlist and a check reads that; its save must not write the key back."""
    first, second = sessions("alpha"), sessions("alpha")
    first_id, second_id = _queue(first), _queue(second)
    assert routes._resolve_approval_legacy(first, first_id, "always") is True
    assert _allowlist(homes["alpha"]) == [KEY]

    granted, resume = threading.Event(), threading.Event()
    real_approve_permanent = routes.approve_permanent

    def held_approve_permanent(key):
        real_approve_permanent(key)
        granted.set()
        resume.wait(10)

    monkeypatch.setattr(routes, "approve_permanent", held_approve_permanent)
    errors = []

    def answer():
        try:
            assert routes._resolve_approval_legacy(second, second_id, "always") is True
        except Exception as exc:  # surfaced below
            errors.append(repr(exc))

    thread = threading.Thread(target=answer, daemon=True)
    try:
        thread.start()
        assert granted.wait(10)
        (homes["alpha"] / "config.yaml").write_text(
            "approvals:\n  mode: manual\ncommand_allowlist: []\n", encoding="utf-8"
        )
        with _in_home(homes["alpha"]):
            if not hasattr(agent_approval, "_sync_permanent_from_disk"):  # v2026.9.24 reads it on load
                agent_approval.load_permanent_allowlist()
            assert not agent_approval.is_approved(LATER, KEY)  # the revocation is in effect
    finally:
        resume.set()
    thread.join(10)

    assert not thread.is_alive() and errors == []
    assert _allowlist(homes["alpha"]) == []
    assert not _approved_in(homes["alpha"], LATER, KEY)
    assert _allowlist(homes["beta"]) == [] and _allowlist(homes["default"]) == []


def test_a_key_removed_by_hand_while_it_is_granted_is_dropped_from_memory_too(homes, sessions, monkeypatch):
    """As above, but nothing re-reads command_allowlist before the second save. An Agent
    that only reconciles its in-memory set at a save (v2026.9.24) must still stop
    honouring the key once the second answer is done, though there is nothing to add."""
    first, second = sessions("alpha"), sessions("alpha")
    first_id, second_id = _queue(first), _queue(second)
    assert routes._resolve_approval_legacy(first, first_id, "always") is True
    assert _allowlist(homes["alpha"]) == [KEY]

    granted, resume = threading.Event(), threading.Event()
    real_approve_permanent = routes.approve_permanent

    def held_approve_permanent(key):
        real_approve_permanent(key)
        granted.set()
        resume.wait(10)

    monkeypatch.setattr(routes, "approve_permanent", held_approve_permanent)
    errors = []

    def answer():
        try:
            assert routes._resolve_approval_legacy(second, second_id, "always") is True
        except Exception as exc:  # surfaced below
            errors.append(repr(exc))

    thread = threading.Thread(target=answer, daemon=True)
    try:
        thread.start()
        assert granted.wait(10)
        (homes["alpha"] / "config.yaml").write_text(
            "approvals:\n  mode: manual\ncommand_allowlist: []\n", encoding="utf-8"
        )
    finally:
        resume.set()
    thread.join(10)

    assert not thread.is_alive() and errors == []
    assert _allowlist(homes["alpha"]) == []
    data = yaml.safe_load((homes["alpha"] / "config.yaml").read_text(encoding="utf-8"))
    assert data["approvals"] == {"mode": "manual"}  # the rest of the file is kept
    assert not _approved_in(homes["alpha"], LATER, KEY)
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


def _agent_answer(home, sid, key):
    """The Agent's own grant for an approval it waited on, in its own thread."""
    with _in_home(home):
        if _agent_takes_keys():
            agent_approval._persist_choice(sid, "always", [key])
        else:  # v2026.9.24: (key, description, is_tirith)
            agent_approval._persist_choice(sid, "always", [(key, key, False)])


@pytest.mark.parametrize("answered_by", ["agent", "webui"])
def test_concurrent_always_answers_in_one_profile_keep_both_grants(homes, sessions, monkeypatch, answered_by):
    """A reads config.yaml in its save and is held there; B saves meanwhile (or waits for
    A, once saves are serialized); then A writes. Without serialization A writes what it
    read before B's write, and both grants are lost."""
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

    def answer(sid, key):
        try:
            if answered_by == "agent":
                _agent_answer(homes["alpha"], sid, key)
            else:
                assert routes._resolve_approval_legacy(sid, _queue(sid, key), "always") is True
        except Exception as exc:  # surfaced below
            errors.append(repr(exc))

    a = threading.Thread(target=answer, args=(sid_a, KEY), name="always-A", daemon=True)
    b = threading.Thread(target=answer, args=(sid_b, KEY_2), name="always-B", daemon=True)
    try:
        a.start()
        assert a_read.wait(10)
        b.start()
        b.join(1.5)  # finishes here without serialization; waits for A's save with it
    finally:
        a_resume.set()
    a.join(10)
    b.join(10)

    assert not a.is_alive() and not b.is_alive()
    assert errors == []
    assert _allowlist(homes["alpha"]) == sorted([KEY, KEY_2])
    assert _approved_in(homes["alpha"], LATER, KEY)
    assert _approved_in(homes["alpha"], LATER, KEY_2)
    assert _allowlist(homes["beta"]) == [] and _allowlist(homes["default"]) == []
