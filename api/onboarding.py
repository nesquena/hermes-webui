"""Hermes Web UI -- first-run onboarding helpers."""

from __future__ import annotations

import json
import logging
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from api.auth import is_auth_enabled
from api.config import (
    DEFAULT_MODEL,
    DEFAULT_WORKSPACE,
    _FALLBACK_MODELS,
    _HERMES_FOUND,
    _PROVIDER_DISPLAY,
    _PROVIDER_MODELS,
    _get_config_path,
    get_available_models,
    get_config,
    load_settings,
    reload_config,
    save_settings,
    verify_hermes_imports,
    _save_yaml_config_file as _guarded_save_yaml_config_file,
)
from api.providers import _write_env_file  # shared impl with _ENV_LOCK (#1164)
from api.workspace import get_last_workspace, load_workspaces

logger = logging.getLogger(__name__)


_SUPPORTED_PROVIDER_SETUPS = {
    # ── Easy start ──────────────────────────────────────────────────────
    "openrouter": {
        "label": "OpenRouter",
        "env_var": "OPENROUTER_API_KEY",
        "default_model": "anthropic/claude-sonnet-4.6",
        "requires_base_url": False,
        "models": [
            {"id": model["id"], "label": model["label"]} for model in _FALLBACK_MODELS
        ],
        "category": "easy_start",
        "quick": True,
    },
    "anthropic": {
        "label": "Anthropic",
        "env_var": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4.6",
        "requires_base_url": False,
        "models": list(_PROVIDER_MODELS.get("anthropic", [])),
        "category": "easy_start",
        "oauth_provider": "anthropic",
        "oauth_label": "Claude Code OAuth",
    },
    "openai": {
        "label": "OpenAI",
        "env_var": "OPENAI_API_KEY",
        "default_model": "gpt-4o",
        "default_base_url": "https://api.openai.com/v1",
        "requires_base_url": False,
        "models": list(_PROVIDER_MODELS.get("openai", [])),
        "category": "easy_start",
    },
    # ── Open / self-hosted ─────────────────────────────────────────────
    "ollama": {
        "label": "Ollama",
        "env_var": "OLLAMA_API_KEY",
        "default_model": "qwen3:32b",
        "default_base_url": "http://localhost:11434/v1",
        "requires_base_url": True,
        # Local Ollama runs keyless by default — only Ollama Cloud requires
        # OLLAMA_API_KEY.  The wizard accepts an empty api_key for this
        # provider; users with auth enabled can still type one.  See #1499.
        "key_optional": True,
        "models": [],
        "category": "self_hosted",
    },
    "lmstudio": {
        "label": "LM Studio",
        # Canonical env var matches the agent CLI runtime (hermes_cli/auth.py:182,
        # api_key_env_vars=("LM_API_KEY",)).  Onboarding writes this name so the
        # agent runtime actually picks up the key on the next chat — pre-#1499/#1500
        # the WebUI wrote LMSTUDIO_API_KEY which the agent runtime ignored, masked
        # in practice by the LMSTUDIO_NOAUTH_PLACEHOLDER fallback for keyless installs.
        "env_var": "LM_API_KEY",
        # Legacy env var written by older WebUI builds (≤ v0.50.272).  Detection
        # paths (_provider_api_key_present here, _provider_has_key in providers.py)
        # also read this name so existing users with the old key in their .env
        # don't flip to "no key" in Settings → Providers after upgrading.
        # Onboarding only writes the canonical name going forward.
        "env_var_aliases": ["LMSTUDIO_API_KEY"],
        "default_model": "gpt-4o-mini",
        "default_base_url": "http://localhost:1234/v1",
        "requires_base_url": True,
        # Most LM Studio installs run keyless (LMSTUDIO_NOAUTH_PLACEHOLDER on the
        # agent side handles this).  The wizard accepts an empty api_key; auth-
        # enabled servers still need one but the user types it in the same field.
        # See #1499 (third sub-bug from #1420).
        "key_optional": True,
        "models": [],
        "category": "self_hosted",
    },
    "custom": {
        "label": "Custom OpenAI-compatible",
        "env_var": "OPENAI_API_KEY",
        "default_model": "gpt-4o-mini",
        "requires_base_url": True,
        # Many self-hosted OpenAI-compatible servers (vLLM, llama-server,
        # TabbyAPI, etc.) run keyless behind a private network.  The wizard
        # accepts an empty api_key — auth-protected endpoints can still
        # supply one.  See #1499.
        "key_optional": True,
        "models": [],
        "category": "self_hosted",
    },
    # ── Specialized / extended ──────────────────────────────────────────
    "gemini": {
        "label": "Google Gemini",
        "env_var": "GOOGLE_API_KEY",
        "default_model": "gemini-3.1-pro-preview",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "requires_base_url": False,
        # _PROVIDER_MODELS in api/config.py is keyed under "google" even though
        # the agent's alias map normalizes "google" → "gemini".  Use the catalog
        # key here so the wizard surfaces the actual model list.
        "models": list(_PROVIDER_MODELS.get("google", [])),
        "category": "specialized",
    },
    "deepseek": {
        "label": "DeepSeek",
        "env_var": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-v4-flash",
        "default_base_url": "https://api.deepseek.com",
        "requires_base_url": False,
        "models": list(_PROVIDER_MODELS.get("deepseek", [])),
        "category": "specialized",
    },
    "xiaomi": {
        "label": "Xiaomi MiMo",
        "env_var": "XIAOMI_API_KEY",
        "default_model": "mimo-v2.5-pro",
        "default_base_url": "https://api.xiaomimimo.com/v1",
        "requires_base_url": False,
        "models": list(_PROVIDER_MODELS.get("xiaomi", [])),
        "category": "specialized",
    },
    "zai": {
        "label": "Z.AI / GLM (智谱)",
        "env_var": "GLM_API_KEY",
        "default_model": "glm-5.1",
        "default_base_url": "https://open.bigmodel.cn/api/paas/v4",
        "requires_base_url": False,
        "models": list(_PROVIDER_MODELS.get("zai", [])),
        "category": "specialized",
    },
    "nvidia": {
        "label": "NVIDIA NIM",
        "env_var": "NVIDIA_API_KEY",
        "default_model": "nvidia/llama-3.3-nemotron-super-49b-v1.5",
        "default_base_url": "https://integrate.api.nvidia.com/v1",
        "requires_base_url": False,
        "models": list(_PROVIDER_MODELS.get("nvidia", [])),
        "category": "specialized",
    },
    "mistralai": {
        "label": "Mistral",
        "env_var": "MISTRAL_API_KEY",
        "default_model": "mistral-large-latest",
        "default_base_url": "https://api.mistral.ai/v1",
        "requires_base_url": False,
        # No catalog entry for mistralai today — wizard shows a free-text input.
        "models": list(_PROVIDER_MODELS.get("mistralai", [])),
        "category": "specialized",
    },
    "x-ai": {
        "label": "xAI (Grok)",
        "env_var": "XAI_API_KEY",
        "default_model": "grok-4.20",
        "default_base_url": "https://api.x.ai/v1",
        "requires_base_url": False,
        # Agent normalizes "x-ai" → "xai"; _PROVIDER_MODELS is also keyed "xai"
        # when populated, so check both keys for forward-compatibility.
        "models": list(_PROVIDER_MODELS.get("xai", []) or _PROVIDER_MODELS.get("x-ai", [])),
        "category": "specialized",
    },
}

