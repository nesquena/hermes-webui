"""Regression guards for #7151 — profile display_name in the Profiles panel.

Hermes Agent lets the default (and named) profiles carry a presentation-only
``display_name`` in profile.yaml (canonical id unchanged; the CLI renders it
as ``display_name (canonical_id)``). The WebUI was rendering the canonical
``name`` everywhere. These tests pin:

  * /api/profiles includes ``display_name`` in every profile payload row
    (from profile.yaml via the fast path, or from the upstream ProfileInfo
    attr on the slow fallback path), while the canonical ``name`` stays the
    programmatic identity;
  * the panel card, detail title, and compose dropdown render the display
    label instead of the bare canonical name, mirroring the CLI format.
"""
from __future__ import annotations

import re
import subprocess
import sys
import textwrap
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")


def _profile_row(name: str, path: Path, *, is_default: bool = False, display_name: str = ""):
    return SimpleNamespace(
        name=name,
        path=path,
        is_default=is_default,
        display_name=display_name,
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


def _call_list_profiles_api(monkeypatch, rows):
    import api.profiles as profiles

    _install_fake_hermes_profiles(monkeypatch, rows)
    monkeypatch.setattr(profiles, "_get_profile_skills_stats", lambda _path: (0, 0))
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    return profiles.list_profiles_api()


class TestApiPayload:
    def test_display_name_read_from_profile_yaml(self, monkeypatch, tmp_path):
        renamed_default = tmp_path / "profiles" / "default"
        worker = tmp_path / "profiles" / "worker"
        for path in (renamed_default, worker):
            path.mkdir(parents=True)
        (renamed_default / "profile.yaml").write_text(
            "display_name: programmer\nvisible: true\n", encoding="utf-8"
        )

        rows = [
            _profile_row("default", renamed_default, is_default=True),
            _profile_row("worker", worker),
        ]
        result = {row["name"]: row for row in _call_list_profiles_api(monkeypatch, rows)}

        assert result["default"]["display_name"] == "programmer"
        assert result["worker"]["display_name"] == ""
        # Canonical identity is untouched — display_name is presentation-only.
        assert result["default"]["name"] == "default"
        assert result["default"]["is_default"] is True

    def test_display_name_from_upstream_profile_info_attr(self, monkeypatch, tmp_path):
        profile_dir = tmp_path / "profiles" / "default"
        profile_dir.mkdir(parents=True)

        rows = [_profile_row("default", profile_dir, is_default=True, display_name="programmer")]
        result = {row["name"]: row for row in _call_list_profiles_api(monkeypatch, rows)}

        assert result["default"]["display_name"] == "programmer"
        assert result["default"]["name"] == "default"

    def test_display_name_empty_when_unset_or_unreadable(self, monkeypatch, tmp_path):
        missing = tmp_path / "profiles" / "missing-meta"
        malformed = tmp_path / "profiles" / "malformed"
        empty = tmp_path / "profiles" / "empty"
        for path in (missing, malformed, empty):
            path.mkdir(parents=True)
        (malformed / "profile.yaml").write_text("display_name: [\n", encoding="utf-8")
        (empty / "profile.yaml").write_text("display_name: ''\n", encoding="utf-8")

        rows = [
            _profile_row("missing-meta", missing),
            _profile_row("malformed", malformed),
            _profile_row("empty", empty),
        ]
        result = {row["name"]: row for row in _call_list_profiles_api(monkeypatch, rows)}

        assert result["missing-meta"]["display_name"] == ""
        assert result["malformed"]["display_name"] == ""
        assert result["empty"]["display_name"] == ""

    def test_display_name_non_string_falls_back_to_canonical(self, monkeypatch, tmp_path):
        """Review #7156: a non-string display_name (list / int / dict in
        profile.yaml, or a non-string ProfileInfo attr) must render as the
        bare canonical label — never stringified garbage like
        '['friendly'] (default)' or '42 (default)'."""
        cases = {}
        for name, raw in (("list-name", "[friendly]"), ("int-name", "42"),
                          ("dict-name", "{friendly: true}")):
            pdir = tmp_path / "profiles" / name
            pdir.mkdir(parents=True)
            (pdir / "profile.yaml").write_text(
                f"display_name: {raw}\n", encoding="utf-8")
            cases[name] = pdir
        rows = [_profile_row(n, d) for n, d in cases.items()]
        result = {row["name"]: row for row in _call_list_profiles_api(monkeypatch, rows)}

        for name in cases:
            assert result[name]["display_name"] == "", (
                f"non-string display_name in {name} must fall back to '' "
                f"(got {result[name]['display_name']!r})")

    def test_display_name_upstream_attr_non_string_falls_back(self, monkeypatch, tmp_path):
        """Review #7156: same fallback when ProfileInfo.display_name is a
        non-string (upstream attr path)."""
        pdir = tmp_path / "profiles" / "int-attr"
        pdir.mkdir(parents=True)
        rows = [_profile_row("int-attr", pdir, display_name=42)]
        result = {row["name"]: row for row in _call_list_profiles_api(monkeypatch, rows)}
        assert result["int-attr"]["display_name"] == ""

    def test_default_profile_dict_includes_display_name_key(self, monkeypatch):
        import api.profiles as profiles

        monkeypatch.setattr(profiles, "_get_profile_skills_stats", lambda _path: (0, 0))

        row = profiles._default_profile_dict()
        assert isinstance(row["display_name"], str)
        assert row["name"] == "default"


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


class TestPanelRendering:
    def test_label_helper_defined_and_used_by_panel_card(self):
        assert "function _profileDisplayLabel(p){" in PANELS_JS
        body = _function_body(PANELS_JS, "async function loadProfilesPanel()")
        # The card must render the display label, not the bare canonical name.
        assert "${esc(_profileDisplayLabel(p))}" in body
        # ...but keep the canonical name as the programmatic identity.
        assert "card.dataset.name = p.name;" in body
        assert "openProfileDetail(p.name, card)" in body

    def test_detail_title_uses_display_label(self):
        body = _function_body(PANELS_JS, "function _renderProfileDetail(")
        assert "title.textContent = _profileDisplayLabel(p);" in body

    def test_dropdown_option_uses_display_label_but_switches_canonical(self):
        body = _function_body(PANELS_JS, "function renderProfileDropdown(data)")
        assert "${esc(_profileDisplayLabel(p))}" in body
        # Switching must keep using the canonical name (programmatic identity).
        assert "switchToProfile(p.name)" in body

    def test_label_helper_mirrors_cli_format(self):
        # Behavioral check (node): _profileDisplayLabel must render
        # "display_name (canonical_id)" and fall back to the bare canonical id
        # when display_name is unset or equals the id — matching upstream
        # format_profile_label byte-for-byte for the pre-feature rendering.
        helper = _function_body(PANELS_JS, "function _profileDisplayLabel(")
        script = textwrap.dedent(
            f"""
            const assert = require('assert');
            {helper}
            assert.strictEqual(_profileDisplayLabel({{name: 'default', display_name: 'programmer'}}), 'programmer (default)');
            assert.strictEqual(_profileDisplayLabel({{name: 'default', display_name: ''}}), 'default');
            assert.strictEqual(_profileDisplayLabel({{name: 'default', display_name: 'default'}}), 'default');
            assert.strictEqual(_profileDisplayLabel({{name: 'worker', display_name: ''}}), 'worker');
            assert.strictEqual(_profileDisplayLabel({{name: 'worker', display_name: '  Worker  '}}), 'Worker (worker)');
            assert.strictEqual(_profileDisplayLabel({{name: 'default'}}), 'default');
            assert.strictEqual(_profileDisplayLabel(null), '');
            """
        )
        subprocess.run(
            ["node", "-e", script], cwd=REPO_ROOT, check=True, text=True, capture_output=True
        )
