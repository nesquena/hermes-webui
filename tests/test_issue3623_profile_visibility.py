from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _profile_row(name: str, path: Path, *, is_default: bool = False):
    return SimpleNamespace(
        name=name,
        path=path,
        is_default=is_default,
        gateway_running=False,
        model=None,
        provider=None,
        has_env=False,
    )


@pytest.fixture(autouse=True)
def _clear_profile_rows_cache():
    import api.profiles as profiles

    profiles._LIST_PROFILES_CACHE = None
    yield
    profiles._LIST_PROFILES_CACHE = None


def _install_fake_hermes_profiles(monkeypatch, rows):
    hermes_cli = types.ModuleType("hermes_cli")
    profiles_mod = types.ModuleType("hermes_cli.profiles")
    profiles_mod.list_profiles = lambda: rows
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", profiles_mod)


def test_profile_yaml_visible_false_is_exposed_as_hidden(monkeypatch, tmp_path):
    import api.profiles as profiles

    hidden = tmp_path / "profiles" / "worker-coder"
    visible = tmp_path / "profiles" / "human"
    missing = tmp_path / "profiles" / "missing-meta"
    malformed = tmp_path / "profiles" / "malformed"
    string_false = tmp_path / "profiles" / "string-false"
    for path in (hidden, visible, missing, malformed, string_false):
        path.mkdir(parents=True)
    (hidden / "profile.yaml").write_text("visible: false\n", encoding="utf-8")
    (visible / "profile.yaml").write_text("visible: true\n", encoding="utf-8")
    (malformed / "profile.yaml").write_text("visible: [\n", encoding="utf-8")
    (string_false / "profile.yaml").write_text('visible: "false"\n', encoding="utf-8")

    rows = [
        _profile_row("worker-coder", hidden),
        _profile_row("human", visible),
        _profile_row("missing-meta", missing),
        _profile_row("malformed", malformed),
        _profile_row("string-false", string_false),
    ]
    _install_fake_hermes_profiles(monkeypatch, rows)
    monkeypatch.setattr(profiles, "_get_profile_skills_stats", lambda _path: (0, 0))
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "human")

    result = {row["name"]: row["visible"] for row in profiles.list_profiles_api()}

    assert result == {
        "worker-coder": False,
        "human": True,
        "missing-meta": True,
        "malformed": True,
        "string-false": True,
    }


def test_default_profile_fallback_stays_visible(monkeypatch):
    import api.profiles as profiles

    monkeypatch.setattr(profiles, "_get_profile_skills_stats", lambda _path: (0, 0))

    assert profiles._default_profile_dict()["visible"] is True


def _panels_js() -> str:
    return (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")


def _function_body(src: str, signature: str) -> str:
    start = src.find(signature)
    assert start != -1, f"{signature} not found"
    i = src.find("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start : j + 1]
    raise AssertionError(f"could not find end of {signature}")


def test_profile_dropdown_filters_hidden_profiles_but_preserves_active():
    body = _function_body(_panels_js(), "function renderProfileDropdown(data)")

    assert "data = data || {};" in body
    assert "const allProfiles = (Array.isArray(data.profiles) ? data.profiles : []).filter(p => p && typeof p.name === 'string');" in body
    assert "allProfiles.some(p => p.name === S.activeProfile)" in body
    assert "const profiles = allProfiles.filter(p => p && (p.visible !== false || p.name === active));" in body


def test_profiles_management_panel_still_renders_all_profiles():
    body = _function_body(_panels_js(), "async function loadProfilesPanel()")

    assert "for (const p of data.profiles)" in body
    assert "visible !== false" not in body
