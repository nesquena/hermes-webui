"""HermesOS Cloud fork patches for ``api/config.py`` — extension-module form.

This module reproduces every fork-marker patch that previously lived inline
inside ``api/config.py`` as runtime extensions, so that:

  * Daily ``track-upstream`` rebases land cleanly — we no longer touch the
    same lines upstream is editing.
  * Adding a new fork patch is mechanical: drop an ``update()`` /
    ``add()`` / wrapper into this file. No fork-marker block in upstream
    files.
  * If upstream renames or restructures something we patch, this module
    raises at import time and the smoke-test in CI catches it on the
    ``:stable`` rebuild — we get notified before a broken image rolls to
    the fleet.

It MUST be imported after ``api.config`` has finished initializing. The
import hook lives at the bottom of ``api/__init__.py`` so any module that
does ``from api import config`` gets the patched module transparently.

How to add a new fork patch
---------------------------

  * Adding an entry to an existing dict / set: call ``.update()`` /
    ``.add()`` on the module attribute (see "ADDITIVE DATA" section).
  * Defining a new helper function or new module-level constant: define
    it here and assign back to upstream via ``_u.<name> = <function>``
    so other fork code (and our wrappers) can call it through the
    upstream namespace.
  * Changing the behaviour of an existing function: capture the original
    reference, define a wrapper that calls it, then assign the wrapper
    back to the upstream module (see "WRAPPERS" section).
"""

from __future__ import annotations

from . import config as _u


# ───────────────────────────────────────────────────────────────────────
# ADDITIVE DATA
# ───────────────────────────────────────────────────────────────────────

# Was: surgical edit inside ``_PROVIDER_DISPLAY = { ... }``
_u._PROVIDER_DISPLAY.update({
    "venice": "Venice",
    "crof": "CrofAI",
    "bankr": "Bankr",
    "xiaomi": "Xiaomi MiMo",
    "cometapi": "CometAPI",
    "groq": "Groq",
})

# Was: surgical edit inside ``_PROVIDER_MODELS = { ... }``. Empty lists are
# intentional — each provider's real lineup is fetched live from /v1/models
# when the user clicks "Refresh models" in Settings. Hardcoding lists here
# only invites drift.
_u._PROVIDER_MODELS.update({
    "venice": [],
    "crof": [],
    "bankr": [],
    "xiaomi": [],
    "cometapi": [],
    "groq": [],
})

# Was: surgical edit inside ``_SETTINGS_SKIN_VALUES = { ... }``. Without
# these, /api/claude-config autosave normalises HermesOS-skinned users to
# "default" on every config save.
_u._SETTINGS_SKIN_VALUES.add("hermesos")
_u._SETTINGS_SKIN_VALUES.add("sienna")


# ───────────────────────────────────────────────────────────────────────
# NEW HELPER: built-in base-URL → canonical provider-slug map
# ───────────────────────────────────────────────────────────────────────
#
# Without this table any user pointing OPENAI_BASE_URL at a known
# OpenAI-compatible aggregator (CrofAI, CometAPI, Venice, etc.) sees their
# entire model list labelled "Custom" in the picker AND every model row
# tagged with the generic "Custom" chip — because upstream's slug-resolver
# only looks at config.yaml's ``custom_providers:`` entries. Users would
# have to hand-write a custom_providers block in YAML to get a friendly
# name.
#
# This table is the auto-detect fallback consulted AFTER the config.yaml
# lookup misses. Hostnames are normalised to lowercase and matched against
# the substring of the request URL's netloc (so "api.crof.ai" and
# "crof.ai" both hit the "crof" entry — no per-subdomain enumeration
# needed).
#
# Keys are substrings; values must be a canonical id present in
# ``_PROVIDER_DISPLAY`` above so the group inherits a friendly display
# name.
_BUILTIN_BASE_URL_PROVIDERS = (
    ("crof.ai",              "crof"),
    ("venice.ai",            "venice"),
    ("bankr.com",            "bankr"),
    ("cometapi.com",         "cometapi"),
    ("openrouter.ai",        "openrouter"),
    ("api.anthropic.com",    "anthropic"),
    ("api.openai.com",       "openai"),
    ("api.groq.com",         "groq"),
    ("api.deepseek.com",     "deepseek"),
    ("api.minimaxi.com",     "minimax"),
    ("api.moonshot.cn",      "kimi-coding"),
    ("api.together.xyz",     "together"),
    ("api.fireworks.ai",     "fireworks"),
)


