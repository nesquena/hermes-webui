"""Hermes Web UI -- provider management endpoints.

Provides CRUD operations for configuring provider API keys post-onboarding.
Closes #586 (allow provider key update) and part of #604 (model picker
multi-provider support).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from api.config import (
    _PROVIDER_DISPLAY,
    _PROVIDER_MODELS,
    _get_label_for_model,
    _models_from_live_provider_ids,
    _read_live_provider_model_ids,
    _read_visible_codex_cache_model_ids,
    _save_yaml_config_file,
    get_config,
    invalidate_models_cache,
    reload_config,
)

logger = logging.getLogger(__name__)

_OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
_PROVIDER_QUOTA_TIMEOUT_SECONDS = 3.0
_ACCOUNT_USAGE_SUBPROCESS_TIMEOUT_SECONDS = 35.0
_ACCOUNT_USAGE_PROVIDERS = frozenset({"openai-codex", "anthropic"})
_ACCOUNT_USAGE_SUBPROCESS_CODE = r"""
import json
import sys

from agent.account_usage import fetch_account_usage


def _iso(value):
    if value in (None, ""):
        return None
    if hasattr(value, "isoformat"):
        text = value.isoformat()
        return text.replace("+00:00", "Z")
    text = str(value).strip()
    return text or None


def _snapshot_payload(snapshot):
    if snapshot is None:
        return None
    windows = []
    for window in getattr(snapshot, "windows", ()) or ():
        windows.append({
            "label": str(getattr(window, "label", "") or ""),
            "used_percent": getattr(window, "used_percent", None),
            "reset_at": _iso(getattr(window, "reset_at", None)),
            "detail": getattr(window, "detail", None),
        })
    return {
        "provider": str(getattr(snapshot, "provider", "") or ""),
        "source": str(getattr(snapshot, "source", "") or ""),
        "title": str(getattr(snapshot, "title", "") or ""),
        "plan": getattr(snapshot, "plan", None),
        "windows": windows,
        "details": list(getattr(snapshot, "details", ()) or ()),
        "available": bool(getattr(snapshot, "available", bool(windows))),
        "unavailable_reason": getattr(snapshot, "unavailable_reason", None),
        "fetched_at": _iso(getattr(snapshot, "fetched_at", None)),
    }


