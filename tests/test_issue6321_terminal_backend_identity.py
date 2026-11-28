"""Terminal backend identity: one fingerprint for both caches (#6175 review, PR #6321).

The terminal environment cache (``_active_environments`` in the Agent's terminal
tool module) is process-global and outlives every ``AIAgent``. Rebuilding the
agent therefore does not rebuild the backend: a same-type switch (SSH host A->B,
Docker image A->B) silently keeps executing on the old host/container.

The contract under test:

* ``_terminal_backend_identity()`` is the single fingerprint of the full
  terminal identity (backend type plus every same-type endpoint/image field) and
  the agent cache signature embeds it, so both caches agree on what "the same
  backend" means.
* ``_invalidate_stale_terminal_environments()`` evicts the cached environment
  when that fingerprint changes, removes the exact inspected entry under the
  Agent's ``_env_lock``, preserves a newer replacement installed in between,
  cleans the removed environment exactly once and outside the lock, and never
  mutates an environment it did not remove.
* An unavailable runtime env (``None``) never evicts: unknown identity must not
  tear down a live backend.
"""

import threading
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _reset_identity_record(monkeypatch):
    import api.streaming as streaming

    record = getattr(streaming, "_TERMINAL_ENV_IDENTITY_BY_KEY", None)
    if isinstance(record, dict):
        record.clear()
    yield
    if isinstance(record, dict):
        record.clear()


class _FakeEnv:
    """Stand-in for a terminal environment: records its teardown calls."""

    def __init__(self, label="env", lock=None):
        self.label = label
        self.cleanup_calls = 0
        self.stop_calls = 0
        self.terminate_calls = 0
        self.lock_held_during_cleanup = None
        self._lock = lock

    def cleanup(self):
        self.cleanup_calls += 1
        if self._lock is not None:
            self.lock_held_during_cleanup = self._lock.locked()

    def stop(self):
        self.stop_calls += 1

    def terminate(self):
        self.terminate_calls += 1


def _fake_terminal_tool(env, key="default", lock=None):
    return SimpleNamespace(
        _active_environments={key: env},
        _env_lock=lock if lock is not None else threading.Lock(),
    )


def _identity(**env):
    import api.streaming as streaming

    return streaming._terminal_backend_identity(env)


def _signature(env):
    import api.streaming as streaming

    return streaming._compute_agent_cache_signature(
        "model-x",
        None,
        None,
        None,
        {},
        prefill_context={},
        safe_profile_runtime_env=dict(env),
    )


# --------------------------------------------------------------------------
# One fingerprint for both caches
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "field,old_value,new_value",
    [
        ("TERMINAL_SSH_HOST", "old-host", "new-host"),
        ("TERMINAL_SSH_USER", "root", "deploy"),
        ("TERMINAL_SSH_PORT", "22", "2222"),
        ("TERMINAL_SSH_KEY", "/keys/a", "/keys/b"),
    ],
)
def test_signature_changes_with_the_ssh_endpoint_identity(field, old_value, new_value):
    base = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "old-host"}
    old = dict(base, **{field: old_value})
    new = dict(base, **{field: new_value})
    assert _signature(old) != _signature(new)


@pytest.mark.parametrize(
    "field",
    [
        "TERMINAL_DOCKER_IMAGE",
        "TERMINAL_SINGULARITY_IMAGE",
        "TERMINAL_MODAL_IMAGE",
        "TERMINAL_DAYTONA_IMAGE",
    ],
)
def test_signature_changes_with_the_container_image(field):
    old = {"TERMINAL_ENV": "docker", field: "image-a"}
    new = {"TERMINAL_ENV": "docker", field: "image-b"}
    assert _signature(old) != _signature(new)


def test_signature_is_stable_for_an_unchanged_identity():
    env = {
        "TERMINAL_ENV": "ssh",
        "TERMINAL_SSH_HOST": "host",
        "TERMINAL_SSH_PORT": "22",
        "TERMINAL_DOCKER_IMAGE": "img",
    }
    assert _signature(env) == _signature(dict(env))


def test_identity_covers_the_backend_type_alone():
    assert _identity(TERMINAL_ENV="ssh") != _identity(TERMINAL_ENV="local")
    assert _identity(TERMINAL_ENV="ssh") != _identity()


# --------------------------------------------------------------------------
# Invalidation honors that fingerprint
# --------------------------------------------------------------------------

def test_backend_type_change_evicts_the_cached_environment(monkeypatch):
    import api.streaming as streaming

    env = _FakeEnv("ssh")
    mod = _fake_terminal_tool(env)
    monkeypatch.setattr(streaming, "_terminal_tool_module", lambda: mod)

    streaming._invalidate_stale_terminal_environments(
        _identity(TERMINAL_ENV="ssh", TERMINAL_SSH_HOST="host")
    )
    assert mod._active_environments["default"] is env

    streaming._invalidate_stale_terminal_environments(_identity(TERMINAL_ENV="local"))
    assert "default" not in mod._active_environments
    assert env.cleanup_calls == 1


