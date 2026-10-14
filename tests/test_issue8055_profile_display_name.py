"""Tests for issue #8055: profile display_name reaches the chip/titlebar/assistant name.

A renamed profile (``profile.yaml`` -> ``display_name``) already showed its
display name in the Profiles panel and the composer dropdown, but the composer
chip, the titlebar label and the assistant name in the transcript still
rendered the canonical id — so the same profile read differently depending on
where you looked.

Fix: ``/api/profiles``, ``/api/profile/active`` and ``/api/profile/switch``
now carry ``display_name``, and the client renders the chip / titlebar /
assistant name from it (falling back to the canonical id when unset). The
canonical id stays the identity for switching and every API call.
"""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
ROUTES_PY = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
PROFILES_PY = (REPO_ROOT / "api" / "profiles.py").read_text(encoding="utf-8")


# ── API surface ────────────────────────────────────────────────────────────


def test_profile_display_name_helper_reads_meta_and_never_raises(tmp_path, monkeypatch):
    """_profile_display_name returns the meta value, '' when unreadable."""
    import sys
    import types

    import api.profiles as p

    (tmp_path / "profile.yaml").write_text("display_name: Base Profile\n", encoding="utf-8")

    # The helper imports hermes_cli.profiles lazily; provide a stub module so
    # the test does not depend on the agent checkout being importable here.
    stub = types.ModuleType("hermes_cli.profiles")
    stub.read_profile_meta = lambda _dir: {"display_name": "Base Profile"}  # noqa: B010
    agent_stub = types.ModuleType("hermes_cli")
    agent_stub.profiles = stub  # noqa: B010
    monkeypatch.setitem(sys.modules, "hermes_cli", agent_stub)
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", stub)

    assert p._profile_display_name(tmp_path) == "Base Profile"

    # Unreadable meta must degrade to '' rather than 500 the whole payload.
    def _boom(_dir):
        raise OSError("nope")

    stub.read_profile_meta = _boom  # noqa: B010
    assert p._profile_display_name(tmp_path) == ""


def test_profile_rows_carry_display_name(monkeypatch):
    """Every list_profiles_api row shape must include the display_name key."""
    import api.profiles as p

    rows = p.list_profiles_api()
    assert rows, "expected at least one profile row"
    for row in rows:
        assert "display_name" in row
        assert isinstance(row["display_name"], str)


def test_active_profile_endpoint_returns_display_name():
    assert '"display_name": profiles_api._profile_display_name(' in ROUTES_PY


def test_switch_profile_response_returns_display_name(monkeypatch, tmp_path):
    """The switch response carries the newly active profile's display name."""
    import api.profiles as p

    profile_dir = tmp_path / "work"
    profile_dir.mkdir()

    monkeypatch.setattr(p, "list_profiles_api", lambda: [
        {"name": "work", "display_name": "Client A", "is_default": False, "path": str(profile_dir)},
    ])
    monkeypatch.setattr(p, "get_active_profile_name", lambda: "work")
    monkeypatch.setattr(p, "_is_root_profile", lambda _n: False)
    monkeypatch.setattr(p, "_resolve_named_profile_home", lambda _n: profile_dir)
    monkeypatch.setattr("api.config.invalidate_models_cache", lambda **kw: None)

    result = p.switch_profile("work", process_wide=False)

    assert result["active"] == "work"
    assert result["display_name"] == "Client A"


# ── Client rendering ───────────────────────────────────────────────────────


def test_assistant_display_name_prefers_profile_display_name():
    """The transcript's assistant name uses the display name when present."""
    idx = UI_JS.find("function assistantDisplayName(){")
    assert idx != -1
    body = UI_JS[idx: idx + 600]
    assert "S.activeProfileDisplayName" in body
    # Canonical id remains the fallback, so unset display names are unchanged.
    assert "S.activeProfile.charAt(0).toUpperCase()" in body


def test_profile_chip_label_helper_prefers_display_name():
    idx = UI_JS.find("function profileChipDisplayLabel(){")
    assert idx != -1
    body = UI_JS[idx: idx + 300]
    assert "S.activeProfileDisplayName" in body
    assert "S.activeProfile||'default'" in body


def test_no_profile_chip_written_from_canonical_id():
    """No surface may write S.activeProfile straight into the chip/titlebar."""
    for name, src in (("boot.js", BOOT_JS), ("ui.js", UI_JS), ("panels.js", PANELS_JS)):
        assert "profileChipLabel" in src, name
    for src in (BOOT_JS, UI_JS):
        assert "profileLabel.textContent=S.activeProfile||'default'" not in src
        assert "_profileLabel.textContent=S.activeProfile||'default'" not in src
        assert "titleLabel.textContent=S.activeProfile||'default'" not in src
        assert "_titleLabel.textContent=S.activeProfile||'default'" not in src


def test_switch_paths_cache_display_name_on_session_state():
    """Both switch entry points read display_name off the API response."""
    assert "S.activeProfileDisplayName = data.display_name" in PANELS_JS
    assert "S.activeProfileDisplayName=data.display_name" in SESSIONS_JS


def test_boot_caches_display_name_from_active_profile():
    assert "S.activeProfileDisplayName = p.display_name" in BOOT_JS


def test_profile_rows_include_display_name_in_fast_and_slow_paths():
    """Both the fast row builder and the upstream fallback carry the field."""
    assert "'display_name': _profile_display_name(home)," in PROFILES_PY
    assert "'display_name': str(getattr(p, 'display_name', '') or '')," in PROFILES_PY
