"""Regression tests for shared OpenCode API key runtime lookup (#3145)."""

import pytest


@pytest.mark.parametrize("provider_id", ["opencode-zen", "opencode-go"])
def test_shared_opencode_api_key_resolves_for_runtime(monkeypatch, tmp_path, provider_id):
    """Runtime-facing key lookup should honor OPENCODE_API_KEY for both groups."""
    import api.providers as providers

    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / ".env").write_text("OPENCODE_API_KEY=shared-opencode-key\n", encoding="utf-8")

    monkeypatch.setattr(providers, "_get_hermes_home", lambda: hermes_home)
    monkeypatch.delenv("OPENCODE_ZEN_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)

    assert providers._provider_has_key(provider_id) is True
    assert providers._get_provider_api_key(provider_id) == "shared-opencode-key"


@pytest.mark.parametrize("provider_id", ["opencode-zen", "opencode-go"])
def test_shared_opencode_api_key_resolves_from_process_env(monkeypatch, tmp_path, provider_id):
    """The shared env var should work even when no .env file exists."""
    import api.providers as providers

    monkeypatch.setattr(providers, "_get_hermes_home", lambda: tmp_path / "missing-home")
    monkeypatch.delenv("OPENCODE_ZEN_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
    monkeypatch.setenv("OPENCODE_API_KEY", "shared-env-opencode-key")

    assert providers._provider_has_key(provider_id) is True
    assert providers._get_provider_api_key(provider_id) == "shared-env-opencode-key"


def test_provider_specific_opencode_key_still_wins_over_shared(monkeypatch, tmp_path):
    """Provider-specific env vars keep precedence over the shared fallback."""
    import api.providers as providers

    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / ".env").write_text(
        "OPENCODE_API_KEY=shared-opencode-key\n"
        "OPENCODE_ZEN_API_KEY=zen-specific-key\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(providers, "_get_hermes_home", lambda: hermes_home)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_ZEN_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)

    assert providers._get_provider_api_key("opencode-zen") == "zen-specific-key"
    assert providers._get_provider_api_key("opencode-go") == "shared-opencode-key"
