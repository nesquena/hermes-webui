"""#9061 item 2: a refusal from the Agent's own claim must be recorded for the construction.

``_claim_for_profile`` (api/memory_profile_scope.py) wraps the Agent's
``claim_instance``. When the Agent's own claim returns False the guard returns
False as well — but it did not append the provider to ``_construction_refusals``,
while the guard's own cross-profile refusal does.
``construct_profile_scoped_agent`` reads that list once the constructor has
returned (it decides ``MEMORY_OFF_PROVIDER_SHARED_INSTANCE``), so a
construction whose provider the AGENT dropped used to answer "memory ok"
although its provider was gone. Whether the build is cached does not depend on
this list.

The advisor could only reach this by claiming before the guard was installed,
which production does not do; recording the provider name there closes it.

These tests drive ``_claim_for_profile`` directly with a stand-in ``original``
callback, so they do not depend on an installed Agent.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def scope(monkeypatch):
    """Fresh guard state: no recorded owners, nothing logged once."""
    import api.memory_profile_scope as scope  # noqa: PLC0415

    monkeypatch.setattr(scope, "_logged_once", set())
    monkeypatch.setattr(scope, "_instance_owners", {})
    return scope


@pytest.fixture
def refusals(scope):
    """A construction-scoped refusal list, installed the way the constructor does."""
    seen: list = []
    token = scope._construction_refusals.set(seen)
    try:
        yield seen
    finally:
        scope._construction_refusals.reset(token)


class _Provider:
    name = "probe"


def _claim(scope, original, provider, home):
    return scope._claim_for_profile(original, provider, home)


def test_the_agents_own_refusal_is_recorded_for_the_construction(scope, refusals, tmp_path):
    """#9061 item 2: the Agent drops the provider, so the construction must see it."""

    def original(provider, home, *args, **kwargs):  # the Agent's claim refuses
        return False

    provider, home = _Provider(), str(tmp_path / "alpha")
    assert _claim(scope, original, provider, home) is False
    assert refusals == [provider.name]


def test_an_accepted_claim_is_not_a_refusal(scope, refusals, tmp_path):
    """Control: an accepted claim records nothing and makes the home the owner."""

    def original(provider, home, *args, **kwargs):
        return True

    provider, home = _Provider(), str(tmp_path / "alpha")
    assert _claim(scope, original, provider, home) is True
    assert refusals == []
    assert id(provider) in scope._instance_owners


def test_a_refusal_without_a_construction_context_is_not_an_error(scope, tmp_path):
    """Control: no ``_construction_refusals`` set (a claim outside any construction)
    still answers False, records nothing and raises nothing."""
    assert scope._construction_refusals.get() is None

    def original(provider, home, *args, **kwargs):
        return False

    assert _claim(scope, original, _Provider(), str(tmp_path / "alpha")) is False
    assert scope._construction_refusals.get() is None


def test_the_guards_own_cross_profile_refusal_still_records(scope, refusals, tmp_path):
    """Regression control: the path the guard itself takes (a second profile on an
    instance another home owns) keeps appending to ``_construction_refusals``."""

    def original(provider, home, *args, **kwargs):
        return True

    shared = _Provider()  # one instance handed to two profiles
    alpha, beta = str(tmp_path / "alpha"), str(tmp_path / "beta")
    assert _claim(scope, original, shared, alpha) is True
    assert _claim(scope, original, shared, beta) is False
    assert refusals == [shared.name]
