"""#9096 item 1: the secret-scope capability cache must follow the SETTER.

``api/profiles.py::_set_secret_scope_for_profile`` caches the
``profile_home`` capability decision as ``(module, setter,
accepts_profile_home)`` and reuses it only when both the module AND the
``set_secret_scope`` function are the same objects.

The existing test (``test_legacy_set_secret_scope_is_called_positionally_and_decided_once``)
pins the "inspected once per module" half: while the setter stays the same
object, the signature is inspected once and not per turn.

Nothing pinned the other half. Replacing ``mod.set_secret_scope`` on the SAME
module object -- an Agent reload, or a plugin wrapping the setter -- has to
re-inspect the new setter, because the new setter may accept
``profile_home`` where the old one did not. The ``cached[1] is setter``
comparison is what makes that work today; this file pins it so a future
"cache the module only" refactor cannot silently drop it.

The cache is module-level state, so each test resets it.
"""

from __future__ import annotations

import pytest

from api import profiles

from tests.test_issue9084_secret_scope_profile_stamp import (
    PROFILE,
    _count_signature_calls,
    _enter,
    _install_fake_secret_scope,
    _setup_profile,
)


@pytest.fixture(autouse=True)
def _reset_capability_cache():
    """The capability cache is process-global; keep each test independent."""
    profiles._secret_scope_stamp_support = None
    yield
    profiles._secret_scope_stamp_support = None


@pytest.mark.parametrize("which", ("background_worker", "active_request_readonly"))
def test_replacing_the_setter_on_the_same_module_is_re_inspected(
    monkeypatch, tmp_path, which
):
    """A swapped setter is a different capability, even on the same module."""
    _base, home = _setup_profile(monkeypatch, tmp_path)
    calls, resets = [], []

    def legacy_set_secret_scope(secrets):
        calls.append(("legacy", dict(secrets)))
        return "legacy-token"

    def stamped_set_secret_scope(secrets, profile_home=None):
        calls.append(("stamped", dict(secrets), profile_home))
        return "stamped-token"

    module = _install_fake_secret_scope(monkeypatch, legacy_set_secret_scope, resets)
    inspected = _count_signature_calls(monkeypatch, legacy_set_secret_scope)
    stamped_inspected = _count_signature_calls(monkeypatch, stamped_set_secret_scope)
    expected_home = profiles.get_hermes_home_for_profile(PROFILE)

    # First entry: the legacy setter, installed positionally, decided once.
    with _enter(which):
        assert len(calls) == 1
    assert calls[0][0] == "legacy"
    assert len(inspected) == 1

    # Swap the setter on the SAME module object -- an Agent reload or a plugin
    # wrapping the setter does exactly this.
    module.set_secret_scope = stamped_set_secret_scope

    # Second entry: the new setter accepts profile_home, so the install must be
    # stamped. Reusing the old decision would call it positionally and lose the
    # stamp (and raise TypeError inside the swallowed context manager).
    with _enter(which):
        assert len(calls) == 2, calls
    assert calls[1][0] == "stamped", calls[1]
    assert calls[1][2] == str(expected_home), calls[1]

    # The new setter's signature was inspected, and only the new one.
    assert len(stamped_inspected) == 1, stamped_inspected
    assert len(inspected) == 1, "the legacy setter was re-inspected"


def test_a_stale_cache_does_not_leak_across_modules(monkeypatch, tmp_path):
    """The decision is per (module, setter) pair, not per module."""
    _base, _home = _setup_profile(monkeypatch, tmp_path)
    calls, resets = [], []

    def legacy_set_secret_scope(secrets):
        calls.append(("legacy", dict(secrets)))
        return "legacy-token"

    def stamped_set_secret_scope(secrets, profile_home=None):
        calls.append(("stamped", dict(secrets), profile_home))
        return "stamped-token"

    _install_fake_secret_scope(monkeypatch, legacy_set_secret_scope, resets)
    with _enter("background_worker"):
        pass
    assert calls[-1][0] == "legacy"

    # A different module object with a stamped setter must not inherit the
    # legacy decision.
    _install_fake_secret_scope(monkeypatch, stamped_set_secret_scope, resets)
    with _enter("background_worker"):
        pass
    assert calls[-1][0] == "stamped", calls[-1]
    assert calls[-1][2] == str(_home)


def test_the_cached_entry_records_both_the_module_and_the_setter(monkeypatch, tmp_path):
    """The cache entry itself keeps the setter identity, not just the module."""
    _base, _home = _setup_profile(monkeypatch, tmp_path)
    calls, resets = [], []

    def legacy_set_secret_scope(secrets):
        calls.append(dict(secrets))
        return "legacy-token"

    _install_fake_secret_scope(monkeypatch, legacy_set_secret_scope, resets)
    with _enter("background_worker"):
        pass

    cached = profiles._secret_scope_stamp_support
    assert cached is not None
    cached_module, cached_setter, accepts = cached
    assert cached_module is not None
    assert cached_setter is legacy_set_secret_scope
    assert accepts is False
    # The entry is a 3-tuple; a 2-tuple shape would lose the setter identity.
    assert len(cached) == 3
