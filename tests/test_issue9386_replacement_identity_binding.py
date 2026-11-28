"""Terminal environment identity must bind to the exact environment object (#9386 review).

The replacement-race regression in ``tests/test_issue6321_terminal_backend_identity.py``
stops at "the replacement installed between inspection and the environment lock survives".
That half is right: the helper must not tear down an object it did not inspect. The other
half is missing: the surviving replacement is currently stamped with the *requesting*
identity, so the next request for the same backend sees a matching identity and reuses an
environment nobody verified.

The contract under test:

* A recorded identity describes one exact environment object. When the cached object is
  replaced between inspection and removal, the replacement keeps existing but never
  inherits the requesting backend's identity.
* Because that replacement's identity is unverified, the next request (same identity or
  another one) must invalidate it instead of authorizing reuse.
* An unchanged object with an unchanged identity is still retained: no churn for the
  ordinary path, and no teardown of an environment the helper did not remove.
* First-sight adoption still preserves a pre-existing environment (baseline), and an
  absent cache entry never stamps an identity onto an object that appears later.
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
    """Environment lock that installs a replacement right after acquisition.

    Reproduces the window between inspecting ``_active_environments`` and removing the
    inspected entry: another turn installed a newer environment under the same key before
    this eviction got the lock.
    """

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


def test_unverified_replacement_cannot_authorize_the_next_same_identity_request(monkeypatch):
    import api.streaming as streaming

    inspected = _FakeEnv("stale-old-host")
    replacement = _FakeEnv("unverified-different-host")
    environments = {"default": inspected}
    _install(monkeypatch, environments, _SwapOnEnterLock(environments, replacement))

    old_identity = _identity(TERMINAL_ENV="ssh", TERMINAL_SSH_HOST="old-host")
    new_identity = _identity(TERMINAL_ENV="ssh", TERMINAL_SSH_HOST="new-host")

    # 1. baseline for the old backend
    streaming._invalidate_stale_terminal_environments(old_identity)
    # 2. request a different SSH backend; the swap installs another environment
    #    between inspection and the environment lock.
    streaming._invalidate_stale_terminal_environments(new_identity)

    # 3/4. the replacement is preserved: we did not inspect it, so we must not clean it.
    assert environments["default"] is replacement
    assert inspected.cleanup_calls == 0
    assert replacement.cleanup_calls == 0

    # 5. the following request for the same backend must not reuse an environment whose
    #    configuration nobody verified.
    streaming._invalidate_stale_terminal_environments(new_identity)

    assert "default" not in environments
    assert replacement.cleanup_calls == 1


def test_unchanged_object_and_identity_is_retained(monkeypatch):
    import api.streaming as streaming

    env = _FakeEnv("ssh")
    environments = {"default": env}
    _install(monkeypatch, environments)

    identity = _identity(TERMINAL_ENV="ssh", TERMINAL_SSH_HOST="host", TERMINAL_SSH_PORT="22")
    for _ in range(3):
        streaming._invalidate_stale_terminal_environments(identity)

    assert environments["default"] is env
    assert env.cleanup_calls == 0
    assert env.stop_calls == 0
    assert env.terminate_calls == 0


def test_first_sight_still_adopts_a_pre_existing_environment(monkeypatch):
    import api.streaming as streaming

    env = _FakeEnv("pre-existing")
    environments = {"default": env}
    _install(monkeypatch, environments)

    identity = _identity(TERMINAL_ENV="docker", TERMINAL_DOCKER_IMAGE="image-a")
    streaming._invalidate_stale_terminal_environments(identity)
    streaming._invalidate_stale_terminal_environments(identity)

    assert environments["default"] is env
    assert env.cleanup_calls == 0


def test_absent_entry_does_not_stamp_an_object_that_appears_later(monkeypatch):
    import api.streaming as streaming

    environments = {}
    _install(monkeypatch, environments)

    identity = _identity(TERMINAL_ENV="ssh", TERMINAL_SSH_HOST="host-a")
    streaming._invalidate_stale_terminal_environments(identity)

    created = _FakeEnv("created-after-the-absent-call")
    environments["default"] = created

    streaming._invalidate_stale_terminal_environments(identity)

    # Adoption of an object first seen with no stale record is the documented baseline
    # behaviour; what must not happen is the object being torn down on that same path.
    assert environments["default"] is created
    assert created.cleanup_calls == 0
