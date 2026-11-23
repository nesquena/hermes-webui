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
