"""WebUI-held session/always grants land only in the session's own profile.

For a raw ``_pending`` entry no Agent waits on, WebUI saves the grant itself.
It used to call the Agent's grant functions in the HTTP thread's unscoped home
(the launch profile) and save the whole in-memory permanent set, so a grant in
one profile was written into another profile's ``command_allowlist``.
``_resolve_approval_legacy`` now binds the Agent's context-local Hermes home to
the session's own profile around those calls, saves nothing when that profile
can't be established, and saves nothing for a gateway mirror whose producer is
gone. With an Agent whose ``_persist_choice`` takes plain keys, the grant and
save are the Agent's own (it saves what it granted beyond its last read of the
file, so a grant whose save failed is not lost); otherwise WebUI saves the keys
just granted. Every save in the process is serialized, so two "always" answers
in one profile can't overwrite each other.

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


def test_session_and_always_answers_save_through_this_agents_path(homes, sessions, monkeypatch):
    """Runs on every Agent: a keys-shaped ``_persist_choice`` saves for WebUI, otherwise
    (v2026.9.24) WebUI's own calls save the granted keys."""
    saved = []
    webui_save = routes.save_permanent_allowlist
    monkeypatch.setattr(routes, "save_permanent_allowlist", lambda keys: saved.append(set(keys)) or webui_save(keys))
    by_session, by_always = sessions("alpha"), sessions("alpha")

    assert routes._resolve_approval_legacy(by_session, _queue(by_session), "session") is True
    assert _approved_in(homes["alpha"], by_session)
    assert not _approved_in(homes["alpha"], LATER)
    assert _allowlist(homes["alpha"]) == []

    assert routes._resolve_approval_legacy(by_always, _queue(by_always, KEY_2), "always") is True
    assert _allowlist(homes["alpha"]) == [KEY_2]
    assert _approved_in(homes["alpha"], LATER, KEY_2)
    assert saved == ([] if _agent_takes_keys() else [{KEY_2}])
    assert _allowlist(homes["beta"]) == [] and _allowlist(homes["default"]) == []


def test_always_after_a_failed_save_keeps_the_earlier_grant(homes, sessions, monkeypatch):
    """A grant whose save failed stays granted in memory. The next "always" in the same
    profile saves it as well (the Agent saves everything it granted beyond its last read
    of the file), instead of saving only its own key and replacing the in-memory set with
    that. Other profiles' unsaved grants stay where they are."""
    if route_approvals.persist_grant_choice is None:
        pytest.skip("this Agent's _persist_choice takes tuples; WebUI saves the granted keys only")
    beta_only, root_only = f"beta key {uuid.uuid4().hex[:6]}", f"root key {uuid.uuid4().hex[:6]}"
    with _in_home(homes["beta"]):
        agent_approval.approve_permanent(beta_only)
    agent_approval.approve_permanent(root_only)  # the unscoped launch profile's set
    saves = _fail_first_save(monkeypatch)
    first, second = sessions("alpha"), sessions("alpha")

    assert routes._resolve_approval_legacy(first, _queue(first), "always") is True
    assert _allowlist(homes["alpha"]) == []
    assert _approved_in(homes["alpha"], LATER)
    assert routes._resolve_approval_legacy(second, _queue(second, KEY_2), "always") is True

    assert len(saves) == 2
    assert _allowlist(homes["alpha"]) == sorted([KEY, KEY_2])
    assert _approved_in(homes["alpha"], LATER, KEY)
    assert _approved_in(homes["alpha"], LATER, KEY_2)
    assert _allowlist(homes["beta"]) == []
    assert _allowlist(homes["default"]) == []
    assert not _approved_in(homes["beta"], LATER, KEY)
    assert _approved_in(homes["beta"], LATER, beta_only)


def test_without_a_keys_persist_choice_always_saves_its_own_keys(homes, sessions, monkeypatch):
    """The fallback (no ``_persist_choice``, or the tuple shape): the keys granted now."""
    monkeypatch.setattr(routes, "persist_grant_choice", None)
    _fail_first_save(monkeypatch)
    first, second = sessions("alpha"), sessions("alpha")

    assert routes._resolve_approval_legacy(first, _queue(first), "always") is True
    assert routes._resolve_approval_legacy(second, _queue(second, KEY_2), "always") is True

    assert _allowlist(homes["alpha"]) == [KEY_2]
    assert _approved_in(homes["alpha"], LATER, KEY_2)
    assert _allowlist(homes["beta"]) == []
    assert _allowlist(homes["default"]) == []


@pytest.mark.parametrize("path", ["persist_choice", "fallback"])
def test_always_does_not_restore_an_entry_removed_by_hand(homes, sessions, monkeypatch, path):
    if path == "fallback":
        monkeypatch.setattr(routes, "persist_grant_choice", None)
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
