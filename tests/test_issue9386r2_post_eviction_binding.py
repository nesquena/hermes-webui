"""A post-eviction environment must not be adopted by another backend (#9386 review 2).

The previous round bound the recorded identity to the exact environment object, so a
replacement installed between inspection and the environment lock no longer inherits the
requesting backend's identity. That round also dropped the record on a successful removal.

Dropping the record opened the ordinary eviction -> recreation lifecycle: after the SSH
environment for host A is evicted by a request for host B, the environment created for B
has no recorded identity, so the next request for A reaches the startup-style first-sight
branch and adopts B's object as A's own backend.

Contract under test:

* An eviction leaves a pending expectation for the backend that displaced the previous
  environment instead of an empty record. The environment created under that key
  afterwards is bound to that backend, so the ordinary path keeps working without churn.
* An object that appears while the pending expectation belongs to ANOTHER backend is
  unverified: the requesting backend must invalidate it instead of adopting it.
* The repaired rules from the previous round stay: an unverified concurrent replacement is
  never authorized for reuse, the exact inspected object is removed under the environment
  lock, cleanup runs once outside the lock, and an unchanged object with an unchanged
  identity is retained.
"""

import threading
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _reset_identity_record():
    import api.streaming as streaming

    record = getattr(streaming, "_TERMINAL_ENV_IDENTITY_BY_KEY", None)
    if isinstance(record, dict):
        record.clear()
    yield
    if isinstance(record, dict):
        record.clear()


class _FakeEnv:
    """Terminal environment stand-in that records its teardown calls."""

    def __init__(self, label="env"):
        self.label = label
        self.cleanup_calls = 0
        self.stop_calls = 0
        self.terminate_calls = 0

    def cleanup(self):
        self.cleanup_calls += 1

    def stop(self):
        self.stop_calls += 1

    def terminate(self):
        self.terminate_calls += 1


class _SwapOnEnterLock:
    """Environment lock that installs a replacement right after acquisition."""

    def __init__(self, environments, replacement, key="default"):
        self._inner = threading.Lock()
        self._environments = environments
        self._replacement = replacement
        self._key = key
        self._pending = True

    def __enter__(self):
        self._inner.acquire()
        if self._pending:
            self._pending = False
            self._environments[self._key] = self._replacement
        return self

    def __exit__(self, exc_type, exc, tb):
        self._inner.release()
        return False

    def locked(self):
        return self._inner.locked()


def _identity(**env):
    import api.streaming as streaming

    return streaming._terminal_backend_identity(env)


def _install(monkeypatch, environments, lock=None):
    import api.streaming as streaming

    mod = SimpleNamespace(
        _active_environments=environments,
        _env_lock=lock if lock is not None else threading.Lock(),
    )
    monkeypatch.setattr(streaming, "_terminal_tool_module", lambda: mod)
    return mod


def _evict_host_a_for_host_b(monkeypatch):
    """Baseline host A, then evict it with a request for host B.

    Returns the identities so each case can drive the recreation step itself.
    """
    import api.streaming as streaming

    env_a = _FakeEnv("ssh-a")
    environments = {"default": env_a}
    _install(monkeypatch, environments)

    identity_a = _identity(TERMINAL_ENV="ssh", TERMINAL_SSH_HOST="host-a")
    identity_b = _identity(TERMINAL_ENV="ssh", TERMINAL_SSH_HOST="host-b")

    streaming._invalidate_stale_terminal_environments(identity_a)
    streaming._invalidate_stale_terminal_environments(identity_b)

    assert "default" not in environments
    assert env_a.cleanup_calls == 1
    return streaming, environments, identity_a, identity_b


def test_recreated_environment_is_invalidated_for_the_previous_backend(monkeypatch):
    """Decisive case: eviction -> B recreation -> request for A."""
    streaming, environments, identity_a, _identity_b = _evict_host_a_for_host_b(monkeypatch)

    created_b = _FakeEnv("ssh-b-created")
    environments["default"] = created_b

    streaming._invalidate_stale_terminal_environments(identity_a)

    assert "default" not in environments, (
        "backend B recreated after eviction was adopted as A and retained"
    )
    assert created_b.cleanup_calls == 1


def test_recreated_environment_is_bound_to_the_backend_that_displaced_the_old_one(monkeypatch):
    """Control: the recreated environment is kept for the backend that created it."""
    streaming, environments, _identity_a, identity_b = _evict_host_a_for_host_b(monkeypatch)

    created_b = _FakeEnv("ssh-b-created")
    environments["default"] = created_b

    streaming._invalidate_stale_terminal_environments(identity_b)
    streaming._invalidate_stale_terminal_environments(identity_b)

    assert environments["default"] is created_b
    assert created_b.cleanup_calls == 0
    assert created_b.stop_calls == 0
    assert created_b.terminate_calls == 0


def test_requesting_backend_binds_its_own_new_environment_without_churn(monkeypatch):
    """Control: the backend whose turn starts on the empty cache binds its own creation."""
    streaming, environments, identity_a, _identity_b = _evict_host_a_for_host_b(monkeypatch)

    # A turn for host A always runs the helper before the terminal tool creates anything.
    streaming._invalidate_stale_terminal_environments(identity_a)
    assert "default" not in environments

    created_a = _FakeEnv("ssh-a-created")
    environments["default"] = created_a

    streaming._invalidate_stale_terminal_environments(identity_a)
    streaming._invalidate_stale_terminal_environments(identity_a)

    assert environments["default"] is created_a
    assert created_a.cleanup_calls == 0


def test_object_created_after_an_absent_entry_is_not_adopted_by_another_backend(monkeypatch):
    """The absent-entry path must not become a first-sight adoption under another backend."""
    import api.streaming as streaming

    environments = {}
    _install(monkeypatch, environments)

    identity_a = _identity(TERMINAL_ENV="ssh", TERMINAL_SSH_HOST="host-a")
    identity_b = _identity(TERMINAL_ENV="ssh", TERMINAL_SSH_HOST="host-b")

    streaming._invalidate_stale_terminal_environments(identity_a)

    created_b = _FakeEnv("ssh-b-created")
    environments["default"] = created_b

    streaming._invalidate_stale_terminal_environments(identity_b)

    assert "default" not in environments, (
        "backend B created after an absent entry was adopted as B without verification"
    )
    assert created_b.cleanup_calls == 1


def test_unverified_replacement_is_still_never_authorized_for_reuse(monkeypatch):
    """Preserved rule: a replacement installed before the lock is not a verified object."""
    import api.streaming as streaming

    inspected = _FakeEnv("stale-old-host")
    replacement = _FakeEnv("unverified-different-host")
    environments = {"default": inspected}
    _install(monkeypatch, environments, _SwapOnEnterLock(environments, replacement))

    old_identity = _identity(TERMINAL_ENV="ssh", TERMINAL_SSH_HOST="old-host")
    new_identity = _identity(TERMINAL_ENV="ssh", TERMINAL_SSH_HOST="new-host")

    streaming._invalidate_stale_terminal_environments(old_identity)
    streaming._invalidate_stale_terminal_environments(new_identity)

    assert environments["default"] is replacement
    assert inspected.cleanup_calls == 0
    assert replacement.cleanup_calls == 0

    streaming._invalidate_stale_terminal_environments(new_identity)

    assert "default" not in environments
    assert replacement.cleanup_calls == 1
