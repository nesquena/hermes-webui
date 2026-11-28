"""A newly appeared terminal environment is adopted only on creator evidence (#9386 review 3).

The previous round left a pending expectation ``(None, requesting_identity)`` on an empty
cache slot and adopted whatever object appeared under that key when the next request carried
the same identity. That pending expectation is a *request*, not evidence about who created the
object, so the review's schedule still stamps one backend's environment with another
backend's identity:

1. the cache is empty and backend A observes it;
2. the cache is still empty and backend B observes it;
3. A's delayed creation publishes A's environment under the key;
4. B's check adopts A's object as B's own (no eviction, no cleanup).

The post-eviction mirror is the same defect: evicting A's environment for B, having A observe
the empty slot, and then publishing B's delayed creation makes the next A check adopt B's
object as A's.

Contract under test:

* An environment object carries its creation evidence: the backend tag the agent stamps on
  every object it builds plus that backend's own endpoint fields (SSH host/user/port/key,
  container image). Adoption is decided from that evidence, never from who asked first.
* Evidence that contradicts the requester invalidates the object; evidence that agrees binds
  it without churn; evidence that the object cannot prove falls back to the pending rule.
* A requester that leaves a field unset (the backend applies its own default) is not a
  mismatch: leaving a field unset must not cause an environment to be torn down every turn.
* The repaired rules from the earlier rounds stay: an unchanged object with an unchanged
  identity is retained, an unverified concurrent replacement is never authorized for reuse,
  the exact inspected object is removed under the environment lock, and cleanup runs once
  outside the lock.
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
    """Terminal environment stand-in carrying the creation evidence a real backend exposes."""

    def __init__(self, label="env", *, env_type=None, host=None, user=None,
                 port=None, key_path=None, image=None):
        self.label = label
        if env_type is not None:
            self.env_type = env_type
        if host is not None:
            self.host = host
        if user is not None:
            self.user = user
        if port is not None:
            self.port = port
        if key_path is not None:
            self.key_path = key_path
        if image is not None:
            self._image = image
        self.cleanup_calls = 0
        self.stop_calls = 0
        self.terminate_calls = 0

    def cleanup(self):
        self.cleanup_calls += 1

    def stop(self):
        self.stop_calls += 1

    def terminate(self):
        self.terminate_calls += 1


def _ssh_env(label, host, **kwargs):
    return _FakeEnv(label, env_type="ssh", host=host, **kwargs)


def _identity(**env):
    import api.streaming as streaming

    return streaming._terminal_backend_identity(env)


def _install(monkeypatch, environments):
    import api.streaming as streaming

    mod = SimpleNamespace(
        _active_environments=environments,
        _env_lock=threading.Lock(),
    )
    monkeypatch.setattr(streaming, "_terminal_tool_module", lambda: mod)
    return mod


def _identity_a():
    return _identity(TERMINAL_ENV="ssh", TERMINAL_SSH_HOST="host-a")


def _identity_b():
    return _identity(TERMINAL_ENV="ssh", TERMINAL_SSH_HOST="host-b")


# --------------------------------------------------------------------------
# Creation evidence read from the environment object
# --------------------------------------------------------------------------

def test_creator_evidence_reads_the_backend_tag_and_endpoint_fields():
    import api.streaming as streaming

    ssh_env = _ssh_env("ssh", "host-a", user="root", port=2222, key_path="/keys/a")
    assert streaming._terminal_environment_creator_identity(ssh_env) == _identity(
        TERMINAL_ENV="ssh",
        TERMINAL_SSH_HOST="host-a",
        TERMINAL_SSH_USER="root",
        TERMINAL_SSH_PORT="2222",
        TERMINAL_SSH_KEY="/keys/a",
    )

    docker_env = _FakeEnv("docker", env_type="docker", image="image-a")
    assert streaming._terminal_environment_creator_identity(docker_env) == _identity(
        TERMINAL_ENV="docker", TERMINAL_DOCKER_IMAGE="image-a"
    )

    assert streaming._terminal_environment_creator_identity(_FakeEnv()) is None


def test_creator_state_is_match_mismatch_or_unknown():
    import api.streaming as streaming

    identity = _identity_a()
    assert streaming._terminal_environment_creator_state(
        _ssh_env("ssh", "host-a"), identity
    ) == "match"
    assert streaming._terminal_environment_creator_state(
        _ssh_env("ssh", "host-b"), identity
    ) == "mismatch"
    assert streaming._terminal_environment_creator_state(_FakeEnv(), identity) == "unknown"

    # A field the requester specifies but the object cannot prove stays unknown.
    assert streaming._terminal_environment_creator_state(
        _ssh_env("ssh", "host-a"),
        _identity(TERMINAL_ENV="ssh", TERMINAL_SSH_HOST="host-a", TERMINAL_SSH_KEY="/keys/a"),
    ) == "unknown"

    # A field the requester leaves unset (backend default applies) is not a mismatch.
    assert streaming._terminal_environment_creator_state(
        _ssh_env("ssh", "host-a", port=22), identity
    ) == "match"


# --------------------------------------------------------------------------
# The review schedules: an empty-slot request is not creator evidence
# --------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["default", "host-local-default"])
def test_delayed_creator_is_not_bound_to_a_later_empty_slot_requester(monkeypatch, key):
    import api.streaming as streaming

    environments = {}
    _install(monkeypatch, environments)

    # 1/2: both backends observe the empty cache slot.
    streaming._invalidate_stale_terminal_environments(_identity_a())
    streaming._invalidate_stale_terminal_environments(_identity_b())
    assert key not in environments

    # 3: A's delayed creation publishes A's environment.
    created_a = _ssh_env("ssh-a-created", "host-a")
    environments[key] = created_a

    # 4: B's check must not authorize the object A created.
    streaming._invalidate_stale_terminal_environments(_identity_b())

    assert key not in environments, (
        "an environment created by another backend was adopted by the requester that "
        "merely observed the empty slot first"
    )
    assert created_a.cleanup_calls == 1


def test_delayed_creator_after_an_eviction_is_not_bound_to_another_backend(monkeypatch):
    import api.streaming as streaming

    key = "default"
    env_a = _ssh_env("ssh-a", "host-a")
    environments = {key: env_a}
    _install(monkeypatch, environments)

    streaming._invalidate_stale_terminal_environments(_identity_a())
    assert environments[key] is env_a

    streaming._invalidate_stale_terminal_environments(_identity_b())
    assert key not in environments
    assert env_a.cleanup_calls == 1

    # A observes the empty slot left behind by the eviction it did not ask for.
    streaming._invalidate_stale_terminal_environments(_identity_a())
    assert key not in environments

    # B's delayed creation publishes B's environment.
    created_b = _ssh_env("ssh-b-created", "host-b")
    environments[key] = created_b

    streaming._invalidate_stale_terminal_environments(_identity_a())

    assert key not in environments, (
        "an environment created by the displacing backend was retained as the previous backend"
    )
    assert created_b.cleanup_calls == 1


# --------------------------------------------------------------------------
# Controls that must keep passing
# --------------------------------------------------------------------------

def test_the_displacing_backend_binds_its_own_creation_without_churn(monkeypatch):
    import api.streaming as streaming

    key = "default"
    env_a = _ssh_env("ssh-a", "host-a")
    environments = {key: env_a}
    _install(monkeypatch, environments)

    streaming._invalidate_stale_terminal_environments(_identity_a())
    streaming._invalidate_stale_terminal_environments(_identity_b())
    assert key not in environments

    created_b = _ssh_env("ssh-b-created", "host-b")
    environments[key] = created_b

    streaming._invalidate_stale_terminal_environments(_identity_b())
    streaming._invalidate_stale_terminal_environments(_identity_b())

    assert environments[key] is created_b
    assert created_b.cleanup_calls == 0
    assert created_b.stop_calls == 0
    assert created_b.terminate_calls == 0


def test_a_defaulted_endpoint_field_does_not_cause_per_turn_churn(monkeypatch):
    """The requester leaves the port unset; the object was created with the backend default."""
    import api.streaming as streaming

    key = "default"
    environments = {}
    _install(monkeypatch, environments)

    identity_without_port = _identity(TERMINAL_ENV="ssh", TERMINAL_SSH_HOST="host-b")
    streaming._invalidate_stale_terminal_environments(identity_without_port)

    created_b = _ssh_env("ssh-b-created", "host-b", port=22)
    environments[key] = created_b

    streaming._invalidate_stale_terminal_environments(identity_without_port)
    streaming._invalidate_stale_terminal_environments(identity_without_port)

    assert environments[key] is created_b
    assert created_b.cleanup_calls == 0


def test_object_created_by_another_backend_is_invalidated(monkeypatch):
    """The earlier absence rule, now decided by evidence instead of by the pending request."""
    import api.streaming as streaming

    key = "default"
    environments = {}
    _install(monkeypatch, environments)

    streaming._invalidate_stale_terminal_environments(_identity_b())

    created_a = _ssh_env("ssh-a-created", "host-a")
    environments[key] = created_a

    streaming._invalidate_stale_terminal_environments(_identity_b())

    assert key not in environments
    assert created_a.cleanup_calls == 1


def test_unchanged_object_and_identity_are_reused_without_teardown(monkeypatch):
    import api.streaming as streaming

    key = "default"
    env_a = _ssh_env("ssh-a", "host-a")
    environments = {key: env_a}
    _install(monkeypatch, environments)

    streaming._invalidate_stale_terminal_environments(_identity_a())
    streaming._invalidate_stale_terminal_environments(_identity_a())

    assert environments[key] is env_a
    assert env_a.cleanup_calls == 0


def test_objects_without_creation_evidence_keep_the_pending_rule(monkeypatch):
    """A stand-in the backends cannot describe stays on the previous, fail-closed rule."""
    import api.streaming as streaming

    key = "default"
    environments = {}
    _install(monkeypatch, environments)

    streaming._invalidate_stale_terminal_environments(_identity_a())

    created = _FakeEnv("opaque-created")
    environments[key] = created

    streaming._invalidate_stale_terminal_environments(_identity_a())
    assert environments[key] is created
    assert created.cleanup_calls == 0

    # Another backend still may not inherit it.
    other = _FakeEnv("opaque-created-2")
    environments[key] = other
    streaming._invalidate_stale_terminal_environments(_identity_b())
    assert key not in environments
    assert other.cleanup_calls == 1