def _builtin_provider_slug_for_base_url(base_url: object) -> str:
    """Resolve a base_url to a canonical provider slug using the built-in
    aggregator dictionary. Returns "" if no entry matches.

    Substring match against the URL's host so "https://api.crof.ai/v1" and
    "https://crof.ai/v1" both resolve to "crof".
    """
    target = _u._normalize_base_url_for_match(base_url)
    if not target:
        return ""
    target_lower = target.lower()
    for hostname_fragment, slug in _BUILTIN_BASE_URL_PROVIDERS:
        if hostname_fragment in target_lower:
            return slug
    return ""


# Expose on the upstream module so other code (including the wrappers
# below) can call it through the natural ``api.config`` namespace.
_u._BUILTIN_BASE_URL_PROVIDERS = _BUILTIN_BASE_URL_PROVIDERS
_u._builtin_provider_slug_for_base_url = _builtin_provider_slug_for_base_url


# ───────────────────────────────────────────────────────────────────────
# WRAPPER: ``_named_custom_provider_slug_for_base_url``
# ───────────────────────────────────────────────────────────────────────
#
# Was: surgical edit inside the function body. We add an
# ``include_builtin_fallback`` keyword so the runtime auth path
# (resolve_model_provider via _resolve_configured_provider_id with
# resolve_alias=False) can opt out — without that knob, a user whose
# ``OPENAI_BASE_URL`` matches our built-in table would have their request
# authenticated against, say, CROF_API_KEY (empty) instead of OPENAI_API_KEY
# (the agent's "custom" provider env var). UI-only fallback keeps the
# friendly slug in the dropdown without touching auth.
_orig_named_custom_provider_slug_for_base_url = _u._named_custom_provider_slug_for_base_url


def _wrapped_named_custom_provider_slug_for_base_url(
    base_url,
    config_obj=None,
    *,
    include_builtin_fallback: bool = True,
) -> str:
    """Upstream signature is ``(base_url, config_obj=None)``. We extend it
    with the ``include_builtin_fallback`` keyword so the runtime auth path
    (resolve_model_provider via _resolve_configured_provider_id with
    resolve_alias=False) can opt out of the built-in table lookup.
    """
    result = _orig_named_custom_provider_slug_for_base_url(base_url, config_obj)
    if result:
        return result
    if include_builtin_fallback:
        return _builtin_provider_slug_for_base_url(base_url) or ""
    return ""


_u._named_custom_provider_slug_for_base_url = (
    _wrapped_named_custom_provider_slug_for_base_url
)


# ───────────────────────────────────────────────────────────────────────
# WRAPPER: ``resolve_model_provider``
# ───────────────────────────────────────────────────────────────────────
#
# Was: surgical edit inside the ``@provider:model`` branch of the function
# body. The model dropdown groups labelled "CrofAI" / "Venice" / etc.
# (because the user's ``model.base_url`` matches our built-in table) emit
# model IDs like ``@crof:<model>`` from the frontend. Upstream would
# return those as ``provider_hint="crof"``, which then fails auth at
# hermes-agent because its PROVIDER_REGISTRY doesn't have "crof". We
# post-process the upstream return value and translate built-in slugs
# back to ``provider="custom"`` + the user's configured base_url so the
# request authenticates against OPENAI_API_KEY (the agent's "custom"
# provider env var).
#
# Only fires when the user's actual config is ``provider: custom`` — an
# explicit ``provider: openrouter`` user routing via ``@crof:some-model``
# still gets respected.
_orig_resolve_model_provider = _u.resolve_model_provider


def _wrapped_resolve_model_provider(model_id: str) -> tuple:
    result = _orig_resolve_model_provider(model_id)
    if not (isinstance(result, tuple) and len(result) == 3):
        return result
    bare_model, provider_hint, base_url_override = result

    # protect runtime from @<built-in-slug>: leak
    # Only intercept when upstream returned a bare provider hint with no
    # base_url override. If upstream already routed through a
    # ``custom_providers:`` entry it set base_url_override — leave it.
    if base_url_override is not None:
        return result
    if not isinstance(provider_hint, str) or not provider_hint:
        return result

    cfg = getattr(_u, "cfg", None)
    model_cfg = cfg.get("model", {}) if isinstance(cfg, dict) else {}
    if not isinstance(model_cfg, dict):
        return result
    config_base_url = (model_cfg.get("base_url") or "").strip()
    if not config_base_url:
        return result

    # Use the same resolver upstream uses (resolve_alias=False keeps it
    # honest to what's actually in config.yaml).
    config_provider = _u._resolve_configured_provider_id(
        model_cfg.get("provider"),
        cfg,
        base_url=config_base_url,
        resolve_alias=False,
    )
    if isinstance(config_provider, str) and config_provider.strip().lower() == "local":
        config_provider = "custom"

    if (
        config_provider == "custom"
        and provider_hint == _builtin_provider_slug_for_base_url(config_base_url)
    ):
        return bare_model, "custom", config_base_url

    return result


_u.resolve_model_provider = _wrapped_resolve_model_provider
