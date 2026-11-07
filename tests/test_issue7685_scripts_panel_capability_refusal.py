"""Regression tests for #7685: a capability refusal must not read as empty.

The maintainer review flagged one thing the earlier rounds left open: on a
platform without ``O_NOFOLLOW``/``O_DIRECTORY`` the listing came back empty,
so the panel said "No scripts in this profile yet." -- a fact the panel
cannot know. The directory could not be opened safely, so the listing was
never produced at all.

That is the difference the response-time guidance calls out: a refusal is
an UNKNOWN answer, an empty folder is a KNOWN-EMPTY one, and collapsing the
two makes "this platform cannot serve the listing" indistinguishable from
"this profile has no scripts".

``list_scripts()`` therefore reports the refusal in an ``unavailable`` field
and the panel renders that state instead of the empty one.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path


def _scripts_module():
    import api.scripts_panel as mod

    return importlib.reload(mod)


def test_a_capability_refusal_is_reported_not_returned_as_empty(
    tmp_path, monkeypatch
) -> None:
    """Without the containment flags the listing is refused, not empty."""
    mod = _scripts_module()
    (tmp_path / "plain.py").write_text("print('plain')\n", encoding="utf-8")
    monkeypatch.setattr(mod, "scripts_dir", lambda: tmp_path)

    # Simulate a platform without O_NOFOLLOW: the directory open refuses.
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)

    result = mod.list_scripts()

    assert result["unavailable"], (
        "a capability refusal must be reported in `unavailable`; an empty "
        "listing reads as 'this profile has no scripts'"
    )
    assert result["scripts"] == [], "a refusal must not describe any entry"
    assert "O_NOFOLLOW" in result["unavailable"], result["unavailable"]


def test_a_refused_listing_does_not_claim_the_directory_is_empty(
    tmp_path, monkeypatch
) -> None:
    """The refusal is distinguishable from the known-empty state."""
    mod = _scripts_module()
    (tmp_path / "plain.py").write_text("print('plain')\n", encoding="utf-8")
    monkeypatch.setattr(mod, "scripts_dir", lambda: tmp_path)
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)

    refused = mod.list_scripts()

    monkeypatch.undo()
    mod = _scripts_module()
    monkeypatch.setattr(mod, "scripts_dir", lambda: tmp_path)
    normal = mod.list_scripts()

    assert refused["unavailable"], "the refused listing must say so"
    assert normal["unavailable"] == "", (
        "a capable platform must not report a refusal"
    )
    assert normal["scripts"], "the capable platform must list the script"


def test_a_genuinely_empty_profile_is_still_empty_not_refused(
    tmp_path, monkeypatch
) -> None:
    """An empty directory stays a known-empty answer."""
    mod = _scripts_module()
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    monkeypatch.setattr(mod, "scripts_dir", lambda: scripts_dir)

    result = mod.list_scripts()

    assert result["unavailable"] == "", "an empty directory is not a refusal"
    assert result["scripts"] == []
    assert result["exists"] is True


def test_the_panel_renders_the_refusal_instead_of_the_empty_state() -> None:
    """The front end distinguishes `unavailable` from `exists: false`."""
    panels = Path(__file__).resolve().parent.parent / "static" / "panels.js"
    src = panels.read_text(encoding="utf-8")

    assert "data.unavailable" in src, (
        "the Scripts pane must branch on the `unavailable` field"
    )
    assert "scripts_unavailable" in src, (
        "the refusal state needs its own copy"
    )

    # The refusal branch must come BEFORE the empty-state branch, otherwise
    # a refusal would still render as "no scripts in this profile".
    refusal_at = src.index("data.unavailable")
    empty_at = src.index("!data.exists")
    assert refusal_at < empty_at, (
        "the refusal branch must be checked before the empty-state branch"
    )


def test_the_refusal_copy_lives_in_the_english_locale() -> None:
    """New copy goes in `en` and rides the fallback (GUIDELINES section 8)."""
    i18n = Path(__file__).resolve().parent.parent / "static" / "i18n.js"
    src = i18n.read_text(encoding="utf-8")

    en_start = src.index("\n  en: {")
    en_end = src.index("\n  it: {")
    assert "scripts_unavailable:" in src[en_start:en_end], (
        "the `en` locale must carry the refusal copy"
    )

    # And it must NOT be pasted into the other locale blocks.
    for locale in ("it", "ja", "ru", "es", "de", "zh", "pt", "ko", "fr"):
        start = src.index(f"\n  {locale}: {{")
        end = src.index("\n  ", start + 3)
        assert "scripts_unavailable:" not in src[start:end], (
            f"{locale} must not carry an English copy of scripts_unavailable"
        )
