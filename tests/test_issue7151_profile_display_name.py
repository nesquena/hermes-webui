"""Regression tests for issue #7151 — WebUI should show profile display_name.

The agent resolves a profile's friendly name from ``display_name`` in the
profile's ``profile.yaml`` (the CLI shows e.g. ``programmer (default)``), but
the WebUI serialized only the canonical ``name`` and rendered it everywhere.

Fix:
- ``api/profiles.py``: ``_profile_display_name_from_meta()`` reads
  ``display_name`` from each profile's ``profile.yaml``; every serialization
  point in ``list_profiles_api()`` now includes ``display_name``.
- ``api/routes.py``: ``/api/profile/active`` includes ``display_name``.
- Frontend renders ``display_name || name`` (dropdown, chips, titlebar).

Identity-sensitive spots (profile switching, cache lookups) intentionally keep
using the canonical ``name`` — only display surfaces changed.
"""

import pathlib
import re

import api.profiles as profiles  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
PROFILES_PY = (REPO_ROOT / "api" / "profiles.py").read_text(encoding="utf-8")
ROUTES_PY = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Backend: profile rows include display_name
# ---------------------------------------------------------------------------

def test_profile_rows_include_display_name():
    """Every serialized profile row must carry display_name."""
    assert "display_name" in PROFILES_PY
    # Fast path row builder
    fast = PROFILES_PY[PROFILES_PY.index("def _build_profile_rows_fast"):]
    fast_row = fast[fast.index("def _row"):fast.index("rows: list = []")]
    assert "'display_name': _profile_display_name_from_meta" in fast_row
    # Slow fallback rows
    slow = PROFILES_PY[PROFILES_PY.index("def list_profiles_api"):]
    assert slow.count("'display_name':") >= 1


def test_active_profile_endpoint_includes_display_name():
    """/api/profile/active must include display_name."""
    active_block = ROUTES_PY[ROUTES_PY.index('"/api/profile/active"'):]
    assert '"display_name": profiles_api._profile_display_name_from_meta' in active_block


def test_display_name_helper_reads_profile_yaml(tmp_path):
    """_profile_display_name_from_meta reads profile.yaml; never raises."""
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "profile.yaml").write_text("display_name: Programmer\nvisible: true\n")
    assert profiles._profile_display_name_from_meta(prof) == "Programmer"

    # Missing file → ''
    empty = tmp_path / "empty"
    empty.mkdir()
    assert profiles._profile_display_name_from_meta(empty) == ""

    # Corrupt yaml → ''
    (prof / "profile.yaml").write_text("::: not yaml")
    assert profiles._profile_display_name_from_meta(prof) == ""

    # Missing key → ''
    (prof / "profile.yaml").write_text("visible: false\n")
    assert profiles._profile_display_name_from_meta(prof) == ""


# ---------------------------------------------------------------------------
# Frontend: display surfaces use display_name || name
# ---------------------------------------------------------------------------

def test_profile_dropdown_uses_display_name():
    """The compose-footer profile dropdown renders display_name || name."""
    assert "esc(p.display_name||p.name)" in PANELS_JS


def test_sessions_profile_chip_uses_display_name():
    """Session profile chips render display_name || name."""
    assert "p.display_name||p.name" in SESSIONS_JS


def test_boot_keeps_canonical_name_for_identity():
    """Boot must keep canonical name as identity, add profileDisplay for UI."""
    assert "profile: p.name || 'default'" in BOOT_JS
    assert "profileDisplay:" in BOOT_JS
    assert "S.activeProfileDisplay" in BOOT_JS
    # Chip/titlebar labels must prefer the display name.
    assert "S.activeProfileDisplay||S.activeProfile||'default'" in BOOT_JS
    assert "S.activeProfileDisplay||S.activeProfile||'default'" in UI_JS
