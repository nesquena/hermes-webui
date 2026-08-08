"""_SETTINGS_DEFAULTS theme/skin must survive load_settings() when nothing is stored.

``_read_raw_settings_file()`` returns ``{}`` for a MISSING settings.json, and ``{}``
is a dict — so ``load_settings()`` took its ``isinstance(stored, dict)`` arms, passed
``None`` into ``_normalize_appearance``, and got back that function's unknown-theme
fallback ``("dark", "default")``. The defaults dict was unreachable for the one case
it exists to serve: a user with no settings file yet.

On stock defaults this is invisible, because dark/default is exactly what the
fallback produces — the two paths agree, which is why nothing caught it. It only
surfaces once ``_SETTINGS_DEFAULTS`` is changed, at which point the dict silently
does nothing and the appearance a deployment configured never reaches the client.

The guard is on the PAIR of keys, not per field. A per-field
``or settings.get(...)`` looks equivalent and is not: with a stored legacy theme and
no stored skin, ``slate`` normalises to ``("dark", "slate")``, but per-field fallback
injects the default skin and yields ``("dark", "default")``, silently dropping the
legacy migration. The tests below pin that distinction in both directions.
"""

import json

import pytest

import api.config as config


@pytest.fixture()
def settings_file(tmp_path, monkeypatch):
    """Point config.SETTINGS_FILE at a temp path for isolated load_settings tests."""
    f = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", f)
    return f


@pytest.fixture()
def custom_defaults(monkeypatch):
    """Override ONLY theme/skin in the defaults, keeping every other key intact.

    Values deliberately differ from the ("dark", "default") fallback, so a test
    that passes here cannot be passing by coincidence — which is exactly how this
    bug survived on stock defaults.
    """
    d = dict(config._SETTINGS_DEFAULTS)
    d["theme"] = "light"
    d["skin"] = "poseidon"
    monkeypatch.setattr(config, "_SETTINGS_DEFAULTS", d)
    return d


def _write(f, payload):
    f.write_text(json.dumps(payload), encoding="utf-8")


# ── the bug ───────────────────────────────────────────────────────────────

def test_defaults_are_used_when_no_settings_file_exists(settings_file, custom_defaults):
    assert not settings_file.exists()
    s = config.load_settings()
    assert (s["theme"], s["skin"]) == ("light", "poseidon"), (
        "with no settings.json, _SETTINGS_DEFAULTS theme/skin must be honoured; "
        "falling through to _normalize_appearance(None, None) makes the defaults "
        "dict unreachable for new users"
    )


def test_defaults_are_used_when_file_has_no_appearance_keys(settings_file, custom_defaults):
    _write(settings_file, {"font_size": "large"})
    s = config.load_settings()
    assert (s["theme"], s["skin"]) == ("light", "poseidon"), (
        "a settings file that stores no appearance must still take the defaults"
    )


# ── no behaviour change on stock defaults ─────────────────────────────────

def test_stock_defaults_are_unchanged_by_the_guard(settings_file):
    """On the shipped defaults this fix is a no-op — both paths give dark/default."""
    assert not settings_file.exists()
    s = config.load_settings()
    assert (s["theme"], s["skin"]) == (
        config._SETTINGS_DEFAULTS["theme"],
        config._SETTINGS_DEFAULTS["skin"],
    )
    assert (s["theme"], s["skin"]) == ("dark", "default")


# ── a stored preference still wins, exactly as before ─────────────────────

@pytest.mark.parametrize(
    "stored,expected",
    [
        ({"theme": "dark"}, ("dark", "default")),
        ({"theme": "system"}, ("system", "default")),
        ({"theme": "light", "skin": "mono"}, ("light", "mono")),
        ({"skin": "mono"}, ("dark", "mono")),
        ({"theme": "not-a-theme"}, ("dark", "default")),
    ],
)
def test_stored_appearance_still_wins(settings_file, custom_defaults, stored, expected):
    """Either key present means the user has a preference; defaults must not leak in.

    Note ``{"skin": "mono"}`` resolves the THEME to dark, not to the default light:
    a stored skin makes the pair user-owned, so the whole pair goes through
    normalisation unchanged. That is the pre-existing behaviour and must not move.
    """
    _write(settings_file, stored)
    s = config.load_settings()
    assert (s["theme"], s["skin"]) == expected


# ── the reason the guard is pair-level ────────────────────────────────────

@pytest.mark.parametrize(
    "legacy,expected",
    [
        ("slate", ("dark", "slate")),
        ("solarized", ("dark", "poseidon")),
        ("monokai", ("dark", "sisyphus")),
        ("nord", ("dark", "slate")),
        ("oled", ("dark", "default")),
    ],
)
def test_legacy_theme_migration_is_not_broken_by_the_fallback(
    settings_file, custom_defaults, legacy, expected
):
    """Only the theme is stored; the skin is DERIVED from the legacy mapping.

    A per-field fallback would substitute the default skin here and destroy the
    migration. With custom defaults of light/poseidon, that failure mode is
    visible: `slate` would come back ("dark", "poseidon") instead of
    ("dark", "slate").
    """
    _write(settings_file, {"theme": legacy})
    s = config.load_settings()
    assert (s["theme"], s["skin"]) == expected
