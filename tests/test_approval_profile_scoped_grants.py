"""WebUI-held session/always grants land only in the session's own profile.

For a raw ``_pending`` entry no Agent waits on, WebUI saves the grant itself.
It used to call the Agent's grant functions in the HTTP thread's unscoped home
(the launch profile) and save the whole in-memory permanent set, so a grant in
one profile was written into another profile's ``command_allowlist``.
``_resolve_approval_legacy`` now binds the Agent's context-local Hermes home to
the session's own profile around those calls, saves only the keys just granted,
saves nothing when that profile can't be established, and saves nothing for a
gateway mirror whose producer is gone.

These tests use the real Agent ``tools.approval`` in process and read each
profile's ``config.yaml``.
"""
from __future__ import annotations

import logging
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