provider = sys.argv[1]
api_key = sys.argv[2] or None
print(json.dumps(_snapshot_payload(fetch_account_usage(provider, api_key=api_key))))
"""

# SECTION: Provider ↔ env var mapping

# Maps canonical provider slug → env var name for API key.
# Providers not listed here (OAuth/token-flow providers like copilot, nous,
# openai-codex) cannot have their keys managed from the WebUI.
_PROVIDER_ENV_VAR: dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "zai": "GLM_API_KEY",
    "kimi-coding": "KIMI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "minimax-cn": "MINIMAX_CN_API_KEY",
    "mistralai": "MISTRAL_API_KEY",
    "x-ai": "XAI_API_KEY",
    "opencode-zen": "OPENCODE_ZEN_API_KEY",
    "opencode-go": "OPENCODE_GO_API_KEY",
    # >>> hermes-fork: first-class providers (HermesOS Cloud)
    "venice": "VENICE_API_KEY",
    "crof": "CROF_API_KEY",
    "bankr": "BANKR_API_KEY",
    "xiaomi": "XIAOMI_API_KEY",
    # <<< hermes-fork
    # NOTE: bare "ollama" (local) deliberately omitted — local Ollama is keyless
    # by default and the runtime in hermes_cli/runtime_provider.py only consumes
    # OLLAMA_API_KEY when the base URL hostname is ollama.com (Ollama Cloud).
    # If we mapped both providers to the same env var, configuring Ollama Cloud
    # would falsely flip the local Ollama card to "API key configured" (#1410).
    # Users who genuinely run an authenticated local Ollama can still set a key
    # via providers.ollama.api_key in config.yaml — that path remains supported
    # by _provider_has_key().
    "ollama-cloud": "OLLAMA_API_KEY",
    # Bare "lmstudio" maps to LM_API_KEY — the canonical env var the agent CLI
    # runtime reads (hermes_cli/auth.py:182, api_key_env_vars=("LM_API_KEY",)).
    # Pre-#1499/#1500 the WebUI used LMSTUDIO_API_KEY here, which made Settings
    # report keys correctly but the agent runtime ignored them — masked in
    # practice by the LMSTUDIO_NOAUTH_PLACEHOLDER for keyless local installs.
    # Aligning to LM_API_KEY makes a configured LM Studio key actually work
    # for chat. The legacy LMSTUDIO_API_KEY name is read by `_provider_has_key`
    # via _PROVIDER_ENV_VAR_ALIASES below so existing users don't see Settings
    # flip to "no key" after upgrading.
    "lmstudio": "LM_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
}

# Read-only legacy env-var aliases.  When `_provider_has_key(pid)` looks up its
# canonical env var name and finds nothing, it also checks any aliases listed
# here.  Onboarding (api/onboarding.py:apply_onboarding_setup) only writes the
# canonical name.  Use this for env vars that were renamed in a past release;
# add an entry, ship for a few releases, then remove the alias once enough
# users have upgraded.
_PROVIDER_ENV_VAR_ALIASES: dict[str, tuple[str, ...]] = {
    # #1500 — agent runtime reads LM_API_KEY (canonical), but WebUI builds
    # ≤ v0.50.272 wrote LMSTUDIO_API_KEY into .env.  Keep reading both.
    "lmstudio": ("LMSTUDIO_API_KEY",),
}

# Providers that use OAuth or token flows — their credentials are managed
# through the Hermes CLI, not via API keys.  The WebUI cannot set these.
_OAUTH_PROVIDERS = frozenset({
    "copilot",
    "copilot-acp",
    "nous",
    "openai-codex",
    "qwen-oauth",
})

# >>> hermes-fork: provider logo color hint (HermesOS Cloud)
# Frontend renders a deterministic first-letter glyph as the provider icon
# (avoids any external CDN dependency). The color is a per-provider hue used
# for the glyph background gradient — looks like a brand swatch without
# fetching anything. Falls back to a neutral gradient when not listed here.
_PROVIDER_LOGO_HUE: dict[str, int] = {
    "venice":       12,    # red-orange
    "crof":         200,   # cyan
    "bankr":        140,   # green
    "xiaomi":       18,    # orange
    "anthropic":    28,    # warm tan
    "openai":       150,   # green
    "openai-codex": 150,
    "openrouter":   270,   # purple
    "deepseek":     220,   # blue
    "google":       210,   # google blue
    "gemini":       210,
    "x-ai":         0,     # red (xAI)
    "mistralai":    20,
    "nvidia":       100,   # nvidia green
    "alibaba":      30,
    "huggingface":  45,    # yellow
    "ollama":       180,
    "ollama-cloud": 180,
    "minimax":      280,
    "minimax-cn":   280,
    "kimi-coding":  220,
    "zai":          330,
    "qwen":         260,
    "qwen-oauth":   260,
    "lmstudio":     200,
    "copilot":      0,     # github black -> neutral hue
    "copilot-acp":  0,
    "nous":         260,   # purple-ish for nous
    "meta-llama":   210,
    "opencode-zen": 290,
    "opencode-go":  290,
}
# <<< hermes-fork

# SECTION: Helper functions


def _get_hermes_home() -> Path:
    """Return the active Hermes home directory."""
    try:
        from api.profiles import get_active_hermes_home
        return get_active_hermes_home()
    except ImportError:
        return Path.home() / ".hermes"


def _load_env_file(env_path: Path) -> dict[str, str]:
    """Read key=value pairs from a .env file."""
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    except Exception:
        return {}
    return values


def _write_env_file(env_path: Path, updates: dict[str, str | None]) -> None:
    """Write key=value pairs to the .env file.

    Values of ``None`` cause the key to be removed.

    Preserves comments, blank lines, and original key order (#1164).
    New keys are appended at the end of the file with a blank-line separator.

    Holds ``_ENV_LOCK`` from ``api.streaming`` for the entire load → modify →
    write cycle to prevent TOCTOU races between concurrent POST /api/providers
    calls (each reading the same file baseline and overwriting the other's key).
    Also serialises os.environ mutations with streaming sessions.
    """
    from api.streaming import _ENV_LOCK
    import stat as _stat

    with _ENV_LOCK:
        # ── Read existing lines (preserving comments and blank lines) ──
        existing_lines: list[str] = []
        if env_path.exists():
            try:
                existing_lines = env_path.read_text(encoding="utf-8").splitlines()
            except Exception:
                existing_lines = []

        # Map each existing key to its line index so we can update in-place.
        existing_key_indices: dict[str, int] = {}
        for _i, _raw in enumerate(existing_lines):
            _stripped = _raw.strip()
            if _stripped and not _stripped.startswith("#") and "=" in _stripped:
                _existing_key_indices_key = _stripped.split("=", 1)[0].strip()
                existing_key_indices[_existing_key_indices_key] = _i

        output_lines = list(existing_lines)
        new_keys: list[str] = []

        for key, value in updates.items():
            if value is None:
                # Mark the line for removal (None sentinel) and clear env.
                os.environ.pop(key, None)
                if key in existing_key_indices:
                    output_lines[existing_key_indices[key]] = None  # type: ignore[assignment]
                continue
            clean = str(value).strip()
            if not clean:
                continue
            # Reject embedded newlines/carriage returns to prevent .env injection
            if "\n" in clean or "\r" in clean:
                raise ValueError("API key must not contain newline characters.")
            os.environ[key] = clean

            if key in existing_key_indices:
                output_lines[existing_key_indices[key]] = f"{key}={clean}"
            else:
                new_keys.append(f"{key}={clean}")

        # Remove deleted lines (None sentinels)
        output_lines = [l for l in output_lines if l is not None]

        # Append new keys after a blank-line separator
        if new_keys:
            if output_lines and output_lines[-1].strip() != "":
                output_lines.append("")
            output_lines.extend(new_keys)

        env_path.parent.mkdir(parents=True, exist_ok=True)
        content = "\n".join(output_lines)
        if content:
            content += "\n"
        # Atomic write via tempfile + os.replace so cross-process readers
        # (Telegram bot, CLI) never see a half-truncated file.  The shared
        # ``~/.hermes/.env`` is also written by ``hermes_cli.config.save_env_value``
        # using the same atomic pattern; matching it here closes the
        # cross-process leg of #1164 (within-process is covered by _ENV_LOCK).
        _mode = _stat.S_IRUSR | _stat.S_IWUSR  # 0o600
        import tempfile as _tempfile
        _tmp_fd, _tmp_path = _tempfile.mkstemp(
            dir=str(env_path.parent), prefix=".env_", suffix=".tmp"
        )
        try:
            with os.fdopen(_tmp_fd, "w", encoding="utf-8") as _f:
                _f.write(content)
                _f.flush()
                os.fsync(_f.fileno())
            os.chmod(_tmp_path, _mode)  # tighten before rename so readers see 0600
            os.replace(_tmp_path, env_path)
        except BaseException:
            try:
                os.unlink(_tmp_path)
            except OSError:
                pass
            raise
        try:
            env_path.chmod(_mode)
        except OSError:
            pass


def _provider_has_key(provider_id: str) -> bool:
    """Check whether a provider has a configured API key.

    Checks (in order):
    1. ``~/.hermes/.env`` for the known env var
    2. ``os.environ`` for the known env var
    3. ``config.yaml → model.api_key`` (only if provider is the active one)
    4. ``config.yaml → providers.<id>.api_key``
    5. ``config.yaml → custom_providers[].api_key`` (for custom providers)
    """
    env_var = _PROVIDER_ENV_VAR.get(provider_id)
    if env_var:
        env_path = _get_hermes_home() / ".env"
        env_values = _load_env_file(env_path)
        if env_values.get(env_var):
            return True
        if os.getenv(env_var):
            return True
        # Fall back to legacy env-var aliases (e.g. lmstudio's pre-#1500
        # LMSTUDIO_API_KEY name) so existing users don't lose detection
        # after an env-var rename.  See _PROVIDER_ENV_VAR_ALIASES.
        for alias in _PROVIDER_ENV_VAR_ALIASES.get(provider_id, ()) or ():
            if env_values.get(alias):
                return True
            if os.getenv(alias):
                return True

    cfg = get_config()
    # Check model.api_key — only match if this provider is the active one.
    # Previously this checked globally, causing all providers to show
    # "configured" when the active provider had a top-level api_key.
    model_cfg = cfg.get("model", {})
    if isinstance(model_cfg, dict) and str(model_cfg.get("api_key") or "").strip():
        active_provider = model_cfg.get("provider")
        if active_provider and str(active_provider).strip().lower() == provider_id.lower():
            return True
    # Check providers.<id>.api_key
    providers_cfg = cfg.get("providers", {})
    if isinstance(providers_cfg, dict):
        provider_cfg = providers_cfg.get(provider_id, {})
        if isinstance(provider_cfg, dict) and str(provider_cfg.get("api_key") or "").strip():
            return True
    # Check custom_providers
    custom_providers = cfg.get("custom_providers", [])
    if isinstance(custom_providers, list):
        for cp in custom_providers:
            if isinstance(cp, dict):
                cp_name = (cp.get("name") or "").strip().lower().replace(" ", "-")
                if f"custom:{cp_name}" == provider_id or cp.get("name", "").strip().lower() == provider_id:
                    if str(cp.get("api_key") or "").strip():
                        return True
    return False


def _get_provider_api_key(provider_id: str) -> str | None:
    """Return a configured provider API key without exposing it to callers."""
    provider_id = (provider_id or "").strip().lower()
    env_var = _PROVIDER_ENV_VAR.get(provider_id)
    if env_var:
        env_path = _get_hermes_home() / ".env"
        env_values = _load_env_file(env_path)
        if env_values.get(env_var):
            return str(env_values[env_var]).strip() or None
        if os.getenv(env_var):
            return os.getenv(env_var, "").strip() or None
        for alias in _PROVIDER_ENV_VAR_ALIASES.get(provider_id, ()) or ():
            if env_values.get(alias):
                return str(env_values[alias]).strip() or None
            if os.getenv(alias):
                return os.getenv(alias, "").strip() or None

    cfg = get_config()
    model_cfg = cfg.get("model", {})
    if isinstance(model_cfg, dict):
        active_provider = str(model_cfg.get("provider") or "").strip().lower()
        model_key = str(model_cfg.get("api_key") or "").strip()
        if model_key and active_provider == provider_id:
            return model_key

    providers_cfg = cfg.get("providers", {})
    if isinstance(providers_cfg, dict):
        provider_cfg = providers_cfg.get(provider_id, {})
        if isinstance(provider_cfg, dict):
            provider_key = str(provider_cfg.get("api_key") or "").strip()
            if provider_key:
                return provider_key

    custom_providers = cfg.get("custom_providers", [])
    if isinstance(custom_providers, list):
        for cp in custom_providers:
            if not isinstance(cp, dict):
                continue
            cp_name = str(cp.get("name") or "").strip().lower().replace(" ", "-")
            if f"custom:{cp_name}" == provider_id or str(cp.get("name", "")).strip().lower() == provider_id:
                cp_key = str(cp.get("api_key") or "").strip()
                if cp_key.startswith("${") and cp_key.endswith("}"):
                    return os.getenv(cp_key[2:-1], "").strip() or None
                if cp_key:
                    return cp_key
    return None


def _active_provider_id() -> str | None:
    cfg = get_config()
    model_cfg = cfg.get("model", {})
    if not isinstance(model_cfg, dict):
        return None
    provider = str(model_cfg.get("provider") or "").strip().lower()
    return provider or None


def _quota_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        text = str(value).strip()
        if not text:
            return None
        number = float(text)
        return int(number) if number.is_integer() else number
    except (TypeError, ValueError):
        return None


def _sanitize_openrouter_quota(payload: Any) -> dict[str, int | float | None]:
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    if not isinstance(payload, dict):
        payload = {}
    return {
        "limit_remaining": _quota_number(payload.get("limit_remaining")),
        "usage": _quota_number(payload.get("usage")),
        "limit": _quota_number(payload.get("limit")),
    }


def _isoformat_utc(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    text = str(value).strip()
    return text or None


def _serialize_account_usage_snapshot(snapshot: Any) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    windows: list[dict[str, Any]] = []
    for window in getattr(snapshot, "windows", ()) or ():
        label = str(getattr(window, "label", "") or "").strip()
        if not label:
            continue
        used_percent = _quota_number(getattr(window, "used_percent", None))
        remaining_percent = None
        if used_percent is not None:
            remaining_percent = max(0.0, min(100.0, 100.0 - float(used_percent)))
        windows.append({
            "label": label,
            "used_percent": used_percent,
            "remaining_percent": remaining_percent,
            "reset_at": _isoformat_utc(getattr(window, "reset_at", None)),
            "detail": str(getattr(window, "detail", "") or "").strip() or None,
        })

    details = [
        str(detail).strip()
        for detail in (getattr(snapshot, "details", ()) or ())
        if str(detail).strip()
    ]
    plan = str(getattr(snapshot, "plan", "") or "").strip() or None
    unavailable_reason = str(getattr(snapshot, "unavailable_reason", "") or "").strip() or None
    return {
        "provider": str(getattr(snapshot, "provider", "") or "").strip() or None,
        "source": str(getattr(snapshot, "source", "") or "").strip() or None,
        "title": str(getattr(snapshot, "title", "") or "").strip() or "Account limits",
        "plan": plan,
        "windows": windows,
        "details": details,
        "available": bool(getattr(snapshot, "available", bool(windows or details))) and not unavailable_reason,
        "unavailable_reason": unavailable_reason,
        "fetched_at": _isoformat_utc(getattr(snapshot, "fetched_at", None)),
    }


def _agent_fetch_account_usage(provider: str, *, base_url: str | None = None, api_key: str | None = None) -> Any:
    from agent.account_usage import fetch_account_usage

    return fetch_account_usage(provider, base_url=base_url, api_key=api_key)


def _account_usage_subprocess_env(home: Path, provider: str, api_key: str | None) -> dict[str, str]:
    env = dict(os.environ)
    env["HERMES_HOME"] = str(Path(home))

    # Profile .env values should affect only the child quota probe, not the
    # WebUI process-global environment. This is especially important for
    # Anthropic account usage, where the agent resolver reads OAuth/API tokens
    # from environment variables.
    for key, value in _load_env_file(Path(home) / ".env").items():
        if value:
            env[key] = value

    env_var = _PROVIDER_ENV_VAR.get((provider or "").strip().lower())
    if env_var and api_key:
        env[env_var] = api_key

    try:
        from api.config import _AGENT_DIR
    except Exception:
        _AGENT_DIR = None
    pythonpath_parts: list[str] = []
    if _AGENT_DIR:
        pythonpath_parts.append(str(_AGENT_DIR))
    existing_pythonpath = env.get("PYTHONPATH", "")
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    if pythonpath_parts:
        env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    return env


def _account_usage_payload_to_snapshot(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return None
    windows = tuple(
        SimpleNamespace(
            label=window.get("label"),
            used_percent=window.get("used_percent"),
            reset_at=window.get("reset_at"),
            detail=window.get("detail"),
        )
        for window in (payload.get("windows") or ())
        if isinstance(window, dict)
    )
    return SimpleNamespace(
        provider=payload.get("provider"),
        source=payload.get("source"),
        title=payload.get("title"),
        plan=payload.get("plan"),
        windows=windows,
        details=tuple(payload.get("details") or ()),
        available=bool(payload.get("available")),
        unavailable_reason=payload.get("unavailable_reason"),
        fetched_at=payload.get("fetched_at"),
    )


def _agent_fetch_account_usage_for_home(provider: str, home: Path, *, api_key: str | None = None) -> Any:
    try:
        from api.config import PYTHON_EXE
    except Exception:
        PYTHON_EXE = sys.executable or "python3"

    try:
        proc = subprocess.run(
            [PYTHON_EXE, "-c", _ACCOUNT_USAGE_SUBPROCESS_CODE, provider, api_key or ""],
            env=_account_usage_subprocess_env(home, provider, api_key),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=_ACCOUNT_USAGE_SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.debug("Account usage probe for %s timed out", provider)
        return None
    except Exception:
        logger.debug("Account usage probe for %s failed to launch", provider, exc_info=True)
        return None

    if proc.returncode != 0:
        logger.debug("Account usage probe for %s exited with status %s", provider, proc.returncode)
        return None
    try:
        payload = json.loads((proc.stdout or "").strip() or "null")
    except json.JSONDecodeError:
        logger.debug("Account usage probe for %s returned invalid JSON", provider)
        return None
    return _account_usage_payload_to_snapshot(payload)


def _fetch_account_usage_with_profile_context(provider: str) -> Any:
    home = _get_hermes_home()
    api_key = _get_provider_api_key(provider)
    try:
        return _agent_fetch_account_usage_for_home(provider, home, api_key=api_key)
    except Exception:
        logger.debug("Failed to fetch account usage for %s", provider, exc_info=True)
        return None


def _provider_account_usage_status(provider: str, display_name: str) -> dict[str, Any]:
    snapshot = _fetch_account_usage_with_profile_context(provider)
    account_limits = _serialize_account_usage_snapshot(snapshot)
    if account_limits and account_limits.get("available"):
        return {
            "ok": True,
            "provider": provider,
            "display_name": display_name,
            "supported": True,
            "status": "available",
            "label": account_limits.get("title") or "Account limits",
            "quota": None,
            "account_limits": account_limits,
            "message": f"{display_name} account limits loaded.",
        }

    reason = ""
    if account_limits:
        reason = str(account_limits.get("unavailable_reason") or "").strip()
    message = (
        f"{display_name} account limits are unavailable. {reason}"
        if reason
        else f"{display_name} account limits are unavailable. Confirm provider authentication and try again."
    )
    return {
        "ok": False,
        "provider": provider,
        "display_name": display_name,
        "supported": True,
        "status": "unavailable",
        "quota": None,
        "account_limits": account_limits,
        "message": message,
    }


def get_provider_quota(provider_id: str | None = None) -> dict[str, Any]:
    """Return sanitized quota/rate-limit status for the active provider.

    OpenRouter keeps its documented key endpoint. OAuth-backed account usage
    providers reuse Hermes Agent's /usage account-limits abstraction so WebUI
    stays aligned with CLI/Gateway provider semantics.
    """
    provider = (provider_id or _active_provider_id() or "").strip().lower()
    if not provider:
        return {
            "ok": False,
            "provider": None,
            "display_name": None,
            "supported": False,
            "status": "unavailable",
            "quota": None,
            "message": "No active provider is configured.",
        }

    display_name = _PROVIDER_DISPLAY.get(provider, provider.replace("-", " ").title())
    if provider in _ACCOUNT_USAGE_PROVIDERS:
        return _provider_account_usage_status(provider, display_name)

    if provider != "openrouter":
        detail = "OpenAI/Anthropic rate-limit headers are a follow-up once WebUI captures provider response metadata."
        return {
            "ok": False,
            "provider": provider,
            "display_name": display_name,
            "supported": False,
            "status": "unsupported",
            "quota": None,
            "message": f"Quota status is not available for {display_name}. {detail}",
        }

    api_key = _get_provider_api_key("openrouter")
    if not api_key:
        return {
            "ok": False,
            "provider": "openrouter",
            "display_name": display_name,
            "supported": True,
            "status": "no_key",
            "quota": None,
            "message": "OpenRouter quota status needs an OPENROUTER_API_KEY configured on the server.",
        }

    req = urllib.request.Request(
        _OPENROUTER_KEY_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_PROVIDER_QUOTA_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
        payload = json.loads(raw.decode("utf-8")) if isinstance(raw, (bytes, bytearray)) else json.loads(raw)
        quota = _sanitize_openrouter_quota(payload)
        return {
            "ok": True,
            "provider": "openrouter",
            "display_name": display_name,
            "supported": True,
            "status": "available",
            "label": "OpenRouter credits",
            "quota": quota,
            "message": "OpenRouter quota status loaded.",
        }
    except urllib.error.HTTPError as exc:
        status = "invalid_key" if exc.code in (401, 403) else "unavailable"
        message = (
            "OpenRouter rejected the configured API key."
            if status == "invalid_key"
            else "OpenRouter quota status is temporarily unavailable."
        )
        return {
            "ok": False,
            "provider": "openrouter",
            "display_name": display_name,
            "supported": True,
            "status": status,
            "quota": None,
            "message": message,
        }
    except (TimeoutError, urllib.error.URLError, json.JSONDecodeError, OSError, ValueError):
        return {
            "ok": False,
            "provider": "openrouter",
            "display_name": display_name,
            "supported": True,
            "status": "unavailable",
            "quota": None,
            "message": "OpenRouter quota status is temporarily unavailable.",
        }


def _provider_is_oauth(provider_id: str) -> bool:
    """Check whether a provider uses OAuth/token flows (managed by CLI)."""
    return provider_id in _OAUTH_PROVIDERS


# SECTION: Public API


def get_providers() -> dict[str, Any]:
    """Return a list of all known providers with their configuration status.

    Each entry contains:
    - ``id``: canonical provider slug
    - ``display_name``: human-readable name
    - ``has_key``: whether an API key is configured
    - ``configurable``: whether the key can be set from the WebUI
    - ``key_source``: where the key was found (``env_file``, ``env_var``,
      ``config_yaml``, ``oauth``, ``none``)
    - ``models``: list of known model IDs for this provider
    """
    providers = []

    # Collect all known provider IDs from multiple sources
    known_ids = set(_PROVIDER_DISPLAY.keys()) | set(_PROVIDER_MODELS.keys())

    # Also detect providers from config.yaml providers section
    cfg = get_config()
    providers_cfg = cfg.get("providers", {})
    if isinstance(providers_cfg, dict):
        known_ids.update(providers_cfg.keys())

    # Add OAuth providers even if not in _PROVIDER_DISPLAY
    known_ids.update(_OAUTH_PROVIDERS)

    for pid in sorted(known_ids):
        display_name = _PROVIDER_DISPLAY.get(pid, pid.replace("-", " ").title())
        is_oauth = _provider_is_oauth(pid)
        has_key = _provider_has_key(pid)

        # Determine key source
        key_source = "none"
        auth_error = None
        if is_oauth:
            key_source = "oauth"
            # Check if actually authenticated via hermes_cli.
            # IMPORTANT: do not unconditionally overwrite has_key from _provider_has_key().
            # A token in config.yaml is a valid credential even when get_auth_status()
            # returns logged_in=False (e.g. token not in the hermes credential pool,
            # or refresh token consumed by native Codex CLI / VS Code extension).
            try:
                from hermes_cli.auth import get_auth_status as _gas
                status = _gas(pid)
                if isinstance(status, dict) and status.get("logged_in"):
                    has_key = True
                    key_source = status.get("key_source", "oauth")
                elif has_key:
                    # _provider_has_key() found a token in config.yaml — respect it
                    # rather than hiding a working credential from the Settings UI.
                    key_source = "config_yaml"
                    auth_error = status.get("error") if isinstance(status, dict) else None
                else:
                    has_key = False
                    auth_error = status.get("error") if isinstance(status, dict) else None
            except Exception:
                # Import failed or auth check errored — don't override a known-good
                # key just because the hermes_cli auth module is unavailable.
                logger.debug("hermes_cli auth check failed for %s", pid, exc_info=True)
                # keep has_key from _provider_has_key()
        elif has_key:
            env_var = _PROVIDER_ENV_VAR.get(pid)
            if env_var:
                env_path = _get_hermes_home() / ".env"
                env_values = _load_env_file(env_path)
                if env_values.get(env_var):
                    key_source = "env_file"
                elif os.getenv(env_var):
                    key_source = "env_var"
                else:
                    # Canonical name not set; check legacy aliases (e.g. lmstudio's
                    # pre-#1500 LMSTUDIO_API_KEY) so existing users see "env_file"
                    # instead of being misreported as "config_yaml" when the key
                    # actually lives in .env under the old name.
                    aliased = False
                    for alias in _PROVIDER_ENV_VAR_ALIASES.get(pid, ()) or ():
                        if env_values.get(alias):
                            key_source = "env_file"
                            aliased = True
                            break
                        if os.getenv(alias):
                            key_source = "env_var"
                            aliased = True
                            break
                    if not aliased:
                        key_source = "config_yaml"
            else:
                key_source = "config_yaml"
        elif pid not in _PROVIDER_ENV_VAR:
            # Fallback: provider is not a known API-key provider and not in
            # the hardcoded _OAUTH_PROVIDERS set.  It may be a custom or
            # newly-added OAuth provider (e.g. Anthropic connected via OAuth).
            # Check live auth status so the Providers tab agrees with the
            # model picker (#1212).
            #
            # IMPORTANT: we skip providers in _PROVIDER_ENV_VAR because they
            # are pure API-key providers — calling get_auth_status() for every
            # unconfigured API-key provider would add unnecessary latency
            # (network round-trip per provider) on the Settings page.
            # Validate pid looks like a real provider before probing
            import re as _re
            if _re.match(r'^[a-z][a-z0-9_-]{0,63}$', pid):
                try:
                    from hermes_cli.auth import get_auth_status as _gas
                    status = _gas(pid)
                    if isinstance(status, dict) and status.get("logged_in"):
                        has_key = True
                        # Constrain key_source to a known-safe closed set
                        _raw_ks = status.get("key_source", "")
                        key_source = _raw_ks if _raw_ks in {"oauth", "env", "config", "token"} else "oauth"
                        is_oauth = True
                except Exception:
                    pass

        models = list(_PROVIDER_MODELS.get(pid, []))
        models_total = len(models)
        # OpenAI Codex account catalogs drift independently from WebUI releases.
        # The model picker already prefers hermes_cli + Codex local cache for
        # this provider (the agent's `provider_model_ids("openai-codex")` filters
        # IDs with `supported_in_api: false`, but Codex CLI still surfaces some
        # of those — notably `gpt-5.3-codex-spark` from #1680 — in its picker).
        # Merge both sources here so the providers card matches the picker
        # exactly. Static entries remain the offline fallback when live
        # discovery and the local Codex cache are both unavailable. (#1807
        # follow-up to v0.51.19 #1812.)
        if pid == "openai-codex":
            live_ids = _read_live_provider_model_ids("openai-codex")
            for mid in _read_visible_codex_cache_model_ids():
                if mid not in live_ids:
                    live_ids.append(mid)
            live_models = _models_from_live_provider_ids(pid, live_ids)
            if live_models:
                models = live_models
                models_total = len(models)
        # Nous Portal: prefer the live catalog so the providers card matches
        # the dropdown picker (#1538). Same fallback shape as the static-only
        # case below — when hermes_cli is unavailable or its lookup raises,
        # we keep the four-entry curated list.
        #
        # On large-tier accounts (#1567 reporter Deor saw 396 entries), we
        # render the same featured subset the picker uses so the providers
        # card body doesn't become a 396-pill wall. The full count is still
        # reported via models_total — surfaced in the header line as
        # "396 models · OAuth" by static/panels.js — so the user knows the
        # complete catalog is reachable (via /model autocomplete or a future
        # "show all" disclosure if added).
        if pid == "nous":
            try:
                from hermes_cli.models import provider_model_ids as _provider_model_ids

                live_ids = _provider_model_ids("nous") or []
                if live_ids:
                    # Lazy-import to avoid circular dep with api.config.
                    from api.config import _format_nous_label, _build_nous_featured_set

                    featured_ids, _extras = _build_nous_featured_set(live_ids)
                    models = [
                        {"id": f"@nous:{mid}", "label": _format_nous_label(mid)}
                        for mid in featured_ids
                    ]
                    models_total = len(live_ids)
            except Exception:
                logger.debug("Failed to load Nous Portal models from hermes_cli")
        # Also include models from config.yaml providers section
        if isinstance(providers_cfg, dict):
            provider_cfg = providers_cfg.get(pid, {})
            if isinstance(provider_cfg, dict) and "models" in provider_cfg:
                cfg_models = provider_cfg["models"]
                if isinstance(cfg_models, dict):
                    models = models + [{"id": k, "label": k} for k in cfg_models.keys()]
                elif isinstance(cfg_models, list):
                    models = models + [{"id": k, "label": k} for k in cfg_models]
                # Recompute models_total when config.yaml contributes additional
                # entries on top of the live/static catalog. For non-Nous
                # providers models_total still equals len(models); for Nous
                # we keep the live count (which already includes any models
                # surfaced in the curated featured slice).
                if pid != "nous":
                    models_total = len(models)

        providers.append({
            "id": pid,
            "display_name": display_name,
            "has_key": has_key,
            "configurable": not is_oauth and pid in _PROVIDER_ENV_VAR,
            "is_oauth": is_oauth,
            "key_source": key_source,
            "auth_error": auth_error,
            # >>> hermes-fork: provider logo (color hint for first-letter glyph)
            "logo_hue": _PROVIDER_LOGO_HUE.get(pid),
            # <<< hermes-fork
            "models": models,
            # models_total reflects the complete catalog size (e.g. 396 for
            # an enterprise Nous Portal account), even when "models" is
            # trimmed to a featured subset for UI scannability. The frontend
            # uses this for the header text "396 models · OAuth" so users
            # know the full catalog exists and is reachable via the slash
            # command. For providers that don't trim, models_total ==
            # len(models) and the frontend behaves identically to before.
            "models_total": models_total,
        })

    # Scan custom_providers from config.yaml (e.g. glmcode, timicc)
    custom_providers_cfg = cfg.get("custom_providers", [])
    if isinstance(custom_providers_cfg, list):
        for cp in custom_providers_cfg:
            if not isinstance(cp, dict) or not cp.get("name"):
                continue
            cp_name = str(cp["name"]).strip()
            cp_id = f"custom:{cp_name}"
            # Collect models from `models` list or `model` single
            cp_models = []
            if isinstance(cp.get("models"), list):
                cp_models = [{"id": str(m), "label": str(m)} for m in cp["models"]]
            elif cp.get("model"):
                cp_models = [{"id": cp["model"], "label": cp["model"]}]
            # Check for env var reference (${VAR_NAME} pattern)
            cp_api_key = str(cp.get("api_key") or "")
            cp_has_key = bool(cp_api_key.strip())
            # Replace env var reference to check actual value
            if cp_api_key.startswith("${") and cp_api_key.endswith("}"):
                env_var = cp_api_key[2:-1]
                cp_has_key = bool(os.getenv(env_var, "").strip())
            providers.append({
                "id": cp_id,
                "display_name": cp_name,
                # >>> hermes-fork: surface custom_providers as configurable
                "has_key": cp_has_key,
                "configurable": True,
                "is_oauth": False,
                "logo_hue": _PROVIDER_LOGO_HUE.get(cp_name.lower().replace(" ", "")),
                # <<< hermes-fork
                "key_source": "config_yaml" if cp_has_key else "none",
                "models": cp_models,
            })

    # Determine active provider
    active_provider = None
    model_cfg = cfg.get("model", {})
    if isinstance(model_cfg, dict):
        active_provider = model_cfg.get("provider")

    return {
        "providers": providers,
        "active_provider": active_provider,
    }


def set_provider_key(provider_id: str, api_key: str | None) -> dict[str, Any]:
    """Set or update the API key for a provider.

    Writes the key to ``~/.hermes/.env`` using the standard env var name.
    If ``api_key`` is None or empty, the key is removed.

    Returns a status dict with the operation result.
    """
    provider_id = provider_id.strip().lower()

    if not provider_id:
        return {"ok": False, "error": "Provider ID is required."}

    if _provider_is_oauth(provider_id):
        return {
            "ok": False,
            "error": f"'{_PROVIDER_DISPLAY.get(provider_id, provider_id)}' uses OAuth authentication. "
                     f"Use `hermes model` in the terminal to configure it.",
        }

    env_var = _PROVIDER_ENV_VAR.get(provider_id)
    if not env_var:
        return {
            "ok": False,
            "error": f"Cannot configure API key for '{_PROVIDER_DISPLAY.get(provider_id, provider_id)}'. "
                     f"This provider does not have a known env var mapping.",
        }

    # Validate API key format (basic sanity check)
    if api_key:
        api_key = api_key.strip()
        if "\n" in api_key or "\r" in api_key:
            return {"ok": False, "error": "API key must not contain newline characters."}
        if len(api_key) < 8:
            return {"ok": False, "error": "API key appears too short."}

    env_path = _get_hermes_home() / ".env"
    try:
        _write_env_file(env_path, {env_var: api_key})
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.exception("Failed to write env file for provider %s", provider_id)
        return {"ok": False, "error": f"Failed to save API key: {exc}"}

    # Invalidate the model cache so the dropdown refreshes on next request.
    # Using invalidate_models_cache() instead of reload_config() to avoid
    # disrupting active streaming sessions that may be reading config.cfg.
    invalidate_models_cache()

    return {
        "ok": True,
        "provider": provider_id,
        "display_name": _PROVIDER_DISPLAY.get(provider_id, provider_id),
        "action": "updated" if api_key else "removed",
    }


def remove_provider_key(provider_id: str) -> dict[str, Any]:
    """Remove the API key for a provider.

    Removes the key from ``~/.hermes/.env`` (via ``set_provider_key``)
    and also cleans up ``config.yaml`` if the key is stored there
    (``providers.<id>.api_key`` or top-level ``model.api_key`` when this
    provider is the active one).

    Returns a status dict with the operation result.
    """
    result = set_provider_key(provider_id, None)

    # Even if the .env removal succeeded, the key might also live in
    # config.yaml (e.g. providers.<id>.api_key or model.api_key).
    # Clean those up so _provider_has_key() returns False after removal.
    if result.get("ok"):
        _clean_provider_key_from_config(provider_id)

    return result


def _clean_provider_key_from_config(provider_id: str) -> None:
    """Remove provider API key entries from config.yaml.

    Handles three storage locations:
    1. ``providers.<id>.api_key`` — per-provider key
    2. ``model.api_key`` — top-level key (only if provider is active)
    3. ``custom_providers[].api_key`` — custom provider entries

    Writes back to config.yaml only if something was actually removed.
    Uses ``_cfg_lock`` to prevent TOCTOU races.
    """
    from api.config import _cfg_lock

    try:
        # Resolve through api.config at call time instead of the function imported
        # at module load. Several tests (and some profile flows) monkeypatch the
        # config module's path resolver after api.providers has already been
        # imported; using the stale imported reference can clean the wrong
        # config.yaml.
        import api.config as _config
        config_path = _config._get_config_path()
    except Exception:
        return

    if not config_path.exists():
        return

    try:
        import yaml as _yaml

        changed = False

        with _cfg_lock:
            raw = config_path.read_text(encoding="utf-8")
            cfg = _yaml.safe_load(raw)
            if not isinstance(cfg, dict):
                return

            # 1. Clean providers.<id>.api_key
            providers_cfg = cfg.get("providers", {})
            if isinstance(providers_cfg, dict):
                provider_cfg = providers_cfg.get(provider_id, {})
                if isinstance(provider_cfg, dict) and provider_cfg.get("api_key"):
                    del provider_cfg["api_key"]
                    changed = True

            # 2. Clean model.api_key — only if this provider is the active one
            model_cfg = cfg.get("model", {})
            if isinstance(model_cfg, dict) and model_cfg.get("api_key"):
                active_provider = model_cfg.get("provider")
                if active_provider and str(active_provider).strip().lower() == provider_id.lower():
                    del model_cfg["api_key"]
                    changed = True

            # 3. Clean custom_providers[].api_key
            custom_providers = cfg.get("custom_providers", [])
            if isinstance(custom_providers, list):
                for cp in custom_providers:
                    if isinstance(cp, dict):
                        cp_name = (cp.get("name") or "").strip().lower().replace(" ", "-")
                        if f"custom:{cp_name}" == provider_id or cp.get("name", "").strip().lower() == provider_id:
                            if cp.get("api_key"):
                                del cp["api_key"]
                                changed = True

            if changed:
                _save_yaml_config_file(config_path, cfg)
        # Sync in-memory cache and bust model TTL cache
        # MUST be called outside _cfg_lock to avoid deadlock:
        # _cfg_lock is a threading.Lock (non-reentrant) and
        # reload_config() also acquires _cfg_lock internally.
        if changed:
            reload_config()
    except Exception:
        logger.exception("Failed to clean provider key from config.yaml for %s", provider_id)
