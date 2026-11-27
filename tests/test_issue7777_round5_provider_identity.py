"""#7777 re-gate 2026-11-23 — provider identity may not cross the custom/builtin boundary.

The reviewer's counterexample:

    {"custom:openai": ["shared"]} hides the builtin ``openai``'s ``shared`` too.

``_provider_id_lookup_keys("custom:openai")`` strips the ``custom:`` prefix and
adds the bare ``openai`` to its lookup set, where it collides with the REAL
builtin provider id ``openai``. ``get_picker_excludes("openai")`` then unions
the custom provider's policy onto the builtin.

The contract this pins: the bare spelling of a builtin provider belongs to
that builtin. A custom endpoint whose slug happens to spell a builtin's name
(``custom:openai``) is a DIFFERENT provider and is not that builtin's policy
owner. When the bare slug does NOT collide with a builtin id, the historical
bare↔``custom:<slug>`` bridge must keep working (that is what the previous
round added it for).
"""

from __future__ import annotations

import pytest

from api import config


@pytest.fixture
def _settings_store(monkeypatch):
    store: dict = {}

    def _install(payload: dict) -> None:
        store.clear()
        store.update(payload)
        monkeypatch.setattr(config, "load_settings", lambda: store)

    monkeypatch.setattr(config, "load_settings", lambda: store)
    return _install


# ── finding 1: custom and builtin provider identity overlap ───────────────


def test_custom_provider_named_after_a_builtin_does_not_own_its_exclusions(
    _settings_store,
):
    """`custom:openai` must not hide the builtin `openai`'s models."""
    _settings_store({"picker_excludes": {"custom:openai": ["shared"]}})
    assert config.get_picker_excludes("custom:openai") == {"shared"}
    assert config.get_picker_excludes("openai") == set(), (
        "a custom provider named after a builtin leaked its excludes onto that builtin"
    )


def test_builtin_exclusions_do_not_reach_a_same_named_custom_provider(
    _settings_store,
):
    """The reverse direction: the builtin's policy is not `custom:openai`'s."""
    _settings_store({"picker_excludes": {"openai": ["shared"]}})
    assert config.get_picker_excludes("openai") == {"shared"}
    assert config.get_picker_excludes("custom:openai") == set()


def test_lookup_keys_do_not_collapse_a_custom_slug_onto_a_builtin_id():
    """The generator itself is the fix site — assert it directly.

    A ``custom:<known-builtin>`` id must NOT advertise the builtin's bare
    spelling as one of its lookup keys, or every consumer of
    ``_provider_id_lookup_keys`` inherits the collision.
    """
    keys = set(config._provider_id_lookup_keys("custom:openai"))
    assert "openai" not in keys, (
        "custom:openai advertised the builtin bare id 'openai' as its own spelling"
    )
    assert "custom:openai" in keys


def test_custom_provider_bare_slug_bridge_survives_when_not_a_builtin(
    _settings_store,
):
    """Control: a custom slug that does not collide with a builtin keeps the
    historical bare form working. Alpha is not a builtin provider id."""
    _settings_store({"picker_excludes": {"alpha": ["chat-a"]}})
    assert config.get_picker_excludes("custom:alpha") == {"chat-a"}
    assert config.get_picker_excludes("alpha") == {"chat-a"}


def test_custom_provider_own_exclusion_does_not_bleed_to_sibling_builtin(
    _settings_store,
):
    """Multi-provider control: the custom provider's own ids stay its own even
    when a same-named builtin is present in the same store."""
    _settings_store(
        {
            "picker_excludes": {
                "custom:openai": ["shared", "custom-only"],
                "openai": ["builtin-only"],
                "anthropic": ["claude-keep"],
            }
        }
    )
    custom = config.get_picker_excludes("custom:openai")
    builtin = config.get_picker_excludes("openai")
    assert custom == {"shared", "custom-only"}, (
        "custom:openai lost its own policy or absorbed the builtin's: "
        f"got {custom!r}"
    )
    assert builtin == {"builtin-only"}, builtin


# ── 2026-11-23 09:16 re-review: builtin alias names have TWO owners ─────────

def test_alias_key_hides_builtin_but_not_a_custom_endpoint_named_after_it(
    _settings_store,
):
    """``{"glm": ["m"]}`` → zai hides m; custom:glm keeps its own models.

    ``glm`` is a builtin ALIAS of ``zai``. Before the fix
    ``_provider_id_lookup_keys("custom:glm")`` advertised the bare ``glm``
    spelling, so one stored key was applied to both providers.
    """
    _settings_store({"picker_excludes": {"glm": ["m"]}})
    assert config.get_picker_excludes("zai") == {"m"}
    assert config.get_picker_excludes("custom:glm") == set(), (
        "a builtin alias exclusion leaked onto a custom endpoint of the same name"
    )
    assert "glm" not in set(config._provider_id_lookup_keys("custom:glm"))


def test_exclusion_under_alias_named_custom_endpoint_does_not_reach_builtin(
    _settings_store,
):
    """``{"custom:glm": ["m"]}`` → custom:glm hides m; zai untouched.

    Reverse direction / over-canonicalisation guard: a fix that stripped the
    ``custom:`` prefix before alias resolution would fold ``custom:glm`` into
    ``zai`` and fail this test.
    """
    _settings_store({"picker_excludes": {"custom:glm": ["m"]}})
    assert config.get_picker_excludes("custom:glm") == {"m"}
    assert config.get_picker_excludes("zai") == set(), (
        "a custom endpoint's policy reached the builtin behind its alias"
    )
    payload = config._picker_excludes_payload()
    assert payload.get("custom:glm") == ["m"]
    assert "m" not in payload.get("zai", [])


@pytest.mark.parametrize(
    "alias,canonical",
    [("glm", "zai"), ("kimi", "kimi-coding"), ("moonshot", "kimi-coding")],
)
def test_representative_builtin_alias_keys_are_single_owner(
    _settings_store, alias, canonical
):
    """The rule covers alias keys generally, not only ``glm``."""
    _settings_store({"picker_excludes": {alias: ["m"]}})
    assert config.get_picker_excludes(canonical) == {"m"}
    assert config.get_picker_excludes("custom:" + alias) == set()


def test_browser_payload_applies_the_same_ownership_rule(_settings_store):
    """Server lookup and browser payload agree on which group each key filters."""
    _settings_store({"picker_excludes": {"glm": ["m"], "custom:glm": ["m2"]}})
    payload = config._picker_excludes_payload()
    assert payload == {"zai": ["m"], "custom:glm": ["m2"]}
    # Same group on both sides: zai sees m, custom:glm sees m2, never crossed.
    assert config.get_picker_excludes("zai") == set(payload["zai"])
    assert config.get_picker_excludes("custom:glm") == set(payload["custom:glm"])
