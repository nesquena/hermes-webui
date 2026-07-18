"""Expose hermes-agent's toolset configuration (``hermes_cli.tools_config``)
to the WebUI: enable/disable toolsets, provider/model selection, and API key
entry for provider-aware toolsets (TTS, image/video gen, web search, ...) —
the GUI counterpart of ``hermes tools``.

This module is the single integration point with hermes_cli.tools_config,
mirroring the read-only-by-default / integration-point conventions of
api/commands.py. Read endpoints degrade to an "unavailable" response when
hermes-agent isn't installed; write endpoints additionally require the
HERMES_WEBUI_ALLOW_TOOLSET_WRITE env gate — config.yaml and ~/.hermes/.env
are agent-owned state the WebUI should not rewrite without an explicit
operator opt-in.

Scope note: the CLI's "post-setup" step (spawning `hermes tools post-setup
<key>` for hooks like npm-installing a browser backend) is intentionally NOT
mirrored here. It is a long-running, log-tailed background action and
hermes-webui has no generic spawn+log-tail action framework yet (the
dashboard's ``_spawn_hermes_action``/`/api/actions/*` has no WebUI
equivalent) — building one is a separate, larger piece of work. Providers
whose setup requires it can still be selected and keyed through this API;
only the extra install hook is out of scope for this PR.
"""
from __future__ import annotations

import logging
import os
from contextlib import nullcontext
from typing import Any

logger = logging.getLogger(__name__)

_WRITE_GATE_ENV = "HERMES_WEBUI_ALLOW_TOOLSET_WRITE"

# Toolsets with a selectable model catalog, mapped to the config.yaml section
# their `model` key lives in. Mirrors hermes_cli.tools_config's own
# _MODEL_CATALOG_TOOLSETS (image/video gen post-selection model pickers).
_MODEL_CATALOG_TOOLSETS = {
    "image_gen": "image_gen",
    "video_gen": "video_gen",
}


class ToolsetConfigError(Exception):
    """Raised for any toolset-config failure; carries an HTTP status and
    optional extra JSON fields."""

    def __init__(self, message: str, *, status: int = 400, extra: dict | None = None):
        super().__init__(message)
        self.status = status
        self.extra = extra or {}


class ToolsetConfigUnavailable(ToolsetConfigError):
    """hermes_cli.tools_config (or a sibling module it needs) could not be
    imported — hermes-agent is not installed or is too old."""

    def __init__(self, message: str = "Toolset configuration is unavailable (hermes-agent not installed)."):
        super().__init__(message, status=503)