def test_same_type_ssh_endpoint_change_evicts_the_cached_environment(monkeypatch):
    import api.streaming as streaming

    env = _FakeEnv("ssh-old")
    mod = _fake_terminal_tool(env)
    monkeypatch.setattr(streaming, "_terminal_tool_module", lambda: mod)

    streaming._invalidate_stale_terminal_environments(
        _identity(TERMINAL_ENV="ssh", TERMINAL_SSH_HOST="old-host")
    )
    streaming._invalidate_stale_terminal_environments(
        _identity(TERMINAL_ENV="ssh", TERMINAL_SSH_HOST="new-host")
    )
    assert "default" not in mod._active_environments
    assert env.cleanup_calls == 1


def test_container_image_change_evicts_the_cached_environment(monkeypatch):
    import api.streaming as streaming

    env = _FakeEnv("docker-a")
    mod = _fake_terminal_tool(env)
    monkeypatch.setattr(streaming, "_terminal_tool_module", lambda: mod)

    streaming._invalidate_stale_terminal_environments(
        _identity(TERMINAL_ENV="docker", TERMINAL_DOCKER_IMAGE="image-a")
    )
    streaming._invalidate_stale_terminal_environments(
        _identity(TERMINAL_ENV="docker", TERMINAL_DOCKER_IMAGE="image-b")
    )
    assert "default" not in mod._active_environments
    assert env.cleanup_calls == 1


def test_unchanged_identity_reuses_the_cached_environment(monkeypatch):
    import api.streaming as streaming

    env = _FakeEnv("ssh")
    mod = _fake_terminal_tool(env)
    monkeypatch.setattr(streaming, "_terminal_tool_module", lambda: mod)

    identity = _identity(TERMINAL_ENV="ssh", TERMINAL_SSH_HOST="host", TERMINAL_SSH_PORT="22")
    streaming._invalidate_stale_terminal_environments(identity)
    streaming._invalidate_stale_terminal_environments(identity)
    streaming._invalidate_stale_terminal_environments(
        _identity(TERMINAL_ENV="ssh", TERMINAL_SSH_PORT="22", TERMINAL_SSH_HOST="host")
    )

    assert mod._active_environments["default"] is env
    assert env.cleanup_calls == 0
    assert env.stop_calls == 0
    assert env.terminate_calls == 0


def test_eviction_cleans_the_environment_exactly_once_outside_the_lock(monkeypatch):
    import api.streaming as streaming

    lock = threading.Lock()
    env = _FakeEnv("ssh", lock=lock)
    mod = _fake_terminal_tool(env, lock=lock)
    monkeypatch.setattr(streaming, "_terminal_tool_module", lambda: mod)

    streaming._invalidate_stale_terminal_environments(
        _identity(TERMINAL_ENV="ssh", TERMINAL_SSH_HOST="old-host")
    )
    streaming._invalidate_stale_terminal_environments(
        _identity(TERMINAL_ENV="ssh", TERMINAL_SSH_HOST="new-host")
    )
    # A later call must not clean the same object again.
    streaming._invalidate_stale_terminal_environments(
        _identity(TERMINAL_ENV="ssh", TERMINAL_SSH_HOST="newer-host")
    )

    assert env.cleanup_calls == 1
    assert env.stop_calls == 0
    assert env.terminate_calls == 0
    assert env.lock_held_during_cleanup is False


class _SwapOnEnterLock:
    """Environment lock that installs a replacement right after acquisition.

    It reproduces the window between inspecting ``_active_environments`` and
    removing the inspected entry: another turn installed a newer environment
    under the same key before the eviction got the lock.
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


def test_replacement_installed_between_inspection_and_invalidation_survives(monkeypatch):
    import api.streaming as streaming

    inspected = _FakeEnv("stale")
    replacement = _FakeEnv("replacement")
    environments = {"default": inspected}
    mod = SimpleNamespace(
        _active_environments=environments,
        _env_lock=_SwapOnEnterLock(environments, replacement),
    )
    monkeypatch.setattr(streaming, "_terminal_tool_module", lambda: mod)

    streaming._invalidate_stale_terminal_environments(
        _identity(TERMINAL_ENV="ssh", TERMINAL_SSH_HOST="old-host")
    )
    streaming._invalidate_stale_terminal_environments(
        _identity(TERMINAL_ENV="ssh", TERMINAL_SSH_HOST="new-host")
    )

    assert environments["default"] is replacement
    assert replacement.cleanup_calls == 0
    assert inspected.cleanup_calls == 0


def test_missing_runtime_environment_never_evicts(monkeypatch):
    import api.streaming as streaming

    inspected = _FakeEnv("ssh")
    mod = _fake_terminal_tool(inspected)
    monkeypatch.setattr(streaming, "_terminal_tool_module", lambda: mod)

    streaming._invalidate_stale_terminal_environments(
        _identity(TERMINAL_ENV="ssh", TERMINAL_SSH_HOST="old-host")
    )
    streaming._invalidate_stale_terminal_environments(None)

    assert mod._active_environments["default"] is inspected
    assert inspected.cleanup_calls == 0


def test_absent_terminal_tool_is_not_an_error(monkeypatch):
    import api.streaming as streaming

    monkeypatch.setattr(streaming, "_terminal_tool_module", lambda: None)
    assert streaming._invalidate_stale_terminal_environments(
        _identity(TERMINAL_ENV="ssh", TERMINAL_SSH_HOST="host")
    ) == []
