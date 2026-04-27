import sys
import types
from pathlib import Path


def test_list_profiles_api_falls_back_to_filesystem_when_cli_listing_fails(monkeypatch, tmp_path):
    """Profiles panel should still get data if hermes_cli list_profiles crashes."""
    import api.profiles as profiles

    base = tmp_path / ".hermes"
    named = base / "profiles" / "coder"
    named.mkdir(parents=True)
    (named / "skills").mkdir()
    (named / "config.yaml").write_text(
        "model:\n  default: gpt-5.5\n  provider: openai-codex\n",
        encoding="utf-8",
    )
    (base / ".env").write_text("OPENAI_API_KEY=test\n", encoding="utf-8")

    fake_profiles_mod = types.ModuleType("hermes_cli.profiles")

    def boom():
        raise RuntimeError("bad profile config should not blank the UI list")

    fake_profiles_mod.list_profiles = boom
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", fake_profiles_mod)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_active_profile", "coder")

    data = profiles.list_profiles_api()

    assert [p["name"] for p in data] == ["default", "coder"]
    coder = next(p for p in data if p["name"] == "coder")
    assert coder["is_active"] is True
    assert coder["model"] == "gpt-5.5"
    assert coder["provider"] == "openai-codex"


def test_profiles_menu_label_uses_requested_short_copy():
    i18n = Path("static/i18n.js").read_text(encoding="utf-8")
    assert "tab_profiles: 'profiles : multi agents'" in i18n