def _write_enabled() -> bool:
    return os.getenv(_WRITE_GATE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _require_write_enabled() -> None:
    if not _write_enabled():
        raise ToolsetConfigError(
            f"Toolset configuration writes are disabled. Set {_WRITE_GATE_ENV}=1 to enable them.",
            status=403,
            extra={"write_gate_env": _WRITE_GATE_ENV},
        )


def _profile_context(purpose: str):
    """Resolve the active-profile env wrapper hermes_cli calls need.

    Mirrors api.commands._bundle_profile_context: hermes_cli.tools_config
    resolves config/env through helpers (``load_config``/``save_config``/
    ``get_env_value``) that read process env / ``get_hermes_home()``
    directly rather than the thread-local-only readonly scope, so the
    write-capable "legacy mirrored" wrapper is required here too.
    """
    try:
        from api.profiles import profile_env_for_active_request
    except ImportError:
        return nullcontext()
    return profile_env_for_active_request(purpose, logger_override=logger)


def _import_tools_config():
    try:
        import hermes_cli.tools_config as _tc

        return _tc
    except ImportError as exc:
        raise ToolsetConfigUnavailable() from exc


def _import_config_module():
    try:
        import hermes_cli.config as _cfg

        return _cfg
    except ImportError as exc:
        raise ToolsetConfigUnavailable() from exc


def _import_platform_label():
    try:
        from hermes_cli.platforms import platform_label

        return platform_label
    except ImportError as exc:
        raise ToolsetConfigUnavailable() from exc


def _resolve_toolset_tools_fn():
    try:
        from toolsets import resolve_toolset

        return resolve_toolset
    except ImportError:
        return None


# ── GET /api/tools/toolsets ─────────────────────────────────────────────


def list_toolsets() -> dict[str, Any]:
    tc = _import_tools_config()
    cfgmod = _import_config_module()
    platform_label = _import_platform_label()
    resolve_toolset = _resolve_toolset_tools_fn()

    with _profile_context("toolsets list"):
        config = cfgmod.load_config()
        toolset_rows = tc._get_effective_configurable_toolsets()
        target_platforms = {tc._toolset_configuration_platform(name) for name, _, _ in toolset_rows}
        enabled_by_platform = {
            platform: set(
                tc._get_platform_tools(config, platform, include_default_mcp_servers=False)
            )
            for platform in target_platforms
        }
        result: list[dict[str, Any]] = []
        for name, label, desc in toolset_rows:
            tools: list[str] = []
            if resolve_toolset is not None:
                try:
                    tools = sorted(set(resolve_toolset(name)))
                except Exception:
                    tools = []
            target_platform = tc._toolset_configuration_platform(name)
            is_enabled = name in enabled_by_platform[target_platform]
            result.append({
                "name": name,
                "label": tc.gui_toolset_label(label),
                "description": desc,
                "platform": target_platform,
                "platform_label": tc.gui_toolset_label(platform_label(target_platform, target_platform)),
                "enabled": is_enabled,
                "configured": tc._toolset_has_keys(name, config),
                "tools": tools,
            })
    return {"toolsets": result, "allowed": _write_enabled(), "write_gate_env": _WRITE_GATE_ENV}


def _valid_toolset_keys(tc) -> set[str]:
    return {ts_key for ts_key, _, _ in tc._get_effective_configurable_toolsets()}


def _require_known_toolset(tc, name: str) -> None:
    if name not in _valid_toolset_keys(tc):
        raise ToolsetConfigError(f"Unknown toolset: {name}", status=400)


# ── PUT /api/tools/toolsets/{name} ──────────────────────────────────────


def toggle_toolset(name: str, enabled: bool) -> dict[str, Any]:
    _require_write_enabled()
    tc = _import_tools_config()
    cfgmod = _import_config_module()

    with _profile_context("toolset toggle"):
        _require_known_toolset(tc, name)
        target_platform = tc._toolset_configuration_platform(name)
        config = cfgmod.load_config()
        enabled_set = set(
            tc._get_platform_tools(config, target_platform, include_default_mcp_servers=False)
        )
        if enabled:
            enabled_set.add(name)
        else:
            enabled_set.discard(name)
        # _save_platform_tools() calls save_config() itself.
        tc._save_platform_tools(config, target_platform, enabled_set)
    return {"ok": True, "name": name, "platform": target_platform, "enabled": bool(enabled)}


# ── GET /api/tools/toolsets/{name}/config ───────────────────────────────


def get_toolset_config(name: str) -> dict[str, Any]:
    tc = _import_tools_config()
    cfgmod = _import_config_module()

    with _profile_context("toolset config read"):
        _require_known_toolset(tc, name)
        config = cfgmod.load_config()
        cat = tc.TOOL_CATEGORIES.get(name)
        providers: list[dict[str, Any]] = []
        active_provider = None
        if cat:
            for prov in tc._visible_providers(cat, config, force_fresh=True):
                env_vars = [
                    {
                        "key": e["key"],
                        "prompt": e.get("prompt", e["key"]),
                        "url": e.get("url"),
                        "default": e.get("default"),
                        "is_set": bool(cfgmod.get_env_value(e["key"])),
                    }
                    for e in prov.get("env_vars", [])
                ]
                is_active = tc._is_provider_active(prov, config, force_fresh=True)
                if is_active and active_provider is None:
                    active_provider = prov["name"]
                providers.append({
                    "name": prov["name"],
                    "badge": prov.get("badge", ""),
                    "tag": prov.get("tag", ""),
                    "env_vars": env_vars,
                    "requires_nous_auth": bool(prov.get("requires_nous_auth")),
                    "is_active": is_active,
                })
    return {
        "name": name,
        "has_category": cat is not None,
        "providers": providers,
        "active_provider": active_provider,
    }


def _find_toolset_provider_row(tc, ts_key: str, config: dict, provider: str | None) -> dict | None:
    cat = tc.TOOL_CATEGORIES.get(ts_key)
    if cat is None:
        return None
    rows = tc._visible_providers(cat, config, force_fresh=True)
    if provider:
        return next((p for p in rows if p.get("name") == provider), None)
    return next((p for p in rows if tc._is_provider_active(p, config, force_fresh=True)), None)


def _resolve_toolset_model_plugin(ts_key: str, provider_row: dict) -> str | None:
    if ts_key == "image_gen":
        return provider_row.get("image_gen_plugin_name") or (
            "fal" if provider_row.get("imagegen_backend") else None
        )
    if ts_key == "video_gen":
        return provider_row.get("video_gen_plugin_name")
    return None


def _toolset_model_catalog(tc, ts_key: str, plugin_name: str):
    if ts_key == "image_gen":
        return tc._plugin_image_gen_catalog(plugin_name)
    return tc._plugin_video_gen_catalog(plugin_name)


# ── PUT /api/tools/toolsets/{name}/provider ─────────────────────────────


def select_toolset_provider(name: str, provider: str) -> dict[str, Any]:
    _require_write_enabled()
    tc = _import_tools_config()
    cfgmod = _import_config_module()

    provider = str(provider or "").strip()
    if not provider:
        raise ToolsetConfigError("provider is required", status=400)

    with _profile_context("toolset provider select"):
        _require_known_toolset(tc, name)
        config = cfgmod.load_config()
        try:
            tc.apply_provider_selection(name, provider, config)
        except KeyError as exc:
            raise ToolsetConfigError(str(exc).strip('"'), status=400) from exc
        cfgmod.save_config(config)
    return {"ok": True, "name": name, "provider": provider}


# ── GET /api/tools/toolsets/{name}/models ───────────────────────────────


def get_toolset_models(name: str, provider: str | None = None) -> dict[str, Any]:
    section = _MODEL_CATALOG_TOOLSETS.get(name)
    if section is None:
        return {"name": name, "has_models": False, "models": [], "current": None, "default": None}

    tc = _import_tools_config()
    cfgmod = _import_config_module()

    with _profile_context("toolset models read"):
        config = cfgmod.load_config()
        row = _find_toolset_provider_row(tc, name, config, provider)
        plugin = _resolve_toolset_model_plugin(name, row) if row else None
        if not plugin:
            return {"name": name, "has_models": False, "models": [], "current": None, "default": None}

        catalog, default_model = _toolset_model_catalog(tc, name, plugin)
        section_cfg = config.get(section)
        current = None
        if isinstance(section_cfg, dict):
            raw = section_cfg.get("model")
            if isinstance(raw, str) and raw.strip():
                current = raw.strip()
        if current not in catalog:
            current = default_model if default_model in catalog else None

    models = [
        {
            "id": model_id,
            "display": meta.get("display", model_id),
            "speed": meta.get("speed", ""),
            "strengths": meta.get("strengths", ""),
            "price": meta.get("price", ""),
        }
        for model_id, meta in catalog.items()
    ]
    return {
        "name": name,
        "has_models": bool(models),
        "provider": row.get("name") if row else None,
        "plugin": plugin,
        "models": models,
        "current": current,
        "default": default_model,
    }


# ── PUT /api/tools/toolsets/{name}/model ────────────────────────────────


def select_toolset_model(name: str, model: str, provider: str | None = None) -> dict[str, Any]:
    _require_write_enabled()
    section = _MODEL_CATALOG_TOOLSETS.get(name)
    if section is None:
        raise ToolsetConfigError(f"Toolset has no model catalog: {name}", status=400)

    model_id = str(model or "").strip()
    if not model_id:
        raise ToolsetConfigError("model is required", status=400)

    tc = _import_tools_config()
    cfgmod = _import_config_module()

    with _profile_context("toolset model select"):
        config = cfgmod.load_config()
        row = _find_toolset_provider_row(tc, name, config, provider)
        plugin = _resolve_toolset_model_plugin(name, row) if row else None
        if not plugin:
            raise ToolsetConfigError(f"No model-capable backend is active for {name}", status=400)

        catalog, _default = _toolset_model_catalog(tc, name, plugin)
        if model_id not in catalog:
            raise ToolsetConfigError(f"Unknown model {model_id!r} for backend {plugin!r}", status=400)

        section_cfg = config.setdefault(section, {})
        if not isinstance(section_cfg, dict):
            section_cfg = {}
            config[section] = section_cfg
        section_cfg["model"] = model_id
        cfgmod.save_config(config)

    return {"ok": True, "name": name, "model": model_id, "plugin": plugin}


# ── PUT /api/tools/toolsets/{name}/env ──────────────────────────────────


def save_toolset_env(name: str, env: dict[str, str]) -> dict[str, Any]:
    _require_write_enabled()
    if not isinstance(env, dict):
        raise ToolsetConfigError("env must be an object of key/value pairs", status=400)

    tc = _import_tools_config()
    cfgmod = _import_config_module()

    with _profile_context("toolset env save"):
        _require_known_toolset(tc, name)
        config = cfgmod.load_config()
        cat = tc.TOOL_CATEGORIES.get(name)
        allowed: set[str] = set()
        if cat:
            for prov in tc._visible_providers(cat, config, force_fresh=True):
                for e in prov.get("env_vars", []):
                    allowed.add(e["key"])

        unknown = [k for k in env if k not in allowed]
        if unknown:
            raise ToolsetConfigError(
                f"Unknown env var(s) for toolset {name}: {', '.join(sorted(unknown))}",
                status=400,
                extra={"unknown_keys": sorted(unknown)},
            )

        saved: list[str] = []
        skipped: list[str] = []
        for key, value in env.items():
            if value and str(value).strip():
                try:
                    cfgmod.save_env_value(key, str(value).strip())
                except ValueError as exc:
                    raise ToolsetConfigError(str(exc), status=400) from exc
                saved.append(key)
            else:
                skipped.append(key)

        status = {k: bool(cfgmod.get_env_value(k)) for k in allowed}
    return {"ok": True, "name": name, "saved": saved, "skipped": skipped, "is_set": status}