_PROVIDER_CATEGORIES = [
    {"id": "easy_start", "label": "Easy start", "order": 0},
    {"id": "self_hosted", "label": "Open / self-hosted", "order": 1},
    {"id": "specialized", "label": "Specialized", "order": 2},
]

_UNSUPPORTED_PROVIDER_NOTE = (
    "Advanced provider flows such as Nous Portal and GitHub Copilot are still "
    "terminal-first. OpenAI Codex and Anthropic Claude Code can be authenticated in this onboarding flow "
    "when your Hermes config selects the corresponding provider."
)


def _get_active_hermes_home() -> Path:
    try:
        from api.profiles import get_active_hermes_home

        return get_active_hermes_home()
    except ImportError:
        return Path.home() / ".hermes"


def _load_env_file(env_path: Path) -> dict[str, str]:
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



def _load_yaml_config(config_path: Path) -> dict:
    try:
        import yaml as _yaml
    except ImportError:
        return {}

    if not config_path.exists():
        return {}
    try:
        loaded = _yaml.safe_load(config_path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _save_yaml_config(config_path: Path, config: dict) -> None:
    _guarded_save_yaml_config_file(config_path, config)


def _normalize_model_for_provider(provider: str, model: str) -> str:
    clean = (model or "").strip()
    if not clean:
        return ""
    if provider in {"anthropic", "openai"} and clean.startswith(provider + "/"):
        return clean.split("/", 1)[1]
    return clean


def _normalize_base_url(base_url: str) -> str:
    return (base_url or "").strip().rstrip("/")


# ── Provider endpoint probe (#1499) ─────────────────────────────────────────

# Probe error codes — stable strings the frontend can switch on for inline
# error rendering.  Add new codes only by extending this set; never reuse.
PROBE_ERROR_CODES = (
    "invalid_url",       # base_url failed urlparse / scheme / host check
    "dns",               # hostname did not resolve
    "connect_refused",   # TCP RST on connect (server not listening)
    "timeout",           # exceeded probe timeout
    "http_4xx",          # endpoint returned 4xx (auth required, wrong path, …)
    "http_5xx",          # endpoint returned 5xx (server-side fault)
    "parse",             # body not JSON or not the OpenAI /models shape
    "unreachable",       # other network / SSL / unknown error
)

PROBE_TIMEOUT_SECONDS = 5.0
# OpenAI /models response can list dozens of entries on Ollama / LM Studio.
# 256 KB is more than enough for any realistic catalog and bounds the worst
# case for a hostile / mis-pointed endpoint that streams forever.
PROBE_MAX_BYTES = 256 * 1024


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse to follow HTTP redirects on the probe path.

    `urllib.request.urlopen` follows redirects by default — without this
    handler, a probe at `http://example.com/v1/models` could be redirected
    to `http://internal-service:8080/admin`, surfacing internal HTTP services
    to whatever the probe targets next.  The probe is already gated behind
    WebUI auth and the local-network check, so the threat model is
    "authenticated user enumerating internal services" — same as `curl`
    from their browser DevTools.  Disabling redirects tightens defaults
    without breaking any legitimate use case (a self-hosted /models endpoint
    that 3xx-redirects is itself misconfigured).  Redirects surface to the
    caller as `unreachable` (mapped from `HTTPError(3xx)` in the probe).
    Reviewer-flagged in PR #1501 (#1499 + #1500).
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # tell urllib to NOT follow; raises HTTPError(3xx) instead


_PROBE_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def probe_provider_endpoint(
    provider: str,
    base_url: str,
    api_key: str | None = None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> dict:
    """Probe `<base_url>/models` for a self-hosted OpenAI-compatible provider.

    Used by the onboarding wizard to validate the user's configured base URL
    before persisting (#1499).  Distinguishes failure modes so the frontend
    can render a precise inline error instead of a generic "could not save."

    Returns one of:

      {"ok": True, "models": [{"id": "...", "label": "..."}, ...]}
      {"ok": False, "error": "<code>", "detail": "<human string>"}

    Where ``<code>`` is one of ``PROBE_ERROR_CODES``.

    The probe is a single HTTP GET — no retries.  The timeout is short by
    design: the wizard runs the probe synchronously on the user's submit
    click, and we'd rather report "timeout" quickly than block the UI for
    the kernel default ~75s.

    The probe response is NOT persisted.  This function returns model IDs
    so the wizard can populate its dropdown, but ``apply_onboarding_setup``
    only writes the user's typed selection — never auto-pinning a stale
    list of models to ``config.yaml``.

    SSRF: ``base_url`` is whatever the user typed in the onboarding form.
    The wizard is gated behind authentication (post-onboarding, the user
    has already authenticated to the WebUI), and the legitimate target is
    a local LM Studio / Ollama / vLLM server, so we deliberately do not
    block private-IP ranges — that would make the feature useless.  The
    risk surface is "authenticated user crafts a probe to enumerate
    internal HTTP services," which is a different threat model from
    unauthenticated SSRF.
    """
    base_url = _normalize_base_url(base_url)
    if not base_url:
        return {"ok": False, "error": "invalid_url", "detail": "base_url is required"}

    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"}:
        return {
            "ok": False,
            "error": "invalid_url",
            "detail": "base_url must start with http:// or https://",
        }
    if not parsed.hostname:
        return {"ok": False, "error": "invalid_url", "detail": "base_url has no host"}

    # Build the probe URL.  OpenAI-compatible servers expose /v1/models or
    # /models.  Most users supply a base URL ending in /v1, so we just append
    # /models to whatever they typed.  Strip the trailing slash and append
    # rather than urljoin to avoid eating the /v1 segment when there's no
    # trailing slash.
    probe_url = f"{base_url}/models"

    headers = {
        "Accept": "application/json",
        "User-Agent": "hermes-webui-onboarding-probe",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(probe_url, headers=headers, method="GET")

    try:
        with _PROBE_OPENER.open(req, timeout=timeout) as resp:
            status = resp.status
            body = resp.read(PROBE_MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        # 3xx / 4xx / 5xx with a body — categorize.  3xx happens when the
        # endpoint redirects (we refuse to follow on the probe path — see
        # _NoRedirectHandler).  Map to `unreachable` rather than introducing a
        # new error code, since a self-hosted /models endpoint that 3xx-
        # redirects is itself misconfigured.
        if 300 <= exc.code < 400:
            code = "unreachable"
            detail = (
                f"HTTP {exc.code} — endpoint returned a redirect "
                f"(probe does not follow redirects).  Point base_url at the "
                f"final URL directly."
            )
            return {"ok": False, "error": code, "detail": detail, "status": exc.code}
        code = "http_4xx" if 400 <= exc.code < 500 else "http_5xx"
        # Try to surface a useful detail (LM Studio sometimes returns text/plain).
        try:
            err_body = exc.read(2048).decode("utf-8", errors="replace").strip()
        except Exception:
            err_body = ""
        detail = f"HTTP {exc.code}"
        if err_body:
            err_first = err_body.splitlines()[0][:200]
            detail = f"{detail}: {err_first}"
        return {"ok": False, "error": code, "detail": detail, "status": exc.code}
    except urllib.error.URLError as exc:
        # Distinguish DNS / connect-refused / timeout / generic.
        reason = exc.reason
        if isinstance(reason, socket.timeout) or "timed out" in str(reason).lower():
            return {"ok": False, "error": "timeout", "detail": f"connection timed out after {timeout:g}s"}
        if isinstance(reason, socket.gaierror):
            return {
                "ok": False,
                "error": "dns",
                "detail": f"could not resolve host '{parsed.hostname}'",
            }
        if isinstance(reason, ConnectionRefusedError) or "refused" in str(reason).lower():
            port_hint = parsed.port or ("443" if parsed.scheme == "https" else "80")
            return {
                "ok": False,
                "error": "connect_refused",
                "detail": f"connection refused at {parsed.hostname}:{port_hint}",
            }
        return {"ok": False, "error": "unreachable", "detail": str(reason)[:200]}
    except (TimeoutError, socket.timeout):
        return {"ok": False, "error": "timeout", "detail": f"connection timed out after {timeout:g}s"}
    except Exception as exc:  # pragma: no cover — defensive net
        logger.debug("probe_provider_endpoint unexpected error", exc_info=True)
        return {"ok": False, "error": "unreachable", "detail": str(exc)[:200]}

    # If the response was huge, refuse to parse.  256 KB cap is generous;
    # anything bigger is likely the user pointed us at the wrong service.
    if len(body) > PROBE_MAX_BYTES:
        return {
            "ok": False,
            "error": "parse",
            "detail": f"response exceeded {PROBE_MAX_BYTES // 1024} KB cap",
        }

    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError) as exc:
        return {
            "ok": False,
            "error": "parse",
            "detail": f"response is not JSON ({exc.__class__.__name__})",
        }

    # Accept both the OpenAI shape (`{"data": [{"id": ...}, ...]}`) and the
    # bare-list shape some self-hosted servers return (`[{"id": ...}, ...]`).
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        entries = payload["data"]
    elif isinstance(payload, list):
        entries = payload
    else:
        return {
            "ok": False,
            "error": "parse",
            "detail": "response is not in OpenAI /models shape (expected {'data': [...]} or [...])",
        }

    models = []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id"):
            mid = str(entry["id"]).strip()
            if mid:
                models.append({"id": mid, "label": mid})
        elif isinstance(entry, str) and entry.strip():
            models.append({"id": entry.strip(), "label": entry.strip()})

    return {"ok": True, "models": models, "status": status}


def _extract_current_provider(cfg: dict) -> str:
    model_cfg = cfg.get("model", {})
    if isinstance(model_cfg, dict):
        provider = str(model_cfg.get("provider") or "").strip().lower()
        if provider:
            return provider
    return ""


def _extract_current_model(cfg: dict) -> str:
    model_cfg = cfg.get("model", {})
    if isinstance(model_cfg, str):
        return model_cfg.strip()
    if isinstance(model_cfg, dict):
        return str(model_cfg.get("default") or "").strip()
    return ""


def _extract_current_base_url(cfg: dict) -> str:
    model_cfg = cfg.get("model", {})
    if isinstance(model_cfg, dict):
        return _normalize_base_url(str(model_cfg.get("base_url") or ""))
    return ""


def _provider_api_key_present(
    provider: str, cfg: dict, env_values: dict[str, str]
) -> bool:
    provider = (provider or "").strip().lower()
    if not provider:
        return False

    env_var = _SUPPORTED_PROVIDER_SETUPS.get(provider, {}).get("env_var")
    if env_var and env_values.get(env_var):
        return True

    # Legacy env-var aliases (read-only fallback for env vars renamed in past
    # releases — e.g. lmstudio's LM_API_KEY canonical + LMSTUDIO_API_KEY legacy
    # in #1500).  Canonical name is what onboarding writes going forward;
    # aliases keep existing users' detection working without forcing an .env
    # rewrite.
    for alias in _SUPPORTED_PROVIDER_SETUPS.get(provider, {}).get("env_var_aliases", []) or []:
        if alias and env_values.get(alias):
            return True

    model_cfg = cfg.get("model", {})
    if isinstance(model_cfg, dict) and str(model_cfg.get("api_key") or "").strip():
        return True

    providers_cfg = cfg.get("providers", {})
    if isinstance(providers_cfg, dict):
        provider_cfg = providers_cfg.get(provider, {})
        if (
            isinstance(provider_cfg, dict)
            and str(provider_cfg.get("api_key") or "").strip()
        ):
            return True
        if provider == "custom":
            custom_cfg = providers_cfg.get("custom", {})
            if (
                isinstance(custom_cfg, dict)
                and str(custom_cfg.get("api_key") or "").strip()
            ):
                return True

    # For providers not in _SUPPORTED_PROVIDER_SETUPS (e.g. minimax-cn, deepseek,
    # xai, etc.), ask the hermes_cli auth registry — it knows every provider's env
    # var names and can check os.environ for a valid key.
    # Exclude known OAuth/token-flow providers — those are handled separately by
    # _provider_oauth_authenticated() and should not be short-circuited here.
    _known_oauth = {"openai-codex", "copilot", "copilot-acp", "qwen-oauth", "nous", "anthropic"}
    if provider not in _SUPPORTED_PROVIDER_SETUPS and provider not in _known_oauth:
        try:
            from hermes_cli.auth import get_auth_status as _gas
            status = _gas(provider)
            if isinstance(status, dict) and status.get("logged_in"):
                return True
        except Exception:
            pass

    return False



def _oauth_payload_has_token(payload: dict) -> bool:
    """Return True if an auth payload contains usable token material."""
    if not isinstance(payload, dict):
        return False

    token_fields = (
        payload,
        payload.get("tokens") if isinstance(payload.get("tokens"), dict) else {},
    )
    for candidate in token_fields:
        if not isinstance(candidate, dict):
            continue
        if any(
            str(candidate.get(key) or "").strip()
            for key in ("access_token", "refresh_token", "api_key")
        ):
            return True
    return False



def _provider_oauth_authenticated(provider: str, hermes_home: "Path") -> bool:
    """Return True if the provider has valid OAuth credentials.

    Reads the profile-scoped auth.json directly so onboarding respects the
    requested Hermes home. Known OAuth providers may store auth either in the
    legacy providers[provider_id] singleton state or in credential_pool entries
    used by current Hermes runtime auth resolution.
    """
    provider = (provider or "").strip().lower()
    provider = {"claude": "anthropic", "claude-code": "anthropic"}.get(provider, provider)
    if not provider:
        return False

    _known_oauth_providers = {"openai-codex", "copilot", "copilot-acp", "qwen-oauth", "nous", "anthropic"}
    if provider not in _known_oauth_providers:
        return False

    try:
        import json as _j

        auth_path = hermes_home / "auth.json"
        if not auth_path.exists():
            return False
        store = _j.loads(auth_path.read_text(encoding="utf-8"))

        providers_store = store.get("providers")
        if isinstance(providers_store, dict):
            state = providers_store.get(provider)
            if _oauth_payload_has_token(state):
                return True

        pool_store = store.get("credential_pool")
        if isinstance(pool_store, dict):
            entries = pool_store.get(provider)
            if isinstance(entries, list):
                for entry in entries:
                    if _oauth_payload_has_token(entry):
                        return True
                    if (
                        provider == "anthropic"
                        and isinstance(entry, dict)
                        and entry.get("auth_type") == "oauth"
                        and entry.get("source") == "claude_code_linked"
                    ):
                        return True

        return False
    except Exception:
        return False


def _status_from_runtime(cfg: dict, imports_ok: bool) -> dict:
    provider = _extract_current_provider(cfg)
    model = _extract_current_model(cfg)
    base_url = _extract_current_base_url(cfg)
    env_values = _load_env_file(_get_active_hermes_home() / ".env")

    provider_configured = bool(provider and model)
    provider_ready = False

    if provider_configured:
        meta = _SUPPORTED_PROVIDER_SETUPS.get(provider, {})
        if provider in _SUPPORTED_PROVIDER_SETUPS:
            # key_optional providers (lmstudio, ollama, custom) are ready as
            # soon as the user has saved a provider+model+base_url; an api_key
            # is allowed but not required.  The agent runtime substitutes a
            # placeholder for keyless local servers (LMSTUDIO_NOAUTH_PLACEHOLDER
            # for lmstudio, equivalent paths for ollama / custom).  See #1499
            # third sub-bug from #1420.
            if meta.get("key_optional"):
                if meta.get("requires_base_url"):
                    provider_ready = bool(base_url)
                else:
                    provider_ready = True
            else:
                # Standard wizard provider (openrouter, anthropic, openai, gemini,
                # deepseek, zai, …) — needs an api_key.  Custom historically also
                # took this branch, but is now key_optional via the meta flag.
                if meta.get("requires_base_url"):
                    provider_ready = bool(
                        base_url
                        and _provider_api_key_present(provider, cfg, env_values)
                    )
                else:
                    provider_ready = _provider_api_key_present(provider, cfg, env_values)
                if not provider_ready and meta.get("oauth_provider"):
                    provider_ready = _provider_oauth_authenticated(
                        str(meta.get("oauth_provider")), _get_active_hermes_home()
                    )
        else:
            # Unknown provider — may be an OAuth flow (openai-codex, copilot, etc.)
            # OR an API-key provider not in the quick-setup list (minimax-cn, deepseek,
            # xai, etc.).  Check both: api key presence first (covers the majority of
            # third-party providers), then OAuth auth.json.
            provider_ready = (
                _provider_api_key_present(provider, cfg, env_values)
                or _provider_oauth_authenticated(provider, _get_active_hermes_home())
            )

    chat_ready = bool(_HERMES_FOUND and imports_ok and provider_ready)

    if not _HERMES_FOUND or not imports_ok:
        state = "agent_unavailable"
        note = (
            "Hermes is not fully importable from the Web UI yet. Finish bootstrap or fix the "
            "agent install before provider setup will work."
        )
    elif chat_ready:
        state = "ready"
        provider_name = _PROVIDER_DISPLAY.get(
            provider, provider.title() if provider else "Hermes"
        )
        note = f"Hermes is minimally configured and ready to chat via {provider_name}."
    elif provider_configured:
        state = "provider_incomplete"
        if provider == "custom" and not base_url:
            note = (
                "Hermes has a saved provider/model selection but still needs the "
                "base URL and API key required to chat."
            )
        elif provider not in _SUPPORTED_PROVIDER_SETUPS:
            # OAuth / unsupported provider: avoid misleading "API key" wording.
            note = (
                f"Provider '{provider}' is configured but not yet authenticated. "
                "Run 'hermes auth' or 'hermes model' in a terminal to complete "
                "setup, then reload the Web UI."
            )
        else:
            note = (
                "Hermes has a saved provider/model selection but still needs the "
                "API key required to chat."
            )
    else:
        state = "needs_provider"
        note = "Hermes is installed, but you still need to choose a provider and save working credentials."

    return {
        "provider_configured": provider_configured,
        "provider_ready": provider_ready,
        "chat_ready": chat_ready,
        "setup_state": state,
        "provider_note": note,
        "current_provider": provider or None,
        "current_model": model or None,
        "current_base_url": base_url or None,
        "env_path": str(_get_active_hermes_home() / ".env"),
    }


def _build_setup_catalog(cfg: dict) -> dict:
    current_provider = _extract_current_provider(cfg) or "openrouter"
    current_model = _extract_current_model(cfg)
    current_base_url = _extract_current_base_url(cfg)

    providers = []
    for provider_id, meta in _SUPPORTED_PROVIDER_SETUPS.items():
        providers.append(
            {
                "id": provider_id,
                "label": meta["label"],
                "env_var": meta["env_var"],
                "default_model": meta["default_model"],
                "default_base_url": meta.get("default_base_url") or "",
                "requires_base_url": bool(meta.get("requires_base_url")),
                # #1499 (third sub-bug from #1420) — providers that may run
                # keyless (lmstudio, ollama, custom).  Frontend uses this to
                # show a "(optional)" hint and allow Continue without a key.
                "key_optional": bool(meta.get("key_optional")),
                "models": list(meta.get("models", [])),
                "category": meta.get("category", "easy_start"),
                "quick": meta.get("quick", False),
                "oauth_provider": meta.get("oauth_provider") or "",
                "oauth_label": meta.get("oauth_label") or "",
            }
        )

    # Sort providers by category order, then alphabetically within each category.
    cat_order = {c["id"]: c["order"] for c in _PROVIDER_CATEGORIES}
    providers.sort(key=lambda p: (cat_order.get(p["category"], 99), p["label"]))

    # Group providers by category for the frontend.
    categories = []
    for cat in sorted(_PROVIDER_CATEGORIES, key=lambda c: c["order"]):
        categories.append({
            "id": cat["id"],
            "label": cat["label"],
            "providers": [p["id"] for p in providers if p["category"] == cat["id"]],
        })

    # Flag whether the currently-configured provider is OAuth-based (not in the
    # API-key flow).  The frontend uses this to show a confirmation card instead
    # of a key input when the user has already authenticated via 'hermes auth'.
    current_is_oauth = (
        current_provider not in _SUPPORTED_PROVIDER_SETUPS and bool(current_provider)
    ) or _provider_oauth_authenticated(current_provider, _get_active_hermes_home())

    return {
        "providers": providers,
        "categories": categories,
        "unsupported_note": _UNSUPPORTED_PROVIDER_NOTE,
        "current_is_oauth": current_is_oauth,
        "current": {
            "provider": current_provider,
            "model": current_model
            or _SUPPORTED_PROVIDER_SETUPS.get(current_provider, {}).get(
                "default_model", ""
            ),
            "base_url": current_base_url,
        },
    }


def get_onboarding_status() -> dict:
    settings = load_settings()
    cfg = get_config()
    imports_ok, missing, errors = verify_hermes_imports()
    runtime = _status_from_runtime(cfg, imports_ok)
    workspaces = load_workspaces()
    last_workspace = get_last_workspace()
    available_models = get_available_models()

    # HERMES_WEBUI_SKIP_ONBOARDING=1 lets hosting providers (e.g. Agent37) ship
    # a pre-configured instance without the wizard blocking the first load.
    # This is an operator-level override and is honoured unconditionally —
    # the operator knows their deployment is configured; we must not second-guess
    # it by requiring chat_ready to also be true.
    skip_env = os.environ.get("HERMES_WEBUI_SKIP_ONBOARDING", "").strip()
    skip_requested = skip_env in {"1", "true", "yes"}
    auto_completed = skip_requested  # unconditional: operator says skip, we skip

    # Auto-complete for existing Hermes users: if config.yaml already exists
    # AND the provider is configured (or the system is chat_ready), treat onboarding
    # as done.  These users configured Hermes via the CLI before the Web UI existed;
    # they must never be shown the first-run wizard — it would silently overwrite their
    # config.  We use provider_configured (not chat_ready) so that users with
    # non-wizard providers (ollama-cloud, deepseek, xai, kimi, etc.) are not forced
    # through the wizard just because their provider doesn't have a detectable API key
    # — the wizard cannot represent their provider and would overwrite their config
    # with whichever wizard-supported provider they accidentally select.
    config_exists = Path(_get_config_path()).exists()

    # For providers not in the wizard's quick-setup list (e.g. ollama-cloud, deepseek,
    # xai, kimi-k2.6), the wizard can never help — it only knows how to configure
    # openrouter/anthropic/openai/google/custom.  If such a user has a configured
    # provider + model in config.yaml, showing the wizard would only confuse them
    # (or worse, let them accidentally overwrite their config with gpt-5.4-mini).
    _current_provider = str(
        (cfg.get("model", {}) or {}).get("provider", "") if isinstance(cfg.get("model"), dict)
        else ""
    ).strip().lower()
    _is_non_wizard_provider = bool(
        _current_provider and _current_provider not in _SUPPORTED_PROVIDER_SETUPS
    )

    config_auto_completed = config_exists and (
        bool(runtime.get("chat_ready"))
        or (_is_non_wizard_provider and bool(runtime.get("provider_configured")))
    )

    # Persist the flag so it survives future transient import failures (e.g. after
    # a git branch switch in the hermes-agent repo).  Without this, a CLI-configured
    # user who never ran the wizard has no onboarding_completed flag — any momentary
    # imports_ok=False during restart makes chat_ready=False, config_auto_completed=False,
    # and the wizard reappears with a broken dropdown that clobbers their config.
    #
    # Best-effort: if save_settings raises (read-only FS, disk full, permission error),
    # log and continue.  The `config_auto_completed` branch of `completed=` below still
    # returns True for this request, so the user sees the correct state — only the
    # persistence-across-restart guarantee is degraded.  Raising here would turn every
    # /api/onboarding/status call into a 500 until disk was writable, which is worse UX
    # than losing the next-restart protection.
    if config_auto_completed and not settings.get("onboarding_completed"):
        try:
            save_settings({"onboarding_completed": True})
            settings["onboarding_completed"] = True
        except Exception:
            logger.debug("Failed to persist onboarding_completed", exc_info=True)

    return {
        "completed": bool(settings.get("onboarding_completed")) or auto_completed or config_auto_completed,
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
            **runtime,
        },
        "setup": _build_setup_catalog(cfg),
        "workspaces": {
            "items": workspaces,
            "last": last_workspace,
        },
        "models": available_models,
    }


def apply_onboarding_setup(body: dict) -> dict:
    # Hard guard: if the operator set SKIP_ONBOARDING, the wizard should never
    # have appeared.  Even if the frontend somehow calls this endpoint anyway
    # (e.g. a stale JS bundle or a curious user), we must not overwrite the
    # operator's config.yaml or .env files.  Just mark onboarding complete and
    # return the current status — no file writes.
    skip_env = os.environ.get("HERMES_WEBUI_SKIP_ONBOARDING", "").strip()
    if skip_env in {"1", "true", "yes"}:
        save_settings({"onboarding_completed": True})
        return get_onboarding_status()

    provider = str(body.get("provider") or "").strip().lower()
    model = str(body.get("model") or "").strip()
    api_key = str(body.get("api_key") or "").strip()
    base_url = _normalize_base_url(str(body.get("base_url") or ""))

    if provider not in _SUPPORTED_PROVIDER_SETUPS:
        # Unsupported providers (openai-codex, copilot, nous, etc.) are already
        # configured via the CLI. Just mark onboarding as complete and let the
        # user through — the agent is already set up, no further setup needed.
        save_settings({"onboarding_completed": True})
        return get_onboarding_status()
    if not model:
        raise ValueError("model is required")

    provider_meta = _SUPPORTED_PROVIDER_SETUPS[provider]
    if provider_meta.get("requires_base_url"):
        if not base_url:
            raise ValueError("base_url is required for custom endpoints")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("base_url must start with http:// or https://")

    config_path = _get_config_path()
    # Guard: if config.yaml already exists and the caller did not explicitly
    # acknowledge the overwrite, refuse to proceed.  The frontend must pass
    # confirm_overwrite=True after showing the user a confirmation step.
    if Path(config_path).exists() and not body.get("confirm_overwrite"):
        return {
            "error": "config_exists",
            "message": (
                "Hermes is already configured (config.yaml exists). "
                "Pass confirm_overwrite=true to overwrite it."
            ),
            "requires_confirm": True,
        }

    cfg = _load_yaml_config(config_path)
    env_path = _get_active_hermes_home() / ".env"
    env_values = _load_env_file(env_path)

    if not api_key and not _provider_api_key_present(provider, cfg, env_values):
        # Providers that may run keyless (lmstudio, ollama, custom — gated by
        # `key_optional` in _SUPPORTED_PROVIDER_SETUPS) are allowed to onboard
        # with no api_key. OAuth-capable wizard providers (currently Anthropic
        # via Claude Code) are also allowed once their server-side OAuth/link
        # marker is present.
        oauth_ready = bool(provider_meta.get("oauth_provider")) and _provider_oauth_authenticated(
            str(provider_meta.get("oauth_provider")), _get_active_hermes_home()
        )
        if not provider_meta.get("key_optional") and not oauth_ready:
            raise ValueError(f"{provider_meta['env_var']} is required")

    model_cfg = cfg.get("model", {})
    if not isinstance(model_cfg, dict):
        model_cfg = {}

    model_cfg["provider"] = provider
    model_cfg["default"] = _normalize_model_for_provider(provider, model)

    if provider_meta.get("requires_base_url"):
        model_cfg["base_url"] = base_url
    elif provider_meta.get("default_base_url"):
        model_cfg["base_url"] = provider_meta["default_base_url"]
    else:
        model_cfg.pop("base_url", None)

    cfg["model"] = model_cfg
    _save_yaml_config(config_path, cfg)

    if api_key:
        _write_env_file(env_path, {provider_meta["env_var"]: api_key})

    # Reload the hermes_cli provider/config cache so the next streaming call
    # picks up the new key without requiring a server restart.
    try:
        from api.profiles import _reload_dotenv
        _reload_dotenv(_get_active_hermes_home())
    except Exception:
        logger.debug("Failed to reload dotenv")

    # Belt-and-braces: set directly on os.environ AFTER _reload_dotenv so the
    # value survives even if _reload_dotenv cleared it (e.g. when _write_env_file
    # wrote to disk but the profile isolation tracking hasn't seen it yet).
    if api_key:
        os.environ[provider_meta["env_var"]] = api_key

    try:
        # hermes_cli may cache config at import time; ask it to reload if possible.
        from hermes_cli.config import reload as _cli_reload
        _cli_reload()
    except Exception:
        logger.debug("Failed to reload hermes_cli config")

    reload_config()
    return get_onboarding_status()


def complete_onboarding() -> dict:
    save_settings({"onboarding_completed": True})
    return get_onboarding_status()
