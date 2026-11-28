"""A pending expectation is ambiguous once another backend has claimed the same slot (#9386 r4).

The round-3 helper decides ownership of a newly appeared environment from the object's own
creation evidence, but keeps one fallback: when the object cannot prove its creator
(``creator_state == 'unknown'``) it adopts the object whenever the *pending* record carries the
requesting identity. That record is the identity of the LAST requester that observed the empty
slot, so an intervening requester is invisible:

1. backend A (modal image-a) observes the empty cache slot;
2. backend B (modal image-b) observes the same empty slot, overwriting A's pending record;
3. A's delayed creation publishes a direct Modal environment, which keeps no ``_image``, so the
   creator check is ``unknown``;
4. B's check adopts A's object as B's own, with no eviction and no cleanup.

The same hole exists after an eviction: evicting A's environment for B leaves B's expectation
pending, an A request overwrites it, and B's delayed creation is then retained as A.

A second defect is the endpoint comparison itself: ``_terminal_environment_creator_state``
skips every slot the requester leaves unset. For the same SSH host/user, requesting port 2222 as
A and then omitting the port as B makes the helper accept the port-2222 object even though B's
effective port is the backend default 22.

Contract under test:

* An object that cannot prove its creator is adopted only when exactly one backend has claimed
  the empty slot since the slot was last bound. Two distinct claimants make the expectation
  ambiguous, and an ambiguous expectation never authorizes an object.
* A sole claimant still binds its own creation without per-turn teardown (direct Modal objects
  can never prove an image, so evicting every ``unknown`` object each turn is not acceptable).
* A requester that omits an endpoint slot means that backend's effective default: an object
  created with a different, non-default value contradicts the requester and is invalidated.
* The rules repaired in earlier rounds stay: unchanged object with unchanged identity is
  retained, contradicting evidence is invalidated with one cleanup outside the lock, and a
  first-sight baseline is still adopted.
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
    """Terminal environment stand-in carrying whatever creation evidence a backend exposes."""

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


def _direct_modal_env(label, image=None):
    """Direct Modal environments keep no ``_image``: their creator evidence is partial."""
    return _FakeEnv(label, env_type="modal", image=image)


def _managed_modal_env(label, image):
    return _FakeEnv(label, env_type="modal", image=image)


def _opaque_env(label):
    """A stand-in the backends cannot describe at all."""
    return _FakeEnv(label)


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


def _modal_a():
    return _identity(TERMINAL_ENV="modal", TERMINAL_MODAL_IMAGE="image-a")


def _modal_b():
    return _identity(TERMINAL_ENV="modal", TERMINAL_MODAL_IMAGE="image-b")


def _ssh_a():
    return _identity(TERMINAL_ENV="ssh", TERMINAL_SSH_HOST="host-a")


def _ssh_b():
    return _identity(TERMINAL_ENV="ssh", TERMINAL_SSH_HOST="host-b")


def _ssh_port_request(port=None):
    env = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-a", "TERMINAL_SSH_USER": "root"}
    if port is not None:
        env["TERMINAL_SSH_PORT"] = port
    return _identity(**env)


# --------------------------------------------------------------------------
# Defect 1 — an unknown-evidence object and more than one claimant
# --------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["default", "host-local-default"])
def test_unknown_evidence_delayed_creator_is_not_adopted_by_an_intervening_requester(monkeypatch, key):
    import api.streaming as streaming

    environments = {}
    _install(monkeypatch, environments)

    # 1/2: A and then B both observe the empty slot.
    streaming._invalidate_stale_terminal_environments(_modal_a())
    streaming._invalidate_stale_terminal_environments(_modal_b())
    assert key not in environments

    # 3: A's delayed creation publishes a direct Modal environment (no image evidence).
    created_a = _direct_modal_env("modal-a-created")
    environments[key] = created_a

    # 4: B must not adopt an object another backend built while B only had a pending request.
    streaming._invalidate_stale_terminal_environments(_modal_b())

    assert key not in environments, (
        "a direct Modal creator that cannot prove its image was adopted as the later "
        "requester's environment just because the pending record matched"
    )
    assert created_a.cleanup_calls == 1


def test_unknown_evidence_creation_after_an_eviction_is_not_bound_back(monkeypatch):
    import api.streaming as streaming

    key = "default"
    env_a = _ssh_env("ssh-a", "host-a")
    environments = {key: env_a}
    _install(monkeypatch, environments)

    streaming._invalidate_stale_terminal_environments(_ssh_a())
    assert environments[key] is env_a

    # B displaces A's environment and leaves its own expectation pending.
    streaming._invalidate_stale_terminal_environments(_ssh_b())
    assert key not in environments
    assert env_a.cleanup_calls == 1

    # A observes the empty slot left behind by that eviction.
    streaming._invalidate_stale_terminal_environments(_ssh_a())
    assert key not in environments

    # B's delayed creation publishes an object that cannot prove its creator.
    created_b = _opaque_env("opaque-created-for-b")
    environments[key] = created_b

    streaming._invalidate_stale_terminal_environments(_ssh_a())

    assert key not in environments, (
        "an unprovable object published after another backend displaced the slot was "
        "retained by the previous backend"
    )
    assert created_b.cleanup_calls == 1


# --------------------------------------------------------------------------
# Defect 2 — an omitted request slot means the backend default, not "anything"
# --------------------------------------------------------------------------

def test_nondefault_port_creator_is_rejected_when_the_request_omits_the_port(monkeypatch):
    import api.streaming as streaming

    key = "default"
    environments = {}
    _install(monkeypatch, environments)

    omitted = _ssh_port_request()  # effective port is the backend default, 22
    streaming._invalidate_stale_terminal_environments(omitted)

    # An object built for port 2222 contradicts the defaulted effective endpoint.
    created_nondefault = _ssh_env("ssh-port-2222", "host-a", user="root", port=2222)
    environments[key] = created_nondefault

    streaming._invalidate_stale_terminal_environments(omitted)

    assert key not in environments, (
        "the helper accepted a port-2222 environment for a request whose effective port "
        "is the backend default 22"
    )
    assert created_nondefault.cleanup_calls == 1


def test_explicit_nondefault_port_creator_is_reused_for_the_same_port(monkeypatch):
    import api.streaming as streaming

    key = "default"
    environments = {}
    _install(monkeypatch, environments)

    explicit = _ssh_port_request(2222)
    streaming._invalidate_stale_terminal_environments(explicit)

    created = _ssh_env("ssh-port-2222-same", "host-a", user="root", port=2222)
    environments[key] = created

    streaming._invalidate_stale_terminal_environments(explicit)
    streaming._invalidate_stale_terminal_environments(explicit)

    assert environments[key] is created
    assert created.cleanup_calls == 0


def test_explicit_port_creator_is_rejected_for_a_defaulted_request(monkeypatch):
    """Same host/user, explicit 2222 object, requester asks for the default port explicitly."""
    import api.streaming as streaming

    key = "default"
    environments = {}
    _install(monkeypatch, environments)

    explicit_default = _ssh_port_request(22)
    streaming._invalidate_stale_terminal_environments(explicit_default)

    created = _ssh_env("ssh-port-2222", "host-a", user="root", port=2222)
    environments[key] = created

    streaming._invalidate_stale_terminal_environments(explicit_default)

    assert key not in environments
    assert created.cleanup_calls == 1


# --------------------------------------------------------------------------
# Controls that must keep passing
# --------------------------------------------------------------------------

def test_sole_pending_requester_still_binds_an_unprovable_creation_without_churn(monkeypatch):
    import api.streaming as streaming

    key = "default"
    environments = {}
    _install(monkeypatch, environments)

    streaming._invalidate_stale_terminal_environments(_modal_a())

    created = _direct_modal_env("modal-a-created")
    environments[key] = created

    streaming._invalidate_stale_terminal_environments(_modal_a())
    streaming._invalidate_stale_terminal_environments(_modal_a())

    assert environments[key] is created, (
        "the only backend that ever claimed the slot must still reuse its own creation"
    )
    assert created.cleanup_calls == 0
    assert created.stop_calls == 0
    assert created.terminate_calls == 0


def test_defaulted_port_creation_is_reused_without_per_turn_churn(monkeypatch):
    import api.streaming as streaming

    key = "default"
    environments = {}
    _install(monkeypatch, environments)

    omitted = _ssh_port_request()
    streaming._invalidate_stale_terminal_environments(omitted)

    created = _ssh_env("ssh-default-port", "host-a", user="root", port=22)
    environments[key] = created

    streaming._invalidate_stale_terminal_environments(omitted)
    streaming._invalidate_stale_terminal_environments(omitted)

    assert environments[key] is created
    assert created.cleanup_calls == 0


def test_managed_modal_creator_still_matches_by_image(monkeypatch):
    import api.streaming as streaming

    key = "default"
    environments = {}
    _install(monkeypatch, environments)

    streaming._invalidate_stale_terminal_environments(_modal_a())

    created = _managed_modal_env("managed-modal-a", "image-a")
    environments[key] = created

    streaming._invalidate_stale_terminal_environments(_modal_a())
    assert environments[key] is created
    assert created.cleanup_calls == 0

    # The mirror: the same object cannot be adopted by a different image.
    other = _managed_modal_env("managed-modal-b", "image-a")
    environments[key] = other
    streaming._invalidate_stale_terminal_environments(_modal_b())
    assert key not in environments
    assert other.cleanup_calls == 1


def test_first_sight_baseline_is_still_adopted(monkeypatch):
    import api.streaming as streaming

    key = "default"
    env_a = _ssh_env("ssh-a", "host-a", user="root", port=22)
    environments = {key: env_a}
    _install(monkeypatch, environments)

    streaming._invalidate_stale_terminal_environments(_ssh_a())
    streaming._invalidate_stale_terminal_environments(_ssh_a())

    assert environments[key] is env_a
    assert env_a.cleanup_calls == 0


def test_contradicting_host_is_still_invalidated(monkeypatch):
    import api.streaming as streaming

    key = "default"
    environments = {}
    _install(monkeypatch, environments)

    streaming._invalidate_stale_terminal_environments(_ssh_a())

    created_b = _ssh_env("ssh-b-created", "host-b", user="root", port=22)
    environments[key] = created_b

    streaming._invalidate_stale_terminal_environments(_ssh_a())

    assert key not in environments
    assert created_b.cleanup_calls == 1
