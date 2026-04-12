"""Hermes Web UI -- first-run onboarding helpers."""

from pathlib import Path

from api.auth import is_auth_enabled
from api.config import (
    DEFAULT_MODEL,
    DEFAULT_WORKSPACE,
    _HERMES_FOUND,
    _get_config_path,
    get_available_models,
    get_config,
    load_settings,
    save_settings,
    verify_hermes_imports,
)
from api.workspace import get_last_workspace, load_workspaces


def _heuristic_provider_status(cfg: dict) -> tuple[bool, str]:
    """Return a lightweight provider-config heuristic and UI note."""
    model_cfg = cfg.get("model")
    provider_cfg = cfg.get("provider")
    custom_providers = cfg.get("custom_providers")

    configured = bool(model_cfg or provider_cfg or custom_providers)
    note = (
        "Provider readiness is estimated from your Hermes config. "
        "If you still need to finish auth or API keys, run `hermes model` in a terminal."
    )
    return configured, note


def get_onboarding_status() -> dict:
    settings = load_settings()
    cfg = get_config()
    imports_ok, missing, errors = verify_hermes_imports()
    provider_configured, provider_note = _heuristic_provider_status(cfg)
    workspaces = load_workspaces()
    last_workspace = get_last_workspace()
    available_models = get_available_models()

    return {
        "completed": bool(settings.get("onboarding_completed")),
        "settings": {
            "default_model": settings.get("default_model") or DEFAULT_MODEL,
            "default_workspace": settings.get("default_workspace")
            or str(DEFAULT_WORKSPACE),
            "password_enabled": is_auth_enabled(),
            "bot_name": settings.get("bot_name") or "Hermes",
        },
        "system": {
            "hermes_found": bool(_HERMES_FOUND),
            "imports_ok": bool(imports_ok),
            "missing_modules": missing,
            "import_errors": errors,
            "config_path": str(_get_config_path()),
            "config_exists": Path(_get_config_path()).exists(),
            "provider_configured": provider_configured,
            "provider_note": provider_note,
        },
        "workspaces": {
            "items": workspaces,
            "last": last_workspace,
        },
        "models": available_models,
    }


def complete_onboarding() -> dict:
    save_settings({"onboarding_completed": True})
    return get_onboarding_status()
