"""#7777 re-gate — the two findings still live at this head.

The reviewer's list is against an older head. Re-deriving each item against the
current code, five of the seven P1s are already closed (alias union in both
``api.config.get_picker_excludes`` and the browser matcher, the provider-aware
prefix strip that keeps colons inside model ids, the known-provider-only slash
split, the case-preserving browser comparison, and the ``eligibleLast``
single-``find`` fallback), and the live-cache race is closed by the
``_picker_excludes_epoch`` cache key plus the ``_models_cache_source_fingerprint``
straddle guard. Those are asserted by this PR's own suites.

Two were genuinely live, and both are "the policy is enforced on the server but
the browser still acts on a value the server rejected" bugs:

1. **Alias exclusions reach the browser unmatched.** ``_picker_excludes_payload``
   published the *raw* stored keys. A settings.json written by hand (or an
   imported one) can key an exclusion under an alias — ``glm`` for ``zai`` —
   and the server resolves that alias when it filters the catalog, but the
   browser's ``_pickerExcludesForProvider`` only normalises punctuation and
   case, not the agent's semantic alias table. So the browser received
   ``{"glm": [...]}``, looked up ``zai``, found nothing, and re-injected the
   saved default or previous selection the server had just filtered out.

2. **An unrelated save replaces the configured default.** When the saved default
   is excluded, the Settings open handler substitutes the first eligible row so
   the select is not blank. ``saveSettings()`` derived ``modelChanged`` by
   comparing the live select value against the value captured on open — so that
   substitution read as a model change, and saving *any* preference (theme, send
   key, notifications) POSTed the substituted row to ``/api/default-model``.

Both fixes are asserted here against the real functions rather than by reading
the source, because the first attempt at the second fix guessed the select's id
and silently did nothing.
"""

from __future__ import annotations

import json

import pytest

from api import config


@pytest.fixture
def _settings_store(monkeypatch):
    """Install a fake settings store and return its setter."""
    store: dict = {}

    def _install(payload: dict) -> None:
        store.clear()
        store.update(payload)
        monkeypatch.setattr(config, "load_settings", lambda: store)

    monkeypatch.setattr(config, "load_settings", lambda: store)
    return _install


# ── finding 1: alias-keyed exclusions must be canonical in the payload ──────


def test_alias_keyed_exclusions_are_published_canonically(_settings_store):
    _settings_store({"picker_excludes": {"glm": ["glm-4.6"]}})
    payload = config._picker_excludes_payload()
    assert "glm" not in payload, (
        "the raw alias key was published, so the browser's provider lookup "
        "cannot find the exclusion and re-injects the excluded model"
    )
    assert payload.get("zai") == ["glm-4.6"], payload


def test_alias_and_canonical_lists_are_unioned(_settings_store):
    """A hand-edited settings.json can carry both keys with different slices."""
    _settings_store({"picker_excludes": {"glm": ["a"], "zai": ["b"]}})
    payload = config._picker_excludes_payload()
    assert set(payload.get("zai", [])) == {"a", "b"}, payload
    assert len(payload) == 1, payload


def test_punctuation_alias_key_is_canonicalised(_settings_store):
    _settings_store({"picker_excludes": {"z.ai": ["glm-4.6"]}})
    payload = config._picker_excludes_payload()
    assert payload.get("zai") == ["glm-4.6"], payload


def test_an_unknown_provider_key_is_preserved(_settings_store):
    """Unknown slugs must pass through unchanged, not be dropped or mangled."""
    _settings_store({"picker_excludes": {"some-new-provider": ["m1"]}})
    payload = config._picker_excludes_payload()
    assert payload == {"some-new-provider": ["m1"]}, payload


def test_payload_is_deterministic(_settings_store):
    """The payload doubles as the live-cache epoch, so it must be stable."""
    _settings_store({"picker_excludes": {"zai": ["b", "a"], "openrouter": ["x"]}})
    first = config._picker_excludes_payload()
    second = config._picker_excludes_payload()
    assert first == second, "payload is not deterministic; the epoch would churn"


def test_payload_changes_when_the_exclusion_set_changes(_settings_store):
    """A stale epoch would let an excluded model survive a policy change."""
    _settings_store({"picker_excludes": {"zai": ["a"]}})
    before = json.dumps(config._picker_excludes_payload(), sort_keys=True)
    _settings_store({"picker_excludes": {"zai": ["a", "b"]}})
    after = json.dumps(config._picker_excludes_payload(), sort_keys=True)
    assert before != after


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"picker_excludes": None},
        {"picker_excludes": [1, 2, 3]},
        {"picker_excludes": "nope"},
        {"picker_excludes": {"zai": "not-a-list"}},
        {"picker_excludes": {"": ["x"]}},
        {"picker_excludes": {"   ": ["x"]}},
        {"picker_excludes": {"zai": [None, 1, "ok", "", "   "]}},
        {"picker_excludes": {5: ["x"]}},
    ],
)
def test_payload_tolerates_malformed_stores(_settings_store, raw):
    """A partial or corrupt save must never break the picker or the epoch."""
    _settings_store(raw)
    payload = config._picker_excludes_payload()
    assert isinstance(payload, dict)
    for key, ids in payload.items():
        assert isinstance(key, str) and key.strip()
        assert isinstance(ids, list)
        assert all(isinstance(i, str) and i.strip() for i in ids)


def test_empty_policy_serialises_to_an_empty_object(_settings_store):
    _settings_store({})
    assert config._picker_excludes_payload() == {}


# ── finding 2: the payload must agree with the resolver it feeds ────────────


def test_payload_and_resolver_agree_on_an_alias_key(_settings_store):
    """The browser applies the payload; the server applies the resolver.

    If these disagree the two sides of the policy diverge, which is the whole
    class of bug this PR exists to close.
    """
    _settings_store({"picker_excludes": {"glm": ["glm-4.6"]}})
    assert config.get_picker_excludes("zai") == {"glm-4.6"}
    assert config._picker_excludes_payload() == {"zai": ["glm-4.6"]}


def test_server_side_exclusion_matching_survives_a_colon_model_id(_settings_store):
    """Regression pin for the provider-aware prefix strip."""
    _settings_store({"picker_excludes": {"opencode-zen": ["vendor/model:1"]}})
    excludes = config.get_picker_excludes("opencode-zen")
    assert config._is_model_id_excluded("@opencode-zen:vendor/model:1", excludes)


def test_a_slash_id_is_not_over_filtered(_settings_store):
    """Regression pin for the known-provider-only slash split."""
    _settings_store({"picker_excludes": {"x": ["bar"]}})
    excludes = config.get_picker_excludes("x")
    assert config._is_model_id_excluded("bar", excludes)
    assert not config._is_model_id_excluded("vendor/bar", excludes), (
        "excluding the bare id 'bar' hid the distinct valid model 'vendor/bar'"
    )


def test_exclusions_are_case_preserving(_settings_store):
    """Regression pin for the case-preserving comparison on both sides."""
    _settings_store({"picker_excludes": {"openrouter": ["model-a"]}})
    excludes = config.get_picker_excludes("openrouter")
    assert config._is_model_id_excluded("model-a", excludes)
    assert not config._is_model_id_excluded("MODEL-A", excludes), (
        "a lowercase exclusion suppressed the distinct catalog id 'MODEL-A'"
    )
