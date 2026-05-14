"""Regression coverage for #749 profile creation model/provider selection."""

from pathlib import Path

import yaml

import api.profiles as profiles


REPO = Path(__file__).resolve().parent.parent
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
ROUTES_PY = (REPO / "api" / "routes.py").read_text(encoding="utf-8")


def test_profile_create_form_exposes_model_picker():
    assert 'id="profileFormModel"' in PANELS_JS
    assert "_populateProfileFormModelSelect" in PANELS_JS
    assert "profile_model_label" in PANELS_JS
    assert "profile_model_hint" in PANELS_JS


def test_profile_create_payload_preserves_provider_context():
    fn_start = PANELS_JS.find("async function saveProfileForm()")
    assert fn_start != -1
    fn_body = PANELS_JS[fn_start : PANELS_JS.find("\n}", fn_start) + 2]
    assert "profileFormModel" in fn_body
    assert "_modelStateForSelect(modelEl, selectedModel)" in fn_body
    assert "payload.default_model" in fn_body
    assert "payload.model_provider" in fn_body


def test_profile_create_route_passes_model_fields_to_profile_api():
    route_start = ROUTES_PY.find('if parsed.path == "/api/profile/create":')
    assert route_start != -1
    route_body = ROUTES_PY[route_start : ROUTES_PY.find('if parsed.path == "/api/profile/delete":', route_start)]
    assert 'default_model = body.get("default_model"' in route_body
    assert 'model_provider = body.get("model_provider"' in route_body
    assert "default_model=default_model" in route_body
    assert "model_provider=model_provider" in route_body


def test_profile_model_config_writer_persists_default_and_provider(tmp_path):
    profile_dir = tmp_path / "profiles" / "research"
    profile_dir.mkdir(parents=True)

    profiles._write_model_defaults_to_config(
        profile_dir,
        default_model="anthropic/claude-opus-4.6",
        model_provider="nous",
    )

    saved = yaml.safe_load((profile_dir / "config.yaml").read_text(encoding="utf-8"))
    assert saved["model"]["default"] == "anthropic/claude-opus-4.6"
    assert saved["model"]["provider"] == "nous"


def test_profile_model_config_writer_preserves_existing_model_settings(tmp_path):
    profile_dir = tmp_path / "profiles" / "research"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text(
        "model:\n  base_url: https://gateway.example/v1\n",
        encoding="utf-8",
    )

    profiles._write_model_defaults_to_config(
        profile_dir,
        default_model="gpt-5.5",
        model_provider="openai-codex",
    )

    saved = yaml.safe_load((profile_dir / "config.yaml").read_text(encoding="utf-8"))
    assert saved["model"]["base_url"] == "https://gateway.example/v1"
    assert saved["model"]["default"] == "gpt-5.5"
    assert saved["model"]["provider"] == "openai-codex"
